"""Observed-session holding-value trims and frozen per-order sell capacity.

Every target is an explicit caller-supplied rupee value; no weight, NAV
denominator or strategy is inferred. A missing close, capacity or position is an
error, never a silent skip. The fractional-share rounding difference is never
credited as cash: it appears only in the actual sale proceeds on settlement.
"""
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal as D

import pytest

from nse_pipeline.backtest.accounting_types import (
    EventKind, EventTime, ExternalFlowDirection, OpeningCondition, OpeningPrint,
    Phase, Position)
from nse_pipeline.backtest.invariants import reconcile
from nse_pipeline.backtest.observed_session import (
    BuyTarget, CapacityInput, CloseBar, ExternalFlowInput, HaltEvidence,
    InputProvenance, OpeningObservation, SellCapacityInput, SellTarget,
    SessionInput, SessionRunner, TrimTarget)
from nse_pipeline.backtest.observations import Bar
from nse_pipeline.backtest.trims import (
    AT_OR_BELOW_TARGET_REASON, KNOWN_ZERO_CAPACITY_REASON, TrimRequest)
from nse_pipeline.specs import CostSpec, ExecutionSpec, PortfolioSpec

D0 = date(2024, 6, 3)
D1 = date(2024, 6, 4)
D2 = date(2024, 6, 5)
ZERO = D("0.00")

ZERO_COSTS = CostSpec(
    brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0, stt_bps_sell=0,
    stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)


def execution(*, buy_slip=0, sell_slip=0, partial_fill_policy="partial",
              max_order_notional=None):
    return ExecutionSpec(
        trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=buy_slip,
        adverse_slippage_bps_sell=sell_slip, partial_fill_policy=partial_fill_policy,
        max_order_notional=max_order_notional)


def source(on, phase=Phase.PRE_OPEN_EVIDENCE, character="a"):
    return InputProvenance("supplied fixture", character * 64, EventTime(on, phase),
                           "supplied fixture; not authenticated")


def bar(key, on, close="100"):
    price = D(close)
    return CloseBar(Bar(key, key, "EQ", on, price, price, price, price, 1000, True),
                    source(on, Phase.CLOSE_VALUATION))


def opening(key, on, price="100"):
    return OpeningObservation(OpeningPrint(key, on, D(price)),
                              source(on, Phase.OPEN_FILLS))


def buy(order_id, key, rank, *, quantity=10, budget="1000", capacity="10000", on=D0):
    return BuyTarget(order_id, key, quantity, D(budget),
                     CapacityInput(D(capacity), source(on, Phase.CLOSE_ORDERS)),
                     rank, source(on, Phase.CLOSE_ORDERS))


def sell(order_id, position_id, quantity, rank=0, *, on=D0):
    return SellTarget(order_id, position_id, quantity, rank,
                      source(on, Phase.CLOSE_ORDERS))


def trim(order_id, position_id, target_value, *, rank=0, capacity="1000000",
         condition=None, on=D0):
    return TrimTarget(
        TrimRequest(order_id, position_id, D(target_value), rank),
        SellCapacityInput(D(capacity), source(on, Phase.CLOSE_ORDERS)),
        source(on, Phase.CLOSE_ORDERS), condition)


def runner(*, cash="0", positions=(), max_positions=10, trim_flag=True,
           costs=None, execution_spec=None, portfolio_spec="auto"):
    if portfolio_spec == "auto":
        portfolio_spec = PortfolioSpec(max_positions=max_positions,
                                       trim_overweight_targets=trim_flag)
    return SessionRunner(
        cash, on=D0, initial_provenance=source(D0), opening_positions=positions,
        max_positions=max_positions, costs=costs or ZERO_COSTS,
        execution=execution_spec or execution(), portfolio_spec=portfolio_spec)


def held(position_id="held", key="AAA", shares=100):
    return Position(position_id, key, shares, date(2024, 5, 31), shares)


def trade_events(result):
    return tuple(event for event in result.events
                 if event.kind in (EventKind.SELL, EventKind.SELL_SKIPPED))


