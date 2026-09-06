"""Build the isolated 420-cell feedback grid from immutable world snapshots."""
from __future__ import annotations

import hashlib
import json

from .models import ExperimentPreregistration, canonical_json, content_sha256


FEEDBACK_PROMPT_CONTRACT = "colfi-feedback-decision-v5.1"


def build_feedback_case_input(member: dict, common_snapshot: dict,
                              private_snapshot: dict) -> dict:
    initial = json.loads(member["initial_case_input_json"])
    common = {
        key: value for key, value in common_snapshot.items()
        if key not in {"world_id", "input_action_set_sha256"}
    }
    private = {
        key: value for key, value in private_snapshot.items()
        if key not in {"world_id", "institution_profile_version_id"}
    }
    return {
        "schema_version": 5,
        "prompt_contract": FEEDBACK_PROMPT_CONTRACT,
        "event": initial["event"],
        "common_evidence": initial["common_evidence"],
        "private_institution": initial["private_institution"],
        "feedback_context": {
            "instruction": (
                "This is an engine-generated counterfactual state, not an observed "
                "future fact. Reassess once using the common market state and only "
                "your own post-execution state."
            ),
            "common_market_state": common,
            "own_private_state": private,
        },
        "decision_request": {
            "stage": "feedback",
            "task": (
                "Choose this institution's next intended response after the first "
                "world transition, under the unchanged mandate and constraints."
            ),
            "permitted_action_types": initial["decision_request"][
                "permitted_action_types"
            ],
            "requirements": [
                "Return only the typed decision schema requested by the caller.",
                "Cite frozen or SIM feedback evidence IDs for every active action.",
                "Use the explicit size basis defined for each action type.",
                "Do not infer another institution's private state or assignment.",
            ],
        },
    }


def build_feedback_run_cells(
    plan: ExperimentPreregistration,
    world_inputs: list[dict],
) -> list[dict]:
    if len(world_inputs) != 60:
        raise ValueError("all 60 transitioned worlds are required for feedback planning")
    ranked = []
    seen_worlds = set()
    for context in world_inputs:
        world = context["world"]
        if world["id"] in seen_worlds:
            raise ValueError("feedback world inputs must be unique")
        seen_worlds.add(world["id"])
        if world["preregistration_id"] != plan.preregistration_id:
            raise ValueError("feedback world belongs to another preregistration")
        if len(context["members"]) != 7:
            raise ValueError("each feedback world requires seven private states")
        for member in context["members"]:
            case_input = build_feedback_case_input(
                member,
                context["common_snapshot"],
                member["private_snapshot"],
            )
            case_json = canonical_json(case_input)
            case_sha = content_sha256(case_input)
            identity = "|".join((
                plan.preregistration_id,
                world["evidence_snapshot_id"],
                world["id"],
                member["institution_profile_version_id"],
                member["model_spec_id"],
                str(world["replicate_index"]),
            ))
            identity_sha = hashlib.sha256(identity.encode()).hexdigest()
            rank = hashlib.sha256(
                f"{plan.randomization_seed}|feedback|{identity}".encode()
            ).hexdigest()
            ranked.append((rank, {
                "id": f"CELL-F-{identity_sha[:20].upper()}",
                "preregistration_id": plan.preregistration_id,
                "evidence_snapshot_id": world["evidence_snapshot_id"],
                "stage": "feedback",
                "world_id": world["id"],
                "institution_profile_version_id": member[
                    "institution_profile_version_id"
                ],
                "institution_id": member["institution_id"],
                "model_spec_id": member["model_spec_id"],
                "replicate_index": world["replicate_index"],
                "case_input_sha256": case_sha,
                "case_input_json": case_json,
            }))
    cells = [
        {**cell, "execution_order": execution_order}
        for execution_order, (_, cell) in enumerate(sorted(ranked), start=1)
    ]
    if len(cells) != 420 or len({cell["id"] for cell in cells}) != 420:
        raise RuntimeError("feedback grid must contain 420 unique cells")
    return cells
