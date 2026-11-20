"""Production-runner identity migration and lifecycle admission contracts.

Every fixture below is hand-derived and synthetic. No historical document is
authenticated and no strategy, return or performance claim is produced. The
runner receives dated observations and dated evidence only; it never infers an
identity, suspension, delisting or terminal proceeds amount.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

DAY1 = date(2024, 6, 3)
DAY2 = date(2024, 6, 4)
DAY3 = date(2024, 6, 5)
DAY4 = date(2024, 6, 6)

OLD_KEY = "OLD|INE1|2024-06-03"
MID_KEY = "MID|NOISIN|2024-06-04"
NEW_KEY = "NEW|NOISIN|2024-06-05"


def source(phase, day=DAY1, character="a"):
    from nse_pipeline.backtest.accounting_types import EventTime
    from nse_pipeline.backtest.observed_session import InputProvenance
    return InputProvenance("fixture document", character * 64, EventTime(day, phase),
                           "supplied fixture; not authenticated")


def components():
    from nse_pipeline.specs import CostSpec, ExecutionSpec
    costs = CostSpec(brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0,
                     stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0,
                     gst_on_brokerage_pct=0, schedule_basis="flat_research_assumption",
                     monetary_rounding="paise_half_up_per_component")
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, fill_timing="next_session_open",
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0,
                              sizing_participation_rate=0.05, max_participation_rate=0.05,
                              adv_window_sessions=20, max_order_notional=None,
                              min_order_notional=0.0, partial_fill_policy="partial",
                              settlement_funding="settled_cash_only",
                              unfilled_remainder="expire", circuit_limit_pct=0.20,
                              hold_suspended_positions=True)
    return costs, execution


def observation(symbol, day, isin=None, series="EQ"):
    from nse_pipeline.backtest.identity import Observation
    return Observation(symbol, series, isin, day)


def identity(edges=(), retirements=()):
    from nse_pipeline.backtest.observed_session import IdentityEvidence
    return IdentityEvidence(tuple(edges), tuple(retirements), ("EQ",), False)


def listed(key, day=DAY1):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.lifecycle import LifecycleRecord, LifecycleState
    return LifecycleRecord(key, LifecycleState.LISTED, day,
                           EventTime(day, Phase.PRE_OPEN_EVIDENCE), "fixture-listed")


def state_record(key, state, effective_from, available_day, available_phase,
                 terminal_proceeds=None, proceeds_verified=False):
    from nse_pipeline.backtest.accounting_types import EventTime
    from nse_pipeline.backtest.lifecycle import LifecycleRecord, LifecycleState
    return LifecycleRecord(key, LifecycleState[state], effective_from,
                           EventTime(available_day, available_phase),
                           "fixture-" + state, terminal_proceeds=terminal_proceeds,
                           proceeds_verified=proceeds_verified)


def lifecycle(*records):
    from nse_pipeline.backtest.observed_session import LifecycleEvidence
    return LifecycleEvidence(tuple(records))


def runner(*, identity_evidence=None, lifecycle_evidence=None,
           position_key=OLD_KEY, shares=10, cash="1000", valuation_policy=None):
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import SessionRunner
    costs, execution = components()
    return SessionRunner(
        cash, on=DAY1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE, DAY1),
        opening_positions=(Position("p", position_key, shares, date(2023, 12, 29)),),
        costs=costs, execution=execution, identity_evidence=identity_evidence,
        lifecycle_evidence=lifecycle_evidence, valuation_policy=valuation_policy)


def session(day, **kwargs):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput
    return SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE, day), **kwargs)


def close_bar(key, day, close="100"):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import CloseBar
    from nse_pipeline.backtest.observations import Bar
    return CloseBar(Bar(key, key.split("|")[0], "EQ", day, "100", "101", "99", close,
                        100, True, traded_value="10000"),
                    source(Phase.CLOSE_VALUATION, day))


def opening(key, day, price="100"):
    from nse_pipeline.backtest.accounting_types import OpeningPrint, Phase
    from nse_pipeline.backtest.observed_session import OpeningObservation
    return OpeningObservation(OpeningPrint(key, day, price), source(Phase.OPEN_FILLS, day))


def buy_target(order_id, key, day, quantity=10, budget="1000"):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import BuyTarget, CapacityInput
    return BuyTarget(order_id, key, quantity, budget,
                     CapacityInput(budget, source(Phase.CLOSE_ORDERS, day)), 1,
                     source(Phase.CLOSE_ORDERS, day))


def sell_target(order_id, position_id, day, quantity=10):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SellTarget
    return SellTarget(order_id, position_id, quantity, 1,
                      source(Phase.CLOSE_ORDERS, day))


def held(run):
    return next(item for item in run.snapshot.positions if item.position_id == "p")


def edge(edge_id, old, new, effective, available_day, available_phase=None):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.identity import IdentityEdge
    phase = available_phase or Phase.PRE_OPEN_ACTIONS
    return IdentityEdge(edge_id, old, new, effective,
                        EventTime(available_day, phase), "fixture-edge")


def retirement(retirement_id, symbol, effective, available_day):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.identity import Retirement
    return Retirement(retirement_id, symbol, effective,
                      EventTime(available_day, Phase.PRE_OPEN_ACTIONS), "fixture-retirement")


def test_identity_edge_acts_only_after_its_availability_and_effective_date():
    from nse_pipeline.backtest.accounting_types import Phase
    late = edge("E1", "OLD", "MID", DAY2, DAY2, available_phase=Phase.CLOSE_VALUATION)
    r = runner(identity_evidence=identity((late,)))
    first = r.step(session(DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
                           close_bars=(close_bar(OLD_KEY, DAY1),)))
    assert first.identity_migrations == ()
    assert r.identity_ledger.migrations == ()
    assert held(r).key == OLD_KEY

    second = r.step(session(DAY2))
    assert second.identity_migrations == ()
    assert held(r).key == OLD_KEY
    assert [m.migration_id for m in r.identity_ledger.migrations] == ["E1"]

    third = r.step(session(DAY3))
    assert [m.migration_id for m in third.identity_migrations] == ["E1"]
    assert third.identity_migrations[0].admitted_at.session == DAY2
    assert third.identity_migrations[0].admitted_at.phase == Phase.CLOSE_AUDIT
    assert held(r).key == MID_KEY
    assert third.reconciliation.passed


def test_pre_open_migration_precedes_a_same_day_frozen_sale():
    from nse_pipeline.backtest.accounting_types import EventKind, Phase
    identity_evidence = identity((edge("E1", "OLD", "MID", DAY2, DAY2),))
    lifecycle_evidence = lifecycle(
        listed(OLD_KEY), state_record(MID_KEY, "LISTED", DAY2, DAY2, Phase.PRE_OPEN_EVIDENCE))
    r = runner(identity_evidence=identity_evidence, lifecycle_evidence=lifecycle_evidence)

    first = r.step(session(DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
                           close_bars=(close_bar(OLD_KEY, DAY1),),
                           sell_targets=(sell_target("sell-p", "p", DAY1, 4),)))
    assert len(first.next_sells) == 1

    second = r.step(session(DAY2, opening_prints=(opening(MID_KEY, DAY2),)))
    assert [m.migration_id for m in second.identity_migrations] == ["E1"]
    sells = tuple(event for event in second.events if event.kind == EventKind.SELL)
    assert len(sells) == 1 and sells[0].key == MID_KEY
    assert held(r).key == MID_KEY
    assert held(r).shares == 6
    assert second.reconciliation.passed


def test_historical_events_keep_the_old_stable_key_after_migration():
    from nse_pipeline.backtest.accounting_types import EventKind, Phase
    identity_evidence = identity((edge("E1", "OLD", "MID", DAY2, DAY2),))
    lifecycle_evidence = lifecycle(
        listed(OLD_KEY), state_record(MID_KEY, "LISTED", DAY2, DAY2, Phase.PRE_OPEN_EVIDENCE))
    r = runner(identity_evidence=identity_evidence, lifecycle_evidence=lifecycle_evidence)

    first = r.step(session(DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
                           close_bars=(close_bar(OLD_KEY, DAY1),),
                           buy_targets=(buy_target("buy-old", OLD_KEY, DAY1),)))
    assert [event.kind for event in first.events] == [EventKind.RESERVE]
    assert first.events[0].key == OLD_KEY

    second = r.step(session(DAY2, opening_prints=(opening(MID_KEY, DAY2),)))
    assert second.identity_migrations[0].old_key == OLD_KEY
    assert second.identity_migrations[0].new_key == MID_KEY
    assert [event.kind for event in second.events] == [EventKind.BUY]
    assert second.events[0].key == MID_KEY
    assert r.events[0].key == OLD_KEY
    assert held(r).key == MID_KEY
    assert held(r).shares == 20
    old = next(key for key in r.identity_ledger.keys if key.key == OLD_KEY)
    assert (old.retired_on, old.retired_by) == (DAY2, "E1")
    assert second.reconciliation.passed


def test_migration_rekeys_a_live_dividend_entitlement_and_its_later_events():
    from nse_pipeline.backtest.accounting_types import Evidence, EventKind, EventTime, Phase
    from nse_pipeline.backtest.dividends import (
        GROSS_DIVIDEND_NO_WITHHOLDING_MODEL, DividendTerms, VerifiedPayment,
    )
    from nse_pipeline.backtest.observed_session import (
        DividendAccrualInput, DividendConfirmationInput, DividendEntitlementInput,
    )
    from nse_pipeline.specs import ValuationPolicy

    policy = ValuationPolicy(fallback_lag=None, lag_unit="calendar_days",
                             dividend_settlement_mode="hold_receivable")
    terms = DividendTerms("div-1", "2", Evidence(
        "issuer dividend fixture", OLD_KEY, DAY1,
        EventTime(DAY1, Phase.PRE_OPEN_ACTIONS), True))
    r = runner(identity_evidence=identity((edge("E1", "OLD", "MID", DAY2, DAY2),)),
               valuation_policy=policy)
    first = r.step(session(
        DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
        dividend_entitlements=(DividendEntitlementInput(
            "p", terms, source(Phase.PRE_OPEN_ACTIONS, DAY1, "b")),),
        dividend_accruals=(DividendAccrualInput(
            "div-1", source(Phase.PRE_OPEN_ACTIONS, DAY1, "c")),),
        close_bars=(close_bar(OLD_KEY, DAY1),)))
    assert first.events[0].key == OLD_KEY
    assert r.snapshot.entitlements[0].key == OLD_KEY
    assert r.snapshot.claims[0].state == "accrued_held"

    payment = VerifiedPayment(
        DAY2, EventTime(DAY1, Phase.PRE_OPEN_ACTIONS), "issuer payment notice", None,
        settlement_model=GROSS_DIVIDEND_NO_WITHHOLDING_MODEL)
    second = r.step(session(
        DAY2,
        dividend_confirmations=(DividendConfirmationInput(
            "div-1", payment, source(Phase.PRE_OPEN_ACTIONS, DAY2, "d")),)))
    assert [m.migration_id for m in second.identity_migrations] == ["E1"]
    assert r.snapshot.entitlements[0].key == MID_KEY
    confirmed = next(event for event in second.events
                     if event.kind == EventKind.DIVIDEND_PAYMENT_CONFIRMED)
    assert confirmed.key == MID_KEY
    assert confirmed.amount == D("20.00")
    assert second.reconciliation.passed


def test_migration_never_fills_a_frozen_sell_from_the_retired_key_opening_print():
    from nse_pipeline.backtest.accounting_types import EventKind
    identity_evidence = identity((edge("E1", "OLD", "MID", DAY2, DAY2),))
    r = runner(identity_evidence=identity_evidence)
    r.step(session(DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
                   close_bars=(close_bar(OLD_KEY, DAY1),),
                   sell_targets=(sell_target("sell-p", "p", DAY1, 4),)))
    before = r.snapshot

    with pytest.raises(ValueError, match="Missing opening print for security MID"):
        r.step(session(DAY2, opening_prints=(opening(OLD_KEY, DAY2),)))
    assert len(r.result) == 1
    assert r.snapshot == before
    assert held(r).key == OLD_KEY
    assert held(r).shares == 10
    assert r.result[0].next_sells[0].target.order_id == "sell-p"
    assert not any(event.kind is EventKind.SELL for event in r.events)

    third = r.step(session(DAY3, opening_prints=(opening(MID_KEY, DAY3),)))
    assert [m.migration_id for m in third.identity_migrations] == ["E1"]
    sells = tuple(event for event in third.events if event.kind is EventKind.SELL)
    assert len(sells) == 1 and sells[0].key == MID_KEY
    assert held(r).key == MID_KEY
    assert held(r).shares == 6
    assert third.reconciliation.passed


def test_retirement_blocks_symbol_reuse_and_rolls_back_the_whole_session():
    from nse_pipeline.backtest.accounting_types import Phase
    r = runner(identity_evidence=identity((), (retirement("R1", "OLD", DAY2, DAY2),)))
    r.step(session(DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
                   close_bars=(close_bar(OLD_KEY, DAY1),)))
    before_snapshot = r.snapshot
    before_ledger = r.identity_ledger

    with pytest.raises(ValueError, match="observed_on_or_after_retirement"):
        r.step(session(DAY2, identity_observations=(observation("OLD", DAY2, "INE1"),)))
    assert len(r.result) == 1
    assert r.snapshot == before_snapshot
    assert r.identity_ledger == before_ledger
    assert next(key for key in r.identity_ledger.keys if key.key == OLD_KEY).retired_on is None


def test_unknown_lifecycle_blocks_nav_and_negotiation_without_inventing_a_state():
    from nse_pipeline.backtest.accounting_types import EventKind
    from nse_pipeline.backtest.lifecycle import LifecycleState
    r = runner(lifecycle_evidence=lifecycle())
    first = r.step(session(DAY1, close_bars=(close_bar(OLD_KEY, DAY1),)))
    assert first.valuation.nav is None
    assert first.valuation.unresolved_positions == ("p",)
    assert first.lifecycle_admissions[0].status.state == LifecycleState.UNKNOWN
    assert first.lifecycle_admissions[0].status.reason == "no_authoritative_evidence"
    assert held(r).shares == 10
    assert r.snapshot.claims == ()
    assert all(event.kind is not EventKind.REDEMPTION for event in r.events)

    with pytest.raises(ValueError, match="lifecycle_unknown"):
        r.step(session(DAY2, sell_targets=(sell_target("sell-p", "p", DAY2),)))
    assert len(r.result) == 1
    assert held(r).shares == 10


def test_suspended_lifecycle_blocks_a_frozen_sale_and_preserves_state():
    from nse_pipeline.backtest.accounting_types import Phase
    records = (listed(OLD_KEY),
               state_record(OLD_KEY, "SUSPENDED", DAY2, DAY2, Phase.PRE_OPEN_ACTIONS))
    r = runner(lifecycle_evidence=lifecycle(*records))
    first = r.step(session(DAY1, close_bars=(close_bar(OLD_KEY, DAY1),),
                           sell_targets=(sell_target("sell-p", "p", DAY1, 4),)))
    assert len(first.next_sells) == 1

    with pytest.raises(ValueError, match="lifecycle_suspended"):
        r.step(session(DAY2, opening_prints=(opening(OLD_KEY, DAY2),)))
    assert len(r.result) == 1
    assert held(r).shares == 10
    assert r.result[0].next_sells[0].target.order_id == "sell-p"


def test_delisted_terminal_proceeds_are_never_inferred_from_the_last_close():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.lifecycle import LifecycleState
    records = (listed(OLD_KEY),
               state_record(OLD_KEY, "DELISTED", DAY2, DAY2, Phase.PRE_OPEN_ACTIONS))
    r = runner(lifecycle_evidence=lifecycle(*records))
    first = r.step(session(DAY1, close_bars=(close_bar(OLD_KEY, DAY1),)))
    assert first.valuation.nav == D("2000.00")

    second = r.step(session(DAY2, close_bars=(close_bar(OLD_KEY, DAY2),)))
    assert second.valuation.nav is None
    assert second.valuation.unresolved_positions == ("p",)
    admission = next(item for item in second.lifecycle_admissions
                     if item.status.state is LifecycleState.DELISTED)
    assert admission.status.reason == "delisted_terminal_proceeds_unknown"
    assert admission.status.terminal_proceeds is None
    assert held(r).shares == 10
    assert r.snapshot.cash == D("1000.00")
    assert r.snapshot.claims == ()

    with pytest.raises(ValueError, match="lifecycle_delisted_terminal_proceeds_unknown"):
        r.step(session(DAY3, sell_targets=(sell_target("sell-p", "p", DAY3),)))
    assert len(r.result) == 2
    assert held(r).shares == 10

    verified = state_record(OLD_KEY, "DELISTED", DAY2, DAY2, Phase.PRE_OPEN_ACTIONS,
                            terminal_proceeds=D("50"), proceeds_verified=True)
    verified_runner = runner(lifecycle_evidence=lifecycle(listed(OLD_KEY), verified))
    verified_runner.step(session(DAY1, close_bars=(close_bar(OLD_KEY, DAY1),)))
    result = verified_runner.step(session(DAY2, close_bars=(close_bar(OLD_KEY, DAY2),)))
    status = next(item.status for item in result.lifecycle_admissions
                  if item.status.state is LifecycleState.DELISTED)
    assert status.terminal_proceeds == D("50")
    assert result.valuation.nav is None
    assert verified_runner.snapshot.cash == D("1000.00")
    with pytest.raises(ValueError, match="lifecycle_delisted_terminal_proceeds_known"):
        verified_runner.step(session(DAY3, sell_targets=(sell_target("sell-p", "p", DAY3),)))


def test_appended_future_identity_and_lifecycle_records_leave_earlier_results_unchanged():
    from nse_pipeline.backtest.accounting_types import Phase
    early_identity = identity((edge("E2", "MID", "NEW", DAY3, DAY3),))
    full_identity = identity((edge("E2", "MID", "NEW", DAY3, DAY3),
                              edge("E3", "NEW", "NEXT", DAY4, DAY4)))
    early_lifecycle = lifecycle(listed(OLD_KEY))
    full_lifecycle = lifecycle(
        listed(OLD_KEY),
        state_record(OLD_KEY, "SUSPENDED", DAY3, DAY3, Phase.PRE_OPEN_ACTIONS))

    prefix = runner(identity_evidence=early_identity, lifecycle_evidence=early_lifecycle)
    full = runner(identity_evidence=full_identity, lifecycle_evidence=full_lifecycle)
    for target in (prefix, full):
        target.step(session(DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
                            close_bars=(close_bar(OLD_KEY, DAY1),)))
        target.step(session(DAY2))

    assert prefix.result == full.result
    assert prefix.result[0].identity_resolutions == full.result[0].identity_resolutions
    assert prefix.result[0].lifecycle_admissions == full.result[0].lifecycle_admissions


def test_identity_migration_failure_late_in_the_session_rolls_back_completely():
    identity_evidence = identity((edge("E1", "OLD", "MID", DAY2, DAY2),))
    r = runner(identity_evidence=identity_evidence)
    r.step(session(DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
                   close_bars=(close_bar(OLD_KEY, DAY1),)))
    before_snapshot = r.snapshot
    before_events = r.events
    before_ledger = r.identity_ledger

    with pytest.raises(ValueError, match="Missing valid same-session close"):
        r.step(session(DAY2, buy_targets=(buy_target("buy-mid", MID_KEY, DAY2),)))
    assert len(r.result) == 1
    assert r.snapshot == before_snapshot
    assert r.events == before_events
    assert r.identity_ledger == before_ledger
    assert held(r).key == OLD_KEY


def test_prefix_metamorphic_production_identity_with_synthetic_causal_edges():
    from nse_pipeline.backtest.accounting_types import Phase
    first_edge = edge("E1", "OLD", "MID", DAY2, DAY2)
    second_edge = edge("E2", "MID", "NEW", DAY3, DAY3)

    prefix = runner(identity_evidence=identity((first_edge,)))
    full = runner(identity_evidence=identity((first_edge, second_edge)))

    day1 = session(DAY1, identity_observations=(observation("OLD", DAY1, "INE1"),),
                   close_bars=(close_bar(OLD_KEY, DAY1),))
    day2 = session(DAY2, identity_observations=(observation("MID", DAY2),))
    prefix.step(day1)
    prefix.step(day2)
    full.step(day1)
    second = full.step(day2)

    assert prefix.result == full.result
    assert second.identity_migrations[0].migration_id == "E1"
    assert second.phase_states[0].state.positions[0].key == OLD_KEY
    assert second.phase_states[2].state.positions[0].key == MID_KEY
    assert held(full).key == MID_KEY

    third = full.step(session(DAY3, identity_observations=(observation("NEW", DAY3),)))
    assert [m.migration_id for m in third.identity_migrations] == ["E2"]
    assert third.identity_migrations[0].old_key == MID_KEY
    assert third.identity_migrations[0].new_key == NEW_KEY
    assert held(full).key == NEW_KEY
    assert held(full).shares == 10
    assert full.result[0].phase_states[0].state.positions[0].key == OLD_KEY
    assert all(item.reconciliation.passed for item in full.result)


def test_optional_decision_time_caps_the_lifecycle_information_horizon():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    records = (listed(OLD_KEY),
               state_record(OLD_KEY, "SUSPENDED", DAY2, DAY2, Phase.CLOSE_VALUATION))

    capped = runner(lifecycle_evidence=lifecycle(*records))
    capped.step(session(DAY1, close_bars=(close_bar(OLD_KEY, DAY1),)))
    capped_result = capped.step(session(
        DAY2, close_bars=(close_bar(OLD_KEY, DAY2),),
        decision_at=EventTime(DAY2, Phase.PRE_OPEN_ACTIONS)))
    assert capped_result.valuation.nav == D("2000.00")
    assert all(item.status.state.value == "listed"
               for item in capped_result.lifecycle_admissions)

    uncapped = runner(lifecycle_evidence=lifecycle(*records))
    uncapped.step(session(DAY1, close_bars=(close_bar(OLD_KEY, DAY1),)))
    uncapped_result = uncapped.step(session(DAY2, close_bars=(close_bar(OLD_KEY, DAY2),)))
    assert uncapped_result.valuation.nav is None


def test_optional_inputs_are_absent_by_default_and_disclosed_as_inactive():
    r = runner()
    assert r.identity_ledger is None
    assert r.lifecycle_evidence is None
    result = r.step(session(DAY1, close_bars=(close_bar(OLD_KEY, DAY1),)))
    joined = " ".join(result.limitations)
    assert "identity migration and lifecycle admission run only from optional typed evidence" in joined
    assert "identity migrations and lifecycle state admission remain unsupported" not in joined
    assert "identity migration and retirement admission are inactive for this run" in joined
    assert "lifecycle state admission and lifecycle NAV blocking are inactive for this run" in joined
