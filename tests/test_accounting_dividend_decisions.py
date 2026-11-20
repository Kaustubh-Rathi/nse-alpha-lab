"""Owner-approved modified U-10/U-11 dividend evidence and opening balances.

These cases are synthetic fixtures. They prove fail-closed behaviour only; they
are not evidence about any real issuer, payment, withholding rate or opening
broker balance.
"""
from datetime import date
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.accounting_types import (
    Claim, ClaimKind, Due, EventKind, EventTime, Evidence, Phase)
from nse_pipeline.backtest.core import Portfolio
from nse_pipeline.backtest.dividends import (
    GROSS_DIVIDEND_NO_WITHHOLDING_MODEL, VerifiedPayment)
from nse_pipeline.backtest.invariants import reconcile

ON = date(2024, 6, 3)
GROSS = GROSS_DIVIDEND_NO_WITHHOLDING_MODEL


def proof(*, key="AAA", verified=True, citation="issuer payment notice", on=ON):
    return Evidence(citation, key, on, EventTime(on, Phase.PRE_OPEN_ACTIONS), verified)


def payment(*, on=ON, amount="50.00", model=GROSS, citation="issuer payment notice"):
    return VerifiedPayment(on, EventTime(on, Phase.PRE_OPEN_ACTIONS), citation,
                           amount, settlement_model=model)


def opening_book(*, amount="50.00", basis="opening_balance", with_position=True):
    positions = ()
    position_id = ""
    if with_position:
        from nse_pipeline.backtest.accounting_types import Position
        positions = (Position("p", "AAA", 1, date(2024, 5, 31)),)
        position_id = "p"
    claim = Claim("dividend:opening", ClaimKind.DIVIDEND, amount, "synthetic opening fixture",
                  position_id=position_id, basis=basis)
    return Portfolio("0", on=ON, claims=(claim,), positions=positions)


def reconcile_book(book):
    return reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions)


def test_full_gross_payment_requires_the_explicit_no_withholding_label():
    with pytest.raises(ValueError, match="gross_dividend_no_withholding_model"):
        VerifiedPayment(ON, EventTime(ON, Phase.PRE_OPEN_ACTIONS), "unlabelled notice",
                        "50.00")
    with pytest.raises(ValueError, match="gross_dividend_no_withholding_model"):
        payment(model="net_of_withholding_tax")
    labelled = payment(amount=None)
    assert labelled.settlement_model == GROSS


