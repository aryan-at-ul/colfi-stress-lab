from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from adcs.experiment.catalog import (
    V5_INSTITUTION_PROFILES,
    build_confirmatory_preregistration,
    profile_catalog_readiness,
)
from adcs.experiment.models import (
    EvidenceSnapshotV5,
    FrozenEvidenceItem,
    InstitutionDecisionV5,
    InstitutionProfileV5,
    ModelRegistration,
    content_sha256,
)
from adcs.experiment.execution import execute_feedback_cell, execute_initial_cell
from adcs.experiment.feedback import build_feedback_run_cells
from adcs.experiment.metrics import (
    compute_feedback_trajectories,
    compute_initial_estimates,
)
from adcs.experiment.runner import build_initial_run_cells
from adcs.experiment.store import ExperimentStore
from adcs.experiment.transition import (
    transition_feedback_world,
    transition_initial_world,
)
from adcs.experiment.worlds import build_feedback_worlds
from adcs.llm.client import Completion, ModelSpec


MODELS = [
    "deepseek:deepseek-v4-flash",
    "deepseek:deepseek-v4-pro",
    "anthropic:claude-haiku-4-5-20251001",
    "anthropic:claude-sonnet-5",
    "xai:grok-4.3",
    "xai:grok-4.6",
]


def _evidence_snapshot(
    snapshot_id: str = "EVIDENCE-TEST-V1",
) -> EvidenceSnapshotV5:
    observed_at = datetime(2024, 8, 5, 13, 30, tzinfo=timezone.utc)
    return EvidenceSnapshotV5(
        snapshot_id=snapshot_id,
        decision_cutoff=datetime(2024, 8, 5, 13, 35, tzinfo=timezone.utc),
        created_by="Evidence curator",
        items=[FrozenEvidenceItem(
            evidence_id="EVD-SP500-OPEN",
            evidence_class="public_fact",
            title="S&P 500 opening observation",
            instrument_id="sp500",
            value=5186.33,
            unit="index_points",
            observed_at=observed_at,
            available_at=observed_at,
            source_url="https://example.invalid/sp500-source",
            acquisition_url="https://example.invalid/sp500-acquisition",
            raw_sha256="a" * 64,
        )],
    )


def test_profile_catalog_uses_workbook_risk_states_and_user_holdings():
    readiness = profile_catalog_readiness()
    assert readiness["ready"] is True
    assert readiness["footprint_total_pct"] == 100.0
    assert [
        V5_INSTITUTION_PROFILES[f"INST-{index:02d}"].system_footprint_pct
        for index in range(1, 8)
    ] == [15.0, 12.0, 28.0, 18.0, 10.0, 9.0, 8.0]

    expected = {
        "INST-01": {"sp500": 100.0},
        "INST-02": {"nasdaq": 100.0},
        "INST-03": {"nikkei": 100.0},
        "INST-04": {"eurostoxx": 100.0},
        "INST-05": {"sp500": 50.0, "nasdaq": 50.0},
        "INST-06": {"nikkei": 50.0, "eurostoxx": 50.0},
        "INST-07": {
            "sp500": 25.0,
            "nasdaq": 25.0,
            "nikkei": 25.0,
            "eurostoxx": 25.0,
        },
    }
    actual = {
        institution_id: {
            holding.instrument_id: holding.notional_pct
            for holding in profile.holdings
        }
        for institution_id, profile in V5_INSTITUTION_PROFILES.items()
    }
    assert actual == expected
    assert (
        V5_INSTITUTION_PROFILES["INST-01"].triggers[0].observed_value == 88.0
    )
    assert (
        V5_INSTITUTION_PROFILES["INST-04"].liquidity_state.market_depth_pct_of_normal
        == 55.0
    )


def test_private_profile_payload_excludes_provenance_and_other_institutions():
    for institution_id, profile in V5_INSTITUTION_PROFILES.items():
        payload = profile.private_model_payload()
        rendered = str(payload)
        assert payload["institution_id"] == institution_id
        assert "sources" not in payload
        assert "catalog_version" not in payload
        assert "holdings_status" not in payload
        for other_id in V5_INSTITUTION_PROFILES:
            if other_id != institution_id:
                assert other_id not in rendered


def test_cyclic_cohorts_are_balanced_and_preregistration_is_hashable():
    plan = build_confirmatory_preregistration(MODELS, "PREREG-TEST-V1")
    assert len(plan.heterogeneous_cohorts) == 6
    assert len(plan.institution_profile_version_ids) == 7
    assert len(plan.sha256) == 64
    for profile_id in plan.institution_profile_version_ids:
        assigned = [
            cohort.assignments[profile_id]
            for cohort in plan.heterogeneous_cohorts
        ]
        assert sorted(assigned) == sorted(MODELS)


