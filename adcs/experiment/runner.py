"""Deterministic planning for the governed v5 initial experiment grid."""
from __future__ import annotations

import hashlib

from .models import (
    EvidenceSnapshotV5,
    ExperimentPreregistration,
    InstitutionProfileV5,
    canonical_json,
    content_sha256,
)


INITIAL_PROMPT_CONTRACT = "colfi-initial-decision-v5.1"


def build_initial_case_input(
    plan: ExperimentPreregistration,
    evidence: EvidenceSnapshotV5,
    profile: InstitutionProfileV5,
) -> dict:
    """Build the exact arm-neutral input shared across models and repetitions."""

    return {
        "schema_version": 5,
        "prompt_contract": INITIAL_PROMPT_CONTRACT,
        "event": {
            "event_id": plan.event_id,
            "decision_cutoff": plan.decision_clock.initial_cutoff.isoformat(),
            "instruction": (
                "Use only the frozen facts below and the institution's private "
                "state. Do not infer information that became available later."
            ),
        },
        "common_evidence": [
            {
                "evidence_id": item.evidence_id,
                "evidence_class": item.evidence_class,
                "title": item.title,
                "instrument_id": item.instrument_id,
                "value": item.value,
                "unit": item.unit,
                "observed_at": item.observed_at.isoformat(),
                "available_at": item.available_at.isoformat(),
            }
            for item in evidence.items
        ],
        "private_institution": profile.private_model_payload(),
        "decision_request": {
            "stage": "initial",
            "task": (
                "Choose this institution's intended response under its declared "
                "objectives, constraints, holdings, risk state and liquidity state."
            ),
            "permitted_action_types": profile.permitted_actions,
            "requirements": [
                "Return only the typed decision schema requested by the caller.",
                "Cite frozen evidence IDs for every active action.",
                "Use the explicit size basis defined for each action type.",
                "Do not assume what any other institution or model will do.",
            ],
        },
    }


def build_initial_run_cells(
    plan: ExperimentPreregistration,
    evidence: EvidenceSnapshotV5,
    profiles: list[InstitutionProfileV5],
) -> list[dict]:
    """Return the preregistered 7×6×5 grid in deterministic shuffled order."""

    if evidence.event_id != plan.event_id:
        raise ValueError("evidence event does not match the preregistration")
    if evidence.decision_cutoff != plan.decision_clock.initial_cutoff:
        raise ValueError("evidence cutoff does not match the preregistration")

    by_version = {profile.profile_version_id: profile for profile in profiles}
    expected_profiles = plan.institution_profile_version_ids
    if len(by_version) != len(profiles) or set(by_version) != set(expected_profiles):
        raise ValueError("profiles do not exactly match the preregistration")
    if any(profile.holdings_status != "approved" for profile in profiles):
        raise ValueError("all holdings must be approved before planning runs")

    ranked: list[tuple[str, dict]] = []
    for profile_id in expected_profiles:
        profile = by_version[profile_id]
        case_input = build_initial_case_input(plan, evidence, profile)
        case_json = canonical_json(case_input)
        case_sha = content_sha256(case_input)
        for model in plan.models:
            for replicate_index in range(1, plan.repetitions + 1):
                identity = "|".join((
                    plan.preregistration_id,
                    evidence.snapshot_id,
                    profile_id,
                    model.model_id,
                    str(replicate_index),
                ))
                identity_sha = hashlib.sha256(identity.encode("utf-8")).hexdigest()
                rank = hashlib.sha256(
                    f"{plan.randomization_seed}|{identity}".encode("utf-8")
                ).hexdigest()
                ranked.append((rank, {
                    "id": f"CELL-I-{identity_sha[:20].upper()}",
                    "preregistration_id": plan.preregistration_id,
                    "evidence_snapshot_id": evidence.snapshot_id,
                    "stage": "initial",
                    "world_id": None,
                    "institution_profile_version_id": profile_id,
                    "institution_id": profile.institution_id,
                    "model_spec_id": model.model_id,
                    "replicate_index": replicate_index,
                    "case_input_sha256": case_sha,
                    "case_input_json": case_json,
                }))

    cells = []
    for execution_order, (_, cell) in enumerate(sorted(ranked), start=1):
        cells.append({**cell, "execution_order": execution_order})

    expected_count = (
        len(plan.institution_profile_version_ids)
        * len(plan.models)
        * plan.repetitions
    )
    if len(cells) != expected_count or len({cell["id"] for cell in cells}) != expected_count:
        raise RuntimeError("initial run grid is incomplete or contains duplicate cells")
    return cells
