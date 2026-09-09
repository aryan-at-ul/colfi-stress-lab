from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


SourceName = Literal["yahoo", "cboe", "ecb", "google_reuters", "official_event"]
EventType = Literal[
    "Bank run",
    "Equity market crisis",
    "FX/currency stress",
    "No material stress",
]
Condition = Literal["control", "stress", "safeguarded"]
ExecutionMode = Literal["llm_decision", "deterministic_stress_rules"]


class PortfolioHoldingSelection(BaseModel):
    asset_id: str = Field(min_length=1, max_length=80)
    weight_pct: float = Field(gt=0.0, le=100.0)


class InstitutionSelection(BaseModel):
    institution_id: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=3, max_length=200)
    holdings: list[PortfolioHoldingSelection] | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        description=(
            "Operator-selected portfolio. Null uses the versioned catalog default."
        ),
    )

    @field_validator("model")
    @classmethod
    def live_model(cls, value: str) -> str:
        value = value.strip()
        if value.startswith(("sim:", "mock:")) or ":" not in value:
            raise ValueError("institution agents require a live provider:model")
        return value

    @model_validator(mode="after")
    def holdings_total_100(self):
        if self.holdings is None:
            return self
        assets = [item.asset_id for item in self.holdings]
        if len(assets) != len(set(assets)):
            raise ValueError("portfolio assets must be unique")
        total = sum(item.weight_pct for item in self.holdings)
        if abs(total - 100.0) > 0.001:
            raise ValueError("portfolio holding weights must total 100%")
        return self


class AssessmentRequest(BaseModel):
    schema_version: Literal[4] = 4
    event_id: str = Field(default="custom", min_length=1, max_length=80)
    expected_taxonomy: EventType | None = None
    start_date: date
    end_date: date
    sources: list[SourceName]
    instruments: list[str]
    news_query: str = Field(min_length=2, max_length=240)
    supervisor_model: str = Field(min_length=3, max_length=200)
    institutions: list[InstitutionSelection] = Field(min_length=1, max_length=7)
    samples_per_agent: int = Field(default=1, ge=1, le=5)
    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=1.5,
        description="Null uses the exact model's provider-default sampling.",
    )
    include_safeguard: bool = True
    safeguard_goal: str = Field(default="", max_length=1000)
    execution_mode: ExecutionMode = "llm_decision"
    use_cached: bool = Field(
        default=False,
        description=(
            "Reuse an exactly matching released assessment for an explicitly "
            "labelled demo replay. False always starts a fresh assessment."
        ),
    )
    created_by: str = Field(min_length=1, max_length=100)

    @field_validator("sources", "instruments")
    @classmethod
    def unique_nonempty(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(v.strip() for v in value if v.strip()))
        if not cleaned:
            raise ValueError("select at least one value")
        return cleaned

    @field_validator("supervisor_model")
    @classmethod
    def supervisor_is_live(cls, value: str) -> str:
        value = value.strip()
        if value.startswith(("sim:", "mock:")) or ":" not in value:
            raise ValueError("the supervisor requires a live provider:model")
        return value

    @model_validator(mode="after")
    def valid_request(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > 31:
            raise ValueError("the observation window cannot exceed 31 days")
        if self.end_date >= date.today():
            raise ValueError("end_date must be before today so daily observations are final")
        ids = [item.institution_id for item in self.institutions]
        if len(ids) != len(set(ids)):
            raise ValueError("each institution may be selected only once")
        return self


class ApprovalRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)
    approved: bool = True
    event_type: EventType | None = None
    event_label: str | None = Field(default=None, max_length=200)
    suite_objective: str | None = Field(default=None, max_length=1200)
    safeguard_instruction: str | None = Field(default=None, max_length=1200)
    max_single_asset_sell_pct: float | None = Field(
        default=None, ge=0.0, le=100.0,
        description="Maximum percentage of a held asset sold per execution round.",
    )
    max_daily_portfolio_sell_pct: float | None = Field(
        default=None, ge=0.0, le=100.0,
        description="Maximum percentage of starting portfolio value sold per session.",
    )
    institution_sell_targets_pct: dict[str, float] | None = None

    @field_validator("institution_sell_targets_pct")
    @classmethod
    def valid_sell_targets(
        cls, value: dict[str, float] | None
    ) -> dict[str, float] | None:
        if value is None:
            return None
        clean = {str(key): float(target) for key, target in value.items()}
        if any(target < 0.0 or target > 100.0 for target in clean.values()):
            raise ValueError("institution sell targets must be between 0% and 100%")
        return clean


