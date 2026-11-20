from datetime import date
from decimal import Decimal as D
from fractions import Fraction

import pytest


DAY1 = date(2024, 6, 3)
DAY2 = date(2024, 6, 4)
DAY3 = date(2024, 6, 5)


def source(phase, *, day=DAY1, citation="fixture document", character="a"):
    from nse_pipeline.backtest.accounting_types import EventTime
    from nse_pipeline.backtest.observed_session import InputProvenance
    return InputProvenance(citation, character * 64, EventTime(day, phase),
                           "supplied fixture; not authenticated")


def components():
    from nse_pipeline.specs import CostSpec, ExecutionSpec
    # Zero modelled frictions keep hand-derived cash and unit arithmetic exact.
    costs = CostSpec(brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0,
                     stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0,
                     schedule_basis="flat_research_assumption",
                     monetary_rounding="paise_half_up_per_component")
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, fill_timing="next_session_open",
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0,
                              sizing_participation_rate=0.05, max_participation_rate=0.05,
                              adv_window_sessions=20, max_order_notional=None,
                              min_order_notional=0.0, partial_fill_policy="partial",
                              settlement_funding="settled_cash_only", unfilled_remainder="expire",
                              circuit_limit_pct=0.20, hold_suspended_positions=True)
    return costs, execution


def policy(lag=1, *, mode="verified_else_lag"):
    from nse_pipeline.specs import ValuationPolicy
    if mode == "hold_receivable":
        return ValuationPolicy(fallback_lag=None, lag_unit="calendar_days",
                               dividend_settlement_mode="hold_receivable")
    return ValuationPolicy(fallback_lag=lag, lag_unit="exchange_sessions")


def evidence(key="AAA", *, day=DAY1, verified=True, citation="issuer corporate-action fixture"):
    from nse_pipeline.backtest.accounting_types import Evidence, EventTime, Phase
    return Evidence(citation, key, day, EventTime(day, Phase.PRE_OPEN_ACTIONS), verified)


def terms(dividend_id="div-1", per_share="2", *, key="AAA", day=DAY1, payment=None):
    from nse_pipeline.backtest.dividends import DividendTerms
    return DividendTerms(dividend_id, per_share,
                         evidence(key, day=day, citation="issuer dividend fixture"), payment)


def payment(on, *, available_day=DAY1, amount=None, citation="issuer payment notice"):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.dividends import (
        GROSS_DIVIDEND_NO_WITHHOLDING_MODEL, VerifiedPayment,
    )
    return VerifiedPayment(on, EventTime(available_day, Phase.PRE_OPEN_ACTIONS), citation,
                           amount, settlement_model=GROSS_DIVIDEND_NO_WITHHOLDING_MODEL)


def close_bar(key, price, day=DAY1, character="e"):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import CloseBar
    from nse_pipeline.backtest.observations import Bar
    return CloseBar(Bar(key, key, "EQ", day, price, price, price, price, 100, True),
                    source(Phase.CLOSE_VALUATION, day=day, character=character))


def runner(*, cash="1000", positions=(), claims=(), valuation_policy=None,
           day=DAY1, character="a"):
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionRunner
    costs, execution = components()
    return SessionRunner(cash, on=day,
                         initial_provenance=source(Phase.PRE_OPEN_EVIDENCE, day=day,
                                                   character=character),
                         opening_claims=claims, opening_positions=positions,
                         costs=costs, execution=execution,
                         valuation_policy=valuation_policy)


