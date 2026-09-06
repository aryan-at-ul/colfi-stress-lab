"""Deterministic UC-01/UC-02 estimators over accepted initial decisions."""
from __future__ import annotations

import hashlib
import math
import random
from itertools import combinations
from statistics import fmean, pstdev

from .models import (
    ExperimentPreregistration,
    InstitutionDecisionV5,
    content_sha256,
)


METRIC_VERSION = "colfi-pairwise-v5.1"
TRAJECTORY_METRIC_VERSION = "colfi-world-trajectories-v5.1"
PAIRWISE_METRICS = (
    "pairwise_action_category_agreement",
    "pairwise_direction_agreement",
    "pairwise_size_similarity",
    "pairwise_urgency_similarity",
)


def _target(action) -> str:
    return (
        getattr(action, "instrument_id", None)
        or getattr(action, "hedge_instrument_id", None)
        or getattr(action, "market_instrument_id", None)
        or "portfolio"
    )


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _size_vector(decision: InstitutionDecisionV5) -> dict[tuple[str, str, str], float]:
    vector: dict[tuple[str, str, str], float] = {}
    for action in decision.actions:
        key = (action.action_type, _target(action), action.size_basis)
        vector[key] = vector.get(key, 0.0) + action.size_value
    return vector


def _pair_metrics(left: InstitutionDecisionV5,
                  right: InstitutionDecisionV5) -> dict[str, float]:
    left_categories = {action.action_type for action in left.actions}
    right_categories = {action.action_type for action in right.actions}
    left_sizes = _size_vector(left)
    right_sizes = _size_vector(right)
    size_keys = left_sizes.keys() | right_sizes.keys()
    size_similarity = 1.0 if not size_keys else fmean(
        1.0 - abs(left_sizes.get(key, 0.0) - right_sizes.get(key, 0.0))
        / max(left_sizes.get(key, 0.0), right_sizes.get(key, 0.0), 1e-12)
        for key in size_keys
    )
    left_urgency = max((action.urgency for action in left.actions), default=0)
    right_urgency = max((action.urgency for action in right.actions), default=0)
    return {
        "pairwise_action_category_agreement": _jaccard(
            left_categories, right_categories
        ),
        "pairwise_direction_agreement": float(left.stance == right.stance),
        "pairwise_size_similarity": size_similarity,
        "pairwise_urgency_similarity": 1.0 - abs(
            left_urgency - right_urgency
        ) / 5.0,
    }


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires observations")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _seed(base: str, label: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"{base}|{label}".encode("utf-8")).digest()[:8],
        "big",
    )


def _grid(records: list[dict], plan: ExperimentPreregistration):
    expected_profiles = plan.institution_profile_version_ids
    profile_to_institution: dict[str, str] = {}
    decisions: dict[tuple[str, str, int], InstitutionDecisionV5] = {}
    for record in records:
        profile_id = record["institution_profile_version_id"]
        institution_id = record["institution_id"]
        known = profile_to_institution.setdefault(profile_id, institution_id)
        if known != institution_id:
            raise ValueError("one profile version maps to multiple institutions")
        key = (institution_id, record["model_spec_id"], record["replicate_index"])
        if key in decisions:
            raise ValueError(f"duplicate accepted initial cell: {key}")
        decision = InstitutionDecisionV5.model_validate(record["decision"])
        if decision.stage != "initial" or decision.institution_id != institution_id:
            raise ValueError("accepted decision metadata does not match its cell")
        decisions[key] = decision

    if set(profile_to_institution) != set(expected_profiles):
        raise ValueError("accepted decisions do not cover every preregistered profile")
    institutions = [profile_to_institution[item] for item in expected_profiles]
    models = [model.model_id for model in plan.models]
    expected = {
        (institution, model, replicate)
        for institution in institutions
        for model in models
        for replicate in range(1, plan.repetitions + 1)
    }
    missing = expected - decisions.keys()
    extra = decisions.keys() - expected
    if missing or extra:
        raise ValueError(
            f"balanced initial grid required; missing={len(missing)}, extra={len(extra)}"
        )
    return institutions, models, decisions


def _observations(institutions: list[str], models: list[str],
                  decisions: dict, repetitions: int) -> list[dict]:
    values = []
    for replicate in range(1, repetitions + 1):
        for left_institution, right_institution in combinations(institutions, 2):
            for left_model in models:
                for right_model in models:
                    values.append({
                        "replicate_index": replicate,
                        "same_model": left_model == right_model,
                        **_pair_metrics(
                            decisions[(left_institution, left_model, replicate)],
                            decisions[(right_institution, right_model, replicate)],
                        ),
                    })
    return values


