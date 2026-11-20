"""Predeclared opening-price conditions, frozen before the next session opens."""
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal as D

import pytest

D0 = date(2024, 6, 3)
D1 = date(2024, 6, 4)
D2 = date(2024, 6, 5)
ZERO = D("0.00")


def source(phase, *, day=D0, character="a"):
    from nse_pipeline.backtest.accounting_types import EventTime
    from nse_pipeline.backtest.observed_session import InputProvenance
    digest = (character.encode("utf-8").hex() * 32)[:64]
    return InputProvenance("supplied fixture", digest, EventTime(day, phase),
                           "supplied fixture; not authenticated")


def costs(*, charged=False):
    from nse_pipeline.specs import CostSpec
    if not charged:
        return CostSpec(brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0,
                        stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0,
                        gst_on_brokerage_pct=0)
    return CostSpec(brokerage_bps=100, brokerage_cap_per_order=1000, stt_bps_buy=100,
                    stt_bps_sell=100, stamp_bps_buy=100, exchange_bps=100,
                    gst_on_brokerage_pct=18)


def execution(*, buy_slip=0, sell_slip=0):
    from nse_pipeline.specs import ExecutionSpec
    return ExecutionSpec(trade_settlement_lag_sessions=1,
                         adverse_slippage_bps_buy=buy_slip,
                         adverse_slippage_bps_sell=sell_slip,
                         sizing_participation_rate=0.05, max_participation_rate=0.05,
                         min_order_notional=0.0, partial_fill_policy="partial",
                         unfilled_remainder="expire")


def runner(*, cash="2000", positions=(), costs_spec=None, execution_spec=None):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionRunner
    return SessionRunner(
        cash, on=D0, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
        opening_positions=positions, max_positions=10,
        costs=costs_spec or costs(), execution=execution_spec or execution())


def close_bar(price="100", *, day=D0, character="b"):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import CloseBar
    from nse_pipeline.backtest.observations import Bar
    price = D(price)
    return CloseBar(Bar("AAA", "AAA", "EQ", day, price, price, price, price, 100, True),
                    source(Phase.CLOSE_VALUATION, day=day, character=character))


def opening(price, *, day=D1, character="c"):
    from nse_pipeline.backtest.accounting_types import OpeningPrint, Phase
    from nse_pipeline.backtest.observed_session import OpeningObservation
    return OpeningObservation(OpeningPrint("AAA", day, D(price)),
                              source(Phase.OPEN_FILLS, day=day, character=character))


def buy_target(condition=None, *, order_id="order-aaa", quantity=10,
               budget="1000", capacity="1000", rank=0, day=D0, character="d"):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import BuyTarget, CapacityInput
    return BuyTarget(order_id, "AAA", quantity, D(budget),
                     CapacityInput(D(capacity), source(Phase.CLOSE_ORDERS, day=day,
                                                      character=character)),
                     rank, source(Phase.CLOSE_ORDERS, day=day, character=character),
                     condition)


def sell_target(condition=None, *, order_id="sell-aaa", position_id="held",
                quantity=10, rank=0, day=D0):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SellTarget
    return SellTarget(order_id, position_id, quantity, rank,
                      source(Phase.CLOSE_ORDERS, day=day), condition)


def frozen_buy(condition, *, charges=False, slip=0):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput
    book = runner(costs_spec=costs(charged=charges),
                  execution_spec=execution(buy_slip=slip))
    first = book.step(SessionInput(
        D0, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(close_bar("100"),),
        buy_targets=(buy_target(condition),),
    ))
    return book, first


def through_buy(condition, open_price, *, charges=False, slip=0, close_price="100"):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput
    book, first = frozen_buy(condition, charges=charges, slip=slip)
    result = book.step(SessionInput(
        D1, source(Phase.PRE_OPEN_EVIDENCE, day=D1, character="e"),
        opening_prints=(opening(open_price),),
        close_bars=(close_bar(close_price, day=D1, character="f"),),
    ))
    return book, first, result


