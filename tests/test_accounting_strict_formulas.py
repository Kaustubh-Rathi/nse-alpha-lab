"""Direct tests for the bounded, non-Python price formula module."""
from datetime import date
from decimal import Decimal
from fractions import Fraction
import json

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.formulas import (
    AdjustmentEvidence, Formula, FormulaBasis, FormulaExpr, FormulaPriceInputs,
    add, admit_price_inputs, constant, divide, evaluate_formula,
    evaluate_formula_prices, multiply, parameter, raw_close, scoped_adjusted_close, subtract,
)
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.backtest.scoped_adjustment import AdjustmentFactor

DAYS = (date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5))
KEY = "AAA|INE000000001|2019-01-01"


def ledger(closes=("100", "102", "98")):
    stream = SessionStream()
    result = ObservationLedger(stream)
    for on, close in zip(DAYS, closes):
        stream.observe(on)
        price = Decimal(close) if close is not None else None
        valid = close is not None
        bar = Bar(KEY, "AAA", "EQ", on, price, price, price, price,
                  100 if valid else 0, valid, "INE000000001")
        result.ingest(on, (bar,))
    return result


def factor(*, action="split", version=1, value="1/2", available=DAYS[0],
           scope_from=DAYS[0], scope_to=DAYS[1], citation="synthetic-split"):
    return AdjustmentFactor(
        security_key=KEY, action_id=action, effective_date=DAYS[1],
        available_at=EventTime(available, Phase.PRE_OPEN_EVIDENCE), factor=value,
        scope_from=scope_from, scope_to=scope_to, version=version, citation=citation)


def test_raw_arithmetic_and_declared_parameter_evaluate_exactly():
    expr = divide(add(multiply(raw_close(), parameter("scale")), constant("1/2")),
                  subtract(constant(3), constant(1)))
    formula = Formula(expr, {"scale": 2})
    result = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1])

    # (102*2 + 1/2) / (3-1) = 409/4 exactly.
    assert result.price == Fraction(409, 4)
    assert result.reason is None
    assert result.basis.formula_sha256 == formula.sha256
    assert result.basis.raw_series_sha256 is not None


def test_scoped_adjustment_uses_existing_point_in_time_engine_and_records_scope():
    formula = Formula(scoped_adjusted_close())
    evidence = AdjustmentEvidence("verified", (factor(),))
    result = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1],
                              adjustment_evidence=evidence)

    assert result.price == Fraction(51)
    assert result.reason is None
    assert result.basis.adjustment_scope == (
        (KEY, "split", "2024-06-04", "2024-06-03", int(Phase.PRE_OPEN_EVIDENCE),
         0, 1, 2, "2024-06-03", "2024-06-04", 1, "synthetic-split", None),)
    assert result.basis.raw_series_sha256 is not None


def test_split_and_bonus_are_exact_and_both_appear_in_provenance():
    formula = Formula(scoped_adjusted_close())
    split = factor(action="split", value="1/2", citation="split-evidence")
    bonus = factor(action="bonus", value="2/3", citation="bonus-evidence")
    result = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1],
                              adjustment_evidence=AdjustmentEvidence("verified", (bonus, split)))

    # 102 * 1/2 * 2/3 = 34, with each exact factor and version recorded.
    assert result.price == Fraction(34)
    assert [(row[1], row[6], row[7], row[10]) for row in result.basis.adjustment_scope] == [
        ("bonus", 2, 3, 1), ("split", 1, 2, 1)]


def test_adjusted_formula_must_fail_closed_for_unknown_or_quarantined_evidence():
    formula = Formula(scoped_adjusted_close())
    unknown = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1],
                               adjustment_evidence=AdjustmentEvidence("unknown"))
    quarantined = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1],
                                   adjustment_evidence=AdjustmentEvidence("quarantined"))
    unsupported = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1],
                                   adjustment_evidence=AdjustmentEvidence("unsupported"))
    missing = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1])
    assert unknown.price is quarantined.price is unsupported.price is missing.price is None
    assert unknown.reason == "formula_adjustment_evidence_unknown_blocked"
    assert quarantined.reason == "formula_adjustment_evidence_quarantined_blocked"
    assert unsupported.reason == "formula_adjustment_evidence_unsupported_blocked"
    assert missing.reason == "formula_adjustment_evidence_required"


def test_conflicting_same_version_adjustment_fails_closed():
    formula = Formula(scoped_adjusted_close())
    conflict = factor(value="1/3", citation="conflicting-evidence", version=1)
    result = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1],
                              adjustment_evidence=AdjustmentEvidence(
                                  "verified", (factor(), conflict)))
    assert result.price is None
    assert "conflicting_adjustment_versions" in result.reason


