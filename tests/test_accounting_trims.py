"""Explicit rupee-target trims: exact floor-share math, errors and pure ordering."""
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

from nse_pipeline.backtest.accounting_types import BookState, EventTime, Phase, Position
from nse_pipeline.backtest.observations import Bar
from nse_pipeline.backtest.trims import (
    AT_OR_BELOW_TARGET_REASON,
    DISABLED_TRIMS_REASON,
    KNOWN_ZERO_CAPACITY_REASON,
    NO_DELIVERED_SHARES_REASON,
    PROPOSED_TRIM_REASON,
    TRIM_LIMITATIONS,
    TrimDecision,
    TrimPlan,
    TrimRequest,
    plan_trims,
)
from nse_pipeline.specs import PortfolioSpec

GOLDEN = json.loads((Path(__file__).parent / "golden/trim_cases.json")
                    .read_text(encoding="utf-8"))
DAY = date(2024, 6, 3)
PRIOR = date(2024, 5, 31)


def bar(key, close, *, session=DAY, valid=True, series="EQ"):
    """A close-only observation; ``None`` means the caller has no price."""
    price = None if close is None else D(close)
    return Bar(key, key, series, session, None, None, None, price, 0, valid)


def held(position_id="p1", key="AAA", shares=100, delivered=None, blocked=""):
    return Position(position_id, key, shares, PRIOR, delivered, blocked)


def book(*positions, cash="0", on=DAY):
    return BookState(D(cash), positions=tuple(positions),
                     as_of=EventTime(on, Phase.CLOSE_VALUATION))


def request(order_id="trim-1", position_id="p1", target="1000", rank=0):
    return TrimRequest(order_id, position_id, D(target), rank)


def plan(state, requests, *, bars=None, capacities=None, portfolio=None, on=DAY):
    return plan_trims(
        state, on, requests,
        bars=(bar("AAA", "30"),) if bars is None else bars,
        capacities={"AAA": D("300000")} if capacities is None else capacities,
        portfolio=PortfolioSpec() if portfolio is None else portfolio,
    )


def test_explicit_rupee_target_maps_to_floor_shares_and_integer_reduction():
    state = book(held(), cash="123.45")
    original = state
    decision = plan(state, [request()]).decisions[0]

    # floor(1000 / 30) = 33 target shares; 100 owned - 33 = 67 proposed reduction.
    assert decision.target_shares == 33
    assert decision.requested_quantity == 67
    assert decision.reason == PROPOSED_TRIM_REASON
    assert decision.is_trim
    assert decision.decision_close == D("30")
    assert decision.notional_at_decision_close == D("2010.00")
    assert decision.capacity_notional == D("300000.00")
    assert not decision.capacity_below_request
    assert plan(state, [request()]).total_requested_quantity == 67
    assert state == original and state.cash == D("123.45")


@pytest.mark.parametrize("supplied,expected", [
    ("99.996", D("99.99")), ("0.009", D("0.00")),
])
def test_supplied_hard_capacity_never_rounds_up(supplied, expected):
    decision = plan(book(held()), [request()], capacities={"AAA": supplied}).decisions[0]
    assert decision.capacity_notional == expected
    assert decision.capacity_notional <= D(supplied)
    if expected == 0:
        assert decision.reason == KNOWN_ZERO_CAPACITY_REASON


def test_target_above_or_equal_to_current_value_is_an_intentional_no_op():
    for target in ("3000", "4000.00"):
        state = book(held(shares=100))
        result = plan(state, [request(target=target)], capacities={})
        decision = result.decisions[0]
        assert decision.requested_quantity == 0
        assert decision.reason == AT_OR_BELOW_TARGET_REASON
        assert decision.capacity_notional is None
        assert not decision.is_trim
        assert not result.is_empty  # the caller's target is still recorded
        assert state.cash == D("0")


def test_delivered_shares_cap_the_proposed_reduction():
    state = book(held(shares=100, delivered=40))
    decision = plan(state, [request()]).decisions[0]

    assert decision.target_shares == 33
    assert decision.requested_quantity == 40
    assert decision.reason == PROPOSED_TRIM_REASON


def test_zero_target_is_an_explicit_full_reduction_when_evidence_is_known():
    state = book(held())
    decision = plan(state, [request(target="0")]).decisions[0]

    assert decision.target_shares == 0
    assert decision.requested_quantity == 100
    assert decision.reason == PROPOSED_TRIM_REASON
    assert decision.notional_at_decision_close == D("3000.00")


def test_zero_delivered_shares_is_an_intentional_no_op_not_missing_evidence():
    state = book(held(shares=100, delivered=0))
    decision = plan(state, [request(target="0")], capacities={}).decisions[0]

    assert decision.target_shares == 0
    assert decision.requested_quantity == 0
    assert decision.reason == NO_DELIVERED_SHARES_REASON
    assert decision.capacity_notional is None


