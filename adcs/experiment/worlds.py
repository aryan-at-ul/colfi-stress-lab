"""Immutable shared and heterogeneous feedback-world construction."""
from __future__ import annotations

import hashlib

from .models import ExperimentPreregistration, content_sha256


WORLD_CONTRACT_VERSION = "colfi-worlds-v5.1"


def build_feedback_worlds(
    plan: ExperimentPreregistration,
    accepted_initial_decisions: list[dict],
) -> list[dict]:
    """Resolve 60 worlds from the exact balanced 210-decision initial grid."""

    by_key = {}
    evidence_ids = set()
    profile_to_institution = {}
    for record in accepted_initial_decisions:
        key = (
            record["institution_profile_version_id"],
            record["model_spec_id"],
            record["replicate_index"],
        )
        if key in by_key:
            raise ValueError(f"duplicate accepted initial decision: {key}")
        by_key[key] = record
        evidence_ids.add(record["evidence_snapshot_id"])
        profile_to_institution[record["institution_profile_version_id"]] = record[
            "institution_id"
        ]
    if len(evidence_ids) != 1:
        raise ValueError("all initial decisions must share one evidence snapshot")
    evidence_snapshot_id = next(iter(evidence_ids))

    profile_ids = plan.institution_profile_version_ids
    model_ids = [model.model_id for model in plan.models]
    expected = {
        (profile_id, model_id, replicate)
        for profile_id in profile_ids
        for model_id in model_ids
        for replicate in range(1, plan.repetitions + 1)
    }
    if set(by_key) != expected:
        raise ValueError(
            "complete accepted 7 × 6 × 5 grid required before building worlds; "
            f"missing={len(expected - set(by_key))}, extra={len(set(by_key) - expected)}"
        )

    assignments = [
        (
            "shared",
            f"SHARED-{index:02d}",
            {profile_id: model_id for profile_id in profile_ids},
        )
        for index, model_id in enumerate(model_ids, start=1)
    ] + [
        ("heterogeneous", cohort.cohort_id, cohort.assignments)
        for cohort in plan.heterogeneous_cohorts
    ]

    worlds = []
    for assignment_kind, assignment_key, assignment in assignments:
        assignment_rows = [
            {
                "institution_profile_version_id": profile_id,
                "model_spec_id": assignment[profile_id],
            }
            for profile_id in profile_ids
        ]
        assignment_sha = content_sha256(assignment_rows)
        for replicate in range(1, plan.repetitions + 1):
            members = []
            for ordinal, profile_id in enumerate(profile_ids):
                model_id = assignment[profile_id]
                record = by_key[(profile_id, model_id, replicate)]
                members.append({
                    "ordinal": ordinal,
                    "institution_profile_version_id": profile_id,
                    "institution_id": profile_to_institution[profile_id],
                    "model_spec_id": model_id,
                    "initial_run_cell_id": record["run_cell_id"],
                    "initial_decision_sha256": record["content_sha256"],
                })
            stage1_sha = content_sha256([
                {
                    "institution_profile_version_id": member[
                        "institution_profile_version_id"
                    ],
                    "initial_run_cell_id": member["initial_run_cell_id"],
                    "initial_decision_sha256": member["initial_decision_sha256"],
                }
                for member in members
            ])
            identity = "|".join((
                plan.preregistration_id,
                evidence_snapshot_id,
                assignment_key,
                str(replicate),
                stage1_sha,
            ))
            world_id = "WORLD-" + hashlib.sha256(
                identity.encode("utf-8")
            ).hexdigest()[:20].upper()
            worlds.append({
                "id": world_id,
                "contract_version": WORLD_CONTRACT_VERSION,
                "preregistration_id": plan.preregistration_id,
                "evidence_snapshot_id": evidence_snapshot_id,
                "assignment_kind": assignment_kind,
                "assignment_key": assignment_key,
                "replicate_index": replicate,
                "assignment_sha256": assignment_sha,
                "stage1_action_set_sha256": stage1_sha,
                "members": members,
            })

    if len(worlds) != 12 * plan.repetitions:
        raise RuntimeError("feedback world plan is incomplete")
    if len({world["id"] for world in worlds}) != len(worlds):
        raise RuntimeError("feedback world identities are not unique")
    worlds.sort(key=lambda world: (
        world["assignment_kind"],
        world["assignment_key"],
        world["replicate_index"],
    ))
    return worlds
