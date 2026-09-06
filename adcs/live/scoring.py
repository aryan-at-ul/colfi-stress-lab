from __future__ import annotations

import statistics
from collections import Counter
from itertools import combinations

from .catalog import HEDGE_LINKS, INSTITUTIONS
from .simulation import simulate
from .sources import INSTRUMENTS


def _tokens(value: str) -> set[str]:
    return {
        token.strip(".,;:()[]").lower()
        for token in value.split()
        if len(token.strip(".,;:()[]")) >= 5
    }


def _constraint_score(institution: dict, considered: list[str]) -> float:
    supplied = institution["constraints"]
    combined = _tokens(" ".join(considered))
    matches = []
    for constraint in supplied:
        keywords = _tokens(constraint)
        matches.append(bool(keywords & combined))
    return sum(matches) / len(matches) if matches else 1.0


def _portfolio_alignment(institution: dict, actions: list[dict],
                         evidence_assets: set[str]) -> float:
    holdings = {item["asset_id"] for item in institution["portfolio"]}
    active = [item for item in actions if item["action"] != "hold"]
    if not active:
        return 1.0
    scores = []
    for item in active:
        if item["action"] == "hedge":
            linked_to_holding = HEDGE_LINKS.get(item["asset_id"]) in holdings
            scores.append(
                item["asset_id"] in holdings | evidence_assets
                or linked_to_holding
            )
        else:
            scores.append(item["asset_id"] in holdings)
    return sum(scores) / len(scores)


def _safeguard_score(record: dict, safeguard: dict) -> tuple[float, list[str]]:
    checks: list[tuple[bool, str]] = []
    cap = safeguard["max_single_asset_sell_pct"]
    schedule = record.get("execution_schedule") or []
    sells = [item for item in schedule if item["action"] == "sell"]
    intended_sells = [
        item for item in record["output"]["actions"] if item["action"] == "sell"
    ]
    checks.append((
        not intended_sells or bool(sells) or cap == 0,
        "Every intended sale has a deterministic execution schedule.",
    ))
    checks.append((
        all(float(item["size_pct"]) <= cap + 1e-9 for item in sells),
        f"Every per-round asset sale is at or below the approved {cap:g}% cap.",
    ))
    portfolio_cap = safeguard.get("max_daily_portfolio_sell_pct")
    if portfolio_cap is not None:
        by_round: dict[int, float] = {}
        for item in sells:
            round_number = int(item["round"])
            by_round[round_number] = by_round.get(round_number, 0.0) + (
                float(item.get("portfolio_weight", 0.0))
                * float(item["size_pct"])
            )
        checks.append((
            all(
                value <= float(portfolio_cap) + 1e-9
                for value in by_round.values()
            ),
            (
                "Every session sells at most "
                f"{float(portfolio_cap):g}% of starting portfolio value."
            ),
        ))
    if safeguard["require_staged_execution"]:
        minimum = int(safeguard.get("minimum_stages", 2))
        spacing = int(safeguard.get("minimum_spacing_sessions", 1))
        by_asset: dict[str, list[int]] = {}
        for item in sells:
            by_asset.setdefault(item["asset_id"], []).append(int(item["round"]))
        paced = all(
            len(rounds) >= minimum
            and all(right - left >= spacing for left, right in zip(
                sorted(rounds), sorted(rounds)[1:]
            ))
            for rounds in by_asset.values()
        )
        checks.append((
            paced,
            f"Each sale is paced across at least {minimum} rounds separated by {spacing} session(s).",
        ))
    if not safeguard["allow_hedging"]:
        checks.append((
            not any(item["action"] == "hedge" for item in schedule),
            "No hedge is used when hedging is disallowed.",
        ))
    return float(all(ok for ok, _ in checks)), [
        f"{'PASS' if ok else 'FAIL'} — {description}" for ok, description in checks
    ]


