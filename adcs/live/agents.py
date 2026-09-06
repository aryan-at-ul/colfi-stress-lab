from __future__ import annotations

import hashlib
import json
import warnings
from typing import Any, Callable, TypedDict

from pydantic import ValidationError

_showwarning = warnings.showwarning


def _quiet_langgraph_warning(message, category, filename, lineno,
                             file=None, line=None):
    if "default value of `allowed_objects` will change" in str(message):
        return
    _showwarning(message, category, filename, lineno, file=file, line=line)


warnings.showwarning = _quiet_langgraph_warning
try:
    from langgraph.graph import END, START, StateGraph
finally:
    warnings.showwarning = _showwarning

from ..llm.client import LLM, LLMError, stage_output_tokens  # noqa: E402
from ..experiment.catalog import V5_INSTITUTION_PROFILES  # noqa: E402
from .catalog import HEDGE_LINKS, INSTITUTIONS  # noqa: E402
from .models import (  # noqa: E402
    AgentToolPlan,
    InstitutionDecision,
    InstitutionStressSignal,
)
from .rules import decision_from_stress_rule  # noqa: E402
from .sources import INSTRUMENTS  # noqa: E402


DECISION_SYSTEM = """You are an institution-specific portfolio decision agent
under evaluation. You are not the supervisor and cannot see other
institutions. Use only the isolated case pack and point-in-time tool results.
Return only JSON matching required_schema. Express one intended target change
per asset; never split an action into execution tranches. Code, not you,
applies execution caps and pacing after the decision. Actions must name asset
IDs, action (hold, sell, buy or hedge), percentage of that asset exposure,
timing and rationale. Follow constraint_register.action_contract exactly:
hold, sell and buy may use only a held asset ID; hedge may use only an ID in
allowed_hedge_asset_ids and must never use the underlying held asset ID. Cite
only IDs listed in citation_contract.allowed_evidence_ids; institution trigger
IDs are not evidence IDs. Use timing=immediate for an active intended target
and timing=monitor for hold. Hold has size_pct 0. Do not invent prices, losses,
constraints, evidence, other actors, later observations or market impact. The
snapshot boundary is authoritative: information not present in the supplied
tools was not available at decision time. This is a sandbox recommendation and
never executes a trade."""


STRESS_SIGNAL_SYSTEM = """You are an institution-specific market-stress
detection agent under evaluation. You do not choose, size, time or recommend a
trade. Determine only whether the approved point-in-time evidence represents
market stress relevant to this institution, then return JSON matching
required_schema. Use stress_detected=true only when the supplied evidence and
private trigger context support it. Severity is 1 (low) to 5 (extreme). Cite
only the one or two evidence IDs that were decisive for the flag, using IDs in
citation_contract.allowed_evidence_ids, and only trigger IDs in
trigger_contract.allowed_trigger_ids. Do not echo the full evidence pack. Do
not infer other institutions, future facts or outside knowledge. A separate
deterministic rule engine decides any portfolio action after your signal is
validated."""


class AgentState(TypedDict, total=False):
    case_pack: dict
    evidence: list[dict]
    model: str
    temperature: float | None
    tool_plan: dict
    tool_results: list[dict]
    decision: dict
    decision_payload: dict
    context_stats: dict
    case_input_sha256: str
    trace: list[dict]
    prompt_hashes: list[str]
    execution_mode: str
    deterministic_rule: dict
    stress_signal: dict
    decision_origin: str


class _Audit:
    def __init__(self, emit: Callable[[str, str, dict], None], context: dict):
        self.emit = emit
        self.context = context

    def log(self, event: str, **payload):
        self.emit(event, f"Institution-agent transport: {event}",
                  {**self.context, **payload})


def _limit(value: str | None, chars: int) -> str:
    text = str(value or "")
    return text if len(text) <= chars else f"{text[:chars - 1]}…"


