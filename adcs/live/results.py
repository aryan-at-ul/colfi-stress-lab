"""Read-only projection for the short deterministic safeguard demonstration."""
from __future__ import annotations

from datetime import datetime, timezone

from .simulation import verify_simulation_arithmetic
from .sources import INSTRUMENTS


def _pct_effect_phrase(effect: dict, label: str) -> str:
    relative = effect.get("relative_reduction")
    status = effect.get("status", "undefined")
    if relative is None:
        return f"{label} had no defined relative change"
    amount = abs(float(relative)) * 100.0
    if status == "improved":
        return f"reduced {label} by {amount:.1f}%"
    if status == "worsened":
        return f"increased {label} by {amount:.1f}%"
    return f"left {label} unchanged"


def _display_effect(
    unmitigated: float | None,
    safeguarded: float | None,
    label: str,
) -> dict:
    """Build an honest display comparison for older persisted simulations."""
    if unmitigated is None or safeguarded is None:
        return {
            "unmitigated": unmitigated,
            "safeguarded": safeguarded,
            "relative_reduction": None,
            "status": "undefined",
            "interpretation": f"{label} is unavailable for this run.",
        }
    baseline = float(unmitigated)
    guarded = float(safeguarded)
    if baseline == 0.0:
        return {
            "unmitigated": baseline,
            "safeguarded": guarded,
            "relative_reduction": None,
            "status": "unchanged" if guarded == 0.0 else "undefined",
            "interpretation": (
                f"{label} was zero in both paths."
                if guarded == 0.0
                else f"Relative change in {label} is undefined from a zero baseline."
            ),
        }
    relative = (baseline - guarded) / abs(baseline)
    status = "improved" if relative > 0 else "worsened" if relative < 0 else "unchanged"
    return {
        "unmitigated": baseline,
        "safeguarded": guarded,
        "absolute_reduction": baseline - guarded,
        "relative_reduction": relative,
        "status": status,
        "interpretation": (
            f"Safeguarded {label} was {abs(relative) * 100:.1f}% "
            f"{'lower' if status == 'improved' else 'higher' if status == 'worsened' else 'different'} "
            "than unmitigated."
            if status != "unchanged"
            else f"{label} was unchanged."
        ),
        "formula": "100 * (unmitigated - safeguarded) / abs(unmitigated)",
    }


def _approval(approvals: list[dict], gate: str) -> dict | None:
    return next(
        (
            item for item in reversed(approvals)
            if item.get("gate") == gate and item.get("approved")
        ),
        None,
    )


def _approval_summary(approvals: list[dict], gate: str) -> dict | None:
    item = _approval(approvals, gate)
    if item is None:
        return None
    return {
        "actor": item["actor"],
        "at": datetime.fromtimestamp(
            float(item["at"]), tz=timezone.utc
        ).isoformat(),
        "note": item.get("note", ""),
    }


def _mean_unexecuted(records: list[dict], condition: str) -> float | None:
    values = [
        float(
            (item.get("policy_application") or {}).get(
                "unexecuted_portfolio_within_horizon_pct", 0.0
            )
        )
        for item in records
        if item.get("condition") == condition and item.get("status") == "complete"
    ]
    return sum(values) / len(values) if values else None


def _execution_pattern(record: dict | None) -> list[float]:
    if record is None:
        return []
    by_round: dict[int, float] = {}
    for item in record.get("execution_schedule", []):
        if item.get("action") != "sell":
            continue
        round_number = int(item["round"])
        by_round[round_number] = by_round.get(round_number, 0.0) + (
            float(item.get("portfolio_weight", 0.0))
            * float(item["size_pct"])
        )
    if not by_round:
        return []
    return [round(by_round.get(index, 0.0), 10) for index in range(1, max(by_round) + 1)]


def _primary_action(record: dict | None) -> str:
    if record is None or record.get("status") != "complete":
        return "No completed action"
    active = [
        item for item in record["output"]["actions"]
        if item["action"] != "hold"
    ]
    if not active:
        return "Hold"
    action = max(active, key=lambda item: float(item["size_pct"]))
    return (
        f"{action['action'].title()} {action['asset_id']} "
        f"{float(action['size_pct']):g}%"
    )