def test_split_changes_gross_and_delivered_shares_and_preserves_exact_units():
    from nse_pipeline.backtest.accounting_types import EventKind, EventTime, Phase, Position
    from nse_pipeline.backtest.observed_session import SessionInput, ShareActionInput
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="1000", positions=(Position("held", "AAA", 5, date(2024, 5, 31)),))
    action = ShareAction("split-1", "split", 2, 1, evidence("AAA", day=DAY1))
    result = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        share_actions=(ShareActionInput(action, "held", source(Phase.PRE_OPEN_ACTIONS)),),
        close_bars=(close_bar("AAA", "50"),),
    ))

    position = book.snapshot.positions[0]
    assert (position.position_id, position.key) == ("held", "AAA")
    assert (position.shares, position.delivered_shares) == (10, 10)
    split = result.events[0]
    assert split.kind == EventKind.SPLIT
    assert split.at == EventTime(DAY1, Phase.PRE_OPEN_ACTIONS, 0)
    assert (split.quantity, split.delivery_quantity) == (5, 5)
    assert split.factor == Fraction(2, 1)
    assert tuple((p.account, p.amount, p.position_id) for p in split.postings) == (
        ("shares", D("5"), "held"), ("delivered", D("5"), "held"))
    assert result.valuation.nav == D("1500.00")  # 10 x 50 mark + 1000 cash
    assert result.reconciliation.passed


def test_order_only_action_cancels_frozen_order_and_releases_its_reserve():
    from nse_pipeline.backtest.accounting_types import EventKind, EventTime, Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="1000")
    first = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(close_bar("AAA", "100"),),
        buy_targets=(BuyTarget("order-aaa", "AAA", 10, "1000",
                               CapacityInput("1000", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    assert (first.next_orders[0].quantity, first.next_orders[0].reserved) == (10, D("1000.00"))
    assert book.snapshot.balances.cash_available == D("0.00")

    action = ShareAction("split-order-only", "split", 2, 1, evidence("AAA", day=DAY2))
    result = book.step(SessionInput(
        DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2, character="b"),
        share_actions=(ShareActionInput(action, None,
                                        source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="d")),),
    ))

    assert [event.kind for event in result.events] == [EventKind.ACTION_ORDERS_CANCELED]
    canceled = result.events[0]
    assert canceled.at == EventTime(DAY2, Phase.PRE_OPEN_ACTIONS, 0)
    assert canceled.key == "AAA"
    assert canceled.canceled_orders == ("order-aaa",)
    assert canceled.released_reserve == D("1000.00")
    assert canceled.evidence == action.evidence
    assert result.next_orders == ()
    assert result.expired_orders == ()
    assert book.snapshot.balances.order_reserved == D("0.00")
    assert book.snapshot.balances.cash == book.snapshot.balances.cash_available == D("1000.00")
    assert book.snapshot.positions == ()
    assert not [event for event in result.events if event.kind == EventKind.BUY]
    assert result.reconciliation.passed


def test_held_security_action_cancels_same_key_buy_without_open_or_halt():
    from nse_pipeline.backtest.accounting_types import EventKind, Phase, Position
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="2000", positions=(Position("held", "AAA", 5, date(2024, 5, 31)),))
    first = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(close_bar("AAA", "100"),),
        buy_targets=(BuyTarget("order-aaa", "AAA", 10, "1000",
                               CapacityInput("1000", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    assert first.next_orders[0].order_id == "order-aaa"
    action = ShareAction("split-held-cancel-buy", "split", 2, 1,
                         evidence("AAA", day=DAY2))

    result = book.step(SessionInput(
        DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2, character="b"),
        share_actions=(ShareActionInput(
            action, "held", source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="c")),),
    ))

    assert [event.kind for event in result.events] == [EventKind.SPLIT]
    assert book.snapshot.positions[0].shares == 10
    assert result.next_orders == ()
    assert result.expired_orders == ()
    assert not any(event.kind == EventKind.BUY for event in result.events)
    assert result.reconciliation.passed


def test_supplied_halt_for_buy_precedes_phase30_action_and_is_consumed():
    from nse_pipeline.backtest.accounting_types import EventKind, EventTime, Phase, Position
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, HaltEvidence, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="2000", positions=(Position("held", "AAA", 5, date(2024, 5, 31)),))
    book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(close_bar("AAA", "100"),),
        buy_targets=(BuyTarget("order-aaa", "AAA", 10, "1000",
                               CapacityInput("1000", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    action = ShareAction("split-after-halt", "split", 2, 1, evidence("AAA", day=DAY2))
    halt_provenance = source(Phase.OPEN_FILLS, day=DAY2, character="d")
    result = book.step(SessionInput(
        DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2, character="b"),
        halt_evidence=(HaltEvidence("order-aaa", halt_provenance),),
        share_actions=(ShareActionInput(
            action, "held", source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="c")),),
    ))

    assert [event.kind for event in result.events] == [
        EventKind.CANCEL, EventKind.SPLIT,
    ]
    assert [event.at for event in result.events] == [
        EventTime(DAY2, Phase.PRE_OPEN_ACTIONS, 0),
        EventTime(DAY2, Phase.PRE_OPEN_ACTIONS, 1),
    ]
    assert result.expired_orders[0].order_id == "order-aaa"
    assert halt_provenance in result.consumed_provenance
    assert result.events[1].canceled_orders == ()
    assert result.next_orders == ()
    assert result.reconciliation.passed


def test_missing_print_still_rejects_buy_left_live_after_phase30():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="2000", positions=(Position("held-bbb", "BBB", 4,
                                                        date(2024, 5, 31)),))
    book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(close_bar("AAA", "100"),),
        buy_targets=(BuyTarget("order-aaa", "AAA", 5, "500",
                               CapacityInput("500", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    before = book.snapshot, book.events, book.result
    action = ShareAction("split-bbb", "split", 2, 1, evidence("BBB", day=DAY2))

    with pytest.raises(ValueError, match="Missing opening print for security AAA"):
        book.step(SessionInput(
            DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2, character="b"),
            share_actions=(ShareActionInput(
                action, "held-bbb", source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="c")),),
        ))
    assert (book.snapshot, book.events, book.result) == before


def test_unverified_share_action_blocks_position_with_explicit_reason():
    from nse_pipeline.backtest.accounting_types import EventKind, Phase, Position
    from nse_pipeline.backtest.observed_session import SessionInput, ShareActionInput
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="1000", positions=(Position("held", "AAA", 4, date(2024, 5, 31)),))
    action = ShareAction("unverified-split", "split", 2, 1,
                         evidence("AAA", day=DAY1, verified=False))
    result = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        share_actions=(ShareActionInput(action, "held", source(Phase.PRE_OPEN_ACTIONS)),),
    ))

    position = book.snapshot.positions[0]
    assert (position.shares, position.delivered_shares) == (4, 4)
    assert position.blocked_reason == "unverified_action_terms"
    blocked = result.events[0]
    assert blocked.kind == EventKind.ACTION_BLOCKED
    assert blocked.reason == "unverified_action_terms"
    assert (blocked.quantity, blocked.delivery_quantity) == (4, 4)
    assert blocked.postings == ()
    assert book.snapshot.balances.cash == D("1000.00")
    assert result.valuation.nav is None
    assert result.valuation.unresolved_positions == ("held",)
    assert result.reconciliation.passed


