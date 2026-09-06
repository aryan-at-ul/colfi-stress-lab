"""Deterministic first `5 → 6` world transition for the v5 experiment."""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict

from .models import (
    ExperimentPreregistration,
    HoldingPosition,
    InstitutionDecisionV5,
    InstitutionProfileV5,
    content_sha256,
)


TRANSITION_CONTRACT_VERSION = "colfi-transition-v5.1"


def _rounded(value: float) -> float:
    return round(value, 12)


def _order_id(world_id: str, profile_id: str, action_id: str,
              suffix: str = "") -> str:
    identity = f"{world_id}|{profile_id}|{action_id}|{suffix}"
    return "ORDER-" + hashlib.sha256(identity.encode()).hexdigest()[:20].upper()


def _return_in_base_currency(instrument_id: str, base_currency: str,
                             returns: dict[str, float], assumptions: dict) -> float:
    local_return = returns[instrument_id] / 100.0
    local_currency = assumptions[instrument_id].valuation_currency
    if local_currency == base_currency:
        return local_return * 100.0
    fx = next((
        item for item in assumptions.values()
        if item.instrument_kind == "fx" and {
            item.fx_base_currency, item.fx_quote_currency
        } == {local_currency, base_currency}
    ), None)
    if fx is None:
        raise ValueError(
            f"no FX translation instrument for {local_currency}/{base_currency}"
        )
    fx_move = returns[fx.instrument_id] / 100.0
    if fx.fx_base_currency == local_currency and fx.fx_quote_currency == base_currency:
        currency_factor = 1.0 + fx_move
    else:
        currency_factor = 1.0 / (1.0 + fx_move)
    return ((1.0 + local_return) * currency_factor - 1.0) * 100.0


