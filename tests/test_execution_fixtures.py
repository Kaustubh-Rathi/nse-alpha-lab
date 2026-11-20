"""Hand arithmetic only: these are future production comparison vectors."""
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path

CASES = Path(__file__).parent / "golden/execution_cases.json"


def test_fee_components_are_rounded_once_and_slippage_only_changes_price():
    case = json.loads(CASES.read_text(encoding="utf-8"))["buy_cost"]
    fill = Decimal(case["reference"]) * (1 + Decimal(case["slippage_bps"]) / 10000)
    notional = Decimal(case["shares"]) * fill
    paise = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    brokerage = paise(min(notional * Decimal("0.0002"), Decimal("2000")))
    components = {"brokerage": brokerage,
                  "gst": paise(brokerage * Decimal("0.18")),
                  "stt": paise(notional * Decimal("0.001")),
                  "stamp": paise(notional * Decimal("0.00015")),
                  "exchange": paise(notional * Decimal("0.00003"))}
    assert fill == Decimal(case["expected"]["fill"])
    assert components == {name: Decimal(value) for name, value in case["expected"]["fees"].items()}
    assert notional + sum(components.values()) == Decimal(case["expected"]["total_payable"])


def test_liquidity_scale_fixture_uses_a_binding_asof_capacity():
    cases = json.loads(CASES.read_text(encoding="utf-8"))["liquidity"]
    for case in cases:
        capacity = Decimal(case["adv"]) * Decimal(case["participation_rate"])
        shares = int(min(Decimal(case["target"]), capacity, Decimal(case["cash"])) / Decimal(case["open"]))
        assert shares == case["expected_shares"]


def test_screen_sizing_desires_do_not_create_cash():
    cases = json.loads(CASES.read_text(encoding="utf-8"))["sizing"]
    for case in cases:
        cash = Decimal(case["cash"])
        filled = []
        for desired in case["desired_in_rank_order"]:
            notional = min(Decimal(desired), cash)
            cash -= notional
            filled.append(notional)
        assert filled == [Decimal(value) for value in case["expected_filled"]]
        assert cash == Decimal(case["expected_cash"])
