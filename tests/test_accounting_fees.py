from datetime import date
from decimal import Decimal as D, ROUND_FLOOR
import json
from pathlib import Path

import pytest

CAP_GOLDEN = Path(__file__).parent / "golden/cap_boundary_cases.json"


def zero_costs():
    from nse_pipeline.specs import CostSpec
    return CostSpec(brokerage_bps=0, stt_bps_buy=0, stt_bps_sell=0,
                    stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)


def test_production_fee_quote_matches_independent_execution_fixture():
    from nse_pipeline.backtest.fees import opening_quote
    from nse_pipeline.specs import CostSpec, ExecutionSpec

    case = json.loads((Path(__file__).parent / "golden/execution_cases.json").read_text())["buy_cost"]
    result = opening_quote("buy", int(case["shares"]), case["reference"], CostSpec(),
                           ExecutionSpec(trade_settlement_lag_sessions=1))
    assert result.fill_price == D(case["expected"]["fill"])
    assert dict(result.fees.components) == {k: D(v) for k, v in case["expected"]["fees"].items()}
    assert result.notional + result.fees.total == D(case["expected"]["total_payable"])
    sell = opening_quote("sell", 1000, "100", CostSpec(), ExecutionSpec(trade_settlement_lag_sessions=1))
    assert sell.fill_price == D("99.95") and sell.fees.stamp == 0
    # 19.99 brokerage + 3.60 GST + 99.95 STT + 3.00 exchange = 126.54.
    assert sell.notional - sell.fees.total == D("99823.46")


def test_affordability_counts_fees_rounding_and_integer_boundaries():
    from nse_pipeline.backtest.fees import affordable_buy
    from nse_pipeline.specs import ExecutionSpec, CostSpec

    execution = ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0)
    assert affordable_buy(5, "60", "250", "250", zero_costs(), execution).quantity == 4
    assert affordable_buy(1, "60", "59.99", "100", zero_costs(), execution).quantity == 0
    assert affordable_buy(1, "60", "60", "100", zero_costs(), execution).quantity == 1
    costs = CostSpec(brokerage_bps=10000, brokerage_cap_per_order=1, stt_bps_buy=0,
                     stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)
    assert affordable_buy(1, "60", "60", "100", costs, execution).quantity == 0
    assert affordable_buy(1, "60", "61", "100", costs, execution).quantity == 1
    skip = ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                         partial_fill_policy="skip")
    assert affordable_buy(5, "60", "250", "250", zero_costs(), skip).quantity == 0


@pytest.mark.parametrize("quantity,price", [(True, "10"), (1.2, "10"), (-1, "10"), (1, "0"), (1, "NaN")])
def test_invalid_fills_do_not_produce_quotes(quantity, price):
    from nse_pipeline.backtest.fees import opening_quote
    from nse_pipeline.specs import ExecutionSpec
    with pytest.raises((ValueError, TypeError)):
        opening_quote("buy", quantity, price, zero_costs(), ExecutionSpec(trade_settlement_lag_sessions=1))


def test_production_integer_fills_match_all_independent_capacity_vectors():
    from nse_pipeline.backtest.fees import affordable_buy
    from nse_pipeline.specs import ExecutionSpec
    cases = json.loads((Path(__file__).parent / "golden/execution_cases.json").read_text())["liquidity"]
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0)
    for case in cases:
        requested = int(D(case["target"]) / D(case["open"]))
        supplied_asof_capacity = D(case["adv"]) * D(case["participation_rate"])
        quote = affordable_buy(requested, case["open"], case["cash"], supplied_asof_capacity, zero_costs(), execution)
        assert quote.quantity == case["expected_shares"]


T1_1_ROWS = (
    # declared cap, applied principal cap, quantity, executed principal
    ("99.994", "99.99", 9, "90.00"),
    ("99.996", "99.99", 9, "90.00"),
    ("19.999", "19.99", 1, "10.00"),
    ("100.00", "100.00", 10, "100.00"),
    ("99.99", "99.99", 9, "90.00"),
    ("0.00", "0.00", 0, "0.00"),
)


