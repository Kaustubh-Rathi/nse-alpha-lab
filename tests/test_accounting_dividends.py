from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path

import pytest


def gross_payment(on, available_at, citation, amount=None):
    from nse_pipeline.backtest.dividends import (
        GROSS_DIVIDEND_NO_WITHHOLDING_MODEL, VerifiedPayment)
    return VerifiedPayment(on, available_at, citation, amount,
                           settlement_model=GROSS_DIVIDEND_NO_WITHHOLDING_MODEL)


def terms(payment=None):
    from nse_pipeline.backtest.dividends import DividendTerms
    from nse_pipeline.backtest.accounting_types import Evidence, EventTime, Phase
    return DividendTerms("d", "2", Evidence("issuer dividend fixture", "AAA", date(2024, 6, 3),
        EventTime(date(2024, 5, 31), Phase.CLOSE_VALUATION), True), payment)


def book_and_policy(lag=2, unit="exchange_sessions"):
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import Position
    from nse_pipeline.specs import ValuationPolicy
    return (Portfolio("10000", on=date(2024, 6, 3), positions=(Position("p", "AAA", 1000, date(2024, 5, 31)),)),
            ValuationPolicy(fallback_lag=lag, lag_unit=unit))


def accrue(book, policy, action=None):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    book.entitle_dividend("p", action or terms(), policy,
                           at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS))
    book.accrue_dividend("d", at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 1))


def test_production_dividend_accrual_and_delayed_payment_match_golden():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    book, policy = book_and_policy()
    case = next(c for c in json.loads((Path(__file__).parent / "golden/accounting_cases.json").read_text())["cases"]
                if c["id"] == "dividend_lag")
    accrue(book, policy)
    assert book.balances.dividend_receivable == D(case["events"][0]["expected"]["dividend_receivable"])
    assert book.balances.cash == 10000 and book.balances.cash_available == 10000
    book.observe_session(date(2024, 6, 4))
    before = book.snapshot
    with pytest.raises(ValueError):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 4), Phase.POST_OPEN_SETTLEMENT))
    assert book.snapshot == before
    book.observe_session(date(2024, 6, 5))
    with pytest.raises(ValueError):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 5), Phase.PRE_OPEN_SETTLEMENT))
    book.settle("dividend:d", at=EventTime(date(2024, 6, 5), Phase.POST_OPEN_SETTLEMENT))
    assert book.balances.cash == D(case["events"][3]["expected"]["cash"])
    assert book.balances.dividend_receivable == 0
    with pytest.raises(ValueError):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 5), Phase.POST_OPEN_SETTLEMENT, 1))


def test_weekend_payment_and_late_evidence_are_causal():
    from nse_pipeline.backtest.dividends import VerifiedPayment
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    known = gross_payment(date(2024, 6, 8), EventTime(date(2024, 5, 31), Phase.CLOSE_VALUATION), "issuer payment notice")
    book, policy = book_and_policy(lag=30, unit="calendar_days")
    accrue(book, policy, terms(known))
    assert book.snapshot.claims[0].basis == "verified"
    for day in (4, 5, 6, 7, 10):
        book.observe_session(date(2024, 6, day))
    with pytest.raises(ValueError):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 10), Phase.PRE_OPEN_SETTLEMENT))
    book.settle("dividend:d", at=EventTime(date(2024, 6, 10), Phase.POST_OPEN_SETTLEMENT))
    assert book.balances.cash == 12000
    late = gross_payment(date(2024, 6, 8), EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION), "late discovery")
    a, p = book_and_policy()
    b, _ = book_and_policy()
    accrue(a, p)
    accrue(b, p, terms(late))
    assert a.snapshot == b.snapshot and a.events == b.events


