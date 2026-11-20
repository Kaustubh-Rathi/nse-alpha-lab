from datetime import date
from decimal import Decimal as D
from fractions import Fraction
import json
from pathlib import Path

import pytest


def seeded(shares=100):
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position
    return Portfolio("0", on=date(2024, 6, 4), positions=(Position("p", "AAA", shares, date(2024, 5, 31)),))


def evidence(*, verified=True, key="AAA", available=None):
    from nse_pipeline.backtest.accounting_types import Evidence, EventTime, Phase
    return Evidence("issuer document fixture", key, date(2024, 6, 4),
                    available or EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION), verified)


@pytest.mark.parametrize("kind,numerator,denominator", [("split", 2, 1), ("bonus", 1, 1)])
def test_split_bonus_match_golden_raw_share_transformations(kind, numerator, denominator):
    from nse_pipeline.backtest.share_actions import ShareAction
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    book = seeded()
    book.apply_share_action("p", ShareAction("a", kind, numerator, denominator, evidence()),
                            at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS))
    assert book.positions["p"].shares == 200
    assert book.positions["p"].delivered_shares == 200
    assert book.positions["p"].position_id == "p" and book.balances.cash == 0
    cases = json.loads((Path(__file__).parent / "golden/accounting_cases.json").read_text())["cases"]
    for name, price in (("split_up", "51"), ("split_down", "49")):
        case = next(c for c in cases if c["id"] == name)
        assert book.positions["p"].shares * D(price) == D(case["events"][1]["expected"]["nav"])
    before = book.snapshot
    with pytest.raises(ValueError):
        book.apply_share_action("p", ShareAction("a", kind, numerator, denominator, evidence()),
            at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS, 1))
    assert book.snapshot == before


def test_fractional_entitlement_is_quarantined_without_rounding_or_releasing_position():
    from nse_pipeline.backtest.share_actions import ShareAction
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    book = seeded(1)
    book.apply_share_action("p", ShareAction("fractional", "bonus", 1, 2, evidence()),
                            at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS))
    assert book.positions["p"].shares == 1
    assert book.positions["p"].blocked_reason == "fractional_entitlement_unsupported"
    assert book.balances.cash == 0 and book.balances.receivables == 0


def test_wrong_security_or_future_evidence_cannot_change_a_position():
    from nse_pipeline.backtest.share_actions import ShareAction
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    book = seeded()
    before = book.snapshot
    for proof in (evidence(key="OTHER"), evidence(available=EventTime(date(2024, 6, 5), Phase.CLOSE_VALUATION))):
        with pytest.raises(ValueError):
            book.apply_share_action("p", ShareAction("a", "split", 2, 1, proof),
                                    at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS))
        assert book.snapshot == before
    book.apply_share_action("p", ShareAction("unverified", "split", 2, 1, evidence(verified=False)),
                            at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS))
    assert book.positions["p"].shares == 100 and book.positions["p"].blocked_reason == "unverified_action_terms"


def test_split_updates_unsettled_delivery_and_cancels_old_share_unit_orders():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position, Evidence, EventTime, Phase, OpeningPrint
    from nse_pipeline.backtest.share_actions import ShareAction
    from nse_pipeline.backtest.invariants import reconcile
    from nse_pipeline.specs import CostSpec, ExecutionSpec
    book = Portfolio("1000", on=date(2024, 6, 3), positions=(Position("p", "AAA", 1, date(2024, 5, 31)),))
    costs = CostSpec(brokerage_bps=0, stt_bps_buy=0, stt_bps_sell=0, stamp_bps_buy=0,
                     exchange_bps=0, gst_on_brokerage_pct=0)
    execution = ExecutionSpec(trade_settlement_lag_sessions=2, adverse_slippage_bps_buy=0)
    book.reserve("o", "AAA", 2, "200", "200", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
    book.observe_session(date(2024, 6, 4))
    book.buy("o", OpeningPrint("AAA", date(2024, 6, 4), "100"), costs, execution,
               at=EventTime(date(2024, 6, 4), Phase.OPEN_FILLS))
    book.reserve("stale_units", "AAA", 1, "100", "100", at=EventTime(date(2024, 6, 4), Phase.CLOSE_ORDERS))
    book.observe_session(date(2024, 6, 5))
    proof = Evidence("verified split", "AAA", date(2024, 6, 5),
                      EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION), True)
    book.apply_share_action("p", ShareAction("a", "split", 2, 1, proof),
                            at=EventTime(date(2024, 6, 5), Phase.PRE_OPEN_ACTIONS))
    assert book.positions["p"].shares == 6 and book.positions["p"].delivered_shares == 2
    assert book.snapshot.claims[0].delivery_shares == 4
    assert book.balances.liabilities == 200 and book.balances.cash_available == 800
    assert book.snapshot.orders == ()
    book.observe_session(date(2024, 6, 6))
    book.settle("trade:buy:o", at=EventTime(date(2024, 6, 6), Phase.PRE_OPEN_SETTLEMENT))
    assert book.positions["p"].delivered_shares == 6 and book.balances.cash == 800
    assert reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions).passed


def test_action_cancels_prior_close_order_even_when_security_is_not_held():
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.share_actions import ShareAction
    from nse_pipeline.backtest.invariants import reconcile
    book = Portfolio("1000", on=date(2024, 6, 3))
    book.reserve("new", "AAA", 10, "1000", "1000", at=EventTime(date(2024, 6, 3), Phase.CLOSE_ORDERS))
    book.observe_session(date(2024, 6, 4))
    book.apply_share_action(None, ShareAction("a", "split", 2, 1, evidence()),
                            at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS))
    assert book.snapshot.orders == () and book.positions == {}
    assert book.balances.cash == book.balances.cash_available == 1000
    assert reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions).passed
