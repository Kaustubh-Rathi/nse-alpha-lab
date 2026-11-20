"""Independent closed-form valuation examples; no engine functions imported."""
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path

import pytest

CASES = Path(__file__).parent / "golden/valuation_cases.json"


@pytest.mark.parametrize("case_id", ("mixed", "larger_unknown_reference", "no_reference", "empty"))
def test_reference_coverage_is_distinct_from_position_completeness(case_id):
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    case = next(row for row in cases if row["id"] == case_id)
    positions = case["positions"]
    priced = [p for p in positions if p["mark"] is not None]
    unresolved = [p for p in positions if p["mark"] is None]
    marked = sum((Decimal(p["shares"]) * Decimal(p["mark"]) for p in priced), Decimal(0))
    completeness = Fraction(len(priced), len(positions)) if positions else Fraction(1)
    reference = None if any(p["reference"] is None for p in unresolved) else sum(
        (Decimal(p["shares"]) * Decimal(p["reference"]) for p in unresolved), Decimal(0))
    coverage = None if reference is None or marked + reference == 0 else Fraction(marked) / Fraction(marked + reference)
    nav = None if unresolved else Decimal(case["cash"]) + marked + Decimal(case["receivables"]) - Decimal(case["liabilities"])
    expected = case["expected"]
    assert completeness == Fraction(expected["position_completeness"])
    assert coverage == (None if expected["reference_coverage"] is None else Fraction(expected["reference_coverage"]))
    assert nav == (None if expected["nav"] is None else Decimal(expected["nav"]))
    # A partial liquidatable subtotal is not called NAV, even if numerically
    # identical to the explicitly zero-recovery scenario.
    liquidation_known = Decimal(case["cash"]) + marked - Decimal(case["liabilities"])
    assert liquidation_known == Decimal(expected["liquidation_known_zero_friction"])