def test_1000_target_at_30_keeps_33_sells_67_and_credits_only_the_settled_2010():
    book = runner(positions=(held(),))
    first = book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000"),)))

    decision = first.trim_decisions[0]
    assert decision.target_shares == 33
    assert decision.requested_quantity == 67
    assert decision.decision_close == D("30")
    assert first.next_sells[0].target.requested_quantity == 67
    assert first.next_orders == ()

    filled = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))

    sale = trade_events(filled)[0]
    assert sale.kind == EventKind.SELL
    assert (sale.quantity, sale.requested_quantity) == (67, 67)
    assert sale.amount == D("2010")
    # The rounding remainder is never a cash posting or a second credit.
    assert {posting.account for posting in sale.postings} == {
        "shares", "delivered", "sale_receivable"}
    assert not any(posting.account == "cash" for posting in sale.postings)
    assert book.snapshot.positions[0].shares == 33
    assert filled.valuation.marked_value_priced == D("990")
    assert book.snapshot.balances.sale_receivable == D("2010")
    assert book.snapshot.cash == ZERO
    assert book.snapshot.balances.cash_available == ZERO
    assert not any(event.kind == EventKind.EXTERNAL_FLOW for event in book.events)

    settled = book.step(SessionInput(D2, source(D2), close_bars=(bar("AAA", D2, "30"),)))
    assert book.snapshot.cash == D("2010")
    assert book.snapshot.balances.sale_receivable == ZERO
    assert settled.valuation.nav == D("3000")
    assert all(result.reconciliation.passed for result in (first, filled, settled))


def test_capacity_limits_the_fill_while_requested_quantity_stays_frozen():
    book = runner(positions=(held(),))
    first = book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000", capacity="1000"),)))
    assert first.trim_decisions[0].requested_quantity == 67

    filled = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))
    sale = trade_events(filled)[0]
    # 1000 / 30 -> at most 33 shares under the frozen cap, never rounded up.
    assert (sale.kind, sale.quantity, sale.requested_quantity) == (EventKind.SELL, 33, 67)
    assert sale.amount == D("990")
    assert sale.sell_capacity_notional == D("1000.00")
    assert book.snapshot.positions[0].shares == 67
    assert filled.reconciliation.passed


def test_known_zero_capacity_is_explicit_no_execution_not_unknown_or_unbounded():
    book = runner(positions=(held(),))
    first = book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000", capacity="0"),)))

    decision = first.trim_decisions[0]
    assert decision.capacity_notional == ZERO
    assert decision.requested_quantity == 67
    assert decision.reason == KNOWN_ZERO_CAPACITY_REASON
    assert first.next_sells[0].target.capacity.principal_notional == ZERO

    filled = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))
    skipped = trade_events(filled)[0]
    assert skipped.kind == EventKind.SELL_SKIPPED
    assert (skipped.quantity, skipped.amount, skipped.claim_id) == (0, ZERO, "")
    assert skipped.sell_capacity_notional == ZERO
    assert book.snapshot.positions[0].shares == 100
    assert book.snapshot.claims == ()
    assert filled.reconciliation.passed


def test_trim_capacity_is_floored_to_paise_and_never_rounded_up():
    assert SellCapacityInput("99.996", source(D0, Phase.CLOSE_ORDERS)).principal_notional == D("99.99")
    assert SellCapacityInput("0.009", source(D0, Phase.CLOSE_ORDERS)).principal_notional == ZERO
    with pytest.raises(ValueError):
        SellCapacityInput("-1", source(D0, Phase.CLOSE_ORDERS))
    with pytest.raises((TypeError, ValueError)):
        SellCapacityInput(1.0, source(D0, Phase.CLOSE_ORDERS))


def test_fees_and_slippage_reduce_proceeds_without_creating_cash():
    costs = replace(ZERO_COSTS, brokerage_bps=100, brokerage_cap_per_order=1000,
                    stt_bps_sell=100, exchange_bps=100, gst_on_brokerage_pct=18)
    book = runner(positions=(held(),), costs=costs,
                  execution_spec=execution(sell_slip=1000))
    book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000"),)))
    filled = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))
    sale = trade_events(filled)[0]
    assert sale.price == D("27.0")  # 10% adverse sell slippage.
    assert sale.amount == sale.quantity * sale.price - sale.fee
    assert sale.amount < D("1809")
    assert book.snapshot.cash == ZERO and book.snapshot.balances.sale_receivable == sale.amount
    assert filled.reconciliation.passed


def test_no_op_trim_is_persisted_as_an_explicit_decision_not_omitted():
    book = runner(positions=(held(),))
    result = book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "100000"),)))
    decision = result.trim_decisions[0]
    assert decision.requested_quantity == 0
    assert decision.reason == AT_OR_BELOW_TARGET_REASON
    assert result.next_sells == ()
    assert book.snapshot.positions[0].shares == 100
    assert result.reconciliation.passed


