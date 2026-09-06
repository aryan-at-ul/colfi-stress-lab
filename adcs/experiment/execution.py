"""Live-only execution of approved v5 initial run cells."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Callable

from pydantic import ValidationError

from adcs.llm.client import LLM, LLMError, _extract_json

from .models import (
    ExperimentPreregistration,
    InstitutionDecisionV5,
    ProviderDecisionV5,
    canonical_json,
    content_sha256,
)
from .store import ExperimentStore


INITIAL_SYSTEM_PROMPT = """You are an institutional risk-decision agent in a governed
counterfactual stress experiment. Use only the frozen common evidence and the one
private institution state supplied by the user. Make an independent decision; do not
guess other institutions' behavior and do not follow any example action path. Respect
the declared mandate and permitted actions. Return one JSON object matching the
provided schema, with no markdown or commentary outside JSON."""

INITIAL_MAX_OUTPUT_TOKENS = 6144


class InitialExecutionConflict(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request_json(model_id: str, system: str, user: str,
                  temperature: float | None, max_tokens: int) -> str:
    # This is deliberately identical to adcs.llm.client._prompt_hash's payload.
    return json.dumps(
        [model_id, system, user, temperature, max_tokens],
        separators=(",", ":"),
    )


def _validate_decision_scope(
    decision: InstitutionDecisionV5,
    *,
    case_input: dict,
    plan: ExperimentPreregistration,
) -> None:
    evidence_ids = {
        item["evidence_id"] for item in case_input["common_evidence"]
    }
    feedback = case_input.get("feedback_context") or {}
    evidence_ids.update(
        item["evidence_id"]
        for item in feedback.get("common_market_state", {}).get("markets", [])
        if item.get("evidence_id")
    )
    private_evidence = feedback.get("own_private_state", {}).get("state_evidence_id")
    if private_evidence:
        evidence_ids.add(private_evidence)
    cited = set(decision.evidence_ids)
    cited.update(
        evidence_id
        for action in decision.actions
        for evidence_id in action.evidence_ids
    )
    unknown_evidence = sorted(cited - evidence_ids)
    if unknown_evidence:
        raise ValueError(
            "decision cites evidence outside the frozen snapshot: "
            + ", ".join(unknown_evidence)
        )

    permitted = set(case_input["private_institution"]["permitted_actions"])
    unpermitted = sorted({action.action_type for action in decision.actions} - permitted)
    if unpermitted:
        raise ValueError("unpermitted action types: " + ", ".join(unpermitted))

    allowed_instruments = set(plan.instrument_ids)
    referenced_instruments: set[str] = set()
    for action in decision.actions:
        for attribute in (
            "instrument_id",
            "hedge_instrument_id",
            "market_instrument_id",
        ):
            value = getattr(action, attribute, None)
            if value:
                referenced_instruments.add(value)
        referenced_instruments.update(getattr(action, "protects_instrument_ids", []))
        referenced_instruments.update(getattr(action, "preferred_instrument_ids", []))
    unknown_instruments = sorted(referenced_instruments - allowed_instruments)
    if unknown_instruments:
        raise ValueError(
            "decision references instruments outside the preregistration: "
            + ", ".join(unknown_instruments)
        )
    holdings = {
        item["instrument_id"]
        for item in case_input["private_institution"]["holdings"]
        if item.get("mapping_status") == "approved"
    }
    hedge_instruments = {
        item.instrument_id
        for item in plan.transition_policy.instrument_assumptions
        if item.instrument_kind == "fx" or item.instrument_id == "vix"
    }
    profile_id = case_input["private_institution"]["profile_version_id"]
    liquidity_markets = {
        item.instrument_id
        for item in plan.transition_policy.liquidity_provision
        if item.institution_profile_version_id == profile_id
    }
    for action in decision.actions:
        if action.action_type in {"sell", "buy_support"} and (
            action.instrument_id not in holdings
        ):
            raise ValueError("direct trades must target a declared holding")
        if action.action_type == "hedge":
            if action.hedge_instrument_id not in hedge_instruments:
                raise ValueError("hedge instrument is not preregistered for hedging")
            if not set(action.protects_instrument_ids) <= holdings:
                raise ValueError("hedge can protect only declared holdings")
        if action.action_type == "deleverage" and not set(
            action.preferred_instrument_ids
        ) <= holdings:
            raise ValueError("deleveraging preference is outside declared holdings")
        if action.action_type == "withdraw_liquidity" and (
            action.market_instrument_id not in liquidity_markets
        ):
            raise ValueError("institution has no preregistered depth allocation")


def _execute_run_cell(
    store: ExperimentStore,
    plan: ExperimentPreregistration,
    cell: dict,
    *,
    stage: str,
    actor: str,
    llm_factory: Callable[..., LLM] = LLM,
    max_schema_attempts: int = 3,
) -> InstitutionDecisionV5:
    """Make fresh provider calls until one response passes the v5 contract."""

    case_input = json.loads(cell["case_input_json"])
    if canonical_json(case_input) != cell["case_input_json"] or content_sha256(
        case_input
    ) != cell["case_input_sha256"]:
        raise ValueError("stored case input failed its integrity check")
    if not store.approvals_ready(
        cell["preregistration_id"], cell["evidence_snapshot_id"]
    )["ready"]:
        raise PermissionError("current plan and evidence approvals are required")
    if case_input.get("decision_request", {}).get("stage") != stage:
        raise ValueError("case-input stage does not match the run cell")
    record_attempt = (
        store.record_initial_attempt
        if stage == "initial" else store.record_feedback_attempt
    )
    base_user_payload = {
        "case_input": case_input,
        "output_schema": ProviderDecisionV5.model_json_schema(),
    }
    llm = llm_factory(mode="live")
    validation_error = ""
    previous = ""

    for _ in range(max_schema_attempts):
        user_payload = dict(base_user_payload)
        if validation_error:
            user_payload["repair"] = {
                "instruction": (
                    "Correct the validation error. Return a new complete JSON object."
                ),
                "validation_error": validation_error[:1600],
                "previous_response": previous[:2000],
            }
        user = canonical_json(user_payload)
        stats = llm.context_stats(
            cell["model_spec_id"],
            INITIAL_SYSTEM_PROMPT,
            user,
            INITIAL_MAX_OUTPUT_TOKENS,
        )
        effective_tokens = stats["effective_output_tokens"]
        request_json = _request_json(
            cell["model_spec_id"],
            INITIAL_SYSTEM_PROMPT,
            user,
            None,
            effective_tokens,
        )
        request_sha = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        started_at = _utc_now()
        try:
            completion = llm.complete(
                cell["model_spec_id"],
                INITIAL_SYSTEM_PROMPT,
                user,
                temperature=None,
                max_tokens=INITIAL_MAX_OUTPUT_TOKENS,
            )
        except LLMError as exc:
            record_attempt(
                run_cell_id=cell["id"],
                actor=actor,
                started_at=started_at,
                completed_at=_utc_now(),
                outcome="transport_error",
                provider_request_sha256=request_sha,
                provider_request_json=request_json,
                raw_response_text=None,
                latency_ms=None,
                finish_reason=None,
                usage=None,
                error=f"{type(exc).__name__}: {exc}"[:2000],
                decision=None,
            )
            raise

        previous = completion.text
        completed_at = _utc_now()
        try:
            parsed = _extract_json(completion.text)
            if parsed is None:
                raise ValueError("provider response is not a JSON object")
            body = ProviderDecisionV5.model_validate(parsed)
            decision_id = "DEC-" + hashlib.sha256(
                cell["id"].encode("utf-8")
            ).hexdigest()[:20].upper()
            decision = body.attach_run_metadata(
                decision_id=decision_id,
                institution_id=cell["institution_id"],
                stage=stage,
            )
            _validate_decision_scope(decision, case_input=case_input, plan=plan)
        except (ValidationError, ValueError) as exc:
            validation_error = f"{type(exc).__name__}: {exc}"
            record_attempt(
                run_cell_id=cell["id"],
                actor=actor,
                started_at=started_at,
                completed_at=completed_at,
                outcome="rejected",
                provider_request_sha256=completion.prompt_sha256,
                provider_request_json=request_json,
                raw_response_text=completion.text,
                latency_ms=completion.latency_ms,
                finish_reason=completion.finish_reason,
                usage=completion.usage,
                error=validation_error[:2000],
                decision=None,
            )
            continue

        record_attempt(
            run_cell_id=cell["id"],
            actor=actor,
            started_at=started_at,
            completed_at=completed_at,
            outcome="accepted",
            provider_request_sha256=completion.prompt_sha256,
            provider_request_json=request_json,
            raw_response_text=completion.text,
            latency_ms=completion.latency_ms,
            finish_reason=completion.finish_reason,
            usage=completion.usage,
            error=None,
            decision=decision,
        )
        return decision

    raise LLMError(
        f"{cell['model_spec_id']}: response failed v5 validation after "
        f"{max_schema_attempts} fresh calls: {validation_error[:800]}"
    )


def execute_initial_cell(
    store: ExperimentStore,
    plan: ExperimentPreregistration,
    cell: dict,
    *,
    actor: str,
    llm_factory: Callable[..., LLM] = LLM,
    max_schema_attempts: int = 3,
) -> InstitutionDecisionV5:
    return _execute_run_cell(
        store,
        plan,
        cell,
        stage="initial",
        actor=actor,
        llm_factory=llm_factory,
        max_schema_attempts=max_schema_attempts,
    )


def execute_feedback_cell(
    store: ExperimentStore,
    plan: ExperimentPreregistration,
    cell: dict,
    *,
    actor: str,
    llm_factory: Callable[..., LLM] = LLM,
    max_schema_attempts: int = 3,
) -> InstitutionDecisionV5:
    return _execute_run_cell(
        store,
        plan,
        cell,
        stage="feedback",
        actor=actor,
        llm_factory=llm_factory,
        max_schema_attempts=max_schema_attempts,
    )


class InitialExecutionEngine:
    """Single-process background worker; all durable progress lives in SQLite."""

    def __init__(self, store: ExperimentStore):
        self.store = store
        self._running: set[str] = set()
        self._lock = threading.RLock()

    def is_running(self, preregistration_id: str) -> bool:
        with self._lock:
            return preregistration_id in self._running

    def start(self, plan: ExperimentPreregistration, *, actor: str,
              confirmed_plan_sha256: str, max_cells: int) -> dict:
        summary = self.store.begin_initial_execution(
            plan.preregistration_id,
            actor=actor,
            confirmed_plan_sha256=confirmed_plan_sha256,
            max_cells=max_cells,
        )
        with self._lock:
            if plan.preregistration_id in self._running:
                raise InitialExecutionConflict("initial execution is already running")
            self._running.add(plan.preregistration_id)

        def run() -> None:
            processed = 0
            error = None
            try:
                for _ in range(max_cells):
                    cell = self.store.next_pending_initial_cell(plan.preregistration_id)
                    if cell is None:
                        break
                    approvals = self.store.approvals_ready(
                        plan.preregistration_id, cell["evidence_snapshot_id"]
                    )
                    if not approvals["ready"]:
                        raise PermissionError(
                            "an approval was withdrawn; execution stopped before the next call"
                        )
                    execute_initial_cell(self.store, plan, cell, actor=actor)
                    processed += 1
            except Exception as exc:  # durable failure is recorded for operator review
                error = f"{type(exc).__name__}: {exc}"[:2000]
            finally:
                self.store.finish_initial_execution(
                    plan.preregistration_id,
                    actor=actor,
                    processed_cells=processed,
                    error=error,
                )
                with self._lock:
                    self._running.discard(plan.preregistration_id)

        threading.Thread(
            target=run,
            daemon=True,
            name=f"v5-initial-{plan.preregistration_id}",
        ).start()
        return {**summary, "running": True, "authorized_max_cells": max_cells}


class FeedbackExecutionEngine:
    """Resumable live worker for the 420 isolated world-feedback decisions."""

    def __init__(self, store: ExperimentStore):
        self.store = store
        self._running: set[str] = set()
        self._lock = threading.RLock()

    def is_running(self, preregistration_id: str) -> bool:
        with self._lock:
            return preregistration_id in self._running

    def start(self, plan: ExperimentPreregistration, *, actor: str,
              confirmed_plan_sha256: str, max_cells: int) -> dict:
        summary = self.store.begin_feedback_execution(
            plan.preregistration_id,
            actor=actor,
            confirmed_plan_sha256=confirmed_plan_sha256,
            max_cells=max_cells,
        )
        with self._lock:
            if plan.preregistration_id in self._running:
                raise InitialExecutionConflict("feedback execution is already running")
            self._running.add(plan.preregistration_id)

        def run() -> None:
            processed = 0
            error = None
            try:
                for _ in range(max_cells):
                    cell = self.store.next_pending_feedback_cell(
                        plan.preregistration_id
                    )
                    if cell is None:
                        break
                    if not self.store.approvals_ready(
                        plan.preregistration_id, cell["evidence_snapshot_id"]
                    )["ready"]:
                        raise PermissionError(
                            "an approval was withdrawn; feedback stopped before the next call"
                        )
                    execute_feedback_cell(self.store, plan, cell, actor=actor)
                    processed += 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:2000]
            finally:
                self.store.finish_feedback_execution(
                    plan.preregistration_id,
                    actor=actor,
                    processed_cells=processed,
                    error=error,
                )
                with self._lock:
                    self._running.discard(plan.preregistration_id)

        threading.Thread(
            target=run,
            daemon=True,
            name=f"v5-feedback-{plan.preregistration_id}",
        ).start()
        return {**summary, "running": True, "authorized_max_cells": max_cells}
