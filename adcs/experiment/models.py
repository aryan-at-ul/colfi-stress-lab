"""Typed, hashable contracts for the COLFI v5 experiment."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def canonical_json(value: BaseModel | dict | list) -> str:
    """Return the one canonical representation used by v5 content hashes."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_sha256(value: BaseModel | dict | list) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class SourceLocator(BaseModel):
    artifact: str = Field(min_length=1, max_length=240)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    worksheet: str = Field(min_length=1, max_length=120)
    row: int = Field(ge=1)
    data_class: Literal["synthetic_scenario_input"] = "synthetic_scenario_input"


class ExposureDescriptor(BaseModel):
    """A source-supplied exposure that still needs an approved proxy mapping."""

    exposure_id: str = Field(pattern=r"^EXP-[A-Z0-9-]+$")
    description: str = Field(min_length=3, max_length=500)
    asset_classes: list[str] = Field(min_length=1, max_length=8)
    mapping_status: Literal["unmapped"] = "unmapped"


class HoldingPosition(BaseModel):
    """An approved tradable-proxy position for the future holdings matrix."""

    position_id: str = Field(pattern=r"^POS-[A-Z0-9-]+$")
    instrument_id: str = Field(min_length=1, max_length=80)
    side: Literal["long", "short"]
    notional_pct: float = Field(gt=0.0, le=1000.0)
    size_basis: Literal["footprint_notional_pct"] = "footprint_notional_pct"
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    mapping_status: Literal["approved"] = "approved"
    source_note: str = Field(min_length=3, max_length=500)


HoldingRecord = Annotated[
    ExposureDescriptor | HoldingPosition,
    Field(discriminator="mapping_status"),
]


class PortfolioRiskState(BaseModel):
    portfolio_description: str = Field(min_length=3, max_length=800)
    current_loss_pct: float = Field(ge=-100.0, le=100.0)
    current_loss_basis: Literal["portfolio_notional", "allocated_risk_capital"]
    net_equity_exposure_pct: float | None = Field(default=None, ge=-1000.0, le=1000.0)
    gross_exposure_pct: float | None = Field(default=None, ge=0.0, le=2000.0)
    long_only_global_equity_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    equity_risk_share_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    equity_risk_target_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    client_sell_flow_multiple: float | None = Field(default=None, ge=0.0, le=100.0)


class LiquidityState(BaseModel):
    cash_buffer_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    margin_headroom_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    capital_headroom_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    redemption_requests_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    market_depth_pct_of_normal: float | None = Field(default=None, ge=0.0, le=500.0)


class RiskTrigger(BaseModel):
    trigger_id: str = Field(pattern=r"^TRG-[A-Z0-9-]+$")
    metric: str = Field(min_length=2, max_length=120)
    observed_value: float | None = None
    unit: str = Field(min_length=1, max_length=80)
    limit_value: float | None = None
    status: Literal["within_limit", "approaching_limit", "binding_obligation"]
    description: str = Field(min_length=3, max_length=500)


V5ActionType = Literal[
    "sell",
    "hedge",
    "deleverage",
    "withdraw_liquidity",
    "buy_support",
]


