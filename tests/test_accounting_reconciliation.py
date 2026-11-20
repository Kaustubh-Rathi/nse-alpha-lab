"""All eight golden accounting vectors exercise production operations and replay.

Point valuations use the fixture's explicit hypothetical current quote at the
close. The split's mechanical 50 mark is a unit-conservation control, not a
claim that an ex-date close was available pre-open. No valuation sizes an order.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

CASES = {c["id"]: c for c in json.loads((Path(__file__).parent / "golden/accounting_cases.json").read_text())["cases"]}


def run_case(case_id):
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Claim, ClaimKind, Position, Evidence, Due, EventTime, Phase, OpeningPrint
    from nse_pipeline.backtest.dividends import DividendTerms
    from nse_pipeline.backtest.share_actions import ShareAction
    from nse_pipeline.backtest.valuation import Mark, value
    from nse_pipeline.specs import CostSpec, ExecutionSpec, ValuationPolicy

    case = CASES[case_id]
    initial = case["initial"]
    rows = case["events"]
    first_day = date.fromisoformat(rows[0]["session"])
    positions = (Position("p", "AAA", int(initial["shares"]), date(2024, 5, 31)),) if initial.get("shares") else ()
    claims = (Claim("opening", ClaimKind.DIVIDEND, initial["dividend_receivable"], "opening fixture"),) if initial.get("dividend_receivable") else ()
    book = Portfolio(initial.get("cash", "0"), on=first_day, positions=positions, claims=claims)
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    def costs(cap):
        return CostSpec(brokerage_bps=10000 if cap else 0, brokerage_cap_per_order=cap,
                        stt_bps_buy=0, stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)
    mark = initial.get("mark")
    for row in rows:
        day = date.fromisoformat(row["session"])
        if day > book.observed_sessions[-1]:
            book.observe_session(day)
        at = EventTime(day, Phase(row["phase"]), row["sequence"])
        row_id = row["id"]
        proof = Evidence("verified fixture", "AAA", day, EventTime(date(2024, 5, 31), Phase.CLOSE_VALUATION), True)
        if row_id == "reserve":
            amount = "310" if case_id == "prefunded_purchase" else "300"
            book.reserve("o", "AAA", 3, amount, "300", at=at)
        elif row_id == "cancel":
            book.cancel("o", at=at)
        elif row_id == "buy":
            book.buy("o", OpeningPrint("AAA", day, "100"), costs(10), execution, at=at)
        elif row_id == "sell":
            book.sell("s", "p", 2, OpeningPrint("AAA", day, "100"), costs(1), execution, at=at)
        elif row_id == "settle_buy":
            book.settle("trade:buy:o", at=at)
        elif row_id == "settle_sale":
            book.settle("trade:sell:s", at=at)
        elif row_id == "split":
            book.apply_share_action("p", ShareAction("a", "split", 2, 1, proof), at=at)
        elif row_id == "accrue":
            book.entitle_dividend("p", DividendTerms("d", "2", proof),
                ValuationPolicy(fallback_lag=2, lag_unit="exchange_sessions"), at=EventTime(day, at.phase))
            book.accrue_dividend("d", at=at)
        elif row_id == "settle_dividend":
            book.settle("dividend:d", at=at)
        elif row_id == "verified_consideration":
            book.redeem("r", "p", "12", proof, Due(Phase.POST_OPEN_SETTLEMENT, on=date(2024, 6, 5)), at=at)
        elif row_id == "settle_consideration":
            book.settle("consideration:r", at=at)
        if "mark" in row:
            mark = row["mark"]
        close = EventTime(day, Phase.CLOSE_VALUATION)
        valuation_at = max(close, book.snapshot.as_of)
        result = value(book.snapshot, valuation_at, marks=() if mark is None or not positions else (Mark("p", mark, day, close),))
        balances = book.balances
        actual = {"cash": balances.cash, "cash_available": balances.cash_available,
                  "order_reserve": balances.order_reserved, "payable_reserve": balances.payable_reserved,
                  "liability": balances.liabilities, "dividend_receivable": balances.dividend_receivable,
                  "sale_receivable": balances.sale_receivable, "other_receivable": balances.other_receivable,
                  "shares": sum(p.shares for p in book.positions.values()), "nav": result.nav}
        for name, expected in row["expected"].items():
            assert actual[name] == (None if expected is None else D(expected)), (case_id, row_id, name)
    return book


@pytest.mark.parametrize("case_id", tuple(CASES))
def test_all_golden_cases_reconcile_from_typed_production_events(case_id):
    from nse_pipeline.backtest.invariants import reconcile
    book = run_case(case_id)
    result = reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions)
    assert result.passed, result.checks
    assert all(check.status == "PASS" for check in result.checks)


def test_reconciliation_detects_cash_tampering_missing_and_duplicate_events():
    from nse_pipeline.backtest.invariants import reconcile
    book = run_case("prefunded_purchase")
    changed = replace(book.snapshot, cash=book.snapshot.cash - 1)
    assert not reconcile(book.initial, book.events, changed, sessions=book.observed_sessions).passed
    assert not reconcile(book.initial, book.events[:-1], book.snapshot, sessions=book.observed_sessions).passed
    assert not reconcile(book.initial, book.events + (book.events[-1],), book.snapshot,
                         sessions=book.observed_sessions).passed


def test_reconciliation_recomputes_event_recipes_not_just_posting_sums():
    from nse_pipeline.backtest.invariants import reconcile
    from nse_pipeline.backtest.accounting_types import Posting
    book = run_case("prefunded_purchase")
    events = list(book.events)
    buy = events[1]
    events[1] = replace(buy, postings=buy.postings + (Posting("cash", D("-10")),))
    bad = reconcile(book.initial, tuple(events), book.snapshot, sessions=book.observed_sessions)
    assert not bad.passed and bad.checks[0].status == "FAIL"
    events[1] = replace(buy, fee=D("0"))
    assert not reconcile(book.initial, tuple(events), book.snapshot, sessions=book.observed_sessions).passed


def test_empty_event_stream_cannot_certify_invalid_opening_delivery():
    from nse_pipeline.backtest.accounting_types import BookState, Position, EventTime, Phase
    from nse_pipeline.backtest.invariants import reconcile
    invalid = BookState(D("100"), positions=(Position("p", "AAA", 1, date(2024, 5, 31), delivered_shares=0),),
                        as_of=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_EVIDENCE))
    assert not reconcile(invalid, (), invalid, sessions=(date(2024, 6, 3),)).passed


def test_replay_rejects_fill_when_pinned_execution_limits_are_tampered():
    from nse_pipeline.backtest.invariants import reconcile
    book = run_case("prefunded_purchase")
    events = list(book.events)
    events[1] = replace(events[1], execution_spec=replace(events[1].execution_spec, max_order_notional=10))
    assert not reconcile(book.initial, tuple(events), book.snapshot, sessions=book.observed_sessions).passed


def test_empty_event_stream_cannot_hide_a_missed_settlement_phase():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Claim, ClaimKind, Due, EventTime, Phase
    from nse_pipeline.backtest.invariants import reconcile
    book = Portfolio("50", on=date(2024, 6, 3), claims=(Claim("sale", ClaimKind.SALE, "100", "opening",
        Due(Phase.PRE_OPEN_SETTLEMENT, on=date(2024, 6, 3))),))
    invalid = replace(book.initial, as_of=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_EVIDENCE))
    result = reconcile(book.initial, (), invalid, sessions=(date(2024, 6, 3), date(2024, 6, 4)))
    assert not result.passed