def held_runner(*, charges=False, slip=0):
    from nse_pipeline.backtest.accounting_types import Position
    return runner(positions=(Position("held", "AAA", 10, date(2024, 5, 31)),),
                  costs_spec=costs(charged=charges),
                  execution_spec=execution(sell_slip=slip))


def frozen_sell(condition, *, charges=False, slip=0):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput
    book = held_runner(charges=charges, slip=slip)
    first = book.step(SessionInput(
        D0, source(Phase.PRE_OPEN_EVIDENCE),
        sell_targets=(sell_target(condition),),
    ))
    return book, first


def through_sell(condition, open_price, *, charges=False, slip=0):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput
    book, first = frozen_sell(condition, charges=charges, slip=slip)
    result = book.step(SessionInput(
        D1, source(Phase.PRE_OPEN_EVIDENCE, day=D1, character="c"),
        opening_prints=(opening(open_price, character="d"),),
    ))
    return book, first, result


def core_buy(condition, open_price):
    from nse_pipeline.backtest.accounting_types import EventTime, OpeningPrint, Phase
    from nse_pipeline.backtest.core import Portfolio
    book = Portfolio("2000", on=D0)
    book.reserve("order-aaa", "AAA", 10, "1000", "1000",
                 at=EventTime(D0, Phase.CLOSE_ORDERS), condition=condition)
    book.observe_session(D1)
    book.buy("order-aaa", OpeningPrint("AAA", D1, D(open_price)), costs(), execution(),
             at=EventTime(D1, Phase.OPEN_FILLS))
    return book


def core_sell(condition, open_price):
    from nse_pipeline.backtest.accounting_types import (
        EventTime, OpeningPrint, Phase, Position)
    from nse_pipeline.backtest.core import Portfolio
    book = Portfolio("2000", on=D1,
                     positions=(Position("held", "AAA", 10, date(2024, 5, 31)),))
    book.sell("sell-aaa", "held", 10, OpeningPrint("AAA", D1, D(open_price)),
              costs(), execution(), at=EventTime(D1, Phase.OPEN_FILLS),
              condition=condition)
    return book


def test_condition_bounds_are_exact_inclusive_and_reject_invalid_inputs():
    from nse_pipeline.backtest.accounting_types import OpeningCondition

    condition = OpeningCondition(min_price="100", max_price=D("110.00"))
    assert condition.min_price == D("100") and condition.max_price == D("110.00")
    assert condition.met(D("100")) and condition.met(D("110.00"))
    assert not condition.met(D("99.999")) and not condition.met(D("110.001"))

    minimum_only = OpeningCondition(min_price=100)
    maximum_only = OpeningCondition(max_price="99.50")
    assert minimum_only.met(D("100")) and not minimum_only.met(D("99.99"))
    assert maximum_only.met(D("99.50")) and not maximum_only.met(D("99.51"))

    with pytest.raises(ValueError):
        OpeningCondition()
    with pytest.raises(ValueError):
        OpeningCondition(min_price="110", max_price="100")
    for invalid in (0, -1, "0", "-1"):
        with pytest.raises(ValueError):
            OpeningCondition(min_price=invalid)
    for invalid in (100.0, True, False):
        with pytest.raises((TypeError, ValueError)):
            OpeningCondition(min_price=invalid)