def test_ex_date_seller_keeps_frozen_receivable_and_ex_date_buyer_gets_none():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, OpeningPrint
    from nse_pipeline.specs import CostSpec, ExecutionSpec
    from nse_pipeline.backtest.core import Portfolio
    costs = CostSpec(brokerage_bps=0, stt_bps_sell=0, stt_bps_buy=0, stamp_bps_buy=0, exchange_bps=0,
                     gst_on_brokerage_pct=0)
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    seller, policy = book_and_policy()
    accrue(seller, policy)
    seller.sell("s", "p", 1000, OpeningPrint("AAA", date(2024, 6, 3), "100"), costs, execution,
                at=EventTime(date(2024, 6, 3), Phase.OPEN_FILLS))
    assert seller.balances.dividend_receivable == 2000 and seller.positions["p"].shares == 0
    buyer = Portfolio("100000", on=date(2024, 5, 31))
    buyer.reserve("o", "AAA", 1000, "100000", "100000", at=EventTime(date(2024, 5, 31), Phase.CLOSE_ORDERS))
    buyer.observe_session(date(2024, 6, 3))
    buyer.buy("o", OpeningPrint("AAA", date(2024, 6, 3), "100"), costs, execution,
               at=EventTime(date(2024, 6, 3), Phase.OPEN_FILLS))
    with pytest.raises(ValueError):
        buyer.entitle_dividend("position:buy:o", terms(), policy,
            at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS))
    assert buyer.balances.dividend_receivable == 0


def test_hold_receivable_and_explicit_lag_sensitivity_never_create_cash():
    from nse_pipeline.specs import ValuationPolicy
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    a, one = book_and_policy(lag=1)
    b, two = book_and_policy(lag=2)
    held, _ = book_and_policy()
    hold = ValuationPolicy(fallback_lag=None, lag_unit="calendar_days", dividend_settlement_mode="hold_receivable")
    for book, policy in ((a, one), (b, two), (held, hold)):
        accrue(book, policy)
        book.observe_session(date(2024, 6, 4))
    a.settle("dividend:d", at=EventTime(date(2024, 6, 4), Phase.POST_OPEN_SETTLEMENT))
    for book in (b, held):
        with pytest.raises(ValueError):
            book.settle("dividend:d", at=EventTime(date(2024, 6, 4), Phase.POST_OPEN_SETTLEMENT))
    assert a.balances.cash_available == 12000
    assert b.balances.cash_available == held.balances.cash_available == 10000


def test_conflicting_payment_evidence_is_rejected_atomically():
    from nse_pipeline.backtest.dividends import VerifiedPayment
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    book, policy = book_and_policy()
    before = book.snapshot
    bad = gross_payment(date(2024, 5, 30), EventTime(date(2024, 5, 31), Phase.CLOSE_VALUATION), "conflicting notice")
    with pytest.raises(ValueError):
        book.entitle_dividend("p", terms(bad), policy, at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS))
    assert book.snapshot == before and book.events == ()


def test_unaccrued_entitlement_and_ambiguous_same_day_units_block_nav():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.valuation import value, Mark
    from nse_pipeline.backtest.share_actions import ShareAction
    book, policy = book_and_policy()
    book.entitle_dividend("p", terms(), policy, at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS))
    close = EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION)
    result = value(book.snapshot, close, marks=[Mark("p", "100", close.session, close)])
    assert result.nav is None and result.unresolved_assets == ("unaccrued:d",)
    assert all(s.amount is None for s in result.scenarios)
    book.apply_share_action("p", ShareAction("a", "split", 2, 1, terms().evidence),
                            at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 1))
    assert book.positions["p"].shares == 1000 and book.positions["p"].blocked_reason
    assert value(book.snapshot, close, marks=[Mark("p", "100", close.session, close)]).nav is None


LIFECYCLE_GOLDEN = Path(__file__).parent / "golden/dividend_lifecycle_cases.json"


def lifecycle_cases():
    return {case["id"]: case for case in json.loads(LIFECYCLE_GOLDEN.read_text())["cases"]}