def test_entitlement_accrual_same_day_phase_50_settlement_moves_cash_exactly_once():
    from nse_pipeline.backtest.accounting_types import (
        Due, EventKind, EventTime, Phase, Position,
    )
    from nse_pipeline.backtest.observed_session import (
        DividendAccrualInput, DividendEntitlementInput, SessionInput,
    )

    book = runner(cash="10000", positions=(Position("held", "AAA", 100, date(2024, 5, 31)),),
                  valuation_policy=policy())
    result = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        dividend_entitlements=(DividendEntitlementInput(
            "held", terms("div-1", "2", payment=payment(DAY1)),
            source(Phase.PRE_OPEN_ACTIONS)),),
        dividend_accruals=(DividendAccrualInput(
            "div-1", source(Phase.PRE_OPEN_ACTIONS, character="b")),),
        close_bars=(close_bar("AAA", "100"),),
    ))

    assert [event.kind for event in result.events] == [
        EventKind.DIVIDEND_ENTITLEMENT, EventKind.DIVIDEND_ACCRUAL, EventKind.SETTLE_DIVIDEND]
    assert tuple(event.at for event in result.events) == (
        EventTime(DAY1, Phase.PRE_OPEN_ACTIONS, 0),
        EventTime(DAY1, Phase.PRE_OPEN_ACTIONS, 1),
        EventTime(DAY1, Phase.POST_OPEN_SETTLEMENT, 2),
    )
    assert result.events[1].amount == D("200.00")  # 100 frozen shares x 2.00
    assert tuple((p.account, p.amount) for p in result.events[1].postings) == (
        ("dividend_receivable", D("200.00")),)
    assert result.events[2].amount == D("200.00")
    assert tuple((p.account, p.amount) for p in result.events[2].postings) == (
        ("cash", D("200.00")), ("dividend_receivable", D("-200.00")))
    entitlement = book.snapshot.entitlements[0]
    assert (entitlement.dividend_id, entitlement.shares, entitlement.accrued) == ("div-1", 100, True)
    assert entitlement.due == Due(Phase.POST_OPEN_SETTLEMENT, on=DAY1)
    assert book.snapshot.balances.cash == D("10200.00")
    assert book.snapshot.balances.dividend_receivable == D("0.00")
    assert book.snapshot.balances.cash_available == D("10200.00")
    assert len([e for e in result.events if e.kind == EventKind.SETTLE_DIVIDEND]) == 1
    assert result.valuation.nav == D("20200.00")  # 10000 shares mark + 10200 cash
    assert result.valuation.unresolved_settlement_claims == ()
    assert result.reconciliation.passed