@pytest.mark.parametrize("declared,applied,quantity,notional", T1_1_ROWS)
def test_cap_boundary_table_buy_and_sell(declared, applied, quantity, notional):
    from nse_pipeline.backtest.fees import affordable_buy, bounded_sell
    from nse_pipeline.specs import ExecutionSpec

    execution = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=float(declared),
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    buy = affordable_buy(100, "10", "1000000", "1000000", zero_costs(), execution)
    sell = bounded_sell(100, "10", zero_costs(), execution)
    for quote, zero_reason in (
            (buy, "unaffordable_or_partial_disallowed"),
            (sell, "notional_limit_or_partial_disallowed")):
        assert quote.applied_principal_cap == D(applied)
        assert quote.quantity == quantity
        assert quote.notional == D(notional)
        assert quote.fees.total == 0
        # The declared cap is a hard maximum even though only its floor is applied.
        assert quote.notional <= D(declared)
        assert quote.reason == (zero_reason if quantity == 0 else "filled")


T1_2_ROWS = (
    # declared cap, applied principal cap, quantity, executed principal
    ("99.991", "99.99", 9, "90.00"),
    ("99.999", "99.99", 9, "90.00"),
    ("100.001", "100.00", 10, "100.00"),
    ("19.9999", "19.99", 1, "10.00"),
)


@pytest.mark.parametrize("declared,applied,quantity,notional", T1_2_ROWS)
def test_cap_never_rounds_up_at_paise_boundary(declared, applied, quantity, notional):
    from nse_pipeline.backtest.fees import affordable_buy, bounded_sell
    from nse_pipeline.specs import ExecutionSpec

    execution = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=float(declared),
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    buy = affordable_buy(100, "10", "1000000", "1000000", zero_costs(), execution)
    sell = bounded_sell(100, "10", zero_costs(), execution)
    for quote in (buy, sell):
        assert quote.applied_principal_cap == D(applied)
        assert quote.quantity == quantity
        assert quote.notional == D(notional)
        assert quote.notional <= D(declared)


def test_zero_cap_and_zero_budget():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.fees import affordable_buy, bounded_sell
    from nse_pipeline.specs import CostSpec, ExecutionSpec

    execution = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=0,
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    costs = CostSpec(brokerage_bps=100, brokerage_cap_per_order=5, stt_bps_buy=10,
                     stt_bps_sell=10, stamp_bps_buy=1.5, exchange_bps=0.3,
                     gst_on_brokerage_pct=18)
    buy = affordable_buy(10, "10", "0", "0", costs, execution)
    assert buy.applied_principal_cap == D("0.00")
    assert buy.quantity == 0 and buy.notional == D("0.00")
    assert buy.fees.total == D("0.00")
    assert buy.notional + buy.fees.total == D("0.00")  # payable
    assert D("0") - (buy.notional + buy.fees.total) == D("0.00")  # buying power
    sell = bounded_sell(10, "10", costs, execution)
    assert sell.applied_principal_cap == D("0.00")
    assert sell.quantity == 0 and sell.notional == D("0.00")
    assert sell.fees.total == D("0.00")
    assert sell.notional - sell.fees.total == D("0.00")  # net proceeds
    assert D("0") + (sell.notional - sell.fees.total) == D("0.00")

    # Zero cash cannot fund a reservation at all, so the ledger path is the
    # rejection itself: the failed attempt creates no order and moves no cash.
    book = Portfolio("0", on=date(2024, 6, 4))
    before, events = book.snapshot, book.events
    with pytest.raises(ValueError):
        book.reserve("zero", "AAA", 10, "100", "100",
                     at=EventTime(date(2024, 6, 4), Phase.CLOSE_ORDERS))
    assert book.snapshot == before and book.events == events == ()
    assert book.balances.cash == 0 and book.balances.cash_available == 0
    assert book.balances.order_reserved == 0 and book.balances.liabilities == 0


def test_fee_inclusive_cash_is_not_the_cap():
    from nse_pipeline.backtest.fees import affordable_buy
    from nse_pipeline.specs import CostSpec, ExecutionSpec

    execution = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=1000,
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    costs = CostSpec(brokerage_bps=100, brokerage_cap_per_order=1000, stt_bps_buy=0,
                     stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)
    quote = affordable_buy(10, "100", "1000", "1000", costs, execution)
    # 9 * 100 = 900 principal, 100 bps brokerage = 9.00, payable 909.00.
    assert quote.applied_principal_cap == D("1000.00")
    assert quote.quantity == 9
    assert quote.notional == D("900.00")
    assert quote.fees.brokerage == D("9.00") and quote.fees.total == D("9.00")
    payable = quote.notional + quote.fees.total
    assert payable == D("909.00")
    assert D("1000") - payable == D("91.00")  # remaining buying power
    assert D("1000") - quote.notional == D("100.00")  # remaining principal cap
    # Ten shares would have principal 1000.00 but payable 1010.00, so ten cannot
    # fill against the 1000.00 cash budget; the quantity of nine proves rejection.
    ten_principal, ten_brokerage = D("100") * 10, D("1000") * D("100") / D("10000")
    assert ten_principal + ten_brokerage == D("1010.00") > D("1000")
    assert quote.quantity != 10