class MarketObservation(BaseModel):
    """One dated value retained from a market-data response."""

    session_date: date
    observed_at: str
    value: float
    daily_change_pct: float | None = None
    volume: float | None = Field(default=None, ge=0.0)


class EvidenceItem(BaseModel):
    id: str
    kind: Literal["market", "news", "official_reference"]
    source: str
    title: str
    observed_at: str
    source_url: str
    acquisition_url: str
    fetched_at: str
    raw_sha256: str
    symbol: str | None = None
    value: float | None = None
    previous_value: float | None = None
    change_pct: float | None = None
    window_change_pct: float | None = None
    reference_date: date | None = None
    window_start_date: date | None = None
    window_end_date: date | None = None
    min_daily_change_pct: float | None = None
    max_daily_change_pct: float | None = None
    observations: list[MarketObservation] = Field(default_factory=list)
    unit: str | None = None
    summary: str = ""


class EventClassification(BaseModel):
    event_type: EventType
    event_label: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=1200)
    evidence_ids: list[str]
    evidence_gaps: list[str] = Field(default_factory=list)


class InstitutionStressSignal(BaseModel):
    stress_detected: bool
    severity: int = Field(ge=1, le=5)
    summary: str = Field(min_length=3, max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(
        min_length=1,
        max_length=2,
        description="The one or two evidence items decisive for the stress flag.",
    )
    trigger_ids: list[str] = Field(default_factory=list, max_length=8)


class SuiteCase(BaseModel):
    id: str = Field(pattern=r"^CASE-[A-Z0-9-]+$")
    condition: Condition
    title: str = Field(min_length=2, max_length=160)
    objective: str = Field(min_length=5, max_length=1000)
    agent_instruction: str = Field(min_length=5, max_length=1600)
    evidence_ids: list[str]


RUBRIC_KEYS = (
    "schema_validity",
    "evidence_grounding",
    "portfolio_alignment",
    "constraint_awareness",
    "safeguard_adherence",
)


class SafeguardPolicy(BaseModel):
    instruction: str = Field(min_length=5, max_length=1200)
    max_single_asset_sell_pct: float = Field(
        ge=0.0, le=100.0,
        description="Maximum percentage of a held asset sold in one execution round.",
    )
    max_daily_portfolio_sell_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description=(
            "Maximum aggregate sale as a percentage of starting portfolio value "
            "in one execution session."
        ),
    )
    carry_unexecuted_intent: bool = Field(
        default=True,
        description=(
            "When false, the safeguarded initial sale is limited to the cap and "
            "the blocked remainder is not automatically rescheduled."
        ),
    )
    require_staged_execution: bool
    allow_hedging: bool
    minimum_stages: int = Field(default=2, ge=2, le=10)
    minimum_spacing_sessions: int = Field(default=1, ge=1, le=5)


class SimulationSettings(BaseModel):
    """Versioned, explicit assumptions for the deterministic MVP engine.

    ``market_depth_multiple`` expresses market depth as a multiple of the
    equal-notional test system. It is a scenario assumption, not an estimate of
    real market depth. The price-impact functional form follows the bounded,
    concave specification used in Bank of England Staff Working Paper No. 878.
    """

    model_version: Literal["colfi-mvp-v1"] = "colfi-mvp-v1"
    rounds: int = Field(default=5, ge=3, le=20)
    market_depth_multiple: float = Field(default=10.0, gt=0.0, le=1000.0)
    maximum_asset_impact_pct: float = Field(default=50.0, gt=0.0, le=100.0)
    impact_persistence: float = Field(default=0.60, ge=0.0, le=1.0)
    feedback_sale_sensitivity: float = Field(default=4.0, ge=0.0, le=5.0)
    max_feedback_sale_pct_per_round: float = Field(default=5.0, ge=0.0, le=100.0)
    limit_breach_impact_threshold_pct: float = Field(
        default=0.25,
        ge=0.0,
        le=100.0,
        description=(
            "Outstanding modelled asset impact above this scenario threshold "
            "activates deterministic next-session selling."
        ),
    )


