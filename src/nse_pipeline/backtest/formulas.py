"""Bounded, deterministic price formulas with point-in-time scoped adjustments.

This module admits a deliberately small expression language, not Python code. Its
identity describes the declared expression and bindings; adjustment provenance is
separately captured from the existing scoped-adjustment implementation at the
requested horizon. It does not authenticate caller-supplied evidence or prove that
mathematically equivalent expressions have the same identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import re
import weakref
from types import MappingProxyType
from typing import Mapping

from .accounting_types import EventTime, Phase, identifier
from .evidence import ObservationLedger
from .observations import Bar
from .scoped_adjustment import AdjustmentFactor, RawPriceSeries, adjusted_series

_FORMULA_VERSION = 1
_MAX_NODES = 64
_MAX_DEPTH = 16
_MAX_NUMBER_CHARS = 256
_MAX_RESULT_BITS = 4096
_MAX_PRICE_REQUESTS = 10000
_MAX_ADJUSTMENT_FACTORS = 1024
_MAX_EVIDENCE_KEYS = 10000
_MAX_NUMBER_BITS = 1024
_MAX_DECIMAL_DIGITS = 256
_MAX_EXPONENT = 308
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
_DECIMAL_TEXT = re.compile(
    r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?)|(?:\.[0-9]+))(?:[eE][+-]?[0-9]+)?\Z")
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+\Z")
_SLASH_TEXT = re.compile(r"[+-]?[0-9]+/[+-]?[0-9]+\Z")
_BINARY = frozenset(("add", "subtract", "multiply", "divide"))
_ARITY = {"raw_close": 0, "scoped_adjusted_close": 0, "constant": 0,
          "parameter": 0, "add": 2, "subtract": 2, "multiply": 2, "divide": 2}


def _bounded_integer(text: str) -> int:
    """Parse an integer literal only after bounding its digits and magnitude."""
    if not _INTEGER_TEXT.fullmatch(text):
        raise ValueError("formula_number_must_be_exact_rational")
    digits = text.lstrip("+-")
    if len(digits) > _MAX_DECIMAL_DIGITS:
        raise ValueError("formula_number_too_large")
    number = int(text)
    if abs(number).bit_length() > _MAX_NUMBER_BITS:
        raise ValueError("formula_number_too_large")
    return number


def _parse_exact_text(value: str) -> Fraction:
    """Parse a rational or decimal/scientific string without expanding a huge exponent."""
    if len(value) > _MAX_NUMBER_CHARS:
        raise ValueError("formula_number_too_large")
    text = value.strip()
    if not text:
        raise ValueError("formula_number_must_be_exact_rational")
    if "/" in text:
        if not _SLASH_TEXT.fullmatch(text):
            raise ValueError("formula_number_must_be_exact_rational")
        numerator_text, denominator_text = text.split("/")
        numerator = _bounded_integer(numerator_text)
        denominator = _bounded_integer(denominator_text)
        if denominator == 0:
            raise ValueError("formula_number_must_be_exact_rational")
        return Fraction(numerator, denominator)
    if not _DECIMAL_TEXT.fullmatch(text):
        raise ValueError("formula_number_must_be_exact_rational")
    mantissa = text.split("e", 1)[0].split("E", 1)[0].lstrip("+-").replace(".", "")
    if len(mantissa) > _MAX_DECIMAL_DIGITS:
        raise ValueError("formula_number_too_large")
    exponent_text = re.split(r"[eE]", text, maxsplit=1)
    if len(exponent_text) == 2:
        exponent_digits = exponent_text[1].lstrip("+-")
        if len(exponent_digits) > 6:
            raise ValueError("formula_number_too_large")
        if abs(int(exponent_text[1])) > _MAX_EXPONENT:
            raise ValueError("formula_number_too_large")
    return Fraction(text)


def _exact(value) -> Fraction:
    """Parse only bounded exact numeric values; bool and float are never admitted."""
    if type(value) is int:
        if value.bit_length() > _MAX_NUMBER_BITS:
            raise ValueError("formula_number_too_large")
        result = Fraction(value)
    elif type(value) is Fraction:
        result = value
    elif type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("formula_number_must_be_finite")
        _sign, digits, exponent = value.as_tuple()
        if len(digits) > _MAX_DECIMAL_DIGITS or abs(exponent) > _MAX_EXPONENT:
            raise ValueError("formula_number_too_large")
        result = Fraction(value)
    elif type(value) is str:
        result = _parse_exact_text(value)
    else:
        raise TypeError("Formula numbers must be int, Decimal, Fraction or rational string; not float/bool")
    if (abs(result.numerator).bit_length() > _MAX_NUMBER_BITS
            or result.denominator.bit_length() > _MAX_NUMBER_BITS):
        raise ValueError("formula_number_too_large")
    return result


def _bounded_tuple(value, limit: int, message: str) -> tuple:
    """Consume an iterable only up to ``limit`` items, rejecting longer input."""
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise ValueError(message)
    items = []
    for item in value:
        if len(items) >= limit:
            raise ValueError(message)
        items.append(item)
    return tuple(items)


def _fraction_json(value: Fraction) -> list[int]:
    return [value.numerator, value.denominator]


@dataclass(frozen=True)
class FormulaExpr:
    """One immutable node in the strict formula language.

    Construct with the module factories (``raw_close``, ``constant``, ``add``, etc.).
    Formula construction revalidates even directly-created nodes.
    """

    op: str
    args: tuple["FormulaExpr", ...] = ()
    value: Fraction | None = None
    name: str | None = None

    def __post_init__(self):
        if type(self.op) is not str:
            raise TypeError("formula_operation_must_be_a_string")
        if type(self.args) is not tuple:
            raise TypeError("formula_args_must_be_an_immutable_tuple")
        if self.value is not None and type(self.value) is not Fraction:
            raise TypeError("formula_payload_must_be_an_exact_fraction")
        if self.name is not None and type(self.name) is not str:
            raise TypeError("formula_parameter_name_must_be_a_string")


def raw_close() -> FormulaExpr:
    return FormulaExpr("raw_close")


def scoped_adjusted_close() -> FormulaExpr:
    return FormulaExpr("scoped_adjusted_close")


def constant(value) -> FormulaExpr:
    return FormulaExpr("constant", value=_exact(value))


def parameter(name: str) -> FormulaExpr:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid_formula_parameter_name")
    return FormulaExpr("parameter", name=name)


def _binary(op: str, left: FormulaExpr, right: FormulaExpr) -> FormulaExpr:
    if type(left) is not FormulaExpr or type(right) is not FormulaExpr:
        raise TypeError("Formula operations accept FormulaExpr values only")
    return FormulaExpr(op, (left, right))


def add(left: FormulaExpr, right: FormulaExpr) -> FormulaExpr:
    return _binary("add", left, right)


def subtract(left: FormulaExpr, right: FormulaExpr) -> FormulaExpr:
    return _binary("subtract", left, right)


def multiply(left: FormulaExpr, right: FormulaExpr) -> FormulaExpr:
    return _binary("multiply", left, right)


def divide(left: FormulaExpr, right: FormulaExpr) -> FormulaExpr:
    return _binary("divide", left, right)


def _validate_tree(root: FormulaExpr):
    if type(root) is not FormulaExpr:
        raise TypeError("formula_expression_required")
    count = 0
    names: set[str] = set()
    price_leaves = set()

    def visit(node, depth):
        nonlocal count
        if type(node) is not FormulaExpr:
            raise TypeError("formula_expression_required")
        count += 1
        if count > _MAX_NODES:
            raise ValueError("formula_expression_too_large")
        if depth > _MAX_DEPTH:
            raise ValueError("formula_expression_too_deep")
        if node.op not in _ARITY:
            raise ValueError("unsupported_formula_operation")
        if len(node.args) != _ARITY[node.op] or any(not isinstance(arg, FormulaExpr) for arg in node.args):
            raise ValueError("invalid_formula_operation_arity")
        if node.op in _BINARY:
            if node.value is not None or node.name is not None:
                raise ValueError("formula_operation_has_unexpected_payload")
            for arg in node.args:
                visit(arg, depth + 1)
        elif node.op == "constant":
            if node.args or node.name is not None or not isinstance(node.value, Fraction):
                raise ValueError("invalid_formula_constant")
            # Re-check size in case a node was built without the public factory.
            _exact(node.value)
        elif node.op == "parameter":
            if node.args or node.value is not None or not isinstance(node.name, str) or not _NAME.fullmatch(node.name):
                raise ValueError("invalid_formula_parameter")
            names.add(node.name)
        else:
            if node.args or node.value is not None or node.name is not None:
                raise ValueError("invalid_formula_price_leaf")
            price_leaves.add(node.op)

    visit(root, 1)
    if not price_leaves:
        raise ValueError("formula_must_reference_a_price")
    return names, price_leaves


def _node_record(node: FormulaExpr):
    if node.op == "constant":
        return ["constant", *_fraction_json(node.value)]
    if node.op == "parameter":
        return ["parameter", node.name]
    if node.op in ("raw_close", "scoped_adjusted_close"):
        return [node.op]
    return [node.op, *(_node_record(child) for child in node.args)]


@dataclass(frozen=True, init=False)
class Formula:
    expression: FormulaExpr
    parameters: Mapping[str, Fraction]
    canonical_json: str
    sha256: str
    parameter_names: tuple[str, ...]
    uses_adjustment: bool
    uses_raw: bool

    def __init__(self, expression: FormulaExpr, parameters: Mapping[str, object] | None = None):
        names, leaves = _validate_tree(expression)
        if parameters is None:
            parameters = {}
        if type(parameters) is not dict and type(parameters) is not MappingProxyType:
            raise TypeError("formula_parameters_must_be_an_immutable_or_plain_mapping")
        normalized = {}
        for name, value in parameters.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError("invalid_formula_parameter_name")
            if name in normalized:
                raise ValueError("duplicate_formula_parameter")
            normalized[name] = _exact(value)
        if set(normalized) != names:
            missing = sorted(names - set(normalized))
            unused = sorted(set(normalized) - names)
            raise ValueError(f"formula_parameter_binding_mismatch:missing={missing},unused={unused}")
        record = {"version": _FORMULA_VERSION, "expression": _node_record(expression),
                  "parameters": {name: _fraction_json(normalized[name]) for name in sorted(normalized)}}
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        object.__setattr__(self, "expression", expression)
        object.__setattr__(self, "parameters", MappingProxyType(dict(sorted(normalized.items()))))
        object.__setattr__(self, "canonical_json", encoded)
        object.__setattr__(self, "sha256", hashlib.sha256(encoded.encode("utf-8")).hexdigest())
        object.__setattr__(self, "parameter_names", tuple(sorted(names)))
        object.__setattr__(self, "uses_adjustment", "scoped_adjusted_close" in leaves)
        object.__setattr__(self, "uses_raw", "raw_close" in leaves)


def _formula_snapshot(value: Formula) -> Formula:
    """Rebind a definition to its identity before trusting deserialized/cached fields."""
    if type(value) is not Formula:
        raise TypeError("formula_and_observation_ledger_required")
    rebuilt = Formula(value.expression, value.parameters)
    if (value.canonical_json != rebuilt.canonical_json or value.sha256 != rebuilt.sha256
            or value.parameter_names != rebuilt.parameter_names
            or value.uses_adjustment != rebuilt.uses_adjustment
            or value.uses_raw != rebuilt.uses_raw):
        raise ValueError("formula_definition_identity_mismatch")
    return rebuilt


@dataclass(frozen=True)
class AdjustmentEvidence:
    """Caller-supplied, unauthenticated point-in-time action evidence declaration."""

    status: str
    factors: tuple[AdjustmentFactor, ...] = ()

    def __post_init__(self):
        if type(self.status) is not str:
            raise TypeError("adjustment_evidence_status_must_be_a_string")
        object.__setattr__(
            self, "factors",
            _bounded_tuple(self.factors, _MAX_ADJUSTMENT_FACTORS,
                           "formula_adjustment_evidence_too_large"))


@dataclass(frozen=True)
class AdjustmentEvidenceSet:
    """Explicit per-security evidence; one key never inherits another key's factors.

    This is still a caller declaration, not an authentication of the evidence source.
    """

    by_key: Mapping[str, AdjustmentEvidence]

    def __post_init__(self):
        if not isinstance(self.by_key, Mapping):
            raise TypeError("adjustment_evidence_set_must_be_a_mapping")
        items = _bounded_tuple(self.by_key.items(), _MAX_EVIDENCE_KEYS,
                               "formula_adjustment_evidence_set_too_large")
        frozen = {}
        for key, value in items:
            normalized = identifier(key)
            if normalized in frozen:
                raise ValueError("formula_adjustment_evidence_set_duplicate_key")
            if type(value) is not AdjustmentEvidence:
                raise TypeError("adjustment_evidence_set_values_must_be_AdjustmentEvidence")
            frozen[normalized] = value
        object.__setattr__(self, "by_key", MappingProxyType(frozen))

    def for_key(self, key: str) -> AdjustmentEvidence | None:
        return self.by_key.get(key)


@dataclass(frozen=True)
class FormulaBasis:
    formula_sha256: str
    sha256: str
    canonical_json: str
    adjustment_scope: tuple[tuple[object, ...], ...]
    raw_series_sha256: str | None
    version: int = _FORMULA_VERSION


@dataclass(frozen=True)
class FormulaEvaluation:
    key: str
    session: date
    as_of: date
    price: Fraction | None
    reason: str | None
    basis: FormulaBasis


@dataclass(frozen=True)
class FormulaPriceInputs:
    """Immutable exact price inputs plus a full aggregate formula/evidence fingerprint.

    Instances are minted only by :func:`evaluate_formula_prices`; a caller-built
    lookalike is not accepted as production price evidence. Prices remain exact
    ``Fraction`` values, never rounded through ``Decimal``.
    """

    formula_sha256: str
    as_of: date
    price_basis: str
    values: tuple[tuple[str, date, Fraction | None, str | None], ...]
    evaluations: tuple[FormulaEvaluation, ...]
    basis: FormulaBasis

    def value_for(self, key: str, session: date) -> Fraction | None:
        for item_key, item_session, value, _reason in self.values:
            if item_key == key and item_session == session:
                return value
        raise KeyError((key, session))


_ISSUED_PRICE_INPUTS: "weakref.WeakValueDictionary[int, FormulaPriceInputs]" = (
    weakref.WeakValueDictionary())


def admit_price_inputs(value) -> FormulaPriceInputs:
    """Admit only an immutable price-input snapshot minted by this module."""
    if type(value) is not FormulaPriceInputs:
        raise TypeError("formula_price_inputs_required")
    if _ISSUED_PRICE_INPUTS.get(id(value)) is not value:
        raise ValueError("formula_price_inputs_not_module_issued")
    # Frozen data is not an authentication primitive. In addition to issuance,
    # reject stale values or metadata that no longer match their canonical record.
    try:
        rows = []
        for result in value.evaluations:
            if result.as_of != value.as_of or result.basis.formula_sha256 != value.formula_sha256:
                raise ValueError("inconsistent formula evaluation horizon/identity")
            definition = json.loads(result.basis.canonical_json)
            encoded_formula = json.dumps(
                definition["formula"], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if (hashlib.sha256(encoded_formula.encode("utf-8")).hexdigest() != value.formula_sha256
                    or hashlib.sha256(result.basis.canonical_json.encode("utf-8")).hexdigest()
                    != result.basis.sha256):
                raise ValueError("stale formula basis")
            rows.append([result.key, result.session.isoformat(),
                         None if result.price is None else _fraction_json(result.price),
                         result.reason, result.basis.sha256, result.basis.canonical_json])
        expected_values = tuple((r.key, r.session, r.price, r.reason) for r in value.evaluations)
        record = {"version": _FORMULA_VERSION, "formula_sha256": value.formula_sha256,
                  "as_of": value.as_of.isoformat(), "price_basis": value.price_basis,
                  "values": rows}
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if (value.values != expected_values or value.basis.formula_sha256 != value.formula_sha256
                or value.basis.canonical_json != encoded
                or value.basis.sha256 != hashlib.sha256(encoded.encode("utf-8")).hexdigest()):
            raise ValueError("stale formula price inputs")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ValueError("formula_price_inputs_identity_mismatch") from error
    return value


def evaluate_formula_prices(formula: Formula, ledger: ObservationLedger,
                            requests, as_of: date, *,
                            adjustment_evidence: AdjustmentEvidence | AdjustmentEvidenceSet | None = None
                            ) -> FormulaPriceInputs:
    """Evaluate immutable exact ``(key, session)`` price inputs at one horizon.

    Requests are consumed up to a strict bound before any tuple is materialized,
    and duplicate ``(key, session)`` requests are rejected. Every request is
    independently evaluated against evidence visible at ``as_of``; values and
    provenance are returned together without a callable.

    Raw-series provenance is pinned to the decision horizon: only observations on
    or before ``as_of`` contribute to the digest. Evidence whose recorded
    ``raw_series_sha256`` spans a later observation therefore fails closed against
    this horizon-truncated series instead of leaking future facts.
    """
    if type(formula) is not Formula:
        raise TypeError("formula_price_inputs_require_a_validated_formula")
    formula = _formula_snapshot(formula)
    if type(as_of) is not date:
        raise ValueError("formula_decision_horizon_must_be_a_date")
    ledger.session_index(as_of)
    bounded = _bounded_tuple(requests, _MAX_PRICE_REQUESTS,
                             "formula_price_request_set_too_large")
    seen = set()
    evaluated = []
    for request in bounded:
        if type(request) is not tuple or len(request) != 2:
            raise ValueError("formula_price_request_must_be_key_session_pair")
        key, session = request
        if type(key) is not str or not key:
            raise ValueError("formula_price_request_key_must_be_a_nonempty_string")
        if type(session) is not date:
            raise ValueError("formula_price_request_session_must_be_a_date")
        marker = (key, session)
        if marker in seen:
            raise ValueError("formula_duplicate_price_request")
        seen.add(marker)
        evaluated.append(evaluate_formula(formula, ledger, key, session, as_of,
                                          adjustment_evidence=adjustment_evidence))
    evaluated = tuple(sorted(evaluated, key=lambda result: (result.key, result.session)))
    rows = tuple((result.key, result.session, result.price, result.reason, result.basis.sha256)
                 for result in evaluated)
    price_basis = ("mixed" if formula.uses_raw and formula.uses_adjustment else
                   "adj_close" if formula.uses_adjustment else "close")
    aggregate = {"version": _FORMULA_VERSION, "formula_sha256": formula.sha256,
                 "as_of": as_of.isoformat(), "price_basis": price_basis,
                 "values": [[result.key, result.session.isoformat(),
                             None if result.price is None else _fraction_json(result.price),
                             result.reason, result.basis.sha256, result.basis.canonical_json]
                            for result in evaluated]}
    encoded = json.dumps(aggregate, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    scopes = tuple(sorted({(key, session.isoformat(), basis)
                           for key, session, _value, _reason, basis in rows}))
    raw_digests = tuple(sorted({result.basis.raw_series_sha256 for result in evaluated
                               if result.basis.raw_series_sha256 is not None}))
    raw_digest = hashlib.sha256("\n".join(raw_digests).encode("ascii")).hexdigest() if raw_digests else None
    basis = FormulaBasis(formula.sha256, digest, encoded, scopes, raw_digest)
    result = FormulaPriceInputs(
        formula.sha256, as_of, price_basis,
        tuple((key, session, value, reason)
              for key, session, value, reason, _basis in rows),
        evaluated, basis)
    _ISSUED_PRICE_INPUTS[id(result)] = result
    return result


def _raw_series_through(ledger: ObservationLedger, key: str, as_of: date) -> RawPriceSeries:
    rows = []
    for session in ledger.sessions:
        if session > as_of:
            break
        bar = ledger.bar(key, session)
        if bar is not None and bar.valid_ohlcv and bar.close is not None:
            rows.append((session, bar.close))
    if not rows:
        raise ValueError("formula_raw_price_history_unavailable")
    return RawPriceSeries(key, tuple(rows))


def _adjustment_records(result, evidence: AdjustmentEvidence, session: date):
    selected = set(result.applied)
    records = []
    for factor in evidence.factors:
        if (factor.action_id, factor.version) not in selected or not factor.covers(session):
            continue
        records.append((factor.security_key, factor.action_id, factor.effective_date.isoformat(),
                        factor.available_at.session.isoformat(), int(factor.available_at.phase),
                        factor.available_at.sequence, factor.factor.numerator,
                        factor.factor.denominator, factor.scope_from.isoformat(),
                        None if factor.scope_to is None else factor.scope_to.isoformat(),
                        factor.version, factor.citation, factor.raw_series_sha256))
    return tuple(sorted(records, key=lambda row: (row[1], row[10], row[0])))


def _basis(formula: Formula, records=(), raw_digest=None) -> FormulaBasis:
    record = {"version": _FORMULA_VERSION, "formula": json.loads(formula.canonical_json),
              "adjustment_scope": [list(row) for row in records],
              "raw_series_sha256": raw_digest}
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return FormulaBasis(formula.sha256, digest, encoded, tuple(records), raw_digest)


def _evaluate(node: FormulaExpr, values: Mapping[str, Fraction], raw_price: Fraction | None,
              adjusted_price: Fraction | None) -> Fraction:
    if node.op == "constant":
        return node.value
    if node.op == "parameter":
        return values[node.name]
    if node.op == "raw_close":
        if raw_price is None:
            raise ValueError("formula_raw_price_unavailable")
        return raw_price
    if node.op == "scoped_adjusted_close":
        if adjusted_price is None:
            raise ValueError("formula_adjusted_price_unavailable")
        return adjusted_price
    left = _evaluate(node.args[0], values, raw_price, adjusted_price)
    right = _evaluate(node.args[1], values, raw_price, adjusted_price)
    if node.op == "add":
        result = left + right
    elif node.op == "subtract":
        result = left - right
    elif node.op == "multiply":
        result = left * right
    elif node.op == "divide":
        if right == 0:
            raise ValueError("formula_division_by_zero")
        result = left / right
    else:  # Formula validation makes this unreachable.
        raise ValueError("unsupported_formula_operation")
    if (abs(result.numerator).bit_length() > _MAX_RESULT_BITS
            or result.denominator.bit_length() > _MAX_RESULT_BITS):
        raise ValueError("formula_result_too_large")
    return result


def evaluate_formula(formula: Formula, ledger: ObservationLedger, key: str,
                     session: date, as_of: date, *,
                     adjustment_evidence: AdjustmentEvidence | AdjustmentEvidenceSet | None = None
                     ) -> FormulaEvaluation:
    """Evaluate one positive price as of a decision close; never reads beyond ``as_of``.

    For adjusted expressions, callers must explicitly supply evidence with status
    ``verified`` (this is an unauthenticated caller declaration). A per-key
    ``AdjustmentEvidenceSet`` selects only the named key's declaration; a missing
    declaration fails closed for that key. The existing ``adjusted_series``
    machinery decides factor applicability/version/scope. Unknown, quarantined,
    unsupported, malformed, wrong-key, or unavailable adjustments fail closed.
    """
    if type(formula) is not Formula or not isinstance(ledger, ObservationLedger):
        raise TypeError("formula_and_observation_ledger_required")
    formula = _formula_snapshot(formula)
    if type(session) is not date or type(as_of) is not date or session > as_of:
        raise ValueError("formula_session_outside_decision_horizon")
    ledger.session_index(session)
    ledger.session_index(as_of)
    if not isinstance(key, str) or not key:
        raise ValueError("formula_security_key_required")
    records = ()
    raw_digest = None
    raw_bar = ledger.bar(key, session) if formula.uses_raw else None
    raw_value = (Fraction(raw_bar.close) if isinstance(raw_bar, Bar) and raw_bar.valid_ohlcv
                 and raw_bar.close is not None else None)
    if formula.uses_raw:
        try:
            raw_digest = _raw_series_through(ledger, key, as_of).raw_series_sha256
        except ValueError:
            # Preserve an explicit unavailable-price result; never substitute a
            # different source when no valid raw history exists.
            raw_digest = None
    adjusted_value = None
    if formula.uses_adjustment:
        decision_at = EventTime(as_of, Phase.CLOSE_VALUATION)
        if type(adjustment_evidence) is AdjustmentEvidenceSet:
            selected = adjustment_evidence.for_key(key)
            if selected is None:
                basis = _basis(formula)
                return FormulaEvaluation(key, session, as_of, None,
                                         "formula_adjustment_evidence_missing_for_key", basis)
            adjustment_evidence = selected
        if not isinstance(adjustment_evidence, AdjustmentEvidence):
            basis = _basis(formula)
            return FormulaEvaluation(key, session, as_of, None,
                                     "formula_adjustment_evidence_required", basis)
        if adjustment_evidence.status != "verified":
            basis = _basis(formula)
            return FormulaEvaluation(
                key, session, as_of, None,
                f"formula_adjustment_evidence_{adjustment_evidence.status or 'unsupported'}_blocked", basis)
        if len(adjustment_evidence.factors) > _MAX_ADJUSTMENT_FACTORS:
            basis = _basis(formula)
            return FormulaEvaluation(key, session, as_of, None,
                                     "formula_adjustment_evidence_too_large", basis)
        if not all(type(factor) is AdjustmentFactor for factor in adjustment_evidence.factors):
            basis = _basis(formula)
            return FormulaEvaluation(key, session, as_of, None,
                                     "formula_adjustment_evidence_unsupported_blocked", basis)
        try:
            raw_series = _raw_series_through(ledger, key, as_of)
            raw_digest = raw_series.raw_series_sha256
            # Filter by the decision event before invoking the existing adjustment
            # machinery: even irrelevant future facts (including another security's
            # future action) must not affect this value or its provenance.
            admissible = tuple(factor for factor in adjustment_evidence.factors
                               if factor.available_at <= decision_at)
            adjusted = adjusted_series(raw_series, admissible,
                                      mode="point_in_time", decision_at=decision_at)
            value = adjusted.value_on(session)
            adjusted_value = value
            records = _adjustment_records(adjusted, AdjustmentEvidence("verified", admissible),
                                          session)
        except (ValueError, TypeError) as error:
            basis = _basis(formula, records, raw_digest)
            return FormulaEvaluation(key, session, as_of, None,
                                     f"formula_adjustment_blocked:{error}", basis)
    basis = _basis(formula, records, raw_digest)
    try:
        result = _evaluate(formula.expression, formula.parameters, raw_value, adjusted_value)
    except ValueError as error:
        return FormulaEvaluation(key, session, as_of, None, str(error), basis)
    if result <= 0:
        return FormulaEvaluation(key, session, as_of, None, "formula_nonpositive_price", basis)
    return FormulaEvaluation(key, session, as_of, result, None, basis)
