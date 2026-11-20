"""Production components compared to literal/hand-authored golden expectations."""
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden/accounting_cases.json"


def test_reservation_and_cancel_match_golden_without_spending_cash():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    case = next(c for c in json.loads(GOLDEN.read_text())["cases"] if c["id"] == "reservation_cancel")
    book = Portfolio("1000", on=date(2024, 6, 3))
    book.reserve("order", "AAA", 3, "300", "300", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
    assert book.balances.cash == D("1000")
    assert book.balances.cash_available == D(case["events"][0]["expected"]["cash_available"])
    assert book.balances.order_reserved == D("300")
    book.observe_session(date(2024, 6, 4))
    book.cancel("order", at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS))
    assert book.balances.cash_available == D(case["events"][1]["expected"]["cash_available"])
    assert book.balances.cash == D("1000")
    assert book.balances.liabilities == D("0")
    assert book.balances.receivables == D("0")


@pytest.mark.parametrize("amount", ("-1", "NaN", "Infinity", True, .1))
def test_invalid_money_is_rejected_without_a_book(amount):
    from nse_pipeline.backtest.core import Portfolio
    with pytest.raises((ValueError, TypeError)):
        Portfolio(amount, on=date(2024, 6, 3))


def test_overreservation_duplicate_and_out_of_order_are_atomic():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    book = Portfolio("100", on=date(2024, 6, 3))
    t = EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS)
    with pytest.raises(ValueError):
        book.reserve("o", "AAA", 1, "101", "101", at=t)
    assert book.events == () and book.balances.cash_available == 100
    book.reserve("o", "AAA", 1, "100", "100", at=t)
    before = book.snapshot
    with pytest.raises(ValueError):
        book.reserve("o", "AAA", 1, "1", "1", at=EventTime(t.session, t.phase, 1))
    with pytest.raises(ValueError):
        book.cancel("o", at=EventTime(t.session, Phase.PRE_OPEN_ACTIONS))
    assert book.snapshot == before and len(book.events) == 1


def flat_costs(amount):
    from nse_pipeline.specs import CostSpec
    return CostSpec(brokerage_bps=10000 if amount else 0, brokerage_cap_per_order=amount,
                    stt_bps_buy=0, stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0,
                    gst_on_brokerage_pct=0)


def execution():
    from nse_pipeline.specs import ExecutionSpec
    return ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                         adverse_slippage_bps_sell=0)