def test_hold_receivable_leaves_accrued_held_claim_and_no_cash():
    from nse_pipeline.backtest.accounting_types import EventKind, Phase, Position
    from nse_pipeline.backtest.observed_session import (
        DividendAccrualInput, DividendEntitlementInput, SessionInput,
    )

    book = runner(cash="10000", positions=(Position("held", "AAA", 100, date(2024, 5, 31)),),
                  valuation_policy=policy(mode="hold_receivable"))
    result = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        dividend_entitlements=(DividendEntitlementInput(
            "held", terms("div-1", "2"), source(Phase.PRE_OPEN_ACTIONS)),),
        dividend_accruals=(DividendAccrualInput(
            "div-1", source(Phase.PRE_OPEN_ACTIONS, character="b")),),
        close_bars=(close_bar("AAA", "100"),),
    ))

    claim = next(c for c in book.snapshot.claims if c.claim_id == "dividend:div-1")
    assert claim.amount == D("200.00")
    assert claim.due is None
    assert claim.state == "accrued_held"
    assert claim.unresolved_reason == "hold_receivable"
    assert [event.kind for event in result.events] == [
        EventKind.DIVIDEND_ENTITLEMENT, EventKind.DIVIDEND_ACCRUAL]
    assert book.snapshot.balances.cash == D("10000.00")
    assert book.snapshot.balances.cash_available == D("10000.00")
    assert book.snapshot.balances.dividend_receivable == D("200.00")
    assert result.valuation.nav == D("20200.00")  # 10000 shares mark + 10000 cash + 200 held
    assert result.valuation.unresolved_settlement_claims == ("dividend:div-1",)
    assert result.reconciliation.passed