def test_missing_close_rolls_back_phase10_flows_and_leaves_no_result():
    book = runner(cash="0", positions=(held(),))
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="raw same-session close"):
        book.step(SessionInput(
            D0, source(D0),
            external_flows=(ExternalFlowInput(
                "seed", D("500"), ExternalFlowDirection.DEPOSIT, source(D0)),),
            close_bars=(bar("BBB", D0, "30"),),
            trim_targets=(trim("trim-held", "held", "1000"),)))
    assert (book.snapshot, book.events, book.result) == before
    assert book.snapshot.cash == ZERO and book.snapshot.claims == ()


def test_missing_or_unresolved_trim_inputs_raise_and_preserve_the_book():
    book = runner(positions=(held(),))
    before = book.snapshot, book.events, book.result

    with pytest.raises(ValueError):
        TrimTarget(TrimRequest("t", "held", D("1000")), None,
                   source(D0, Phase.CLOSE_ORDERS))
    assert (book.snapshot, book.events, book.result) == before

    with pytest.raises(ValueError, match="no open position"):
        book.step(SessionInput(
            D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
            trim_targets=(trim("trim-missing", "absent", "1000"),)))
    assert (book.snapshot, book.events, book.result) == before

    blocked = Position("blocked", "AAA", 10, date(2024, 5, 31), 10,
                       "unverified_action_terms")
    blocked_book = runner(positions=(blocked,))
    blocked_before = blocked_book.snapshot, blocked_book.events, blocked_book.result
    with pytest.raises(ValueError, match="blocked/unresolved"):
        blocked_book.step(SessionInput(
            D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
            trim_targets=(trim("trim-blocked", "blocked", "1000"),)))
    assert (blocked_book.snapshot, blocked_book.events, blocked_book.result) == blocked_before


def test_trim_requests_require_a_spec_and_the_trim_flag():
    no_spec = runner(positions=(held(),), portfolio_spec=None)
    before = no_spec.snapshot, no_spec.events, no_spec.result
    with pytest.raises(ValueError, match="PortfolioSpec"):
        no_spec.step(SessionInput(
            D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
            trim_targets=(trim("trim-held", "held", "1000"),)))
    assert (no_spec.snapshot, no_spec.events, no_spec.result) == before

    disabled = runner(positions=(held(),), trim_flag=False)
    disabled_before = disabled.snapshot, disabled.events, disabled.result
    with pytest.raises(ValueError, match="trim_overweight_targets"):
        disabled.step(SessionInput(
            D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
            trim_targets=(trim("trim-held", "held", "1000"),)))
    assert (disabled.snapshot, disabled.events, disabled.result) == disabled_before

    with pytest.raises(ValueError, match="max_positions"):
        runner(positions=(held(),), max_positions=10,
               portfolio_spec=PortfolioSpec(max_positions=1))


def test_same_position_or_same_security_contradictions_are_rejected_but_sell_a_buy_b_is_allowed():
    book = runner(positions=(held(),), cash="10000")
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="same position"):
        book.step(SessionInput(
            D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
            trim_targets=(trim("trim-held", "held", "1000"),),
            sell_targets=(sell("explicit-sell", "held", 10),)))
    assert (book.snapshot, book.events, book.result) == before

    with pytest.raises(ValueError, match="same security"):
        book.step(SessionInput(
            D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
            trim_targets=(trim("trim-held", "held", "1000"),),
            buy_targets=(buy("buy-aaa", "AAA", 0),)))
    assert (book.snapshot, book.events, book.result) == before

    result = book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"), bar("BBB", D0, "100")),
        trim_targets=(trim("trim-held", "held", "1000"),),
        buy_targets=(buy("buy-bbb", "BBB", 0),)))
    assert result.trim_decisions[0].requested_quantity == 67
    assert tuple(order.order_id for order in result.next_orders) == ("buy-bbb",)


def test_sale_proceeds_cannot_fund_a_same_open_purchase():
    book = runner(cash="0", positions=(held(),))
    with pytest.raises(ValueError, match="unaffordable at decision close"):
        book.step(SessionInput(
            D0, source(D0), close_bars=(bar("AAA", D0, "30"), bar("BBB", D0, "100")),
            trim_targets=(trim("trim-held", "held", "0"),),
            buy_targets=(buy("buy-bbb", "BBB", 0, quantity=5, budget="500"),)))
    assert book.events == () and book.snapshot.claims == ()