def test_buy_condition_passes_and_fails_at_inclusive_bounds_and_releases_once():
    from nse_pipeline.backtest.accounting_types import EventKind, EventTime, OpeningCondition, Phase

    for condition, open_price, should_fill in (
            (OpeningCondition(min_price="100"), "100", True),
            (OpeningCondition(max_price="100"), "100", True),
            (OpeningCondition(min_price="100"), "99.99", False),
            (OpeningCondition(max_price="100"), "100.01", False),
    ):
        book, first, result = through_buy(condition, open_price, charges=True)
        assert first.next_orders[0].condition == condition
        reserve = next(event for event in first.events if event.kind == EventKind.RESERVE)
        assert reserve.condition == condition and reserve.condition_met is None
        trade = next(event for event in result.events
                     if event.kind in (EventKind.BUY, EventKind.BUY_SKIPPED))
        assert trade.condition == condition
        assert trade.condition_met is should_fill
        assert trade.at.phase == Phase.OPEN_FILLS
        assert trade.reference_price == D(open_price)
        if should_fill:
            assert trade.kind == EventKind.BUY
            assert trade.quantity > 0 and trade.amount > 0 and trade.claim_id
            assert book.snapshot.positions[0].shares == trade.quantity
        else:
            assert trade.kind == EventKind.BUY_SKIPPED
            assert (trade.quantity, trade.amount, trade.fee, trade.claim_id) == (0, ZERO, ZERO, "")
            assert all(amount == ZERO for _, amount in trade.fees)
            assert len(trade.postings) == 1
            assert trade.postings[0].account == "order_reserved"
            assert trade.postings[0].amount == -first.next_orders[0].reserved
            assert trade.released_reserve == first.next_orders[0].reserved
            assert result.next_orders == () and book.snapshot.positions == ()
            assert book.snapshot.claims == ()
            assert book.snapshot.balances.order_reserved == ZERO
            assert book.snapshot.balances.cash_available == D("2000")
            assert not [event for event in result.events if event.kind == EventKind.CANCEL]
        assert result.reconciliation.passed, result.reconciliation.checks


def test_buy_condition_uses_raw_open_not_slipped_fill_price():
    from nse_pipeline.backtest.accounting_types import EventKind, OpeningCondition

    # 10% buy slippage would make a 100.00 raw open look like 110.00 at fill time.
    book, _, result = through_buy(OpeningCondition(min_price="105"), "100", slip=1000)
    skipped = next(event for event in result.events if event.kind == EventKind.BUY_SKIPPED)
    assert skipped.condition_met is False
    assert skipped.reference_price == D("100") and skipped.price == D("110.0")
    assert book.snapshot.balances.cash_available == D("2000")

    # The same raw open passes a max bound and then fills at the slipped price.
    book, _, result = through_buy(OpeningCondition(max_price="100"), "100", slip=1000)
    filled = next(event for event in result.events if event.kind == EventKind.BUY)
    assert filled.condition_met is True
    assert filled.reference_price == D("100") and filled.price == D("110.0")
    assert filled.quantity > 0


def test_sell_condition_pass_and_fail_never_creates_a_fee_or_claim_on_failure():
    from nse_pipeline.backtest.accounting_types import EventKind, OpeningCondition, Phase

    book, first, result = through_sell(OpeningCondition(max_price="100"), "101",
                                       charges=True)
    assert first.next_sells[0].target.condition == OpeningCondition(max_price="100")
    skipped = next(event for event in result.events if event.kind == EventKind.SELL_SKIPPED)
    assert skipped.condition_met is False and skipped.reason == "opening_condition_unmet"
    assert skipped.at.phase == Phase.OPEN_FILLS
    assert (skipped.quantity, skipped.amount, skipped.fee, skipped.claim_id) == (0, ZERO, ZERO, "")
    assert skipped.requested_quantity == 10
    assert skipped.postings == () and skipped.due is None
    assert all(amount == ZERO for _, amount in skipped.fees)
    assert book.snapshot.claims == () and book.snapshot.balances.sale_receivable == ZERO
    assert book.snapshot.balances.cash == book.snapshot.balances.cash_available == D("2000")
    held = book.snapshot.positions[0]
    assert (held.shares, held.delivered_shares) == (10, 10)
    assert result.reconciliation.passed, result.reconciliation.checks

    # A raw-open pass fills even though sell slippage lowers the execution price.
    book, _, result = through_sell(OpeningCondition(min_price="100"), "100",
                                   charges=True, slip=1000)
    filled = next(event for event in result.events if event.kind == EventKind.SELL)
    assert filled.condition_met is True
    assert filled.reference_price == D("100") and filled.price == D("90.0")
    assert filled.quantity == 10 and filled.claim_id
    assert book.snapshot.positions[0].shares == 0
    assert result.reconciliation.passed, result.reconciliation.checks