def hold_policy():
    from nse_pipeline.specs import ValuationPolicy
    return ValuationPolicy(fallback_lag=None, lag_unit="calendar_days",
                           dividend_settlement_mode="hold_receivable")


def held_book(cash="10000.00"):
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    book = Portfolio(cash, on=date(2024, 6, 3),
                     positions=(Position("p", "AAA", 1000, date(2024, 5, 31)),))
    book.entitle_dividend("p", terms(), hold_policy(),
                          at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS))
    book.accrue_dividend("d", at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 1))
    return book


def close_value(book, on, price="100"):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.valuation import Mark, value
    at = EventTime(on, Phase.CLOSE_VALUATION)
    return value(book.snapshot, at, marks=(Mark("p", price, on, at),))


def test_hold_receivable_is_in_nav_but_unsettleable():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    book = held_book()
    claim = next(c for c in book.snapshot.claims if c.claim_id == "dividend:d")
    assert claim.amount == D("2000.00")
    assert claim.due is None
    assert claim.unresolved_reason == "hold_receivable"
    assert claim.state == "accrued_held"

    before = book.snapshot
    with pytest.raises(ValueError, match="unbound_payment_evidence"):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 3), Phase.POST_OPEN_SETTLEMENT))
    assert book.snapshot == before
    book.observe_session(date(2024, 6, 4))
    with pytest.raises(ValueError, match="unbound_payment_evidence"):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 4), Phase.PRE_OPEN_SETTLEMENT))

    assert book.balances.cash == D("10000.00")
    assert book.balances.cash_available == D("10000.00")
    assert book.balances.dividend_receivable == D("2000.00")
    result = close_value(book, date(2024, 6, 4))
    assert result.nav == D("112000.00")
    assert result.nav_reason == "nav_available"
    assert result.unresolved_settlement_claims == ("dividend:d",)


def test_confirm_verified_payment_then_settle():
    from nse_pipeline.backtest.accounting_types import Due, EventKind, EventTime, Phase
    from nse_pipeline.backtest.dividends import VerifiedPayment

    book = held_book()
    for day in (4, 5, 6, 7, 10):
        book.observe_session(date(2024, 6, day))
    payment = gross_payment(date(2024, 6, 10),
                              EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION),
                              "issuer payment notice")
    before = close_value(book, date(2024, 6, 10))
    book.confirm_dividend_payment("d", payment,
                                  at=EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION))
    claim = next(c for c in book.snapshot.claims if c.claim_id == "dividend:d")
    assert claim.state == "confirmed"
    assert claim.basis == "verified"
    assert claim.unresolved_reason == ""
    assert claim.due == Due(Phase.POST_OPEN_SETTLEMENT, session_index=6)
    event = book.events[-1]
    assert event.event_id == "confirm-dividend:d"
    assert event.kind == EventKind.DIVIDEND_PAYMENT_CONFIRMED
    assert event.amount == D("2000.00")
    assert event.payment_on == date(2024, 6, 10)
    assert event.postings == ()
    assert book.balances.cash == D("10000.00") and book.balances.dividend_receivable == D("2000.00")
    after = close_value(book, date(2024, 6, 10))
    assert after.nav == before.nav == D("112000.00")
    with pytest.raises(ValueError):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 10), Phase.POST_OPEN_SETTLEMENT))

    book.observe_session(date(2024, 6, 11))
    book.settle("dividend:d", at=EventTime(date(2024, 6, 11), Phase.POST_OPEN_SETTLEMENT))
    assert book.balances.cash == D("12000.00")
    assert book.balances.dividend_receivable == 0
    assert close_value(book, date(2024, 6, 11)).nav == D("112000.00")
    with pytest.raises(ValueError, match="claim_absent"):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 11), Phase.POST_OPEN_SETTLEMENT, 1))