def test_frozen_capacity_and_condition_cannot_be_replaced_by_a_later_input():
    condition = OpeningCondition(min_price="20")
    book = runner(positions=(held(),))
    first = book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000", capacity="1000",
                           condition=condition),)))
    frozen = first.next_sells[0].target
    assert frozen.capacity.principal_notional == D("1000.00")
    assert frozen.condition == condition
    with pytest.raises(FrozenInstanceError):
        frozen.capacity = SellCapacityInput("999999", source(D0, Phase.CLOSE_ORDERS))
    with pytest.raises(FrozenInstanceError):
        frozen.condition = None

    # A later, larger trim request is a new intent; it cannot enlarge the frozen sell.
    filled = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),),
        trim_targets=(trim("trim-later", "held", "0", capacity="999999", on=D1),)))
    sale = trade_events(filled)[0]
    assert (sale.quantity, sale.sell_capacity_notional) == (33, D("1000.00"))
    assert sale.condition == condition and sale.condition_met is True
    assert filled.reconciliation.passed


def test_phase30_share_action_rejects_a_frozen_trim_in_pre_action_units():
    from nse_pipeline.backtest.observed_session import ShareActionInput
    from nse_pipeline.backtest.share_actions import ShareAction
    from nse_pipeline.backtest.accounting_types import Evidence

    evidence = Evidence("split-evidence", "AAA", D1, EventTime(D1, Phase.PRE_OPEN_ACTIONS), True)
    book = runner(positions=(held(),))
    book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000"),)))
    before = book.snapshot, book.events, book.result
    action = ShareAction("split-held", "split", 2, 1, evidence)
    with pytest.raises(ValueError, match="invalidate frozen sell quantity"):
        book.step(SessionInput(
            D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
            close_bars=(bar("AAA", D1, "30"),),
            share_actions=(ShareActionInput(
                action, "held", source(D1, Phase.PRE_OPEN_ACTIONS)),)))
    assert (book.snapshot, book.events, book.result) == before


def test_missing_opening_print_still_requires_halt_and_halt_expires_the_trim():
    book = runner(positions=(held(),))
    book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000"),)))
    before = book.snapshot, book.events, book.result
    with pytest.raises(ValueError, match="Missing opening print for security AAA"):
        book.step(SessionInput(D1, source(D1), close_bars=(bar("AAA", D1, "30"),)))
    assert (book.snapshot, book.events, book.result) == before

    halt = source(D1, Phase.OPEN_FILLS, character="b")
    result = book.step(SessionInput(
        D1, source(D1), halt_evidence=(HaltEvidence("trim-held", halt),),
        close_bars=(bar("AAA", D1, "30"),)))
    assert [record.order_id for record in result.expired_orders] == ["trim-held"]
    assert halt in result.consumed_provenance
    assert not trade_events(result)
    assert book.snapshot.positions[0].shares == 100
    assert result.reconciliation.passed


def test_full_trim_frees_the_position_slot_before_a_preexisting_cash_buy():
    def run(with_trim):
        book = runner(cash="1000", positions=(held(),), max_positions=1)
        inputs = dict(
            close_bars=(bar("AAA", D0, "30"), bar("BBB", D0, "100")),
            buy_targets=(buy("buy-bbb", "BBB", 0, quantity=5, budget="500"),))
        if with_trim:
            inputs["trim_targets"] = (trim("trim-held", "held", "0"),)
        first = book.step(SessionInput(D0, source(D0), **inputs))
        second = book.step(SessionInput(
            D1, source(D1),
            opening_prints=(opening("AAA", D1, "30"), opening("BBB", D1, "100")),
            close_bars=(bar("AAA", D1, "30"), bar("BBB", D1, "100"))))
        return first, second, book

    first, second, book = run(True)
    assert trade_events(second)[0].kind == EventKind.SELL
    filled = next(event for event in second.events if event.kind == EventKind.BUY)
    assert filled.order_id == "buy-bbb" and filled.quantity == 5
    assert {position.key: position.shares for position in book.snapshot.positions} == {
        "AAA": 0, "BBB": 5}
    assert first.reconciliation.passed and second.reconciliation.passed

    _first, control, control_book = run(False)
    skipped = next(event for event in control.events if event.kind == EventKind.BUY_SKIPPED)
    assert (skipped.order_id, skipped.reason, skipped.quantity) == ("buy-bbb", "position_limit", 0)
    assert {position.key: position.shares for position in control_book.snapshot.positions} == {
        "AAA": 100}
    assert control.reconciliation.passed


