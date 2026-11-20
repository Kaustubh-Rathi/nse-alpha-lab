"""Direct contracts for the independent accounting replay (P0-CONSERVATION).

The replay is a second implementation: it must accept every genuine lifecycle
and reject a tampered amount, date, phase, evidence record or posting recipe.
"""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
def gross_payment(on, available_at, citation, amount=None):
    from nse_pipeline.backtest.dividends import (
        GROSS_DIVIDEND_NO_WITHHOLDING_MODEL, VerifiedPayment)
    return VerifiedPayment(on, available_at, citation, amount,
                           settlement_model=GROSS_DIVIDEND_NO_WITHHOLDING_MODEL)


CHECK_NAMES = (
    "event_integrity",
    "cash_conservation",
    "claim_conservation",
    "share_delivery_and_identity",
    "reservation_conservation",
    "receivable_rollforward",
    "payable_rollforward",
    "entitlement_conservation",
    "snapshot_clock",
    "settlement_phase_completion",
    "funded_cash",
    "position_cap",
    "claim_lifecycle",
)


def evidence(on=date(2024, 6, 3), verified=True, citation="verified fixture"):
    from nse_pipeline.backtest.accounting_types import Evidence, EventTime, Phase
    return Evidence(citation, "AAA", on, EventTime(date(2024, 5, 31), Phase.CLOSE_VALUATION), verified)


def terms():
    from nse_pipeline.backtest.dividends import DividendTerms
    return DividendTerms("d", "2", evidence(citation="issuer dividend fixture"))


def hold_policy():
    from nse_pipeline.specs import ValuationPolicy
    return ValuationPolicy(fallback_lag=None, lag_unit="calendar_days",
                           dividend_settlement_mode="hold_receivable")


def held_book():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    book = Portfolio("10000.00", on=date(2024, 6, 3),
                     positions=(Position("p", "AAA", 1000, date(2024, 5, 31)),))
    book.entitle_dividend("p", terms(), hold_policy(),
                          at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS))
    book.accrue_dividend("d", at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 1))
    return book


def confirmed_book():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.dividends import VerifiedPayment
    book = held_book()
    for day in (4, 5, 6, 7, 10):
        book.observe_session(date(2024, 6, day))
    book.confirm_dividend_payment(
        "d", gross_payment(date(2024, 6, 10), EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION),
                             "issuer payment notice"),
        at=EventTime(date(2024, 6, 10), Phase.CLOSE_VALUATION))
    return book


def settled_book():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    book = confirmed_book()
    book.observe_session(date(2024, 6, 11))
    book.settle("dividend:d", at=EventTime(date(2024, 6, 11), Phase.POST_OPEN_SETTLEMENT))
    return book


def voided_book():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    book = held_book()
    book.void_dividend("d", evidence(citation="issuer withdrawal notice"), "issuer withdrew the dividend",
                       at=EventTime(date(2024, 6, 3), Phase.PRE_OPEN_ACTIONS, 2))
    return book


def reconcile(book):
    from nse_pipeline.backtest.invariants import reconcile
    return reconcile(book.initial, book.events, book.snapshot, sessions=book.observed_sessions)


def event(book, kind):
    return next(e for e in book.events if e.kind == kind)


def test_reconcile_reports_every_named_check_on_a_golden_case():
    book = held_book()
    result = reconcile(book)
    assert result.passed, result.checks
    assert tuple(check.name for check in result.checks) == CHECK_NAMES
    assert all(check.status == "PASS" for check in result.checks)
    assert result.events_checked == len(book.events) == 2


def test_replay_accepts_confirmed_then_settled_dividend():
    from nse_pipeline.backtest.accounting_types import EventKind
    book = settled_book()
    result = reconcile(book)
    assert result.passed, result.checks
    assert [e.kind for e in book.events] == [
        EventKind.DIVIDEND_ENTITLEMENT, EventKind.DIVIDEND_ACCRUAL,
        EventKind.DIVIDEND_PAYMENT_CONFIRMED, EventKind.SETTLE_DIVIDEND]
    assert book.balances.cash == D("12000.00") and not book.snapshot.claims