def _summaries(observations: list[dict], repetitions: int,
               bootstrap_samples: int, seed_base: str) -> dict:
    result = {}
    for metric in PAIRWISE_METRICS:
        same = [item[metric] for item in observations if item["same_model"]]
        cross = [item[metric] for item in observations if not item["same_model"]]
        same_mean = fmean(same)
        cross_mean = fmean(cross)
        replicate_deltas = []
        for replicate in range(1, repetitions + 1):
            replicate_same = [
                item[metric] for item in observations
                if item["replicate_index"] == replicate and item["same_model"]
            ]
            replicate_cross = [
                item[metric] for item in observations
                if item["replicate_index"] == replicate and not item["same_model"]
            ]
            replicate_deltas.append(fmean(replicate_same) - fmean(replicate_cross))
        rng = random.Random(_seed(seed_base, f"bootstrap|{metric}"))
        bootstrapped = [
            fmean(rng.choice(replicate_deltas) for _ in replicate_deltas)
            for _ in range(bootstrap_samples)
        ] if bootstrap_samples else []
        result[metric] = {
            "same_model_mean": same_mean,
            "cross_model_mean": cross_mean,
            "delta_shared": same_mean - cross_mean,
            "same_model_pair_count": len(same),
            "cross_model_pair_count": len(cross),
            "replicate_deltas": replicate_deltas,
            "cluster_bootstrap_95pct": (
                [_quantile(bootstrapped, 0.025), _quantile(bootstrapped, 0.975)]
                if bootstrapped else None
            ),
            "bootstrap_unit": "whole_repetition",
            "bootstrap_samples": bootstrap_samples,
        }
    return result


def _permutation_nulls(institutions: list[str], models: list[str], decisions: dict,
                       plan: ExperimentPreregistration, observed: dict,
                       permutations: int) -> dict:
    nulls = {metric: [] for metric in PAIRWISE_METRICS}
    for permutation_index in range(permutations):
        relabelled = {}
        for replicate in range(1, plan.repetitions + 1):
            for institution in institutions:
                shuffled = list(models)
                rng = random.Random(_seed(
                    plan.randomization_seed,
                    f"permutation|{permutation_index}|{replicate}|{institution}",
                ))
                rng.shuffle(shuffled)
                for new_label, old_label in zip(models, shuffled, strict=True):
                    relabelled[(institution, new_label, replicate)] = decisions[
                        (institution, old_label, replicate)
                    ]
        permuted = _observations(
            institutions, models, relabelled, plan.repetitions
        )
        for metric in PAIRWISE_METRICS:
            same = [item[metric] for item in permuted if item["same_model"]]
            cross = [item[metric] for item in permuted if not item["same_model"]]
            nulls[metric].append(fmean(same) - fmean(cross))

    return {
        metric: {
            "permutations": permutations,
            "null_mean": fmean(values) if values else None,
            "null_95pct": (
                [_quantile(values, 0.025), _quantile(values, 0.975)]
                if values else None
            ),
            "two_sided_p_value": (
                (1 + sum(
                    abs(value) >= abs(observed[metric]["delta_shared"])
                    for value in values
                )) / (permutations + 1)
                if values else None
            ),
            "procedure": "independent within-institution/repetition model-label shuffle",
        }
        for metric, values in nulls.items()
    }