def test_trim_decisions_and_results_are_deeply_immutable_prefix_records():
    book = runner(positions=(held(),))
    first = book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000"),)))
    snapshot = first.trim_decisions
    assert isinstance(snapshot, tuple)
    with pytest.raises(FrozenInstanceError):
        snapshot[0].requested_quantity = 0

    second = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))
    stored = (first, second)
    book.step(SessionInput(D2, source(D2), close_bars=(bar("AAA", D2, "30"),)))
    assert stored == (first, second)
    assert book.result[:2] == stored
    assert first.trim_decisions == snapshot


def test_replay_rejects_sell_capacity_tampering_and_unrelated_metadata():
    book = runner(positions=(held(),))
    book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000", capacity="1000"),)))
    book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))
    events = list(book.events)
    index = next(i for i, event in enumerate(events) if event.kind == EventKind.SELL)

    def rejected(changed_events, message):
        result = reconcile(book._book.initial, tuple(changed_events), book.snapshot,
                           sessions=book._book.observed_sessions)
        assert not result.passed
        assert result.checks[0].name == "event_integrity"
        assert message in result.checks[0].detail

    changed = list(events)
    changed[index] = replace(changed[index], sell_capacity_notional=ZERO)
    rejected(changed, "capacity")

    changed = list(events)
    changed[index] = replace(changed[index], sell_capacity_notional=D("100"))
    rejected(changed, "capacity")

    # Capacity metadata on an unrelated event, including an external flow, is rejected.
    flow_book = runner(cash="0", positions=(held(),))
    flow_book.step(SessionInput(
        D0, source(D0),
        external_flows=(ExternalFlowInput("seed", D("500"), ExternalFlowDirection.DEPOSIT,
                                         source(D0)),)))
    flow_events = list(flow_book.events)
    flow_index = next(i for i, event in enumerate(flow_events)
                      if event.kind == EventKind.EXTERNAL_FLOW)
    flow_events[flow_index] = replace(flow_events[flow_index],
                                      sell_capacity_notional=ZERO)
    result = reconcile(flow_book._book.initial, tuple(flow_events), flow_book.snapshot,
                       sessions=flow_book._book.observed_sessions)
    assert not result.passed
    assert result.checks[0].name == "event_integrity"


def test_trim_condition_is_evaluated_at_the_raw_open_and_false_skips_without_a_claim():
    book = runner(positions=(held(),))
    condition = OpeningCondition(max_price="29")
    book.step(SessionInput(
        D0, source(D0), close_bars=(bar("AAA", D0, "30"),),
        trim_targets=(trim("trim-held", "held", "1000", condition=condition),)))
    filled = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))

    skipped = trade_events(filled)[0]
    assert skipped.kind == EventKind.SELL_SKIPPED
    assert skipped.condition == condition and skipped.condition_met is False
    assert skipped.reason == "opening_condition_unmet"
    assert (skipped.quantity, skipped.amount, skipped.fee, skipped.claim_id) == (0, ZERO, ZERO, "")
    assert skipped.sell_capacity_notional == D("1000000.00")
    assert book.snapshot.positions[0].shares == 100 and book.snapshot.claims == ()
    assert filled.reconciliation.passed

    events = list(book.events)
    index = next(i for i, event in enumerate(events) if event.kind == EventKind.SELL_SKIPPED)
    events[index] = replace(events[index], condition=OpeningCondition(max_price="31"),
                            condition_met=True)
    result = reconcile(book._book.initial, tuple(events), book.snapshot,
                       sessions=book._book.observed_sessions)
    assert not result.passed and result.checks[0].name == "event_integrity"


def test_legacy_unconditional_sell_without_capacity_is_unchanged():
    book = runner(positions=(held(),))
    book.step(SessionInput(
        D0, source(D0), sell_targets=(sell("legacy-sell", "held", 67),),
        close_bars=(bar("AAA", D0, "30"),)))
    filled = book.step(SessionInput(
        D1, source(D1), opening_prints=(opening("AAA", D1, "30"),),
        close_bars=(bar("AAA", D1, "30"),)))
    sale = trade_events(filled)[0]
    assert sale.kind == EventKind.SELL
    assert sale.quantity == 67 and sale.amount == D("2010")
    assert sale.sell_capacity_notional is None
    assert sale.condition is None and sale.condition_met is None
    assert filled.reconciliation.passed