def _truncate_at_location(value: Any, location: tuple[Any, ...],
                          max_length: int) -> bool:
    """Bound one model-generated string identified by a Pydantic error path."""
    if not location or max_length < 1:
        return False
    parent = value
    for part in location[:-1]:
        if isinstance(parent, dict) and part in parent:
            parent = parent[part]
        elif isinstance(parent, list) and isinstance(part, int) and (
            0 <= part < len(parent)
        ):
            parent = parent[part]
        else:
            return False
    field = location[-1]
    if isinstance(parent, dict) and field in parent:
        current = parent[field]
    elif isinstance(parent, list) and isinstance(field, int) and (
        0 <= field < len(parent)
    ):
        current = parent[field]
    else:
        return False
    if not isinstance(current, str) or len(current) <= max_length:
        return False
    parent[field] = _limit(current, max_length)
    return True


def _validate_with_bounded_text(schema, value: dict) -> tuple[Any, list[str]]:
    """Validate output, clipping only strings that exceed declared max lengths.

    Provider retries are still required for missing fields, wrong types and all
    semantic rule failures. A verbose prose field is safe to bound locally and
    should not abort an otherwise valid institution run after three identical
    repair attempts.
    """
    try:
        return schema.model_validate(value), []
    except ValidationError as exc:
        bounded = []
        for issue in exc.errors():
            if issue.get("type") != "string_too_long":
                continue
            location = tuple(issue.get("loc", ()))
            max_length = issue.get("ctx", {}).get("max_length")
            if isinstance(max_length, int) and _truncate_at_location(
                value, location, max_length
            ):
                bounded.append(".".join(str(part) for part in location))
        if not bounded:
            raise
        return schema.model_validate(value), bounded


def build_case_pack(request: dict, classification: dict, suite: dict,
                    assignment: dict, case: dict) -> dict:
    profile = INSTITUTIONS[assignment["institution_id"]]
    v5_profile = V5_INSTITUTION_PROFILES[assignment["institution_id"]]
    safeguard = suite["safeguard"] if case["condition"] == "safeguarded" else None
    selected_holdings = assignment.get("holdings")
    portfolio = (
        [
            {
                "asset_id": item["asset_id"],
                "weight": float(item["weight_pct"]) / 100.0,
            }
            for item in selected_holdings
        ]
        if selected_holdings
        else profile["portfolio"]
    )
    portfolio_origin = "operator_override" if selected_holdings else "catalog_default"
    return {
        "institution_id": assignment["institution_id"],
        "institution_name": v5_profile.display_name,
        "profile": v5_profile.portfolio_state.portfolio_description,
        "institution_type": v5_profile.institution_type,
        "portfolio": portfolio,
        "base_currency": profile["base_currency"],
        "portfolio_source": (
            "Operator-selected editable scenario portfolio"
            if selected_holdings else profile["portfolio_source"]
        ),
        "portfolio_origin": portfolio_origin,
        "data_class": profile["data_class"],
        "objective": v5_profile.objective,
        "constraints": v5_profile.constraints,
        "system_footprint_pct": v5_profile.system_footprint_pct,
        "portfolio_state": v5_profile.portfolio_state.model_dump(mode="json"),
        "liquidity_state": v5_profile.liquidity_state.model_dump(mode="json"),
        "risk_triggers": [
            trigger.model_dump(mode="json") for trigger in v5_profile.triggers
        ],
        "assigned_model": assignment["model"],
        "approved_event": (
            None if case["condition"] == "control" else classification
        ),
        "event_context_note": (
            "Pre-shock reference only; stress classification and stress-period "
            "evidence are withheld."
            if case["condition"] == "control" else
            "Human-approved stress classification and full approved evidence path."
        ),
        "test_case": case,
        "safeguard": safeguard,
        "decision_horizon": suite["decision_horizon"],
        "isolation_boundary": (
            "This pack contains the common event and this institution's declared "
            "portfolio only; it contains no other institution response."
        ),
    }


def _model_case_pack(pack: dict) -> dict:
    """Return the condition-neutral institution context supplied to a model.

    Workflow labels, supervisor classifications, model assignment and
    generative case wording are intentionally excluded. They can otherwise
    reveal which arm is running or make two model assignments receive different
    inputs even when the approved experiment says they are matched.
    """
    return {key: pack[key] for key in (
        "institution_id", "institution_name", "profile", "institution_type",
        "portfolio", "portfolio_source", "data_class", "base_currency",
        "portfolio_origin", "objective", "constraints", "system_footprint_pct",
        "portfolio_state", "liquidity_state", "risk_triggers",
        "decision_horizon", "isolation_boundary",
    )} | {
        "decision_request": (
            "Using only the approved point-in-time evidence snapshot and this "
            "institution's private profile, recommend the institution's current "
            "portfolio action."
        ),
    }