def _portfolio_label(record: dict | None) -> str:
    if record is None:
        return "Portfolio unavailable"
    holdings = record.get("portfolio") or []
    return " + ".join(
        f"{_holding_weight_pct(item):g}% "
        f"{INSTRUMENTS.get(item['asset_id'], {}).get('label', item['asset_id'])}"
        for item in holdings
    )


def _holding_weight_pct(item: dict) -> float:
    if item.get("weight") is not None:
        return float(item["weight"]) * 100.0
    return float(item.get("weight_pct", 0.0))


def _evidence_display_label(evidence_id: str, item: dict) -> str:
    """Return a human label while retaining raw IDs separately for audit."""
    asset_id = evidence_id[4:].lower() if evidence_id.startswith("MKT-") else ""
    instrument = INSTRUMENTS.get(asset_id, {})
    label = instrument.get("label") or item.get("title") or evidence_id
    symbol = str(item.get("symbol") or instrument.get("symbol") or "").lstrip("^")
    return f"{label} ({symbol})" if symbol and symbol.lower() not in label.lower() else label


def _execution_text(pattern: list[float], empty: str = "0%") -> str:
    if not pattern:
        return empty
    return " / ".join(
        f"{value:g}% on session {index}"
        for index, value in enumerate(pattern, start=1)
        if abs(value) > 1e-12
    ) or empty


