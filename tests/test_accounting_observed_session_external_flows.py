"""External capital at the production runner's atomic phase-10 boundary."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.accounting_types import (
    Claim, ClaimKind, EventKind, ExternalFlowDirection, Phase)
from nse_pipeline.backtest.invariants import reconcile
from nse_pipeline.backtest.observed_session import (
    ExternalFlowInput, SessionInput, SessionRunner)
from tests.test_stage1_production_runner_contracts import (
    D0, D1, ZERO_COSTS, bar, buy, execution, opening, runner, source)


def flow(flow_id="deposit", amount="100", direction=ExternalFlowDirection.DEPOSIT,
         *, on=D0, phase=Phase.PRE_OPEN_EVIDENCE):
    return ExternalFlowInput(flow_id, amount, direction, source(on, phase))


@pytest.mark.parametrize("changes,error", [
    ({"flow_id": ""}, ValueError), ({"amount": "0"}, ValueError),
    ({"amount": "-1"}, ValueError), ({"amount": 1.1}, TypeError),
    ({"direction": "deposit"}, ValueError), ({"provenance": None}, ValueError),
])
def test_invalid_external_flow_input_rejects_at_construction(changes, error):
    valid = flow()
    with pytest.raises(error):
        replace(valid, **changes)


def test_session_external_flows_are_immutable_typed_and_unique():
    supplied = [flow()]
    session = SessionInput(D0, source(D0), external_flows=supplied)
    supplied.clear()
    assert len(session.external_flows) == 1
    with pytest.raises(ValueError, match="unique"):
        SessionInput(D0, source(D0), external_flows=(flow(), flow()))
    with pytest.raises(ValueError, match="typed"):
        SessionInput(D0, source(D0), external_flows=("untyped",))


def test_deposit_precedes_decisions_and_funds_next_open_without_creating_income():
    book = runner(cash="0")
    supplied = flow(amount="1000")
    first = book.step(SessionInput(
        D0, source(D0), external_flows=(supplied,),
        close_bars=(bar("AAA", D0),),
        buy_targets=(buy("buy", "AAA", 0, quantity=10, budget="1000"),)))
    assert first.events[0].kind == EventKind.EXTERNAL_FLOW
    assert first.phase_states[0].state.cash == D("1000")
    assert supplied.provenance in first.consumed_provenance
    assert first.events[0].citation == supplied.provenance.citation
    assert book.snapshot.claims == ()
    result = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1),),
        close_bars=(bar("AAA", D1),)))
    assert result.reconciliation.passed
    assert book.snapshot.positions[0].shares == 10
    assert result.valuation.nav == D("1000")


@pytest.mark.parametrize("provenance", [
    source(D1), source(D0, Phase.CLOSE_VALUATION),
])
def test_unavailable_flow_provenance_rejects_without_committing_any_prefix(provenance):
    book = runner()
    before = book.snapshot, book.events, book.result
    unavailable = replace(flow(), provenance=provenance)
    with pytest.raises(ValueError, match="unavailable"):
        book.step(SessionInput(D0, source(D0), external_flows=(flow("first"), unavailable)))
    assert (book.snapshot, book.events, book.result) == before


def test_later_flow_failure_rolls_back_earlier_deposit_and_duplicate_id_rejects():
    book = runner(cash="0")
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="reserved or payable cash"):
        book.step(SessionInput(
            D0, source(D0), external_flows=(
                flow(), flow("too-much", "101", ExternalFlowDirection.WITHDRAWAL))))
    assert (book.snapshot, book.events, book.result) == before
    book.step(SessionInput(D0, source(D0), external_flows=(flow(),)))
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="already applied"):
        book.step(SessionInput(D1, source(D1), external_flows=(flow(on=D1),)))
    assert (book.snapshot, book.events, book.result) == before


def unknown_payable_runner():
    return SessionRunner("1000", on=D0, initial_provenance=source(D0),
                         opening_claims=(Claim("unknown-tax", ClaimKind.OTHER_PAYABLE, None,
                                               "unresolved tax demand"),),
                         max_positions=10, costs=ZERO_COSTS, execution=execution())


def test_runner_accepts_kernel_valid_deposit_while_a_payable_is_unknown():
    book = unknown_payable_runner()
    supplied = flow(amount="25")
    result = book.step(SessionInput(D0, source(D0), external_flows=(supplied,)))
    assert result.reconciliation.passed, result.reconciliation.checks
    funded = next(check for check in result.reconciliation.checks
                  if check.name == "funded_cash")
    assert funded.status == "PASS"
    assert "unknown_liability_admission" in funded.detail
    assert book.snapshot.cash == D("1025")
    assert book.snapshot.balances.cash_available is None
    assert result.events[0].flow_source_digest == supplied.provenance.digest
    assert result.events[0].flow_citation_digest

    # The same committed prefix still reconciles, and the digest is bound to the citation.
    assert reconcile(book._book.initial, book.events, book.snapshot,
                     sessions=book._book.observed_sessions).passed
    tampered = replace(result.events[0], citation="substituted bank credit advice")
    replay = reconcile(book._book.initial, (tampered,), book.snapshot,
                       sessions=book._book.observed_sessions)
    assert not replay.passed
    assert replay.checks[0].name == "event_integrity"
    assert "citation/source digest substitution" in replay.checks[0].detail


def test_runner_still_rejects_withdrawal_while_a_payable_is_unknown():
    book = unknown_payable_runner()
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="Unknown liabilities"):
        book.step(SessionInput(
            D0, source(D0),
            external_flows=(flow("draw", "1", ExternalFlowDirection.WITHDRAWAL),)))
    assert (book.snapshot, book.events, book.result) == before


def test_phase10_withdrawal_cannot_spend_a_sale_that_settles_at_phase20():
    from tests.test_stage1_production_e2e import inputs, runner as integrated_runner

    book = integrated_runner()
    sessions = inputs()
    book.step(sessions[0])
    book.step(sessions[1])
    before = book.snapshot, book.events, book.result
    # Cash 1120, purchase payable 200 => phase-10 available 920.
    # The incoming sale 490 will settle only at phase 20.
    withdrawal = replace(sessions[2].external_flows[0], amount=D("1000"))
    with pytest.raises(ValueError, match="reserved or payable cash"):
        book.step(replace(sessions[2], external_flows=(withdrawal,)))
    assert (book.snapshot, book.events, book.result) == before