def test_ex_date_buy_filled_at_phase_40_is_not_entitled():
    from nse_pipeline.backtest.accounting_types import (
        EventKind, EventTime, OpeningPrint, Phase, Position,
    )
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, DividendAccrualInput, DividendEntitlementInput,
        OpeningObservation, SessionInput,
    )

    book = runner(cash="1000", positions=(Position("held", "AAA", 4, date(2024, 5, 31)),),
                  valuation_policy=policy())
    book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(close_bar("AAA", "100"),),
        buy_targets=(BuyTarget("order-aaa", "AAA", 6, "1000",
                               CapacityInput("1000", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    result = book.step(SessionInput(
        DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2, character="b"),
        opening_prints=(OpeningObservation(OpeningPrint("AAA", DAY2, "100"),
                                           source(Phase.OPEN_FILLS, day=DAY2, character="c")),),
        dividend_entitlements=(DividendEntitlementInput(
            "held", terms("div-1", "2", day=DAY2, payment=payment(DAY2, available_day=DAY2)),
            source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="d")),),
        dividend_accruals=(DividendAccrualInput(
            "div-1", source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="e")),),
        close_bars=(close_bar("AAA", "100", day=DAY2, character="f"),),
    ))

    entitlement = result.events[0]
    accrual = result.events[1]
    buy = next(event for event in result.events if event.kind == EventKind.BUY)
    assert entitlement.kind == EventKind.DIVIDEND_ENTITLEMENT
    assert entitlement.at == EventTime(DAY2, Phase.PRE_OPEN_ACTIONS, 0)
    assert entitlement.quantity == 4  # the phase-40 purchase is not cum-dividend
    assert accrual.amount == D("8.00")  # 4 frozen shares x 2.00
    assert buy.at == EventTime(DAY2, Phase.OPEN_FILLS, 2)
    assert buy.quantity == 6
    assert result.events.index(entitlement) < result.events.index(buy)
    position = next(p for p in book.snapshot.positions if p.position_id == "held")
    assert (position.shares, position.delivered_shares) == (10, 4)
    assert book.snapshot.entitlements[0].shares == 4
    settled = next(e for e in result.events if e.kind == EventKind.SETTLE_DIVIDEND)
    assert settled.amount == D("8.00")
    assert book.snapshot.balances.cash == D("1008.00")
    assert book.snapshot.balances.dividend_receivable == D("0.00")
    assert book.snapshot.balances.liabilities == D("600.00")
    assert book.snapshot.balances.cash_available == D("408.00")
    assert result.valuation.nav == D("1408.00")  # 1000 marked + 1008 cash - 600 payable
    assert result.reconciliation.passed


def test_share_action_rejects_stale_frozen_sell_quantity_atomically():
    from nse_pipeline.backtest.accounting_types import OpeningPrint, Phase, Position
    from nse_pipeline.backtest.observed_session import (
        OpeningObservation, SellTarget, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="1000", positions=(Position("held", "AAA", 10, date(2024, 5, 31)),))
    first = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        sell_targets=(SellTarget("sell-held", "held", 5, 1, source(Phase.CLOSE_ORDERS)),),
    ))
    before = book.snapshot, book.events, book.result
    action = ShareAction("split-before-sell", "split", 2, 1,
                         evidence("AAA", day=DAY2))
    with pytest.raises(ValueError, match="invalidate frozen sell quantity"):
        book.step(SessionInput(
            DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2, character="b"),
            opening_prints=(OpeningObservation(OpeningPrint("AAA", DAY2, "50"),
                                               source(Phase.OPEN_FILLS, day=DAY2, character="c")),),
            share_actions=(ShareActionInput(
                action, "held", source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="d")),),
        ))
    assert (book.snapshot, book.events, book.result) == before == (
        book.snapshot, book.events, (first,))
    held = book.snapshot.positions[0]
    assert (held.shares, held.delivered_shares, held.blocked_reason) == (10, 10, "")