def _run_score(record: dict, suite: dict, evidence: list[dict]) -> dict:
    run_id = record["run_id"]
    if record["status"] != "complete":
        return {
            "run_id": run_id,
            "institution_id": record["institution_id"],
            "condition": record["condition"],
            "status": "failed",
            "score": None,
            "passed": False,
            "failure": record.get("error", "agent run failed"),
        }
    output = record["output"]
    institution = dict(INSTITUTIONS[record["institution_id"]])
    if record.get("portfolio"):
        institution["portfolio"] = record["portfolio"]
    case = next(item for item in suite["cases"] if item["condition"] == record["condition"])
    known_ids = {item["id"] for item in evidence}
    case_ids = set(case["evidence_ids"])
    cited = set(output["evidence_ids"])
    market_symbols = {item.get("symbol"): item["id"] for item in evidence if item.get("symbol")}
    evidence_assets = {
        asset_id for asset_id, meta in INSTRUMENTS.items()
        if meta.get("symbol") in market_symbols
    }
    evidence_grounding = (
        len(cited & case_ids) / len(cited)
        if cited and cited <= known_ids else 0.0
    )
    dimensions = {
        "schema_validity": 1.0,
        "evidence_grounding": evidence_grounding,
        "portfolio_alignment": _portfolio_alignment(
            institution, output["actions"], evidence_assets
        ),
        "constraint_awareness": _constraint_score(
            institution, output["constraints_considered"]
        ),
        "safeguard_adherence": 1.0,
    }
    safeguard_checks: list[str] = []
    applicable = set(dimensions)
    if record["condition"] == "safeguarded":
        dimensions["safeguard_adherence"], safeguard_checks = _safeguard_score(
            record, suite["safeguard"]
        )
    else:
        applicable.remove("safeguard_adherence")
    weights = suite["rubric_weights"]
    denominator = sum(weights[key] for key in applicable)
    score = sum(dimensions[key] * weights[key] for key in applicable) / denominator
    tools = {item["tool"] for item in record.get("tool_results", [])}
    return {
        "run_id": run_id,
        "institution_id": record["institution_id"],
        "condition": record["condition"],
        "status": "scored",
        "dimensions": dimensions,
        "applicable_dimensions": sorted(applicable),
        "tool_execution": {
            "selected": record.get("tool_plan", {}).get("tool_requests", []),
            "completed": sorted(tools),
            "portfolio_and_constraints_used": {
                "portfolio_shock", "constraint_register"
            } <= tools,
        },
        "safeguard_checks": safeguard_checks,
        "score": score,
        "passed": score >= suite["pass_threshold"],
    }


def _jaccard_mean(outputs: list[dict]) -> float | None:
    sets = [
        {(a["asset_id"], a["action"]) for a in output["actions"]
         if a["action"] != "hold"}
        for output in outputs
    ]
    pairs = list(combinations(sets, 2))
    if not pairs:
        return None
    values = []
    for left, right in pairs:
        union = left | right
        values.append(len(left & right) / len(union) if union else 1.0)
    return statistics.fmean(values)


def _action_class_jaccard(outputs: list[dict]) -> float | None:
    sets = [
        {a["action"] for a in output["actions"] if a["action"] != "hold"}
        for output in outputs
    ]
    pairs = list(combinations(sets, 2))
    if not pairs:
        return None
    return statistics.fmean(
        len(left & right) / len(left | right) if left | right else 1.0
        for left, right in pairs
    )


def _sell_pressure(record: dict) -> float:
    if record["status"] != "complete":
        return 0.0
    weights = {
        item["asset_id"]: item.get("weight", item.get("weight_pct", 0.0))
        for item in (
            record.get("portfolio")
            or INSTITUTIONS[record["institution_id"]]["portfolio"]
        )
    }
    return sum(
        weights.get(action["asset_id"], 0.0) * action["size_pct"]
        for action in record["output"]["actions"]
        if action["action"] == "sell"
    )


def _condition_metrics(records: list[dict], condition: str) -> dict:
    attempted = [item for item in records if item["condition"] == condition]
    valid = [item for item in attempted if item["status"] == "complete"]
    outputs = [item["output"] for item in valid]
    stances = Counter(item["stance"] for item in outputs)
    majority = max(stances.values()) / len(outputs) if outputs else None
    pressures = [_sell_pressure(item) for item in valid]
    urgency = [item["urgency"] for item in outputs]
    active = [
        any(action["action"] != "hold" for action in item["actions"])
        for item in outputs
    ]
    sellers = [
        any(action["action"] == "sell" for action in item["actions"])
        for item in outputs
    ]
    return {
        "condition": condition,
        "attempted": len(attempted),
        "valid": len(valid),
        "failed": len(attempted) - len(valid),
        "stance_distribution": dict(sorted(stances.items())),
        "stance_majority_share": majority,
        "hold_rate": stances.get("hold", 0) / len(outputs) if outputs else None,
        "risk_off_rate": stances.get("risk_off", 0) / len(outputs) if outputs else None,
        "mean_normalised_sell_pct": statistics.fmean(pressures) if pressures else None,
        "mean_desired_sell_pct": statistics.fmean(pressures) if pressures else None,
        "sell_pct_population_stddev": statistics.pstdev(pressures) if pressures else None,
        "mean_action_jaccard": _jaccard_mean(outputs),
        "mean_action_class_jaccard": _action_class_jaccard(outputs),
        "active_action_rate": sum(active) / len(active) if active else None,
        "seller_rate": sum(sellers) / len(sellers) if sellers else None,
        "sell_action_count": sum(
            action["action"] == "sell" for item in outputs for action in item["actions"]
        ),
        "urgency_mean": statistics.fmean(urgency) if urgency else None,
        "urgency_population_stddev": statistics.pstdev(urgency) if urgency else None,
    }


