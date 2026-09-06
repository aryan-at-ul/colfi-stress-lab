from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from adcs.live.agents import (
    _constraint_tool,
    _evidence_tool,
    _model_case_pack,
    _portfolio_tool,
    _validated_json,
    build_case_pack,
)
from adcs.live.catalog import INSTITUTIONS, KNOWN_EVENTS
from adcs.live.engine import AssessmentEngine, WorkflowConflict
from adcs.live.models import (
    ApprovalRequest,
    AssessmentRequest,
    AssessmentSuite,
    InstitutionDecision,
    InstitutionStressSignal,
)
from adcs.live.results import build_demo_result
from adcs.live.rules import default_stress_rules, decision_from_stress_rule
from adcs.live.scoring import calculate_scores
from adcs.live.simulation import (
    attach_unmitigated_schedule,
    derive_safeguarded_record,
    effect,
    simulate,
)
from adcs.live.sources import INSTRUMENTS, _cboe, _ecb, _yahoo, verify_evidence_snapshot
from adcs.live.sessions import sessions_after
from adcs.live.store import LiveStore
from adcs.llm.client import LLMError


def request(**updates):
    value = {
        "event_id": "august_2024_turmoil",
        "expected_taxonomy": "Equity market crisis",
        "start_date": "2024-08-02",
        "end_date": "2024-08-05",
        "sources": ["yahoo", "cboe", "official_event"],
        "instruments": ["sp500", "nasdaq", "vix"],
        "news_query": "August 2024 market turmoil",
        "supervisor_model": "deepseek:supervisor",
        "institutions": [
            {"institution_id": "INST-01", "model": "deepseek:agent-a"},
            {"institution_id": "INST-02", "model": "deepseek:agent-b"},
        ],
        "samples_per_agent": 1,
        "temperature": 0.2,
        "include_safeguard": True,
        "created_by": "Reviewer",
    }
    value.update(updates)
    return AssessmentRequest(**value)


def suite():
    return AssessmentSuite(**{
        "title": "Equity-agent stress suite",
        "objective": "Assess portfolio decisions under control, observed stress and safeguards.",
        "hypothesis": "Observed stress may align portfolio decisions across institution agents.",
        "decision_horizon": "same trading day",
        "cases": [
            {
                "id": "CASE-CONTROL", "condition": "control",
                "title": "Pre-shock control",
                "objective": "Assess the agent at the captured pre-shock reference.",
                "agent_instruction": "Use the pre-shock reference values only.",
                "evidence_ids": ["MKT-SP500", "MKT-NASDAQ"],
            },
            {
                "id": "CASE-STRESS", "condition": "stress",
                "title": "Observed stress",
                "objective": "Assess the agent against observed market changes.",
                "agent_instruction": "Use observed evidence and the isolated portfolio.",
                "evidence_ids": ["MKT-SP500", "MKT-NASDAQ"],
            },
            {
                "id": "CASE-SAFEGUARDED", "condition": "safeguarded",
                "title": "Safeguarded stress",
                "objective": "Rerun observed stress with the approved control.",
                "agent_instruction": "Apply the approved sale cap and pacing rule.",
                "evidence_ids": ["MKT-SP500", "MKT-NASDAQ"],
            },
        ],
        "safeguard": {
            "instruction": "Cap every sale and stage execution.",
            "max_single_asset_sell_pct": 5,
            "require_staged_execution": True,
            "allow_hedging": True,
        },
        "rubric_weights": {
            "schema_validity": 0.1,
            "evidence_grounding": 0.25,
            "portfolio_alignment": 0.25,
            "constraint_awareness": 0.2,
            "safeguard_adherence": 0.2,
        },
        "pass_threshold": 0.7,
        "limitations": ["Declared portfolios are sandbox inputs."],
    }).model_dump()


def evidence():
    common = {
        "kind": "market", "source": "Yahoo Finance",
        "observed_at": "2024-08-05T20:00:00Z",
        "source_url": "https://finance.yahoo.com/",
        "acquisition_url": "https://query1.finance.yahoo.com/",
        "fetched_at": "2026-09-03T20:00:00Z", "raw_sha256": "abc",
        "previous_value": 100.0, "unit": "index", "summary": "Test evidence",
        "reference_date": "2024-08-01",
        "window_start_date": "2024-08-02",
        "window_end_date": "2024-08-05",
        "window_change_pct": -3.0,
        "min_daily_change_pct": -3.0,
        "max_daily_change_pct": 0.0,
    }
    return [
        {**common, "id": "MKT-SP500", "title": "S&P 500", "symbol": "^GSPC",
         "value": 97.0, "change_pct": -3.0, "observations": [
             {"session_date": "2024-08-01", "observed_at": "2024-08-01T20:00:00Z", "value": 100.0, "daily_change_pct": None, "volume": 1000},
             {"session_date": "2024-08-02", "observed_at": "2024-08-02T20:00:00Z", "value": 99.0, "daily_change_pct": -1.0, "volume": 1100},
             {"session_date": "2024-08-05", "observed_at": "2024-08-05T20:00:00Z", "value": 97.0, "daily_change_pct": -2.020202, "volume": 1200},
         ]},
        {**common, "id": "MKT-NASDAQ", "title": "Nasdaq 100", "symbol": "^NDX",
         "value": 96.0, "change_pct": -4.0, "window_change_pct": -4.0,
         "observations": [
             {"session_date": "2024-08-01", "observed_at": "2024-08-01T20:00:00Z", "value": 100.0, "daily_change_pct": None, "volume": 1000},
             {"session_date": "2024-08-02", "observed_at": "2024-08-02T20:00:00Z", "value": 98.0, "daily_change_pct": -2.0, "volume": 1100},
             {"session_date": "2024-08-05", "observed_at": "2024-08-05T20:00:00Z", "value": 96.0, "daily_change_pct": -2.040816, "volume": 1200},
         ]},
    ]


def test_market_instrument_names_and_nasdaq_series_match_the_ui_contract():
    assert {
        key: INSTRUMENTS[key]["label"]
        for key in ("sp500", "nasdaq", "nikkei", "eurostoxx", "us_banks")
    } == {
        "sp500": "S&P 500",
        "nasdaq": "Nasdaq 100",
        "nikkei": "Nikkei 225",
        "eurostoxx": "STOXX 50",
        "us_banks": "State Street SPDR S&P Regional Banking ETF",
    }
    assert INSTRUMENTS["nasdaq"]["symbol"] == "^NDX"