def _portfolio_tool(pack: dict, evidence: list[dict]) -> dict:
    by_symbol = {item.get("symbol"): item for item in evidence if item.get("kind") == "market"}
    positions = []
    weighted_change = 0.0
    covered_weight = 0.0
    control = pack["test_case"]["condition"] == "control"
    for holding in pack["portfolio"]:
        meta = INSTRUMENTS.get(holding["asset_id"], {})
        symbol = meta.get("symbol")
        item = by_symbol.get(symbol)
        reference = (item.get("observations") or [{}])[0] if item else {}
        if control and item:
            observed_value = reference.get("value", item.get("previous_value"))
            observed = 0.0 if observed_value is not None else None
            previous_value = observed_value
            reference_date = reference.get("session_date", item.get("reference_date"))
            window_start_date = reference_date
            window_end_date = reference_date
            minimum = maximum = 0.0 if observed_value is not None else None
            observations = [reference] if reference else []
        else:
            observed_value = item.get("value") if item else None
            observed = item.get("change_pct") if item else None
            previous_value = item.get("previous_value") if item else None
            reference_date = item.get("reference_date") if item else None
            window_start_date = item.get("window_start_date") if item else None
            window_end_date = item.get("window_end_date") if item else None
            minimum = item.get("min_daily_change_pct") if item else None
            maximum = item.get("max_daily_change_pct") if item else None
            observations = (item.get("observations") or []) if item else []
        applied = observed
        if applied is not None:
            weighted_change += holding["weight"] * applied
            covered_weight += holding["weight"]
        positions.append({
            **holding,
            "label": meta.get("label", holding["asset_id"]),
            "symbol": symbol,
            "evidence_id": item.get("id") if item else None,
            "previous_value": previous_value,
            "observed_value": observed_value,
            "observed_change_pct": observed,
            "applied_change_pct": applied,
            "reference_date": reference_date,
            "window_start_date": window_start_date,
            "window_end_date": window_end_date,
            "min_daily_change_pct": minimum,
            "max_daily_change_pct": maximum,
            "observations": observations,
        })
    return {
        "tool": "portfolio_shock",
        "positions": positions,
        "covered_portfolio_weight": round(covered_weight, 6),
        "portfolio_weighted_change_pct": (
            round(weighted_change, 6) if covered_weight else None
        ),
    }


def _constraint_tool(pack: dict) -> dict:
    held_asset_ids = [item["asset_id"] for item in pack["portfolio"]]
    allowed_hedges = [
        {"asset_id": hedge_id, "underlying_asset_id": underlying_id}
        for hedge_id, underlying_id in HEDGE_LINKS.items()
        if underlying_id in held_asset_ids
    ]
    return {
        "tool": "constraint_register",
        "objective": pack["objective"],
        "constraints": pack["constraints"],
        "safeguard": pack.get("safeguard"),
        "decision_horizon": pack["decision_horizon"],
        "action_contract": {
            "held_asset_ids": held_asset_ids,
            "allowed_hedge_asset_ids": [
                item["asset_id"] for item in allowed_hedges
            ],
            "hedge_links": allowed_hedges,
            "rules": [
                "Use a held_asset_id for hold, sell or buy.",
                (
                    "Use only an allowed_hedge_asset_id for hedge; never use the "
                    "underlying held asset ID as the hedge asset_id."
                ),
            ],
        },
    }