def test_prefunded_purchase_and_settlement_match_golden():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, Claim, ClaimKind, EventTime, Phase, OpeningPrint
    from dataclasses import FrozenInstanceError

    case = next(c for c in json.loads(GOLDEN.read_text())["cases"] if c["id"] == "prefunded_purchase")
    book = Portfolio("1000", on=date(2024, 6, 3),
        positions=(Position("p", "AAA", 5, date(2024, 5, 31)),),
        claims=(Claim("opening", ClaimKind.DIVIDEND, "50", "opening fixture"),))
    book.reserve("o", "AAA", 3, "310", "300", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
    book.observe_session(date(2024, 6, 4))
    quote = book.buy("o", OpeningPrint("AAA", date(2024, 6, 4), "100"), flat_costs(10), execution(),
                     at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    assert quote.quantity == 3
    expected = case["events"][1]["expected"]
    assert book.balances.cash == D(expected["cash"])
    assert book.balances.liabilities == D(expected["liability"])
    assert book.balances.cash_available == D(expected["cash_available"])
    assert book.positions["p"].shares == 8 and book.positions["p"].delivered_shares == 5
    with pytest.raises(FrozenInstanceError):
        book.positions["p"].position_id = "changed"
    before = book.snapshot
    with pytest.raises(ValueError):
        book.settle("trade:buy:o", at=EventTime(date(2024, 6, 4), Phase.POST_OPEN_SETTLEMENT))
    assert book.snapshot == before
    book.observe_session(date(2024, 6, 5))
    book.settle("trade:buy:o", at=EventTime(date(2024, 6, 5), Phase.PRE_OPEN_SETTLEMENT))
    assert book.balances.cash == D(case["events"][2]["expected"]["cash"])
    assert book.balances.liabilities == 0 and book.positions["p"].delivered_shares == 8
    with pytest.raises(ValueError):
        book.settle("trade:buy:o", at=EventTime(date(2024, 6, 5), Phase.PRE_OPEN_SETTLEMENT, 1))


def test_unsettled_sale_is_not_cash_and_matches_golden():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase, OpeningPrint

    case = next(c for c in json.loads(GOLDEN.read_text())["cases"] if c["id"] == "unsettled_sale")
    book = Portfolio("1000", on=date(2024, 6, 4), positions=(Position("p", "AAA", 5, date(2024, 5, 31)),))
    book.sell("s", "p", 2, OpeningPrint("AAA", date(2024, 6, 4), "100"), flat_costs(1), execution(),
              at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    expected = case["events"][0]["expected"]
    assert book.balances.cash == D(expected["cash"])
    assert book.balances.cash_available == D(expected["cash_available"])
    assert book.balances.sale_receivable == D(expected["sale_receivable"])
    assert book.positions["p"].shares == 3
    book.observe_session(date(2024, 6, 5))
    book.settle("trade:sell:s", at=EventTime(date(2024, 6, 5), Phase.PRE_OPEN_SETTLEMENT))
    assert book.balances.cash == D(case["events"][1]["expected"]["cash"])
    assert book.balances.sale_receivable == 0


def test_gap_up_resizes_integer_shares_without_borrowing_or_reusing_sale_cash():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, OpeningPrint

    book = Portfolio("250", on=date(2024, 6, 3))
    book.reserve("o", "AAA", 5, "250", "250", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
    book.observe_session(date(2024, 6, 4))
    quote = book.buy("o", OpeningPrint("AAA", date(2024, 6, 4), "60"), flat_costs(0), execution(),
                     at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    assert quote.quantity == 4 and quote.notional == D("240")
    assert book.balances.cash == 250 and book.balances.cash_available == 10
    position = next(iter(book.positions.values()))
    with pytest.raises(ValueError):
        book.sell("bad", position.position_id, 1, OpeningPrint("AAA", date(2024, 6, 4), "60"),
                  flat_costs(0), execution(), at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS, 1))
    assert position.shares == 4 and position.delivered_shares == 0


def test_sale_claim_cannot_fund_a_reservation_and_liability_is_prefunded():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase, OpeningPrint, Due
    book = Portfolio("0", on=date(2024, 6, 4), positions=(Position("p", "AAA", 5, date(2024, 5, 31)),))
    book.sell("s", "p", 2, OpeningPrint("AAA", date(2024, 6, 4), "100"), flat_costs(1), execution(),
              at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    before = book.snapshot
    with pytest.raises(ValueError):
        book.reserve("o", "OTHER", 1, "199", "199", at=EventTime(date(2024, 6, 4), Phase.CLOSE_ORDERS))
    assert book.snapshot == before and book.balances.sale_receivable == 199
    liability = Portfolio("100", on=date(2024, 6, 4))
    due = Due(Phase.POST_OPEN_SETTLEMENT, on=date(2024, 6, 5))
    liability.recognize_liability("tax", "20", "verified obligation", due,
                                  at=EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION))
    assert liability.balances.cash == 100 and liability.balances.cash_available == 80
    assert liability.balances.liabilities == liability.balances.payable_reserved == 20
    liability.observe_session(date(2024, 6, 5))
    liability.settle("liability:tax", at=EventTime(date(2024, 6, 5), Phase.POST_OPEN_SETTLEMENT))
    assert liability.balances.cash == 80 and liability.balances.liabilities == 0


def test_verified_consideration_becomes_receivable_not_immediate_cash():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, Evidence, EventTime, Phase, Due
    case = next(c for c in json.loads(GOLDEN.read_text())["cases"] if c["id"] == "verified_proceeds")
    book = Portfolio("200", on=date(2024, 6, 3), positions=(Position("p", "AAA", 100, date(2024, 5, 31)),))
    book.observe_session(date(2024, 6, 4))
    proof = Evidence("verified cash consideration fixture", "AAA", date(2024, 6, 4),
                      EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION), True)
    book.redeem("r", "p", "12", proof, Due(Phase.POST_OPEN_SETTLEMENT, on=date(2024, 6, 5)),
                at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS))
    assert book.balances.cash_available == D(case["events"][1]["expected"]["cash_available"])
    assert book.balances.other_receivable == D(case["events"][1]["expected"]["other_receivable"])
    assert book.positions["p"].shares == 0
    book.observe_session(date(2024, 6, 5))
    book.settle("consideration:r", at=EventTime(date(2024, 6, 5), Phase.POST_OPEN_SETTLEMENT))
    assert book.balances.cash == D(case["events"][2]["expected"]["cash_available"])


def test_opening_sale_does_not_increase_a_prefunded_buy_budget():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase, OpeningPrint
    book = Portfolio("100", on=date(2024, 6, 3), positions=(Position("old", "AAA", 2, date(2024, 5, 31)),))
    book.reserve("o", "BBB", 3, "100", "1000", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
    book.observe_session(date(2024, 6, 4))
    book.sell("s", "old", 1, OpeningPrint("AAA", date(2024, 6, 4), "100"), flat_costs(0), execution(),
              at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    quote = book.buy("o", OpeningPrint("BBB", date(2024, 6, 4), "100"), flat_costs(0), execution(),
                     at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS, 1))
    assert quote.quantity == 1
    assert book.balances.cash == 100 and book.balances.sale_receivable == 100
    assert book.balances.liabilities == 100 and book.balances.cash_available == 0


@pytest.mark.parametrize("which", ("price", "date", "internal_id", "old_order"))
def test_invalid_opening_transition_preserves_every_balance(which):
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase, OpeningPrint
    positions = (Position("position:buy:o", "CLOSED", 0, date(2024, 5, 31)),) if which == "internal_id" else ()
    book = Portfolio("100", on=date(2024, 6, 3), positions=positions)
    book.reserve("o", "AAA", 1, "100", "100", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
    book.observe_session(date(2024, 6, 4))
    if which == "old_order":
        book.observe_session(date(2024, 6, 5))
    day = book.observed_sessions[-1]
    opening = OpeningPrint("OTHER" if which == "price" else "AAA",
                            date(2024, 6, 5) if which == "date" else day, "100")
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError):
        book.buy("o", opening, flat_costs(0), execution(), at=EventTime(day, Phase.OPEN_FILLS))
    assert book.snapshot == before and book.events == events


def test_zero_fill_and_position_cap_release_only_order_reservations():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase, OpeningPrint
    from nse_pipeline.backtest.invariants import reconcile
    for positions, budget in (((), "50"), ((Position("held", "BBB", 1, date(2024, 5, 31), blocked_reason="unresolved"),), "100")):
        book = Portfolio(budget, on=date(2024, 6, 3), positions=positions, max_positions=1)
        book.reserve("o", "AAA", 1, budget, "100", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
        book.observe_session(date(2024, 6, 4))
        quote = book.buy("o", OpeningPrint("AAA", date(2024, 6, 4), "100"), flat_costs(0), execution(),
                         at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
        assert quote.quantity == 0 and book.balances.cash_available == D(budget)
        assert book.balances.liabilities == 0 and book.balances.order_reserved == 0
        assert len([p for p in book.positions.values() if p.shares]) == len(positions)
        assert reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions, max_positions=1).passed


def test_due_settlements_must_precede_later_session_phases():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Claim, ClaimKind, Due, EventTime, Phase
    claim = Claim("sale", ClaimKind.SALE, "100", "opening sale", Due(Phase.PRE_OPEN_SETTLEMENT, on=date(2024, 6, 3)))
    book = Portfolio("50", on=date(2024, 6, 3), claims=(claim,))
    before = book.snapshot
    with pytest.raises(ValueError):
        book.reserve("o", "AAA", 1, "50", "50", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
    assert book.snapshot == before
    with pytest.raises(ValueError):
        book.observe_session(date(2024, 6, 4))
    assert book.observed_sessions == (date(2024, 6, 3),)
    book.settle("sale", at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_SETTLEMENT))
    book.observe_session(date(2024, 6, 4))
    assert book.balances.cash == 150


@pytest.mark.parametrize("mode,expected", (("partial", 1), ("skip", 0)))
def test_sales_respect_pinned_notional_and_partial_fill_controls(mode, expected):
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase, OpeningPrint
    from nse_pipeline.backtest.invariants import reconcile
    from nse_pipeline.specs import ExecutionSpec
    book = Portfolio("0", on=date(2024, 6, 4), positions=(Position("p", "AAA", 10, date(2024, 5, 31)),))
    spec = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=100,
                         partial_fill_policy=mode, adverse_slippage_bps_sell=0)
    quote = book.sell("s", "p", 10, OpeningPrint("AAA", date(2024, 6, 4), "100"),
                      flat_costs(0), spec, at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    assert quote.quantity == expected
    assert book.positions["p"].shares == 10 - expected
    assert book.balances.sale_receivable == expected * 100
    assert reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions).passed


def test_rank_sequential_funding_matches_independent_sizing_vectors():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, OpeningPrint
    vectors = json.loads((Path(__file__).parent / "golden/execution_cases.json").read_text())["sizing"]
    for case in vectors:
        book = Portfolio(case["cash"], on=date(2024, 6, 3))
        for i, desired in enumerate(case["desired_in_rank_order"]):
            # Funding is an explicit pre-open budget, not an invented sale or
            # a draw on screen-valued equity. Expected fills remain JSON literals.
            budget = min(D(desired), book.balances.cash_available)
            book.reserve(f"o{i}", f"KEY{i}", int(desired), budget, desired, rank=i,
                           at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS, i))
        book.observe_session(date(2024, 6, 4))
        actual = []
        for i in range(len(case["expected_filled"])):
            quote = book.buy(f"o{i}", OpeningPrint(f"KEY{i}", date(2024, 6, 4), "1"),
                             flat_costs(0), execution(), at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS, i))
            actual.append(quote.notional)
        assert actual == [D(x) for x in case["expected_filled"]]
        assert book.balances.cash_available == D(case["expected_cash"])
        assert book.balances.cash == D(case["cash"])  # still settled cash until payment
        book.observe_session(date(2024, 6, 5))
        for i in range(len(case["expected_filled"])):
            book.settle(f"trade:buy:o{i}", at=EventTime(date(2024, 6, 5), Phase.PRE_OPEN_SETTLEMENT, i))
        assert book.balances.cash == D(case["expected_cash"]) and book.balances.liabilities == 0


def test_subpaise_cap_normalization_uses_floor_and_agrees_with_independent_replay():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase, OpeningPrint
    from nse_pipeline.backtest.invariants import reconcile
    from nse_pipeline.specs import ExecutionSpec
    spec = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=10.005,
                         adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    # D-01: a hard cap of 10.005 floors to 10.00; it never rounds half-up to 10.01.
    rejected = Portfolio("0", on=date(2024, 6, 4), positions=(Position("p", "AAA", 1, date(2024, 5, 31)),))
    quote = rejected.sell("s", "p", 1, OpeningPrint("AAA", date(2024, 6, 4), "10.01"), flat_costs(0), spec,
                          at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    assert quote.applied_principal_cap == D("10.00")
    assert quote.quantity == 0 and quote.notional == 0
    assert quote.reason == "notional_limit_or_partial_disallowed"
    assert reconcile(rejected.initial, rejected.events, rejected.snapshot,
                     sessions=rejected.observed_sessions).passed

    filled = Portfolio("0", on=date(2024, 6, 4), positions=(Position("p", "AAA", 1, date(2024, 5, 31)),))
    quote = filled.sell("s", "p", 1, OpeningPrint("AAA", date(2024, 6, 4), "10.00"), flat_costs(0), spec,
                        at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    assert quote.applied_principal_cap == D("10.00")
    assert quote.quantity == 1 and quote.notional == D("10.00")
    assert reconcile(filled.initial, filled.events, filled.snapshot, sessions=filled.observed_sessions).passed


@pytest.mark.parametrize("side", ("sell", "buy"))
def test_replay_rejects_old_half_up_cap_path(side):
    from dataclasses import replace
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, EventTime, Phase, OpeningPrint
    from nse_pipeline.backtest.invariants import reconcile
    from nse_pipeline.specs import ExecutionSpec

    # The two fills below are legitimate under the inclusive 100.00 cap. Mutating
    # only the pinned cap to 99.996 must be rejected by the independent replay:
    # 100.00 principal exceeds the floored 99.99, although old money() half-up
    # would have rounded 99.996 up to 100.00 and accepted the fill.
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=100,
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    if side == "sell":
        book = Portfolio("0", on=date(2024, 6, 4),
                         positions=(Position("p", "AAA", 10, date(2024, 5, 31)),))
        quote = book.sell("s", "p", 10, OpeningPrint("AAA", date(2024, 6, 4), "10.00"),
                          flat_costs(0), execution, at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
        trade_index = 0
    else:
        book = Portfolio("10000", on=date(2024, 6, 3))
        book.reserve("o", "AAA", 10, "100", "100", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
        book.observe_session(date(2024, 6, 4))
        quote = book.buy("o", OpeningPrint("AAA", date(2024, 6, 4), "10.00"),
                         flat_costs(0), execution, at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
        trade_index = 1
    assert quote.quantity == 10 and quote.notional == D("100.00")
    assert reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions).passed

    events = list(book.events)
    events[trade_index] = replace(
        events[trade_index],
        execution_spec=replace(events[trade_index].execution_spec, max_order_notional=99.996))
    result = reconcile(book.initial, tuple(events), book.snapshot, sessions=book.observed_sessions)
    assert not result.passed
    assert result.checks[0].status == "FAIL"
    assert "maximum order notional" in result.checks[0].detail