def test_partial_or_net_payment_evidence_is_rejected_without_restating_the_receivable():
    book = opening_book()
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="conflicting_dividend_payment_amount"):
        book.confirm_dividend_payment("opening", payment(amount="49.99"),
                                      at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    assert book.snapshot == before and book.events == events
    assert book.balances.dividend_receivable == D("50.00")
    assert book.balances.cash == D("0")


def test_opening_balance_claim_can_be_confirmed_and_settled_with_full_gross_evidence():
    book = opening_book()
    book.confirm_dividend_payment("opening", payment(),
                                  at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    claim = next(c for c in book.snapshot.claims if c.claim_id == "dividend:opening")
    assert claim.state == "confirmed" and claim.basis == "verified"
    assert claim.due == Due(Phase.POST_OPEN_SETTLEMENT, on=ON)
    event = book.events[-1]
    assert event.kind == EventKind.DIVIDEND_PAYMENT_CONFIRMED
    assert event.payment_model == GROSS and event.entitlement_id == "" and event.key == "AAA"
    assert book.balances.cash == D("0") and book.balances.dividend_receivable == D("50.00")
    book.settle("dividend:opening", at=EventTime(ON, Phase.POST_OPEN_SETTLEMENT))
    assert book.balances.cash == D("50.00") and book.balances.dividend_receivable == D("0")
    result = reconcile_book(book)
    assert result.passed, result.checks


def test_opening_balance_write_off_is_evidence_bound_and_never_rewrites_the_opening_snapshot():
    book = opening_book()
    initial, before = book.initial, book.snapshot
    book.void_dividend("opening", proof(), "issuer confirmed no payment",
                       at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    event = book.events[-1]
    assert event.kind == EventKind.DIVIDEND_VOIDED and event.entitlement_id == ""
    assert event.key == "AAA" and event.reason == "issuer confirmed no payment"
    assert [(p.account, p.amount) for p in event.postings] == [
        ("dividend_receivable", D("-50.00")), ("dividend_writeoff", D("50.00"))]
    assert book.balances.dividend_receivable == D("0") and not book.snapshot.claims
    assert book.initial == initial and before.claims[0].amount == D("50.00")
    result = reconcile_book(book)
    assert result.passed, result.checks


def test_opening_balance_write_off_rejects_future_unverified_or_wrong_security_evidence():
    for bad in (proof(verified=False, citation="unsigned note"),
                proof(key="BBB", citation="wrong security"),
                Evidence("future notice", "AAA", date(2024, 6, 4),
                         EventTime(date(2024, 6, 4), Phase.PRE_OPEN_ACTIONS), True)):
        book = opening_book()
        before, events = book.snapshot, book.events
        with pytest.raises(ValueError):
            book.void_dividend("opening", bad, "issuer confirmed no payment",
                               at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
        assert book.snapshot == before and book.events == events


def test_untyped_unbound_dividend_claim_still_cannot_be_voided_arbitrarily():
    book = opening_book(basis="verified")
    before = book.snapshot
    with pytest.raises(ValueError, match="not_voidable"):
        book.void_dividend("opening", proof(), "no documentary basis for cancellation",
                           at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    assert book.snapshot == before and book.events == ()


def test_opening_balance_correction_is_an_immutable_dated_event():
    book = opening_book(amount="50.00")
    initial = book.initial
    book.correct_opening_dividend(
        "opening", "60.00", proof(citation="broker opening-balance correction"),
        "independent statement corrected the opening receivable",
        at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    event = book.events[-1]
    assert event.kind == EventKind.DIVIDEND_CORRECTED
    assert event.claim_id == "dividend:opening" and event.amount == D("10.00")
    assert event.key == "AAA" and event.reason == "independent statement corrected the opening receivable"
    assert [(p.account, p.amount) for p in event.postings] == [
        ("dividend_receivable", D("10.00")), ("dividend_correction", D("-10.00"))]
    claim = book.snapshot.claims[0]
    assert claim.amount == D("60.00") and claim.citation == "broker opening-balance correction"
    assert book.initial == initial
    result = reconcile_book(book)
    assert result.passed, result.checks


def test_opening_balance_correction_rejects_non_opening_claims_and_invalid_amounts():
    book = opening_book(basis="verified")
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="not_correctable"):
        book.correct_opening_dividend(
            "opening", "60.00", proof(), "correction without an opening basis",
            at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    assert book.snapshot == before and book.events == events

    for amount in ("50.00", "0", "-1", "NaN"):
        book = opening_book()
        before = book.snapshot
        with pytest.raises(ValueError):
            book.correct_opening_dividend(
                "opening", amount, proof(), "invalid correction",
                at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
        assert book.snapshot == before and book.events == ()


def test_opening_balance_claim_with_a_position_requires_matching_security_evidence():
    book = opening_book(with_position=True)
    with pytest.raises(ValueError):
        book.void_dividend("opening", proof(key="BBB"), "wrong security",
                           at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    book.void_dividend("opening", proof(key="AAA"), "issuer confirmed no payment",
                       at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    assert not book.snapshot.claims
    result = reconcile_book(book)
    assert result.passed, result.checks


def test_opening_balance_lifecycle_requires_a_proven_position_identity():
    book = opening_book(with_position=False)
    before = book.snapshot
    with pytest.raises(ValueError, match="claim_position_absent"):
        book.void_dividend("opening", proof(key="AAA"), "issuer confirmed no payment",
                           at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    with pytest.raises(ValueError, match="claim_position_absent"):
        book.correct_opening_dividend("opening", "60.00", proof(key="AAA"),
                                      "corrected statement",
                                      at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    with pytest.raises(ValueError, match="claim_position_absent"):
        book.confirm_dividend_payment("opening", payment(),
                                      at=EventTime(ON, Phase.PRE_OPEN_ACTIONS, 1))
    assert book.snapshot == before and book.events == ()
