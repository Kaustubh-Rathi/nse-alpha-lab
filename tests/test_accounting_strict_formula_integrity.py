"""A copied/stale formula definition cannot retain an unrelated intrinsic identity."""
from copy import copy
from fractions import Fraction

import pytest

from nse_pipeline.backtest.formulas import (
    Formula, admit_price_inputs, constant, evaluate_formula, evaluate_formula_prices,
    multiply, raw_close)
from tests.test_accounting_strict_formulas import DAYS, KEY, ledger


def test_stale_formula_definition_cannot_reuse_original_canonical_identity():
    original = Formula(raw_close())
    stale = copy(original)
    # Simulate a corrupted deserializer bypassing frozen dataclass construction.
    object.__setattr__(stale, "expression", multiply(raw_close(), constant(2)))
    assert stale.sha256 == original.sha256
    with pytest.raises(ValueError, match="formula_definition_identity_mismatch"):
        evaluate_formula(stale, ledger(), KEY, DAYS[1], DAYS[1])


def test_materialized_price_tampering_cannot_reuse_unchanged_basis():
    inputs = evaluate_formula_prices(
        Formula(raw_close()), ledger(), ((KEY, DAYS[1]),), DAYS[1])
    object.__setattr__(inputs, "values", ((KEY, DAYS[1], Fraction(999), None),))
    with pytest.raises(ValueError, match="formula_price_inputs_identity_mismatch"):
        admit_price_inputs(inputs)
