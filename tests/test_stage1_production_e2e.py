"""Hand-derived production phase 10-80 accounting proof, not historical readiness.

Initial wealth: cash 1000 + 10 AAA at 100 + 4 BBB at 100 = 2400.
Next session: outside capital +100; BBB splits 2:1 at 50; AAA ex-dividend
price 98 with gross dividend 20; sell five AAA at 98; buy two CCC at 100.
The dividend settles after opening fills; trade claims settle next session.
Final outside withdrawal 10 gives wealth 2490, cash 1400, positions 1090.
This is book arithmetic, not a return calculation. Identity and lifecycle
admission are optional typed inputs to the production runner; neither is
supplied by this fixture, so this proof does not exercise them. Source
authentication remains outside the runner's integration scope.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.accounting_types import (
    ClaimKind, EventKind, EventTime, Evidence, ExternalFlowDirection, Phase, Position)
from nse_pipeline.backtest.dividends import (
    DividendTerms, GROSS_DIVIDEND_NO_WITHHOLDING_MODEL, VerifiedPayment)
from nse_pipeline.backtest.observed_session import (
    DividendAccrualInput, DividendEntitlementInput, ExternalFlowInput,
    SessionInput, SessionRunner, ShareActionInput)
from nse_pipeline.backtest.share_actions import ShareAction
from nse_pipeline.specs import ValuationPolicy
from tests.test_stage1_production_runner_contracts import (
    D0, D1, D2, ZERO_COSTS, bar, buy, execution, opening, sell, source)


def inputs():
    action_source = source(D1, Phase.PRE_OPEN_ACTIONS)
    terms = DividendTerms(
        "aaa-dividend", "2",
        Evidence("synthetic issuer terms", "AAA", D1,
                 EventTime(D1, Phase.PRE_OPEN_ACTIONS), True),
        VerifiedPayment(
            D1, EventTime(D1, Phase.PRE_OPEN_ACTIONS), "synthetic gross payment", "20",
            GROSS_DIVIDEND_NO_WITHHOLDING_MODEL))
    split = ShareAction(
        "bbb-split", "split", 2, 1,
        Evidence("synthetic split terms", "BBB", D1,
                 EventTime(D1, Phase.PRE_OPEN_ACTIONS), True))
    return (
        SessionInput(
            D0, source(D0),
            close_bars=(bar("AAA", D0), bar("BBB", D0), bar("CCC", D0)),
            buy_targets=(buy("buy-ccc", "CCC", 0, quantity=2, budget="200"),),
            sell_targets=(sell("sell-aaa", "aaa-held", 5, 0),)),
        SessionInput(
            D1, source(D1),
            external_flows=(ExternalFlowInput(
                "deposit", "100", ExternalFlowDirection.DEPOSIT, source(D1)),),
            share_actions=(ShareActionInput(split, "bbb-held", action_source),),
            dividend_entitlements=(
                DividendEntitlementInput("aaa-held", terms, action_source),),
            dividend_accruals=(DividendAccrualInput("aaa-dividend", action_source),),
            opening_prints=(opening("AAA", D1, "98"), opening("CCC", D1)),
            close_bars=(bar("AAA", D1, "98"), bar("BBB", D1, "50"), bar("CCC", D1))),
        SessionInput(
            D2, source(D2),
            external_flows=(ExternalFlowInput(
                "withdrawal", "10", ExternalFlowDirection.WITHDRAWAL, source(D2)),),
            close_bars=(bar("AAA", D2, "98"), bar("BBB", D2, "50"), bar("CCC", D2))),
    )


def runner():
    return SessionRunner(
        "1000", on=D0, initial_provenance=source(D0),
        opening_positions=(
            Position("aaa-held", "AAA", 10, date(2024, 5, 31), 10),
            Position("bbb-held", "BBB", 4, date(2024, 5, 31), 4)),
        costs=ZERO_COSTS, execution=execution(),
        valuation_policy=ValuationPolicy(fallback_lag=1, lag_unit="exchange_sessions"))


def test_production_stream_integrates_actions_dividends_flows_and_settlements():
    """AT-E2E-01/03/04: exact phase ordering and independently reconciled books."""
    book = runner()
    results = tuple(book.step(session) for session in inputs())
    for result in results:
        assert tuple(state.at.phase for state in result.phase_states) == tuple(Phase)
        assert tuple(event.at for event in result.events) == tuple(
            sorted(event.at for event in result.events))
        assert result.reconciliation.passed
    middle = results[1]
    phases = {state.at.phase: state.state for state in middle.phase_states}
    assert phases[Phase.PRE_OPEN_EVIDENCE].cash == D("1100")
    assert phases[Phase.OPEN_FILLS].cash == D("1100")
    assert phases[Phase.POST_OPEN_SETTLEMENT].cash == D("1120")
    assert tuple(event.kind for event in middle.events) == (
        EventKind.EXTERNAL_FLOW, EventKind.SPLIT,
        EventKind.DIVIDEND_ENTITLEMENT, EventKind.DIVIDEND_ACCRUAL,
        EventKind.SELL, EventKind.BUY, EventKind.SETTLE_DIVIDEND)
    entitlement = phases[Phase.PRE_OPEN_ACTIONS].entitlements[0]
    assert entitlement.shares == 10  # ex-date seller retains cum-date quantity
    claims = {claim.kind: claim for claim in phases[Phase.OPEN_FILLS].claims}
    assert claims[ClaimKind.SALE].amount == D("490")
    assert claims[ClaimKind.BUY_PAYABLE].amount == D("200")
    assert claims[ClaimKind.DIVIDEND].amount == D("20")
    assert middle.valuation.nav == D("2500")
    assert book.snapshot.cash == D("1400")
    assert book.snapshot.claims == ()
    assert book.snapshot.orders == ()
    held = {position.key: position for position in book.snapshot.positions}
    assert {key: (p.shares, p.delivered_shares) for key, p in held.items()} == {
        "AAA": (5, 5), "BBB": (8, 8), "CCC": (2, 2)}
    assert results[-1].valuation.marked_value_priced == D("1090")
    assert results[-1].valuation.nav == D("2490")
    assert sum(e.kind == EventKind.SETTLE_DIVIDEND for e in book.events) == 1
    repeat = runner()
    assert tuple(repeat.step(session) for session in inputs()) == results


def test_integrated_action_dividend_flow_results_remain_prefix_equal():
    """AT-E2E-05: future append cannot rewrite a phase-30 economic prefix."""
    full = runner()
    sessions = inputs()
    results = tuple(full.step(session) for session in sessions)
    for length in range(1, len(sessions) + 1):
        prefix = runner()
        assert tuple(prefix.step(session) for session in sessions[:length]) == results[:length]
        assert prefix.events == full.events[:len(prefix.events)]


def test_later_phase_failure_rolls_back_an_earlier_external_deposit():
    book = runner()
    sessions = inputs()
    first = book.step(sessions[0])
    before = book.snapshot, book.events, book.result
    # Deposit is processed at phase 10, but the missing opening cannot be fabricated.
    broken = replace(sessions[1], opening_prints=())
    with pytest.raises(ValueError, match="Missing opening print"):
        book.step(broken)
    assert (book.snapshot, book.events, book.result) == before
    assert book.result == (first,)
    assert book.step(sessions[1]).reconciliation.passed