class InstitutionProfileV5(BaseModel):
    schema_version: Literal[5] = 5
    profile_version_id: str = Field(pattern=r"^IPV5-[A-Z0-9-]+$")
    catalog_version: str = Field(min_length=3, max_length=80)
    institution_id: str = Field(pattern=r"^INST-[0-9]{2}$")
    display_name: str = Field(min_length=3, max_length=120)
    institution_type: str = Field(min_length=3, max_length=200)
    objective: str = Field(min_length=5, max_length=800)
    constraints: list[str] = Field(min_length=1, max_length=12)
    system_footprint_pct: float = Field(gt=0.0, le=100.0)
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    portfolio_state: PortfolioRiskState
    liquidity_state: LiquidityState
    triggers: list[RiskTrigger] = Field(min_length=1, max_length=8)
    permitted_actions: list[V5ActionType] = Field(min_length=1, max_length=5)
    holdings_status: Literal["unmapped", "approved"]
    holdings: list[HoldingRecord] = Field(min_length=1, max_length=50)
    sources: list[SourceLocator] = Field(min_length=1, max_length=8)

    @field_validator("constraints", "permitted_actions")
    @classmethod
    def unique_values(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return value

    @model_validator(mode="after")
    def holdings_are_consistent(self):
        statuses = {item.mapping_status for item in self.holdings}
        if self.holdings_status == "approved" and statuses != {"approved"}:
            raise ValueError("approved holdings cannot contain unmapped exposures")
        if self.holdings_status == "unmapped" and "unmapped" not in statuses:
            raise ValueError("unmapped profiles must identify the unresolved exposure")
        approved = [
            item for item in self.holdings if isinstance(item, HoldingPosition)
        ]
        instruments = [item.instrument_id for item in approved]
        if len(instruments) != len(set(instruments)):
            raise ValueError("approved holdings must be unique by instrument")
        if self.holdings_status == "approved" and abs(
            sum(item.notional_pct for item in approved) - 100.0
        ) > 1e-9:
            raise ValueError("approved holdings must total 100%")
        return self

    def private_model_payload(self) -> dict:
        """Return only the private block intended for this institution's model."""

        return self.model_dump(
            mode="json",
            exclude={
                "catalog_version",
                "holdings_status",
                "sources",
            },
        )


class ActionBase(BaseModel):
    action_id: str = Field(pattern=r"^ACT-[A-Z0-9-]+$")
    urgency: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=3, max_length=800)
    evidence_ids: list[str] = Field(min_length=1, max_length=20)


class SellAction(ActionBase):
    action_type: Literal["sell"] = "sell"
    instrument_id: str = Field(min_length=1, max_length=80)
    direction: Literal["reduce_long", "increase_short"] = "reduce_long"
    size_value: float = Field(ge=0.0, le=100.0)
    size_basis: Literal["footprint_notional_pct"] = "footprint_notional_pct"


class HedgeAction(ActionBase):
    action_type: Literal["hedge"] = "hedge"
    hedge_instrument_id: str = Field(min_length=1, max_length=80)
    order_side: Literal["buy", "sell"]
    protects_instrument_ids: list[str] = Field(min_length=1, max_length=20)
    size_value: float = Field(ge=0.0, le=100.0)
    size_basis: Literal["footprint_notional_pct"] = "footprint_notional_pct"


class DeleverageAction(ActionBase):
    action_type: Literal["deleverage"] = "deleverage"
    size_value: float = Field(ge=0.0, le=100.0)
    size_basis: Literal["gross_exposure_reduction_pct"] = (
        "gross_exposure_reduction_pct"
    )
    liquidation_priority: Literal["pro_rata", "waterfall"]
    preferred_instrument_ids: list[str] = Field(default_factory=list, max_length=20)


class WithdrawLiquidityAction(ActionBase):
    action_type: Literal["withdraw_liquidity"] = "withdraw_liquidity"
    market_instrument_id: str = Field(min_length=1, max_length=80)
    size_value: float = Field(ge=0.0, le=100.0)
    size_basis: Literal["provided_depth_pct"] = "provided_depth_pct"


class BuySupportAction(ActionBase):
    action_type: Literal["buy_support"] = "buy_support"
    instrument_id: str = Field(min_length=1, max_length=80)
    support_kind: Literal["investment", "liquidity_support"]
    size_value: float = Field(ge=0.0, le=100.0)
    size_basis: Literal["footprint_notional_pct"] = "footprint_notional_pct"


InstitutionAction = Annotated[
    SellAction
    | HedgeAction
    | DeleverageAction
    | WithdrawLiquidityAction
    | BuySupportAction,
    Field(discriminator="action_type"),
]


