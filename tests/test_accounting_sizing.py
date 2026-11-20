"""P1/P2/P3 sizing compared to the hand-derived policy_cases.json and execution_cases.json goldens."""
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden/execution_cases.json"
POLICY_GOLDEN = Path(__file__).parent / "golden/policy_cases.json"


def golden_cases():
    return [case for case in json.loads(GOLDEN.read_text(encoding="utf-8"))["sizing"]
            if "policy" in case]


def policy_golden():
    return json.loads(POLICY_GOLDEN.read_text(encoding="utf-8"))


def policy_cases():
    return policy_golden()["cases"]


def policy_cost_model(name):
    from nse_pipeline.specs import CostSpec
    return CostSpec(**policy_golden()["cost_models"][name])


def policy_execution():
    from nse_pipeline.specs import ExecutionSpec
    return ExecutionSpec(**policy_golden()["execution_model"])


def run_policy_case(case):
    from nse_pipeline.backtest.sizing import size_orders

    screen = {}
    if case.get("screen_values") is not None:
        screen["screen_values"] = case["screen_values"]
    if case.get("screen_prices") is not None:
        screen["screen_prices"] = case["screen_prices"]
    return size_orders(case["policy"], case["cash"], case["desired_in_rank_order"],
                       price=case["price"], slots=case["slots"],
                       costs=policy_cost_model(case["cost_model"]),
                       execution=policy_execution(),
                       unresolved_ranks=case["unresolved_ranks"], **screen)


def zero_costs():
    from nse_pipeline.specs import CostSpec
    return CostSpec(brokerage_bps=0, stt_bps_buy=0, stt_bps_sell=0,
                     stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)


def execution():
    from nse_pipeline.specs import ExecutionSpec
    return ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                         adverse_slippage_bps_sell=0)


def test_p2_golden_case_fills_both_requested_notionals():
    from nse_pipeline.backtest.sizing import size_orders

    case = next(c for c in golden_cases() if c["policy"] == "P2")
    result = size_orders(case["policy"], case["cash"], case["desired_in_rank_order"],
                         price="1", slots=len(case["desired_in_rank_order"]),
                         costs=zero_costs(), execution=execution())
    assert result.filled == tuple(D(value) for value in case["expected_filled"])
    assert result.remaining_cash == D(case["expected_cash"])


def test_p3_golden_case_clips_the_second_purchase_to_settled_cash():
    from nse_pipeline.backtest.sizing import size_orders

    case = next(c for c in golden_cases() if c["policy"] == "P3")
    result = size_orders(case["policy"], case["cash"], case["desired_in_rank_order"],
                         price="1", slots=len(case["desired_in_rank_order"]),
                         costs=zero_costs(), execution=execution())
    assert result.filled == tuple(D(value) for value in case["expected_filled"])
    assert result.remaining_cash == D(case["expected_cash"])


@pytest.mark.parametrize("case", golden_cases(), ids=lambda case: case["policy"])
def test_every_golden_policy_case_is_reproduced_by_the_module(case):
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders(case["policy"], case["cash"], case["desired_in_rank_order"],
                         price="1", slots=len(case["desired_in_rank_order"]),
                         costs=zero_costs(), execution=execution())
    assert result.filled == tuple(D(value) for value in case["expected_filled"])
    assert result.remaining_cash == D(case["expected_cash"])


def test_zero_cash_fills_nothing_and_keeps_the_cash():
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders("P2", "0", ["100", "100"], price="1", slots=2,
                         costs=zero_costs(), execution=execution())
    assert result.filled == (D("0"), D("0"))
    assert result.remaining_cash == D("0")
    assert result.reasons == ("no_cash", "no_cash")


def test_empty_rank_order_leaves_the_cash_untouched():
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders("P2", "1000", [], price="1", slots=0,
                         costs=zero_costs(), execution=execution())
    assert result.filled == ()
    assert result.remaining_cash == D("1000")
    assert result.reasons == ()


def test_cash_exhausted_mid_rank_clips_the_later_rank():
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders("P2", "1000", ["600", "600"], price="1", slots=2,
                         costs=zero_costs(), execution=execution())
    assert result.filled == (D("600"), D("400"))
    assert result.remaining_cash == D("0")
    assert result.reasons == ("filled", "filled")


