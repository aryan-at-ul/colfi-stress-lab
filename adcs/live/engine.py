from __future__ import annotations

import threading
import uuid
from typing import Callable

from .agents import build_case_pack, run_institution_agent
from .classifier import classify
from .models import ApprovalRequest, AssessmentRequest, AssessmentSuite
from .scoring import calculate_scores
from .simulation import (
    CONTRACT_VERSION,
    attach_unmitigated_schedule,
    derive_safeguarded_record,
    verify_simulation_arithmetic,
)
from .sources import collect, verify_evidence_snapshot
from .store import LiveStore
from .supervisor import generate_report, generate_suite


WAITING = {
    "evidence_review": "awaiting_evidence_approval",
    "event_review": "awaiting_event_approval",
    "suite_review": "awaiting_suite_approval",
    "release_review": "awaiting_release_approval",
}


class WorkflowConflict(RuntimeError):
    pass


class AssessmentStepError(RuntimeError):
    """A user-actionable workflow stop whose message is already UI-safe."""


class AssessmentEngine:
    def __init__(self, store: LiveStore):
        self.store = store
        self._running: set[str] = set()
        self._lock = threading.RLock()
        self._recover_interrupted()

    def _recover_interrupted(self):
        for row in self.store.list():
            if row["status"] != "running":
                continue
            error = "Service restarted while this step was running; inspect the audit and retry."
            self.store.event(row["id"], row["current_step"], "step_failed",
                             error, {"recoverable": True})
            self.store.update(row["id"], status="failed", error=error)

    def create(self, request: AssessmentRequest) -> tuple[str, bool]:
        request_data = request.model_dump(mode="json")
        if request.use_cached:
            source_id = self.store.find_completed_replay(request_data)
            if source_id:
                self.store.event(
                    source_id,
                    "replay",
                    "cache_replay",
                    "Returned an exactly matching released assessment as a demo replay",
                    {
                        "source_assessment_id": source_id,
                        "requested_by": request.created_by,
                        "replay_key_sha256": self.store.replay_key(request_data),
                    },
                )
                return source_id, True
        assessment_id = f"AST-{uuid.uuid4().hex[:10].upper()}"
        self.store.create(assessment_id, request_data)
        self._start(assessment_id, self._collect)
        return assessment_id, False

    def retry(self, assessment_id: str):
        row = self._require(assessment_id)
        if row["status"] != "failed":
            raise WorkflowConflict("only a failed workflow step can be retried")
        step = row["current_step"]
        if step == "collect":
            target = self._collect
        elif step == "classify":
            target = self._classify
        elif step == "suite":
            target = self._build_suite
        elif step.startswith("run_") or step in {"package_cases", "score", "synthesise"}:
            target = self._execute
        else:
            raise WorkflowConflict(f"step {step} is not retryable")
        retry_payload = {}
        retry_message = "Human requested retry of the failed step"
        if target == self._execute:
            responses = list(row.get("responses") or [])
            retained = sum(item.get("status") == "complete" for item in responses)
            failed = sum(item.get("status") == "failed" for item in responses)
            retry_payload = {
                "retained_valid_decisions": retained,
                "failed_decisions_to_retry": failed,
            }
            retry_message = (
                f"Retry requested — retaining {retained} valid result(s) and "
                f"rerunning only {failed} failed result(s)"
            )
            self.store.update(assessment_id, metrics=None, report=None)
        self.store.event(
            assessment_id, step, "retry_requested", retry_message, retry_payload
        )
        self.store.update(assessment_id, status="running", error="")
        self._start(assessment_id, target)

    def _start(self, assessment_id: str, target: Callable[[str], None]):
        with self._lock:
            if assessment_id in self._running:
                raise WorkflowConflict("this assessment already has a running step")
            self._running.add(assessment_id)

        def guarded():
            try:
                target(assessment_id)
            except Exception as exc:
                row = self.store.get(assessment_id)
                step = row["current_step"] if row else "unknown"
                error = (
                    str(exc) if isinstance(exc, AssessmentStepError)
                    else f"{type(exc).__name__}: {exc}"
                )
                self.store.event(assessment_id, step, "step_failed", error,
                                 {"error": error, "recoverable": True})
                self.store.update(assessment_id, status="failed", error=error)
            finally:
                with self._lock:
                    self._running.discard(assessment_id)

        threading.Thread(
            target=guarded, daemon=True, name=f"assessment-{assessment_id}"
        ).start()

    def _emit(self, assessment_id: str, step: str, base: dict | None = None):
        def emit(kind: str, message: str, payload: dict):
            self.store.event(assessment_id, step, kind, message,
                             {**(base or {}), **payload})
        return emit

    def _collect(self, assessment_id: str):
        row = self._require(assessment_id)
        request = AssessmentRequest.model_validate(row["request"])
        self.store.update(assessment_id, status="running", current_step="collect", error="")
        self.store.event(assessment_id, "collect", "step_started",
                         "Started runtime evidence acquisition", {})
        evidence, failures = collect(request, self._emit(assessment_id, "collect"))
        if not evidence:
            raise RuntimeError("every selected connector failed; inspect source events and retry")
        verification = verify_evidence_snapshot(request, evidence)
        snapshot = {
            "items": evidence,
            "failures": failures,
            "verification": verification,
        }
        self.store.update(assessment_id, evidence=snapshot)
        if not verification["passed"]:
            failed = [
                item["detail"] for item in verification["checks"]
                if not item["passed"]
            ]
            raise RuntimeError(
                "evidence snapshot failed correctness gates: " + "; ".join(failed)
            )
        self.store.update(assessment_id, status=WAITING["evidence_review"],
                          current_step="evidence_review", evidence=snapshot)
        self.store.event(assessment_id, "collect", "step_complete",
                         f"Captured {len(evidence)} real evidence items",
                         {"items": len(evidence), "source_failures": len(failures)})
        self.store.event(
            assessment_id, "evidence_review", "approval_required",
            "Review captured evidence before it is supplied to the supervisor",
            {"items": len(evidence), "source_failures": failures},
        )

    def approve(self, assessment_id: str, gate: str, body: ApprovalRequest):
        row = self._require(assessment_id)
        expected_status = WAITING.get(gate)
        payload = {}
        if gate == "event_review":
            if body.event_type:
                payload["event_type"] = body.event_type
            if body.event_label:
                payload["event_label"] = body.event_label
        if gate == "suite_review":
            deterministic = (
                (row.get("plan") or {}).get("execution_mode")
                == "deterministic_stress_rules"
            )
            if (
                deterministic
                and body.max_daily_portfolio_sell_pct is not None
                and body.max_daily_portfolio_sell_pct != 10.0
            ):
                raise WorkflowConflict(
                    "the deterministic demo contract fixes safeguarded selling at 10%"
                )
            if deterministic and body.institution_sell_targets_pct is not None:
                invalid = {
                    key: value
                    for key, value in body.institution_sell_targets_pct.items()
                    if value != 20.0
                }
                if invalid:
                    raise WorkflowConflict(
                        "the deterministic demo contract fixes every institution's "
                        "stress sale at 20%"
                    )
            if body.suite_objective is not None:
                payload["suite_objective"] = body.suite_objective
            if body.safeguard_instruction is not None and not deterministic:
                payload["safeguard_instruction"] = body.safeguard_instruction
            if body.max_single_asset_sell_pct is not None:
                payload["max_single_asset_sell_pct"] = body.max_single_asset_sell_pct
            if body.max_daily_portfolio_sell_pct is not None:
                payload["max_daily_portfolio_sell_pct"] = (
                    body.max_daily_portfolio_sell_pct
                )
            if body.institution_sell_targets_pct is not None:
                known = {
                    item["institution_id"]
                    for item in (row.get("plan") or {}).get(
                        "deterministic_rules", []
                    )
                }
                unknown = sorted(
                    set(body.institution_sell_targets_pct) - known
                )
                if unknown:
                    raise WorkflowConflict(
                        f"sell targets include unknown institutions: {unknown}"
                    )
                payload["institution_sell_targets_pct"] = (
                    body.institution_sell_targets_pct
                )
        if not expected_status or row["status"] != expected_status:
            duplicate = any(
                item["gate"] == gate
                and item["actor"] == body.actor
                and item["approved"] == body.approved
                and item["note"] == body.note
                and item["payload"] == payload
                for item in self.store.approvals(assessment_id)
            )
            if duplicate:
                return
            raise WorkflowConflict(
                f"{gate} approval is unavailable while status is {row['status']}"
            )
        if gate == "evidence_review" and not (
            (row.get("evidence") or {}).get("verification", {}).get("passed")
        ):
            raise WorkflowConflict("evidence cannot be approved until correctness checks pass")
        if gate == "release_review":
            metrics = row.get("metrics") or {}
            simulation = metrics.get("execution_simulation") or {}
            if not metrics.get("verification", {}).get("release_eligible"):
                raise WorkflowConflict(
                    "release is blocked by failed deterministic verification"
                )
            arithmetic = verify_simulation_arithmetic(
                simulation.get("conditions") or {},
                simulation.get("effects") or {},
                records=row.get("responses") or [],
                settings=simulation.get("settings") or {},
                evidence=(row.get("evidence") or {}).get("items", []),
            )
            if not arithmetic["passed"]:
                raise WorkflowConflict(
                    "release is blocked because persisted display values failed "
                    "deterministic arithmetic replay"
                )
        self.store.approve(
            assessment_id, gate, body.actor, body.approved, body.note, payload
        )
        if not body.approved:
            self.store.update(assessment_id, status="rejected", current_step=gate)
            return

        if gate == "evidence_review":
            self.store.update(
                assessment_id, status="running", current_step="classify", error=""
            )
            self._start(assessment_id, self._classify)
        elif gate == "event_review":
            classification = dict(row["classification"])
            original = dict(classification)
            if body.event_type:
                classification["event_type"] = body.event_type
            if body.event_label:
                classification["event_label"] = body.event_label
            if classification != original:
                classification["human_override"] = {
                    "actor": body.actor,
                    "note": body.note,
                    "original": original,
                }
                self.store.update(assessment_id, classification=classification)
            self.store.update(
                assessment_id, status="running", current_step="suite", error=""
            )
            self._start(assessment_id, self._build_suite)
        elif gate == "suite_review":
            suite = dict(row["plan"])
            original = {
                "objective": suite["objective"],
                "safeguard": dict(suite["safeguard"]),
                "deterministic_rules": list(
                    suite.get("deterministic_rules") or []
                ),
            }
            changed = False
            if body.suite_objective is not None and body.suite_objective.strip():
                suite["objective"] = body.suite_objective.strip()
                changed = changed or suite["objective"] != original["objective"]
            if (
                not deterministic
                and body.safeguard_instruction is not None
                and body.safeguard_instruction.strip()
            ):
                suite["safeguard"]["instruction"] = body.safeguard_instruction.strip()
                changed = changed or (
                    suite["safeguard"]["instruction"]
                    != original["safeguard"]["instruction"]
                )
            if body.max_single_asset_sell_pct is not None:
                suite["safeguard"]["max_single_asset_sell_pct"] = (
                    body.max_single_asset_sell_pct
                )
                changed = changed or (
                    body.max_single_asset_sell_pct
                    != original["safeguard"]["max_single_asset_sell_pct"]
                )
            if body.max_daily_portfolio_sell_pct is not None:
                suite["safeguard"]["max_daily_portfolio_sell_pct"] = (
                    body.max_daily_portfolio_sell_pct
                )
                # Deterministic demo rules sell each selected holding pro rata.
                # Keeping this compatibility value equal produces the same cap in
                # the established schedule builder.
                suite["safeguard"]["max_single_asset_sell_pct"] = (
                    body.max_daily_portfolio_sell_pct
                )
                changed = changed or (
                    body.max_daily_portfolio_sell_pct
                    != original["safeguard"].get(
                        "max_daily_portfolio_sell_pct"
                    )
                )
            if body.institution_sell_targets_pct is not None:
                rules = list(suite.get("deterministic_rules") or [])
                for rule in rules:
                    institution_id = rule["institution_id"]
                    if institution_id in body.institution_sell_targets_pct:
                        rule["target_sell_portfolio_pct"] = (
                            body.institution_sell_targets_pct[institution_id]
                        )
                suite["deterministic_rules"] = rules
                changed = changed or rules != original["deterministic_rules"]
            runtime = suite.get("runtime", {})
            execution_mode = suite.get("execution_mode")
            deterministic_rules = suite.get("deterministic_rules")
            suite = AssessmentSuite.model_validate(suite).model_dump()
            suite["runtime"] = runtime
            if execution_mode:
                suite["execution_mode"] = execution_mode
            if deterministic_rules is not None:
                suite["deterministic_rules"] = deterministic_rules
            if changed:
                suite["human_override"] = {
                    "actor": body.actor, "note": body.note, "original": original,
                }
                self.store.update(assessment_id, plan=suite)
            self.store.update(
                assessment_id, status="running", current_step="package_cases", error=""
            )
            self._start(assessment_id, self._execute)
        elif gate == "release_review":
            self.store.update(
                assessment_id, status="complete", current_step="release_review"
            )
            self.store.event(
                assessment_id, "release_review", "step_complete",
                "Human approved the assessment report for release",
                {"actor": body.actor},
            )

    def _classify(self, assessment_id: str):
        row = self._require(assessment_id)
        self.store.update(assessment_id, status="running", current_step="classify", error="")
        self.store.event(assessment_id, "evidence_review", "step_complete",
                         "Evidence approved for supervisor use", {})
        self.store.event(assessment_id, "classify", "step_started",
                         "Supervisor started taxonomy classification", {})
        classification = classify(
            row["evidence"]["items"], row["request"]["supervisor_model"],
            self._emit(assessment_id, "classify"),
        )
        self.store.update(
            assessment_id, status=WAITING["event_review"],
            current_step="event_review", classification=classification,
        )
        self.store.event(
            assessment_id, "classify", "step_complete",
            f"Supervisor proposed {classification['event_type']}", classification,
        )
        self.store.event(
            assessment_id, "event_review", "approval_required",
            "Approve or override the event before any test suite is generated", {},
        )

    def _build_suite(self, assessment_id: str):
        row = self._require(assessment_id)
        self.store.update(assessment_id, status="running", current_step="suite", error="")
        self.store.event(assessment_id, "event_review", "step_complete",
                         "Event classification approved", {})
        self.store.event(assessment_id, "suite", "step_started",
                         "Supervisor started assessment-suite generation", {})
        suite = generate_suite(
            row["request"], row["classification"], row["evidence"]["items"],
            self._emit(assessment_id, "suite"),
        )
        self.store.update(
            assessment_id, status=WAITING["suite_review"],
            current_step="suite_review", plan=suite,
        )
        self.store.event(
            assessment_id, "suite", "step_complete",
            f"Supervisor generated {len(suite['cases'])} assessment cases",
            {"cases": [item["id"] for item in suite["cases"]]},
        )
        self.store.event(
            assessment_id, "suite_review", "approval_required",
            "Approve or amend the generated suite before institution agents run", {},
        )

    def _execute(self, assessment_id: str):
        row = self._require(assessment_id)
        request = row["request"]
        classification = row["classification"]
        evidence = row["evidence"]["items"]
        suite = dict(row["plan"])
        responses = list(row.get("responses") or [])

        if not suite.get("case_packs"):
            self.store.update(
                assessment_id, status="running", current_step="package_cases", error=""
            )
            self.store.event(
                assessment_id, "suite_review", "step_complete",
                "Human approved the generated assessment suite", {},
            )
            self.store.event(
                assessment_id, "package_cases", "step_started",
                "Building isolated case packs", {},
            )
            packs = []
            for assignment in request["institutions"]:
                for case in suite["cases"]:
                    packs.append(build_case_pack(
                        request, classification, suite, assignment, case
                    ))
            suite["case_packs"] = packs
            self.store.update(assessment_id, plan=suite)
            self.store.event(
                assessment_id, "package_cases", "step_complete",
                f"Built {len(packs)} isolated institution case packs",
                {"case_packs": len(packs)},
            )

        for case in suite["cases"]:
            condition = case["condition"]
            step = f"run_{condition}"
            self.store.update(
                assessment_id, status="running", current_step=step,
                responses=responses, error="",
            )
            self.store.event(
                assessment_id, step, "step_started",
                f"Started {condition} institution-agent runs",
                {"condition": condition},
            )
            for assignment in request["institutions"]:
                institution_id = assignment["institution_id"]
                pack = next(
                    item for item in suite["case_packs"]
                    if item["institution_id"] == institution_id
                    and item["test_case"]["condition"] == condition
                )
                for sample in range(1, request["samples_per_agent"] + 1):
                    run_id = f"{case['id']}:{institution_id}:S{sample}"
                    previous = next(
                        (item for item in responses if item["run_id"] == run_id), None
                    )
                    if (
                        previous and previous["status"] == "complete"
                        and previous.get("decision_contract_version") == CONTRACT_VERSION
                    ):
                        self.store.event(
                            assessment_id, step, "agent_run_reused",
                            f"Retained validated run {run_id}",
                            {"run_id": run_id, "institution_id": institution_id},
                        )
                        continue
                    if previous:
                        responses.remove(previous)
                    base = {
                        "run_id": run_id,
                        "case_id": case["id"],
                        "condition": condition,
                        "institution_id": institution_id,
                        "institution_name": pack["institution_name"],
                        "model": assignment["model"],
                        "sample": sample,
                        "status": "running",
                        "portfolio": pack["portfolio"],
                        "execution_mode": request.get(
                            "execution_mode", "llm_decision"
                        ),
                    }
                    self.store.event(
                        assessment_id, step, "agent_run_started",
                        f"{pack['institution_name']} agent started {case['title']}",
                        base,
                    )
                    self.store.update(assessment_id, responses=responses + [base])
                    try:
                        if condition == "safeguarded":
                            stress_case = next(
                                item for item in suite["cases"]
                                if item["condition"] == "stress"
                            )
                            origin_id = (
                                f"{stress_case['id']}:{institution_id}:S{sample}"
                            )
                            origin = next(
                                (item for item in responses
                                 if item["run_id"] == origin_id), None
                            )
                            if not origin or origin.get("status") != "complete":
                                raise ValueError(
                                    f"paired stress decision {origin_id} is unavailable"
                                )
                            record = derive_safeguarded_record(
                                origin, run_id, case["id"], evidence,
                                suite["safeguard"],
                                int(suite["simulation"]["rounds"]),
                            )
                            result = record
                        else:
                            if (
                                request.get("execution_mode")
                                == "deterministic_stress_rules"
                            ):
                                rule = next(
                                    (
                                        item
                                        for item in suite.get(
                                            "deterministic_rules", []
                                        )
                                        if item["institution_id"]
                                        == institution_id
                                    ),
                                    None,
                                )
                                if rule is None:
                                    raise ValueError(
                                        "approved deterministic rule is missing "
                                        f"for {pack['institution_name']}"
                                    )
                                result = run_institution_agent(
                                    pack,
                                    evidence,
                                    assignment["model"],
                                    request.get("temperature"),
                                    self._emit(assessment_id, step, base),
                                    execution_mode="deterministic_stress_rules",
                                    deterministic_rule=rule,
                                )
                            else:
                                result = run_institution_agent(
                                    pack, evidence, assignment["model"],
                                    request.get("temperature"),
                                    self._emit(
                                        assessment_id, step, base,
                                    ),
                                )
                            record = attach_unmitigated_schedule(
                                {**base, **result, "status": "complete"}, evidence
                            )
                        self.store.event(
                            assessment_id, step, "agent_run_complete",
                            (
                                f"Applied safeguard to {pack['institution_name']}'s "
                                "paired stress decision"
                                if condition == "safeguarded" else
                                (
                                    f"{pack['institution_name']} using "
                                    f"{assignment['model']} returned a validated "
                                    "stress signal; approved code produced the action"
                                )
                                if request.get("execution_mode")
                                == "deterministic_stress_rules" else
                                f"{pack['institution_name']} returned a validated decision"
                            ),
                            {**base, "status": "complete",
                             "stance": record["output"]["stance"],
                             "decision_origin": record["decision_origin"]},
                        )
                    except Exception as exc:
                        record = {
                            **base, "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        self.store.event(
                            assessment_id, step, "agent_run_failed",
                            (
                                f"{pack['institution_name']} using "
                                f"{assignment['model']} failed: {exc}"
                            ),
                            record,
                        )
                    responses.append(record)
                    self.store.update(assessment_id, responses=responses)
            expected = (
                len(request["institutions"]) * request["samples_per_agent"]
            )
            valid = sum(
                item["status"] == "complete" and item["condition"] == condition
                for item in responses
            )
            failed = sum(
                item["status"] == "failed" and item["condition"] == condition
                for item in responses
            )
            if failed or valid != expected:
                unresolved = max(expected - valid, failed, 0)
                failed_records = [
                    item for item in responses
                    if item.get("condition") == condition
                    and item.get("status") != "complete"
                ]
                affected = ", ".join(
                    f"{item['institution_name']} using {item['model']}"
                    for item in failed_records
                ) or "an unidentified required run"
                condition_label = {
                    "control": "Control",
                    "stress": "Unmitigated stress",
                    "safeguarded": "Safeguarded execution",
                }.get(condition, condition.title())
                message = (
                    f"{condition_label} stopped: {valid} of {expected} required "
                    f"results are valid and {unresolved} are unresolved "
                    f"({affected}). Valid results are saved. Retry will rerun only "
                    "the unresolved result(s); scoring and report generation have "
                    "not started."
                )
                self.store.event(
                    assessment_id, step, "condition_incomplete", message,
                    {
                        "condition": condition,
                        "expected": expected,
                        "valid": valid,
                        "failed": failed,
                        "affected_runs": [
                            {
                                "run_id": item.get("run_id"),
                                "institution_id": item.get("institution_id"),
                                "institution_name": item.get("institution_name"),
                                "model": item.get("model"),
                                "error": item.get("error"),
                            }
                            for item in failed_records
                        ],
                    },
                )
                raise AssessmentStepError(message)
            self.store.event(
                assessment_id, step, "step_complete",
                f"{condition.title()} finished: all {valid} required results are valid",
                {"expected": expected, "valid": valid, "failed": failed},
            )

        if not any(item["status"] == "complete" for item in responses):
            raise RuntimeError("no institution agent returned a valid decision")

        self.store.update(assessment_id, status="running", current_step="score")
        self.store.event(
            assessment_id, "score", "step_started",
            "Running deterministic graders over agent outputs and traces", {},
        )
        scores = calculate_scores(
            responses, suite, evidence, request.get("expected_taxonomy"),
            classification, row["evidence"].get("verification"),
        )
        self.store.update(assessment_id, metrics=scores)
        if not scores["verification"]["release_eligible"]:
            failed_checks = [
                item for item in scores["verification"]["checks"]
                if not item["passed"]
            ]
            message = (
                "Scoring stopped before report generation because the run did not "
                "pass every required verification check: "
                + "; ".join(item["detail"] for item in failed_checks)
            )
            self.store.event(
                assessment_id, "score", "verification_blocked", message,
                {"failed_checks": failed_checks, "release_eligible": False},
            )
            raise AssessmentStepError(message)
        self.store.event(
            assessment_id, "score", "step_complete",
            "Verified agent, execution, convergence and impact metrics calculated",
            {
                "scored_runs": len(scores["run_scores"]),
                "release_eligible": scores["verification"]["release_eligible"],
            },
        )

        self.store.update(assessment_id, current_step="synthesise")
        self.store.event(
            assessment_id, "synthesise", "step_started",
            "Supervisor started evidence-grounded report synthesis", {},
        )
        report = generate_report(
            request, classification, evidence, suite, responses, scores,
            self._emit(assessment_id, "synthesise"),
        )
        self.store.update(
            assessment_id, status=WAITING["release_review"],
            current_step="release_review", report=report,
        )
        self.store.event(
            assessment_id, "synthesise", "step_complete",
            "Draft assessment report passed required-format and citation checks", {},
        )
        self.store.event(
            assessment_id, "release_review", "approval_required",
            "A named human must approve the assessment before release", {},
        )

    def _require(self, assessment_id: str):
        row = self.store.get(assessment_id)
        if not row:
            raise KeyError(assessment_id)
        return row