@pytest.mark.parametrize("target,close,expected_shares,expected_quantity,expected_notional", [
    ("1000", "30.50", 32, 68, "2074.00"),
    ("0.30", "0.10", 3, 97, "9.70"),
    ("0.29", "0.07", 4, 96, "6.72"),
])
def test_fractional_prices_use_exact_floor_arithmetic(
        target, close, expected_shares, expected_quantity, expected_notional):
    state = book(held())
    decision = plan(
        state, [request(target=target)], bars=(bar("AAA", close),),
        capacities={"AAA": D("100000")}).decisions[0]

    assert decision.target_shares == expected_shares
    assert decision.requested_quantity == expected_quantity
    assert decision.notional_at_decision_close == D(expected_notional)


def test_decisions_are_ordered_by_rank_then_key_then_order_id():
    state = book(
        held("p-c", "CCC"), held("p-a", "AAA"), held("p-b", "BBB"),
        held("p-d", "DDD"))
    requests = [
        request("z", "p-c", rank=0),
        request("y", "p-a", rank=0),
        request("x", "p-b", rank=0),
        request("a", "p-d", rank=1),
    ]
    bars = tuple(bar(key, "30") for key in ("AAA", "BBB", "CCC", "DDD"))
    capacities = {key: D("300000") for key in ("AAA", "BBB", "CCC", "DDD")}

    result = plan(state, requests, bars=bars, capacities=capacities)

    assert result.ordering == ("y", "x", "z", "a")
    assert tuple(decision.request.order_id for decision in result.decisions) == (
        "y", "x", "z", "a")
    assert result.decision_for("x").key == "BBB"
    assert result.decision_for_position("p-a").request.order_id == "y"


def test_plan_and_frozen_records_never_mutate_their_inputs():
    state = book(held())
    requests = [request()]
    bars = (bar("AAA", "30"),)
    capacities = {"AAA": D("300000")}
    result = plan(state, requests, bars=bars, capacities=capacities)

    assert isinstance(result.decisions, tuple)
    assert requests == [request()]
    assert bars == (bar("AAA", "30"),)
    assert capacities == {"AAA": D("300000")}
    assert state == book(held())

    with pytest.raises(FrozenInstanceError):
        result.on = PRIOR
    with pytest.raises(FrozenInstanceError):
        result.decisions[0].requested_quantity = 1
    with pytest.raises(FrozenInstanceError):
        requests[0].target_holding_value = D("0")
    assert result.limitations == TRIM_LIMITATIONS


def test_no_requests_returns_an_empty_plan_even_when_trims_are_disabled():
    state = book(held())
    disabled = PortfolioSpec(trim_overweight_targets=False)

    result = plan(state, [], bars=(), capacities={}, portfolio=disabled)

    assert result.is_empty and result.decisions == ()
    assert result.ordering == () and result.total_requested_quantity == 0
    assert result.on == DAY


def test_snapshot_session_must_match_the_requested_trim_session():
    state = book(held(), on=PRIOR)
    with pytest.raises(ValueError, match="session does not match"):
        plan(state, [request()])
    with pytest.raises(ValueError, match="session does not match"):
        plan_trims(BookState(D("0"), positions=(held(),), as_of=None), DAY,
                   [request()], bars=(bar("AAA", "30"),),
                   capacities={"AAA": D("3000")}, portfolio=PortfolioSpec())
    with pytest.raises(ValueError, match="session date"):
        plan(state, [request()], on="2024-06-03")
    assert state == book(held(), on=PRIOR)


def test_duplicate_order_and_position_identifiers_are_rejected():
    state = book(held("p1", "AAA"), held("p2", "BBB"))
    bars = (bar("AAA", "30"), bar("BBB", "30"))
    capacities = {"AAA": D("3000"), "BBB": D("3000")}

    with pytest.raises(ValueError, match="order_id must be unique"):
        plan(state, [request("same", "p1"), request("same", "p2")],
             bars=bars, capacities=capacities)
    with pytest.raises(ValueError, match="one trim target is allowed per position"):
        plan(state, [request("one", "p1"), request("two", "p1")],
             bars=bars, capacities=capacities)