def record(institution_id: str, condition: str, action: str, size: float,
           timing: str = "staged"):
    case_id = {
        "control": "CASE-CONTROL",
        "stress": "CASE-STRESS",
        "safeguarded": "CASE-SAFEGUARDED",
    }[condition]
    asset = INSTITUTIONS[institution_id]["portfolio"][0]["asset_id"]
    stance = "hold" if action == "hold" else "risk_off"
    return {
        "run_id": f"{case_id}:{institution_id}:S1",
        "case_id": case_id,
        "condition": condition,
        "institution_id": institution_id,
        "institution_name": INSTITUTIONS[institution_id]["name"],
        "model": "deepseek:test",
        "sample": 1,
        "status": "complete",
        "tool_plan": {"plan_summary": "Inspect portfolio and limits.",
                      "tool_requests": [
                          "portfolio_shock", "constraint_register", "evidence_lookup",
                      ]},
        "tool_results": [{"tool": "portfolio_shock"},
                         {"tool": "constraint_register"},
                         {"tool": "evidence_lookup"}],
        "trace": [],
        "output": {
            "stance": stance,
            "executive_decision": "Hold." if action == "hold" else "Reduce risk.",
            "actions": [{
                "asset_id": asset, "action": action, "size_pct": size,
                "timing": timing, "rationale": "Observed portfolio evidence.",
            }],
            "urgency": 1 if action == "hold" else 4,
            "confidence": 0.8,
            "constraints_considered": INSTITUTIONS[institution_id]["constraints"],
            "evidence_ids": ["MKT-SP500" if institution_id == "INST-01" else "MKT-NASDAQ"],
        },
        "cached": False,
    }


def test_request_rejects_simulated_or_duplicate_agent_assignment():
    with pytest.raises(ValidationError):
        request(institutions=[{"institution_id": "INST-01", "model": "mock:test"}])
    with pytest.raises(ValidationError):
        request(institutions=[
            {"institution_id": "INST-01", "model": "deepseek:a"},
            {"institution_id": "INST-01", "model": "deepseek:b"},
        ])


def test_decision_schema_rejects_stance_action_contradiction():
    with pytest.raises(ValidationError):
        InstitutionDecision.model_validate({
            "stance": "risk_off", "executive_decision": "Hold despite risk stance.",
            "actions": [{
                "asset_id": "sp500", "action": "hold", "size_pct": 0,
                "timing": "monitor", "rationale": "No active change.",
            }],
            "urgency": 1, "confidence": 0.8,
            "constraints_considered": ["Remain in mandate."],
            "evidence_ids": ["MKT-SP500"],
        })


def test_request_rejects_reversed_or_excessive_window():
    with pytest.raises(ValidationError):
        request(start_date="2024-08-06", end_date="2024-08-05")
    with pytest.raises(ValidationError):
        request(start_date="2024-01-01", end_date="2024-03-01")
    with pytest.raises(ValidationError):
        request(start_date=date.today(), end_date=date.today())


def test_declared_portfolios_are_complete_and_sum_to_one():
    assert len(INSTITUTIONS) == 7
    for institution in INSTITUTIONS.values():
        assert sum(item["weight"] for item in institution["portfolio"]) == 1
        assert institution["base_currency"] in {"USD", "EUR", "JPY"}
        assert institution["data_class"].startswith("declared sandbox")


def test_svb_preset_puts_regional_bank_exposure_in_two_portfolios():
    overrides = KNOWN_EVENTS["svb_2023_run"]["portfolio_overrides"]
    exposed = [
        institution_id
        for institution_id, holdings in overrides.items()
        if any(item["asset_id"] == "us_banks" for item in holdings)
    ]
    assert exposed == ["INST-01", "INST-05"]
    assert all(
        sum(item["weight_pct"] for item in holdings) == 100
        for holdings in overrides.values()
    )


def test_case_pack_is_isolated_and_control_uses_reference_state():
    req = request().model_dump(mode="json")
    classification = {"event_type": "Equity market crisis", "event_label": "Test"}
    selected_suite = suite()
    case = selected_suite["cases"][0]
    pack = build_case_pack(req, classification, selected_suite,
                           req["institutions"][0], case)
    assert pack["institution_id"] == "INST-01"
    assert "peer_responses" not in pack
    assert len(pack["portfolio"]) == 1
    result = _portfolio_tool(pack, evidence())
    assert result["portfolio_weighted_change_pct"] == 0
    assert result["positions"][0]["observed_change_pct"] == 0
    assert result["positions"][0]["applied_change_pct"] == 0
    assert result["positions"][0]["observed_value"] == 100
    assert result["positions"][0]["window_end_date"] == "2024-08-01"
    assert "condition" not in result
    view = _evidence_tool(pack, evidence())
    assert "evidence_view" not in view
    assert len(view["items"][0]["observations"]) == 1
    assert view["items"][0]["change_pct"] == 0
    constraints = _constraint_tool(pack)
    assert constraints["action_contract"]["held_asset_ids"] == ["sp500"]
    assert constraints["action_contract"]["allowed_hedge_asset_ids"] == [
        "sp500_put_options"
    ]


def test_answer_correction_names_institution_model_and_plain_reason():
    class InvalidDecisionLLM:
        def complete_json(self, *args, **kwargs):
            del args, kwargs
            return {
                "stance": "hold",
                "executive_decision": "Hold while also selling.",
                "actions": [{
                    "asset_id": "sp500", "action": "sell", "size_pct": 10,
                    "timing": "immediate", "rationale": "Reduce risk.",
                }],
                "urgency": 2,
                "confidence": 0.7,
                "constraints_considered": ["Remain within mandate."],
                "evidence_ids": ["MKT-SP500"],
            }

    emitted = []
    with pytest.raises(ValueError, match="failed validation after repair"):
        _validated_json(
            InvalidDecisionLLM(),
            "deepseek:deepseek-v4-flash",
            "system",
            {},
            InstitutionDecision,
            (
                "stance", "executive_decision", "actions", "urgency",
                "confidence", "constraints_considered", "evidence_ids",
            ),
            None,
            lambda kind, message, payload: emitted.append(
                (kind, message, payload)
            ),
            "institution decision",
            institution_name="Institution 1",
        )

    assert len(emitted) == 3
    assert "AI answer check 1 of 3" in emitted[0][1]
    assert "Institution 1" in emitted[0][1]
    assert "deepseek:deepseek-v4-flash" in emitted[0][1]
    assert "mixed a hold decision with active trade actions" in emitted[0][1]
    assert "schema" not in emitted[0][1].lower()
    assert "check 3 of 3 failed" in emitted[-1][1]
    assert emitted[0][2]["model"] == "deepseek:deepseek-v4-flash"


