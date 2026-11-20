"""Bounded proof for replay independence, fee rounding and phase-10 flow replay.

This file supplements, and does not replace, the original goldens or the existing
reconciliation/replay suites. It answers three specific questions:

1. Does ``reconcile`` ever delegate to production ``Portfolio`` recording or
   accounting methods, production fee/rounding helpers, or production valuation
   functions while it replays and while it detects corruption?
2. Does the replay's fee recipe independently reproduce a literal
   ``ROUND_HALF_UP`` result at the half-paise (``0.005``) boundary for both a
   buy and a sell, and does it reject an internally coherent half-even or
   truncated event whose only defect is the fee recipe?
3. Does a phase-10-only external flow get rejected by replay when its event is
   moved to any other phase?

Every expected fee value below is a hand-derived literal. The test never calls
production ``money``, ``fee_breakdown``, ``opening_quote``, ``affordable_buy``,
``bounded_sell``, a valuation function or a ``Portfolio`` recording method to
derive an expectation. The production callables are only installed as tripwires
after the valid and corrupted ledgers have been constructed normally.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
import builtins
import importlib

import pytest

from nse_pipeline.backtest.accounting_types import (
    EventKind,
    EventTime,
    ExternalFlowDirection,
    OpeningPrint,
    Phase,
    Position,
    Posting,
)
from nse_pipeline.backtest.core import Portfolio
from nse_pipeline.backtest.invariants import reconcile
from nse_pipeline.specs import CostSpec, ExecutionSpec

DAY1 = date(2024, 6, 3)
DAY2 = date(2024, 6, 4)
DAY3 = date(2024, 6, 5)
DAY4 = date(2024, 6, 6)
DAY5 = date(2024, 6, 7)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def half_paise_costs():
    """Pinned costs where every raw fee component is exactly half a paise.

    For a 100.00 principal: brokerage, STT, stamp and exchange are each
    100.00 * 0.5 / 10000 == 0.005. GST is 50% of the rounded 0.01 brokerage,
    so it is 0.005 too. ``ROUND_HALF_UP`` must yield 0.01 for each component.
    """
    return CostSpec(
        brokerage_bps=0.5,
        brokerage_cap_per_order=1000,
        stt_bps_buy=0.5,
        stt_bps_sell=0.5,
        stamp_bps_buy=0.5,
        exchange_bps=0.5,
        gst_on_brokerage_pct=50,
    )


def zero_slippage_execution():
    return ExecutionSpec(
        trade_settlement_lag_sessions=1,
        adverse_slippage_bps_buy=0,
        adverse_slippage_bps_sell=0,
    )


def half_paise_buy_book():
    book = Portfolio("1000.00", on=DAY1)
    book.reserve("o", "AAA", 1, "200.00", "200.00",
                 at=EventTime(DAY1, Phase.CLOSE_ORDERS))
    book.observe_session(DAY2)
    book.buy("o", OpeningPrint("AAA", DAY2, "100.00"),
             half_paise_costs(), zero_slippage_execution(),
             at=EventTime(DAY2, Phase.OPEN_FILLS))
    return book


def half_paise_sell_book():
    book = Portfolio("0.00", on=DAY1,
                     positions=(Position("p", "AAA", 1, date(2024, 5, 31)),))
    book.sell("s", "p", 1, OpeningPrint("AAA", DAY1, "100.00"),
              half_paise_costs(), zero_slippage_execution(),
              at=EventTime(DAY1, Phase.OPEN_FILLS))
    return book


def independence_book():
    """A normal lifecycle exercising flow, reserve, buy, settle, sell and settle.

    The trailing observed session has no event, so ``reconcile`` also takes its
    lazy ``EventTime`` import path. That makes the dynamic-import test observe a
    real import while replay runs, not merely an empty hook.
    """
    costs, execution = half_paise_costs(), zero_slippage_execution()
    book = Portfolio("1000.00", on=DAY1)
    book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice",
                              at=EventTime(DAY1, Phase.PRE_OPEN_EVIDENCE))
    book.reserve("o", "AAA", 1, "200.00", "200.00",
                 at=EventTime(DAY1, Phase.CLOSE_ORDERS))
    book.observe_session(DAY2)
    book.buy("o", OpeningPrint("AAA", DAY2, "100.00"), costs, execution,
             at=EventTime(DAY2, Phase.OPEN_FILLS))
    book.observe_session(DAY3)
    book.settle("trade:buy:o", at=EventTime(DAY3, Phase.PRE_OPEN_SETTLEMENT))
    book.sell("s", "position:buy:o", 1, OpeningPrint("AAA", DAY3, "100.00"),
              costs, execution, at=EventTime(DAY3, Phase.OPEN_FILLS))
    book.observe_session(DAY4)
    book.settle("trade:sell:s", at=EventTime(DAY4, Phase.PRE_OPEN_SETTLEMENT))
    book.observe_session(DAY5)
    return book


def replay(book, events=None, current=None):
    return reconcile(book.initial, book.events if events is None else events,
                     book.snapshot if current is None else current,
                     sessions=book.observed_sessions)


# ---------------------------------------------------------------------------
# Dynamic independence: production callables must not run during replay
# ---------------------------------------------------------------------------

PRODUCTION_PORTFOLIO_METHODS = (
    "observe_session",
    "record_external_flow",
    "reserve",
    "cancel",
    "buy",
    "sell",
    "settle",
    "apply_share_action",
    "entitle_dividend",
    "accrue_dividend",
    "confirm_dividend_payment",
    "void_dividend",
    "correct_opening_dividend",
    "recognize_liability",
    "redeem",
    "close",
    "_check",
    "_matured",
    "_future_settlement_phase",
    "_validate_funding",
    "_record",
    "_position",
    "_with_position",
    "_opening",
    "_unused_claim",
    "_claim_key",
)

PRODUCTION_FEE_CALLABLES = (
    "fee_breakdown",
    "opening_quote",
    "affordable_buy",
    "bounded_sell",
    "principal_cap",
    "within_notional_cap",
    "floored_capacity",
    "_floored_principal_cap",
)

PRODUCTION_VALUATION_CALLABLES = ("value", "_index")


def _trap(calls, label):
    def refused(*args, **kwargs):
        calls.append(label)
        raise AssertionError(f"independent replay delegated to production {label}")

    return refused


def install_production_tripwires(monkeypatch):
    """Replace production callables with asserting, call-counting traps."""
    calls = []
    from nse_pipeline.backtest import accounting_types, fees, valuation

    for name in PRODUCTION_PORTFOLIO_METHODS:
        monkeypatch.setattr(Portfolio, name, _trap(calls, f"Portfolio.{name}"))
    for name in PRODUCTION_FEE_CALLABLES:
        monkeypatch.setattr(fees, name, _trap(calls, f"fees.{name}"))
    for name in PRODUCTION_VALUATION_CALLABLES:
        monkeypatch.setattr(valuation, name, _trap(calls, f"valuation.{name}"))
    # Production money() is the shared half-up rounding helper. Replay has its
    # own _round; calling this would be precisely the shared-rounding defect.
    monkeypatch.setattr(accounting_types, "money", _trap(calls, "accounting_types.money"))
    return calls


def _forged_buy_event_and_state(book):
    """Coherent half-even buy event: the only defect is the fee recipe."""
    event = book.events[-1]
    assert event.kind is EventKind.BUY
    fees = (("brokerage", D("0.00")), ("gst", D("0.00")), ("stt", D("0.00")),
            ("stamp", D("0.00")), ("exchange", D("0.00")))
    amount = D("100.00")
    postings = tuple(
        replace(posting, amount=amount) if posting.account == "liabilities" else posting
        for posting in event.postings
    )
    forged = replace(event, fees=fees, fee=D("0.00"), amount=amount,
                     postings=postings)
    current = replace(book.snapshot, claims=tuple(
        replace(claim, amount=amount) if claim.claim_id == event.claim_id else claim
        for claim in book.snapshot.claims
    ))
    return forged, current


def _forged_sell_event_and_state(book):
    """Coherent truncated sell event: the only defect is the fee recipe."""
    event = book.events[-1]
    assert event.kind is EventKind.SELL
    fees = (("brokerage", D("0.00")), ("gst", D("0.00")), ("stt", D("0.00")),
            ("stamp", D("0")), ("exchange", D("0.00")))
    amount = D("100.00")
    postings = tuple(
        replace(posting, amount=amount) if posting.account == "sale_receivable" else posting
        for posting in event.postings
    )
    forged = replace(event, fees=fees, fee=D("0.00"), amount=amount,
                     postings=postings)
    current = replace(book.snapshot, claims=tuple(
        replace(claim, amount=amount) if claim.claim_id == event.claim_id else claim
        for claim in book.snapshot.claims
    ))
    return forged, current


def test_replay_is_independent_of_production_callables(monkeypatch):
    """Valid replays pass and corruptions fail without any production call.

    The tripwires are installed only after the valid ledger has been built
    through normal production operations and the corrupted variants have been
    derived from it. A positive control invokes one trap directly, proving the
    empty call list is a real observation rather than an unused fixture.
    """
    book = independence_book()
    valid = replay(book)
    assert valid.passed, valid.checks

    corrupted_cash = replace(book.snapshot, cash=book.snapshot.cash - D("0.01"))

    events = list(book.events)
    index = next(i for i, event in enumerate(events) if event.kind is EventKind.BUY)
    buy = events[index]
    forged_fees = tuple((name, value + D("0.01")) for name, value in buy.fees)
    forged_fee = sum((value for _, value in forged_fees), D("0"))
    forged_amount = buy.amount - buy.fee + forged_fee
    forged_postings = tuple(
        replace(posting, amount=forged_amount) if posting.account == "liabilities" else posting
        for posting in buy.postings
    )
    events[index] = replace(buy, fees=forged_fees, fee=forged_fee,
                            amount=forged_amount, postings=forged_postings)

    calls = install_production_tripwires(monkeypatch)

    from nse_pipeline.backtest import fees as production_fees
    with pytest.raises(AssertionError, match="production fees.fee_breakdown"):
        production_fees.fee_breakdown(D("100.00"), "buy", half_paise_costs())
    assert calls == ["fees.fee_breakdown"]
    calls.clear()

    original = replay(book)
    assert original.passed, original.checks
    assert calls == []

    bad_cash = replay(book, current=corrupted_cash)
    assert not bad_cash.passed
    assert next(check for check in bad_cash.checks
                if check.name == "cash_conservation").status == "FAIL"
    assert calls == []

    bad_fee = replay(book, events=tuple(events))
    assert not bad_fee.passed
    assert bad_fee.checks[0].name == "event_integrity"
    assert bad_fee.checks[0].status == "FAIL"
    assert "Fee components or fee total" in bad_fee.checks[0].detail
    assert calls == []


def test_replay_does_not_dynamically_import_production_modules(monkeypatch):
    """Dynamic imports of core/fees/valuation during replay are trapped.

    The wrappers are positive-controlled first. Replay then runs with the
    wrappers active; it may lazily import ``accounting_types`` (allowed), but it
    may not import the production core, fee or valuation modules.
    """
    book = independence_book()
    valid = replay(book)
    assert valid.passed, valid.checks

    forbidden = {
        "nse_pipeline.backtest.core",
        "nse_pipeline.backtest.fees",
        "nse_pipeline.backtest.valuation",
    }
    attempted = []
    real_import_module = importlib.import_module
    real_import = builtins.__import__

    def guarded_import_module(name, package=None):
        attempted.append(("import_module", name))
        if name in forbidden:
            raise AssertionError(f"replay dynamically imported production module {name}")
        return real_import_module(name, package)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        attempted.append(("import", name, level))
        if level == 0 and name in forbidden:
            raise AssertionError(f"replay dynamically imported production module {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(importlib, "import_module", guarded_import_module)
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    for guarded, module in ((guarded_import_module, "nse_pipeline.backtest.fees"),
                            (guarded_import, "nse_pipeline.backtest.valuation")):
        with pytest.raises(AssertionError, match="dynamically imported production module"):
            guarded(module)
    assert len(attempted) == 2
    attempted.clear()

    original = replay(book)
    assert original.passed, original.checks
    assert not [entry for entry in attempted if entry[1] in forbidden]
    # The trailing empty session forces reconcile's lazy EventTime import, so a
    # nonempty record proves the import hook was live during replay itself.
    assert any("accounting_types" in str(entry) for entry in attempted), attempted

    corrupted = replace(book.snapshot, cash=book.snapshot.cash - D("0.01"))
    bad = replay(book, current=corrupted)
    assert not bad.passed
    assert not [entry for entry in attempted if entry[1] in forbidden]


# ---------------------------------------------------------------------------
# Literal half-paise ROUND_HALF_UP fee recipes
# ---------------------------------------------------------------------------

def test_half_paise_buy_fees_match_literal_round_half_up_recipe(monkeypatch):
    book = half_paise_buy_book()
    event = book.events[-1]
    assert event.kind is EventKind.BUY
    assert event.quantity == 1 and event.reference_price == D("100.00")
    assert event.price == D("100.00")
    # 100.00 principal; each raw component is 0.005 and must round up to 0.01.
    assert event.fees == (
        ("brokerage", D("0.01")),
        ("gst", D("0.01")),
        ("stt", D("0.01")),
        ("stamp", D("0.01")),
        ("exchange", D("0.01")),
    )
    assert event.fee == D("0.05")
    assert event.amount == D("100.05")
    assert event.postings == (
        Posting("order_reserved", D("-200.00")),
        Posting("liabilities", D("100.05")),
        Posting("shares", 1, "position:buy:o"),
    )

    # The replay must accept the literal recipe without touching production
    # money() or any production fee helper.
    calls = []
    from nse_pipeline.backtest import accounting_types, fees
    for name in PRODUCTION_FEE_CALLABLES:
        monkeypatch.setattr(fees, name, _trap(calls, f"fees.{name}"))
    monkeypatch.setattr(accounting_types, "money", _trap(calls, "accounting_types.money"))

    result = replay(book)
    assert result.passed, result.checks
    assert calls == []


def test_half_paise_sell_fees_match_literal_round_half_up_recipe(monkeypatch):
    book = half_paise_sell_book()
    event = book.events[-1]
    assert event.kind is EventKind.SELL
    assert event.quantity == event.requested_quantity == 1
    assert event.reference_price == event.price == D("100.00")
    # A sell has no stamp duty. The other four raw components are 0.005 and
    # round up to 0.01 each; total fees are 0.04 against 100.00 proceeds.
    assert event.fees == (
        ("brokerage", D("0.01")),
        ("gst", D("0.01")),
        ("stt", D("0.01")),
        ("stamp", D("0")),
        ("exchange", D("0.01")),
    )
    assert event.fee == D("0.04")
    assert event.amount == D("99.96")
    assert event.postings == (
        Posting("shares", -1, "p"),
        Posting("delivered", -1, "p"),
        Posting("sale_receivable", D("99.96")),
    )

    calls = []
    from nse_pipeline.backtest import accounting_types, fees
    for name in PRODUCTION_FEE_CALLABLES:
        monkeypatch.setattr(fees, name, _trap(calls, f"fees.{name}"))
    monkeypatch.setattr(accounting_types, "money", _trap(calls, "accounting_types.money"))

    result = replay(book)
    assert result.passed, result.checks
    assert calls == []


def test_coherent_half_even_buy_fees_fail_independent_recipe(monkeypatch):
    """A half-even event is coherent in every field except the fee recipe.

    Under half-even, 0.005 becomes 0.00 (zero is even), so a coherent forged
    event has zero fees, a 100.00 amount and matching liabilities and claim.
    The original snapshot is adjusted to the same 100.00 amount, so the only
    remaining defect is the replay's independent half-up fee recomputation.
    """
    book = half_paise_buy_book()
    forged, current = _forged_buy_event_and_state(book)

    assert sum((value for _, value in forged.fees), D("0")) == forged.fee == D("0.00")
    assert forged.amount == D("100.00") + forged.fee
    assert forged.amount == D("100.00")
    assert next(posting.amount for posting in forged.postings
                if posting.account == "liabilities") == forged.amount
    assert next(claim.amount for claim in current.claims
                if claim.claim_id == forged.claim_id) == forged.amount

    calls = install_production_tripwires(monkeypatch)
    result = replay(book, events=book.events[:-1] + (forged,), current=current)
    assert not result.passed
    assert len(result.checks) == 1
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"
    assert "Fee components or fee total do not reconcile" in result.checks[0].detail
    assert calls == []


def test_coherent_truncated_sell_fees_fail_independent_recipe(monkeypatch):
    """A truncated sell event is coherent in every field except the fee recipe.

    Truncating 0.005 gives 0.00, so the forged event has zero fees, a 100.00
    receivable and matching postings and claim. The replay must reject it by
    recomputing the literal half-up components, not by comparing rounding
    function objects or by relying on an incoherent total.
    """
    book = half_paise_sell_book()
    forged, current = _forged_sell_event_and_state(book)

    assert sum((value for _, value in forged.fees), D("0")) == forged.fee == D("0.00")
    assert forged.amount == D("100.00") - forged.fee
    assert forged.amount == D("100.00")
    assert next(posting.amount for posting in forged.postings
                if posting.account == "sale_receivable") == forged.amount
    assert next(claim.amount for claim in current.claims
                if claim.claim_id == forged.claim_id) == forged.amount

    calls = install_production_tripwires(monkeypatch)
    result = replay(book, events=book.events[:-1] + (forged,), current=current)
    assert not result.passed
    assert len(result.checks) == 1
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"
    assert "Fee components or fee total do not reconcile" in result.checks[0].detail
    assert calls == []


# ---------------------------------------------------------------------------
# Committed non-phase-10 external-flow replay node
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", (
    Phase.PRE_OPEN_SETTLEMENT,
    Phase.PRE_OPEN_ACTIONS,
    Phase.OPEN_FILLS,
    Phase.POST_OPEN_SETTLEMENT,
    Phase.CLOSE_VALUATION,
    Phase.CLOSE_ORDERS,
    Phase.CLOSE_AUDIT,
))
def test_replay_rejects_external_flow_outside_phase_10(phase):
    book = Portfolio("1000.00", on=DAY1)
    book.record_external_flow("seed", "10.00", ExternalFlowDirection.DEPOSIT,
                              "bank credit advice",
                              at=EventTime(DAY1, Phase.PRE_OPEN_EVIDENCE))
    assert replay(book).passed

    forged = replace(book.events[0], at=EventTime(DAY1, phase))
    result = replay(book, events=(forged,))
    assert not result.passed
    assert result.checks[0].name == "event_integrity"
    assert result.checks[0].status == "FAIL"
    assert "Invalid event phase/session" in result.checks[0].detail