class AssessmentSuite(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    objective: str = Field(min_length=10, max_length=1200)
    hypothesis: str = Field(min_length=10, max_length=1200)
    decision_horizon: str = Field(min_length=2, max_length=120)
    cases: list[SuiteCase] = Field(min_length=2, max_length=3)
    safeguard: SafeguardPolicy
    rubric_weights: dict[str, float]
    pass_threshold: float = Field(ge=0.0, le=1.0)
    limitations: list[str] = Field(min_length=1, max_length=8)
    simulation: SimulationSettings = Field(default_factory=SimulationSettings)

    @field_validator("rubric_weights")
    @classmethod
    def valid_weights(cls, value: dict[str, float]) -> dict[str, float]:
        if set(value) != set(RUBRIC_KEYS):
            raise ValueError(f"rubric_weights must contain exactly {RUBRIC_KEYS}")
        clean = {key: float(value[key]) for key in RUBRIC_KEYS}
        if any(weight < 0 for weight in clean.values()):
            raise ValueError("rubric weights cannot be negative")
        if abs(sum(clean.values()) - 1.0) > 0.001:
            raise ValueError("rubric weights must sum to 1")
        return clean

    @model_validator(mode="after")
    def required_conditions(self):
        conditions = [case.condition for case in self.cases]
        if not {"control", "stress"} <= set(conditions):
            raise ValueError("suite must include control and stress cases")
        if len(conditions) != len(set(conditions)):
            raise ValueError("suite case conditions must be unique")
        return self


AgentToolName = Literal["portfolio_shock", "constraint_register", "evidence_lookup"]


class AgentToolPlan(BaseModel):
    plan_summary: str = Field(min_length=3, max_length=600)
    tool_requests: list[AgentToolName] = Field(min_length=1, max_length=3)

    @field_validator("tool_requests")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value))
        required = {"portfolio_shock", "constraint_register", "evidence_lookup"}
        if set(cleaned) != required:
            raise ValueError(f"tool plan must contain exactly {sorted(required)}")
        return cleaned


class AssetAction(BaseModel):
    asset_id: str = Field(min_length=1, max_length=80)
    action: Literal["hold", "sell", "buy", "hedge"]
    size_pct: float = Field(ge=0.0, le=100.0)
    timing: Literal["immediate", "same_day", "staged", "monitor"]
    rationale: str = Field(min_length=2, max_length=600)

    @model_validator(mode="after")
    def hold_has_zero_size(self):
        if self.action == "hold" and self.size_pct != 0:
            raise ValueError("hold actions must have size_pct=0")
        return self


class InstitutionDecision(BaseModel):
    stance: Literal["risk_off", "risk_on", "hold"]
    executive_decision: str = Field(min_length=3, max_length=600)
    actions: list[AssetAction] = Field(min_length=1, max_length=8)
    urgency: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    constraints_considered: list[str] = Field(min_length=1, max_length=10)
    evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def decision_is_internally_consistent(self):
        active = [item for item in self.actions if item.action != "hold"]
        keys = [(item.asset_id, item.action) for item in active]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "return one intended target per asset/action; execution tranches "
                "are generated deterministically"
            )
        if self.stance == "hold" and active:
            raise ValueError("a hold stance cannot contain active actions")
        if self.stance == "risk_off" and not any(
            item.action in {"sell", "hedge"} for item in active
        ):
            raise ValueError("a risk_off stance requires a sell or hedge action")
        if self.stance == "risk_on" and not any(
            item.action == "buy" for item in active
        ):
            raise ValueError("a risk_on stance requires a buy action")
        if active and any(item.action == "hold" for item in self.actions):
            raise ValueError("do not mix hold with active actions")
        return self


class DraftReport(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    executive_summary: str = Field(min_length=1, max_length=2000)
    findings: list[str]
    institution_findings: list[str]
    limitations: list[str]
    evidence_ids: list[str]
    response_ids: list[str]


WORKFLOW_STEPS = [
    ("configure", "Select event & institution agents"),
    ("collect", "Collect real market evidence"),
    ("evidence_review", "Approve evidence"),
    ("classify", "Supervisor classifies event"),
    ("event_review", "Approve event"),
    ("suite", "Supervisor generates test suite"),
    ("suite_review", "Approve test suite"),
    ("package_cases", "Build isolated institution cases"),
    ("run_control", "Run control agents"),
    ("run_stress", "Run unmitigated stress agents"),
    ("run_safeguarded", "Run safeguarded agents"),
    ("score", "Score agents & convergence"),
    ("synthesise", "Generate assessment report"),
    ("release_review", "Approve release"),
]