def test_later_action_evidence_does_not_change_earlier_price_or_fingerprint():
    formula = Formula(scoped_adjusted_close())
    early = factor()
    later = factor(version=2, value="1/4", available=DAYS[2], citation="future-v2")
    first = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1],
                             adjustment_evidence=AdjustmentEvidence("verified", (early,)))
    appended = evaluate_formula(formula, ledger(), KEY, DAYS[1], DAYS[1],
                                adjustment_evidence=AdjustmentEvidence("verified", (later, early)))
    assert first.price == appended.price == Fraction(51)
    assert first.basis == appended.basis


def test_future_ledger_append_does_not_change_earlier_price_or_fingerprint():
    formula = Formula(raw_close())
    stream = SessionStream()
    book = ObservationLedger(stream)
    for on, close in zip(DAYS[:2], ("100", "102")):
        stream.observe(on)
        price = Decimal(close)
        book.ingest(on, (Bar(KEY, "AAA", "EQ", on, price, price, price, price,
                           100, True, "INE000000001"),))
    first = evaluate_formula(formula, book, KEY, DAYS[1], DAYS[1])

    stream.observe(DAYS[2])
    later_price = Decimal("98")
    book.ingest(DAYS[2], (Bar(KEY, "AAA", "EQ", DAYS[2], later_price, later_price,
                              later_price, later_price, 100, True, "INE000000001"),))
    appended = evaluate_formula(formula, book, KEY, DAYS[1], DAYS[1])
    assert first.price == appended.price == Fraction(102)
    assert first.basis == appended.basis


def test_formula_refuses_to_evaluate_a_session_after_the_decision_horizon():
    formula = Formula(raw_close())
    with pytest.raises(ValueError, match="formula_session_outside_decision_horizon"):
        evaluate_formula(formula, ledger(), KEY, DAYS[2], DAYS[1])


def test_formula_ast_rejects_unknown_ops_bad_arities_and_callable_payloads():
    with pytest.raises(ValueError, match="unsupported_formula_operation"):
        Formula(FormulaExpr("call", (raw_close(),)))
    with pytest.raises(ValueError, match="invalid_formula_operation_arity"):
        Formula(FormulaExpr("add", (raw_close(),)))
    with pytest.raises(ValueError, match="invalid_formula_price_leaf"):
        Formula(FormulaExpr("raw_close", value=Fraction(5)))
    with pytest.raises(TypeError, match="Formula operations accept FormulaExpr"):
        add(raw_close(), lambda: 5)
    with pytest.raises(TypeError, match="formula_args_must_be_an_immutable_tuple"):
        FormulaExpr("add", [raw_close(), raw_close()])
    with pytest.raises(ValueError, match="formula_must_reference_a_price"):
        Formula(add(constant(1), constant(2)))


@pytest.mark.parametrize("bad", [0.5, True, False])
def test_formula_constants_reject_float_and_bool(bad):
    with pytest.raises(TypeError, match="not float/bool"):
        constant(bad)


def test_parameters_must_be_exactly_bound_and_values_reject_float_or_bool():
    expr = add(raw_close(), parameter("offset"))
    with pytest.raises(ValueError, match="formula_parameter_binding_mismatch"):
        Formula(expr)
    with pytest.raises(ValueError, match="formula_parameter_binding_mismatch"):
        Formula(expr, {"offset": 1, "unused": 2})
    with pytest.raises(TypeError, match="not float/bool"):
        Formula(expr, {"offset": 0.25})
    with pytest.raises(TypeError, match="not float/bool"):
        Formula(expr, {"offset": True})


def test_division_by_zero_and_nonpositive_results_are_rejected_as_unrankable():
    zero_divisor = Formula(divide(raw_close(), constant(0)))
    zero_result = evaluate_formula(zero_divisor, ledger(), KEY, DAYS[1], DAYS[1])
    assert zero_result.price is None
    assert zero_result.reason == "formula_division_by_zero"

    nonpositive = Formula(subtract(raw_close(), constant(103)))
    negative_result = evaluate_formula(nonpositive, ledger(), KEY, DAYS[1], DAYS[1])
    assert negative_result.price is None
    assert negative_result.reason == "formula_nonpositive_price"


def test_expression_size_and_depth_are_bounded():
    deep = raw_close()
    for _ in range(17):
        deep = add(deep, constant(1))
    with pytest.raises(ValueError, match="formula_expression_too_deep"):
        Formula(deep)

    def balanced(depth):
        if depth == 0:
            return raw_close()
        return add(balanced(depth - 1), balanced(depth - 1))

    with pytest.raises(ValueError, match="formula_expression_too_large"):
        Formula(balanced(6))


def test_formula_values_are_immutable_and_identity_is_canonical_and_binding_sensitive():
    expr = add(parameter("x"), add(raw_close(), parameter("y")))
    first = Formula(expr, {"x": "1/3", "y": "2/5"})
    reordered = Formula(expr, {"y": Fraction(2, 5), "x": Fraction(1, 3)})
    different = Formula(expr, {"x": "1/2", "y": "2/5"})
    other_tree = Formula(add(raw_close(), add(parameter("x"), parameter("y"))),
                         {"x": "1/3", "y": "2/5"})

    assert first.sha256 == reordered.sha256
    assert first.canonical_json == reordered.canonical_json
    assert len(first.sha256) == 64
    assert first.sha256 != different.sha256
    assert first.sha256 != other_tree.sha256
    with pytest.raises(TypeError):
        first.parameters["x"] = Fraction(2)
    with pytest.raises(AttributeError):
        first.sha256 = "forged"


