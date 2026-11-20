"""The six specification interfaces are independent and canonically serializable."""
from dataclasses import FrozenInstanceError
from decimal import Decimal as D

import pytest


def zero_costs():
    from nse_pipeline.specs import CostSpec
    return CostSpec(brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0,
                    stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0,
                    gst_on_brokerage_pct=0)


def zero_execution():
    from nse_pipeline.specs import ExecutionSpec
    return ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                         adverse_slippage_bps_sell=0)


def valuation(*, screen_value=False):
    from nse_pipeline.specs import ValuationPolicy
    return ValuationPolicy(fallback_lag=30, lag_unit="calendar_days",
                           sizing_uses_screen_value=screen_value)


def test_universe_round_trip_is_immutable_and_has_no_signal_fields():
    from nse_pipeline.specs import UniverseSpec

    spec = UniverseSpec(series=("EQ",), min_volume=5000)
    assert UniverseSpec.from_json(spec.to_json()) == spec
    assert '"schema_version":1' in spec.to_json()
    with pytest.raises(FrozenInstanceError):
        spec.min_price = 1
    with pytest.raises(ValueError):
        UniverseSpec(min_price=float("nan"))
    with pytest.raises(ValueError):
        UniverseSpec(series=("BE",))  # excluded settlement series
    with pytest.raises(TypeError):
        UniverseSpec(lookback_sessions=63)


def test_momentum_configuration_cannot_change_universe_membership_contract():
    from nse_pipeline.specs import MomentumSpec, UniverseSpec

    before = UniverseSpec().to_json()
    signal = MomentumSpec(lookback_sessions=21, min_observations=22)
    assert MomentumSpec.from_json(signal.to_json()) == signal
    assert UniverseSpec().to_json() == before
    with pytest.raises(TypeError):
        MomentumSpec(min_price=5)
    with pytest.raises(ValueError):
        MomentumSpec(lookback_sessions=True)


def test_execution_requires_explicit_delayed_settlement_and_valid_capacity():
    from nse_pipeline.specs import ExecutionSpec

    with pytest.raises(TypeError):
        ExecutionSpec()
    spec = ExecutionSpec(trade_settlement_lag_sessions=1)
    assert ExecutionSpec.from_json(spec.to_json()) == spec
    assert spec.max_order_notional is None  # JSON has no Infinity
    for kwargs in ({"trade_settlement_lag_sessions": 0},
                   {"trade_settlement_lag_sessions": 1, "sizing_participation_rate": .2,
                    "max_participation_rate": .05}):
        with pytest.raises(ValueError):
            ExecutionSpec(**kwargs)


def test_valuation_requires_explicit_dividend_lag_and_prohibits_stale_nav():
    from nse_pipeline.specs import ValuationPolicy

    with pytest.raises(TypeError):
        ValuationPolicy()
    spec = ValuationPolicy(fallback_lag=30, lag_unit="calendar_days")
    assert spec.dividend_settlement_mode == "verified_else_lag"
    assert ValuationPolicy.from_json(spec.to_json()) == spec
    held = ValuationPolicy(fallback_lag=None, lag_unit="calendar_days",
                           dividend_settlement_mode="hold_receivable")
    assert held.fallback_lag is None
    for overrides in ({"fallback_lag": 0}, {"fallback_lag": None},
                      {"carried_position_is": "valued_at_last_close"},
                      {"allow_scenario_statistics": True}, {"lag_unit": "days"}):
        with pytest.raises(ValueError):
            ValuationPolicy(**({"fallback_lag": 30, "lag_unit": "calendar_days"} | overrides))


def test_portfolio_keeps_zero_retirement_out_of_reference_runs():
    from nse_pipeline.specs import PortfolioSpec

    spec = PortfolioSpec()
    assert PortfolioSpec.from_json(spec.to_json()) == spec
    for overrides in ({"max_positions": 0}, {"initial_cash": float("inf")},
                      {"disposal_policy": "retire_zero"},
                      {"rebalance_anchor": "last_observed_session_of_month"}):
        with pytest.raises(ValueError):
            PortfolioSpec(**overrides)
    assert PortfolioSpec(disposal_policy="retire_zero", scenario_only=True).scenario_only


def test_cost_spec_has_no_slippage_or_implicit_historical_schedule():
    from nse_pipeline.specs import CostSpec

    spec = CostSpec()
    assert CostSpec.from_json(spec.to_json()) == spec
    assert spec.schedule_basis == "flat_research_assumption"
    with pytest.raises(TypeError):
        CostSpec(slippage_bps=5)
    with pytest.raises(ValueError):
        CostSpec(stt_bps_buy=-1)
    with pytest.raises(ValueError):
        CostSpec.from_json(spec.to_json().replace('"schema_version":1', '"schema_version":2'))