def calculate_scores(records: list[dict], suite: dict, evidence: list[dict],
                     expected_taxonomy: str | None,
                     classification: dict,
                     evidence_verification: dict | None = None) -> dict:
    run_scores = [_run_score(record, suite, evidence) for record in records]
    conditions = [case["condition"] for case in suite["cases"]]
    condition_metrics = {
        condition: _condition_metrics(records, condition)
        for condition in conditions
    }
    stress = condition_metrics.get("stress", {})
    safeguard = condition_metrics.get("safeguarded", {})
    stress_sell = stress.get("mean_normalised_sell_pct")
    safe_sell = safeguard.get("mean_normalised_sell_pct")
    reduction = None
    if stress_sell is not None and safe_sell is not None and stress_sell > 0:
        reduction = (stress_sell - safe_sell) / stress_sell

    execution_simulation = simulate(records, suite, evidence)
    quality_passed = bool(run_scores) and all(
        item.get("status") == "scored" and item.get("passed")
        for item in run_scores
    )
    required_tools = {"portfolio_shock", "constraint_register", "evidence_lookup"}
    tools_passed = all(
        required_tools <= {
            result.get("tool") for result in record.get("tool_results", [])
        }
        for record in records
        if record.get("status") == "complete"
        and record.get("decision_origin") != "deterministic_policy_application"
    )
    execution_simulation["verification"]["checks"].extend([
        {
            "name": "quality_thresholds",
            "passed": quality_passed,
            "detail": (
                "Every required agent record passed the approved quality threshold."
                if quality_passed else
                "One or more required agent records failed or fell below the "
                "approved quality threshold."
            ),
        },
        {
            "name": "required_tool_execution",
            "passed": tools_passed,
            "detail": (
                "Every model decision received portfolio, constraint and evidence "
                "tool outputs."
                if tools_passed else
                "One or more model decisions did not receive every required "
                "portfolio, constraint and evidence tool output."
            ),
        },
    ])
    execution_simulation["verification"]["passed"] = (
        execution_simulation["verification"]["passed"]
        and quality_passed and tools_passed
    )
    execution_simulation["verification"]["release_eligible"] = (
        execution_simulation["verification"]["release_eligible"]
        and quality_passed and tools_passed
    )
    if evidence_verification is not None:
        evidence_passed = bool(evidence_verification.get("passed"))
        execution_simulation["verification"]["checks"].insert(0, {
            "name": "evidence_snapshot",
            "passed": evidence_passed,
            "detail": (
                "Required evidence coverage and dated paths passed collection gates."
                if evidence_passed else
                "Evidence snapshot failed collection correctness gates."
            ),
        })
        execution_simulation["verification"]["passed"] = (
            execution_simulation["verification"]["passed"] and evidence_passed
        )
        execution_simulation["verification"]["release_eligible"] = (
            execution_simulation["verification"]["release_eligible"]
            and evidence_passed
        )

    institution_scores = {}
    for institution_id in sorted({item["institution_id"] for item in records}):
        values = [
            item["score"] for item in run_scores
            if item["institution_id"] == institution_id and item["score"] is not None
        ]
        institution_scores[institution_id] = {
            "name": INSTITUTIONS[institution_id]["name"],
            "mean_quality_score": statistics.fmean(values) if values else None,
            "passed_runs": sum(
                bool(item["passed"]) for item in run_scores
                if item["institution_id"] == institution_id
            ),
            "scored_runs": len(values),
        }

    classifier_observed = (
        classification.get("human_override", {})
        .get("original", {})
        .get("event_type", classification["event_type"])
    )
    return {
        "definition": (
            "Quality scores assess validated provider outputs and tool traces. "
            "Desired sell is the equal-notional AI intent before policy. "
            "Scheduled and executed selling apply the approved deterministic "
            "policy; market impact is calculated from executed selling only."
        ),
        "classification_check": {
            "expected": expected_taxonomy,
            "observed": classifier_observed,
            "approved": classification["event_type"],
            "matched": (
                classifier_observed == expected_taxonomy
                if expected_taxonomy else None
            ),
        },
        "pass_threshold": suite["pass_threshold"],
        "rubric_weights": suite["rubric_weights"],
        "run_scores": run_scores,
        "institution_scores": institution_scores,
        "conditions": condition_metrics,
        "safeguard_sell_reduction": reduction,
        "safeguard_sell_reduction_deprecated": True,
        "execution_simulation": execution_simulation,
        "safeguard_effects": execution_simulation["effects"],
        "verification": execution_simulation["verification"],
    }