def transition_initial_world(
    plan: ExperimentPreregistration,
    world: dict,
    members: list[dict],
    profiles: list[InstitutionProfileV5],
    *,
    round_index: int = 1,
    input_stage: str = "initial",
    starting_private_snapshots: list[dict] | None = None,
    previous_common_snapshot: dict | None = None,
) -> dict:
    """Apply seven accepted decisions and emit the next common/private state."""

    if len(members) != 7:
        raise ValueError("a world transition requires seven members")
    if round_index not in {1, 2} or input_stage not in {"initial", "feedback"}:
        raise ValueError("only initial round 1 and feedback round 2 are supported")
    if (round_index == 1) != (input_stage == "initial"):
        raise ValueError("transition round and input stage do not match")
    if world["preregistration_id"] != plan.preregistration_id:
        raise ValueError("world does not belong to the preregistration")
    if world["stage1_action_set_sha256"] != content_sha256([
        {
            "institution_profile_version_id": member[
                "institution_profile_version_id"
            ],
            "initial_run_cell_id": member["initial_run_cell_id"],
            "initial_decision_sha256": member["initial_decision_sha256"],
        }
        for member in sorted(members, key=lambda item: item["ordinal"])
    ]):
        raise ValueError("world stage-one action-set hash does not match its members")

    profile_map = {profile.profile_version_id: profile for profile in profiles}
    if set(profile_map) != {
        member["institution_profile_version_id"] for member in members
    }:
        raise ValueError("profiles do not exactly match world membership")
    assumptions = {
        item.instrument_id: item
        for item in plan.transition_policy.instrument_assumptions
    }
    liquidity_shares = {
        (item.institution_profile_version_id, item.instrument_id): (
            item.provided_depth_share_pct
        )
        for item in plan.transition_policy.liquidity_provision
    }
    previous_markets = {
        item["instrument_id"]: item
        for item in (previous_common_snapshot or {}).get("markets", [])
    }
    depth_reference = {
        instrument_id: previous_markets.get(instrument_id, {}).get(
            "effective_depth_system_notional_pct",
            assumption.round_depth_system_notional_pct,
        )
        for instrument_id, assumption in assumptions.items()
    }
    starting_by_profile = {
        item["institution_profile_version_id"]: item
        for item in (starting_private_snapshots or [])
    }
    if round_index == 2 and set(starting_by_profile) != set(profile_map):
        raise ValueError("round two requires all seven prior private snapshots")
    order_scope = f"{world['id']}|R{round_index}"

    positions: dict[str, dict[str, float]] = {}
    cash: dict[str, float] = {}
    orders: list[dict] = []
    withdrawal_by_instrument: dict[str, float] = defaultdict(float)
    directed_sell_by_profile: dict[str, float] = defaultdict(float)
    decisions: dict[str, InstitutionDecisionV5] = {}

    for member in members:
        profile_id = member["institution_profile_version_id"]
        profile = profile_map[profile_id]
        decision = InstitutionDecisionV5.model_validate(member["decision"])
        if decision.institution_id != profile.institution_id or decision.stage != input_stage:
            raise ValueError("world decision metadata does not match its profile")
        decisions[profile_id] = decision
        if round_index == 1:
            positions[profile_id] = {
                holding.instrument_id: (
                    profile.system_footprint_pct * holding.notional_pct / 100.0
                )
                for holding in profile.holdings
                if isinstance(holding, HoldingPosition)
            }
            cash_pct = profile.liquidity_state.cash_buffer_pct or 0.0
            cash[profile_id] = profile.system_footprint_pct * cash_pct / 100.0
        else:
            prior = starting_by_profile[profile_id]
            positions[profile_id] = {
                item["instrument_id"]: float(item["after"])
                for item in prior["positions_system_notional_pct"]
            }
            cash[profile_id] = float(prior["cash_system_notional_pct"]["after"])

    initial_positions = {
        profile_id: dict(values) for profile_id, values in positions.items()
    }
    initial_cash = dict(cash)

    # Directed trades, hedges and depth changes are applied before residual
    # deleveraging so one action cannot be counted twice.
    for member in sorted(members, key=lambda item: item["ordinal"]):
        profile_id = member["institution_profile_version_id"]
        profile = profile_map[profile_id]
        decision = decisions[profile_id]
        for action in decision.actions:
            requested = profile.system_footprint_pct * action.size_value / 100.0
            if action.action_type == "sell":
                available = positions[profile_id].get(action.instrument_id, 0.0)
                executed = min(requested, available)
                positions[profile_id][action.instrument_id] = available - executed
                cash[profile_id] += executed
                directed_sell_by_profile[profile_id] += executed
                orders.append({
                    "order_id": _order_id(order_scope, profile_id, action.action_id),
                    "institution_profile_version_id": profile_id,
                    "source_decision_id": decision.decision_id,
                    "source_action_id": action.action_id,
                    "order_kind": "directed_sell",
                    "instrument_id": action.instrument_id,
                    "side": "sell",
                    "requested_system_notional_pct": _rounded(requested),
                    "executed_system_notional_pct": _rounded(executed),
                    "curtailed_system_notional_pct": _rounded(requested - executed),
                })
            elif action.action_type == "buy_support":
                executed = min(requested, cash[profile_id])
                positions[profile_id][action.instrument_id] = (
                    positions[profile_id].get(action.instrument_id, 0.0) + executed
                )
                cash[profile_id] -= executed
                orders.append({
                    "order_id": _order_id(order_scope, profile_id, action.action_id),
                    "institution_profile_version_id": profile_id,
                    "source_decision_id": decision.decision_id,
                    "source_action_id": action.action_id,
                    "order_kind": "buy_support",
                    "instrument_id": action.instrument_id,
                    "side": "buy",
                    "requested_system_notional_pct": _rounded(requested),
                    "executed_system_notional_pct": _rounded(executed),
                    "curtailed_system_notional_pct": _rounded(requested - executed),
                })
            elif action.action_type == "hedge":
                orders.append({
                    "order_id": _order_id(order_scope, profile_id, action.action_id),
                    "institution_profile_version_id": profile_id,
                    "source_decision_id": decision.decision_id,
                    "source_action_id": action.action_id,
                    "order_kind": "hedge",
                    "instrument_id": action.hedge_instrument_id,
                    "side": action.order_side,
                    "requested_system_notional_pct": _rounded(requested),
                    "executed_system_notional_pct": _rounded(requested),
                    "curtailed_system_notional_pct": 0.0,
                })
            elif action.action_type == "withdraw_liquidity":
                instrument_id = action.market_instrument_id
                share = liquidity_shares.get((profile_id, instrument_id), 0.0)
                baseline = depth_reference[instrument_id]
                withdrawn = baseline * share / 100.0 * action.size_value / 100.0
                withdrawal_by_instrument[instrument_id] += withdrawn
                orders.append({
                    "order_id": _order_id(order_scope, profile_id, action.action_id),
                    "institution_profile_version_id": profile_id,
                    "source_decision_id": decision.decision_id,
                    "source_action_id": action.action_id,
                    "order_kind": "liquidity_withdrawal",
                    "instrument_id": instrument_id,
                    "side": "none",
                    "requested_system_notional_pct": _rounded(withdrawn),
                    "executed_system_notional_pct": _rounded(withdrawn),
                    "curtailed_system_notional_pct": 0.0,
                })

    for member in sorted(members, key=lambda item: item["ordinal"]):
        profile_id = member["institution_profile_version_id"]
        profile = profile_map[profile_id]
        decision = decisions[profile_id]
        for action in decision.actions:
            if action.action_type != "deleverage":
                continue
            gross_pct = profile.portfolio_state.gross_exposure_pct or 100.0
            target = (
                profile.system_footprint_pct * gross_pct / 100.0
                * action.size_value / 100.0
            )
            residual = max(0.0, target - directed_sell_by_profile[profile_id])
            eligible = list(action.preferred_instrument_ids) or sorted(
                positions[profile_id]
            )
            eligible = [
                instrument_id for instrument_id in eligible
                if positions[profile_id].get(instrument_id, 0.0) > 0
            ]
            available_total = sum(positions[profile_id][item] for item in eligible)
            executed_total = min(residual, available_total)
            remaining = executed_total
            for index, instrument_id in enumerate(eligible):
                available = positions[profile_id][instrument_id]
                if index == len(eligible) - 1:
                    executed = min(available, remaining)
                else:
                    executed = min(
                        available,
                        executed_total * available / available_total
                        if available_total else 0.0,
                    )
                remaining -= executed
                positions[profile_id][instrument_id] -= executed
                cash[profile_id] += executed
                orders.append({
                    "order_id": _order_id(
                        order_scope, profile_id, action.action_id, instrument_id
                    ),
                    "institution_profile_version_id": profile_id,
                    "source_decision_id": decision.decision_id,
                    "source_action_id": action.action_id,
                    "order_kind": "forced_deleverage",
                    "instrument_id": instrument_id,
                    "side": "sell",
                    "requested_system_notional_pct": _rounded(
                        residual * available / available_total
                        if available_total else 0.0
                    ),
                    "executed_system_notional_pct": _rounded(executed),
                    "curtailed_system_notional_pct": 0.0,
                })
            curtailed = residual - executed_total
            if curtailed > 1e-12:
                orders.append({
                    "order_id": _order_id(
                        order_scope, profile_id, action.action_id, "curtailed"
                    ),
                    "institution_profile_version_id": profile_id,
                    "source_decision_id": decision.decision_id,
                    "source_action_id": action.action_id,
                    "order_kind": "forced_deleverage_unfilled",
                    "instrument_id": "portfolio",
                    "side": "none",
                    "requested_system_notional_pct": _rounded(curtailed),
                    "executed_system_notional_pct": 0.0,
                    "curtailed_system_notional_pct": _rounded(curtailed),
                })

    flow = defaultdict(lambda: {
        "directed_sell": 0.0,
        "forced_deleverage": 0.0,
        "hedge_sell": 0.0,
        "hedge_buy": 0.0,
        "buy_support": 0.0,
    })
    for order in orders:
        instrument_id = order["instrument_id"]
        executed = order["executed_system_notional_pct"]
        if instrument_id not in assumptions:
            continue
        if order["order_kind"] == "directed_sell":
            flow[instrument_id]["directed_sell"] += executed
        elif order["order_kind"] == "forced_deleverage":
            flow[instrument_id]["forced_deleverage"] += executed
        elif order["order_kind"] == "buy_support":
            flow[instrument_id]["buy_support"] += executed
        elif order["order_kind"] == "hedge":
            flow[instrument_id][f"hedge_{order['side']}"] += executed

    markets = []
    returns = {}
    for instrument_id in plan.instrument_ids:
        assumption = assumptions[instrument_id]
        values = flow[instrument_id]
        net_sale = (
            values["directed_sell"]
            + values["forced_deleverage"]
            + values["hedge_sell"]
            - values["hedge_buy"]
            - values["buy_support"]
        )
        baseline_depth = depth_reference[instrument_id]
        withdrawn = min(withdrawal_by_instrument[instrument_id], baseline_depth)
        effective_depth = max(baseline_depth - withdrawn, baseline_depth * 0.01)
        depth_multiplier = baseline_depth / effective_depth
        raw_impact = (
            assumption.square_root_impact_coefficient_pct
            * math.sqrt(abs(net_sale) / assumption.daily_adv_system_notional_pct)
            * depth_multiplier
        ) if abs(net_sale) > 1e-12 else 0.0
        signed_impact = math.copysign(
            min(raw_impact, plan.transition_policy.max_abs_price_impact_pct),
            -net_sale,
        ) if raw_impact else 0.0
        returns[instrument_id] = signed_impact
        markets.append({
            "evidence_id": f"SIM-{instrument_id.upper()}-R{round_index}",
            "instrument_id": instrument_id,
            "reference_price_index": previous_markets.get(instrument_id, {}).get(
                "new_price_index", assumption.reference_price_index
            ),
            "new_price_index": _rounded(
                previous_markets.get(instrument_id, {}).get(
                    "new_price_index", assumption.reference_price_index
                ) * (1.0 + signed_impact / 100.0)
            ),
            "price_impact_pct": _rounded(signed_impact),
            "directed_sell_system_notional_pct": _rounded(values["directed_sell"]),
            "forced_sell_system_notional_pct": _rounded(values["forced_deleverage"]),
            "hedge_sell_system_notional_pct": _rounded(values["hedge_sell"]),
            "hedge_buy_system_notional_pct": _rounded(values["hedge_buy"]),
            "support_buy_system_notional_pct": _rounded(values["buy_support"]),
            "net_sale_system_notional_pct": _rounded(net_sale),
            "baseline_depth_system_notional_pct": _rounded(baseline_depth),
            "withdrawn_depth_system_notional_pct": _rounded(withdrawn),
            "effective_depth_system_notional_pct": _rounded(effective_depth),
        })

    policy_sha = plan.transition_policy.sha256
    transition_identity = "|".join((
        world["id"],
        str(round_index),
        world["stage1_action_set_sha256"],
        policy_sha,
    ))
    transition_id = "TRANSITION-" + hashlib.sha256(
        transition_identity.encode()
    ).hexdigest()[:20].upper()
    common = {
        "schema_version": 5,
        "snapshot_kind": "common",
        "world_id": world["id"],
        "round_index": round_index,
        "as_of": (
            plan.decision_clock.feedback_at.isoformat()
            if round_index == 1
            else plan.decision_clock.second_transition_at.isoformat()
        ),
        "transition_contract_version": TRANSITION_CONTRACT_VERSION,
        "transition_policy_sha256": policy_sha,
        "input_action_set_sha256": world["stage1_action_set_sha256"],
        "markets": markets,
    }
    common_sha = content_sha256(common)
    common_id = "SNAP-C-" + common_sha[:20].upper()

    private_snapshots = []
    for member in sorted(members, key=lambda item: item["ordinal"]):
        profile_id = member["institution_profile_version_id"]
        profile = profile_map[profile_id]
        base_returns = {
            instrument_id: _return_in_base_currency(
                instrument_id, profile.base_currency, returns, assumptions
            )
            for instrument_id in positions[profile_id]
        }
        incremental_pnl = sum(
            positions[profile_id].get(instrument_id, 0.0)
            * base_returns[instrument_id]
            / max(profile.system_footprint_pct, 1e-12)
            for instrument_id in positions[profile_id]
        )
        private = {
            "schema_version": 5,
            "snapshot_kind": "private",
            "world_id": world["id"],
            "round_index": round_index,
            "as_of": (
                plan.decision_clock.feedback_at.isoformat()
                if round_index == 1
                else plan.decision_clock.second_transition_at.isoformat()
            ),
            "institution_profile_version_id": profile_id,
            "institution_id": profile.institution_id,
            "state_evidence_id": (
                f"SIM-{profile.institution_id}-PRIVATE-R{round_index}"
            ),
            "base_currency": profile.base_currency,
            "positions_system_notional_pct": [
                {
                    "instrument_id": instrument_id,
                    "valuation_currency": assumptions[instrument_id].valuation_currency,
                    "before": _rounded(initial_positions[profile_id].get(instrument_id, 0.0)),
                    "after": _rounded(value),
                    "round_return_in_base_currency_pct": _rounded(
                        base_returns[instrument_id]
                    ),
                }
                for instrument_id, value in sorted(positions[profile_id].items())
            ],
            "cash_system_notional_pct": {
                "before": _rounded(initial_cash[profile_id]),
                "after": _rounded(cash[profile_id]),
            },
            "incremental_base_currency_return_pct_of_footprint": _rounded(
                incremental_pnl
            ),
            "fx_translation_status": "applied_with_preregistered_fx_pairs",
            "trigger_state": [item.model_dump(mode="json") for item in profile.triggers],
            "own_orders": [
                order for order in orders
                if order["institution_profile_version_id"] == profile_id
            ],
        }
        private_sha = content_sha256(private)
        private_snapshots.append({
            "id": "SNAP-P-" + private_sha[:20].upper(),
            "sha256": private_sha,
            "content": private,
        })

    transition = {
        "schema_version": 5,
        "transition_id": transition_id,
        "world_id": world["id"],
        "round_index": round_index,
        "input_stage": input_stage,
        "input_action_set_sha256": world["stage1_action_set_sha256"],
        "transition_policy_sha256": policy_sha,
        "common_snapshot_id": common_id,
        "common_snapshot_sha256": common_sha,
        "private_snapshot_sha256": {
            snapshot["content"]["institution_profile_version_id"]: snapshot["sha256"]
            for snapshot in private_snapshots
        },
        "order_count": len(orders),
    }
    return {
        "transition": transition,
        "common_snapshot": {
            "id": common_id,
            "sha256": common_sha,
            "content": common,
        },
        "private_snapshots": private_snapshots,
        "orders": orders,
    }