def test_confirmation_before_the_settlement_phase_may_settle_same_session():
    from nse_pipeline.backtest.accounting_types import Due, EventTime, Phase
    from nse_pipeline.backtest.dividends import VerifiedPayment

    book = held_book()
    payment = gross_payment(date(2024, 6, 3),
                              EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS),
                              "same-day payment notice")
    book.confirm_dividend_payment("d", payment,
                                  at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    claim = next(c for c in book.snapshot.claims if c.claim_id == "dividend:d")
    assert claim.due == Due(Phase.POST_OPEN_SETTLEMENT, on=date(2024, 6, 3))
    book.settle("dividend:d", at=EventTime(date(2024, 6, 3), Phase.POST_OPEN_SETTLEMENT))
    assert book.balances.cash == D("12000.00")
    assert book.balances.dividend_receivable == 0


def test_confirm_rejects_mismatched_amount_phase_and_impossible_date():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.dividends import VerifiedPayment

    def payment(on=date(2024, 6, 3), available=None, amount=None, citation="payment notice"):
        available = available or EventTime(on, Phase.PRE_OPEN_ACTIONS)
        return gross_payment(on, available, citation, amount)

    cases = (
        ("conflicting_dividend_payment_amount", payment(amount="1999.99"),
         EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS)),
        ("conflicting_dividend_payment_amount", payment(amount="2000.01"),
         EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS)),
        ("payment_precedes_entitlement_date", payment(on=date(2024, 6, 2)),
         EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS)),
        ("payment_evidence_not_yet_available",
         payment(on=date(2024, 6, 10), available=EventTime(date(2024, 6, 10), Phase.PRE_OPEN_ACTIONS)),
         EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS)),
        ("confirmation_phase_not_allowed", payment(),
         EventTime(date(2024, 6, 3), Phase.POST_OPEN_SETTLEMENT)),
    )
    for error, evidence, at in cases:
        book = held_book()
        before, events = book.snapshot, book.events
        with pytest.raises(ValueError, match=error):
            book.confirm_dividend_payment("d", evidence, at=at)
        assert book.snapshot == before and book.events == events
        claim = next(c for c in book.snapshot.claims if c.claim_id == "dividend:d")
        assert claim.state == "accrued_held" and claim.due is None
        assert book.balances.cash == D("10000.00")
        assert close_value(book, date(2024, 6, 3)).unresolved_settlement_claims == ("dividend:d",)


def test_duplicate_and_post_void_confirmation_are_rejected_atomically():
    from nse_pipeline.backtest.accounting_types import EventKind, EventTime, Phase
    from nse_pipeline.backtest.dividends import VerifiedPayment
    from nse_pipeline.backtest.accounting_types import Evidence

    book = held_book()
    payment = gross_payment(date(2024, 6, 3),
                              EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS),
                              "same-day payment notice")
    book.confirm_dividend_payment("d", payment,
                                  at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="already_confirmed"):
        book.confirm_dividend_payment("d", payment,
                                      at=EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION, 3))
    assert book.snapshot == before and book.events == events
    assert len([e for e in book.events if e.kind == EventKind.DIVIDEND_PAYMENT_CONFIRMED]) == 1

    voided = held_book()
    proof = Evidence("issuer withdrawal notice", "AAA", date(2024, 6, 3),
                     EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS), True)
    voided.void_dividend("d", proof, "issuer withdrew the dividend",
                         at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    before, events = voided.snapshot, voided.events
    with pytest.raises(ValueError, match="claim_absent"):
        voided.confirm_dividend_payment("d", payment,
                                        at=EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION, 3))
    assert voided.snapshot == before and voided.events == events