def test_same_session_split_and_dividend_is_blocked_and_quarantined():
    from nse_pipeline.backtest.accounting_types import EventKind, Phase, Position
    from nse_pipeline.backtest.observed_session import (
        DividendEntitlementInput, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="1000", positions=(Position("held", "AAA", 10, date(2024, 5, 31)),),
                  valuation_policy=policy())
    result = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        share_actions=(ShareActionInput(
            ShareAction("split-1", "split", 2, 1, evidence("AAA", day=DAY1)),
            "held", source(Phase.PRE_OPEN_ACTIONS)),),
        dividend_entitlements=(DividendEntitlementInput(
            "held", terms("div-1", "2"), source(Phase.PRE_OPEN_ACTIONS, character="b")),),
    ))

    assert [event.kind for event in result.events] == [
        EventKind.SPLIT, EventKind.DIVIDEND_BLOCKED]
    position = book.snapshot.positions[0]
    assert (position.shares, position.delivered_shares) == (20, 20)
    assert position.blocked_reason == "simultaneous_share_dividend_units_unsupported"
    assert result.events[1].reason == "simultaneous_share_dividend_units_unsupported"
    assert book.snapshot.entitlements == ()
    assert book.snapshot.claims == ()
    assert book.snapshot.balances.cash == D("1000.00")
    assert book.snapshot.balances.dividend_receivable == D("0.00")
    assert result.valuation.nav is None
    assert result.valuation.nav_reason == "unpriced_positions"
    assert result.reconciliation.passed


def test_missing_valuation_policy_with_entitlement_rejects_atomically():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        DividendEntitlementInput, SessionInput, SessionRunner,
    )

    costs, execution = components()
    with pytest.raises(ValueError, match="ValuationPolicy"):
        SessionRunner("1000", on=DAY1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                      costs=costs, execution=execution, valuation_policy="hold_receivable")
    book = runner(cash="1000", positions=(Position("held", "AAA", 10, date(2024, 5, 31)),))
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="ValuationPolicy"):
        book.step(SessionInput(
            DAY1, source(Phase.PRE_OPEN_EVIDENCE),
            dividend_entitlements=(DividendEntitlementInput(
                "held", terms("div-1"), source(Phase.PRE_OPEN_ACTIONS)),),
        ))
    assert (book.snapshot, book.events, book.result) == before
    accepted = book.step(SessionInput(DAY1, source(Phase.PRE_OPEN_EVIDENCE)))
    assert (accepted.on, len(book.result)) == (DAY1, 1)


def test_unavailable_phase30_provenance_rejects_atomically_and_keeps_prefix():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        DividendAccrualInput, DividendConfirmationInput, DividendCorrectionInput,
        DividendEntitlementInput, DividendVoidInput, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="1000", positions=(Position("held", "AAA", 5, date(2024, 5, 31)),),
                  valuation_policy=policy())
    first = book.step(SessionInput(DAY1, source(Phase.PRE_OPEN_EVIDENCE)))
    late = source(Phase.OPEN_FILLS, day=DAY2, character="f")
    action = ShareAction("split-1", "split", 2, 1, evidence("AAA", day=DAY2))
    cases = (
        {"share_actions": (ShareActionInput(action, "held", late),)},
        {"dividend_entitlements": (DividendEntitlementInput(
            "held", terms("div-1", day=DAY2), late),)},
        {"dividend_accruals": (DividendAccrualInput("div-1", late),)},
        {"dividend_confirmations": (DividendConfirmationInput(
            "div-1", payment(DAY3, available_day=DAY2), late),)},
        {"dividend_voids": (DividendVoidInput(
            "div-1", evidence("AAA", day=DAY2), "issuer withdrew the dividend", late),)},
        {"dividend_corrections": (DividendCorrectionInput(
            "div-1", "100", evidence("AAA", day=DAY2), "issuer corrected the amount", late),)},
    )
    for case in cases:
        before = book.snapshot, book.events, book.result
        with pytest.raises(ValueError, match="unavailable"):
            book.step(SessionInput(DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2), **case))
        assert (book.snapshot, book.events, book.result) == before == (
            book.snapshot, book.events, (first,))
    assert book.step(SessionInput(DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2))) is not None
    assert len(book.result) == 2