def build_demo_result(
    row: dict, approvals: list[dict], events: list[dict]
) -> dict | None:
    """Build display data only from persisted, verified assessment values."""
    request = row.get("request") or {}
    suite = row.get("plan") or {}
    metrics = row.get("metrics") or {}
    simulation = metrics.get("execution_simulation") or {}
    if not simulation:
        return None
    deterministic = (
        request.get("execution_mode") == "deterministic_stress_rules"
    )

    conditions = simulation.get("conditions") or {}
    stress = conditions.get("stress") or {}
    safe = conditions.get("safeguarded") or {}
    effects = simulation.get("effects") or {}
    impact = effects.get("peak_modelled_impact") or {}
    added_loss = effects.get("model_added_loss_at_horizon") or _display_effect(
        stress.get("terminal_system_price_impact_pct"),
        safe.get("terminal_system_price_impact_pct"),
        "model-added impact at horizon",
    )
    cap = float(
        (suite.get("safeguard") or {}).get("max_daily_portfolio_sell_pct")
        or (suite.get("safeguard") or {}).get("max_single_asset_sell_pct")
        or 0.0
    )
    policy_label = (
        f"Replacing the 20% stress sale with a {cap:g}% safeguarded sale"
        if deterministic else
        f"The approved {cap:g}% per-asset execution policy"
    )
    headline = (
        f"{policy_label} "
        f"{_pct_effect_phrase(impact, 'modelled peak market impact')}."
    )
    if (
        deterministic
        and float(stress.get("scheduled_sell_pct") or 0.0) == 0.0
        and float(safe.get("scheduled_sell_pct") or 0.0) == 0.0
    ):
        headline = (
            "No institution stress flag activated the selling rule; neither "
            "the 20% nor the 10% path sold."
        )

    evidence_by_id = {
        item["id"]: item for item in (row.get("evidence") or {}).get("items", [])
    }
    responses = row.get("responses") or []
    display_verification = verify_simulation_arithmetic(
        conditions,
        effects,
        records=responses,
        settings=simulation.get("settings") or {},
        evidence=(row.get("evidence") or {}).get("items", []),
    )
    stress_records = {
        item["institution_id"]: item
        for item in responses
        if item.get("condition") == "stress"
        and item.get("status") == "complete"
        and item.get("sample") == 1
    }
    safe_records = {
        item["institution_id"]: item
        for item in responses
        if item.get("condition") == "safeguarded"
        and item.get("status") == "complete"
        and item.get("sample") == 1
    }
    assignment_models = {
        item["institution_id"]: item["model"]
        for item in request.get("institutions", [])
    }
    signal_rows = []
    for institution_id, record in stress_records.items():
        signal = record.get("stress_signal") or {}
        decision = record.get("output") or {}
        evidence_ids = (signal.get("evidence_ids") or decision.get(
            "evidence_ids", []
        ))[:2]
        cited = []
        for evidence_id in evidence_ids:
            item = evidence_by_id.get(evidence_id, {})
            cited.append(_evidence_display_label(evidence_id, item))
        signal_rows.append({
            "institution_id": institution_id,
            "institution_name": record["institution_name"],
            "model": record["model"],
            "stress_detected": signal.get("stress_detected"),
            "severity": signal.get("severity"),
            "confidence": signal.get("confidence"),
            "decision": _primary_action(record),
            "cited": cited,
            "evidence_ids": evidence_ids,
        })

    rule_rows = []
    for rule in suite.get("deterministic_rules", []):
        institution_id = rule["institution_id"]
        stress_record = stress_records.get(institution_id)
        safe_record = safe_records.get(institution_id)
        rule_rows.append({
            **rule,
            "model": assignment_models.get(institution_id),
            "portfolio_label": _portfolio_label(stress_record),
            "stress_detected": (
                (stress_record or {}).get("stress_signal") or {}
            ).get("stress_detected"),
            "unmitigated_execution_pct": _execution_pattern(stress_record),
            "safeguarded_execution_pct": _execution_pattern(safe_record),
            "unmitigated_text": (
                f"{float(rule['target_sell_portfolio_pct']):g}% on session 1"
                if (
                    (stress_record or {}).get("stress_signal") or {}
                ).get("stress_detected") else "0% — no stress flag"
            ),
            "safeguarded_text": _execution_text(
                _execution_pattern(safe_record), "0% — no stress flag"
            ),
        })
    if not rule_rows:
        for institution_id, stress_record in stress_records.items():
            safe_record = safe_records.get(institution_id)
            stress_pattern = _execution_pattern(stress_record)
            safe_pattern = _execution_pattern(safe_record)
            rule_rows.append({
                "institution_id": institution_id,
                "institution_name": stress_record["institution_name"],
                "model": stress_record["model"],
                "portfolio_label": _portfolio_label(stress_record),
                "description": (
                    "AI-generated action retained from this older assessment: "
                    f"{_primary_action(stress_record)}."
                ),
                "stress_detected": None,
                "target_sell_portfolio_pct": None,
                "unmitigated_execution_pct": stress_pattern,
                "safeguarded_execution_pct": safe_pattern,
                "unmitigated_text": (
                    " / ".join(f"{value:g}" for value in stress_pattern)
                    if stress_pattern else "No sale scheduled"
                ),
                "safeguarded_text": (
                    " / ".join(f"{value:g}" for value in safe_pattern)
                    if safe_pattern else "No sale scheduled"
                ),
            })
    signal_rows.sort(key=lambda item: item["institution_id"])
    rule_rows.sort(key=lambda item: item["institution_id"])

    rounds = []
    stress_rounds = stress.get("rounds", [])
    safe_rounds = safe.get("rounds", [])
    max_rounds = max(len(stress_rounds), len(safe_rounds))
    for index in range(max_rounds):
        left = stress_rounds[index] if index < len(stress_rounds) else {}
        right = safe_rounds[index] if index < len(safe_rounds) else {}
        rounds.append({
            "round": index + 1,
            "session_dates": sorted(set(
                (left.get("session_dates") or [])
                + (right.get("session_dates") or [])
            )),
            "unmitigated_sell_pct": left.get("net_executed_sell_pct"),
            "unmitigated_feedback_pct": left.get("feedback_sell_pct"),
            "safeguarded_sell_pct": right.get("net_executed_sell_pct"),
            "safeguarded_feedback_pct": right.get("feedback_sell_pct"),
        })

    stress_model_count = len({
        item.get("model") for item in signal_rows if item.get("model")
    })
    held_assets = {
        holding["asset_id"]
        for record in stress_records.values()
        for holding in record.get("portfolio", [])
    }
    warnings = []
    if deterministic and stress_model_count == 1:
        warnings.append(
            "All institution signals in this run used one AI model; unanimous "
            "flags are not a cross-model convergence result."
        )
    elif deterministic and 1 < stress_model_count < 6:
        warnings.append(
            f"This run used {stress_model_count} of the six-model demo roster; "
            "the stress-signal comparison covers only the selected models."
        )
    if request.get("event_id") == "svb_2023_run" and "us_banks" not in held_assets:
        warnings.append(
            "This SVB run contains no regional-bank holding, so the State Street "
            "SPDR S&P Regional Banking ETF (KRE) shock does not enter portfolio "
            "returns."
        )
    if (
        deterministic
        and float(stress.get("scheduled_sell_pct") or 0.0) > 0.0
        and float(stress.get("feedback_sell_pct") or 0.0) == 0.0
    ):
        warnings.append(
            "No modelled limit breach activated later forced selling in this run."
        )
    if not display_verification["passed"]:
        warnings.append(
            "The persisted display values did not pass deterministic arithmetic "
            "replay. Do not release this assessment until the mismatch is resolved."
        )

    return {
        "status": (
            "released" if row.get("status") == "complete"
            else "rejected" if row.get("status") == "rejected"
            else "draft"
        ),
        "assessment_id": row["id"],
        "event_label": (row.get("classification") or {}).get("event_label"),
        "start_date": request.get("start_date"),
        "end_date": request.get("end_date"),
        "institution_count": len(request.get("institutions", [])),
        "daily_cap_pct": cap,
        "execution_mode": request.get("execution_mode", "llm_decision"),
        "deterministic_rules": deterministic,
        "stress_flag_count": sum(
            item.get("stress_detected") is True for item in signal_rows
        ),
        "stress_signal_count": len(signal_rows),
        "stress_model_count": stress_model_count,
        "warnings": warnings,
        "display_verification": display_verification,
        "ai_section_title": (
            "Did each institution’s assigned AI flag stress?"
            if deterministic else
            "What did each institution’s assigned AI decide under stress?"
        ),
        "rules_section_title": (
            "The selling rules approved by a person"
            if deterministic else
            "The AI-generated actions retained from this older run"
        ),
        "headline": headline,
        "historical_exogenous_return_pct": stress.get(
            "historical_exogenous_system_return_pct"
        ),
        "caveat": (
            "AI desired selling is held fixed between paths; the safeguard "
            "changes only deterministic execution. Impact is calculated from "
            "executed selling under stated assumptions. This is not a forecast "
            "or actual firm behaviour."
            if deterministic else
            "This result uses the older AI-decision contract and its approved "
            "execution policy. It is retained as a real run, not relabelled as "
            "a deterministic-rule assessment."
        ),
        "cards": [
            {
                "id": "peak_impact",
                "label": "Peak modelled market impact",
                "unmitigated": impact.get("unmitigated"),
                "safeguarded": impact.get("safeguarded"),
                "unit": "%",
                "precision": 3,
                "effect": impact,
            },
            {
                "id": "initial_selling",
                "label": "Executable scheduled selling",
                "unmitigated": stress.get("scheduled_sell_pct"),
                "safeguarded": safe.get("scheduled_sell_pct"),
                "unit": "%",
                "precision": 2,
                "effect": effects.get("scheduled_sell_volume"),
            },
            {
                "id": "feedback_selling",
                "label": "Forced selling after limit breaches",
                "unmitigated": stress.get("feedback_sell_pct"),
                "safeguarded": safe.get("feedback_sell_pct"),
                "unit": "%",
                "precision": 3,
                "effect": effects.get("feedback_selling"),
            },
            {
                "id": "model_added_loss",
                "label": "Model-added impact at horizon",
                "unmitigated": added_loss.get("unmitigated"),
                "safeguarded": added_loss.get("safeguarded"),
                "unit": "%",
                "precision": 3,
                "effect": added_loss,
            },
        ],
        "rounds": rounds,
        "stress_signals": signal_rows,
        "rules": rule_rows,
        "assumptions": {
            "system_weighting": "Equal-notional across selected institutions",
            "portfolio_loss_formula": (
                "Historical market return is held fixed and displayed separately; "
                "safeguard effects use only model-added impact."
            ),
            "simulation": simulation.get("settings", {}),
            "evidence_sha256": simulation.get("input_sha256"),
            "ruleset_versions": sorted({
                item.get("ruleset_version")
                for item in suite.get("deterministic_rules", [])
                if item.get("ruleset_version")
            }),
        },
        "verification": metrics.get("verification"),
        "signoff": {
            "evidence": _approval_summary(approvals, "evidence_review"),
            "rules": _approval_summary(approvals, "suite_review"),
            "release": _approval_summary(approvals, "release_review"),
        },
        "audit_event_count": len(events),
    }