def test_void_dividend_is_terminal_and_requires_verified_evidence_reason_and_writeoff():
    from nse_pipeline.backtest.accounting_types import Evidence, EventKind, EventTime, Phase
    from nse_pipeline.backtest.dividends import VerifiedPayment

    case = lifecycle_cases()["void_held"]
    book = held_book()
    assert close_value(book, date(2024, 6, 3), price="20").nav == D(case["expected"]["nav_before"])
    proof = Evidence("issuer withdrawal notice", "AAA", date(2024, 6, 3),
                     EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS), True)
    with pytest.raises(ValueError, match="nonempty"):
        book.void_dividend("d", proof, "", at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    assert book.snapshot.claims and len(book.events) == 2
    unverified = Evidence("unsigned withdrawal note", "AAA", date(2024, 6, 3),
                          EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS), False)
    with pytest.raises(ValueError):
        book.void_dividend("d", unverified, "issuer withdrew the dividend",
                           at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    book.void_dividend("d", proof, case["void_reason"],
                       at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    event = book.events[-1]
    assert event.event_id == case["expected"]["event_id"]
    assert event.kind == EventKind.DIVIDEND_VOIDED
    assert [[p.account, str(p.amount)] for p in event.postings] == case["expected"]["postings"]
    assert book.balances.cash == D(case["expected"]["cash"])
    assert book.balances.dividend_receivable == D(case["expected"]["receivable"])
    assert not book.snapshot.claims
    after = close_value(book, date(2024, 6, 3), price="20")
    assert after.nav == D(case["expected"]["nav_after"])
    assert after.unresolved_settlement_claims == ()

    confirmed = held_book()
    for day in (4, 5, 6, 7, 10):
        confirmed.observe_session(date(2024, 6, day))
    payment = gross_payment(date(2024, 6, 10),
                              EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION),
                              "issuer payment notice")
    confirmed.confirm_dividend_payment("d", payment,
                                       at=EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION))
    later = Evidence("issuer withdrawal notice", "AAA", date(2024, 6, 11),
                     EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION), True)
    confirmed.observe_session(date(2024, 6, 11))
    before = confirmed.snapshot
    with pytest.raises(ValueError, match="not_voidable"):
        confirmed.void_dividend("d", later, "issuer withdrew the dividend",
                                at=EventTime(date(2024, 6, 11), Phase.PRE_OPEN_ACTIONS))
    assert confirmed.snapshot == before


def test_untyped_opening_claim_is_not_confirmable_or_voidable():
    from nse_pipeline.backtest.accounting_types import (
        Claim, ClaimKind, Evidence, EventTime, Phase)
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.dividends import VerifiedPayment

    book = Portfolio("0", on=date(2024, 6, 3),
                     claims=(Claim("dividend:opening", ClaimKind.DIVIDEND, "50", "opening fixture"),))
    payment = gross_payment(date(2024, 6, 3),
                              EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS),
                              "payment notice")
    proof = Evidence("issuer withdrawal notice", "AAA", date(2024, 6, 3),
                     EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS), True)
    before = book.snapshot
    with pytest.raises(ValueError, match="not_confirmable"):
        book.confirm_dividend_payment("opening", payment,
                                      at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS))
    with pytest.raises(ValueError, match="not_voidable"):
        book.void_dividend("opening", proof, "opening balances need a separate adjustment",
                           at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 1))
    assert book.snapshot == before and book.events == ()


