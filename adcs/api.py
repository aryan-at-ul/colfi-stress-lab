"""COLFI institution-agent stress-testing API."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from datetime import date
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config  # noqa: F401 - imports .env into the process environment
from .experiment.catalog import (
    V5_INSTITUTION_PROFILES,
    build_confirmatory_preregistration,
    profile_catalog_readiness,
)
from .experiment.execution import (
    FeedbackExecutionEngine,
    InitialExecutionConflict,
    InitialExecutionEngine,
)
from .experiment.feedback import build_feedback_run_cells
from .experiment.metrics import (
    compute_feedback_trajectories,
    compute_initial_estimates,
)
from .experiment.models import (
    EvidenceApprovalRequest,
    EvidenceSnapshotV5,
    ExperimentPreregistration,
    FeedbackExecutionRequest,
    InitialExecutionRequest,
    InitialRunPlanRequest,
    InstitutionProfileV5,
    MetricComputationRequest,
    ModelRegistration,
    PreregistrationApprovalRequest,
    WorldPlanRequest,
    WorldTransitionRequest,
)
from .experiment.runner import build_initial_run_cells
from .experiment.store import ExperimentStore, ImmutableConflict
from .experiment.transition import (
    transition_feedback_world,
    transition_initial_world,
)
from .experiment.worlds import build_feedback_worlds
from .live.catalog import INSTITUTIONS, KNOWN_EVENTS, TAXONOMY
from .live.engine import AssessmentEngine, WorkflowConflict
from .live.models import (
    ApprovalRequest,
    AssessmentRequest,
    PortfolioHoldingSelection,
    SimulationSettings,
    WORKFLOW_STEPS,
)
from .live.results import build_demo_result
from .live.sources import INSTRUMENTS, SOURCE_CATALOG
from .live.store import LiveStore
from .llm.client import provider_is_configured


app = FastAPI(
    title="COLFI Institution-Agent Stress Lab",
    version="4.1",
    description=(
        "Human-governed stress evaluation of real institution-assigned AI agents "
        "using runtime market evidence and supervisor-generated assessment suites."
    ),
)


class AssessmentPageSize(IntEnum):
    ten = 10
    twenty_five = 25
    fifty = 50


WEB = Path(__file__).resolve().parent.parent / "web"
DB_PATH = os.environ.get("ADCS_LIVE_DB", "adcs_live.sqlite3")
STORE = LiveStore(DB_PATH)
V5_STORE = ExperimentStore(DB_PATH)
V5_EXECUTION = InitialExecutionEngine(V5_STORE)
V5_FEEDBACK_EXECUTION = FeedbackExecutionEngine(V5_STORE)
ENGINE = AssessmentEngine(STORE)


def _models() -> list[str]:
    configured = [
        value.strip()
        for value in os.environ.get("ADCS_AVAILABLE_MODELS", "").split(",")
        if value.strip()
    ]
    if configured:
        return list(dict.fromkeys(configured))
    values = [
        os.environ.get("DEEPSEEK_MODEL", "").strip(),
        os.environ.get("DEEPSEEK_REASONER_MODEL", "").strip(),
    ]
    return list(dict.fromkeys(f"deepseek:{value}" for value in values if value))


def _model_rotation(models: list[str] | None = None) -> list[str]:
    available = models if models is not None else _models()
    configured = [
        value.strip()
        for value in os.environ.get("ADCS_DEFAULT_MODEL_ROTATION", "").split(",")
        if value.strip()
    ]
    return [value for value in configured if value in available] or available


def _ensure_v5_foundation() -> str | None:
    for profile in V5_INSTITUTION_PROFILES.values():
        V5_STORE.register_profile(profile)
    models = _models()
    if len(models) != 6 or not profile_catalog_readiness()["ready"]:
        return None
    for model_id in models:
        provider, exact_model = model_id.split(":", 1)
        V5_STORE.register_model(ModelRegistration(
            model_id=model_id,
            provider=provider,
            exact_model=exact_model,
        ))
    roster = hashlib.sha256("\n".join(models).encode()).hexdigest()[:10].upper()
    plan = build_confirmatory_preregistration(
        models,
        preregistration_id=f"PREREG-AUG2024-M3C-{roster}",
    )
    V5_STORE.create_preregistration(plan)
    return plan.preregistration_id


V5_DEFAULT_PREREGISTRATION_ID = _ensure_v5_foundation()


def _get(assessment_id: str) -> dict:
    row = STORE.get(assessment_id)
    if not row or "institutions" not in row.get("request", {}):
        raise HTTPException(404, f"assessment {assessment_id} not found")
    return row


def _workflow(row: dict, events: list[dict]) -> list[dict]:
    state = {step: "pending" for step, _ in WORKFLOW_STEPS}
    state["configure"] = "complete"
    messages: dict[str, str] = {}
    for event in events:
        step = event["step"]
        if step not in state:
            continue
        if event["kind"] == "step_started":
            state[step] = "running"
        elif event["kind"] == "step_complete":
            state[step] = "complete"
        elif event["kind"] == "step_failed":
            state[step] = "failed"
        elif event["kind"] == "approval_required":
            state[step] = "waiting"
        messages[step] = event["message"]
    request = row["request"]
    plan = row.get("plan") or {}
    if (
        request.get("include_safeguard") is False
        or (plan and not any(
            case.get("condition") == "safeguarded"
            for case in plan.get("cases", [])
        ))
    ):
        state["run_safeguarded"] = "skipped"
    if row["status"] == "rejected" and row["current_step"] in state:
        state[row["current_step"]] = "rejected"
    current_step = row.get("current_step")
    step_ids = [step for step, _ in WORKFLOW_STEPS]
    if current_step in state and row["status"] in {"running", "failed"}:
        current_index = step_ids.index(current_step)
        for later_step in step_ids[current_index + 1:]:
            state[later_step] = "pending"
        state[current_step] = row["status"]
    labels = dict(WORKFLOW_STEPS)
    if request.get("execution_mode") == "deterministic_stress_rules":
        labels.update({
            "run_control": "Check pre-stress baseline signals",
            "run_stress": "AI agents flag market stress",
            "run_safeguarded": "Apply the daily portfolio selling cap",
            "score": "Calculate before-and-after impact",
            "synthesise": "Generate the result page",
        })
    return [
        {
            "id": step,
            "label": labels[step],
            "status": state[step],
            "message": messages.get(step, ""),
        }
        for step, _ in WORKFLOW_STEPS
    ]


def _view(row: dict) -> dict:
    events = STORE.events(row["id"])
    approvals = STORE.approvals(row["id"])
    metrics = row.get("metrics") or {}
    legacy_result = bool(metrics) and not metrics.get("execution_simulation")
    visible_row = dict(row)
    if legacy_result and row.get("report"):
        visible_row["report"] = {
            "withdrawn": True,
            "title": "Archived recommendation-only report withdrawn",
            "executive_summary": (
                "This report predates the paired execution and dated-evidence "
                "contracts and is not valid safeguard-impact evidence."
            ),
            "findings": [], "institution_findings": [],
            "limitations": ["Rerun under schema v4."],
            "evidence_ids": [], "response_ids": [],
            "model": "archived", "prompt_sha256": None, "cached": False,
        }
    return {
        **visible_row,
        "schema_version": row.get("request", {}).get(
            "schema_version", 3 if legacy_result else 4
        ),
        "result_validity": {
            "valid": not legacy_result,
            "status": "withdrawn_legacy_metrics" if legacy_result else "current",
        },
        "workflow": _workflow(row, events),
        "approvals": approvals,
        "activity": events,
        "demo_result": build_demo_result(row, approvals, events),
        "runtime_contract": {
            "institutions": (
                "Portfolios are declared sandbox inputs from the COLFI brief, "
                "not claims about actual firms."
            ),
            "agents": (
                "Every institution run is a fresh provider-backed LangGraph agent "
                "that executes the required analytical tools in an isolated case pack."
            ),
            "evidence": (
                "Every market source retains its complete dated in-window path, "
                "pre-window reference, exact acquisition URL and response hash."
            ),
            "scores": (
                "Agent intent is scored separately from deterministic execution. "
                "Safeguards reuse paired stress intent; all effect metrics expose "
                "raw values, formulas and versioned assumptions."
            ),
        },
    }


def _normalise_request(request: AssessmentRequest) -> AssessmentRequest:
    deterministic = request.execution_mode == "deterministic_stress_rules"
    if (
        deterministic
        and {item.institution_id for item in request.institutions}
        != set(INSTITUTIONS)
    ):
        raise HTTPException(422, {
            "deterministic_assessment_requires_all_seven_institutions": sorted(
                INSTITUTIONS
            ),
        })
    available_models = set(_models())
    if deterministic and not request.include_safeguard:
        raise HTTPException(422, {
            "deterministic_assessment_requires_safeguarded_pair": True,
        })
    requested_models = {request.supervisor_model} | {
        item.model for item in request.institutions
    }
    unavailable = sorted(requested_models - available_models)
    if unavailable:
        raise HTTPException(422, {
            "unavailable_models": unavailable,
            "available_models": sorted(available_models),
        })
    unconfigured = sorted(
        model for model in requested_models if not provider_is_configured(model)
    )
    if unconfigured:
        raise HTTPException(422, {
            "unconfigured_model_credentials": unconfigured,
        })
    unknown_institutions = sorted(
        {item.institution_id for item in request.institutions} - set(INSTITUTIONS)
    )
    if unknown_institutions:
        raise HTTPException(422, {"unknown_institutions": unknown_institutions})
    if request.event_id != "custom" and request.event_id not in KNOWN_EVENTS:
        raise HTTPException(422, f"unknown known-event id {request.event_id}")

    updates: dict = {}
    instruments = list(request.instruments)
    event = None
    if request.event_id in KNOWN_EVENTS:
        event = KNOWN_EVENTS[request.event_id]
        updates.update({
            "start_date": date.fromisoformat(event["start_date"]),
            "end_date": date.fromisoformat(event["end_date"]),
            "news_query": event["news_query"],
            "expected_taxonomy": event["expected_taxonomy"],
        })
        instruments = list(event["instruments"]) + instruments
    else:
        updates["expected_taxonomy"] = None

    normalised_assignments = []
    portfolio_overrides = (event or {}).get("portfolio_overrides", {})
    for assignment in request.institutions:
        holdings = assignment.holdings
        if holdings is None and assignment.institution_id in portfolio_overrides:
            holdings = [
                PortfolioHoldingSelection.model_validate(item)
                for item in portfolio_overrides[assignment.institution_id]
            ]
            assignment = assignment.model_copy(update={"holdings": holdings})
        normalised_assignments.append(assignment)
        if holdings is None:
            instruments.extend(
                item["asset_id"]
                for item in INSTITUTIONS[assignment.institution_id]["portfolio"]
            )
        else:
            instruments.extend(item.asset_id for item in holdings)
    instruments = list(dict.fromkeys(instruments))
    unknown_instruments = sorted(set(instruments) - set(INSTRUMENTS))
    if unknown_instruments:
        raise HTTPException(422, {"unknown_instruments": unknown_instruments})

    sources = list(request.sources)
    required_sources = {
        INSTRUMENTS[item]["source"] for item in instruments
    }
    if request.event_id in KNOWN_EVENTS:
        required_sources.add("official_event")
    sources = list(dict.fromkeys(sources + sorted(required_sources)))
    if request.event_id == "custom" and "official_event" in sources:
        sources.remove("official_event")
    updates.update({
        "instruments": instruments,
        "sources": sources,
        "institutions": normalised_assignments,
    })
    return request.model_copy(update=updates)


@app.get("/api/config")
def configuration():
    models = _models()
    model_registry = [{
        "id": model,
        "provider": model.split(":", 1)[0],
        "credential_configured": provider_is_configured(model),
    } for model in models]
    configured_supervisor = os.environ.get("ADCS_SUPERVISOR_MODEL", "").strip()
    default_supervisor = (
        configured_supervisor if configured_supervisor in models
        else (models[0] if models else None)
    )
    institutions = {}
    for institution_id, profile in INSTITUTIONS.items():
        v5_profile = V5_INSTITUTION_PROFILES[institution_id]
        institutions[institution_id] = {
            **profile,
            "name": v5_profile.display_name,
            "profile": v5_profile.portfolio_state.portfolio_description,
            "institution_type": v5_profile.institution_type,
            "objective": v5_profile.objective,
            "constraints": v5_profile.constraints,
            "system_footprint_pct": v5_profile.system_footprint_pct,
            "portfolio_state": v5_profile.portfolio_state.model_dump(mode="json"),
            "liquidity_state": v5_profile.liquidity_state.model_dump(mode="json"),
            "risk_triggers": [
                trigger.model_dump(mode="json") for trigger in v5_profile.triggers
            ],
            "portfolio": [
                {
                    **holding,
                    "label": INSTRUMENTS[holding["asset_id"]]["label"],
                    "symbol": INSTRUMENTS[holding["asset_id"]]["symbol"],
                }
                for holding in profile["portfolio"]
            ],
        }
    return {
        "models": models,
        "model_registry": model_registry,
        "default_model_rotation": _model_rotation(models),
        "default_supervisor_model": default_supervisor,
        "sources": SOURCE_CATALOG,
        "instruments": INSTRUMENTS,
        "portfolio_instruments": {
            key: INSTRUMENTS[key]
            for key in (
                "sp500", "nasdaq", "nikkei", "eurostoxx", "us_banks"
            )
        },
        "institutions": institutions,
        "known_events": KNOWN_EVENTS,
        "taxonomy": TAXONOMY,
        "v5_profile_catalog": profile_catalog_readiness(),
        "v5_preregistration": (
            V5_STORE.get_preregistration(V5_DEFAULT_PREREGISTRATION_ID)
            if V5_DEFAULT_PREREGISTRATION_ID else None
        ),
        "llm_mode": os.environ.get("ADCS_LLM_MODE", "unset"),
        "database": DB_PATH,
        "ready": (
            bool(models)
            and all(item["credential_configured"] for item in model_registry)
            and os.environ.get("ADCS_LLM_MODE") == "live"
        ),
        "version": "4.1",
    }


@app.get("/api/v5/preregistration")
def v5_preregistration():
    if V5_DEFAULT_PREREGISTRATION_ID is None:
        raise HTTPException(503, "the v5 foundation requires exactly six configured models")
    return V5_STORE.get_preregistration(V5_DEFAULT_PREREGISTRATION_ID)


@app.get("/api/v5/profiles")
def list_v5_profiles():
    return {"profiles": V5_STORE.list_profiles()}


@app.post("/api/v5/profiles", status_code=201)
def create_v5_profile(body: InstitutionProfileV5):
    """Create a new immutable profile version, including edited holdings."""

    try:
        V5_STORE.register_profile(body)
    except (ValueError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return V5_STORE.get_profile(body.profile_version_id)


@app.post("/api/v5/preregistrations", status_code=201)
def create_v5_preregistration(body: ExperimentPreregistration):
    """Create an editable-before-approval experiment contract as a new version."""

    configured = set(_models())
    requested = {model.model_id for model in body.models}
    if requested != configured:
        raise HTTPException(422, {
            "model_roster_must_equal_configured_six": sorted(configured),
            "requested": sorted(requested),
        })
    try:
        V5_STORE.create_preregistration(body)
    except (ValueError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return V5_STORE.get_preregistration(body.preregistration_id)


@app.get("/api/v5/preregistrations/{preregistration_id}")
def get_named_v5_preregistration(preregistration_id: str):
    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    return record


@app.post("/api/v5/preregistrations/{preregistration_id}/approve", status_code=201)
def approve_named_v5_preregistration(
    preregistration_id: str, body: PreregistrationApprovalRequest
):
    approval_id = f"APR-{uuid.uuid4().hex[:12].upper()}"
    try:
        V5_STORE.approve_preregistration(
            preregistration_id,
            approval_id,
            body.actor,
            body.note,
            body.approved_sha256,
            body.approved,
        )
    except (ValueError, KeyError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return V5_STORE.get_preregistration(preregistration_id)


@app.post("/api/v5/preregistration/approve", status_code=201)
def approve_v5_preregistration(body: PreregistrationApprovalRequest):
    if V5_DEFAULT_PREREGISTRATION_ID is None:
        raise HTTPException(503, "the v5 foundation requires exactly six configured models")
    approval_id = f"APR-{uuid.uuid4().hex[:12].upper()}"
    try:
        V5_STORE.approve_preregistration(
            V5_DEFAULT_PREREGISTRATION_ID,
            approval_id,
            body.actor,
            body.note,
            body.approved_sha256,
            body.approved,
        )
    except (ValueError, KeyError, ImmutableConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    return V5_STORE.get_preregistration(V5_DEFAULT_PREREGISTRATION_ID)


@app.post("/api/v5/evidence", status_code=201)
def create_v5_evidence_snapshot(body: EvidenceSnapshotV5):
    """Freeze a point-in-time evidence set; this endpoint never calls an LLM."""

    try:
        V5_STORE.register_evidence(body)
    except (ValueError, ImmutableConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    return V5_STORE.get_evidence(body.snapshot_id)


@app.get("/api/v5/evidence/{snapshot_id}")
def get_v5_evidence_snapshot(snapshot_id: str):
    snapshot = V5_STORE.get_evidence(snapshot_id)
    if snapshot is None:
        raise HTTPException(404, f"evidence snapshot {snapshot_id} not found")
    return snapshot


@app.post("/api/v5/evidence/{snapshot_id}/approve", status_code=201)
def approve_v5_evidence_snapshot(
    snapshot_id: str, body: EvidenceApprovalRequest
):
    approval_id = f"APR-{uuid.uuid4().hex[:12].upper()}"
    try:
        V5_STORE.approve_evidence(
            snapshot_id,
            approval_id,
            body.actor,
            body.note,
            body.approved_sha256,
            body.approved,
        )
    except (ValueError, KeyError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return V5_STORE.get_evidence(snapshot_id)


@app.post("/api/v5/initial-runs/plan", status_code=201)
def materialize_v5_initial_run_plan(body: InitialRunPlanRequest):
    """Create the exact 210-cell schedule after both human approvals."""

    plan_record = V5_STORE.get_preregistration(body.preregistration_id)
    if plan_record is None:
        raise HTTPException(404, f"preregistration {body.preregistration_id} not found")
    evidence_record = V5_STORE.get_evidence(body.evidence_snapshot_id)
    if evidence_record is None:
        raise HTTPException(404, f"evidence snapshot {body.evidence_snapshot_id} not found")
    try:
        plan = ExperimentPreregistration.model_validate(plan_record["content"])
        evidence = EvidenceSnapshotV5.model_validate(evidence_record["content"])
        profiles = []
        for profile_id in plan.institution_profile_version_ids:
            record = V5_STORE.get_profile(profile_id)
            if record is None:
                raise KeyError(profile_id)
            profiles.append(InstitutionProfileV5.model_validate(record["content"]))
        cells = build_initial_run_cells(plan, evidence, profiles)
        return V5_STORE.materialize_initial_cells(cells, body.actor)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, KeyError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v5/initial-runs/{preregistration_id}")
def get_v5_initial_run_plan(preregistration_id: str):
    plan = V5_STORE.get_initial_plan(preregistration_id)
    if plan is None:
        raise HTTPException(404, f"initial plan for {preregistration_id} not found")
    return {**plan, "running": V5_EXECUTION.is_running(preregistration_id)}


@app.post("/api/v5/initial-runs/{preregistration_id}/execute", status_code=202)
def execute_v5_initial_run_plan(
    preregistration_id: str, body: InitialExecutionRequest
):
    """Start fresh paid provider calls for an explicitly confirmed plan hash."""

    if os.environ.get("ADCS_LLM_MODE") != "live":
        raise HTTPException(503, "ADCS_LLM_MODE must be live; no mock mode exists")
    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    plan = ExperimentPreregistration.model_validate(record["content"])
    unconfigured = sorted(
        model.model_id
        for model in plan.models
        if not provider_is_configured(model.model_id)
    )
    if unconfigured:
        raise HTTPException(422, {"unconfigured_model_credentials": unconfigured})
    try:
        return V5_EXECUTION.start(
            plan,
            actor=body.actor,
            confirmed_plan_sha256=body.confirmed_plan_sha256,
            max_cells=body.max_cells,
        )
    except (PermissionError, ValueError, KeyError, ImmutableConflict,
            InitialExecutionConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v5/initial-runs/{preregistration_id}/estimates", status_code=201)
def compute_v5_initial_estimates(
    preregistration_id: str, body: MetricComputationRequest
):
    """Compute and freeze deterministic estimates only from the complete grid."""

    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    plan = ExperimentPreregistration.model_validate(record["content"])
    decisions = V5_STORE.accepted_initial_decisions(preregistration_id)
    try:
        result = compute_initial_estimates(
            decisions,
            plan,
            permutations=plan.inference.permutations,
            bootstrap_samples=plan.inference.bootstrap_samples,
        )
        return V5_STORE.register_metric_result(
            result,
            actor=body.actor,
            permutations=plan.inference.permutations,
            bootstrap_samples=plan.inference.bootstrap_samples,
        )
    except (ValueError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v5/feedback-worlds/{preregistration_id}/plan", status_code=201)
def materialize_v5_feedback_worlds(
    preregistration_id: str, body: WorldPlanRequest
):
    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    plan = ExperimentPreregistration.model_validate(record["content"])
    try:
        worlds = build_feedback_worlds(
            plan, V5_STORE.accepted_initial_decisions(preregistration_id)
        )
        return V5_STORE.materialize_feedback_worlds(worlds, actor=body.actor)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v5/feedback-worlds/{preregistration_id}")
def get_v5_feedback_worlds(preregistration_id: str):
    worlds = V5_STORE.get_feedback_world_plan(preregistration_id)
    if worlds is None:
        raise HTTPException(404, f"feedback worlds for {preregistration_id} not found")
    return worlds


@app.post("/api/v5/feedback-worlds/{preregistration_id}/transition", status_code=201)
def transition_v5_feedback_worlds(
    preregistration_id: str, body: WorldTransitionRequest
):
    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    plan = ExperimentPreregistration.model_validate(record["content"])
    world_ids = V5_STORE.untransitioned_world_ids(preregistration_id)[:body.max_worlds]
    completed = []
    try:
        for world_id in world_ids:
            context = V5_STORE.world_transition_input(world_id)
            if context is None:
                raise KeyError(world_id)
            result = transition_initial_world(
                plan,
                context["world"],
                context["members"],
                [
                    InstitutionProfileV5.model_validate(member["profile"])
                    for member in context["members"]
                ],
            )
            completed.append(V5_STORE.persist_world_transition(
                result, actor=body.actor
            ))
    except PermissionError as exc:
        raise HTTPException(409, {
            "error": str(exc), "completed_before_stop": len(completed),
        }) from exc
    except (ValueError, KeyError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, {
            "error": str(exc), "completed_before_stop": len(completed),
        }) from exc
    return {
        "preregistration_id": preregistration_id,
        "completed_world_count": len(completed),
        "remaining_world_count": len(
            V5_STORE.untransitioned_world_ids(preregistration_id)
        ),
        "transitions": completed,
    }


@app.get("/api/v5/feedback-worlds/world/{world_id}/transition")
def get_v5_world_transition(world_id: str, round_index: int = 1):
    transition = V5_STORE.get_world_transition(world_id, round_index)
    if transition is None:
        raise HTTPException(404, f"transition for world {world_id} not found")
    return transition


@app.post(
    "/api/v5/feedback-worlds/{preregistration_id}/second-transition",
    status_code=201,
)
def transition_v5_feedback_round_two(
    preregistration_id: str, body: WorldTransitionRequest
):
    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    plan = ExperimentPreregistration.model_validate(record["content"])
    world_ids = V5_STORE.worlds_pending_second_transition(
        preregistration_id
    )[:body.max_worlds]
    completed = []
    try:
        for world_id in world_ids:
            context = V5_STORE.second_transition_input(world_id)
            if context is None:
                raise KeyError(world_id)
            result = transition_feedback_world(
                plan,
                context["world"],
                context["feedback_decisions"],
                context["previous_common_snapshot"],
                context["previous_private_snapshots"],
                [
                    InstitutionProfileV5.model_validate(item["profile"])
                    for item in context["feedback_decisions"]
                ],
            )
            completed.append(V5_STORE.persist_world_transition(
                result, actor=body.actor
            ))
    except PermissionError as exc:
        raise HTTPException(409, {
            "error": str(exc), "completed_before_stop": len(completed),
        }) from exc
    except (ValueError, KeyError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, {
            "error": str(exc), "completed_before_stop": len(completed),
        }) from exc
    return {
        "preregistration_id": preregistration_id,
        "completed_world_count": len(completed),
        "remaining_ready_world_count": len(
            V5_STORE.worlds_pending_second_transition(preregistration_id)
        ),
        "transitions": completed,
    }


@app.post("/api/v5/feedback-runs/{preregistration_id}/plan", status_code=201)
def materialize_v5_feedback_run_plan(
    preregistration_id: str, body: WorldPlanRequest
):
    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    plan = ExperimentPreregistration.model_validate(record["content"])
    try:
        cells = build_feedback_run_cells(
            plan, V5_STORE.feedback_world_inputs(preregistration_id)
        )
        return V5_STORE.materialize_feedback_cells(cells, actor=body.actor)
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v5/feedback-runs/{preregistration_id}")
def get_v5_feedback_run_plan(preregistration_id: str):
    plan = V5_STORE.get_feedback_plan(preregistration_id)
    if plan is None:
        raise HTTPException(404, f"feedback plan for {preregistration_id} not found")
    return {
        **plan,
        "running": V5_FEEDBACK_EXECUTION.is_running(preregistration_id),
    }


@app.post("/api/v5/feedback-runs/{preregistration_id}/execute", status_code=202)
def execute_v5_feedback_run_plan(
    preregistration_id: str, body: FeedbackExecutionRequest
):
    if os.environ.get("ADCS_LLM_MODE") != "live":
        raise HTTPException(503, "ADCS_LLM_MODE must be live; no mock mode exists")
    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    plan = ExperimentPreregistration.model_validate(record["content"])
    unconfigured = sorted(
        model.model_id
        for model in plan.models
        if not provider_is_configured(model.model_id)
    )
    if unconfigured:
        raise HTTPException(422, {"unconfigured_model_credentials": unconfigured})
    try:
        return V5_FEEDBACK_EXECUTION.start(
            plan,
            actor=body.actor,
            confirmed_plan_sha256=body.confirmed_plan_sha256,
            max_cells=body.max_cells,
        )
    except (PermissionError, ValueError, KeyError, ImmutableConflict,
            InitialExecutionConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/v5/feedback-runs/{preregistration_id}/trajectories", status_code=201)
def compute_v5_feedback_trajectories(
    preregistration_id: str, body: WorldPlanRequest
):
    record = V5_STORE.get_preregistration(preregistration_id)
    if record is None:
        raise HTTPException(404, f"preregistration {preregistration_id} not found")
    plan = ExperimentPreregistration.model_validate(record["content"])
    try:
        result = compute_feedback_trajectories(
            V5_STORE.world_trajectory_inputs(preregistration_id), plan
        )
        return V5_STORE.register_metric_result(
            result,
            actor=body.actor,
            permutations=0,
            bootstrap_samples=0,
        )
    except (ValueError, ImmutableConflict, sqlite3.IntegrityError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/v5/governance/audit")
def get_v5_governance_audit():
    return {
        "chain_valid": V5_STORE.verify_audit_chain(),
        "events": V5_STORE.audit_events(),
    }


@app.post("/api/assessments", status_code=202)
def create_assessment(request: AssessmentRequest):
    if os.environ.get("ADCS_LLM_MODE") != "live":
        raise HTTPException(503, "ADCS_LLM_MODE must be live; no mock mode exists")
    request = _normalise_request(request)
    assessment_id = ENGINE.create(request)
    return _view(_get(assessment_id))


@app.get("/api/assessments")
def list_assessments(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: AssessmentPageSize = AssessmentPageSize.ten,
    event_id: Annotated[str | None, Query(min_length=1, max_length=80)] = None,
    status: Literal[
        "running", "awaiting_approval", "complete", "failed", "rejected"
    ] | None = None,
):
    resolved_page_size = int(page_size)
    result = STORE.assessment_page(
        page=page,
        page_size=resolved_page_size,
        event_id=event_id,
        status=status,
    )
    assessments = []
    for row in result["rows"]:
        request = row["request"]
        assignments = request.get("institutions", [])
        models = (
            list(dict.fromkeys(item["model"] for item in assignments))
            if assignments else request.get("models", [])
        )
        assessments.append({
            "id": row["id"],
            "schema_version": request.get("schema_version", 3),
            "result_validity": (
                "current" if (row.get("metrics") or {}).get("execution_simulation")
                or not row.get("metrics") else "withdrawn_legacy_metrics"
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "current_step": row["current_step"],
            "window": f"{request['start_date']} to {request['end_date']}",
            "event_id": request["event_id"],
            "event_type": (row.get("classification") or {}).get("event_type"),
            "event_label": (row.get("classification") or {}).get("event_label"),
            "models": models,
            "institution_count": len(assignments),
            "agent_run_count": len(row.get("responses") or []),
        })
    return {
        "assessments": assessments,
        "pagination": {
            "page": result["page"],
            "page_size": result["page_size"],
            "total": result["total"],
            "total_pages": result["total_pages"],
            "has_previous": result["page"] > 1,
            "has_next": result["page"] < result["total_pages"],
        },
    }


@app.get("/api/assessments/{assessment_id}")
def get_assessment(assessment_id: str):
    return _view(_get(assessment_id))


@app.get("/api/assessments/{assessment_id}/progress")
def get_progress(assessment_id: str):
    return _view(_get(assessment_id))


@app.post("/api/assessments/{assessment_id}/approvals/{gate}")
def approve(assessment_id: str, gate: str, body: ApprovalRequest):
    _get(assessment_id)
    try:
        ENGINE.approve(assessment_id, gate, body)
    except WorkflowConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(_get(assessment_id))


@app.post("/api/assessments/{assessment_id}/retry")
def retry(assessment_id: str):
    _get(assessment_id)
    try:
        ENGINE.retry(assessment_id)
    except WorkflowConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(_get(assessment_id))


@app.get("/api/assessments/{assessment_id}/results")
def get_results(assessment_id: str):
    row = _get(assessment_id)
    if not row.get("metrics"):
        raise HTTPException(409, "institution-agent runs have not been scored")
    return _view(row)


@app.get("/api/assessments/{assessment_id}/audit")
def get_audit(assessment_id: str):
    _get(assessment_id)
    return {
        "id": assessment_id,
        "events": STORE.events(assessment_id),
        "approvals": STORE.approvals(assessment_id),
    }


@app.get("/api/assessments/{assessment_id}/evidence/{evidence_id}")
def get_evidence_item(assessment_id: str, evidence_id: str):
    row = _get(assessment_id)
    item = next(
        (value for value in (row.get("evidence") or {}).get("items", [])
         if value.get("id") == evidence_id),
        None,
    )
    if not item:
        raise HTTPException(404, f"evidence {evidence_id} not found")
    return {
        "assessment_id": assessment_id,
        "request_window": {
            "start_date": row["request"]["start_date"],
            "end_date": row["request"]["end_date"],
        },
        "item": item,
    }


@app.get("/api/health")
def health():
    models = _models()
    return {
        "ok": True,
        "version": "4.1",
        "database": DB_PATH,
        "llm_mode": os.environ.get("ADCS_LLM_MODE", "unset"),
        "models": models,
        "model_credentials": {
            model: provider_is_configured(model) for model in models
        },
    }


@app.get("/api/methodology")
def methodology():
    return {
        "version": "colfi-demo-contract-v4.1",
        "experiment": (
            "The AI only flags whether stress is present. If flagged, the "
            "unmitigated engine sells 20% of every holding and the safeguarded "
            "engine sells 10% of every holding. The same stress signal and "
            "portfolio enter both paths."
        ),
        "effect_formula": (
            "100 * (unmitigated - safeguarded) / abs(unmitigated); "
            "positive is lower, negative is higher, zero baseline is undefined"
        ),
        "cap_semantics": (
            "The safeguarded 10% is a one-session intervention, not pacing: the "
            "blocked 10% is cancelled and is not carried into a later session. "
            "Later selling occurs only when the deterministic impact threshold "
            "is breached."
        ),
        "price_impact_formula": (
            "impact_cap * (1 - exp(-net_sale / (impact_cap * market_depth)))"
        ),
        "settings": SimulationSettings().model_dump(),
        "calibration_status": (
            "Scenario assumptions for an equal-notional sandbox; not calibrated "
            "to actual institution positions or a forecast of real market impact."
        ),
        "references": [
            "https://www.bankofengland.co.uk/-/media/boe/files/working-paper/2020/modelling-fire-sale-contagion-across-banks-and-non-banks",
            "https://www.bankofengland.co.uk/financial-stability/boe-system-wide-exploratory-scenario-exercise/boe-swes-exercise-final-report",
            "https://www.bis.org/publications/hanging-phone-electronic-trading-fixed-income-markets-and-its-implications",
        ],
    }


if WEB.exists():
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(WEB / "index.html"))