def test_p1_freezes_the_round_while_a_rank_is_unresolved():
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders("P1", "1000", ["500", "500"], price="1", slots=2,
                         costs=zero_costs(), execution=execution(), unresolved_ranks=(1,))
    assert result.filled == (D("0"), D("0"))
    assert result.remaining_cash == D("1000")
    assert result.reasons == ("frozen_unresolved", "frozen_unresolved")


def test_p2_excludes_unresolved_ranks_from_sizing():
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders("P2", "1000", ["500", "500"], price="1", slots=2,
                         costs=zero_costs(), execution=execution(), unresolved_ranks=(0,))
    assert result.filled == (D("0"), D("500"))
    assert result.remaining_cash == D("500")
    assert result.reasons == ("excluded_unresolved", "filled")


def test_p3_sizes_unresolved_ranks_at_their_screen_value():
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders("P3", "1000", ["550", "450"], price="1", slots=2,
                         costs=zero_costs(), execution=execution(), unresolved_ranks=(0,),
                         screen_values=("550",), screen_prices=("1",))
    assert result.filled == (D("550"), D("450"))
    assert result.remaining_cash == D("0")
    assert result.reasons == ("filled_screen_value", "filled")
    assert result.policy == "P3_size_including_unresolved_at_screen"


def test_slots_limit_the_number_of_new_positions():
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders("P2", "1000", ["400", "400", "400"], price="1", slots=2,
                         costs=zero_costs(), execution=execution())
    assert result.filled == (D("400"), D("400"), D("0"))
    assert result.remaining_cash == D("200")
    assert result.reasons == ("filled", "filled", "no_slot")


def test_fees_are_inclusive_of_the_filled_notional():
    from nse_pipeline.backtest.sizing import size_orders
    from nse_pipeline.specs import CostSpec

    costs = CostSpec(brokerage_bps=10000, brokerage_cap_per_order=1, stt_bps_buy=0,
                     stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)
    result = size_orders("P2", "1000", ["1000"], price="1", slots=1,
                         costs=costs, execution=execution())
    assert result.filled == (D("999"),)
    assert result.fees == D("1")
    assert result.remaining_cash == D("0")


def test_result_is_immutable():
    from dataclasses import FrozenInstanceError
    from nse_pipeline.backtest.sizing import size_orders

    result = size_orders("P2", "1000", ["500"], price="1", slots=1,
                         costs=zero_costs(), execution=execution())
    with pytest.raises(FrozenInstanceError):
        result.remaining_cash = D("1")


@pytest.mark.parametrize("policy", ("P4", "p2", "", 2))
def test_unknown_policy_is_rejected(policy):
    from nse_pipeline.backtest.sizing import size_orders

    with pytest.raises(ValueError):
        size_orders(policy, "1000", ["500"], price="1", slots=1,
                    costs=zero_costs(), execution=execution())


@pytest.mark.parametrize("cash", ("-1", "NaN", "Infinity", 0.1, True))
def test_invalid_cash_is_rejected(cash):
    from nse_pipeline.backtest.sizing import size_orders

    with pytest.raises(ValueError):
        size_orders("P2", cash, ["500"], price="1", slots=1,
                    costs=zero_costs(), execution=execution())


@pytest.mark.parametrize("desired", ("-1", "NaN", 0.1))
def test_invalid_desired_notional_is_rejected(desired):
    from nse_pipeline.backtest.sizing import size_orders

    with pytest.raises(ValueError):
        size_orders("P2", "1000", [desired], price="1", slots=1,
                    costs=zero_costs(), execution=execution())


@pytest.mark.parametrize("price", ("0", "-1", "NaN", 0.1))
def test_invalid_price_is_rejected(price):
    from nse_pipeline.backtest.sizing import size_orders

    with pytest.raises(ValueError):
        size_orders("P2", "1000", ["500"], price=price, slots=1,
                    costs=zero_costs(), execution=execution())


@pytest.mark.parametrize("slots", ("1", -1, True, 1.0))
def test_invalid_slots_are_rejected(slots):
    from nse_pipeline.backtest.sizing import size_orders

    with pytest.raises(ValueError):
        size_orders("P2", "1000", ["500"], price="1", slots=slots,
                    costs=zero_costs(), execution=execution())