def test_replay_accepts_verified_void_writeoff():
    from nse_pipeline.backtest.accounting_types import EventKind
    book = voided_book()
    result = reconcile(book)
    assert result.passed, result.checks
    assert book.events[-1].kind == EventKind.DIVIDEND_VOIDED
    assert book.balances.cash == D("10000.00") and not book.snapshot.claims


@pytest.mark.parametrize("tamper,message", (
    (lambda e: replace(e, amount=D("1999.99")), "Confirmation restated"),
    (lambda e: replace(e, due=None), "not causal"),
    (lambda e: replace(e, payment_on=date(2024, 6, 2)), "pre-ex-date"),
    (lambda e: replace(e, payment_available_at=replace(e.at, session=e.at.session + timedelta(days=1))),
     "Unavailable"),
))
def test_replay_rejects_confirmation_tampering(tamper, message):
    from nse_pipeline.backtest.accounting_types import EventKind
    book = confirmed_book()
    events = list(book.events)
    index = next(i for i, e in enumerate(events) if e.kind == EventKind.DIVIDEND_PAYMENT_CONFIRMED)
    events[index] = tamper(events[index])
    from nse_pipeline.backtest.invariants import reconcile
    result = reconcile(book.initial, tuple(events), book.snapshot, sessions=book.observed_sessions)
    assert not result.passed
    assert result.checks[0].status == "FAIL" and message in result.checks[0].detail


def test_replay_rejects_confirmation_that_moves_cash():
    from nse_pipeline.backtest.accounting_types import EventKind, Posting
    book = confirmed_book()
    events = list(book.events)
    index = next(i for i, e in enumerate(events) if e.kind == EventKind.DIVIDEND_PAYMENT_CONFIRMED)
    events[index] = replace(events[index], postings=(Posting("cash", D("2000.00")),))
    from nse_pipeline.backtest.invariants import reconcile
    result = reconcile(book.initial, tuple(events), book.snapshot, sessions=book.observed_sessions)
    assert not result.passed and "Confirmation moved cash" in result.checks[0].detail


@pytest.mark.parametrize("tamper,message", (
    (lambda e: replace(e, postings=(e.postings[0],)), "postings differ"),
    (lambda e: replace(e, evidence=replace(e.evidence, terms_verified=False)), "verified evidence"),
    (lambda e: replace(e, reason=""), "disclosed reason"),
))
def test_replay_rejects_void_tampering(tamper, message):
    from nse_pipeline.backtest.accounting_types import EventKind
    book = voided_book()
    events = list(book.events)
    index = next(i for i, e in enumerate(events) if e.kind == EventKind.DIVIDEND_VOIDED)
    events[index] = tamper(events[index])
    from nse_pipeline.backtest.invariants import reconcile
    result = reconcile(book.initial, tuple(events), book.snapshot, sessions=book.observed_sessions)
    assert not result.passed and message in result.checks[0].detail


def test_replay_rejects_settlement_of_a_held_claim():
    from nse_pipeline.backtest.accounting_types import EventKind, EventTime, LedgerEvent, Phase
    book = held_book()
    fake = LedgerEvent("settle:dividend:d", EventTime(date(2024, 6, 3), Phase.POST_OPEN_SETTLEMENT),
                       EventKind.SETTLE_DIVIDEND, amount=D("2000.00"), claim_id="dividend:d")
    from nse_pipeline.backtest.invariants import reconcile
    result = reconcile(book.initial, book.events + (fake,), book.snapshot, sessions=book.observed_sessions)
    assert not result.passed and "Settlement amount or source claim is wrong" in result.checks[0].detail


def test_replay_does_not_import_core_fees_or_valuation():
    source = (ROOT / "src/nse_pipeline/backtest/invariants.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {"core", "fees", "valuation", "nse_pipeline.backtest.core",
                 "nse_pipeline.backtest.fees", "nse_pipeline.backtest.valuation"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{(node.module or '').rstrip('.')}.{alias.name}" for alias in node.names)
    assert not (imported & forbidden)
