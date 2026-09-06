"""Approved v5 scenario inputs derived from the workbook and operator defaults."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .models import (
    CohortAssignment,
    DecisionClock,
    ExperimentPreregistration,
    HoldingPosition,
    InstitutionProfileV5,
    LiquidityState,
    ModelRegistration,
    PortfolioRiskState,
    RiskTrigger,
    SamplingPolicy,
    SourceLocator,
    InstrumentImpactAssumption,
    LiquidityProvisionAssumption,
    TransitionPolicyV5,
)


PROFILE_CATALOG_VERSION = "colfi-profiles-v5.2"
SOURCE_ARTIFACT = "COLFI_Agentic_Stress_Lab_Use_Case_NEW.xlsx"
SOURCE_ARTIFACT_SHA256 = (
    "37cdde7f63424de39ec31fc2c8047d3a5090a1ee121540d9b89ac135c47c4dcf"
)
ALL_ACTIONS = [
    "sell",
    "hedge",
    "deleverage",
    "withdraw_liquidity",
    "buy_support",
]
INVESTMENT_ACTIONS = ["sell", "hedge", "deleverage", "buy_support"]
LONG_ONLY_ACTIONS = ["sell", "hedge", "buy_support"]


def _source(worksheet: str, row: int) -> SourceLocator:
    return SourceLocator(
        artifact=SOURCE_ARTIFACT,
        artifact_sha256=SOURCE_ARTIFACT_SHA256,
        worksheet=worksheet,
        row=row,
    )


def _holding(position_id: str, instrument_id: str, notional_pct: float,
             currency: str) -> HoldingPosition:
    return HoldingPosition(
        position_id=position_id,
        instrument_id=instrument_id,
        side="long",
        notional_pct=notional_pct,
        currency=currency,
        source_note=(
            "User-provided editable default portfolio; it is a scenario input, "
            "not actual institutional holdings."
        ),
    )


V5_INSTITUTION_PROFILES = {
    "INST-01": InstitutionProfileV5(
        profile_version_id="IPV5-INST-01-V2",
        catalog_version=PROFILE_CATALOG_VERSION,
        institution_id="INST-01",
        display_name="Institution 1",
        institution_type="Multi-strategy investment firm",
        objective="Protect portfolio value while respecting the benchmark mandate.",
        constraints=[
            "Tracking-error, cash and concentration limits remain binding.",
            "Daily VaR is close to its approved limit.",
        ],
        system_footprint_pct=15.0,
        base_currency="USD",
        portfolio_state=PortfolioRiskState(
            portfolio_description=(
                "Net equity exposure 25%; gross exposure 185%; significant "
                "index-derivative positions."
            ),
            current_loss_pct=-2.8,
            current_loss_basis="portfolio_notional",
            net_equity_exposure_pct=25.0,
            gross_exposure_pct=185.0,
        ),
        liquidity_state=LiquidityState(cash_buffer_pct=10.0, margin_headroom_pct=17.0),
        triggers=[RiskTrigger(
            trigger_id="TRG-INST-01-VAR",
            metric="daily_var_limit_utilisation",
            observed_value=88.0,
            unit="pct_of_limit",
            limit_value=100.0,
            status="approaching_limit",
            description="Daily VaR has reached 88% of its limit.",
        )],
        permitted_actions=INVESTMENT_ACTIONS,
        holdings_status="approved",
        holdings=[_holding("POS-INST-01-SP500", "sp500", 100.0, "USD")],
        sources=[_source("Use Case", 20), _source("Inputs", 31)],
    ),
    "INST-02": InstitutionProfileV5(
        profile_version_id="IPV5-INST-02-V2",
        catalog_version=PROFILE_CATALOG_VERSION,
        institution_id="INST-02",
        display_name="Institution 2",
        institution_type="Quantitative investment manager",
        objective="Limit drawdown while preserving risk-adjusted return.",
        constraints=[
            "Leverage, VaR and margin limits remain binding.",
            "Leverage is approaching its maximum.",
        ],
        system_footprint_pct=12.0,
        base_currency="USD",
        portfolio_state=PortfolioRiskState(
            portfolio_description=(
                "Net equity exposure 18%; gross exposure 220%; systematic "
                "equity and futures positions."
            ),
            current_loss_pct=-3.2,
            current_loss_basis="portfolio_notional",
            net_equity_exposure_pct=18.0,
            gross_exposure_pct=220.0,
        ),
        liquidity_state=LiquidityState(cash_buffer_pct=7.0, margin_headroom_pct=11.0),
        triggers=[RiskTrigger(
            trigger_id="TRG-INST-02-VAR",
            metric="var_limit_utilisation",
            observed_value=94.0,
            unit="pct_of_limit",
            limit_value=100.0,
            status="approaching_limit",
            description="VaR is at 94% of limit and leverage is approaching maximum.",
        )],
        permitted_actions=INVESTMENT_ACTIONS,
        holdings_status="approved",
        holdings=[_holding("POS-INST-02-NASDAQ", "nasdaq", 100.0, "USD")],
        sources=[_source("Use Case", 21), _source("Inputs", 32)],
    ),
    "INST-03": InstitutionProfileV5(
        profile_version_id="IPV5-INST-03-V2",
        catalog_version=PROFILE_CATALOG_VERSION,
        institution_id="INST-03",
        display_name="Institution 3",
        institution_type="Long-horizon asset manager",
        objective="Maintain the funding position and strategic allocation.",
        constraints=[
            "Remain within tracking-error and cash limits.",
            "Meet recorded redemption requests without inventing liquidity.",
        ],
        system_footprint_pct=28.0,
        base_currency="JPY",
        portfolio_state=PortfolioRiskState(
            portfolio_description=(
                "72% long-only global-equity exposure linked closely to a benchmark."
            ),
            current_loss_pct=-3.0,
            current_loss_basis="portfolio_notional",
            long_only_global_equity_pct=72.0,
        ),
        liquidity_state=LiquidityState(
            cash_buffer_pct=4.0,
            redemption_requests_pct=1.2,
        ),
        triggers=[RiskTrigger(
            trigger_id="TRG-INST-03-MANDATE",
            metric="tracking_error_and_cash_limits",
            unit="mandate_constraint",
            status="binding_obligation",
            description="The portfolio must remain within tracking-error and cash limits.",
        )],
        permitted_actions=LONG_ONLY_ACTIONS,
        holdings_status="approved",
        holdings=[_holding("POS-INST-03-NIKKEI", "nikkei", 100.0, "JPY")],
        sources=[_source("Use Case", 22), _source("Inputs", 33)],
    ),
    "INST-04": InstitutionProfileV5(
        profile_version_id="IPV5-INST-04-V2",
        catalog_version=PROFILE_CATALOG_VERSION,
        institution_id="INST-04",
        display_name="Institution 4",
        institution_type="Global bank and market-making dealer",
        objective="Manage inventory while continuing to provide market liquidity.",
        constraints=[
            "Inventory, capital and quoting limits remain binding.",
            "Liquidity-provision obligations remain active.",
        ],
        system_footprint_pct=18.0,
        base_currency="EUR",
        portfolio_state=PortfolioRiskState(
            portfolio_description=(
                "Net-long equity inventory; client sell orders running at twice "
                "normal levels."
            ),
            current_loss_pct=-1.1,
            current_loss_basis="allocated_risk_capital",
            client_sell_flow_multiple=2.0,
        ),
        liquidity_state=LiquidityState(
            capital_headroom_pct=20.0,
            market_depth_pct_of_normal=55.0,
        ),
        triggers=[RiskTrigger(
            trigger_id="TRG-INST-04-INVENTORY",
            metric="inventory_limit_utilisation",
            observed_value=82.0,
            unit="pct_of_limit",
            limit_value=100.0,
            status="approaching_limit",
            description=(
                "Inventory utilisation is 82%; liquidity-provision obligations "
                "remain active."
            ),
        )],
        permitted_actions=ALL_ACTIONS,
        holdings_status="approved",
        holdings=[_holding("POS-INST-04-EUROSTOXX", "eurostoxx", 100.0, "EUR")],
        sources=[_source("Use Case", 23), _source("Inputs", 34)],
    ),
    "INST-05": InstitutionProfileV5(
        profile_version_id="IPV5-INST-05-V2",
        catalog_version=PROFILE_CATALOG_VERSION,
        institution_id="INST-05",
        display_name="Institution 5",
        institution_type="Quantitative multi-asset investment manager",
        objective="Keep the portfolio within its volatility and loss budget.",
        constraints=[
            "Mandate, turnover and hedging limits remain binding.",
            "Realised volatility is above target.",
        ],
        system_footprint_pct=10.0,
        base_currency="USD",
        portfolio_state=PortfolioRiskState(
            portfolio_description=(
                "Net equity exposure 20%; gross multi-asset exposure 165%."
            ),
            current_loss_pct=-2.4,
            current_loss_basis="portfolio_notional",
            net_equity_exposure_pct=20.0,
            gross_exposure_pct=165.0,
        ),
        liquidity_state=LiquidityState(cash_buffer_pct=8.0, margin_headroom_pct=14.0),
        triggers=[RiskTrigger(
            trigger_id="TRG-INST-05-VOL",
            metric="realised_volatility_vs_target",
            observed_value=125.0,
            unit="pct_of_target",
            limit_value=100.0,
            status="approaching_limit",
            description="Realised volatility has reached 125% of target.",
        )],
        permitted_actions=INVESTMENT_ACTIONS,
        holdings_status="approved",
        holdings=[
            _holding("POS-INST-05-SP500", "sp500", 50.0, "USD"),
            _holding("POS-INST-05-NASDAQ", "nasdaq", 50.0, "USD"),
        ],
        sources=[_source("Use Case", 24), _source("Inputs", 35)],
    ),
    "INST-06": InstitutionProfileV5(
        profile_version_id="IPV5-INST-06-V2",
        catalog_version=PROFILE_CATALOG_VERSION,
        institution_id="INST-06",
        display_name="Institution 6",
        institution_type="Systematic multi-asset investment manager",
        objective=(
            "Preserve systematic strategy performance while controlling drawdown "
            "and factor concentration."
        ),
        constraints=[
            "Leverage, factor, liquidity, turnover and margin limits remain binding.",
            "Factor concentration is close to its approved limit.",
        ],
        system_footprint_pct=9.0,
        base_currency="EUR",
        portfolio_state=PortfolioRiskState(
            portfolio_description=(
                "Net equity exposure 12%; gross exposure 240%; concentrated "
                "systematic-factor positions."
            ),
            current_loss_pct=-2.7,
            current_loss_basis="portfolio_notional",
            net_equity_exposure_pct=12.0,
            gross_exposure_pct=240.0,
        ),
        liquidity_state=LiquidityState(cash_buffer_pct=6.0, margin_headroom_pct=10.0),
        triggers=[RiskTrigger(
            trigger_id="TRG-INST-06-FACTOR",
            metric="factor_concentration_limit_utilisation",
            observed_value=91.0,
            unit="pct_of_limit",
            limit_value=100.0,
            status="approaching_limit",
            description="Factor-concentration limit is 91% utilised.",
        )],
        permitted_actions=INVESTMENT_ACTIONS,
        holdings_status="approved",
        holdings=[
            _holding("POS-INST-06-NIKKEI", "nikkei", 50.0, "JPY"),
            _holding("POS-INST-06-EUROSTOXX", "eurostoxx", 50.0, "EUR"),
        ],
        sources=[_source("Use Case", 25), _source("Inputs", 36)],
    ),
    "INST-07": InstitutionProfileV5(
        profile_version_id="IPV5-INST-07-V2",
        catalog_version=PROFILE_CATALOG_VERSION,
        institution_id="INST-07",
        display_name="Institution 7",
        institution_type="Global macro investment manager",
        objective=(
            "Preserve portfolio risk balance and limit drawdown as regimes and "
            "correlations change."
        ),
        constraints=[
            "Risk allocation, leverage, concentration and liquidity limits remain binding.",
            "Counterparty and margin constraints remain in force.",
        ],
        system_footprint_pct=8.0,
        base_currency="USD",
        portfolio_state=PortfolioRiskState(
            portfolio_description=(
                "Equity risk is 32% of portfolio risk versus a 25% target; bonds "
                "partly offset losses."
            ),
            current_loss_pct=-1.8,
            current_loss_basis="portfolio_notional",
            equity_risk_share_pct=32.0,
            equity_risk_target_pct=25.0,
        ),
        liquidity_state=LiquidityState(cash_buffer_pct=12.0, margin_headroom_pct=20.0),
        triggers=[RiskTrigger(
            trigger_id="TRG-INST-07-BUDGET",
            metric="total_risk_budget_utilisation",
            observed_value=90.0,
            unit="pct_of_budget",
            limit_value=100.0,
            status="approaching_limit",
            description="Total risk budget is 90% utilised.",
        )],
        permitted_actions=INVESTMENT_ACTIONS,
        holdings_status="approved",
        holdings=[
            _holding("POS-INST-07-SP500", "sp500", 25.0, "USD"),
            _holding("POS-INST-07-NASDAQ", "nasdaq", 25.0, "USD"),
            _holding("POS-INST-07-NIKKEI", "nikkei", 25.0, "JPY"),
            _holding("POS-INST-07-EUROSTOXX", "eurostoxx", 25.0, "EUR"),
        ],
        sources=[_source("Use Case", 26), _source("Inputs", 37)],
    ),
}


def profile_catalog_readiness(
    profiles: dict[str, InstitutionProfileV5] | None = None,
) -> dict:
    selected = profiles or V5_INSTITUTION_PROFILES
    footprint_total = sum(item.system_footprint_pct for item in selected.values())
    holdings_totals = {
        institution_id: sum(
            holding.notional_pct
            for holding in profile.holdings
            if isinstance(holding, HoldingPosition)
        )
        for institution_id, profile in selected.items()
    }
    checks = {
        "seven_profiles": len(selected) == 7,
        "footprints_total_100": abs(footprint_total - 100.0) < 1e-9,
        "holdings_approved": all(
            item.holdings_status == "approved" for item in selected.values()
        ),
        "holdings_total_100_each": all(
            abs(total - 100.0) < 1e-9 for total in holdings_totals.values()
        ),
    }
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "profile_count": len(selected),
        "footprint_total_pct": footprint_total,
        "holdings_totals_pct": holdings_totals,
    }


def cyclic_heterogeneous_cohorts(
    profile_version_ids: list[str], model_ids: list[str]
) -> list[CohortAssignment]:
    if not profile_version_ids or not model_ids:
        raise ValueError("profiles and models are required")
    return [
        CohortAssignment(
            cohort_id=f"HET-{cohort + 1:02d}",
            assignments={
                profile_id: model_ids[(institution + cohort) % len(model_ids)]
                for institution, profile_id in enumerate(profile_version_ids)
            },
        )
        for cohort in range(len(model_ids))
    ]


def default_transition_policy(profile_version_ids: list[str]) -> TransitionPolicyV5:
    """Return editable normalized defaults, explicitly not observed market data."""

    instruments = [
        ("sp500", "asset", "USD", None, None),
        ("nasdaq", "asset", "USD", None, None),
        ("nikkei", "asset", "JPY", None, None),
        ("eurostoxx", "asset", "EUR", None, None),
        ("vix", "asset", "USD", None, None),
        ("eurjpy", "fx", "JPY", "EUR", "JPY"),
        ("usdjpy", "fx", "JPY", "USD", "JPY"),
        ("eurusd", "fx", "USD", "EUR", "USD"),
    ]
    note = (
        "Operator-editable normalized exercise default: one modelled daily ADV "
        "equals 100% of system notional, round depth equals 10%, and the "
        "square-root move at one ADV equals 1%. This is not observed market data "
        "and requires approval as part of the preregistration."
    )
    dealer_profile = next(
        profile_id for profile_id in profile_version_ids
        if "INST-04" in profile_id
    )
    return TransitionPolicyV5(
        instrument_assumptions=[
            InstrumentImpactAssumption(
                instrument_id=instrument_id,
                instrument_kind=instrument_kind,
                valuation_currency=valuation_currency,
                fx_base_currency=fx_base,
                fx_quote_currency=fx_quote,
                daily_adv_system_notional_pct=100.0,
                round_depth_system_notional_pct=10.0,
                square_root_impact_coefficient_pct=1.0,
                source_note=note,
            )
            for instrument_id, instrument_kind, valuation_currency, fx_base, fx_quote
            in instruments
        ],
        liquidity_provision=[
            LiquidityProvisionAssumption(
                institution_profile_version_id=dealer_profile,
                instrument_id=instrument_id,
                provided_depth_share_pct=100.0,
            )
            for instrument_id, *_ in instruments
        ],
    )


def build_confirmatory_preregistration(
    model_ids: list[str],
    preregistration_id: str = "PREREG-AUG2024-V1",
) -> ExperimentPreregistration:
    profiles = [
        V5_INSTITUTION_PROFILES[key].profile_version_id
        for key in sorted(V5_INSTITUTION_PROFILES)
    ]
    registrations = [
        ModelRegistration(
            model_id=model_id,
            provider=model_id.split(":", 1)[0],
            exact_model=model_id.split(":", 1)[1],
        )
        for model_id in model_ids
    ]
    initial = datetime(2024, 8, 5, 9, 35, tzinfo=timezone(timedelta(hours=-4)))
    feedback = datetime(2024, 8, 5, 10, 5, tzinfo=timezone(timedelta(hours=-4)))
    second_transition = datetime(
        2024, 8, 5, 10, 35, tzinfo=timezone(timedelta(hours=-4))
    )
    return ExperimentPreregistration(
        preregistration_id=preregistration_id,
        title="5 August 2024 heterogeneous and shared-model stress experiment",
        research_questions=["UC-01", "UC-02"],
        instrument_ids=[
            "sp500", "nasdaq", "nikkei", "eurostoxx", "vix", "eurjpy",
            "usdjpy", "eurusd",
        ],
        institution_profile_version_ids=profiles,
        models=registrations,
        decision_clock=DecisionClock(
            initial_cutoff=initial,
            feedback_at=feedback,
            second_transition_at=second_transition,
        ),
        sampling=SamplingPolicy(),
        transition_policy=default_transition_policy(profiles),
        randomization_seed="colfi-august-2024-confirmatory-v1",
        heterogeneous_cohorts=cyclic_heterogeneous_cohorts(profiles, model_ids),
        profile_catalog_version=PROFILE_CATALOG_VERSION,
        metric_ids=[
            "pairwise_action_category_agreement",
            "pairwise_direction_agreement",
            "pairwise_size_similarity",
            "pairwise_urgency_similarity",
            "model_main_effects",
        ],
        release_gate_ids=[
            "approved_preregistration_hash",
            "balanced_initial_cells",
            "world_same_replicate_integrity",
            "private_profile_isolation",
            "typed_action_validity",
            "deterministic_metric_recomputation",
        ],
    )
