"""Production-runner evidence for P0-EXECUTION and P0-PREFIX.

These tests drive the production ``SessionRunner`` (not the test-only Batch 6
composition harness) through real phase 10-80 sessions. They prove:

* sells execute before buys at the opening;
* frozen buy orders fill in rank/key/order-id order;
* perturbing or appending an unrelated later order leaves earlier fill records
  identical;
* the economic and provenance projections of a run are prefix-equal when future
  sessions are appended, and a later append never mutates an earlier immutable
  ``SessionResult``.

All prices, quantities, capacities and sessions are hand-derived fixture inputs.
No strategy, historical claim or performance statistic is produced.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from nse_pipeline.backtest.accounting_types import (
    EventKind, EventTime, OpeningPrint, Phase, Position)
from nse_pipeline.backtest.observed_session import (
    BuyTarget, CapacityInput, CloseBar, InputProvenance, OpeningObservation,
    SellTarget, SessionInput, SessionRunner)
from nse_pipeline.backtest.observations import Bar
from nse_pipeline.specs import CostSpec, ExecutionSpec

D = Decimal
D0 = date(2024, 6, 3)
D1 = date(2024, 6, 4)
D2 = date(2024, 6, 5)
D3 = date(2024, 6, 6)
ZERO_COSTS = CostSpec(
    brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0, stt_bps_sell=0,
    stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)


def execution():
    return ExecutionSpec(trade_settlement_lag_sessions=1,
                         adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)


def source(on, phase=Phase.PRE_OPEN_EVIDENCE, citation="supplied fixture"):
    return InputProvenance(citation, "a" * 64, EventTime(on, phase),
                           "supplied fixture; not authenticated")


def bar(key, on, close="100"):
    price = D(close)
    return CloseBar(Bar(key, key, "EQ", on, price, price, price, price, 1000, True,
                        traded_value=D("100000"), trades=10),
                    source(on, Phase.CLOSE_VALUATION))


def opening(key, on, price="100"):
    return OpeningObservation(OpeningPrint(key, on, D(price)),
                              source(on, Phase.OPEN_FILLS))


def buy(order_id, key, rank, *, quantity=10, budget="1100", capacity="10000",
        on=D0):
    return BuyTarget(order_id, key, quantity, D(budget),
                     CapacityInput(D(capacity), source(on, Phase.CLOSE_ORDERS)),
                     rank, source(on, Phase.CLOSE_ORDERS))


def sell(order_id, position_id, quantity, rank, *, on=D0):
    return SellTarget(order_id, position_id, quantity, rank,
                      source(on, Phase.CLOSE_ORDERS))


def runner(*, positions=(), cash="10000"):
    return SessionRunner(cash, on=D0, initial_provenance=source(D0),
                         opening_positions=positions, max_positions=10,
                         costs=ZERO_COSTS, execution=execution())


def fills(result, order_ids):
    return tuple(event for event in result.events
                 if event.kind == EventKind.BUY and event.order_id in order_ids)


def test_production_runner_executes_sells_before_buys_and_in_frozen_rank_key_order():
    held = Position("pos", "AAA", 5, date(2024, 5, 31), 5)
    book = runner(positions=(held,))
    book.step(SessionInput(
        D0, source(D0),
        close_bars=(bar("AAA", D0), bar("BBB", D0)),
        buy_targets=(
            buy("z-order", "BBB", 1),
            buy("a-order", "BBB", 1),
            buy("first-rank", "BBB", 0),
        ),
        sell_targets=(sell("sell-aaa", "pos", 5, 0),),
    ))
    result = book.step(SessionInput(
        D1, source(D1),
        opening_prints=(opening("AAA", D1), opening("BBB", D1)),
        close_bars=(bar("AAA", D1), bar("BBB", D1)),
        buy_targets=(),
        sell_targets=(),
    ))

    opening_events = tuple(event for event in result.events
                           if event.kind in (EventKind.SELL, EventKind.BUY))
    assert tuple(event.kind for event in opening_events) == (
        EventKind.SELL, EventKind.BUY, EventKind.BUY, EventKind.BUY)
    assert tuple(event.order_id for event in opening_events[1:]) == (
        "first-rank", "a-order", "z-order")
    assert tuple(event.at for event in opening_events) == tuple(
        EventTime(D1, Phase.OPEN_FILLS, index) for index in range(4))
    assert result.reconciliation.passed is True


def _two_order_run(third_key):
    book = runner()
    book.step(SessionInput(
        D0, source(D0),
        close_bars=tuple(bar(key, D0) for key in ("AAA", "BBB", third_key)),
        buy_targets=(
            buy("o0", "AAA", 0),
            buy("o1", "BBB", 1),
            buy("o2", third_key, 2),
        ),
    ))
    result = book.step(SessionInput(
        D1, source(D1),
        opening_prints=tuple(opening(key, D1) for key in ("AAA", "BBB", third_key)),
        close_bars=tuple(bar(key, D1) for key in ("AAA", "BBB", third_key)),
        buy_targets=(),
        sell_targets=(),
    ))
    return fills(result, {"o0", "o1"})


def test_irrelevant_later_rank_perturbation_leaves_earlier_fills_byte_identical():
    base = _two_order_run("CCC")
    perturbed = _two_order_run("DDD")
    assert tuple(event.order_id for event in base) == ("o0", "o1")
    # The later rank's security changed, so only its own fill may differ.
    assert base == perturbed


def test_adding_an_unrelated_order_does_not_change_earlier_decisions():
    base = _two_order_run("CCC")
    book = runner()
    book.step(SessionInput(
        D0, source(D0),
        close_bars=tuple(bar(key, D0) for key in ("AAA", "BBB", "CCC", "EEE")),
        buy_targets=(
            buy("o0", "AAA", 0),
            buy("o1", "BBB", 1),
            buy("o2", "CCC", 2),
            buy("o3", "EEE", 3),
        ),
    ))
    result = book.step(SessionInput(
        D1, source(D1),
        opening_prints=tuple(opening(key, D1) for key in ("AAA", "BBB", "CCC", "EEE")),
        close_bars=tuple(bar(key, D1) for key in ("AAA", "BBB", "CCC", "EEE")),
        buy_targets=(),
        sell_targets=(),
    ))
    assert fills(result, {"o0", "o1"}) == base
    assert tuple(event.order_id for event in result.events
                 if event.kind == EventKind.BUY) == ("o0", "o1", "o2", "o3")


def _three_session_inputs():
    return (
        SessionInput(
            D0, source(D0),
            close_bars=(bar("AAA", D0), bar("BBB", D0)),
            buy_targets=(buy("o0", "AAA", 0), buy("o1", "BBB", 1)),
        ),
        SessionInput(
            D1, source(D1),
            opening_prints=(opening("AAA", D1), opening("BBB", D1)),
            close_bars=(bar("AAA", D1), bar("BBB", D1)),
            buy_targets=(buy("o2", "AAA", 0, on=D1),),
        ),
        SessionInput(
            D2, source(D2),
            opening_prints=(opening("AAA", D2),),
            close_bars=(bar("AAA", D2), bar("BBB", D2)),
            buy_targets=(),
        ),
        SessionInput(
            D3, source(D3),
            close_bars=(bar("AAA", D3), bar("BBB", D3)),
            buy_targets=(),
        ),
    )


def economic_projection(result):
    """Everything that describes past economics, excluding consumed provenance."""
    return (result.on, result.phase_states, result.events, result.valuation,
            result.next_orders, result.next_sells, result.reconciliation,
            result.expired_orders)


def provenance_projection(result):
    return result.consumed_provenance


def test_economic_and_provenance_projections_are_prefix_equal():
    inputs = _three_session_inputs()
    prefix = runner()
    prefix_results = tuple(prefix.step(session) for session in inputs[:2])
    full = runner()
    full_results = tuple(full.step(session) for session in inputs)

    assert tuple(economic_projection(result) for result in full_results[:2]) == tuple(
        economic_projection(result) for result in prefix_results)
    assert tuple(provenance_projection(result) for result in full_results[:2]) == tuple(
        provenance_projection(result) for result in prefix_results)
    assert full_results[:2] == prefix_results
    # Later appends leave the earlier immutable records identical.
    assert full_results[0] == prefix_results[0]
    assert full_results[1] == prefix_results[1]


def test_every_observed_session_cut_is_a_reconciled_immutable_prefix():
    """AT-PREF-01..03: every cut, including the settlement-bearing sessions."""
    inputs = _three_session_inputs()
    full = runner()
    full_results = tuple(full.step(session) for session in inputs)
    for length in range(1, len(inputs) + 1):
        prefix = runner()
        prefix_results = tuple(prefix.step(session) for session in inputs[:length])
        assert prefix_results == full_results[:length]
        assert all(result.reconciliation.passed for result in prefix_results)
        assert prefix.events == full.events[:len(prefix.events)]


def test_run_metadata_is_excluded_from_economics_but_retained_in_provenance():
    """AT-PREF-04: changing the source citation does not change past economics."""
    left = runner()
    right = SessionRunner(
        "10000", on=D0,
        initial_provenance=source(D0, citation="independent second run metadata"),
        max_positions=10, costs=ZERO_COSTS, execution=execution())
    inputs = _three_session_inputs()
    left_results = tuple(left.step(session) for session in inputs)
    right_results = tuple(right.step(session) for session in inputs)
    for before, after in zip(left_results, right_results):
        assert economic_projection(before) == economic_projection(after)
        assert provenance_projection(before) != provenance_projection(after)
        assert before.consumed_provenance[0] == left.provenance
        assert after.consumed_provenance[0] == right.provenance