def test_missing_opening_print_still_requires_explicit_halt_with_a_condition():
    from nse_pipeline.backtest.accounting_types import EventKind, OpeningCondition, Phase
    from nse_pipeline.backtest.observed_session import HaltEvidence, SessionInput

    condition = OpeningCondition(max_price="100")
    book, first = frozen_buy(condition)
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="Missing opening print for security AAA"):
        book.step(SessionInput(
            D1, source(Phase.PRE_OPEN_EVIDENCE, day=D1, character="e"),
        ))
    assert (book.snapshot, book.events, book.result) == before

    halt = source(Phase.OPEN_FILLS, day=D1, character="f")
    result = book.step(SessionInput(
        D1, source(Phase.PRE_OPEN_EVIDENCE, day=D1, character="e"),
        halt_evidence=(HaltEvidence("order-aaa", halt),),
    ))
    assert [event.kind for event in result.events] == [EventKind.CANCEL]
    assert result.expired_orders[0].order_id == "order-aaa"
    assert halt in result.consumed_provenance
    assert not [event for event in result.events
                if event.kind in (EventKind.BUY, EventKind.BUY_SKIPPED)]


def test_later_close_bar_perturbation_cannot_change_the_opening_condition_outcome():
    from nse_pipeline.backtest.accounting_types import OpeningCondition

    stored = []
    for close_price in ("50", "250"):
        book, _, result = through_buy(OpeningCondition(max_price="100"), "101",
                                      close_price=close_price)
        stored.append((book.events, result.events, result.next_orders, result.reconciliation))
    assert stored[0] == stored[1]
    assert all(event.condition_met is False for event in stored[0][1]
               if event.condition is not None)


def test_condition_objects_and_targets_are_frozen_and_buy_cannot_replace_condition_at_fill():
    from nse_pipeline.backtest.accounting_types import (
        EventKind, EventTime, OpeningCondition, OpeningPrint, Phase)
    from nse_pipeline.backtest.core import Portfolio

    condition = OpeningCondition(max_price="100")
    with pytest.raises(FrozenInstanceError):
        condition.max_price = D("101")
    target = buy_target(condition)
    with pytest.raises(FrozenInstanceError):
        target.condition = OpeningCondition(max_price="101")
    with pytest.raises(ValueError):
        buy_target("not-a-condition")
    with pytest.raises(ValueError):
        sell_target(123)

    book = Portfolio("1000", on=D0)
    with pytest.raises(ValueError):
        book.reserve("bad", "AAA", 1, "100", "100",
                     at=EventTime(D0, Phase.CLOSE_ORDERS), condition="not-a-condition")
    assert book.events == ()
    book.reserve("order", "AAA", 1, "100", "100",
                 at=EventTime(D0, Phase.CLOSE_ORDERS), condition=condition)
    reserve = book.events[-1]
    assert reserve.kind == EventKind.RESERVE and reserve.condition is condition
    book.observe_session(D1)
    with pytest.raises(TypeError):
        book.buy("order", OpeningPrint("AAA", D1, "100"), costs(), execution(),
                 at=EventTime(D1, Phase.OPEN_FILLS),
                 condition=OpeningCondition(max_price="90"))
    assert book.snapshot.orders[0].condition is condition


def test_failed_condition_step_rolls_back_its_skip_event_atomically():
    from nse_pipeline.backtest.accounting_types import OpeningCondition, Phase
    from nse_pipeline.backtest.observed_session import SessionInput

    book, _ = frozen_buy(OpeningCondition(max_price="100"))
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="unaffordable at decision close"):
        book.step(SessionInput(
            D1, source(Phase.PRE_OPEN_EVIDENCE, day=D1, character="e"),
            opening_prints=(opening("101"),),
            close_bars=(close_bar("100", day=D1, character="f"),),
            buy_targets=(buy_target(quantity=100, budget="1", capacity="100",
                                    day=D1, character="g"),),
        ))
    assert (book.snapshot, book.events, book.result) == before
    assert book.snapshot.balances.order_reserved == D("1000.00")


