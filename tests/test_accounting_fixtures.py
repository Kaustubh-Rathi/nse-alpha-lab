"""Audit hand-authored economic event vectors, NOT production-kernel readiness.

These golden data are the future kernel's independent expected outputs. This
small Decimal oracle only sums explicit deltas; it does not derive actions,
fills, entitlement, settlement dates, or policy decisions from market data.
"""
from decimal import Decimal
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "golden/accounting_cases.json"
BALANCES = ("cash", "sale_receivable", "dividend_receivable", "other_receivable",
            "liability", "order_reserve", "payable_reserve", "shares")


def audit_hand_calculated_case(case):
    balances = {name: Decimal(str(case["initial"].get(name, 0))) for name in BALANCES}
    mark = case["initial"].get("mark")
    keys = []
    for row in case["events"]:
        keys.append((row["session"], row["phase"], row["sequence"], row["id"]))
        assert set(row["delta"]) <= set(BALANCES)
        for name, value in row["delta"].items():
            balances[name] += Decimal(str(value))
        if "mark" in row:
            mark = row["mark"]
        assert all(value >= 0 for value in balances.values())
        assert balances["payable_reserve"] == balances["liability"]
        available = balances["cash"] - balances["order_reserve"] - balances["payable_reserve"]
        assert available >= 0
        assets = sum(balances[name] for name in ("cash", "sale_receivable", "dividend_receivable", "other_receivable"))
        nav = (None if mark is None and balances["shares"] else
               assets + balances["shares"] * Decimal(str(mark or 0)) - balances["liability"])
        actual = dict(balances, cash_available=available, nav=nav)
        for name, expected in row["expected"].items():
            assert actual[name] == (None if expected is None else Decimal(str(expected))), (case["id"], row["id"], name)
    assert keys == sorted(keys)
    assert len({row["id"] for row in case["events"]}) == len(keys)


def test_prefunded_purchase_and_sale_golden_vectors():
    records = json.loads(FIXTURES.read_text(encoding="utf-8"))
    assert records["status"] == "independent_oracle_only_not_production_P0_pass"
    assert records["schema_version"] == 1
    for case in records["cases"]:
        audit_hand_calculated_case(case)


@pytest.mark.parametrize("case_id", ["dividend_lag", "split_up", "split_down",
                                    "absence_reopening", "reservation_cancel", "verified_proceeds"])
def test_additional_hand_calculated_vectors(case_id):
    cases = {row["id"]: row for row in json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]}
    audit_hand_calculated_case(cases[case_id])


def test_oracle_detects_spendable_sale_receivable_and_missing_fee():
    import copy

    cases = {row["id"]: row for row in json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]}
    sale = copy.deepcopy(cases["unsettled_sale"])
    sale["events"][0]["delta"]["cash"] = "199"
    with pytest.raises(AssertionError):
        audit_hand_calculated_case(sale)
    purchase = copy.deepcopy(cases["prefunded_purchase"])
    purchase["events"][1]["delta"]["liability"] = "300"  # fee omitted
    with pytest.raises(AssertionError):
        audit_hand_calculated_case(purchase)
