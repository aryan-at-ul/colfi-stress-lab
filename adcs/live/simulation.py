"""Deterministic safeguard application and system-pressure simulation.

The model is intentionally small and inspectable. Institution agents express
desired exposure changes once, in the stress condition. Code then applies the
approved execution policy to the same decisions and compares round-by-round
outcomes. No language model computes, transforms, or interprets these metrics.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict
from datetime import date

from .catalog import HEDGE_LINKS, INSTITUTIONS
from .sessions import sessions_after


CONTRACT_VERSION = "paired-execution-v1"
EPSILON = 1e-12


def _record_portfolio(record: dict) -> list[dict]:
    """Return the exact portfolio supplied to this run, including UI edits."""
    return record.get("portfolio") or INSTITUTIONS[record["institution_id"]][
        "portfolio"
    ]


def _portfolio_weights(record: dict) -> dict[str, float]:
    return {
        item["asset_id"]: float(item.get("weight", item.get("weight_pct", 0.0)))
        for item in _record_portfolio(record)
    }


def _asset_end_dates(evidence: list[dict]) -> dict[str, date]:
    output: dict[str, date] = {}
    for item in evidence:
        if item.get("kind") != "market" or not item.get("id", "").startswith("MKT-"):
            continue
        asset_id = item["id"][4:].lower()
        raw = item.get("window_end_date") or str(item.get("observed_at", ""))[:10]
        if raw:
            output[asset_id] = date.fromisoformat(str(raw))
    return output


def _split_evenly(total: float, count: int) -> list[float]:
    if count <= 1:
        return [total]
    tranche = round(total / count, 10)
    values = [tranche] * (count - 1)
    values.append(round(total - sum(values), 10))
    return values


def build_execution_schedule(record: dict, evidence: list[dict],
                             safeguard: dict | None,
                             simulation_rounds: int = 5) -> tuple[list[dict], dict]:
    """Turn one intended decision into an explicit, dated execution schedule."""
    if record.get("status") != "complete":
        return [], {"contract_version": CONTRACT_VERSION, "status": "not_applicable"}

    end_dates = _asset_end_dates(evidence)
    portfolio = _portfolio_weights(record)
    schedule: list[dict] = []
    curtailed = 0.0
    for action in record["output"]["actions"]:
        kind = action["action"]
        if kind == "hold":
            continue
        asset_id = action["asset_id"]
        underlying_asset_id = HEDGE_LINKS.get(asset_id, asset_id)
        size = float(action["size_pct"])
        if kind == "hedge" and safeguard and not safeguard.get("allow_hedging", True):
            curtailed += size
            continue
        if kind == "sell" and safeguard:
            per_round_cap = float(safeguard["max_single_asset_sell_pct"])
            spacing = int(safeguard.get("minimum_spacing_sessions", 1))
            available_stages = 1 + max(0, simulation_rounds - 1) // spacing
            carry_intent = safeguard.get("carry_unexecuted_intent", True)
            allowed = min(
                size,
                per_round_cap * available_stages if carry_intent else per_round_cap,
            )
            curtailed += size - allowed
            if allowed > 0 and per_round_cap > 0:
                cap_stages = (
                    math.ceil((allowed - EPSILON) / per_round_cap)
                    if carry_intent else 1
                )
                minimum_stages = (
                    int(safeguard.get("minimum_stages", 2))
                    if safeguard.get("require_staged_execution") else 1
                )
                stage_count = min(
                    available_stages, max(cap_stages, minimum_stages)
                )
            else:
                stage_count = 1
        else:
            allowed = size
            stage_count = 1
            spacing = 1
        if allowed <= 0:
            continue

        last_observed = end_dates.get(underlying_asset_id)
        if last_observed is None:
            raise ValueError(f"no dated market evidence available for {asset_id}")
        maximum_offset = 1 + (stage_count - 1) * spacing
        dated_sessions = sessions_after(
            underlying_asset_id,
            last_observed,
            maximum_offset,
        )
        for index, tranche in enumerate(_split_evenly(allowed, stage_count)):
            round_number = 1 + index * spacing
            schedule.append({
                "asset_id": asset_id,
                "underlying_asset_id": underlying_asset_id,
                "action": kind,
                "size_pct": tranche,
                "portfolio_weight": portfolio.get(underlying_asset_id, 0.0),
                "round": round_number,
                "scheduled_for": dated_sessions[round_number - 1].isoformat(),
                "source_action_size_pct": size,
            })

    schedule.sort(key=lambda item: (item["round"], item["asset_id"], item["action"]))
    desired_portfolio_sell = sum(
        portfolio.get(action["asset_id"], 0.0) * float(action["size_pct"])
        for action in record["output"]["actions"]
        if action["action"] == "sell"
    )
    scheduled_portfolio_sell = sum(
        float(item.get("portfolio_weight", 0.0)) * float(item["size_pct"])
        for item in schedule
        if item["action"] == "sell"
    )
    return schedule, {
        "contract_version": CONTRACT_VERSION,
        "status": "applied" if safeguard else "unmitigated",
        "unexecuted_within_horizon_pct": round(curtailed, 10),
        "unexecuted_portfolio_within_horizon_pct": round(
            max(0.0, desired_portfolio_sell - scheduled_portfolio_sell), 10
        ),
        "cap_basis": (
            "pct_of_starting_portfolio_value_per_session"
            if safeguard and safeguard.get("max_daily_portfolio_sell_pct") is not None
            else "pct_of_each_asset_per_execution_round"
            if safeguard else "unmitigated"
        ),
        "policy": copy.deepcopy(safeguard),
    }


def attach_unmitigated_schedule(record: dict, evidence: list[dict]) -> dict:
    output = copy.deepcopy(record)
    schedule, application = build_execution_schedule(output, evidence, None)
    output["execution_schedule"] = schedule
    output["policy_application"] = application
    output["decision_contract_version"] = CONTRACT_VERSION
    output["decision_origin"] = output.get(
        "decision_origin", "institution_agent"
    )
    return output


def derive_safeguarded_record(stress_record: dict, run_id: str, case_id: str,
                              evidence: list[dict], safeguard: dict,
                              simulation_rounds: int = 5) -> dict:
    """Apply a safeguard to the exact paired stress intent without another LLM call."""
    output = copy.deepcopy(stress_record)
    output.update({
        "run_id": run_id,
        "case_id": case_id,
        "condition": "safeguarded",
        "status": stress_record.get("status"),
        "origin_run_id": stress_record["run_id"],
        "decision_origin": "deterministic_policy_application",
        "decision_contract_version": CONTRACT_VERSION,
        "cached": False,
    })
    if output["status"] != "complete":
        output["error"] = f"paired stress decision unavailable: {stress_record.get('error', 'failed')}"
        return output
    schedule, application = build_execution_schedule(
        output, evidence, safeguard, simulation_rounds
    )
    output["execution_schedule"] = schedule
    output["policy_application"] = application
    output["trace"] = [{
        "node": "apply_safeguard",
        "status": "complete",
        "detail": (
            "Applied the approved execution intervention deterministically to "
            f"paired stress run {stress_record['run_id']}."
        ),
    }]
    return output


def _actions_hash(record: dict) -> str:
    value = {
        "stance": record["output"]["stance"],
        "actions": record["output"]["actions"],
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _desired_sell_pct(records: list[dict]) -> float | None:
    valid = [item for item in records if item.get("status") == "complete"]
    if not valid:
        return None
    values = []
    for record in valid:
        weights = _portfolio_weights(record)
        values.append(sum(
            weights.get(action["asset_id"], 0.0) * float(action["size_pct"])
            for action in record["output"]["actions"]
            if action["action"] == "sell"
        ))
    return sum(values) / len(values)


def _simulate_condition(records: list[dict], settings: dict,
                        evidence: list[dict]) -> dict:
    valid = [item for item in records if item.get("status") == "complete"]
    if not valid:
        return {
            "valid_records": 0,
            "desired_sell_pct": None,
            "scheduled_sell_pct": None,
            "rounds": [],
        }
    count = len(valid)
    system_weights: dict[str, float] = defaultdict(float)
    scheduled_sell: dict[tuple[int, str], float] = defaultdict(float)
    scheduled_buy: dict[tuple[int, str], float] = defaultdict(float)
    hedge_demand: dict[tuple[int, str], float] = defaultdict(float)
    round_dates: dict[int, set[str]] = defaultdict(set)
    for record in valid:
        weights = _portfolio_weights(record)
        for asset_id, weight in weights.items():
            system_weights[asset_id] += weight / count
        for item in record.get("execution_schedule", []):
            weight = float(item.get("portfolio_weight", 0.0))
            fraction = weight * float(item["size_pct"]) / 100.0 / count
            market_asset = item.get("underlying_asset_id", item["asset_id"])
            key = (int(item["round"]), market_asset)
            round_dates[int(item["round"])].add(item["scheduled_for"])
            if item["action"] == "sell":
                scheduled_sell[key] += fraction
            elif item["action"] == "buy":
                scheduled_buy[key] += fraction
            elif item["action"] == "hedge":
                hedge_demand[key] += fraction

    round_count = int(settings["rounds"])
    depth = float(settings["market_depth_multiple"])
    impact_cap = float(settings["maximum_asset_impact_pct"]) / 100.0
    persistence = float(settings["impact_persistence"])
    feedback_sensitivity = float(settings["feedback_sale_sensitivity"])
    feedback_cap = float(settings["max_feedback_sale_pct_per_round"]) / 100.0
    breach_threshold = float(
        settings.get("limit_breach_impact_threshold_pct", 0.25)
    ) / 100.0
    impact_state: dict[str, float] = defaultdict(float)
    round_rows = []
    total_scheduled = total_feedback = total_executed = 0.0
    peak_pressure = peak_system_impact = 0.0
    assets = sorted(system_weights)
    market_returns = {
        item["id"][4:].lower(): float(item.get("window_change_pct", item.get("change_pct")) or 0.0)
        for item in evidence
        if item.get("kind") == "market" and item.get("id", "").startswith("MKT-")
    }
    exogenous_system_return = sum(
        system_weights[asset_id] * market_returns.get(asset_id, 0.0)
        for asset_id in assets
    )
    for round_number in range(1, round_count + 1):
        asset_rows = []
        round_pressure = 0.0
        round_scheduled = 0.0
        round_feedback = 0.0
        for asset_id in assets:
            scheduled = scheduled_sell[(round_number, asset_id)]
            bought = scheduled_buy[(round_number, asset_id)]
            breach_excess = max(
                0.0, impact_state[asset_id] - breach_threshold
            )
            feedback_rate = min(
                feedback_cap, breach_excess * feedback_sensitivity
            )
            feedback = system_weights[asset_id] * feedback_rate
            net_sale = max(0.0, scheduled + feedback - bought)
            new_impact = impact_cap * (
                1.0 - math.exp(-net_sale / (impact_cap * depth))
            ) if net_sale > 0 else 0.0
            impact_state[asset_id] = min(
                impact_cap,
                persistence * impact_state[asset_id] + new_impact,
            )
            total_scheduled += scheduled
            total_feedback += feedback
            total_executed += net_sale
            round_pressure += net_sale
            round_scheduled += scheduled
            round_feedback += feedback
            asset_rows.append({
                "asset_id": asset_id,
                "scheduled_sell_pct": scheduled * 100.0,
                "offsetting_buy_pct": bought * 100.0,
                "feedback_sell_pct": feedback * 100.0,
                "limit_breached": breach_excess > 0.0,
                "limit_breach_excess_pct": breach_excess * 100.0,
                "net_executed_sell_pct": net_sale * 100.0,
                "new_price_impact_pct": new_impact * 100.0,
                "outstanding_price_impact_pct": impact_state[asset_id] * 100.0,
                "hedge_demand_pct": hedge_demand[(round_number, asset_id)] * 100.0,
            })
        system_impact = sum(
            system_weights[asset_id] * impact_state[asset_id]
            for asset_id in assets
        ) * 100.0
        peak_pressure = max(peak_pressure, round_pressure * 100.0)
        peak_system_impact = max(peak_system_impact, system_impact)
        if not round_dates[round_number]:
            end_dates = _asset_end_dates(evidence)
            for asset_id in assets:
                last_observed = end_dates.get(asset_id)
                if last_observed is not None:
                    round_dates[round_number].add(
                        sessions_after(
                            asset_id, last_observed, round_number
                        )[-1].isoformat()
                    )
        round_rows.append({
            "round": round_number,
            "session_dates": sorted(round_dates[round_number]),
            "scheduled_sell_pct": round_scheduled * 100.0,
            "feedback_sell_pct": round_feedback * 100.0,
            "net_executed_sell_pct": round_pressure * 100.0,
            "system_price_impact_pct": system_impact,
            "assets": asset_rows,
        })
    terminal_impact = round_rows[-1]["system_price_impact_pct"] if round_rows else 0.0
    portfolio_loss = max(0.0, -exogenous_system_return) + terminal_impact
    return {
        "valid_records": count,
        "desired_sell_pct": _desired_sell_pct(valid),
        "scheduled_sell_pct": total_scheduled * 100.0,
        "executed_sell_including_feedback_pct": total_executed * 100.0,
        "feedback_sell_pct": total_feedback * 100.0,
        "historical_exogenous_system_return_pct": exogenous_system_return,
        "peak_executed_sell_pct": peak_pressure,
        "peak_system_price_impact_pct": peak_system_impact,
        "terminal_system_price_impact_pct": terminal_impact,
        "model_added_loss_at_horizon_pct": terminal_impact,
        "combined_exogenous_and_model_loss_at_horizon_pct": portfolio_loss,
        "rounds": round_rows,
    }


def effect(baseline: float | None, safeguarded: float | None,
           metric: str, unit: str) -> dict:
    result = {
        "metric": metric,
        "unit": unit,
        "unmitigated": baseline,
        "safeguarded": safeguarded,
        "absolute_reduction": None,
        "relative_reduction": None,
        "status": "undefined",
        "interpretation": "Effect is undefined because a comparable baseline is unavailable.",
        "formula": "100 * (unmitigated - safeguarded) / abs(unmitigated)",
    }
    if baseline is None or safeguarded is None:
        return result
    absolute = baseline - safeguarded
    result["absolute_reduction"] = absolute
    if abs(baseline) <= EPSILON:
        status = (
            "unchanged" if abs(safeguarded - baseline) <= EPSILON
            else "improved" if safeguarded < baseline else "worsened"
        )
        result["status"] = status
        result["interpretation"] = (
            f"Relative effect is undefined because the unmitigated baseline is zero; "
            f"the absolute change is {absolute:.6g} {unit} ({status})."
        )
        return result
    relative = absolute / abs(baseline)
    status = "improved" if relative > EPSILON else "worsened" if relative < -EPSILON else "unchanged"
    if status == "unchanged":
        interpretation = f"Safeguarded {metric} was unchanged from the unmitigated value."
    else:
        interpretation = (
            f"Safeguarded {metric} was {abs(relative) * 100:.1f}% "
            f"{'lower' if relative > 0 else 'higher'} than the unmitigated value."
        )
    result.update({
        "relative_reduction": relative,
        "status": status,
        "interpretation": interpretation,
    })
    return result


def verify_simulation_arithmetic(
    conditions: dict,
    effects: dict,
    *,
    records: list[dict] | None = None,
    settings: dict | None = None,
    evidence: list[dict] | None = None,
) -> dict:
    """Recompute every numeric result exposed by the results UI.

    The aggregate and effect checks use independent sums and formulas. When the
    original records, settings and evidence are available, the full condition
    output is also replayed and compared field by field. This function is used
    both at release gating and when projecting older persisted assessments.
    """
    checks: list[dict] = []

    def close(left, right) -> bool:
        if left is None or right is None:
            return left is None and right is None
        try:
            return math.isclose(
                float(left), float(right), rel_tol=1e-10, abs_tol=1e-10
            )
        except (TypeError, ValueError):
            return left == right

    aggregate_errors: list[str] = []
    aggregate_fields = 0

    def aggregate_check(label: str, actual, expected):
        nonlocal aggregate_fields
        aggregate_fields += 1
        if not close(actual, expected):
            aggregate_errors.append(
                f"{label}: stored={actual!r}, recomputed={expected!r}"
            )

    for condition_name in ("stress", "safeguarded"):
        condition = conditions.get(condition_name) or {}
        rounds = condition.get("rounds") or []
        if not rounds and not condition.get("valid_records"):
            continue
        for row in rounds:
            assets = row.get("assets") or []
            prefix = f"{condition_name}.rounds[{row.get('round')}]"
            for key in (
                "scheduled_sell_pct", "feedback_sell_pct",
                "net_executed_sell_pct",
            ):
                aggregate_check(
                    f"{prefix}.{key}",
                    row.get(key),
                    sum(float(item.get(key) or 0.0) for item in assets),
                )
        aggregate_expectations = {
            "scheduled_sell_pct": sum(
                float(row.get("scheduled_sell_pct") or 0.0) for row in rounds
            ),
            "feedback_sell_pct": sum(
                float(row.get("feedback_sell_pct") or 0.0) for row in rounds
            ),
            "executed_sell_including_feedback_pct": sum(
                float(row.get("net_executed_sell_pct") or 0.0) for row in rounds
            ),
            "peak_executed_sell_pct": max(
                (float(row.get("net_executed_sell_pct") or 0.0) for row in rounds),
                default=0.0,
            ),
            "peak_system_price_impact_pct": max(
                (float(row.get("system_price_impact_pct") or 0.0) for row in rounds),
                default=0.0,
            ),
            "terminal_system_price_impact_pct": (
                float(rounds[-1].get("system_price_impact_pct") or 0.0)
                if rounds else 0.0
            ),
            "model_added_loss_at_horizon_pct": (
                float(rounds[-1].get("system_price_impact_pct") or 0.0)
                if rounds else 0.0
            ),
        }
        historical = float(
            condition.get("historical_exogenous_system_return_pct") or 0.0
        )
        terminal = aggregate_expectations[
            "terminal_system_price_impact_pct"
        ]
        aggregate_expectations[
            "combined_exogenous_and_model_loss_at_horizon_pct"
        ] = max(0.0, -historical) + terminal
        for key, expected in aggregate_expectations.items():
            aggregate_check(
                f"{condition_name}.{key}", condition.get(key), expected
            )

    stress = conditions.get("stress") or {}
    safeguarded = conditions.get("safeguarded") or {}
    if (
        stress.get("historical_exogenous_system_return_pct") is not None
        and safeguarded.get("historical_exogenous_system_return_pct") is not None
    ):
        aggregate_check(
            "paired historical return",
            stress.get("historical_exogenous_system_return_pct"),
            safeguarded.get("historical_exogenous_system_return_pct"),
        )
    checks.append({
        "name": "displayed_aggregates",
        "passed": not aggregate_errors,
        "checked_fields": aggregate_fields,
        "detail": (
            f"Recomputed {aggregate_fields} displayed round and condition aggregates."
            if not aggregate_errors else "; ".join(aggregate_errors[:3])
        ),
    })

    effect_sources = {
        "desired_sell_volume": "desired_sell_pct",
        "scheduled_sell_volume": "scheduled_sell_pct",
        "peak_executed_pressure": "peak_executed_sell_pct",
        "feedback_selling": "feedback_sell_pct",
        "peak_modelled_impact": "peak_system_price_impact_pct",
        "model_added_loss_at_horizon": "model_added_loss_at_horizon_pct",
    }
    effect_errors: list[str] = []
    effect_fields = 0

    def effect_check(label: str, actual, expected):
        nonlocal effect_fields
        effect_fields += 1
        if not close(actual, expected):
            effect_errors.append(
                f"{label}: stored={actual!r}, recomputed={expected!r}"
            )

    for effect_name, source_key in effect_sources.items():
        item = effects.get(effect_name) or {}
        baseline = stress.get(source_key)
        guarded = safeguarded.get(source_key)
        effect_check(f"{effect_name}.unmitigated", item.get("unmitigated"), baseline)
        effect_check(f"{effect_name}.safeguarded", item.get("safeguarded"), guarded)
        if baseline is None or guarded is None:
            expected_absolute = expected_relative = None
            expected_status = "undefined"
        else:
            baseline_value = float(baseline)
            guarded_value = float(guarded)
            expected_absolute = baseline_value - guarded_value
            if abs(baseline_value) <= EPSILON:
                expected_relative = None
                expected_status = (
                    "unchanged"
                    if abs(guarded_value - baseline_value) <= EPSILON
                    else "improved" if guarded_value < baseline_value
                    else "worsened"
                )
            else:
                expected_relative = expected_absolute / abs(baseline_value)
                expected_status = (
                    "improved" if expected_relative > EPSILON
                    else "worsened" if expected_relative < -EPSILON
                    else "unchanged"
                )
        effect_check(
            f"{effect_name}.absolute_reduction",
            item.get("absolute_reduction"), expected_absolute,
        )
        effect_check(
            f"{effect_name}.relative_reduction",
            item.get("relative_reduction"), expected_relative,
        )
        effect_fields += 1
        if item.get("status") != expected_status:
            effect_errors.append(
                f"{effect_name}.status: stored={item.get('status')!r}, "
                f"recomputed={expected_status!r}"
            )
    checks.append({
        "name": "displayed_effect_formulas",
        "passed": not effect_errors,
        "checked_fields": effect_fields,
        "detail": (
            f"Recomputed all {len(effect_sources)} displayed effect comparisons "
            f"across {effect_fields} fields."
            if not effect_errors else "; ".join(effect_errors[:3])
        ),
    })

    if records is not None and settings is not None and evidence is not None:
        replay_errors: list[str] = []
        replay_fields = 0

        def compare(actual, expected, path: str):
            nonlocal replay_fields
            if isinstance(expected, dict):
                if not isinstance(actual, dict):
                    replay_errors.append(f"{path}: expected an object")
                    return
                for key, expected_value in expected.items():
                    compare(actual.get(key), expected_value, f"{path}.{key}")
                return
            if isinstance(expected, list):
                if not isinstance(actual, list) or len(actual) != len(expected):
                    replay_errors.append(
                        f"{path}: stored length={len(actual) if isinstance(actual, list) else 'invalid'}, "
                        f"replayed length={len(expected)}"
                    )
                    return
                for index, expected_value in enumerate(expected):
                    compare(actual[index], expected_value, f"{path}[{index}]")
                return
            replay_fields += 1
            if not close(actual, expected):
                replay_errors.append(
                    f"{path}: stored={actual!r}, replayed={expected!r}"
                )

        for condition_name in ("stress", "safeguarded"):
            replayed = _simulate_condition(
                [
                    item for item in records
                    if item.get("condition") == condition_name
                ],
                settings,
                evidence,
            )
            compare(
                conditions.get(condition_name) or {}, replayed, condition_name
            )
        checks.append({
            "name": "deterministic_numeric_replay",
            "passed": not replay_errors,
            "checked_fields": replay_fields,
            "detail": (
                f"Replayed {replay_fields} persisted simulation fields from the "
                "approved records, settings and evidence."
                if not replay_errors else "; ".join(replay_errors[:3])
            ),
        })

    return {
        "passed": all(item["passed"] for item in checks),
        "checked_fields": sum(item["checked_fields"] for item in checks),
        "checks": checks,
    }


def _verify(records: list[dict], safeguard: dict,
            conditions: dict, effects: dict, safeguard_requested: bool,
            required_conditions: set[str], settings: dict,
            evidence: list[dict], expected_per_condition: int | None = None) -> dict:
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    attempted = [item for item in records if item.get("status")]
    counts = {
        condition: sum(
            item.get("condition") == condition
            and item.get("status") == "complete"
            for item in records
        )
        for condition in sorted(required_conditions)
    }
    expected_count = (
        expected_per_condition
        if expected_per_condition is not None
        else max(counts.values(), default=0)
    )
    required_total = expected_count * len(required_conditions)
    complete = (
        expected_count > 0
        and len(attempted) == required_total
        and all(item.get("status") == "complete" for item in attempted)
    )
    check(
        "complete_run_set", complete,
        (
            f"All {required_total} required records completed successfully."
            if complete else
            f"Only {sum(counts.values())} of {required_total} required records "
            "completed successfully."
        ),
    )
    covered = expected_count > 0 and all(
        count == expected_count for count in counts.values()
    )
    check(
        "condition_coverage", covered,
        (
            f"Required condition counts are complete and balanced: {counts}."
            if covered else
            f"Required condition counts are incomplete or unbalanced; expected "
            f"{expected_count} per condition and received {counts}."
        ),
    )

    stress = {item["run_id"]: item for item in records
              if item.get("condition") == "stress" and item.get("status") == "complete"}
    safe = [item for item in records
            if item.get("condition") == "safeguarded" and item.get("status") == "complete"]
    origins = [item.get("origin_run_id") for item in safe]
    paired = (not safeguard_requested) or (
        bool(safe) and len(safe) == len(stress)
        and len(origins) == len(set(origins))
        and set(origins) == set(stress)
    )
    check(
        "paired_safeguard", paired,
        (
            "Every safeguarded record derives from exactly one completed stress record."
            if paired and safeguard_requested else
            "One or more safeguarded records lack exactly one completed stress source."
            if safeguard_requested else
            "Safeguarded comparison was not requested."
        ),
    )
    same_intent = paired and (not safeguard_requested or all(
        _actions_hash(item) == _actions_hash(stress[item["origin_run_id"]])
        for item in safe
    ))
    check(
        "identical_intent", same_intent,
        (
            "Both paths use the same stress signal and approved 20% base rule; "
            "the safeguard deterministically replaces execution with 10%."
            if same_intent else
            "At least one safeguarded result does not preserve its paired stress "
            "signal and approved base rule."
        ),
    )

    schedule_valid = True
    detail = (
        "Per-session portfolio cap, per-asset compatibility cap, horizon capacity "
        "and staged session spacing are enforced."
    )
    cap = float(safeguard["max_single_asset_sell_pct"])
    end_dates = _asset_end_dates(evidence)
    for item in stress.values():
        desired: dict[str, float] = defaultdict(float)
        executed: dict[str, list[dict]] = defaultdict(list)
        for action in item["output"]["actions"]:
            if action["action"] == "sell":
                desired[action["asset_id"]] += float(action["size_pct"])
        for tranche in item.get("execution_schedule", []):
            if tranche["action"] == "sell":
                executed[tranche["asset_id"]].append(tranche)
        for asset_id, target in desired.items():
            tranches = executed[asset_id]
            if (
                abs(sum(float(row["size_pct"]) for row in tranches) - target) > 1e-7
                or any(int(row["round"]) != 1 for row in tranches)
            ):
                schedule_valid = False
            for row in tranches:
                underlying = row.get("underlying_asset_id", asset_id)
                last_observed = end_dates.get(underlying)
                if last_observed is None or row.get("scheduled_for") != sessions_after(
                    underlying, last_observed, 1
                )[0].isoformat():
                    schedule_valid = False
    for item in safe:
        desired: dict[str, float] = defaultdict(float)
        executed: dict[str, list[dict]] = defaultdict(list)
        for action in item["output"]["actions"]:
            if action["action"] == "sell":
                desired[action["asset_id"]] += float(action["size_pct"])
        for tranche in item.get("execution_schedule", []):
            if tranche["action"] == "sell":
                executed[tranche["asset_id"]].append(tranche)
        for asset_id, target in desired.items():
            tranches = executed[asset_id]
            spacing = int(safeguard.get("minimum_spacing_sessions", 1))
            available_stages = 1 + max(0, int(settings["rounds"]) - 1) // spacing
            expected = min(
                target,
                cap * available_stages
                if safeguard.get("carry_unexecuted_intent", True) else cap,
            )
            if abs(sum(float(row["size_pct"]) for row in tranches) - expected) > 1e-7:
                schedule_valid = False
            if any(float(row["size_pct"]) > cap + 1e-9 for row in tranches):
                schedule_valid = False
            rounds = sorted(int(row["round"]) for row in tranches)
            if any(round_number > int(settings["rounds"]) for round_number in rounds):
                schedule_valid = False
            for row in tranches:
                underlying = row.get("underlying_asset_id", asset_id)
                last_observed = end_dates.get(underlying)
                if last_observed is None:
                    schedule_valid = False
                    continue
                expected_date = sessions_after(
                    underlying, last_observed, int(row["round"])
                )[-1].isoformat()
                if row.get("scheduled_for") != expected_date:
                    schedule_valid = False
            if safeguard.get("require_staged_execution") and expected > 0:
                if len(rounds) < int(safeguard.get("minimum_stages", 2)) or any(
                    right - left < spacing for left, right in zip(rounds, rounds[1:])
                ):
                    schedule_valid = False
        portfolio_cap = safeguard.get("max_daily_portfolio_sell_pct")
        if portfolio_cap is not None:
            by_round: dict[int, float] = defaultdict(float)
            for row in item.get("execution_schedule", []):
                if row["action"] == "sell":
                    by_round[int(row["round"])] += (
                        float(row.get("portfolio_weight", 0.0))
                        * float(row["size_pct"])
                    )
            if any(
                amount > float(portfolio_cap) + 1e-9
                for amount in by_round.values()
            ):
                schedule_valid = False
    check(
        "safeguard_schedule", schedule_valid,
        detail if schedule_valid else
        "At least one execution schedule violates an approved cap, horizon or spacing rule.",
    )

    arithmetic = verify_simulation_arithmetic(
        conditions,
        effects,
        records=records,
        settings=settings,
        evidence=evidence,
    )
    for arithmetic_check in arithmetic["checks"]:
        check(
            arithmetic_check["name"],
            arithmetic_check["passed"],
            arithmetic_check["detail"],
        )
    return {
        "passed": all(item["passed"] for item in checks),
        "release_eligible": all(item["passed"] for item in checks),
        "contract_version": CONTRACT_VERSION,
        "checks": checks,
    }


def simulate(records: list[dict], suite: dict, evidence: list[dict]) -> dict:
    settings = suite.get("simulation") or {}
    defaults = {
        "model_version": "colfi-mvp-v1", "rounds": 5,
        "market_depth_multiple": 10.0, "maximum_asset_impact_pct": 50.0,
        "impact_persistence": 0.60, "feedback_sale_sensitivity": 4.0,
        "max_feedback_sale_pct_per_round": 5.0,
        "limit_breach_impact_threshold_pct": 0.25,
    }
    settings = {**defaults, **settings}
    condition_results = {
        condition: _simulate_condition(
            [item for item in records if item.get("condition") == condition],
            settings,
            evidence,
        )
        for condition in ("stress", "safeguarded")
    }
    stress = condition_results["stress"]
    safe = condition_results["safeguarded"]
    effects = {
        "desired_sell_volume": effect(
            stress.get("desired_sell_pct"), safe.get("desired_sell_pct"),
            "desired sell volume", "% of equal-notional system",
        ),
        "scheduled_sell_volume": effect(
            stress.get("scheduled_sell_pct"), safe.get("scheduled_sell_pct"),
            "scheduled sell volume", "% of equal-notional system",
        ),
        "peak_executed_pressure": effect(
            stress.get("peak_executed_sell_pct"), safe.get("peak_executed_sell_pct"),
            "peak executed sell pressure", "% of equal-notional system per round",
        ),
        "feedback_selling": effect(
            stress.get("feedback_sell_pct"), safe.get("feedback_sell_pct"),
            "forced selling after modelled limit breaches",
            "% of equal-notional system",
        ),
        "peak_modelled_impact": effect(
            stress.get("peak_system_price_impact_pct"),
            safe.get("peak_system_price_impact_pct"),
            "peak modelled system price impact", "% of system portfolio value",
        ),
        "model_added_loss_at_horizon": effect(
            stress.get("model_added_loss_at_horizon_pct"),
            safe.get("model_added_loss_at_horizon_pct"),
            "model-added loss at horizon",
            "% of equal-notional system portfolio value",
        ),
    }
    safeguard_requested = any(
        case.get("condition") == "safeguarded" for case in suite.get("cases", [])
    )
    verification = _verify(
        records, suite["safeguard"], condition_results, effects,
        safeguard_requested,
        {case["condition"] for case in suite.get("cases", [])},
        settings,
        evidence,
        (suite.get("runtime") or {}).get("institution_case_packs"),
    )
    fingerprint_payload = {
        "settings": settings,
        "safeguard": suite["safeguard"],
        "records": [{
            "run_id": item.get("run_id"),
            "origin_run_id": item.get("origin_run_id"),
            "output": item.get("output"),
            "execution_schedule": item.get("execution_schedule"),
        } for item in records],
        "evidence": [{
            "id": item.get("id"), "raw_sha256": item.get("raw_sha256"),
            "reference_date": item.get("reference_date"),
            "window_end_date": item.get("window_end_date"),
        } for item in evidence],
    }
    input_sha256 = hashlib.sha256(json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    return {
        "definition": (
            "Paired deterministic execution simulation. Safeguarded records reuse the "
            "same stress signals and base rules; code replaces the 20% initial "
            "sale with 10% and runs the same dated limit-breach engine. "
            "Outstanding impact above the stated scenario threshold activates "
            "next-session forced selling in code. Price impact and limit breaches "
            "are scenario-model outputs, not forecasts."
        ),
        "settings": settings,
        "input_sha256": input_sha256,
        "conditions": condition_results,
        "effects": effects,
        "loss_decomposition": {
            "historical_exogenous_return_pct": stress.get(
                "historical_exogenous_system_return_pct"
            ),
            "unmitigated_model_added_loss_pct": stress.get(
                "model_added_loss_at_horizon_pct"
            ),
            "safeguarded_model_added_loss_pct": safe.get(
                "model_added_loss_at_horizon_pct"
            ),
            "unmitigated_combined_scenario_loss_pct": stress.get(
                "combined_exogenous_and_model_loss_at_horizon_pct"
            ),
            "safeguarded_combined_scenario_loss_pct": safe.get(
                "combined_exogenous_and_model_loss_at_horizon_pct"
            ),
            "safeguard_effect_denominator": "model_added_loss_only",
        },
        "primary_effect": effects["peak_modelled_impact"],
        "verification": verification,
    }