def test_cross_typed_and_invalid_inputs_are_rejected():
    state = book(held())
    valid_bar = (bar("AAA", "30"),)
    capacities = {"AAA": D("3000")}

    bad_calls = (
        lambda: plan_trims({}, DAY, [request()], bars=valid_bar,
                           capacities=capacities, portfolio=PortfolioSpec()),
        lambda: plan(state, [request()], bars=valid_bar, capacities=capacities,
                     portfolio={}),
        lambda: plan_trims(state, "2024-06-03", [request()], bars=valid_bar,
                           capacities=capacities, portfolio=PortfolioSpec()),
        lambda: plan_trims(state, DAY, "not-requests", bars=valid_bar,
                           capacities=capacities, portfolio=PortfolioSpec()),
        lambda: plan_trims(state, DAY, [object()], bars=valid_bar,
                           capacities=capacities, portfolio=PortfolioSpec()),
        lambda: plan(state, [request()], bars="not-bars", capacities=capacities),
        lambda: plan(state, [request()], bars=(object(),), capacities=capacities),
        lambda: plan(state, [request()], bars=valid_bar, capacities=[("AAA", D("1"))]),
        lambda: plan(state, [request()], bars=valid_bar, capacities={"AAA": 3000.0}),
        lambda: plan(state, [request()], bars=valid_bar, capacities={"AAA": D("-1")}),
    )
    for call in bad_calls:
        with pytest.raises(ValueError):
            call()

    for bad_request in (
        lambda: TrimRequest("", "p1", D("1000")),
        lambda: TrimRequest("o", "", D("1000")),
        lambda: TrimRequest("o", "p1", 1000.0),
        lambda: TrimRequest("o", "p1", D("-1")),
        lambda: TrimRequest("o", "p1", D("1000"), rank="0"),
        lambda: TrimRequest("o", "p1", D("1000"), rank=True),
        lambda: TrimRequest("o", "p1", D("1000"), rank=-1),
    ):
        with pytest.raises(ValueError):
            bad_request()


@pytest.mark.parametrize("bars", [
    (),
    (bar("AAA", "30", valid=False),),
    (bar("AAA", "0"),),
    (bar("AAA", "30", session=PRIOR),),
    (bar("AAA", "30"), bar("AAA", "30")),
])
def test_missing_invalid_zero_stale_or_duplicate_close_raises(bars):
    state = book(held())
    with pytest.raises(ValueError):
        plan(state, [request()], bars=bars)
    assert state == book(held())


@pytest.mark.parametrize("capacities", [{}, {"AAA": None}])
def test_missing_or_unknown_capacity_for_a_positive_trim_raises(capacities):
    state = book(held())
    with pytest.raises(ValueError, match="known nonnegative capacity"):
        plan(state, [request()], capacities=capacities)
    assert state == book(held())


def test_known_zero_capacity_preserves_the_request_and_freezes_a_zero_cap():
    state = book(held(), cash="50")
    decision = plan(state, [request()], capacities={"AAA": D("0")}).decisions[0]

    # Known zero is explicit no-liquidity, not unknown evidence; the intent stands.
    assert decision.requested_quantity == 67
    assert decision.capacity_notional == D("0.00")
    assert decision.reason == KNOWN_ZERO_CAPACITY_REASON
    assert decision.is_trim and decision.capacity_below_request
    assert state.cash == D("50")


def test_capacity_does_not_shrink_the_requested_quantity():
    decision = plan(book(held()), [request()],
                    capacities={"AAA": D("100")}).decisions[0]

    assert decision.requested_quantity == 67
    assert decision.notional_at_decision_close == D("2010.00")
    assert decision.capacity_below_request
    assert decision.reason == PROPOSED_TRIM_REASON


def test_unknown_inactive_and_blocked_positions_are_rejected():
    with pytest.raises(ValueError, match="unknown position"):
        plan(book(), [request(position_id="missing")])
    with pytest.raises(ValueError, match="existing active position"):
        plan(book(held(shares=0)), [request()])
    with pytest.raises(ValueError, match="blocked/unresolved"):
        plan(book(held(blocked="unresolved corporate action")), [request()])


def test_disabled_trims_reject_explicit_requests():
    state = book(held())
    disabled = PortfolioSpec(trim_overweight_targets=False)

    with pytest.raises(ValueError, match=DISABLED_TRIMS_REASON):
        plan(state, [request()], portfolio=disabled)
    assert state == book(held())


def test_inconsistent_decision_and_plan_records_are_rejected():
    trim = request()
    with pytest.raises(ValueError, match="known nonnegative capacity"):
        TrimDecision(trim, "AAA", D("30"), 33, 67, None, PROPOSED_TRIM_REASON)

    decision = TrimDecision(trim, "AAA", D("30"), 33, 67, D("3000"),
                            PROPOSED_TRIM_REASON)
    with pytest.raises(ValueError, match="floor"):
        TrimPlan(DAY, (replace(decision, target_shares=34),))
    with pytest.raises(ValueError, match="unique"):
        TrimPlan(DAY, (decision, decision))


def test_hand_derived_golden_cases():
    for case in GOLDEN["cases"]:
        state = book(held(shares=case["shares"], delivered=case["delivered"]))
        capacities = {} if case["capacity"] is None else {"AAA": D(case["capacity"])}
        decision = plan(
            state, [request(target=case["target"])],
            bars=(bar("AAA", case["close"]),), capacities=capacities).decisions[0]

        assert decision.target_shares == case["target_shares"], case["name"]
        assert decision.requested_quantity == case["quantity"], case["name"]
        assert decision.reason == case["reason"], case["name"]