def test_answer_correction_retries_provider_without_structured_json():
    class EventuallyValidLLM:
        def __init__(self):
            self.calls = 0

        def complete_json(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            if self.calls < 3:
                raise LLMError(
                    "xai:grok-4.6: provider did not return required structured JSON"
                )
            return {
                "stress_detected": False,
                "severity": 1,
                "summary": "No material stress in the reference snapshot.",
                "confidence": 0.8,
                "evidence_ids": ["MKT-SP500"],
                "trigger_ids": [],
                "_prompt_sha256": "eventual-success",
            }

    llm = EventuallyValidLLM()
    emitted = []
    value, prompt_hash = _validated_json(
        llm,
        "xai:grok-4.6",
        "system",
        {},
        InstitutionStressSignal,
        (
            "stress_detected", "severity", "summary", "confidence",
            "evidence_ids", "trigger_ids",
        ),
        None,
        lambda kind, message, payload: emitted.append((kind, message, payload)),
        "institution stress signal",
        institution_name="Institution 7",
    )
    assert value["stress_detected"] is False
    assert prompt_hash == "eventual-success"
    assert llm.calls == 3
    assert len(emitted) == 2
    assert "Institution 7 using xai:grok-4.6" in emitted[0][1]
    assert "usable structured answer" in emitted[0][1]


def test_overlong_stress_summary_is_bounded_without_aborting_run():
    class VerboseSignalLLM:
        def __init__(self):
            self.calls = 0

        def complete_json(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            return {
                "stress_detected": True,
                "severity": 4,
                "summary": "Material institution stress. " * 40,
                "confidence": 0.92,
                "evidence_ids": ["MKT-SP500"],
                "trigger_ids": [],
                "_prompt_sha256": "verbose-signal",
            }

    llm = VerboseSignalLLM()
    emitted = []
    value, prompt_hash = _validated_json(
        llm,
        "deepseek:deepseek-v4-flash",
        "system",
        {},
        InstitutionStressSignal,
        (
            "stress_detected", "severity", "summary", "confidence",
            "evidence_ids", "trigger_ids",
        ),
        None,
        lambda kind, message, payload: emitted.append((kind, message, payload)),
        "institution stress signal",
        institution_name="Institution 2",
    )

    assert llm.calls == 1
    assert prompt_hash == "verbose-signal"
    assert len(value["summary"]) == 600
    assert value["summary"].endswith("…")
    assert [item[0] for item in emitted] == ["answer_normalized"]
    assert emitted[0][2]["fields"] == ["summary"]


def test_operator_holdings_override_is_validated_and_reaches_case_pack():
    with pytest.raises(ValidationError, match="must total 100"):
        request(institutions=[{
            "institution_id": "INST-01",
            "model": "deepseek:agent-a",
            "holdings": [
                {"asset_id": "sp500", "weight_pct": 60},
                {"asset_id": "nasdaq", "weight_pct": 30},
            ],
        }])

    req = request(institutions=[{
        "institution_id": "INST-01",
        "model": "deepseek:agent-a",
        "holdings": [
            {"asset_id": "sp500", "weight_pct": 60},
            {"asset_id": "nasdaq", "weight_pct": 40},
        ],
    }]).model_dump(mode="json")
    selected_suite = suite()
    pack = build_case_pack(
        req,
        {"event_type": "Equity market crisis", "event_label": "Test"},
        selected_suite,
        req["institutions"][0],
        selected_suite["cases"][1],
    )
    assert pack["portfolio"] == [
        {"asset_id": "sp500", "weight": 0.6},
        {"asset_id": "nasdaq", "weight": 0.4},
    ]
    assert pack["portfolio_origin"] == "operator_override"
    visible = _model_case_pack(pack)
    assert visible["system_footprint_pct"] == 15.0
    assert visible["portfolio_state"]["gross_exposure_pct"] == 185.0
    assert visible["risk_triggers"][0]["observed_value"] == 88.0


def test_model_case_pack_hides_condition_classification_and_model_assignment():
    req = request().model_dump(mode="json")
    classification = {
        "event_type": "Equity market crisis", "event_label": "Test event",
    }
    selected_suite = suite()
    case = selected_suite["cases"][1]
    left = build_case_pack(
        req, classification, selected_suite,
        {"institution_id": "INST-01", "model": "deepseek:first"}, case,
    )
    right = build_case_pack(
        req, classification, selected_suite,
        {"institution_id": "INST-01", "model": "xai:second"}, case,
    )
    left_visible = _model_case_pack(left)
    right_visible = _model_case_pack(right)
    assert left_visible == right_visible
    encoded = json.dumps(left_visible).lower()
    assert "approved_event" not in encoded
    assert "test_case" not in encoded
    assert "assigned_model" not in encoded
    assert "equity market crisis" not in encoded
    assert '"condition"' not in encoded


def test_scores_come_from_agent_outputs_and_safeguard_checks():
    records = []
    for institution_id in ["INST-01", "INST-02"]:
        control = attach_unmitigated_schedule(
            record(institution_id, "control", "hold", 0, "monitor"), evidence()
        )
        stress = attach_unmitigated_schedule(
            record(institution_id, "stress", "sell", 20, "immediate"), evidence()
        )
        safe = derive_safeguarded_record(
            stress, f"CASE-SAFEGUARDED:{institution_id}:S1",
            "CASE-SAFEGUARDED", evidence(), suite()["safeguard"],
        )
        records.extend([control, stress, safe])
    scores = calculate_scores(
        records, suite(), evidence(), "Equity market crisis",
        {"event_type": "Equity market crisis"},
    )
    assert scores["classification_check"]["matched"] is True
    assert scores["conditions"]["control"]["hold_rate"] == 1
    assert scores["conditions"]["stress"]["risk_off_rate"] == 1
    assert scores["conditions"]["stress"]["mean_normalised_sell_pct"] == 20
    assert scores["conditions"]["safeguarded"]["mean_desired_sell_pct"] == 20
    assert scores["execution_simulation"]["conditions"]["safeguarded"]["scheduled_sell_pct"] == 20
    assert scores["safeguard_effects"]["desired_sell_volume"]["relative_reduction"] == 0
    assert scores["safeguard_effects"]["peak_executed_pressure"]["relative_reduction"] > 0
    assert scores["verification"]["release_eligible"] is True
    assert all(item["passed"] for item in scores["run_scores"])


def test_verification_condition_detail_has_deterministic_order():
    records = []
    for institution_id in ["INST-01", "INST-02"]:
        control = attach_unmitigated_schedule(
            record(institution_id, "control", "hold", 0, "monitor"), evidence()
        )
        stress = attach_unmitigated_schedule(
            record(institution_id, "stress", "sell", 20, "immediate"), evidence()
        )
        safe = derive_safeguarded_record(
            stress, f"CASE-SAFEGUARDED:{institution_id}:S1",
            "CASE-SAFEGUARDED", evidence(), suite()["safeguard"],
        )
        records.extend([control, stress, safe])
    scores = calculate_scores(
        records, suite(), evidence(), "Equity market crisis",
        {"event_type": "Equity market crisis"},
    )
    check = next(
        item for item in scores["verification"]["checks"]
        if item["name"] == "condition_coverage"
    )
    assert check["detail"].endswith(
        "{'control': 2, 'safeguarded': 2, 'stress': 2}."
    )


def test_safeguard_violation_is_visible_in_score():
    bad = record("INST-01", "safeguarded", "sell", 25, "immediate")
    scores = calculate_scores(
        [bad], suite(), evidence(), None,
        {"event_type": "Equity market crisis"},
    )
    item = scores["run_scores"][0]
    assert item["dimensions"]["safeguard_adherence"] == 0
    assert any(value.startswith("FAIL") for value in item["safeguard_checks"])


def test_per_round_cap_and_spacing_are_applied_by_code():
    stress = attach_unmitigated_schedule(
        record("INST-01", "stress", "sell", 40, "immediate"), evidence()
    )
    approved_suite = suite()
    approved_suite["safeguard"]["max_single_asset_sell_pct"] = 20
    item = derive_safeguarded_record(
        stress, "CASE-SAFEGUARDED:INST-01:S1", "CASE-SAFEGUARDED",
        evidence(), approved_suite["safeguard"],
    )
    scores = calculate_scores(
        [stress, item], approved_suite, evidence(), "Equity market crisis",
        {"event_type": "Equity market crisis"},
    )
    result = next(
        value for value in scores["run_scores"]
        if value["condition"] == "safeguarded"
    )
    assert result["dimensions"]["safeguard_adherence"] == 1
    assert result["dimensions"]["portfolio_alignment"] == 1
    sells = [
        value for value in item["execution_schedule"]
        if value["action"] == "sell"
    ]
    assert [value["size_pct"] for value in sells] == [20, 20]
    assert [value["round"] for value in sells] == [1, 2]
    assert [value["scheduled_for"] for value in sells] == [
        "2024-08-06", "2024-08-07",
    ]
    simulation = scores["execution_simulation"]
    assert simulation["conditions"]["stress"]["scheduled_sell_pct"] == 40
    assert simulation["conditions"]["safeguarded"]["scheduled_sell_pct"] == 40
    pressure_effect = simulation["effects"]["peak_executed_pressure"]
    impact_effect = simulation["effects"]["peak_modelled_impact"]
    assert 0 < pressure_effect["relative_reduction"] < 1
    assert impact_effect["unmitigated"] > impact_effect["safeguarded"]


def test_store_persists_configuration_events_and_approval(tmp_path: Path):
    store = LiveStore(tmp_path / "live.sqlite3")
    store.create("AST-TEST", request().model_dump(mode="json"))
    store.approve("AST-TEST", "evidence_review", "Reviewer", True,
                  "Checked source", {})
    assert store.get("AST-TEST")["request"]["institutions"][0]["institution_id"] == "INST-01"
    assert store.events("AST-TEST")[-1]["kind"] == "approval"
    assert store.approvals("AST-TEST")[0]["approved"] is True


def test_restart_marks_running_step_failed_and_retryable(tmp_path: Path):
    store = LiveStore(tmp_path / "live.sqlite3")
    store.create("AST-INTERRUPTED", request().model_dump(mode="json"))
    store.update("AST-INTERRUPTED", status="running", current_step="classify")
    AssessmentEngine(store)
    row = store.get("AST-INTERRUPTED")
    assert row["status"] == "failed"
    assert "restarted" in row["error"]
    assert store.events("AST-INTERRUPTED")[-1]["kind"] == "step_failed"


def test_retry_immediately_returns_running_and_preserves_valid_results(tmp_path: Path):
    store = LiveStore(tmp_path / "retry.sqlite3")
    engine = AssessmentEngine(store)
    store.create("AST-RETRY", request().model_dump(mode="json"))
    saved = [
        {"run_id": "ok", "status": "complete"},
        {"run_id": "bad", "status": "failed"},
    ]
    store.update(
        "AST-RETRY", status="failed", current_step="run_stress",
        error="A required result failed.", responses=saved,
        metrics={"stale": True}, report={"stale": True},
    )
    started = []
    engine._start = lambda assessment_id, target: started.append(
        (assessment_id, target.__name__)
    )

    engine.retry("AST-RETRY")

    row = store.get("AST-RETRY")
    assert row["status"] == "running"
    assert row["error"] == ""
    assert row["responses"] == saved
    assert row["metrics"] is None
    assert row["report"] is None
    assert started == [("AST-RETRY", "_execute")]
    event = store.events("AST-RETRY")[-1]
    assert event["kind"] == "retry_requested"
    assert event["payload"]["retained_valid_decisions"] == 1
    assert event["payload"]["failed_decisions_to_retry"] == 1


def test_approval_transitions_before_worker_and_duplicate_is_idempotent(
    tmp_path: Path,
):
    store = LiveStore(tmp_path / "live.sqlite3")
    engine = AssessmentEngine(store)
    store.create("AST-APPROVAL", request().model_dump(mode="json"))
    store.update(
        "AST-APPROVAL",
        status="awaiting_evidence_approval",
        current_step="evidence_review",
        evidence={
            "items": evidence(), "failures": [],
            "verification": {"passed": True},
        },
    )
    started = []
    engine._start = lambda assessment_id, target: started.append(
        (assessment_id, target.__name__)
    )
    approval = ApprovalRequest(
        actor="Reviewer", note="Checked provenance", approved=True
    )

    engine.approve("AST-APPROVAL", "evidence_review", approval)

    row = store.get("AST-APPROVAL")
    assert row["status"] == "running"
    assert row["current_step"] == "classify"
    assert started == [("AST-APPROVAL", "_classify")]

    engine.approve("AST-APPROVAL", "evidence_review", approval)
    assert len(store.approvals("AST-APPROVAL")) == 1
    assert started == [("AST-APPROVAL", "_classify")]


def test_yahoo_retains_full_crash_path_and_exact_dated_link(monkeypatch):
    def stamp(day: str) -> int:
        return int(datetime.combine(
            date.fromisoformat(day), time(20), timezone.utc
        ).timestamp())

    payload = {"chart": {"result": [{
        "timestamp": [
            stamp("2020-03-13"), stamp("2020-03-16"), stamp("2020-03-17"),
        ],
        "indicators": {"quote": [{
            "close": [2711.02, 2386.13, 2529.19],
            "volume": [8_258_670_000, 7_781_540_000, 8_358_500_000],
        }]},
    }]}}
    monkeypatch.setattr(
        "adcs.live.sources._fetch",
        lambda *args, **kwargs: (json.dumps(payload).encode(), "digest"),
    )
    item = _yahoo(
        "sp500", INSTRUMENTS["sp500"],
        date(2020, 3, 15), date(2020, 3, 17),
    ).model_dump(mode="json")
    assert item["reference_date"] == "2020-03-13"
    assert [row["session_date"] for row in item["observations"]] == [
        "2020-03-13", "2020-03-16", "2020-03-17",
    ]
    assert item["observations"][1]["daily_change_pct"] == pytest.approx(
        -11.984, abs=0.01
    )
    assert item["observations"][2]["daily_change_pct"] == pytest.approx(
        5.995, abs=0.01
    )
    assert item["change_pct"] == pytest.approx(-6.706, abs=0.01)
    assert "period1=" in item["source_url"]
    assert "period2=" in item["source_url"]


def test_evidence_verifier_rejects_last_day_only_snapshot():
    req = request(start_date="2024-08-02", end_date="2024-08-05")
    broken = evidence()
    broken[0]["observations"] = [broken[0]["observations"][-1]]
    result = verify_evidence_snapshot(req, broken)
    assert result["passed"] is False
    assert any(
        item["name"] == "dated_path_integrity" and not item["passed"]
        for item in result["checks"]
    )


def test_cboe_retains_reference_crash_and_rebound(monkeypatch):
    csv_body = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "03/13/2020,71.31,77.57,55.17,57.83\n"
        "03/16/2020,57.83,83.56,57.83,82.69\n"
        "03/17/2020,82.69,84.83,70.37,75.91\n"
    ).encode()
    monkeypatch.setattr(
        "adcs.live.sources._fetch",
        lambda *args, **kwargs: (csv_body, "digest"),
    )
    item = _cboe(date(2020, 3, 15), date(2020, 3, 17)).model_dump(mode="json")
    assert item["reference_date"] == "2020-03-13"
    assert item["window_change_pct"] == pytest.approx(31.264, abs=0.01)
    assert [row["session_date"] for row in item["observations"]] == [
        "2020-03-13", "2020-03-16", "2020-03-17",
    ]


def test_gbpusd_is_derived_from_matched_ecb_fixing_dates(monkeypatch):
    csv_body = (
        "CURRENCY,TIME_PERIOD,OBS_VALUE\n"
        "USD,2020-03-13,1.1104\n"
        "GBP,2020-03-13,0.89308\n"
        "USD,2020-03-16,1.1112\n"
        "GBP,2020-03-16,0.90988\n"
    ).encode()
    monkeypatch.setattr(
        "adcs.live.sources._fetch",
        lambda *args, **kwargs: (csv_body, "digest"),
    )
    item = _ecb(
        ["gbpusd"], date(2020, 3, 16), date(2020, 3, 16)
    )[0].model_dump(mode="json")
    assert item["reference_date"] == "2020-03-13"
    assert item["window_start_date"] == "2020-03-16"
    assert item["window_end_date"] == "2020-03-16"
    assert item["previous_value"] == pytest.approx(1.1104 / 0.89308)
    assert item["value"] == pytest.approx(1.1112 / 0.90988)
    assert item["symbol"] == "GBPUSD"
    assert "matched-date ECB" in item["summary"]


def test_effect_handles_negative_and_zero_baselines_without_mislabeling():
    worse = effect(7.857142857, 20.0, "sell volume", "%")
    assert worse["relative_reduction"] == pytest.approx(-1.5454545)
    assert worse["status"] == "worsened"
    zero = effect(0.0, 5.0, "sell volume", "%")
    assert zero["relative_reduction"] is None
    assert zero["status"] == "worsened"


def test_exchange_schedule_skips_weekend_and_us_market_holiday():
    assert sessions_after("sp500", date(2020, 7, 2), 2) == [
        date(2020, 7, 6), date(2020, 7, 7),
    ]


def test_unexecuted_intent_is_explicit_when_round_capacity_is_insufficient():
    stress = attach_unmitigated_schedule(
        record("INST-01", "stress", "sell", 100, "immediate"), evidence()
    )
    policy = suite()["safeguard"]
    policy["max_single_asset_sell_pct"] = 5
    safe = derive_safeguarded_record(
        stress, "CASE-SAFEGUARDED:INST-01:S1", "CASE-SAFEGUARDED",
        evidence(), policy, simulation_rounds=5,
    )
    sells = [
        item for item in safe["execution_schedule"] if item["action"] == "sell"
    ]
    assert len(sells) == 5
    assert sum(item["size_pct"] for item in sells) == 25
    assert safe["policy_application"]["unexecuted_within_horizon_pct"] == 75


def test_paired_verifier_rejects_changed_safeguarded_intent():
    control = attach_unmitigated_schedule(
        record("INST-01", "control", "hold", 0, "monitor"), evidence()
    )
    stress = attach_unmitigated_schedule(
        record("INST-01", "stress", "sell", 20, "immediate"), evidence()
    )
    safe = derive_safeguarded_record(
        stress, "CASE-SAFEGUARDED:INST-01:S1", "CASE-SAFEGUARDED",
        evidence(), suite()["safeguard"],
    )
    safe["output"]["actions"][0]["size_pct"] = 30
    scores = calculate_scores(
        [control, stress, safe], suite(), evidence(), "Equity market crisis",
        {"event_type": "Equity market crisis"},
    )
    assert scores["verification"]["release_eligible"] is False
    assert any(
        item["name"] == "identical_intent" and not item["passed"]
        for item in scores["verification"]["checks"]
    )


def test_engine_calls_agents_once_then_derives_safeguard(monkeypatch, tmp_path: Path):
    store = LiveStore(tmp_path / "workflow.sqlite3")
    engine = AssessmentEngine(store)
    req = request().model_dump(mode="json")
    store.create("AST-E2E", req)
    store.update(
        "AST-E2E", status="running", current_step="package_cases",
        evidence={
            "items": evidence(), "failures": [],
            "verification": {"passed": True, "checks": []},
        },
        classification={
            "event_type": "Equity market crisis", "event_label": "Test event",
            "confidence": 1.0, "rationale": "Test classification.",
            "evidence_ids": ["MKT-SP500", "MKT-NASDAQ"],
            "evidence_gaps": [],
        },
        plan=suite(),
    )
    calls = []

    def fake_agent(pack, captured_evidence, model, temperature, emit):
        del captured_evidence, model, temperature, emit
        condition = pack["test_case"]["condition"]
        calls.append((condition, pack["institution_id"]))
        source = record(
            pack["institution_id"], condition,
            "hold" if condition == "control" else "sell",
            0 if condition == "control" else 40,
            "monitor" if condition == "control" else "immediate",
        )
        return {
            key: source[key] for key in (
                "tool_plan", "tool_results", "trace", "output", "cached"
            )
        } | {"prompt_hashes": ["test"]}

    monkeypatch.setattr("adcs.live.engine.run_institution_agent", fake_agent)
    engine._execute("AST-E2E")
    completed = store.get("AST-E2E")
    assert calls == [
        ("control", "INST-01"), ("control", "INST-02"),
        ("stress", "INST-01"), ("stress", "INST-02"),
    ]
    assert len(completed["responses"]) == 6
    safe = [
        item for item in completed["responses"]
        if item["condition"] == "safeguarded"
    ]
    assert all(item["decision_origin"] == "deterministic_policy_application" for item in safe)
    assert completed["metrics"]["verification"]["release_eligible"] is True
    assert completed["report"]["verified"] is True
    assert completed["status"] == "awaiting_release_approval"


def test_engine_stops_at_incomplete_condition_then_retries_only_failed_run(
    monkeypatch, tmp_path: Path,
):
    store = LiveStore(tmp_path / "workflow-incomplete.sqlite3")
    engine = AssessmentEngine(store)
    req = request().model_dump(mode="json")
    store.create("AST-INCOMPLETE", req)
    store.update(
        "AST-INCOMPLETE", status="running", current_step="package_cases",
        evidence={
            "items": evidence(), "failures": [],
            "verification": {"passed": True, "checks": []},
        },
        classification={
            "event_type": "Equity market crisis", "event_label": "Test event",
            "confidence": 1.0, "rationale": "Test classification.",
            "evidence_ids": ["MKT-SP500", "MKT-NASDAQ"],
            "evidence_gaps": [],
        },
        plan=suite(),
    )
    calls = []
    failed_once = False

    def flaky_agent(pack, captured_evidence, model, temperature, emit):
        nonlocal failed_once
        del captured_evidence, model, temperature, emit
        condition = pack["test_case"]["condition"]
        calls.append((condition, pack["institution_id"]))
        if (
            condition == "stress"
            and pack["institution_id"] == "INST-01"
            and not failed_once
        ):
            failed_once = True
            raise ValueError("answer remained invalid after three checks")
        source = record(
            pack["institution_id"], condition,
            "hold" if condition == "control" else "sell",
            0 if condition == "control" else 40,
            "monitor" if condition == "control" else "immediate",
        )
        return {
            key: source[key] for key in (
                "tool_plan", "tool_results", "trace", "output", "cached"
            )
        } | {"prompt_hashes": ["test"]}

    monkeypatch.setattr("adcs.live.engine.run_institution_agent", flaky_agent)
    with pytest.raises(RuntimeError, match="1 of 2 required results are valid"):
        engine._execute("AST-INCOMPLETE")

    stopped = store.get("AST-INCOMPLETE")
    assert stopped["current_step"] == "run_stress"
    assert stopped["metrics"] is None
    assert stopped["report"] is None
    assert not any(
        item["condition"] == "safeguarded" for item in stopped["responses"]
    )
    events = store.events("AST-INCOMPLETE")
    assert any(item["kind"] == "condition_incomplete" for item in events)
    assert not any(
        item["step"] == "run_stress" and item["kind"] == "step_complete"
        for item in events
    )
    assert not any(item["step"] == "score" for item in events)

    engine._execute("AST-INCOMPLETE")
    completed = store.get("AST-INCOMPLETE")
    assert calls == [
        ("control", "INST-01"), ("control", "INST-02"),
        ("stress", "INST-01"), ("stress", "INST-02"),
        ("stress", "INST-01"),
    ]
    assert len(completed["responses"]) == 6
    assert all(item["status"] == "complete" for item in completed["responses"])
    assert completed["metrics"]["verification"]["release_eligible"] is True
    assert completed["status"] == "awaiting_release_approval"


def test_engine_does_not_complete_score_or_start_report_when_verification_fails(
    monkeypatch, tmp_path: Path,
):
    store = LiveStore(tmp_path / "workflow-score-gate.sqlite3")
    engine = AssessmentEngine(store)
    req = request().model_dump(mode="json")
    store.create("AST-SCORE-GATE", req)
    store.update(
        "AST-SCORE-GATE", status="running", current_step="package_cases",
        evidence={
            "items": evidence(), "failures": [],
            "verification": {"passed": True, "checks": []},
        },
        classification={
            "event_type": "Equity market crisis", "event_label": "Test event",
            "confidence": 1.0, "rationale": "Test classification.",
            "evidence_ids": ["MKT-SP500", "MKT-NASDAQ"],
            "evidence_gaps": [],
        },
        plan=suite(),
    )

    def valid_agent(pack, captured_evidence, model, temperature, emit):
        del captured_evidence, model, temperature, emit
        condition = pack["test_case"]["condition"]
        source = record(
            pack["institution_id"], condition,
            "hold" if condition == "control" else "sell",
            0 if condition == "control" else 20,
            "monitor" if condition == "control" else "immediate",
        )
        return {
            key: source[key] for key in (
                "tool_plan", "tool_results", "trace", "output", "cached"
            )
        } | {"prompt_hashes": ["test"]}

    blocked_scores = {
        "run_scores": [],
        "verification": {
            "release_eligible": False,
            "checks": [{
                "name": "quality_thresholds",
                "passed": False,
                "detail": "One required result fell below the approved threshold.",
            }],
        },
    }
    monkeypatch.setattr("adcs.live.engine.run_institution_agent", valid_agent)
    monkeypatch.setattr(
        "adcs.live.engine.calculate_scores", lambda *args: blocked_scores
    )

    with pytest.raises(RuntimeError, match="stopped before report generation"):
        engine._execute("AST-SCORE-GATE")

    stopped = store.get("AST-SCORE-GATE")
    assert stopped["current_step"] == "score"
    assert stopped["metrics"] == blocked_scores
    assert stopped["report"] is None
    events = store.events("AST-SCORE-GATE")
    assert any(
        item["step"] == "score" and item["kind"] == "verification_blocked"
        for item in events
    )
    assert not any(
        item["step"] == "score" and item["kind"] == "step_complete"
        for item in events
    )
    assert not any(item["step"] == "synthesise" for item in events)


def test_fast_demo_uses_ai_signal_then_exact_approved_rule_and_daily_cap():
    req = request(
        execution_mode="deterministic_stress_rules",
        institutions=[{
            "institution_id": "INST-05", "model": "deepseek:agent-a",
        }],
    ).model_dump(mode="json")
    approved_suite = suite()
    approved_suite["execution_mode"] = "deterministic_stress_rules"
    approved_suite["deterministic_rules"] = default_stress_rules(["INST-05"])
    approved_suite["safeguard"].update({
        "max_single_asset_sell_pct": 10,
        "max_daily_portfolio_sell_pct": 10,
        "require_staged_execution": False,
        "allow_hedging": False,
        "carry_unexecuted_intent": False,
    })
    pack = build_case_pack(
        req,
        {"event_type": "Equity market crisis", "event_label": "Test"},
        approved_suite,
        req["institutions"][0],
        approved_suite["cases"][1],
    )
    signal = InstitutionStressSignal.model_validate({
        "stress_detected": True,
        "severity": 5,
        "summary": "The approved evidence indicates material equity stress.",
        "confidence": 0.9,
        "evidence_ids": ["MKT-SP500"],
        "trigger_ids": [],
    })
    rule = approved_suite["deterministic_rules"][0]
    decision = decision_from_stress_rule(pack, signal, rule)
    assert {item["size_pct"] for item in decision["actions"]} == {20}
    assert {item["asset_id"] for item in decision["actions"]} == {
        "sp500", "nasdaq",
    }

    stress_record = record("INST-05", "stress", "sell", 20, "immediate")
    stress_record.update({
        "portfolio": pack["portfolio"],
        "output": decision,
        "stress_signal": signal.model_dump(),
        "deterministic_rule": rule,
        "decision_origin": "deterministic_stress_rule",
    })
    stress_record = attach_unmitigated_schedule(stress_record, evidence())
    safeguarded = derive_safeguarded_record(
        stress_record,
        "CASE-SAFEGUARDED:INST-05:S1",
        "CASE-SAFEGUARDED",
        evidence(),
        approved_suite["safeguard"],
    )
    by_round: dict[int, float] = {}
    for item in safeguarded["execution_schedule"]:
        by_round[item["round"]] = by_round.get(item["round"], 0.0) + (
            item["portfolio_weight"] * item["size_pct"]
        )
    assert by_round == {1: 10.0}
    assert safeguarded["policy_application"]["cap_basis"] == (
        "pct_of_starting_portfolio_value_per_session"
    )


def test_stress_signal_accepts_only_two_decisive_evidence_items():
    with pytest.raises(ValidationError):
        InstitutionStressSignal.model_validate({
            "stress_detected": True,
            "severity": 4,
            "summary": "Stress is visible in the approved evidence.",
            "confidence": 0.9,
            "evidence_ids": ["MKT-A", "MKT-B", "MKT-C"],
            "trigger_ids": [],
        })


def test_limit_breach_loop_forces_later_unmitigated_selling_only():
    captured = evidence()
    for source, item_id, symbol in (
        (captured[0], "MKT-NIKKEI", "^N225"),
        (captured[1], "MKT-EUROSTOXX", "^STOXX50E"),
    ):
        captured.append({**source, "id": item_id, "symbol": symbol})
    policy = suite()["safeguard"]
    policy.update({
        "max_single_asset_sell_pct": 10,
        "max_daily_portfolio_sell_pct": 10,
        "carry_unexecuted_intent": False,
        "require_staged_execution": False,
        "allow_hedging": False,
    })
    records = []
    for institution_id in ("INST-01", "INST-02", "INST-03", "INST-04"):
        stress = attach_unmitigated_schedule(
            record(institution_id, "stress", "sell", 20, "immediate"),
            captured,
        )
        safe = derive_safeguarded_record(
            stress,
            f"CASE-SAFEGUARDED:{institution_id}:S1",
            "CASE-SAFEGUARDED",
            captured,
            policy,
        )
        records.extend([stress, safe])
    approved_suite = suite()
    approved_suite["safeguard"] = policy
    result = simulate(records, approved_suite, captured)
    stress = result["conditions"]["stress"]
    safe = result["conditions"]["safeguarded"]
    assert stress["rounds"][1]["feedback_sell_pct"] > 0
    assert stress["feedback_sell_pct"] > 0
    assert safe["feedback_sell_pct"] == 0
    assert result["loss_decomposition"]["safeguard_effect_denominator"] == (
        "model_added_loss_only"
    )


def test_every_displayed_simulation_number_is_replayed_and_tamper_detected(
    tmp_path: Path,
):
    captured = evidence()
    records = []
    for institution_id in ("INST-01", "INST-02"):
        stress = attach_unmitigated_schedule(
            record(institution_id, "stress", "sell", 20, "immediate"),
            captured,
        )
        safe = derive_safeguarded_record(
            stress,
            f"CASE-SAFEGUARDED:{institution_id}:S1",
            "CASE-SAFEGUARDED",
            captured,
            suite()["safeguard"],
        )
        records.extend([stress, safe])

    simulation = simulate(records, suite(), captured)
    arithmetic = {
        item["name"]: item for item in simulation["verification"]["checks"]
        if item["name"] in {
            "displayed_aggregates",
            "displayed_effect_formulas",
            "deterministic_numeric_replay",
        }
    }
    assert set(arithmetic) == {
        "displayed_aggregates",
        "displayed_effect_formulas",
        "deterministic_numeric_replay",
    }
    assert all(item["passed"] for item in arithmetic.values())

    row = {
        "id": "AST-ARITHMETIC",
        "status": "awaiting_release_approval",
        "request": {
            "execution_mode": "deterministic_stress_rules",
            "start_date": "2024-08-02",
            "end_date": "2024-08-05",
            "institutions": [
                {"institution_id": institution_id, "model": "test:model"}
                for institution_id in ("INST-01", "INST-02")
            ],
        },
        "classification": {"event_label": "Arithmetic test"},
        "plan": suite(),
        "evidence": {"items": captured},
        "responses": records,
        "metrics": {"execution_simulation": simulation},
    }
    projection = build_demo_result(row, [], [])
    assert projection["display_verification"]["passed"] is True
    assert projection["display_verification"]["checked_fields"] > 100

    tampered = json.loads(json.dumps(row))
    tampered["metrics"]["execution_simulation"]["conditions"]["stress"][
        "rounds"
    ][0]["net_executed_sell_pct"] += 1
    projection = build_demo_result(tampered, [], [])
    assert projection["display_verification"]["passed"] is False

    store = LiveStore(tmp_path / "tampered-release.sqlite3")
    store.create("AST-TAMPERED", request().model_dump(mode="json"))
    store.update(
        "AST-TAMPERED",
        status="awaiting_release_approval",
        current_step="release_review",
        evidence={"items": captured, "verification": {"passed": True}},
        responses=tampered["responses"],
        metrics={
            "verification": {"release_eligible": True},
            "execution_simulation": tampered["metrics"]["execution_simulation"],
        },
    )
    engine = AssessmentEngine(store)
    with pytest.raises(WorkflowConflict, match="arithmetic replay"):
        engine.approve(
            "AST-TAMPERED",
            "release_review",
            ApprovalRequest(actor="Reviewer", approved=True),
        )


def test_fast_demo_result_headline_is_computed_from_persisted_metrics():
    metrics = {
        "execution_simulation": {
            "conditions": {
                "stress": {
                    "rounds": [], "feedback_sell_pct": 2.0,
                    "terminal_system_price_impact_pct": 0.1,
                    "scheduled_sell_pct": 20.0,
                },
                "safeguarded": {
                    "rounds": [], "feedback_sell_pct": 1.0,
                    "terminal_system_price_impact_pct": 0.05,
                    "scheduled_sell_pct": 10.0,
                },
            },
            "effects": {
                "peak_modelled_impact": {
                    "unmitigated": 0.5, "safeguarded": 0.3,
                    "relative_reduction": 0.4, "status": "improved",
                    "interpretation": "Safeguarded impact was 40% lower.",
                },
                "portfolio_loss_at_horizon": {
                    "unmitigated": 3.0, "safeguarded": 2.7,
                    "relative_reduction": 0.1, "status": "improved",
                    "interpretation": "Safeguarded loss was 10% lower.",
                },
                "feedback_selling": {
                    "unmitigated": 2.0, "safeguarded": 1.0,
                    "relative_reduction": 0.5, "status": "improved",
                    "interpretation": "Safeguarded feedback was 50% lower.",
                },
            },
            "settings": {"model_version": "test", "rounds": 5},
            "input_sha256": "abc",
        },
        "verification": {"release_eligible": True, "checks": []},
    }
    row = {
        "id": "AST-DEMO",
        "status": "awaiting_release_approval",
        "request": {
            "execution_mode": "deterministic_stress_rules",
            "start_date": "2024-08-02", "end_date": "2024-08-05",
            "institutions": [],
        },
        "classification": {"event_label": "August 2024 turmoil"},
        "plan": {
            "safeguard": {
                "max_daily_portfolio_sell_pct": 10,
                "max_single_asset_sell_pct": 10,
            },
            "deterministic_rules": [],
        },
        "evidence": {"items": []},
        "responses": [],
        "metrics": metrics,
    }
    result = build_demo_result(row, [], [])
    assert result is not None
    assert result["headline"] == (
        "Replacing the 20% stress sale with a 10% safeguarded sale reduced "
        "modelled peak market impact by 40.0%."
    )
    assert result["cards"][3]["effect"]["relative_reduction"] == 0.5
    row["status"] = "rejected"
    assert build_demo_result(row, [], [])["status"] == "rejected"

    metrics["execution_simulation"]["conditions"]["stress"].update({
        "scheduled_sell_pct": 0.0,
        "feedback_sell_pct": 0.0,
    })
    metrics["execution_simulation"]["conditions"]["safeguarded"].update({
        "scheduled_sell_pct": 0.0,
        "feedback_sell_pct": 0.0,
    })
    zero_result = build_demo_result(row, [], [])
    assert zero_result["headline"] == (
        "No institution stress flag activated the selling rule; neither the "
        "20% nor the 10% path sold."
    )
    assert not any(
        "No modelled limit breach" in warning
        for warning in zero_result["warnings"]
    )


def test_engine_fast_path_calls_ai_for_signal_and_reaches_release_result(
    monkeypatch, tmp_path: Path,
):
    store = LiveStore(tmp_path / "deterministic-workflow.sqlite3")
    engine = AssessmentEngine(store)
    req = request(
        execution_mode="deterministic_stress_rules",
        institutions=[{
            "institution_id": "INST-01", "model": "deepseek:agent-a",
        }],
    ).model_dump(mode="json")
    approved_suite = suite()
    approved_suite["execution_mode"] = "deterministic_stress_rules"
    approved_suite["deterministic_rules"] = default_stress_rules(["INST-01"])
    approved_suite["safeguard"].update({
        "max_single_asset_sell_pct": 10,
        "max_daily_portfolio_sell_pct": 10,
        "require_staged_execution": False,
        "allow_hedging": False,
        "carry_unexecuted_intent": False,
    })
    store.create("AST-DETERMINISTIC", req)
    store.update(
        "AST-DETERMINISTIC",
        status="running",
        current_step="package_cases",
        evidence={
            "items": evidence(), "failures": [],
            "verification": {"passed": True, "checks": []},
        },
        classification={
            "event_type": "Equity market crisis",
            "event_label": "Test event",
            "confidence": 1.0,
            "rationale": "Test classification.",
            "evidence_ids": ["MKT-SP500"],
            "evidence_gaps": [],
        },
        plan=approved_suite,
    )
    calls = []

    def fake_detector(
        pack, captured_evidence, model, temperature, emit,
        execution_mode="llm_decision", deterministic_rule=None,
    ):
        del captured_evidence, temperature, emit
        calls.append((pack["test_case"]["condition"], model, execution_mode))
        detected = pack["test_case"]["condition"] == "stress"
        signal = InstitutionStressSignal.model_validate({
            "stress_detected": detected,
            "severity": 5 if detected else 1,
            "summary": "Stress detected." if detected else "No stress detected.",
            "confidence": 0.9,
            "evidence_ids": ["MKT-SP500"],
            "trigger_ids": [],
        })
        decision = decision_from_stress_rule(
            pack, signal, deterministic_rule
        )
        return {
            "tool_plan": {
                "plan_summary": "Inspect the approved inputs.",
                "tool_requests": [
                    "portfolio_shock", "constraint_register", "evidence_lookup",
                ],
            },
            "tool_results": [
                {"tool": "portfolio_shock"},
                {"tool": "constraint_register"},
                {"tool": "evidence_lookup"},
            ],
            "output": decision,
            "trace": [],
            "prompt_hashes": ["test"],
            "stress_signal": signal.model_dump(),
            "deterministic_rule": deterministic_rule,
            "decision_origin": "deterministic_stress_rule",
            "cached": False,
        }

    monkeypatch.setattr("adcs.live.engine.run_institution_agent", fake_detector)
    engine._execute("AST-DETERMINISTIC")
    completed = store.get("AST-DETERMINISTIC")
    assert calls == [
        ("control", "deepseek:agent-a", "deterministic_stress_rules"),
        ("stress", "deepseek:agent-a", "deterministic_stress_rules"),
    ]
    assert completed["status"] == "awaiting_release_approval"
    assert completed["metrics"]["verification"]["release_eligible"] is True
    result = build_demo_result(
        completed, store.approvals("AST-DETERMINISTIC"),
        store.events("AST-DETERMINISTIC"),
    )
    assert result is not None
    assert result["rules"][0]["safeguarded_execution_pct"] == [10]
    assert result["rules"][0]["safeguarded_text"] == "10% on session 1"
    assert result["stress_signals"][0]["cited"] == ["S&P 500 (GSPC)"]
    assert result["stress_signals"][0]["evidence_ids"] == ["MKT-SP500"]