def test_unresolved_rank_must_exist_in_the_rank_order():
    from nse_pipeline.backtest.sizing import size_orders

    with pytest.raises(ValueError):
        size_orders("P2", "1000", ["500"], price="1", slots=1,
                    costs=zero_costs(), execution=execution(), unresolved_ranks=(1,))


def test_sizing_needs_an_explicit_fee_model():
    from nse_pipeline.backtest.sizing import size_orders

    with pytest.raises(ValueError):
        size_orders("P2", "1000", ["500"], price="1", slots=1,
                    costs=zero_costs(), execution=None)


def test_size_orders_rejects_portfolio_spec_directly():
    from nse_pipeline.backtest.sizing import size_orders
    from nse_pipeline.specs import PortfolioSpec

    with pytest.raises(ValueError, match="PortfolioSpec"):
        size_orders(PortfolioSpec(), "1000", ["500"], price="1", slots=1,
                    costs=zero_costs(), execution=execution())


def test_p1_p2_p3_discriminating_golden():
    cases = {case["id"]: case for case in policy_cases()}
    results = {}
    for case_id in ("rank0_unresolved_cash900_p1", "rank0_unresolved_cash900_p2",
                    "rank0_unresolved_cash900_p3"):
        case = cases[case_id]
        result = run_policy_case(case)
        results[case_id] = result
        assert result.policy == case["policy"]
        assert result.filled == tuple(D(value) for value in case["expected_filled"])
        assert result.remaining_cash == D(case["expected_cash"])
        assert result.fees == D(case["expected_fees"])
        assert result.reasons == tuple(case["expected_reasons"])
    assert len({result.filled for result in results.values()}) == 3


def test_sizing_golden_covers_all_three_policies():
    cases = policy_cases()
    assert {case["policy"] for case in cases} == {
        "P1_freeze_on_unresolved", "P2_size_excluding_unresolved",
        "P3_size_including_unresolved_at_screen"}
    assert not any(case["policy"] in {"P1", "P2", "P3"} for case in cases)
    for case in cases:
        result = run_policy_case(case)
        assert result.policy == case["policy"]
        assert result.filled == tuple(D(value) for value in case["expected_filled"])
        assert result.remaining_cash == D(case["expected_cash"])
        assert result.fees == D(case["expected_fees"])
        assert result.reasons == tuple(case["expected_reasons"])
    assert __doc__ and "policy_cases.json" in __doc__
    by_id = {case["id"]: case for case in cases}
    triple = [run_policy_case(by_id[case_id]).filled
              for case_id in ("rank0_unresolved_cash900_p1", "rank0_unresolved_cash900_p2",
                              "rank0_unresolved_cash900_p3")]
    assert len(set(triple)) == 3


def test_p3_screen_case_discriminates_from_p2():
    from nse_pipeline.backtest.sizing import size_orders

    cases = {case["id"]: case for case in policy_cases()}
    clip = cases["sizing_p3_screen_clip"]
    p3 = run_policy_case(clip)
    assert p3.filled == (D("100000"), D("0"))
    assert p3.remaining_cash == D("0")
    assert p3.fees == D("0")
    assert p3.reasons == ("filled_screen_value", "no_cash")
    p2 = run_policy_case({**clip, "policy": "P2_size_excluding_unresolved",
                          "screen_values": None, "screen_prices": None})
    assert p2.filled == (D("0"), D("30000"))
    assert p2.remaining_cash == D("70000")
    assert p2.reasons == ("excluded_unresolved", "filled")
    assert p3.filled != p2.filled
    unavailable_case = cases["sizing_p3_unavailable"]
    unavailable = run_policy_case(unavailable_case)
    assert unavailable.filled == (D("0"), D("50000"))
    assert unavailable.remaining_cash == D("50000")
    assert unavailable.reasons == ("screen_value_unavailable", "filled")
    missing_price = size_orders(unavailable_case["policy"], unavailable_case["cash"],
                                unavailable_case["desired_in_rank_order"],
                                price=unavailable_case["price"],
                                slots=unavailable_case["slots"],
                                costs=policy_cost_model(unavailable_case["cost_model"]),
                                execution=policy_execution(),
                                unresolved_ranks=unavailable_case["unresolved_ranks"],
                                screen_values=("50000",))
    assert missing_price.filled == (D("0"), D("50000"))
    assert missing_price.reasons == ("screen_value_unavailable", "filled")
