"""Explicit external cash flows: kernel operations and independent replay.

These are synthetic fixtures, not historical evidence. External capital is
neither income nor a financial claim: it moves settled cash and a dedicated
external-flow account exactly once, and a withdrawal may not spend cash that is
already earmarked for an order or a known payable.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.accounting_types import (
    Claim, ClaimKind, Due, EventKind, EventTime, ExternalFlowDirection, OpeningPrint,
    Phase, Position, Posting,
)
from nse_pipeline.backtest.core import Portfolio
from nse_pipeline.backtest.invariants import reconcile
from nse_pipeline.backtest.valuation import Mark, value
from nse_pipeline.specs import CostSpec, ExecutionSpec

DAY1 = date(2024, 6, 3)
DAY2 = date(2024, 6, 4)
DAY3 = date(2024, 6, 5)


def at(day, sequence=0, phase=Phase.PRE_OPEN_EVIDENCE):
    return EventTime(day, phase, sequence)


def flow_args(**overrides):
    args = dict(flow_id="seed", amount="100.00", direction=ExternalFlowDirection.DEPOSIT,
                citation="bank credit advice", at=at(DAY1))
    args.update(overrides)
    return args


def flat_costs(amount):
    return CostSpec(brokerage_bps=10000 if amount else 0, brokerage_cap_per_order=amount,
                    stt_bps_buy=0, stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0,
                    gst_on_brokerage_pct=0)


def execution():
    return ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                         adverse_slippage_bps_sell=0)


def replay_book(book, events=None):
    return reconcile(book.initial, book.events if events is None else events,
                     book.snapshot, sessions=book.observed_sessions)


def flow_book():
    book = Portfolio("1000.00", on=DAY1)
    book.record_external_flow("seed", "250.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1, 0))
    book.record_external_flow("draw", "40.00", ExternalFlowDirection.WITHDRAWAL,
                              "bank debit advice", at=at(DAY1, 1))
    return book


def test_deposit_and_withdrawal_change_cash_exactly_once():
    book = Portfolio("1000.00", on=DAY1)
    book.record_external_flow("seed", "250.50", ExternalFlowDirection.DEPOSIT,
                              "broker funding advice", at=at(DAY1, 0))
    assert book.balances.cash == D("1250.50")
    assert len(book.events) == 1
    event = book.events[0]
    assert event.event_id == "flow:seed"
    assert event.kind == EventKind.EXTERNAL_FLOW
    assert event.amount == D("250.50")
    assert event.reason == "deposit"
    assert event.citation == "broker funding advice"
    assert event.postings == (Posting("cash", D("250.50")),
                              Posting("external_flows", D("250.50")))
    assert not event.claim_id and not event.order_id and not event.position_id
    assert not event.key and not event.entitlement_id and event.due is None
    assert book.balances.order_reserved == D("0.00")
    assert book.balances.payable_reserved == D("0.00")
    assert book.balances.cash_available == D("1250.50")

    book.record_external_flow("draw", "400.25", ExternalFlowDirection.WITHDRAWAL,
                              "bank debit advice", at=at(DAY1, 1))
    assert book.balances.cash == D("850.25")
    assert len(book.events) == 2
    assert book.events[1].postings == (Posting("cash", D("-400.25")),
                                       Posting("external_flows", D("-400.25")))
    assert book.initial.cash == D("1000.00")


def test_withdrawal_cannot_overdraw_settled_cash():
    book = Portfolio("100.00", on=DAY1)
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="reserved or payable"):
        book.record_external_flow("draw", "100.01", ExternalFlowDirection.WITHDRAWAL,
                                  "bank debit advice", at=at(DAY1))
    assert book.snapshot == before and book.events == events
    assert book.balances.cash == D("100.00")
    assert book.balances.cash_available == D("100.00")


def test_withdrawal_cannot_consume_an_order_reservation():
    book = Portfolio("1000.00", on=DAY1)
    book.reserve("o", "AAA", 3, "300.00", "300.00", at=at(DAY1, 0, Phase.CLOSE_ORDERS))
    book.observe_session(DAY2)
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="reserved or payable"):
        book.record_external_flow("draw", "700.01", ExternalFlowDirection.WITHDRAWAL,
                                  "bank debit advice", at=at(DAY2))
    assert book.snapshot == before and book.events == events
    assert book.balances.cash == D("1000.00")
    assert book.balances.order_reserved == D("300.00")
    assert book.balances.cash_available == D("700.00")


def test_withdrawal_cannot_consume_a_known_payable():
    book = Portfolio("1000.00", on=DAY1)
    book.recognize_liability("tax", "250.00", "verified tax demand",
                             Due(Phase.POST_OPEN_SETTLEMENT, on=DAY2),
                             at=at(DAY1, 0, Phase.CLOSE_VALUATION))
    book.observe_session(DAY2)
    assert book.balances.liabilities == book.balances.payable_reserved == D("250.00")
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="reserved or payable"):
        book.record_external_flow("draw", "750.01", ExternalFlowDirection.WITHDRAWAL,
                                  "bank debit advice", at=at(DAY2))
    assert book.snapshot == before and book.events == events
    assert book.balances.cash == D("1000.00")
    assert book.balances.cash_available == D("750.00")


def test_withdrawal_requires_known_available_cash_while_a_deposit_does_not():
    book = Portfolio("1000.00", on=DAY1,
                     claims=(Claim("unknown-tax", ClaimKind.OTHER_PAYABLE, None,
                                   "unresolved tax demand"),))
    assert book.balances.cash_available is None
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="Unknown liabilities"):
        book.record_external_flow("draw", "10.00", ExternalFlowDirection.WITHDRAWAL,
                                  "bank debit advice", at=at(DAY1))
    assert book.snapshot == before and book.events == events

    book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1, 1))
    assert book.balances.cash == D("1010.00")


@pytest.mark.parametrize("override,error", (
    ({"amount": "0"}, ValueError),
    ({"amount": "0.00"}, ValueError),
    ({"amount": "-1.00"}, ValueError),
    ({"amount": 100.0}, TypeError),
    ({"amount": True}, TypeError),
    ({"citation": "   "}, ValueError),
    ({"flow_id": "  "}, ValueError),
    ({"direction": "deposit"}, ValueError),
    ({"direction": "withdrawal"}, ValueError),
))
def test_invalid_external_flow_fields_are_rejected_atomically(override, error):
    book = Portfolio("1000.00", on=DAY1)
    before, events = book.snapshot, book.events
    args = flow_args(**override)
    with pytest.raises(error):
        book.record_external_flow(args["flow_id"], args["amount"], args["direction"],
                                  args["citation"], at=args["at"])
    assert book.snapshot == before and book.events == events


def test_wrong_phase_foreign_session_and_ordering_are_rejected_atomically():
    book = Portfolio("1000.00", on=DAY1)
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="phase"):
        book.record_external_flow("phase", "10.00", ExternalFlowDirection.DEPOSIT,
                                  "bank credit advice", at=at(DAY1, 0, Phase.CLOSE_ORDERS))
    assert book.snapshot == before and book.events == events

    book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1, 1))
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="out of order"):
        book.record_external_flow("back", "10.00", ExternalFlowDirection.DEPOSIT,
                                  "bank credit advice", at=at(DAY1, 0))
    assert book.snapshot == before and book.events == events

    book.observe_session(DAY2)
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="observed session"):
        book.record_external_flow("past", "10.00", ExternalFlowDirection.DEPOSIT,
                                  "bank credit advice", at=at(DAY1, 2))
    assert book.snapshot == before and book.events == events


def test_duplicate_flow_id_is_rejected_atomically():
    book = Portfolio("1000.00", on=DAY1)
    book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1, 0))
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="already applied"):
        book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                                  "bank credit advice", at=at(DAY1, 1))
    assert book.snapshot == before and book.events == events


def test_flows_create_no_financial_records_and_preserve_nav():
    book = Portfolio("1000.00", on=DAY1,
                     positions=(Position("p", "AAA", 5, date(2024, 5, 31)),))
    close = at(DAY1, 0, Phase.CLOSE_VALUATION)
    mark = Mark("p", "20.00", DAY1, close)
    assert value(book.snapshot, close, marks=(mark,)).nav == D("1100.00")
    opening = book.snapshot

    book.record_external_flow("seed", "100.00", ExternalFlowDirection.DEPOSIT,
                              "funding advice", at=at(DAY1, 0))
    book.record_external_flow("draw", "40.00", ExternalFlowDirection.WITHDRAWAL,
                              "debit advice", at=at(DAY1, 1))
    assert book.snapshot.claims == opening.claims == ()
    assert book.snapshot.orders == opening.orders == ()
    assert book.snapshot.positions == opening.positions
    assert book.snapshot.entitlements == opening.entitlements == ()
    assert book.balances.cash == D("1060.00")
    assert book.balances.cash_available == D("1060.00")
    assert book.balances.receivables == D("0.00") and book.balances.liabilities == D("0.00")
    assert value(book.snapshot, close, marks=(mark,)).nav == D("1160.00")


def test_reconcile_passes_for_deposit_and_withdrawal_ledgers():
    book = Portfolio("500.00", on=DAY1)
    book.record_external_flow("seed", "250.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1, 0))
    book.record_external_flow("draw", "75.50", ExternalFlowDirection.WITHDRAWAL,
                              "bank debit advice", at=at(DAY1, 1))
    book.observe_session(DAY2)
    book.record_external_flow("top-up", "10.25", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY2, 0))
    result = replay_book(book)
    assert result.passed, result.checks
    assert all(check.status == "PASS" for check in result.checks)
    assert result.events_checked == 3
    assert book.balances.cash == D("684.75")


@pytest.mark.parametrize("tamper", (
    lambda e: replace(e, postings=(Posting("cash", e.amount),
                                   Posting("external_flows", -e.amount))),
    lambda e: replace(e, postings=(Posting("cash", e.amount),
                                   Posting("dividend_receivable", e.amount))),
    lambda e: replace(e, reason="withdrawal"),
    lambda e: replace(e, amount=e.amount + D("0.01")),
    lambda e: replace(e, reason="transfer"),
    lambda e: replace(e, citation="   "),
    lambda e: replace(e, claim_id="trade:invented"),
))
def test_replay_rejects_tampered_external_flow_recipes(tamper):
    book = flow_book()
    events = list(book.events)
    events[0] = tamper(events[0])
    result = replay_book(book, tuple(events))
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"


def test_replay_rejects_coerced_withdrawal_when_any_payable_is_unknown():
    book = Portfolio("1000.00", on=DAY1,
                     claims=(Claim("unknown-tax", ClaimKind.OTHER_PAYABLE, None,
                                   "unresolved tax demand"),))
    book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1))
    event = replace(book.events[0], reason="withdrawal",
                    postings=(Posting("cash", D("-10.00")),
                              Posting("external_flows", D("-10.00"))))
    current = replace(book.snapshot, cash=D("990.00"))
    result = reconcile(book.initial, (event,), current, sessions=book.observed_sessions)
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"
    assert "unknown" in result.checks[0].detail


@pytest.mark.parametrize("event_id", ("not-a-flow-id", "flow:", "flow:   "))
def test_replay_rejects_noncanonical_or_empty_external_flow_event_ids(event_id):
    book = flow_book()
    event = replace(book.events[0], event_id=event_id)
    result = replay_book(book, (event,) + book.events[1:])
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"
    assert "event ID" in result.checks[0].detail


@pytest.mark.parametrize("field,value", (
    ("price", D("1.00")),
    ("fee", D("1.00")),
    ("released_reserve", D("1.00")),
    ("delivery_quantity", 1),
    ("fees", (("brokerage", D("1.00")),)),
    ("factor", 2),
    ("canceled_orders", ("o",)),
    ("reference_price", D("1.00")),
    ("cost_spec", object()),
    ("execution_spec", object()),
    ("evidence", object()),
    ("dividend_policy", object()),
    ("payment_on", DAY1),
    ("payment_available_at", at(DAY1)),
    ("payment_model", "unsupported"),
    ("capacity_notional", D("1.00")),
    ("rank", 1),
    ("requested_quantity", 1),
))
def test_replay_rejects_nondefault_unrelated_external_flow_fields(field, value):
    book = flow_book()
    event = replace(book.events[0], **{field: value})
    result = replay_book(book, (event,) + book.events[1:])
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"
    assert "unrelated economic event fields" in result.checks[0].detail


def test_replay_rollforward_rejects_a_coherently_reposted_flow_amount():
    book = flow_book()
    original = book.events[0]
    forged = replace(original, amount=D("260.00"),
                     postings=(Posting("cash", D("260.00")),
                               Posting("external_flows", D("260.00"))))
    result = replay_book(book, (forged,) + book.events[1:])
    assert not result.passed
    assert next(c for c in result.checks if c.name == "cash_conservation").status == "FAIL"


def test_replay_detects_missing_and_duplicated_external_flow_events():
    book = flow_book()

    missing = tuple(e for e in book.events if e.event_id != "flow:draw")
    result = replay_book(book, missing)
    assert not result.passed
    assert next(c for c in result.checks if c.name == "cash_conservation").status == "FAIL"

    duplicate = book.events + (book.events[-1],)
    result = replay_book(book, duplicate)
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"


def test_deposit_funds_a_later_buy_without_fabricating_a_claim():
    book = Portfolio("0.00", on=DAY1)
    book.record_external_flow("seed", "1000.00", ExternalFlowDirection.DEPOSIT,
                              "initial funding advice", at=at(DAY1, 0))
    assert book.snapshot.claims == () and book.balances.cash == D("1000.00")
    book.reserve("o", "AAA", 3, "300.00", "300.00", at=at(DAY1, 0, Phase.CLOSE_ORDERS))
    book.observe_session(DAY2)
    quote = book.buy("o", OpeningPrint("AAA", DAY2, "100.00"), flat_costs(0), execution(),
                     at=at(DAY2, 0, Phase.OPEN_FILLS))
    assert quote.quantity == 3 and quote.notional == D("300.00")
    claims = book.snapshot.claims
    assert len(claims) == 1
    assert claims[0].claim_id == "trade:buy:o"
    assert claims[0].kind == ClaimKind.BUY_PAYABLE and claims[0].amount == D("300.00")
    assert book.balances.cash == D("1000.00")
    assert book.balances.liabilities == D("300.00")
    assert book.balances.cash_available == D("700.00")
    result = replay_book(book)
    assert result.passed, result.checks


def test_withdrawal_cannot_evade_settlement_funding():
    book = Portfolio("0.00", on=DAY1)
    book.record_external_flow("seed", "1000.00", ExternalFlowDirection.DEPOSIT,
                              "initial funding advice", at=at(DAY1, 0))
    book.reserve("o", "AAA", 3, "300.00", "300.00", at=at(DAY1, 0, Phase.CLOSE_ORDERS))
    book.observe_session(DAY2)
    book.buy("o", OpeningPrint("AAA", DAY2, "100.00"), flat_costs(0), execution(),
             at=at(DAY2, 0, Phase.OPEN_FILLS))
    book.observe_session(DAY3)
    before = book.snapshot
    with pytest.raises(ValueError, match="reserved or payable"):
        book.record_external_flow("draw", "700.01", ExternalFlowDirection.WITHDRAWAL,
                                  "bank debit advice", at=at(DAY3))
    assert book.snapshot == before
    book.record_external_flow("draw", "700.00", ExternalFlowDirection.WITHDRAWAL,
                              "free cash only", at=at(DAY3, 1))
    assert book.balances.cash == D("300.00")
    assert book.balances.cash_available == D("0.00")
    book.settle("trade:buy:o", at=at(DAY3, 0, Phase.PRE_OPEN_SETTLEMENT))
    assert book.balances.cash == D("0.00")
    assert book.balances.liabilities == D("0.00")
    result = replay_book(book)
    assert result.passed, result.checks


# ---------------------------------------------------------------------------
# Unknown-payable conservation boundary (AT-RECON-04)
# ---------------------------------------------------------------------------

def unknown_payable_book(cash="1000.00"):
    return Portfolio(cash, on=DAY1,
                     claims=(Claim("unknown-tax", ClaimKind.OTHER_PAYABLE, None,
                                   "unresolved tax demand"),))


def test_kernel_valid_deposit_with_unknown_payable_reconciles_explicitly():
    book = unknown_payable_book()
    book.record_external_flow("seed", "25.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1))
    assert book.balances.cash == D("1025.00")
    assert book.balances.cash_available is None
    result = replay_book(book)
    assert result.passed, result.checks
    funded = next(check for check in result.checks if check.name == "funded_cash")
    assert funded.status == "PASS"
    assert "unknown_liability_admission" in funded.detail
    assert "no withdrawal" in funded.detail
    assert "deposit is admissible" in funded.detail


def test_withdrawal_under_unknown_payable_still_fails_kernel_and_replay():
    book = unknown_payable_book()
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="Unknown liabilities"):
        book.record_external_flow("draw", "10.00", ExternalFlowDirection.WITHDRAWAL,
                                  "bank debit advice", at=at(DAY1))
    assert book.snapshot == before and book.events == events

    # A coerced withdrawal event is refused by replay before funded_cash can admit it.
    book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1, 1))
    coerced = replace(book.events[0], reason="withdrawal",
                      postings=(Posting("cash", D("-10.00")),
                                Posting("external_flows", D("-10.00"))))
    result = reconcile(book.initial, (coerced,), replace(book.snapshot, cash=D("990.00")),
                       sessions=book.observed_sessions)
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"
    assert "unknown" in result.checks[0].detail


def test_funded_cash_admission_names_the_unknown_liability_boundary():
    from nse_pipeline.backtest.invariants import _funded_cash_admission

    passed, detail = _funded_cash_admission(None, None,
                                            withdrawals_under_unknown_liability=0)
    assert passed and "no withdrawal" in detail
    passed, detail = _funded_cash_admission(None, None,
                                            withdrawals_under_unknown_liability=1)
    assert not passed and "withdrawal occurred" in detail
    passed, detail = _funded_cash_admission(None, D("0.00"),
                                            withdrawals_under_unknown_liability=0)
    assert not passed and "snapshot reports a known amount" in detail

    # Known-availability behavior is unchanged: nonnegative and exactly equal.
    assert _funded_cash_admission(D("0.00"), D("0.00"),
                                  withdrawals_under_unknown_liability=0)[0]
    assert not _funded_cash_admission(D("-0.01"), D("-0.01"),
                                      withdrawals_under_unknown_liability=0)[0]
    assert not _funded_cash_admission(D("10.00"), D("9.99"),
                                      withdrawals_under_unknown_liability=0)[0]


def test_known_payable_withdrawal_boundary_and_funded_cash_are_unchanged():
    book = Portfolio("1000.00", on=DAY1)
    book.recognize_liability("tax", "250.00", "verified tax demand",
                             Due(Phase.POST_OPEN_SETTLEMENT, on=DAY2),
                             at=at(DAY1, 0, Phase.CLOSE_VALUATION))
    book.observe_session(DAY2)
    book.record_external_flow("draw", "750.00", ExternalFlowDirection.WITHDRAWAL,
                              "free cash only", at=at(DAY2))
    assert book.balances.cash == D("250.00")
    assert book.balances.cash_available == D("0.00")
    result = replay_book(book)
    assert result.passed, result.checks
    funded = next(check for check in result.checks if check.name == "funded_cash")
    assert funded.status == "PASS" and funded.detail == ""

    other = Portfolio("1000.00", on=DAY1)
    other.recognize_liability("tax", "250.00", "verified tax demand",
                              Due(Phase.POST_OPEN_SETTLEMENT, on=DAY2),
                              at=at(DAY1, 0, Phase.CLOSE_VALUATION))
    other.observe_session(DAY2)
    before, events = other.snapshot, other.events
    with pytest.raises(ValueError, match="reserved or payable"):
        other.record_external_flow("draw", "750.01", ExternalFlowDirection.WITHDRAWAL,
                                   "bank debit advice", at=at(DAY2))
    assert other.snapshot == before and other.events == events


# ---------------------------------------------------------------------------
# Replay-verifiable citation/source digest binding
# ---------------------------------------------------------------------------

SOURCE_DIGEST = "a" * 64


def bound_flow_book():
    book = Portfolio("1000.00", on=DAY1)
    book.record_external_flow("seed", "250.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice", at=at(DAY1, 0),
                              source_digest=SOURCE_DIGEST)
    return book


def test_kernel_binds_flow_citation_to_supplied_source_digest_and_replay_verifies():
    from nse_pipeline.backtest.flow_provenance import external_flow_citation_digest

    book = bound_flow_book()
    event = book.events[0]
    expected = external_flow_citation_digest(
        "seed", D("250.00"), "deposit", "bank credit advice", at(DAY1, 0), SOURCE_DIGEST)
    assert event.flow_source_digest == SOURCE_DIGEST
    assert event.flow_citation_digest == expected
    result = replay_book(book)
    assert result.passed, result.checks

    # Omitting the optional digest remains backward compatible.
    plain = Portfolio("1000.00", on=DAY1)
    plain.record_external_flow("seed", "250.00", ExternalFlowDirection.DEPOSIT,
                               "bank credit advice", at=at(DAY1, 0))
    assert plain.events[0].flow_source_digest == ""
    assert plain.events[0].flow_citation_digest == ""
    assert replay_book(plain).passed


def test_invalid_source_digest_rejects_atomically():
    book = Portfolio("1000.00", on=DAY1)
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError, match="source digest"):
        book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                                  "bank credit advice", at=at(DAY1),
                                  source_digest="not-a-sha256")
    assert book.snapshot == before and book.events == events


@pytest.mark.parametrize("tamper", (
    lambda e: replace(e, citation="substituted bank credit advice"),
    lambda e: replace(e, flow_source_digest="b" * 64),
))
def test_replay_rejects_citation_or_source_digest_substitution(tamper):
    book = bound_flow_book()
    events = list(book.events)
    events[0] = tamper(events[0])
    result = replay_book(book, tuple(events))
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"
    assert "citation/source digest substitution" in result.checks[0].detail


def test_replay_rejects_a_partial_citation_digest_binding():
    book = bound_flow_book()
    event = replace(book.events[0], flow_citation_digest="")
    result = replay_book(book, (event,))
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert "incomplete citation/source digest binding" in result.checks[0].detail


def test_replay_rejects_flow_digest_metadata_on_a_nonflow_event():
    book = bound_flow_book()
    event = replace(book.events[0], kind=EventKind.CANCEL,
                    at=at(DAY1, 0, Phase.CLOSE_ORDERS))
    result = replay_book(book, (event,))
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert "Unrelated event kind carries external-flow provenance" in result.checks[0].detail