def transition_feedback_world(
    plan: ExperimentPreregistration,
    world: dict,
    feedback_decisions: list[dict],
    previous_common_snapshot: dict,
    previous_private_snapshots: list[dict],
    profiles: list[InstitutionProfileV5],
) -> dict:
    """Apply the seven feedback decisions for the second `6 → 5` transition."""

    if len(feedback_decisions) != 7:
        raise ValueError("round two requires seven accepted feedback decisions")
    by_profile = {
        item["institution_profile_version_id"]: item
        for item in feedback_decisions
    }
    if set(by_profile) != set(plan.institution_profile_version_ids):
        raise ValueError("feedback decisions do not exactly cover world membership")
    members = []
    for ordinal, profile_id in enumerate(plan.institution_profile_version_ids):
        item = by_profile[profile_id]
        if item["world_id"] != world["id"]:
            raise ValueError("feedback decision belongs to another world")
        members.append({
            "ordinal": ordinal,
            "institution_profile_version_id": profile_id,
            "institution_id": item["institution_id"],
            "model_spec_id": item["model_spec_id"],
            "initial_run_cell_id": item["run_cell_id"],
            "initial_decision_sha256": item["content_sha256"],
            "decision": item["decision"],
        })
    action_set_sha = content_sha256([{
        "institution_profile_version_id": member["institution_profile_version_id"],
        "initial_run_cell_id": member["initial_run_cell_id"],
        "initial_decision_sha256": member["initial_decision_sha256"],
    } for member in members])
    round_two_world = {**world, "stage1_action_set_sha256": action_set_sha}
    return transition_initial_world(
        plan,
        round_two_world,
        members,
        profiles,
        round_index=2,
        input_stage="feedback",
        starting_private_snapshots=previous_private_snapshots,
        previous_common_snapshot=previous_common_snapshot,
    )