@pytest.mark.parametrize("declared,applied,quantity,notional", (
    ("100000", "100000.00", 10_000_000, "100000.00"),
    ("99999.999", "99999.99", 9_999_999, "99999.99"),
))
def test_large_quantity_does_not_overflow_or_round(declared, applied, quantity, notional):
    from nse_pipeline.backtest.fees import affordable_buy, bounded_sell
    from nse_pipeline.specs import ExecutionSpec

    execution = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=float(declared),
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
    buy = affordable_buy(20_000_000, "0.01", "100000000", "100000000", zero_costs(), execution)
    sell = bounded_sell(20_000_000, "0.01", zero_costs(), execution)
    for quote in (buy, sell):
        assert quote.applied_principal_cap == D(applied)
        assert quote.quantity == quantity
        assert quote.notional == D(notional)
        assert str(quote.notional) == notional  # no float/scientific-notation drift
        assert quote.notional <= D(declared)


def test_cap_boundary_golden_is_hand_derived_and_matches_buy_and_sell():
    from nse_pipeline.backtest.fees import affordable_buy, bounded_sell
    from nse_pipeline.specs import ExecutionSpec

    golden = json.loads(CAP_GOLDEN.read_text())
    assert golden["schema_version"] == 1
    assert golden["derivation"].strip()
    ids = [case["id"] for case in golden["cases"]]
    assert len(ids) == len(set(ids))
    for case in golden["cases"]:
        assert case["working"].strip()
        for row in case["rows"]:
            execution = ExecutionSpec(trade_settlement_lag_sessions=1,
                                      max_order_notional=float(row["declared_cap"]),
                                      adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
            buy = affordable_buy(int(case["desired_quantity"]), case["reference_price"],
                                 case["cash_budget"], case["capacity_notional"], zero_costs(), execution)
            sell = bounded_sell(int(case["desired_quantity"]), case["reference_price"],
                                zero_costs(), execution)
            for quote in (buy, sell):
                assert quote.applied_principal_cap == D(row["applied_principal_cap"]), (case["id"], row)
                assert quote.quantity == row["quantity"], (case["id"], row)
                assert quote.notional == D(row["notional"]), (case["id"], row)
                assert quote.notional <= D(row["declared_cap"]), (case["id"], row)


def test_cap_monotonicity_property():
    from nse_pipeline.backtest.fees import affordable_buy, bounded_sell
    from nse_pipeline.specs import ExecutionSpec

    previous_buy = previous_sell = -1
    for paise in range(25_001):
        declared = D(paise) / D(100)
        applied = declared.quantize(D("0.01"), rounding=ROUND_FLOOR)
        # Price 10.00 is 1000 paise, so the unique largest q <= 25 is p // 1000.
        expected = min(25, paise // 1000)
        execution = ExecutionSpec(trade_settlement_lag_sessions=1, max_order_notional=float(declared),
                                  adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0)
        buy = affordable_buy(25, "10", "1000000", "1000000", zero_costs(), execution)
        sell = bounded_sell(25, "10", zero_costs(), execution)
        for quote in (buy, sell):
            assert quote.applied_principal_cap == applied
            assert quote.quantity == expected
            assert quote.notional == D(expected * 10)
            assert quote.notional <= declared
            if expected < 25:
                assert D((expected + 1) * 10) > applied  # q+1 cannot fit
        assert buy.quantity >= previous_buy and sell.quantity >= previous_sell
        assert (buy.quantity == 25) == (applied >= D("250"))
        assert (buy.quantity == 0) == (applied < D("10"))
        previous_buy, previous_sell = buy.quantity, sell.quantity
    assert previous_buy == previous_sell == 25