def _model_main_effects(models: list[str], institutions: list[str], decisions: dict,
                        repetitions: int) -> dict:
    action_types = ("sell", "hedge", "deleverage", "withdraw_liquidity", "buy_support")
    size_bases = (
        "footprint_notional_pct",
        "gross_exposure_reduction_pct",
        "provided_depth_pct",
    )
    signed = {"risk_reduce": -1.0, "risk_add": 1.0, "mixed": 0.0, "hold": 0.0}
    effects = {}
    for model in models:
        sample = [
            decisions[(institution, model, replicate)]
            for institution in institutions
            for replicate in range(1, repetitions + 1)
        ]
        directions = [signed[item.stance] for item in sample]
        urgencies = [max((action.urgency for action in item.actions), default=0) for item in sample]
        magnitudes = {
            basis: [
                sum(action.size_value for action in item.actions if action.size_basis == basis)
                for item in sample
            ]
            for basis in size_bases
        }
        effects[model] = {
            "decision_count": len(sample),
            "active_action_rate": fmean(bool(item.actions) for item in sample),
            "seller_rate": fmean(
                any(action.action_type == "sell" for action in item.actions)
                for item in sample
            ),
            "action_category_rates": {
                action_type: fmean(
                    any(action.action_type == action_type for action in item.actions)
                    for item in sample
                )
                for action_type in action_types
            },
            "mean_signed_direction": fmean(directions),
            "signed_direction_dispersion": pstdev(directions),
            "mean_max_urgency": fmean(urgencies),
            "max_urgency_dispersion": pstdev(urgencies),
            "mean_magnitude_by_size_basis": {
                basis: fmean(values) for basis, values in magnitudes.items()
            },
            "magnitude_dispersion_by_size_basis": {
                basis: pstdev(values) for basis, values in magnitudes.items()
            },
        }
    return effects


def compute_initial_estimates(
    records: list[dict],
    plan: ExperimentPreregistration,
    *,
    permutations: int = 1000,
    bootstrap_samples: int = 5000,
) -> dict:
    """Compute the complete-grid shared-model contrast and transparent components."""

    if permutations < 0 or bootstrap_samples < 0:
        raise ValueError("resampling counts cannot be negative")
    institutions, models, decisions = _grid(records, plan)
    observations = _observations(
        institutions, models, decisions, plan.repetitions
    )
    summaries = _summaries(
        observations,
        plan.repetitions,
        bootstrap_samples,
        plan.randomization_seed,
    )
    return {
        "schema_version": 5,
        "metric_version": METRIC_VERSION,
        "preregistration_id": plan.preregistration_id,
        "accepted_decision_count": len(decisions),
        "accepted_decision_set_sha256": content_sha256([
            {
                "institution_id": institution,
                "model_spec_id": model,
                "replicate_index": replicate,
                "decision": decisions[(institution, model, replicate)].model_dump(
                    mode="json"
                ),
            }
            for institution in institutions
            for model in models
            for replicate in range(1, plan.repetitions + 1)
        ]),
        "institution_count": len(institutions),
        "model_count": len(models),
        "repetitions": plan.repetitions,
        "pairwise_observation_count": len(observations),
        "metrics": summaries,
        "permutation_nulls": _permutation_nulls(
            institutions,
            models,
            decisions,
            plan,
            summaries,
            permutations,
        ),
        "model_main_effects": _model_main_effects(
            models, institutions, decisions, plan.repetitions
        ),
    }


def _world_agreement(decisions: list[dict]) -> dict[str, float]:
    typed = [InstitutionDecisionV5.model_validate(item["decision"]) for item in decisions]
    pairs = [
        _pair_metrics(left, right) for left, right in combinations(typed, 2)
    ]
    return {
        metric: fmean(pair[metric] for pair in pairs)
        for metric in PAIRWISE_METRICS
    }


def _round_flow(snapshot: dict) -> dict:
    markets = snapshot["content"]["markets"]
    net_sale_pressure = sum(
        max(float(item["net_sale_system_notional_pct"]), 0.0)
        for item in markets
    )
    net_buy_pressure = sum(
        max(-float(item["net_sale_system_notional_pct"]), 0.0)
        for item in markets
    )
    return {
        "round_index": snapshot["round_index"],
        "net_sale_pressure_system_notional_pct": net_sale_pressure,
        "net_buy_pressure_system_notional_pct": net_buy_pressure,
        "gross_directed_sell_system_notional_pct": sum(
            float(item["directed_sell_system_notional_pct"]) for item in markets
        ),
        "gross_forced_sell_system_notional_pct": sum(
            float(item["forced_sell_system_notional_pct"]) for item in markets
        ),
        "gross_hedge_sell_system_notional_pct": sum(
            float(item["hedge_sell_system_notional_pct"]) for item in markets
        ),
        "gross_support_buy_system_notional_pct": sum(
            float(item["support_buy_system_notional_pct"]) for item in markets
        ),
        "withdrawn_depth_system_notional_pct": sum(
            float(item["withdrawn_depth_system_notional_pct"]) for item in markets
        ),
        "maximum_absolute_instrument_impact_pct": max(
            (abs(float(item["price_impact_pct"])) for item in markets),
            default=0.0,
        ),
        "markets": markets,
        "snapshot_sha256": snapshot["sha256"],
    }