def _action_base(action_id: str) -> dict:
    return {
        "action_id": action_id,
        "urgency": 4,
        "confidence": 0.8,
        "rationale": "The approved risk state is close to its limit.",
        "evidence_ids": ["MKT-SP500"],
    }


def test_discriminated_actions_preserve_size_basis_and_liquidity_semantics():
    decision = InstitutionDecisionV5(
        decision_id="DEC-TEST-01",
        institution_id="INST-01",
        stage="initial",
        stance="risk_reduce",
        executive_decision="Reduce exposure and displayed liquidity.",
        constraints_considered=["Daily VaR limit"],
        evidence_ids=["MKT-SP500"],
        actions=[
            {
                **_action_base("ACT-SELL-01"),
                "action_type": "sell",
                "instrument_id": "sp500",
                "size_value": 10.0,
                "size_basis": "footprint_notional_pct",
            },
            {
                **_action_base("ACT-DELEV-01"),
                "action_type": "deleverage",
                "size_value": 5.0,
                "size_basis": "gross_exposure_reduction_pct",
                "liquidation_priority": "pro_rata",
            },
            {
                **_action_base("ACT-DEPTH-01"),
                "action_type": "withdraw_liquidity",
                "market_instrument_id": "sp500",
                "size_value": 20.0,
                "size_basis": "provided_depth_pct",
            },
        ],
    )
    assert [action.action_type for action in decision.actions] == [
        "sell", "deleverage", "withdraw_liquidity"
    ]
    assert decision.actions[2].size_basis == "provided_depth_pct"

    invalid = decision.model_dump(mode="json")
    invalid["actions"][0]["size_basis"] = "position_pct"
    with pytest.raises(ValidationError):
        InstitutionDecisionV5.model_validate(invalid)


def test_hold_stance_cannot_hide_active_actions():
    with pytest.raises(ValidationError, match="hold stance"):
        InstitutionDecisionV5(
            decision_id="DEC-TEST-02",
            institution_id="INST-01",
            stage="initial",
            stance="hold",
            executive_decision="Hold while selling.",
            constraints_considered=["Daily VaR limit"],
            evidence_ids=["MKT-SP500"],
            actions=[{
                **_action_base("ACT-SELL-02"),
                "action_type": "sell",
                "instrument_id": "sp500",
                "size_value": 5.0,
            }],
        )


def test_evidence_snapshot_rejects_information_available_after_cutoff():
    payload = _evidence_snapshot().model_dump()
    payload["items"][0]["available_at"] = datetime(
        2024, 8, 5, 13, 36, tzinfo=timezone.utc
    )
    with pytest.raises(ValidationError, match="post-cutoff evidence"):
        EvidenceSnapshotV5.model_validate(payload)