def test_price_input_aggregation_is_exact_and_horizon_bound():
    formula = Formula(raw_close())
    result = evaluate_formula_prices(
        formula, ledger(), ((KEY, DAYS[0]), (KEY, DAYS[1])), DAYS[1])
    assert result.value_for(KEY, DAYS[0]) == Fraction(100)
    assert result.value_for(KEY, DAYS[1]) == Fraction(102)
    assert result.basis.formula_sha256 == formula.sha256
    assert result.basis.sha256
    aggregate = json.loads(result.basis.canonical_json)
    assert aggregate["values"][0][5] == result.evaluations[0].basis.canonical_json


def test_huge_exponents_are_rejected_before_fraction_expansion(monkeypatch):
    import nse_pipeline.backtest.formulas as formulas

    real_fraction = formulas.Fraction

    def guarded(value, *args):
        if isinstance(value, (Decimal, str)):
            raise AssertionError("expanded an unbounded numeric value")
        return real_fraction(value, *args)

    monkeypatch.setattr(formulas, "Fraction", guarded)
    with pytest.raises(ValueError, match="formula_number_too_large"):
        formulas.constant(Decimal("1E100000000"))
    with pytest.raises(ValueError, match="formula_number_too_large"):
        formulas.constant("1e100000000")
    with pytest.raises(ValueError, match="formula_number_too_large"):
        formulas.constant("1e999999")
    with pytest.raises(ValueError, match="formula_number_too_large"):
        formulas.constant("1" * 1000)
    with pytest.raises(ValueError, match="formula_number_too_large"):
        formulas.Formula(add(raw_close(), parameter("x")), {"x": Decimal("1E100000000")})


def test_price_request_iterables_are_bounded_and_duplicates_rejected():
    formula = Formula(raw_close())
    with pytest.raises(ValueError, match="formula_duplicate_price_request"):
        evaluate_formula_prices(formula, ledger(),
                                ((KEY, DAYS[0]), (KEY, DAYS[0])), DAYS[1])

    def endless():
        index = 0
        while True:
            yield (f"K{index:05d}|INE000000001|2019-01-01", DAYS[1])
            index += 1

    with pytest.raises(ValueError, match="formula_price_request_set_too_large"):
        evaluate_formula_prices(formula, ledger(), endless(), DAYS[1])


def test_adjustment_factor_iterable_is_bounded_before_materialization():
    def endless():
        while True:
            yield object()

    with pytest.raises(ValueError, match="formula_adjustment_evidence_too_large"):
        AdjustmentEvidence("verified", endless())


def test_per_key_evidence_mapping_iteration_is_bounded():
    from collections.abc import Mapping as MappingABC

    class EndlessEvidence(MappingABC):
        def __getitem__(self, key):
            raise KeyError(key)

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

        def items(self):
            index = 0
            while True:
                yield (f"K{index}", AdjustmentEvidence("verified"))
                index += 1

    from nse_pipeline.backtest.formulas import AdjustmentEvidenceSet
    with pytest.raises(ValueError, match="formula_adjustment_evidence_set_too_large"):
        AdjustmentEvidenceSet(EndlessEvidence())


def test_caller_authored_price_inputs_are_not_admitted():
    basis = FormulaBasis("0" * 64, "1" * 64, "{}", (), None)
    forged = FormulaPriceInputs("0" * 64, DAYS[1], "close", (), (), basis)
    with pytest.raises(ValueError, match="formula_price_inputs_not_module_issued"):
        admit_price_inputs(forged)

    real = evaluate_formula_prices(Formula(raw_close()), ledger(),
                                   ((KEY, DAYS[1]),), DAYS[1])
    assert admit_price_inputs(real) is real
    lookalike = FormulaPriceInputs(real.formula_sha256, real.as_of, real.price_basis,
                                   real.values, real.evaluations, real.basis)
    assert lookalike == real
    with pytest.raises(ValueError, match="formula_price_inputs_not_module_issued"):
        admit_price_inputs(lookalike)


def test_strict_formula_interface_cannot_accept_method_or_arbitrary_callable_identity():
    class Stateful:
        def price(self, *_args):
            return Decimal("11")

    with pytest.raises(TypeError, match="formula_expression_required"):
        Formula(Stateful().price)
    with pytest.raises(TypeError, match="formula_expression_required"):
        Formula(lambda *_args: Decimal("12"))
    # There is no basis_id input to Formula or evaluate_formula: the canonical
    # formula digest, not caller attribution, is the identity presented downstream.
    assert "basis_id" not in Formula.__init__.__annotations__