def test_duplicate_phase30_identifiers_reject_atomically():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        DividendAccrualInput, DividendConfirmationInput, DividendCorrectionInput,
        DividendEntitlementInput, DividendVoidInput, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="1000", positions=(Position("held", "AAA", 5, date(2024, 5, 31)),),
                  valuation_policy=policy())
    before = book.snapshot, book.events, book.result
    action = ShareAction("same-action", "split", 2, 1, evidence("AAA", day=DAY1))
    share = ShareActionInput(action, "held", source(Phase.PRE_OPEN_ACTIONS))
    entitlement = DividendEntitlementInput(
        "held", terms("same-dividend"), source(Phase.PRE_OPEN_ACTIONS))
    accrual = DividendAccrualInput("same-dividend", source(Phase.PRE_OPEN_ACTIONS))
    confirmation = DividendConfirmationInput(
        "same-dividend", payment(DAY2), source(Phase.PRE_OPEN_ACTIONS))
    void = DividendVoidInput("same-dividend", evidence("AAA", day=DAY1),
                             "issuer withdrew the dividend", source(Phase.PRE_OPEN_ACTIONS))
    correction = DividendCorrectionInput("same-dividend", "100", evidence("AAA", day=DAY1),
                                         "issuer corrected the amount",
                                         source(Phase.PRE_OPEN_ACTIONS))
    cases = (
        {"share_actions": (share, share)},
        {"dividend_entitlements": (entitlement, entitlement)},
        {"dividend_accruals": (accrual, accrual)},
        {"dividend_confirmations": (confirmation, confirmation)},
        {"dividend_voids": (void, void)},
        {"dividend_corrections": (correction, correction)},
    )
    for case in cases:
        with pytest.raises(ValueError, match="unique"):
            SessionInput(DAY1, source(Phase.PRE_OPEN_EVIDENCE), **case)
        assert (book.snapshot, book.events, book.result) == before
    assert book.result == ()


def test_phase30_events_are_ordered_before_opening_fills_and_reconcile():
    from nse_pipeline.backtest.accounting_types import (
        EventKind, EventTime, OpeningPrint, Phase, Position,
    )
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, DividendAccrualInput, DividendConfirmationInput,
        DividendEntitlementInput, OpeningObservation, SessionInput, ShareActionInput,
    )
    from nse_pipeline.backtest.share_actions import ShareAction

    book = runner(cash="10000",
                  positions=(Position("held-aaa", "AAA", 5, date(2024, 5, 31)),
                             Position("held-ccc", "CCC", 4, date(2024, 5, 31))),
                  valuation_policy=policy(mode="hold_receivable"))
    book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(close_bar("BBB", "100"),),
        buy_targets=(BuyTarget("order-bbb", "BBB", 2, "1000",
                               CapacityInput("1000", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    result = book.step(SessionInput(
        DAY2, source(Phase.PRE_OPEN_EVIDENCE, day=DAY2, character="b"),
        opening_prints=(OpeningObservation(OpeningPrint("BBB", DAY2, "100"),
                                           source(Phase.OPEN_FILLS, day=DAY2, character="c")),),
        share_actions=(ShareActionInput(
            ShareAction("split-1", "split", 2, 1, evidence("AAA", day=DAY2)),
            "held-aaa", source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="d")),),
        dividend_entitlements=(DividendEntitlementInput(
            "held-ccc", terms("div-1", "2", key="CCC", day=DAY2),
            source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="e")),),
        dividend_accruals=(DividendAccrualInput(
            "div-1", source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="f")),),
        dividend_confirmations=(DividendConfirmationInput(
            "div-1", payment(DAY3, available_day=DAY2),
            source(Phase.PRE_OPEN_ACTIONS, day=DAY2, character="0")),),
        close_bars=(close_bar("AAA", "50", day=DAY2),
                    close_bar("CCC", "100", day=DAY2, character="b"),
                    close_bar("BBB", "100", day=DAY2, character="c")),
    ))

    assert [event.kind for event in result.events] == [
        EventKind.SPLIT,
        EventKind.DIVIDEND_ENTITLEMENT,
        EventKind.DIVIDEND_ACCRUAL,
        EventKind.DIVIDEND_PAYMENT_CONFIRMED,
        EventKind.BUY,
    ]
    assert tuple(event.at for event in result.events) == (
        EventTime(DAY2, Phase.PRE_OPEN_ACTIONS, 0),
        EventTime(DAY2, Phase.PRE_OPEN_ACTIONS, 1),
        EventTime(DAY2, Phase.PRE_OPEN_ACTIONS, 2),
        EventTime(DAY2, Phase.PRE_OPEN_ACTIONS, 3),
        EventTime(DAY2, Phase.OPEN_FILLS, 4),
    )
    assert all(event.at.phase == Phase.PRE_OPEN_ACTIONS for event in result.events[:4])
    assert result.events[4].at.phase == Phase.OPEN_FILLS
    assert [event.at for event in result.events] == sorted(event.at for event in result.events)
    assert [event.at.sequence for event in result.events] == [0, 1, 2, 3, 4]
    assert book.snapshot.entitlements[0].accrued
    assert next(c for c in book.snapshot.claims if c.claim_id == "dividend:div-1").state == "confirmed"
    assert result.valuation.nav == D("10908.00")
    assert result.reconciliation.passed