def test_screen_value_sizing_requires_matching_explicit_policy_flag():
    from nse_pipeline.specs import PortfolioSpec, ValuationPolicy, validate_policy_combination

    default = PortfolioSpec()
    marked_only = ValuationPolicy(fallback_lag=30, lag_unit="calendar_days")
    validate_policy_combination(default, marked_only)
    p3 = PortfolioSpec(sizing_policy="P3_size_including_unresolved_at_screen")
    with pytest.raises(ValueError):
        validate_policy_combination(p3, marked_only)
    screened = ValuationPolicy(fallback_lag=30, lag_unit="calendar_days", sizing_uses_screen_value=True)
    validate_policy_combination(p3, screened)
    with pytest.raises(ValueError):
        validate_policy_combination(default, screened)


def test_plan_observation_and_execution_controls_are_pinned():
    from nse_pipeline.specs import MomentumSpec, ExecutionSpec

    signal = MomentumSpec(observation_rule="valid_observations")
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, circuit_limit_pct=.2,
                              hold_suspended_positions=True)
    assert MomentumSpec.from_json(signal.to_json()) == signal
    assert ExecutionSpec.from_json(execution.to_json()) == execution
    with pytest.raises(ValueError):
        MomentumSpec(observation_rule="filled_calendar_rows")
    with pytest.raises(ValueError):
        ExecutionSpec(trade_settlement_lag_sessions=1, circuit_limit_pct=2)


def test_long_and_short_policy_names_are_equivalent():
    from nse_pipeline.backtest.sizing import size_orders
    from nse_pipeline.specs import ALIASES, CANONICAL_POLICIES, LONG_POLICIES, PortfolioSpec

    assert ALIASES == {"P1": "P1_freeze_on_unresolved",
                       "P2": "P2_size_excluding_unresolved",
                       "P3": "P3_size_including_unresolved_at_screen"}
    assert tuple(LONG_POLICIES) == tuple(CANONICAL_POLICIES)
    expected_filled = {"P1": (D("0"), D("0")),
                       "P2": (D("0"), D("450")),
                       "P3": (D("550"), D("350"))}
    expected_cash = {"P1": D("900"), "P2": D("450"), "P3": D("0")}
    expected_reasons = {"P1": ("frozen_unresolved", "frozen_unresolved"),
                        "P2": ("excluded_unresolved", "filled"),
                        "P3": ("filled_screen_value", "filled")}
    terminal = {}
    for short, long in ALIASES.items():
        kwargs = ({"screen_values": ("550",), "screen_prices": ("1",)}
                  if short == "P3" else {})
        short_result = size_orders(short, "900", ["550", "450"], price="1", slots=2,
                                   costs=zero_costs(), execution=zero_execution(),
                                   unresolved_ranks=(0,), **kwargs)
        long_result = size_orders(long, "900", ["550", "450"], price="1", slots=2,
                                  costs=zero_costs(), execution=zero_execution(),
                                  unresolved_ranks=(0,), **kwargs)
        assert short_result == long_result
        assert short_result.policy == long
        assert short_result.filled == expected_filled[short]
        assert short_result.remaining_cash == expected_cash[short]
        assert short_result.reasons == expected_reasons[short]
        assert short_result.fees == D("0")
        terminal[short] = short_result.filled
    assert len(set(terminal.values())) == 3  # the three policies discriminate
    with pytest.raises(ValueError, match="unknown_sizing_policy"):
        PortfolioSpec(sizing_policy="P2")
    for long in CANONICAL_POLICIES:
        spec = PortfolioSpec(sizing_policy=long)
        assert spec.sizing_policy == long
        assert PortfolioSpec.from_json(spec.to_json()) == spec


def test_portfolio_default_is_consumable_as_p2():
    from nse_pipeline.backtest.sizing import size_orders, size_portfolio_orders
    from nse_pipeline.specs import PortfolioSpec, resolve_sizing_policy

    default = PortfolioSpec()
    marked = valuation(screen_value=False)
    assert default.sizing_policy == "P2_size_excluding_unresolved"
    assert resolve_sizing_policy(default, marked) == "P2_size_excluding_unresolved"
    direct = size_orders(default.sizing_policy, "900", ["550", "450"], price="1",
                         slots=default.max_positions, costs=zero_costs(),
                         execution=zero_execution(), unresolved_ranks=(0,))
    produced = size_portfolio_orders(default, marked, "900", ["550", "450"], price="1",
                                     costs=zero_costs(), execution=zero_execution(),
                                     unresolved_ranks=(0,))
    assert produced == direct
    assert produced.policy == "P2_size_excluding_unresolved"
    assert produced.filled == (D("0"), D("450"))
    assert produced.remaining_cash == D("450")
    assert produced.reasons == ("excluded_unresolved", "filled")
    # max_positions is the slot source for the portfolio entry point.
    constrained = size_portfolio_orders(PortfolioSpec(max_positions=1), marked, "100000",
                                        ["40000", "40000"], price="1",
                                        costs=zero_costs(), execution=zero_execution())
    assert constrained.filled == (D("40000"), D("0"))
    assert constrained.reasons == ("filled", "no_slot")