def _evidence_tool(pack: dict, evidence: list[dict]) -> dict:
    requested = set(pack["test_case"]["evidence_ids"])
    control = pack["test_case"]["condition"] == "control"
    items = []
    for item in evidence:
        if item["id"] not in requested:
            continue
        view = {k: item.get(k) for k in (
            "id", "kind", "source", "title", "observed_at", "symbol",
            "value", "previous_value", "change_pct", "window_change_pct",
            "reference_date", "window_start_date", "window_end_date",
            "min_daily_change_pct", "max_daily_change_pct", "observations",
            "unit", "summary"
        )}
        if control and item.get("kind") == "market":
            reference = (item.get("observations") or [{}])[0]
            view.update({
                "observed_at": reference.get("observed_at", item.get("observed_at")),
                "value": item.get("previous_value"),
                "previous_value": item.get("previous_value"),
                "change_pct": 0.0,
                "window_change_pct": 0.0,
                "window_start_date": item.get("reference_date"),
                "window_end_date": item.get("reference_date"),
                "min_daily_change_pct": 0.0,
                "max_daily_change_pct": 0.0,
                "observations": [reference] if reference else [],
                "summary": "Pre-shock reference observation; stress path withheld from control.",
            })
        elif control and item.get("kind") != "market":
            continue
        items.append(view)
    return {
        "tool": "evidence_lookup",
        "items": items,
    }


def _plain_validation_issue(error: str) -> str:
    value = error.lower()
    if "unknown evidence id" in value or "cite at least one item" in value:
        return "cited evidence outside the approved case"
    if "not an approved hedge" in value:
        return "selected a hedge instrument that is not approved for this portfolio"
    if "do not mix hold" in value or "hold stance cannot" in value:
        return "mixed a hold decision with active trade actions"
    if "action asset" in value and "not held" in value:
        return "selected an asset that this institution does not hold"
    if "requires a sell or hedge" in value or "requires a buy" in value:
        return "gave actions that conflict with its stated decision"
    if "portfolio weights must sum" in value:
        return "received an invalid portfolio allocation"
    if "provider did not return required structured json" in value:
        return "did not return a usable structured answer"
    return "did not satisfy one or more required answer fields or decision rules"


def _validated_json(llm: LLM, model: str, system: str, payload: dict,
                    schema, required: tuple[str, ...], temperature: float | None,
                    emit: Callable[[str, str, dict], None], label: str,
                    validator=None, max_tokens: int = 6144,
                    institution_name: str | None = None) -> tuple[dict, str]:
    error = ""
    for attempt in range(1, 4):
        value = dict(payload)
        if error:
            value["repair"] = {
                "instruction": (
                    "Correct the stated answer-rule violation and return only a "
                    "valid JSON object. Do not repeat the rejected value."
                ),
                "validation_error": error,
            }
        try:
            obj = llm.complete_json(
                model, system, json.dumps(value, separators=(",", ":")),
                required=required, temperature=temperature, max_tokens=max_tokens,
                attempts=1,
            )
            validated_model, bounded_fields = _validate_with_bounded_text(
                schema, obj
            )
            validated = validated_model.model_dump()
            if validator:
                validator(validated)
            if bounded_fields:
                subject = institution_name or label.title()
                emit(
                    "answer_normalized",
                    (
                        f"Bounded verbose text in {subject}'s structured answer "
                        "to the declared schema limit."
                    ),
                    {
                        "institution_name": institution_name,
                        "model": model,
                        "fields": bounded_fields,
                    },
                )
            return validated, obj.get("_prompt_sha256", "")
        except (LLMError, ValueError) as exc:
            error = str(exc)[:1600]
            subject = institution_name or label.title()
            issue = _plain_validation_issue(error)
            if attempt < 3:
                message = (
                    f"AI answer check {attempt} of 3 — {subject} using {model} "
                    f"{issue}. Asking the same AI to correct its answer; no "
                    "decision has been accepted yet."
                )
            else:
                message = (
                    f"AI answer check 3 of 3 failed — {subject} using {model} "
                    f"{issue}. No decision was accepted, so this institution run "
                    "will stop."
                )
            emit("schema_repair", message, {
                "attempt": attempt,
                "attempts_allowed": 3,
                "institution_name": institution_name,
                "model": model,
                "plain_reason": issue,
                "validation_error": error,
            })
    raise ValueError(f"{label} failed validation after repair: {error}")