def test_replay_rejects_coherent_condition_and_reserve_release_tampering():
    from nse_pipeline.backtest.accounting_types import EventKind, OpeningCondition
    from nse_pipeline.backtest.invariants import reconcile

    book = core_buy(OpeningCondition(max_price="100"), "101")
    events = list(book.events)
    reserve_index = next(i for i, event in enumerate(events)
                         if event.kind == EventKind.RESERVE)
    skip_index = next(i for i, event in enumerate(events)
                      if event.kind == EventKind.BUY_SKIPPED)

    def rejected(changed_events, message):
        result = reconcile(book.initial, tuple(changed_events), book.snapshot,
                           sessions=book.observed_sessions)
        assert not result.passed
        assert result.checks[0].status == "FAIL"
        assert message in result.checks[0].detail

    changed = list(events)
    changed[reserve_index] = replace(changed[reserve_index],
                                     condition=OpeningCondition(max_price="90"))
    rejected(changed, "condition was replaced")

    changed = list(events)
    changed[skip_index] = replace(changed[skip_index], condition_met=True)
    rejected(changed, "Buy condition outcome")

    changed = list(events)
    changed[skip_index] = replace(changed[skip_index], released_reserve=D("1.00"))
    rejected(changed, "Fill exceeds its frozen order")

    changed = list(events)
    changed[skip_index] = replace(changed[skip_index], postings=())
    rejected(changed, "postings differ")


def test_replay_rejects_tampered_sell_condition_outcome():
    from nse_pipeline.backtest.accounting_types import EventKind, OpeningCondition
    from nse_pipeline.backtest.invariants import reconcile

    book = core_sell(OpeningCondition(max_price="100"), "101")
    events = list(book.events)
    index = next(i for i, event in enumerate(events) if event.kind == EventKind.SELL_SKIPPED)
    events[index] = replace(events[index], condition=OpeningCondition(max_price="102"),
                            condition_met=False)
    result = reconcile(book.initial, tuple(events), book.snapshot,
                       sessions=book.observed_sessions)
    assert not result.passed
    assert "Sell condition outcome" in result.checks[0].detail


def test_condition_prefix_is_stable_when_a_later_session_is_appended():
    from nse_pipeline.backtest.accounting_types import OpeningCondition, Phase
    from nse_pipeline.backtest.observed_session import SessionInput

    def run(include_later):
        book, first = frozen_buy(OpeningCondition(max_price="100"))
        second = book.step(SessionInput(
            D1, source(Phase.PRE_OPEN_EVIDENCE, day=D1, character="e"),
            opening_prints=(opening("101"),),
            close_bars=(close_bar("100", day=D1, character="f"),),
        ))
        prefix = (first, second)
        if include_later:
            book.step(SessionInput(
                D2, source(Phase.PRE_OPEN_EVIDENCE, day=D2, character="g"),
                close_bars=(close_bar("100", day=D2, character="h"),),
            ))
        return prefix, book

    prefix, short = run(False)
    appended, full = run(True)
    assert appended == prefix
    assert short.result == prefix
    assert full.events[:len(short.events)] == short.events


def test_unconditioned_events_keep_optional_condition_defaults():
    from nse_pipeline.backtest.accounting_types import (
        BuyOrder, EventKind, EventTime, OpeningPrint, Phase, Position)
    from nse_pipeline.backtest.core import Portfolio

    book = Portfolio("1000", on=D0,
                     positions=(Position("held", "AAA", 2, date(2024, 5, 31)),))
    book.reserve("order", "AAA", 1, "100", "100", at=EventTime(D0, Phase.CLOSE_ORDERS))
    book.observe_session(D1)
    book.sell("sale", "held", 1, OpeningPrint("AAA", D1, "100"), costs(), execution(),
              at=EventTime(D1, Phase.OPEN_FILLS))
    book.buy("order", OpeningPrint("AAA", D1, "100"), costs(), execution(),
             at=EventTime(D1, Phase.OPEN_FILLS, 1))
    assert all(event.condition is None and event.condition_met is None
               for event in book.events
               if event.kind in (EventKind.RESERVE, EventKind.SELL, EventKind.BUY))
    assert all(order.condition is None for order in book.initial.orders)
    assert BuyOrder("manual", "AAA", 1, D("100"), D("100"),
                    EventTime(D0, Phase.CLOSE_ORDERS), 0).condition is None