def test_policy_validation_runs_before_sizing():
    from nse_pipeline.backtest.sizing import size_orders, size_portfolio_orders
    from nse_pipeline.specs import PortfolioSpec

    p3 = PortfolioSpec(sizing_policy="P3_size_including_unresolved_at_screen")
    marked = valuation(screen_value=False)
    screened = valuation(screen_value=True)
    with pytest.raises(ValueError, match="screen-value sizing must be explicitly enabled"):
        size_portfolio_orders(p3, marked, "1000", ["550"], price="1",
                              costs=zero_costs(), execution=zero_execution(),
                              unresolved_ranks=(0,), screen_values=("550",),
                              screen_prices=("1",))
    filled = size_portfolio_orders(p3, screened, "1000", ["550"], price="1",
                                   costs=zero_costs(), execution=zero_execution(),
                                   unresolved_ranks=(0,), screen_values=("550",),
                                   screen_prices=("1",))
    assert filled.filled == (D("550"),)
    assert filled.reasons == ("filled_screen_value",)
    assert filled.policy == p3.sizing_policy
    for policy in ("P1_freeze_on_unresolved", "P2_size_excluding_unresolved"):
        with pytest.raises(ValueError, match="screen-value sizing must be explicitly enabled"):
            size_portfolio_orders(PortfolioSpec(sizing_policy=policy), screened, "1000",
                                  ["550"], price="1", costs=zero_costs(),
                                  execution=zero_execution())
        allowed = size_portfolio_orders(PortfolioSpec(sizing_policy=policy), marked,
                                        "1000", ["550"], price="1",
                                        costs=zero_costs(), execution=zero_execution())
        assert allowed.policy == policy
    # size_orders alone never consults the cross-spec valuation pairing.
    standalone = size_orders("P3_size_including_unresolved_at_screen", "1000", ["550"],
                             price="1", slots=1, costs=zero_costs(),
                             execution=zero_execution(), unresolved_ranks=(0,),
                             screen_values=("550",), screen_prices=("1",))
    assert standalone.policy == "P3_size_including_unresolved_at_screen"


def test_policy_validation_does_not_override_scenario_restriction():
    from nse_pipeline.specs import PortfolioSpec, ValuationPolicy, resolve_sizing_policy

    base = PortfolioSpec()
    p3 = PortfolioSpec(sizing_policy="P3_size_including_unresolved_at_screen")
    for policy, screen_value in ((base, False), (p3, True)):
        with pytest.raises(ValueError, match="Scenario-valued series"):
            ValuationPolicy(fallback_lag=30, lag_unit="calendar_days",
                            sizing_uses_screen_value=screen_value,
                            allow_scenario_statistics=True)
    assert resolve_sizing_policy(p3, valuation(screen_value=True)) == p3.sizing_policy
    with pytest.raises(ValueError):
        PortfolioSpec(disposal_policy="retire_zero")
    assert base.disposal_policy == "hold_unresolved"
    scenario = PortfolioSpec(disposal_policy="retire_zero", scenario_only=True)
    assert scenario.disposal_policy == "retire_zero"
    assert resolve_sizing_policy(scenario, valuation(screen_value=False)) == scenario.sizing_policy


def test_rebalance_schedule_descriptor_is_frozen_and_fails_closed():
    from nse_pipeline.specs import PortfolioSpec
    from nse_pipeline.specs.validation import (RebalanceScheduleDescriptor,
                                               resolve_rebalance_schedule)

    descriptor = resolve_rebalance_schedule(PortfolioSpec())
    assert descriptor == RebalanceScheduleDescriptor(
        frequency="monthly", anchor="known_calendar_else_observed_month_change")
    with pytest.raises(FrozenInstanceError):
        descriptor.frequency = "weekly"
    for kwargs in ({"frequency": "weekly",
                    "anchor": "known_calendar_else_observed_month_change"},
                   {"frequency": "monthly",
                    "anchor": "last_observed_session_of_month"}):
        with pytest.raises(ValueError):
            RebalanceScheduleDescriptor(**kwargs)
    with pytest.raises(ValueError):
        resolve_rebalance_schedule(object())


def test_validate_run_specs_asserts_pinned_single_value_fields():
    from nse_pipeline.specs import PortfolioSpec
    from nse_pipeline.specs.validation import validate_run_specs

    validate_run_specs(PortfolioSpec(), valuation(), zero_execution(), costs=zero_costs())
    tampered = (("execution", "settlement_funding", "expected_proceeds_can_fund"),
                ("portfolio", "allocation_order", "a_before_b"),
                ("valuation", "dividend_cash_phase", "pre_open"),
                ("costs", "schedule_basis", "tiered_schedule"),
                ("costs", "monetary_rounding", "float"))
    for kind, name, value in tampered:
        portfolio, policy, execution, costs = (PortfolioSpec(), valuation(),
                                               zero_execution(), zero_costs())
        target = {"portfolio": portfolio, "valuation": policy,
                  "execution": execution, "costs": costs}[kind]
        object.__setattr__(target, name, value)
        with pytest.raises(ValueError, match=name):
            validate_run_specs(portfolio, policy, execution, costs=costs)