def test_void_and_correction_orchestrate_opening_balance_receivables():
    from nse_pipeline.backtest.accounting_types import (
        Claim, ClaimKind, EventKind, EventTime, Phase, Position,
    )
    from nse_pipeline.backtest.observed_session import (
        DividendCorrectionInput, DividendVoidInput, SessionInput,
    )

    claims = (
        Claim("dividend:void-me", ClaimKind.DIVIDEND, "50", "opening dividend fixture",
              None, "held", basis="opening_balance"),
        Claim("dividend:fix-me", ClaimKind.DIVIDEND, "100", "opening dividend fixture",
              None, "held", basis="opening_balance"),
    )
    book = runner(cash="1000", positions=(Position("held", "AAA", 5, date(2024, 5, 31)),),
                  claims=claims)
    result = book.step(SessionInput(
        DAY1, source(Phase.PRE_OPEN_EVIDENCE),
        dividend_voids=(DividendVoidInput(
            "void-me", evidence("AAA", day=DAY1), "issuer confirmed no distribution",
            source(Phase.PRE_OPEN_ACTIONS)),),
        dividend_corrections=(DividendCorrectionInput(
            "fix-me", "125", evidence("AAA", day=DAY1),
            "issuer revised the record-date amount",
            source(Phase.PRE_OPEN_ACTIONS, character="b")),),
        close_bars=(close_bar("AAA", "10"),),
    ))

    assert [event.kind for event in result.events] == [
        EventKind.DIVIDEND_VOIDED, EventKind.DIVIDEND_CORRECTED]
    assert tuple(event.at for event in result.events) == (
        EventTime(DAY1, Phase.PRE_OPEN_ACTIONS, 0),
        EventTime(DAY1, Phase.PRE_OPEN_ACTIONS, 1),
    )
    assert result.events[0].amount == D("50.00")
    assert result.events[1].amount == D("25.00")  # 125.00 corrected - 100.00 opening
    remaining = tuple(book.snapshot.claims)
    assert len(remaining) == 1
    assert (remaining[0].claim_id, remaining[0].amount) == ("dividend:fix-me", D("125.00"))
    assert book.snapshot.balances.cash == D("1000.00")
    assert book.snapshot.balances.dividend_receivable == D("125.00")
    assert result.valuation.nav == D("1175.00")  # 50 mark + 1000 cash + 125 receivable
    assert result.reconciliation.passed


def test_limitations_disclose_phase30_scope_without_the_old_unsupported_claim():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput

    result = runner(cash="1000").step(SessionInput(DAY1, source(Phase.PRE_OPEN_EVIDENCE)))
    joined = " ".join(result.limitations)
    assert "Phase 30 corporate actions and identity migrations are unsupported" not in joined
    assert ("Phase-30 share actions and dividends are orchestrated only from caller-supplied "
            "evidence") in joined
    assert ("identity migration and lifecycle admission run only from optional typed "
            "evidence supplied to this runner") in joined
    assert "not authenticate sources" in joined