def _mean_world_fields(rows: list[dict]) -> dict:
    scalar_fields = (
        "initial_action_category_agreement",
        "feedback_action_category_agreement",
        "initial_direction_agreement",
        "feedback_direction_agreement",
        "round1_net_sale_pressure_system_notional_pct",
        "round2_net_sale_pressure_system_notional_pct",
        "round1_maximum_absolute_instrument_impact_pct",
        "round2_maximum_absolute_instrument_impact_pct",
    )
    return {
        "world_count": len(rows),
        **{
            field: fmean(float(row[field]) for row in rows)
            for field in scalar_fields
        },
    }


def compute_feedback_trajectories(world_inputs: list[dict],
                                  plan: ExperimentPreregistration) -> dict:
    """Compute transparent round-one/round-two world trajectories."""

    if len(world_inputs) != 60:
        raise ValueError("all 60 complete two-transition worlds are required")
    world_rows = []
    artifact_links = []
    for world in world_inputs:
        if len(world["initial_decisions"]) != 7 or len(world["feedback_decisions"]) != 7:
            raise ValueError("each trajectory requires seven decisions at both stages")
        if [item["round_index"] for item in world["common_snapshots"]] != [1, 2]:
            raise ValueError("each trajectory requires common snapshots for rounds 1 and 2")
        initial_agreement = _world_agreement(world["initial_decisions"])
        feedback_agreement = _world_agreement(world["feedback_decisions"])
        round1, round2 = map(_round_flow, world["common_snapshots"])
        baseline = round1["net_sale_pressure_system_notional_pct"]
        ratio = (
            round2["net_sale_pressure_system_notional_pct"] / baseline
            if abs(baseline) > 1e-12 else None
        )
        row = {
            "world_id": world["world_id"],
            "assignment_kind": world["assignment_kind"],
            "assignment_key": world["assignment_key"],
            "replicate_index": world["replicate_index"],
            "initial_action_category_agreement": initial_agreement[
                "pairwise_action_category_agreement"
            ],
            "feedback_action_category_agreement": feedback_agreement[
                "pairwise_action_category_agreement"
            ],
            "initial_direction_agreement": initial_agreement[
                "pairwise_direction_agreement"
            ],
            "feedback_direction_agreement": feedback_agreement[
                "pairwise_direction_agreement"
            ],
            "round1_net_sale_pressure_system_notional_pct": round1[
                "net_sale_pressure_system_notional_pct"
            ],
            "round2_net_sale_pressure_system_notional_pct": round2[
                "net_sale_pressure_system_notional_pct"
            ],
            "round2_over_round1_net_sale_pressure": ratio,
            "round1_maximum_absolute_instrument_impact_pct": round1[
                "maximum_absolute_instrument_impact_pct"
            ],
            "round2_maximum_absolute_instrument_impact_pct": round2[
                "maximum_absolute_instrument_impact_pct"
            ],
            "rounds": [round1, round2],
        }
        world_rows.append(row)
        artifact_links.append({
            "world_id": world["world_id"],
            "stage1_action_set_sha256": world["stage1_action_set_sha256"],
            "common_snapshot_sha256": [
                item["sha256"] for item in world["common_snapshots"]
            ],
            "initial_decision_sha256": [
                item["sha256"] for item in world["initial_decisions"]
            ],
            "feedback_decision_sha256": [
                item["sha256"] for item in world["feedback_decisions"]
            ],
        })

    by_assignment = {}
    for assignment_key in sorted({row["assignment_key"] for row in world_rows}):
        by_assignment[assignment_key] = _mean_world_fields([
            row for row in world_rows if row["assignment_key"] == assignment_key
        ])
    by_kind = {}
    for kind in ("shared", "heterogeneous"):
        by_kind[kind] = _mean_world_fields([
            row for row in world_rows if row["assignment_kind"] == kind
        ])
    return {
        "schema_version": 5,
        "metric_version": TRAJECTORY_METRIC_VERSION,
        "preregistration_id": plan.preregistration_id,
        "accepted_decision_set_sha256": content_sha256(artifact_links),
        "world_count": len(world_rows),
        "worlds": world_rows,
        "assignment_means": by_assignment,
        "assignment_kind_means": by_kind,
        "interpretation_boundary": (
            "Counterfactual scenario outputs under approved normalized exercise "
            "assumptions; not actual market impact or actual firm behavior."
        ),
    }