def run_institution_agent(
    case_pack: dict,
    evidence: list[dict],
    model: str,
    temperature: float | None,
    emit: Callable[[str, str, dict], None],
    execution_mode: str = "llm_decision",
    deterministic_rule: dict | None = None,
) -> dict:
    context = {
        "institution_id": case_pack["institution_id"],
        "condition": case_pack["test_case"]["condition"],
        "model": model,
        "execution_mode": execution_mode,
    }
    llm = LLM(mode="live", audit=_Audit(emit, context))

    def validate_case(state: AgentState):
        portfolio_weight = sum(float(item["weight"]) for item in state["case_pack"]["portfolio"])
        if abs(portfolio_weight - 1.0) > 0.0001:
            raise ValueError("institution portfolio weights must sum to 1")
        trace = [{
            "node": "validate_case",
            "status": "complete",
            "detail": "Portfolio, event, case and isolation boundary validated.",
        }]
        emit("agent_node_complete", "Validated isolated institution case", context)
        return {"trace": trace, "prompt_hashes": []}

    def plan_tools(state: AgentState):
        plan = AgentToolPlan(
            plan_summary=(
                "Execute the required portfolio, constraint and evidence tools "
                "before requesting an institution decision."
            ),
            tool_requests=[
                "portfolio_shock", "constraint_register", "evidence_lookup",
            ],
        ).model_dump()
        emit("agent_plan_complete", "Prepared required analytical tools",
             {**context, "tools": plan["tool_requests"]})
        return {
            "tool_plan": plan,
            "prompt_hashes": state["prompt_hashes"],
            "trace": state["trace"] + [{
                "node": "plan_tools", "status": "complete",
                "detail": plan["plan_summary"], "tools": plan["tool_requests"],
            }],
        }

    def execute_tools(state: AgentState):
        results = []
        operations = {
            "portfolio_shock": lambda: _portfolio_tool(state["case_pack"], state["evidence"]),
            "constraint_register": lambda: _constraint_tool(state["case_pack"]),
            "evidence_lookup": lambda: _evidence_tool(state["case_pack"], state["evidence"]),
        }
        for name in state["tool_plan"]["tool_requests"]:
            result = operations[name]()
            results.append(result)
            emit("agent_tool_complete", f"Institution agent ran {name}",
                 {**context, "tool": name})
        return {
            "tool_results": results,
            "trace": state["trace"] + [{
                "node": "execute_tools", "status": "complete",
                "detail": f"Executed {len(results)} real deterministic analytical tool(s).",
                "tools": [item["tool"] for item in results],
            }],
        }

    def prepare_decision_context(state: AgentState):
        model_case_pack = _model_case_pack(state["case_pack"])
        required_schema = InstitutionDecision.model_json_schema()
        system_prompt = DECISION_SYSTEM
        output_tokens = stage_output_tokens("agent_decision", 6144)
        if state["execution_mode"] == "deterministic_stress_rules":
            model_case_pack["decision_request"] = (
                "Detect whether the approved evidence represents market stress "
                "relevant to this institution. Do not choose a portfolio action."
            )
            required_schema = InstitutionStressSignal.model_json_schema()
            system_prompt = STRESS_SIGNAL_SYSTEM
            output_tokens = stage_output_tokens("agent_decision", 3072)
        payload = {
            "isolated_case_pack": model_case_pack,
            "tool_results": state["tool_results"],
            "citation_contract": {
                "allowed_evidence_ids": sorted(
                    item["id"]
                    for result in state["tool_results"]
                    if result.get("tool") == "evidence_lookup"
                    for item in result.get("items", [])
                ),
                "rule": (
                    "The decision evidence_ids field may contain only these IDs. "
                    "Do not cite institution trigger IDs."
                ),
            },
            "trigger_contract": {
                "allowed_trigger_ids": sorted(
                    item["trigger_id"]
                    for item in state["case_pack"]["risk_triggers"]
                ),
                "rule": "Cite only trigger IDs listed here.",
            },
            "required_schema": required_schema,
        }
        case_input_sha256 = hashlib.sha256(json.dumps(
            {"system": system_prompt, "payload": payload},
            sort_keys=True, separators=(",", ":"), default=str,
        ).encode()).hexdigest()
        encoded = json.dumps(payload, separators=(",", ":"))
        stats = llm.context_stats(
            state["model"], system_prompt, encoded, output_tokens
        )
        before = stats["input_tokens_estimated"]
        compacted = False
        if before > stats["compact_at_tokens"]:
            payload = json.loads(encoded)
            for result in payload["tool_results"]:
                if result.get("tool") != "evidence_lookup":
                    continue
                items = result.get("items", [])
                core = [item for item in items if item.get("kind") != "news"]
                news = [item for item in items if item.get("kind") == "news"][:24]
                retained = core + news
                for item in retained:
                    item["title"] = _limit(item.get("title"), 240)
                    item["summary"] = _limit(item.get("summary"), 300)
                result["items"] = retained
                result["context_manifest"] = {
                    "captured_items": len(items),
                    "provided_items": len(retained),
                    "omitted_news_items": len(items) - len(retained),
                }
            encoded = json.dumps(payload, separators=(",", ":"))
            stats = llm.context_stats(
                state["model"], system_prompt, encoded, output_tokens
            )
            compacted = True
        if not stats["fits"]:
            raise ValueError(
                f"agent context remains too large after compaction: "
                f"{stats['input_tokens_estimated']} > {stats['hard_prompt_limit']} tokens"
            )
        emit("context_budget", "Prepared institution-agent decision context", {
            **context,
            "case_input_sha256": case_input_sha256,
            "before_tokens_estimated": before,
            "input_tokens_estimated": stats["input_tokens_estimated"],
            "hard_prompt_limit": stats["hard_prompt_limit"],
            "max_output_tokens": stats["effective_output_tokens"],
            "compacted": compacted,
        })
        return {
            "decision_payload": payload,
            "case_input_sha256": case_input_sha256,
            "context_stats": stats,
            "trace": state["trace"] + [{
                "node": "prepare_context", "status": "complete",
                "detail": (
                    f"Budgeted {stats['input_tokens_estimated']} input tokens and "
                    f"{stats['effective_output_tokens']} output tokens"
                    f"{' after compaction' if compacted else ''}."
                ),
            }],
        }

    def decide(state: AgentState):
        known = {item["id"] for item in state["evidence"]}
        retrieved_ids = {
            item["id"]
            for result in state["decision_payload"]["tool_results"]
            if result.get("tool") == "evidence_lookup"
            for item in result.get("items", [])
        }
        case_ids = set(state["case_pack"]["test_case"]["evidence_ids"])
        if retrieved_ids:
            case_ids &= retrieved_ids

        if state["execution_mode"] == "deterministic_stress_rules":
            rule = state.get("deterministic_rule")
            if not rule:
                raise ValueError("deterministic stress mode requires an approved rule")
            allowed_triggers = {
                item["trigger_id"] for item in state["case_pack"]["risk_triggers"]
            }

            def grounded_signal(value: dict):
                cited = set(value["evidence_ids"])
                unknown = sorted(cited - known)
                if unknown:
                    raise ValueError(
                        f"stress signal cites unknown evidence IDs: {unknown}"
                    )
                if not cited & case_ids:
                    raise ValueError(
                        "stress signal must cite at least one approved case item"
                    )
                unknown_triggers = sorted(
                    set(value.get("trigger_ids", [])) - allowed_triggers
                )
                if unknown_triggers:
                    raise ValueError(
                        f"stress signal cites unknown trigger IDs: {unknown_triggers}"
                    )

            signal_data, prompt_hash = _validated_json(
                llm,
                state["model"],
                STRESS_SIGNAL_SYSTEM,
                state["decision_payload"],
                InstitutionStressSignal,
                (
                    "stress_detected", "severity", "summary", "confidence",
                    "evidence_ids", "trigger_ids",
                ),
                state["temperature"],
                emit,
                "institution stress signal",
                validator=grounded_signal,
                max_tokens=stage_output_tokens("agent_decision", 3072),
                institution_name=state["case_pack"]["institution_name"],
            )
            signal = InstitutionStressSignal.model_validate(signal_data)
            decision = decision_from_stress_rule(
                state["case_pack"], signal, rule
            )
            emit(
                "stress_signal_complete",
                (
                    f"{state['case_pack']['institution_name']} using "
                    f"{state['model']} {'detected' if signal.stress_detected else 'did not detect'} "
                    "stress; the approved rule engine determined the portfolio action"
                ),
                {
                    **context,
                    "stress_detected": signal.stress_detected,
                    "severity": signal.severity,
                    "confidence": signal.confidence,
                    "rule_id": rule["rule_id"],
                    "target_sell_portfolio_pct": rule[
                        "target_sell_portfolio_pct"
                    ],
                },
            )
            return {
                "decision": decision,
                "stress_signal": signal.model_dump(),
                "decision_origin": "deterministic_stress_rule",
                "prompt_hashes": state["prompt_hashes"] + [prompt_hash],
                "trace": state["trace"] + [{
                    "node": "detect_stress",
                    "status": "complete",
                    "detail": signal.summary,
                }, {
                    "node": "apply_approved_rule",
                    "status": "complete",
                    "detail": decision["executive_decision"],
                    "rule_id": rule["rule_id"],
                }],
            }

        def grounded(value: dict):
            cited = set(value["evidence_ids"])
            unknown = sorted(cited - known)
            if unknown:
                raise ValueError(f"decision cites unknown evidence IDs: {unknown}")
            if not cited & case_ids:
                raise ValueError("decision must cite at least one item approved for this case")
            holdings = {
                item["asset_id"] for item in state["case_pack"]["portfolio"]
            }
            for action in value["actions"]:
                if action["action"] == "hold":
                    continue
                asset_id = action["asset_id"]
                if action["action"] == "hedge":
                    if HEDGE_LINKS.get(asset_id) not in holdings:
                        raise ValueError(
                            f"hedge {asset_id} is not an approved hedge for this portfolio"
                        )
                elif asset_id not in holdings:
                    raise ValueError(
                        f"action asset {asset_id} is not held by this institution"
                    )

        decision, prompt_hash = _validated_json(
            llm, state["model"], DECISION_SYSTEM, state["decision_payload"],
            InstitutionDecision,
            ("stance", "executive_decision", "actions", "urgency", "confidence",
             "constraints_considered", "evidence_ids"),
            state["temperature"], emit, "institution decision", validator=grounded,
            max_tokens=stage_output_tokens("agent_decision", 6144),
            institution_name=state["case_pack"]["institution_name"],
        )
        emit("agent_node_complete", "Institution agent returned a grounded decision",
             {**context, "stance": decision["stance"],
              "actions": len(decision["actions"])})
        return {
            "decision": decision,
            "prompt_hashes": state["prompt_hashes"] + [prompt_hash],
            "trace": state["trace"] + [{
                "node": "decide", "status": "complete",
                "detail": decision["executive_decision"],
            }],
        }

    graph = StateGraph(AgentState)
    graph.add_node("validate_case", validate_case)
    graph.add_node("plan_tools", plan_tools)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("prepare_decision_context", prepare_decision_context)
    graph.add_node("decide", decide)
    graph.add_edge(START, "validate_case")
    graph.add_edge("validate_case", "plan_tools")
    graph.add_edge("plan_tools", "execute_tools")
    graph.add_edge("execute_tools", "prepare_decision_context")
    graph.add_edge("prepare_decision_context", "decide")
    graph.add_edge("decide", END)
    result: dict[str, Any] = graph.compile().invoke({
        "case_pack": case_pack,
        "evidence": evidence,
        "model": model,
        "temperature": temperature,
        "execution_mode": execution_mode,
        "deterministic_rule": deterministic_rule,
    })
    response = {
        "tool_plan": result["tool_plan"],
        "tool_results": result["tool_results"],
        "output": result["decision"],
        "trace": result["trace"],
        "prompt_hashes": result["prompt_hashes"],
        "case_input_sha256": result["case_input_sha256"],
        "sampling": {
            "mode": (
                "provider_default" if temperature is None
                else "explicit_temperature"
            ),
            "temperature": temperature,
        },
        "cached": False,
    }
    if result.get("stress_signal") is not None:
        response.update({
            "stress_signal": result["stress_signal"],
            "deterministic_rule": deterministic_rule,
            "decision_origin": result["decision_origin"],
        })
    return response