class InstitutionDecisionV5(BaseModel):
    schema_version: Literal[5] = 5
    decision_id: str = Field(pattern=r"^DEC-[A-Z0-9-]+$")
    institution_id: str = Field(pattern=r"^INST-[0-9]{2}$")
    stage: Literal["initial", "feedback"]
    stance: Literal["risk_reduce", "risk_add", "mixed", "hold"]
    executive_decision: str = Field(min_length=3, max_length=800)
    actions: list[InstitutionAction] = Field(default_factory=list, max_length=30)
    constraints_considered: list[str] = Field(min_length=1, max_length=20)
    evidence_ids: list[str] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def internally_consistent(self):
        if self.stance == "hold" and self.actions:
            raise ValueError("hold stance must not contain actions")
        if self.stance != "hold" and not self.actions:
            raise ValueError("an active stance requires at least one action")
        action_types = {item.action_type for item in self.actions}
        stress_adding = action_types & {
            "sell", "hedge", "deleverage", "withdraw_liquidity"
        }
        support = "buy_support" in action_types
        if self.stance == "risk_reduce" and (not stress_adding or support):
            raise ValueError(
                "risk_reduce requires a stress-reducing action and no buy/support action"
            )
        if self.stance == "risk_add" and (not support or stress_adding):
            raise ValueError(
                "risk_add requires buy/support and no risk-reduction action"
            )
        if self.stance == "mixed" and (not stress_adding or not support):
            raise ValueError("mixed stance requires both risk reduction and buy/support")
        ids = [item.action_id for item in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("action_id values must be unique within a decision")
        targets: list[tuple[str, str]] = []
        for action in self.actions:
            target = getattr(
                action,
                "instrument_id",
                getattr(action, "hedge_instrument_id", None),
            )
            target = target or getattr(action, "market_instrument_id", "portfolio")
            key = (action.action_type, target)
            if key in targets:
                raise ValueError("duplicate action type/target within one decision")
            targets.append(key)
        return self


class ProviderDecisionV5(BaseModel):
    """Decision body supplied by a provider; trusted run metadata is server-set."""

    schema_version: Literal[5] = 5
    stance: Literal["risk_reduce", "risk_add", "mixed", "hold"]
    executive_decision: str = Field(min_length=3, max_length=800)
    actions: list[InstitutionAction] = Field(default_factory=list, max_length=30)
    constraints_considered: list[str] = Field(min_length=1, max_length=20)
    evidence_ids: list[str] = Field(min_length=1, max_length=40)

    def attach_run_metadata(
        self, *, decision_id: str, institution_id: str, stage: str
    ) -> InstitutionDecisionV5:
        return InstitutionDecisionV5(
            decision_id=decision_id,
            institution_id=institution_id,
            stage=stage,
            **self.model_dump(mode="python"),
        )


class ModelRegistration(BaseModel):
    model_id: str = Field(min_length=3, max_length=200)
    provider: str = Field(min_length=2, max_length=80)
    exact_model: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def provider_matches_id(self):
        if self.model_id != f"{self.provider}:{self.exact_model}":
            raise ValueError("model_id must be provider:exact_model")
        return self


class CohortAssignment(BaseModel):
    cohort_id: str = Field(pattern=r"^HET-[0-9]{2}$")
    assignments: dict[str, str] = Field(min_length=1)


class DecisionClock(BaseModel):
    initial_cutoff: datetime
    feedback_at: datetime
    second_transition_at: datetime
    feedback_kind: Literal["engine_counterfactual"] = "engine_counterfactual"

    @model_validator(mode="after")
    def aware_and_ordered(self):
        if self.initial_cutoff.tzinfo is None or self.feedback_at.tzinfo is None:
            raise ValueError("decision timestamps must be timezone-aware")
        if self.feedback_at <= self.initial_cutoff:
            raise ValueError("feedback_at must be after initial_cutoff")
        if self.second_transition_at <= self.feedback_at:
            raise ValueError("second_transition_at must be after feedback_at")
        return self


class SamplingPolicy(BaseModel):
    primary: Literal["provider_default"] = "provider_default"
    explicit_temperature: float | None = None
    sensitivity_temperature: float = Field(default=0.2, ge=0.0, le=1.5)
    sensitivity_scope: Literal["supported_models_only"] = "supported_models_only"
    sensitivity_in_primary_estimator: Literal[False] = False


class InferencePolicy(BaseModel):
    permutation_method: Literal[
        "within_institution_repetition_model_label_shuffle"
    ] = "within_institution_repetition_model_label_shuffle"
    permutations: Literal[1000] = 1000
    interval_method: Literal[
        "whole_repetition_percentile_bootstrap"
    ] = "whole_repetition_percentile_bootstrap"
    bootstrap_samples: Literal[5000] = 5000
    confidence_level: Literal[0.95] = 0.95


class InstrumentImpactAssumption(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=80)
    instrument_kind: Literal["asset", "fx"]
    valuation_currency: str = Field(pattern=r"^[A-Z]{3}$")
    fx_base_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    fx_quote_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    reference_price_index: Literal[100.0] = 100.0
    daily_adv_system_notional_pct: float = Field(gt=0.0, le=10000.0)
    round_depth_system_notional_pct: float = Field(gt=0.0, le=1000.0)
    square_root_impact_coefficient_pct: float = Field(gt=0.0, le=100.0)
    assumption_class: Literal["exercise_assumption"] = "exercise_assumption"
    source_note: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def fx_convention_is_explicit(self):
        has_pair = self.fx_base_currency is not None and self.fx_quote_currency is not None
        if self.instrument_kind == "fx" and not has_pair:
            raise ValueError("FX assumptions require base and quote currencies")
        if self.instrument_kind == "asset" and (
            self.fx_base_currency is not None or self.fx_quote_currency is not None
        ):
            raise ValueError("asset assumptions cannot declare an FX pair")
        return self


class LiquidityProvisionAssumption(BaseModel):
    institution_profile_version_id: str = Field(pattern=r"^IPV5-[A-Z0-9-]+$")
    instrument_id: str = Field(min_length=1, max_length=80)
    provided_depth_share_pct: float = Field(gt=0.0, le=100.0)
    assumption_class: Literal["exercise_assumption"] = "exercise_assumption"


class TransitionPolicyV5(BaseModel):
    policy_version: Literal["colfi-transition-v5.1"] = "colfi-transition-v5.1"
    flow_basis: Literal["system_notional_pct"] = "system_notional_pct"
    impact_function: Literal["square_root_adv"] = "square_root_adv"
    deleverage_rule: Literal["directed_first_residual_pro_rata"] = (
        "directed_first_residual_pro_rata"
    )
    position_floor: Literal["long_only_zero"] = "long_only_zero"
    max_abs_price_impact_pct: float = Field(default=20.0, gt=0.0, le=100.0)
    feedback_decision_rounds: Literal[1] = 1
    instrument_assumptions: list[InstrumentImpactAssumption] = Field(min_length=1)
    liquidity_provision: list[LiquidityProvisionAssumption] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def unique_and_bounded(self):
        instrument_ids = [item.instrument_id for item in self.instrument_assumptions]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise ValueError("transition instrument assumptions must be unique")
        allocations: dict[str, float] = {}
        keys = set()
        for item in self.liquidity_provision:
            key = (item.institution_profile_version_id, item.instrument_id)
            if key in keys:
                raise ValueError("liquidity-provision allocations must be unique")
            keys.add(key)
            allocations[item.instrument_id] = (
                allocations.get(item.instrument_id, 0.0)
                + item.provided_depth_share_pct
            )
        if any(value > 100.0 + 1e-9 for value in allocations.values()):
            raise ValueError("liquidity-provider shares cannot exceed 100% per instrument")
        if not set(allocations) <= set(instrument_ids):
            raise ValueError("liquidity provision references an unknown instrument")
        return self

    @property
    def sha256(self) -> str:
        return content_sha256(self)


class FrozenEvidenceItem(BaseModel):
    evidence_id: str = Field(pattern=r"^EVD-[A-Z0-9-]+$")
    evidence_class: Literal["public_fact", "exercise_assumption"]
    title: str = Field(min_length=3, max_length=240)
    instrument_id: str | None = Field(default=None, max_length=80)
    value: float | str
    unit: str = Field(min_length=1, max_length=80)
    observed_at: datetime
    available_at: datetime
    source_url: str = Field(min_length=5, max_length=2000)
    acquisition_url: str = Field(min_length=5, max_length=2000)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def timestamps_are_aware(self):
        if self.observed_at.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        return self


class EvidenceSnapshotV5(BaseModel):
    schema_version: Literal[5] = 5
    snapshot_id: str = Field(pattern=r"^EVIDENCE-[A-Z0-9-]+$")
    event_id: Literal["august_2024_turmoil"] = "august_2024_turmoil"
    decision_cutoff: datetime
    created_by: str = Field(min_length=1, max_length=100)
    source_assessment_id: str | None = Field(default=None, max_length=80)
    items: list[FrozenEvidenceItem] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def point_in_time_snapshot(self):
        if self.decision_cutoff.tzinfo is None:
            raise ValueError("decision_cutoff must be timezone-aware")
        ids = [item.evidence_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence ids must be unique")
        late = [
            item.evidence_id
            for item in self.items
            if item.available_at > self.decision_cutoff
        ]
        if late:
            raise ValueError(
                "post-cutoff evidence is not allowed in an initial snapshot: "
                + ", ".join(late)
            )
        return self

    @property
    def sha256(self) -> str:
        return content_sha256(self)


class ExperimentPreregistration(BaseModel):
    schema_version: Literal[5] = 5
    preregistration_id: str = Field(pattern=r"^PREREG-[A-Z0-9-]+$")
    title: str = Field(min_length=5, max_length=240)
    event_id: Literal["august_2024_turmoil"] = "august_2024_turmoil"
    research_questions: list[Literal["UC-01", "UC-02"]] = Field(min_length=2)
    instrument_ids: list[str] = Field(min_length=1)
    institution_profile_version_ids: list[str] = Field(min_length=1)
    models: list[ModelRegistration] = Field(min_length=2)
    repetitions: Literal[5] = 5
    decision_clock: DecisionClock
    sampling: SamplingPolicy = Field(default_factory=SamplingPolicy)
    inference: InferencePolicy = Field(default_factory=InferencePolicy)
    transition_policy: TransitionPolicyV5
    randomization_method: Literal["sha256_rank_v1"] = "sha256_rank_v1"
    randomization_seed: str = Field(min_length=8, max_length=200)
    heterogeneous_cohorts: list[CohortAssignment] = Field(min_length=2)
    pairwise_primary: Literal[True] = True
    action_schema_version: Literal["colfi-actions-v5.1"] = "colfi-actions-v5.1"
    profile_catalog_version: str = Field(min_length=3, max_length=80)
    stage_transition_version: Literal["colfi-transition-v5.1"] = (
        "colfi-transition-v5.1"
    )
    metric_ids: list[str] = Field(min_length=1)
    release_gate_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def balanced_design(self):
        profile_ids = self.institution_profile_version_ids
        model_ids = [item.model_id for item in self.models]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("institution profile versions must be unique")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("models must be unique")
        if len(self.heterogeneous_cohorts) != len(model_ids):
            raise ValueError("one cyclic heterogeneous cohort is required per model")
        expected_profiles = set(profile_ids)
        seen_ids: set[str] = set()
        for cohort in self.heterogeneous_cohorts:
            if cohort.cohort_id in seen_ids:
                raise ValueError("cohort ids must be unique")
            seen_ids.add(cohort.cohort_id)
            if set(cohort.assignments) != expected_profiles:
                raise ValueError("every cohort must assign every institution profile")
            if not set(cohort.assignments.values()) <= set(model_ids):
                raise ValueError("cohort references an unregistered model")
        for profile_id in profile_ids:
            assigned = [
                cohort.assignments[profile_id]
                for cohort in self.heterogeneous_cohorts
            ]
            if sorted(assigned) != sorted(model_ids):
                raise ValueError("each institution must use every model once across cohorts")
        transition_instruments = {
            item.instrument_id
            for item in self.transition_policy.instrument_assumptions
        }
        if transition_instruments != set(self.instrument_ids):
            raise ValueError(
                "transition assumptions must exactly cover preregistered instruments"
            )
        if any(
            item.institution_profile_version_id not in expected_profiles
            for item in self.transition_policy.liquidity_provision
        ):
            raise ValueError("transition policy references an unknown profile")
        return self

    @property
    def sha256(self) -> str:
        return content_sha256(self)


class PreregistrationApprovalRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)
    approved_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool = True


class EvidenceApprovalRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)
    approved_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool = True


class InitialRunPlanRequest(BaseModel):
    preregistration_id: str = Field(pattern=r"^PREREG-[A-Z0-9-]+$")
    evidence_snapshot_id: str = Field(pattern=r"^EVIDENCE-[A-Z0-9-]+$")
    actor: str = Field(min_length=1, max_length=100)


class InitialExecutionRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    confirmed_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_cells: int = Field(default=210, ge=1, le=210)


class FeedbackExecutionRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    confirmed_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_cells: int = Field(default=420, ge=1, le=420)


class MetricComputationRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    permutations: Literal[1000] = 1000
    bootstrap_samples: Literal[5000] = 5000


class WorldPlanRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class WorldTransitionRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    max_worlds: int = Field(default=60, ge=1, le=60)