def test_sqlite_foundation_is_normalized_hashed_and_append_only(tmp_path):
    path = tmp_path / "experiment.sqlite3"
    store = ExperimentStore(path)
    for profile in V5_INSTITUTION_PROFILES.values():
        store.register_profile(profile)
    for model_id in MODELS:
        provider, exact_model = model_id.split(":", 1)
        store.register_model(ModelRegistration(
            model_id=model_id,
            provider=provider,
            exact_model=exact_model,
        ))
    plan = build_confirmatory_preregistration(MODELS, "PREREG-SQLITE-V1")
    digest = store.create_preregistration(plan)
    assert digest == plan.sha256

    with pytest.raises(ValueError, match="does not match"):
        store.approve_preregistration(
            plan.preregistration_id,
            "APR-WRONG-HASH",
            "Reviewer",
            "stale plan",
            "0" * 64,
        )
    store.approve_preregistration(
        plan.preregistration_id,
        "APR-CORRECT-HASH",
        "Reviewer",
        "Approved exact plan.",
        digest,
    )
    stored = store.get_preregistration(plan.preregistration_id)
    assert stored is not None
    assert stored["sha256"] == digest
    assert stored["approvals"][0]["approved"] is True

    evidence = _evidence_snapshot()
    evidence_digest = store.register_evidence(evidence)
    with pytest.raises(ValueError, match="does not match"):
        store.approve_evidence(
            evidence.snapshot_id,
            "APR-EVIDENCE-WRONG",
            "Reviewer",
            "stale evidence",
            "0" * 64,
        )
    store.approve_evidence(
        evidence.snapshot_id,
        "APR-EVIDENCE-CORRECT",
        "Reviewer",
        "Approved exact evidence snapshot.",
        evidence_digest,
    )
    assert store.approvals_ready(
        plan.preregistration_id, evidence.snapshot_id
    )["ready"] is True
    assert store.verify_audit_chain() is True

    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        profile_count = db.execute(
            "SELECT count(*) FROM experiment_profile_versions"
        ).fetchone()[0]
        holding_count = db.execute(
            "SELECT count(*) FROM experiment_holdings"
        ).fetchone()[0]
        assert profile_count == 7
        assert holding_count == 12
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(
                "UPDATE experiment_profile_versions SET holdings_status='unmapped' "
                "WHERE id='IPV5-INST-01-V2'"
            )

        db.execute(
            "INSERT INTO experiment_run_cells "
            "(id,preregistration_id,evidence_snapshot_id,stage,world_id,"
            "institution_profile_version_id,model_spec_id,replicate_index,"
            "execution_order,case_input_sha256,case_input_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "CELL-INITIAL-1",
                plan.preregistration_id,
                evidence.snapshot_id,
                "initial",
                None,
                "IPV5-INST-01-V2",
                MODELS[0],
                1,
                0,
                "b" * 64,
                '{"test":true}',
                "2026-09-04T00:00:00Z",
            ),
        )
        db.execute(
            "INSERT INTO experiment_worlds "
            "(id,preregistration_id,evidence_snapshot_id,assignment_kind,assignment_key,"
            "replicate_index,assignment_sha256,stage1_action_set_sha256,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "WORLD-WRONG-REPLICATE",
                plan.preregistration_id,
                evidence.snapshot_id,
                "shared",
                "SHARED-1",
                2,
                "1" * 64,
                "2" * 64,
                "2026-09-04T00:00:00Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="does not match"):
            db.execute(
                "INSERT INTO experiment_world_members "
                "(world_id,institution_profile_version_id,model_spec_id,"
                "initial_run_cell_id,ordinal) VALUES (?,?,?,?,?)",
                (
                    "WORLD-WRONG-REPLICATE",
                    "IPV5-INST-01-V2",
                    MODELS[0],
                    "CELL-INITIAL-1",
                    0,
                ),
            )


def test_initial_grid_is_deterministic_balanced_and_double_gated(tmp_path):
    store = ExperimentStore(tmp_path / "initial-grid.sqlite3")
    for profile in V5_INSTITUTION_PROFILES.values():
        store.register_profile(profile)
    for model_id in MODELS:
        provider, exact_model = model_id.split(":", 1)
        store.register_model(ModelRegistration(
            model_id=model_id,
            provider=provider,
            exact_model=exact_model,
        ))

    plan = build_confirmatory_preregistration(MODELS, "PREREG-GRID-V1")
    evidence = _evidence_snapshot("EVIDENCE-GRID-V1")
    store.create_preregistration(plan)
    store.register_evidence(evidence)
    cells = build_initial_run_cells(
        plan, evidence, list(V5_INSTITUTION_PROFILES.values())
    )
    assert cells == build_initial_run_cells(
        plan, evidence, list(reversed(V5_INSTITUTION_PROFILES.values()))
    )
    assert len(cells) == 210
    assert [cell["execution_order"] for cell in cells] == list(range(1, 211))
    assert all(cell["stage"] == "initial" for cell in cells)
    assert all(cell["world_id"] is None for cell in cells)
    assert len({cell["case_input_sha256"] for cell in cells}) == 7
    for profile_id in plan.institution_profile_version_ids:
        profile_cells = [
            cell for cell in cells
            if cell["institution_profile_version_id"] == profile_id
        ]
        assert len(profile_cells) == 30
        assert len({cell["case_input_sha256"] for cell in profile_cells}) == 1
        assert len({cell["model_spec_id"] for cell in profile_cells}) == 6
    for model_id in MODELS:
        assert sum(cell["model_spec_id"] == model_id for cell in cells) == 35
    assert all("grok-4.6" not in cell["case_input_json"] for cell in cells)
    assert all("replicate_index" not in cell["case_input_json"] for cell in cells)

    with pytest.raises(PermissionError, match="approvals"):
        store.materialize_initial_cells(cells, "Experiment operator")
    store.approve_preregistration(
        plan.preregistration_id,
        "APR-GRID-PLAN",
        "Plan reviewer",
        "Approved exact grid contract.",
        plan.sha256,
    )
    with pytest.raises(PermissionError, match="approvals"):
        store.materialize_initial_cells(cells, "Experiment operator")
    store.approve_evidence(
        evidence.snapshot_id,
        "APR-GRID-EVIDENCE",
        "Evidence reviewer",
        "Approved point-in-time evidence.",
        evidence.sha256,
    )
    summary = store.materialize_initial_cells(cells, "Experiment operator")
    assert summary["cell_count"] == 210
    assert summary["planned_call_count"] == 210
    assert summary["attempted_cell_count"] == 0
    assert summary["plan_sha256"] == store.get_initial_plan(
        plan.preregistration_id
    )["plan_sha256"]
    assert store.verify_audit_chain() is True


def test_live_initial_cell_persists_exact_request_raw_response_and_typed_decision(
    tmp_path,
):
    store = ExperimentStore(tmp_path / "live-cell.sqlite3")
    for profile in V5_INSTITUTION_PROFILES.values():
        store.register_profile(profile)
    for model_id in MODELS:
        provider, exact_model = model_id.split(":", 1)
        store.register_model(ModelRegistration(
            model_id=model_id,
            provider=provider,
            exact_model=exact_model,
        ))
    plan = build_confirmatory_preregistration(MODELS, "PREREG-LIVE-CELL-V1")
    evidence = _evidence_snapshot("EVIDENCE-LIVE-CELL-V1")
    store.create_preregistration(plan)
    store.register_evidence(evidence)
    store.approve_preregistration(
        plan.preregistration_id,
        "APR-LIVE-CELL-PLAN",
        "Plan reviewer",
        "Approved.",
        plan.sha256,
    )
    store.approve_evidence(
        evidence.snapshot_id,
        "APR-LIVE-CELL-EVIDENCE",
        "Evidence reviewer",
        "Approved.",
        evidence.sha256,
    )
    cells = build_initial_run_cells(
        plan, evidence, list(V5_INSTITUTION_PROFILES.values())
    )
    store.materialize_initial_cells(cells, "Experiment operator")
    cell = store.next_pending_initial_cell(plan.preregistration_id)
    case_input = json.loads(cell["case_input_json"])
    instrument_id = case_input["private_institution"]["holdings"][0]["instrument_id"]

    class FakeLiveLLM:
        calls = 0

        def __init__(self, mode):
            assert mode == "live"

        def context_stats(self, spec, system, user, max_tokens):
            return {"effective_output_tokens": max_tokens}

        def complete(self, spec, system, user, temperature, max_tokens):
            self.calls += 1
            model = str(ModelSpec.parse(spec))
            request_json = json.dumps(
                [model, system, user, temperature, max_tokens],
                separators=(",", ":"),
            )
            raw = json.dumps({
                "schema_version": 5,
                "stance": "risk_reduce",
                "executive_decision": "Reduce the declared equity position.",
                "actions": [{
                    "action_id": "ACT-FAKE-01",
                    "action_type": "sell",
                    "instrument_id": instrument_id,
                    "direction": "reduce_long",
                    "size_value": 10.0,
                    "size_basis": "footprint_notional_pct",
                    "urgency": 4,
                    "confidence": 0.8,
                    "rationale": "The frozen opening observation supports caution.",
                    "evidence_ids": ["EVD-SP500-OPEN"],
                }],
                "constraints_considered": ["Declared mandate constraints"],
                "evidence_ids": ["EVD-SP500-OPEN"],
            }, separators=(",", ":"))
            return Completion(
                text=raw,
                spec=ModelSpec.parse(spec),
                prompt_sha256=hashlib.sha256(request_json.encode()).hexdigest(),
                cached=False,
                latency_ms=12.5,
                usage={"input_tokens": 100, "output_tokens": 80},
                finish_reason="stop",
            )

    decision = execute_initial_cell(
        store, plan, cell, actor="Run operator", llm_factory=FakeLiveLLM
    )
    assert decision.institution_id == cell["institution_id"]
    assert decision.stage == "initial"
    accepted = store.accepted_initial_decisions(plan.preregistration_id)
    assert len(accepted) == 1
    assert accepted[0]["decision"]["actions"][0]["instrument_id"] == instrument_id
    with sqlite3.connect(store.path) as db:
        attempt = db.execute(
            "SELECT outcome,provider_request_json,raw_response_text,"
            "provider_request_sha256,raw_response_sha256,usage_json "
            "FROM experiment_run_attempts"
        ).fetchone()
    assert attempt[0] == "accepted"
    assert hashlib.sha256(attempt[1].encode()).hexdigest() == attempt[3]
    assert hashlib.sha256(attempt[2].encode()).hexdigest() == attempt[4]
    assert json.loads(attempt[5])["output_tokens"] == 80
    assert store.verify_audit_chain() is True


def test_pairwise_estimator_recovers_same_model_agreement_and_model_effects(tmp_path):
    plan = build_confirmatory_preregistration(MODELS, "PREREG-METRICS-V1")
    records = []
    for institution_index, profile_id in enumerate(
        plan.institution_profile_version_ids, start=1
    ):
        institution_id = f"INST-{institution_index:02d}"
        for model_index, model_id in enumerate(MODELS):
            for replicate in range(1, 6):
                active = model_index < 3
                decision = InstitutionDecisionV5(
                    decision_id=(
                        f"DEC-I{institution_index:02d}-M{model_index:02d}-R{replicate:02d}"
                    ),
                    institution_id=institution_id,
                    stage="initial",
                    stance="risk_reduce" if active else "hold",
                    executive_decision=(
                        "Reduce the position." if active else "Maintain the position."
                    ),
                    actions=([{
                        "action_id": "ACT-SELL-01",
                        "action_type": "sell",
                        "instrument_id": "sp500",
                        "size_value": 5.0 + model_index,
                        "size_basis": "footprint_notional_pct",
                        "urgency": model_index + 1,
                        "confidence": 0.8,
                        "rationale": "Frozen evidence supports this response.",
                        "evidence_ids": ["EVD-SP500-OPEN"],
                    }] if active else []),
                    constraints_considered=["Declared mandate"],
                    evidence_ids=["EVD-SP500-OPEN"],
                )
                records.append({
                    "institution_profile_version_id": profile_id,
                    "institution_id": institution_id,
                    "model_spec_id": model_id,
                    "replicate_index": replicate,
                    "decision": decision.model_dump(mode="json"),
                })

    result = compute_initial_estimates(
        records, plan, permutations=25, bootstrap_samples=100
    )
    assert result["accepted_decision_count"] == 210
    assert result["pairwise_observation_count"] == 3780
    for metric in result["metrics"].values():
        assert metric["same_model_pair_count"] == 630
        assert metric["cross_model_pair_count"] == 3150
        assert metric["delta_shared"] > 0
        assert metric["cluster_bootstrap_95pct"] is not None
    assert result["model_main_effects"][MODELS[0]]["seller_rate"] == 1.0
    assert result["model_main_effects"][MODELS[-1]]["seller_rate"] == 0.0
    assert result["permutation_nulls"][
        "pairwise_action_category_agreement"
    ]["permutations"] == 25

    metric_store = ExperimentStore(tmp_path / "metrics.sqlite3")
    for profile in V5_INSTITUTION_PROFILES.values():
        metric_store.register_profile(profile)
    for model_id in MODELS:
        provider, exact_model = model_id.split(":", 1)
        metric_store.register_model(ModelRegistration(
            model_id=model_id,
            provider=provider,
            exact_model=exact_model,
        ))
    metric_store.create_preregistration(plan)
    artifact = metric_store.register_metric_result(
        result,
        actor="Metrics reviewer",
        permutations=25,
        bootstrap_samples=100,
    )
    assert artifact["sha256"] == metric_store.register_metric_result(
        result,
        actor="Metrics reviewer",
        permutations=25,
        bootstrap_samples=100,
    )["sha256"]
    assert artifact["content"]["accepted_decision_set_sha256"] == result[
        "accepted_decision_set_sha256"
    ]
    assert metric_store.verify_audit_chain() is True

    with pytest.raises(ValueError, match="balanced initial grid"):
        compute_initial_estimates(
            records[:-1], plan, permutations=0, bootstrap_samples=0
        )


def test_feedback_world_plan_resolves_exact_same_replicate_decisions():
    plan = build_confirmatory_preregistration(MODELS, "PREREG-WORLDS-V1")
    records = []
    for institution_index, profile_id in enumerate(
        plan.institution_profile_version_ids, start=1
    ):
        for model_index, model_id in enumerate(MODELS, start=1):
            for replicate in range(1, 6):
                identity = f"{profile_id}|{model_id}|{replicate}"
                records.append({
                    "run_cell_id": (
                        f"CELL-I-{hashlib.sha256(identity.encode()).hexdigest()[:20].upper()}"
                    ),
                    "evidence_snapshot_id": "EVIDENCE-WORLDS-V1",
                    "institution_profile_version_id": profile_id,
                    "institution_id": f"INST-{institution_index:02d}",
                    "model_spec_id": model_id,
                    "replicate_index": replicate,
                    "content_sha256": hashlib.sha256(
                        f"decision|{identity}".encode()
                    ).hexdigest(),
                })

    worlds = build_feedback_worlds(plan, records)
    assert worlds == build_feedback_worlds(plan, list(reversed(records)))
    assert len(worlds) == 60
    assert sum(world["assignment_kind"] == "shared" for world in worlds) == 30
    assert sum(world["assignment_kind"] == "heterogeneous" for world in worlds) == 30
    assert sum(len(world["members"]) for world in worlds) == 420
    for world in worlds:
        assert len(world["members"]) == 7
        assert all(
            f"|{world['replicate_index']}" in next(
                identity
                for identity in (
                    f"{record['institution_profile_version_id']}|"
                    f"{record['model_spec_id']}|{record['replicate_index']}"
                    for record in records
                )
                if record_id(identity) == member["initial_run_cell_id"]
            )
            for member in world["members"]
        )
        if world["assignment_kind"] == "shared":
            assert len({member["model_spec_id"] for member in world["members"]}) == 1
    assert len({world["stage1_action_set_sha256"] for world in worlds}) == 60
    with pytest.raises(ValueError, match="complete accepted"):
        build_feedback_worlds(plan, records[:-1])


def record_id(identity: str) -> str:
    return f"CELL-I-{hashlib.sha256(identity.encode()).hexdigest()[:20].upper()}"


def _one_world(plan, decisions_by_institution):
    members = []
    for ordinal, profile_id in enumerate(plan.institution_profile_version_ids):
        institution_id = f"INST-{ordinal + 1:02d}"
        decision = decisions_by_institution[institution_id]
        members.append({
            "ordinal": ordinal,
            "institution_profile_version_id": profile_id,
            "institution_id": institution_id,
            "model_spec_id": MODELS[0],
            "initial_run_cell_id": f"CELL-TRANSITION-{ordinal + 1}",
            "initial_decision_sha256": content_sha256(decision),
            "decision": decision.model_dump(mode="json"),
        })
    action_set_sha = content_sha256([{
        "institution_profile_version_id": member["institution_profile_version_id"],
        "initial_run_cell_id": member["initial_run_cell_id"],
        "initial_decision_sha256": member["initial_decision_sha256"],
    } for member in members])
    return {
        "id": "WORLD-TRANSITION-TEST",
        "preregistration_id": plan.preregistration_id,
        "stage1_action_set_sha256": action_set_sha,
    }, members


def _hold_decisions():
    return {
        f"INST-{index:02d}": InstitutionDecisionV5(
            decision_id=f"DEC-HOLD-{index:02d}",
            institution_id=f"INST-{index:02d}",
            stage="initial",
            stance="hold",
            executive_decision="Maintain the declared position.",
            actions=[],
            constraints_considered=["Declared mandate"],
            evidence_ids=["EVD-SP500-OPEN"],
        )
        for index in range(1, 8)
    }


def test_transition_keeps_zero_flow_flat_and_withdrawal_out_of_sale_volume():
    plan = build_confirmatory_preregistration(MODELS, "PREREG-TRANSITION-V1")
    assert all(
        item.assumption_class == "exercise_assumption"
        for item in plan.transition_policy.instrument_assumptions
    )
    assert all(
        "not observed market data" in item.source_note
        for item in plan.transition_policy.instrument_assumptions
    )
    decisions = _hold_decisions()
    world, members = _one_world(plan, decisions)
    flat = transition_initial_world(
        plan, world, members, list(V5_INSTITUTION_PROFILES.values())
    )
    assert all(
        market["net_sale_system_notional_pct"] == 0
        and market["price_impact_pct"] == 0
        for market in flat["common_snapshot"]["content"]["markets"]
    )

    decisions["INST-04"] = InstitutionDecisionV5(
        decision_id="DEC-WITHDRAW-04",
        institution_id="INST-04",
        stage="initial",
        stance="risk_reduce",
        executive_decision="Reduce displayed depth under the inventory limit.",
        actions=[{
            "action_id": "ACT-WITHDRAW-04",
            "action_type": "withdraw_liquidity",
            "market_instrument_id": "eurostoxx",
            "size_value": 20.0,
            "size_basis": "provided_depth_pct",
            "urgency": 4,
            "confidence": 0.8,
            "rationale": "Inventory utilization is approaching its limit.",
            "evidence_ids": ["EVD-SP500-OPEN"],
        }],
        constraints_considered=["Liquidity provision obligation"],
        evidence_ids=["EVD-SP500-OPEN"],
    )
    world, members = _one_world(plan, decisions)
    withdrawn = transition_initial_world(
        plan, world, members, list(V5_INSTITUTION_PROFILES.values())
    )
    euro = next(
        market for market in withdrawn["common_snapshot"]["content"]["markets"]
        if market["instrument_id"] == "eurostoxx"
    )
    assert euro["withdrawn_depth_system_notional_pct"] == 2.0
    assert euro["net_sale_system_notional_pct"] == 0.0
    assert euro["price_impact_pct"] == 0.0


def test_transition_routes_declared_sale_only_to_its_target_instrument():
    plan = build_confirmatory_preregistration(MODELS, "PREREG-TRANSITION-SELL-V1")
    decisions = _hold_decisions()
    decisions["INST-01"] = InstitutionDecisionV5(
        decision_id="DEC-SELL-01",
        institution_id="INST-01",
        stage="initial",
        stance="risk_reduce",
        executive_decision="Reduce the S&P position.",
        actions=[{
            "action_id": "ACT-SELL-01",
            "action_type": "sell",
            "instrument_id": "sp500",
            "size_value": 10.0,
            "size_basis": "footprint_notional_pct",
            "urgency": 4,
            "confidence": 0.8,
            "rationale": "The frozen observation supports a smaller position.",
            "evidence_ids": ["EVD-SP500-OPEN"],
        }],
        constraints_considered=["VaR limit"],
        evidence_ids=["EVD-SP500-OPEN"],
    )
    world, members = _one_world(plan, decisions)
    result = transition_initial_world(
        plan, world, members, list(V5_INSTITUTION_PROFILES.values())
    )
    markets = {
        item["instrument_id"]: item
        for item in result["common_snapshot"]["content"]["markets"]
    }
    assert markets["sp500"]["net_sale_system_notional_pct"] == 1.5
    assert markets["sp500"]["price_impact_pct"] < 0
    assert all(
        market["price_impact_pct"] == 0
        for instrument, market in markets.items() if instrument != "sp500"
    )


def test_worlds_and_first_transition_are_persisted_as_exact_immutable_links(tmp_path):
    store = ExperimentStore(tmp_path / "world-transition.sqlite3")
    for profile in V5_INSTITUTION_PROFILES.values():
        store.register_profile(profile)
    for model_id in MODELS:
        provider, exact_model = model_id.split(":", 1)
        store.register_model(ModelRegistration(
            model_id=model_id,
            provider=provider,
            exact_model=exact_model,
        ))
    plan = build_confirmatory_preregistration(MODELS, "PREREG-WORLD-STORE-V1")
    evidence = _evidence_snapshot("EVIDENCE-WORLD-STORE-V1")
    store.create_preregistration(plan)
    store.register_evidence(evidence)
    store.approve_preregistration(
        plan.preregistration_id,
        "APR-WORLD-STORE-PLAN",
        "Plan reviewer",
        "Approved.",
        plan.sha256,
    )
    store.approve_evidence(
        evidence.snapshot_id,
        "APR-WORLD-STORE-EVIDENCE",
        "Evidence reviewer",
        "Approved.",
        evidence.sha256,
    )
    cells = build_initial_run_cells(
        plan, evidence, list(V5_INSTITUTION_PROFILES.values())
    )
    store.materialize_initial_cells(cells, "Experiment operator")
    request_json = "[]"
    request_sha = hashlib.sha256(request_json.encode()).hexdigest()
    for cell in cells:
        institution_id = next(
            profile.institution_id
            for profile in V5_INSTITUTION_PROFILES.values()
            if profile.profile_version_id == cell["institution_profile_version_id"]
        )
        decision = InstitutionDecisionV5(
            decision_id=f"DEC-DB-{cell['id'][-20:]}",
            institution_id=institution_id,
            stage="initial",
            stance="hold",
            executive_decision="Maintain the declared position.",
            actions=[],
            constraints_considered=["Declared mandate"],
            evidence_ids=["EVD-SP500-OPEN"],
        )
        store.record_initial_attempt(
            run_cell_id=cell["id"],
            actor="Test provider",
            started_at="2026-09-04T00:00:00Z",
            completed_at="2026-09-04T00:00:01Z",
            outcome="accepted",
            provider_request_sha256=request_sha,
            provider_request_json=request_json,
            raw_response_text="{}",
            latency_ms=1.0,
            finish_reason="stop",
            usage={"output_tokens": 1},
            error=None,
            decision=decision,
        )

    worlds = build_feedback_worlds(
        plan, store.accepted_initial_decisions(plan.preregistration_id)
    )
    world_summary = store.materialize_feedback_worlds(
        worlds, actor="World operator"
    )
    assert world_summary["world_count"] == 60
    assert world_summary["member_count"] == 420
    assert store.materialize_feedback_worlds(
        worlds, actor="World operator"
    )["world_plan_sha256"] == world_summary["world_plan_sha256"]

    stored = None
    for world in worlds:
        context = store.world_transition_input(world["id"])
        result = transition_initial_world(
            plan,
            context["world"],
            context["members"],
            [
                InstitutionProfileV5.model_validate(member["profile"])
                for member in context["members"]
            ],
        )
        stored = store.persist_world_transition(result, actor="Transition operator")
    world_id = worlds[-1]["id"]
    assert stored["world_id"] == world_id
    assert len(stored["snapshots"]) == 8
    assert stored["order_count"] == 0
    assert store.persist_world_transition(
        result, actor="Transition operator"
    )["sha256"] == stored["sha256"]

    feedback_inputs = store.feedback_world_inputs(plan.preregistration_id)
    feedback_cells = build_feedback_run_cells(plan, feedback_inputs)
    assert len(feedback_inputs) == 60
    assert len(feedback_cells) == 420
    assert len({cell["world_id"] for cell in feedback_cells}) == 60
    for cell in feedback_cells:
        assert "assignment_kind" not in cell["case_input_json"]
        for other_id in V5_INSTITUTION_PROFILES:
            if other_id != cell["institution_id"]:
                assert other_id not in cell["case_input_json"]
    feedback_summary = store.materialize_feedback_cells(
        feedback_cells, actor="Feedback planner"
    )
    assert feedback_summary["cell_count"] == 420
    assert feedback_summary["world_count"] == 60
    assert feedback_summary["feedback_plan_sha256"] == store.get_feedback_plan(
        plan.preregistration_id
    )["feedback_plan_sha256"]

    feedback_cell = store.next_pending_feedback_cell(plan.preregistration_id)

    class FakeFeedbackLLM:
        def __init__(self, mode):
            assert mode == "live"

        def context_stats(self, spec, system, user, max_tokens):
            return {"effective_output_tokens": max_tokens}

        def complete(self, spec, system, user, temperature, max_tokens):
            model = str(ModelSpec.parse(spec))
            request = json.dumps(
                [model, system, user, temperature, max_tokens],
                separators=(",", ":"),
            )
            raw = json.dumps({
                "schema_version": 5,
                "stance": "hold",
                "executive_decision": "Maintain the post-transition position.",
                "actions": [],
                "constraints_considered": ["Declared mandate"],
                "evidence_ids": ["EVD-SP500-OPEN"],
            }, separators=(",", ":"))
            return Completion(
                text=raw,
                spec=ModelSpec.parse(spec),
                prompt_sha256=hashlib.sha256(request.encode()).hexdigest(),
                cached=False,
                latency_ms=2.0,
                usage={"output_tokens": 20},
                finish_reason="stop",
            )

    feedback_decision = execute_feedback_cell(
        store,
        plan,
        feedback_cell,
        actor="Feedback operator",
        llm_factory=FakeFeedbackLLM,
    )
    assert feedback_decision.stage == "feedback"
    assert len(store.accepted_feedback_decisions(plan.preregistration_id)) == 1

    accepted_cell_id = feedback_cell["id"]
    for cell in feedback_cells:
        if cell["id"] == accepted_cell_id:
            continue
        institution_id = next(
            profile.institution_id
            for profile in V5_INSTITUTION_PROFILES.values()
            if profile.profile_version_id == cell["institution_profile_version_id"]
        )
        decision = InstitutionDecisionV5(
            decision_id=f"DEC-FB-{cell['id'][-20:]}",
            institution_id=institution_id,
            stage="feedback",
            stance="hold",
            executive_decision="Maintain the post-transition position.",
            actions=[],
            constraints_considered=["Declared mandate"],
            evidence_ids=["EVD-SP500-OPEN"],
        )
        store.record_feedback_attempt(
            run_cell_id=cell["id"],
            actor="Feedback provider",
            started_at="2026-09-04T00:01:00Z",
            completed_at="2026-09-04T00:01:01Z",
            outcome="accepted",
            provider_request_sha256=request_sha,
            provider_request_json=request_json,
            raw_response_text="{}",
            latency_ms=1.0,
            finish_reason="stop",
            usage={"output_tokens": 1},
            error=None,
            decision=decision,
        )
    assert len(store.accepted_feedback_decisions(plan.preregistration_id)) == 420

    pending_round_two = store.worlds_pending_second_transition(
        plan.preregistration_id
    )
    assert len(pending_round_two) == 60
    for pending_world_id in pending_round_two:
        second = store.second_transition_input(pending_world_id)
        result = transition_feedback_world(
            plan,
            second["world"],
            second["feedback_decisions"],
            second["previous_common_snapshot"],
            second["previous_private_snapshots"],
            [
                InstitutionProfileV5.model_validate(item["profile"])
                for item in second["feedback_decisions"]
            ],
        )
        round_two = store.persist_world_transition(
            result, actor="Second transition operator"
        )
    assert round_two["round_index"] == 2
    assert len(round_two["snapshots"]) == 8
    assert store.worlds_pending_second_transition(plan.preregistration_id) == []
    trajectories = compute_feedback_trajectories(
        store.world_trajectory_inputs(plan.preregistration_id), plan
    )
    assert trajectories["world_count"] == 60
    assert trajectories["assignment_kind_means"]["shared"]["world_count"] == 30
    assert trajectories["assignment_kind_means"]["heterogeneous"][
        "world_count"
    ] == 30
    assert all(
        world["feedback_action_category_agreement"] == 1.0
        and world["round2_net_sale_pressure_system_notional_pct"] == 0.0
        and world["round2_over_round1_net_sale_pressure"] is None
        for world in trajectories["worlds"]
    )
    trajectory_artifact = store.register_metric_result(
        trajectories,
        actor="Trajectory reviewer",
        permutations=0,
        bootstrap_samples=0,
    )
    assert trajectory_artifact["content"]["world_count"] == 60
    assert store.verify_audit_chain() is True