def test_confirmed_payment_does_not_move_cash_or_change_nav():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.dividends import VerifiedPayment

    book = held_book()
    before = close_value(book, date(2024, 6, 3))
    payment = gross_payment(date(2024, 6, 3),
                              EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS),
                              "same-day payment notice")
    book.confirm_dividend_payment("d", payment,
                                  at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    after = close_value(book, date(2024, 6, 3))
    assert book.balances.cash == D("10000.00") and book.balances.cash_available == D("10000.00")
    assert book.balances.dividend_receivable == D("2000.00")
    assert after.nav == before.nav == D("112000.00")
    assert after.marked_value_priced == before.marked_value_priced


def test_duplicate_void_writeoff_and_terminal_disclosure():
    from nse_pipeline.backtest.accounting_types import (
        Evidence, EventKind, EventTime, Phase)
    from nse_pipeline.backtest.dividends import VerifiedPayment

    cases = lifecycle_cases()
    hold = cases["hold_confirm_settle"]
    book = held_book(cash=hold["cash"])
    before = close_value(book, date(2024, 6, 3), price=hold["mark"])
    assert before.nav == D(hold["expected"]["nav_before_settlement"])
    for day in (4, 5, 6, 7, 10):
        book.observe_session(date(2024, 6, day))
    payment = gross_payment(date(2024, 6, 10),
                              EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION),
                              "issuer payment notice")
    book.confirm_dividend_payment("d", payment,
                                  at=EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION))
    claim = next(c for c in book.snapshot.claims if c.claim_id == "dividend:d")
    assert claim.due.session_index == hold["expected"]["confirmed_due_session_index"]
    assert book.balances.cash == D(hold["expected"]["cash_before_settlement"])
    assert close_value(book, date(2024, 6, 10), price=hold["mark"]).nav == D(
        hold["expected"]["nav_before_settlement"])
    with pytest.raises(ValueError, match=cases["duplicate_confirm"]["expected"]["error"]):
        book.confirm_dividend_payment("d", payment,
                                      at=EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION, 1))
    assert len([e for e in book.events if e.kind == EventKind.DIVIDEND_PAYMENT_CONFIRMED]) == \
        cases["duplicate_confirm"]["expected"]["payment_confirmed_events"]

    book.observe_session(date(2024, 6, 11))
    book.settle("dividend:d", at=EventTime(date(2024, 6, 11), Phase.POST_OPEN_SETTLEMENT))
    result = close_value(book, date(2024, 6, 11), price=hold["mark"])
    assert book.balances.cash == D(hold["expected"]["cash_after_settlement"])
    assert result.marked_value_priced == D(hold["expected"]["marked_after_settlement"])
    assert book.balances.receivables == D(hold["expected"]["receivables_after_settlement"])
    assert result.nav == D(hold["expected"]["nav_after_settlement"])
    assert not book.snapshot.claims
    book.close(at=EventTime(date(2024, 6, 11), Phase.CLOSE_AUDIT))

    terminal = cases["terminal_open"]
    open_book = held_book()
    with pytest.raises(ValueError) as caught:
        open_book.close(at=EventTime(date(2024, 6, 3), Phase.CLOSE_AUDIT))
    message = str(caught.value)
    assert terminal["expected"]["error"] in message
    for claim_id in terminal["expected"]["named_claims"]:
        assert claim_id in message
    assert close_value(open_book, date(2024, 6, 3)).unresolved_settlement_claims == tuple(
        terminal["expected"]["disclosure"])
    proof = Evidence("issuer withdrawal notice", "AAA", date(2024, 6, 3),
                     EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS), True)
    open_book.void_dividend("d", proof, cases["void_held"]["void_reason"],
                            at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    open_book.close(at=EventTime(date(2024, 6, 3), Phase.CLOSE_AUDIT))


@pytest.mark.parametrize("accrued_first", (False, True))
def test_ambiguous_share_units_never_turn_a_dividend_into_spendable_cash(accrued_first):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.share_actions import ShareAction
    from nse_pipeline.backtest.invariants import reconcile
    book, policy = book_and_policy(lag=1)
    book.entitle_dividend("p", terms(), policy, at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS))
    if accrued_first:
        book.accrue_dividend("d", at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 1))
    book.apply_share_action("p", ShareAction("a", "split", 2, 1, terms().evidence),
                            at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    if not accrued_first:
        with pytest.raises(ValueError):
            book.accrue_dividend("d", at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 3))
    book.observe_session(date(2024, 6, 4))
    with pytest.raises(ValueError):
        book.settle("dividend:d", at=EventTime(date(2024, 6, 4), Phase.POST_OPEN_SETTLEMENT))
    assert book.balances.cash == book.balances.cash_available == 10000
    assert book.balances.dividend_receivable is None if accrued_first else book.balances.dividend_receivable == 0
    assert reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions).passed
