"""Versioned, operator-approved rules for the fast deterministic demo path.

The values here are editable scenario defaults. They are never model outputs and
are not claims about how a named real institution trades.
"""
from __future__ import annotations

from ..experiment.catalog import V5_INSTITUTION_PROFILES
from .models import InstitutionDecision, InstitutionStressSignal


DEMO_RULESET_VERSION = "deterministic-stress-sell-v1"
DEFAULT_DAILY_PORTFOLIO_CAP_PCT = 10.0


_TARGETS = {
    institution_id: (
        20.0,
        "If the assigned AI flags market stress, sell 20% of every holding.",
    )
    for institution_id in V5_INSTITUTION_PROFILES
}


def default_stress_rules(institution_ids: list[str]) -> list[dict]:
    rules = []
    for institution_id in institution_ids:
        if institution_id not in _TARGETS:
            raise ValueError(f"no deterministic demo rule exists for {institution_id}")
        profile = V5_INSTITUTION_PROFILES[institution_id]
        target, description = _TARGETS[institution_id]
        rules.append({
            "rule_id": f"RULE-{institution_id}-STRESS-SELL-V1",
            "ruleset_version": DEMO_RULESET_VERSION,
            "institution_id": institution_id,
            "institution_name": profile.display_name,
            "trigger_id": profile.triggers[0].trigger_id,
            "condition": "The assigned AI detects market stress in the approved evidence.",
            "target_sell_portfolio_pct": target,
            "size_basis": "pct_of_starting_portfolio_value",
            "allocation": "pro_rata_across_selected_holdings",
            "unmitigated_timing": "full_target_on_first_session",
            "description": description,
            "origin": "operator_editable_scenario_default",
        })
    return rules


def decision_from_stress_rule(
    case_pack: dict, signal: InstitutionStressSignal, rule: dict
) -> dict:
    """Apply a frozen rule to a model's stress signal without model discretion."""

    if rule["institution_id"] != case_pack["institution_id"]:
        raise ValueError("deterministic rule belongs to another institution")
    target = float(rule["target_sell_portfolio_pct"])
    active = bool(signal.stress_detected and target > 0.0)
    if active:
        actions = [{
            "asset_id": holding["asset_id"],
            "action": "sell",
            "size_pct": target,
            "timing": "immediate",
            "rationale": (
                f"Rule {rule['rule_id']} applies a {target:g}% pro-rata portfolio "
                "reduction after the assigned AI detected stress."
            ),
        } for holding in case_pack["portfolio"]]
        stance = "risk_off"
        executive_decision = (
            f"Stress was detected. Apply approved rule {rule['rule_id']}: sell "
            f"{target:g}% of starting portfolio value pro rata."
        )
    else:
        actions = [{
            "asset_id": holding["asset_id"],
            "action": "hold",
            "size_pct": 0.0,
            "timing": "monitor",
            "rationale": (
                "The approved sell rule is inactive because the assigned AI did "
                "not detect stress."
            ),
        } for holding in case_pack["portfolio"]]
        stance = "hold"
        executive_decision = (
            f"Stress was not detected. Approved rule {rule['rule_id']} produces no sale."
        )
    return InstitutionDecision.model_validate({
        "stance": stance,
        "executive_decision": executive_decision,
        "actions": actions,
        "urgency": signal.severity,
        "confidence": signal.confidence,
        "constraints_considered": case_pack["constraints"],
        "evidence_ids": signal.evidence_ids,
    }).model_dump()
