"""P0-SIGNALS: exact causal momentum ranks with window-scoped observations and truthful basis provenance."""
from datetime import date, timedelta
from decimal import Decimal as D
from fractions import Fraction
import functools
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

from nse_pipeline.backtest import signals as signals_module
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.backtest.signals import MomentumRanks, close_basis, rank_momentum
from nse_pipeline.specs import MomentumSpec

GOLDEN = json.loads((Path(__file__).parent / "golden/research_semantics_cases.json").read_text())
TEN = tuple(date.fromisoformat(value) for value in GOLDEN["sessions"]["ten_observed"])

DAYS = (date(2024, 1, 1), date(2024, 1, 2), date(2024, 2, 15), date(2024, 2, 16),
        date(2024, 6, 3), date(2024, 6, 4))
UNOBSERVED = date(2025, 1, 1)


# The exact source of this callable and of module_level_basis is transcribed into
# tests/golden/research_semantics_cases.json together with an independently computed
# SHA-256. Do not edit either body without re-deriving the golden digest.
def custom_basis(key, session, as_of):
    return D("101")


def module_level_basis(key, session, as_of):
    return D("11") if session == as_of else D("10")


_PROVENANCE_GLOBAL_MULTIPLIER = 11
_PROVENANCE_MUTABLE_PRICE_MAP = {"K": "11"}
_PROVENANCE_PRICE_MODULE = types.ModuleType("provenance_price_source")
_PROVENANCE_PRICE_MODULE.PRICE = "11"


def _global_multiplier_basis(key, session, as_of):
    return D(str(_PROVENANCE_GLOBAL_MULTIPLIER)) if session == as_of else D("10")


def _global_mapping_basis(key, session, as_of):
    return D(_PROVENANCE_MUTABLE_PRICE_MAP[key])


def _mutable_module_basis(key, session, as_of):
    return D(_PROVENANCE_PRICE_MODULE.PRICE)


def _dynamic_lookup_basis(key, session, as_of):
    value = globals()["_PROVENANCE_MUTABLE_PRICE_MAP"][key]
    return D(value) if session == as_of else D("10")


class _MethodBasisHolder:
    def __init__(self, as_of_price):
        self.as_of_price = as_of_price

    def price(self, key, session, as_of):
        return self.as_of_price if session == as_of else D("10")


def _alpha_basis(key, session, as_of):
    return D("11") if session == as_of else D("10")


def _beta_basis(key, session, as_of):
    return D("13") if session == as_of else D("10")


def _partial_basis(scale, key, session, as_of):
    return D(10 * scale) if session == as_of else D("10")


def _scale_factory(scale):
    def basis(key, session, as_of):
        return D(10 * scale) if session == as_of else D("10")
    return basis


class _StatefulBasis:
    def __init__(self, bump):
        self.bump = bump

    def __call__(self, key, session, as_of):
        return D("10") + self.bump if session == as_of else D("10")


class _SlottedBasisHolder:
    __slots__ = ("as_of_price",)

    def __init__(self, as_of_price):
        self.as_of_price = as_of_price

    def price(self, key, session, as_of):
        return self.as_of_price if session == as_of else D("10")


class _OpaqueStateBasis:
    def __init__(self, opaque):
        self.opaque = opaque
        self.calls = []

    def __call__(self, key, session, as_of):
        self.calls.append((key, session, as_of))
        return D("10")


class _ClassStateBasisHolder:
    as_of_price = D("11")

    def price(self, key, session, as_of):
        return type(self).as_of_price if session == as_of else D("10")


def bar(key, session, close="10", valid=True, symbol=None):
    c = D(close) if valid and close is not None else None
    return Bar(key, symbol or key, "EQ", session, c, c, c, c, 100 if valid else 0, valid,
               "INE000000001", D("1000") if valid else None, 5)


def build(patterns, sessions):
    """patterns maps key -> per-session None (no bar), close string (valid) or (close, valid)."""
    stream = SessionStream()
    ledger = ObservationLedger(stream)
    for i, session in enumerate(sessions):
        stream.observe(session)
        bars = []
        for key, entries in sorted(patterns.items()):
            entry = entries[i]
            if entry is None:
                continue
            close, valid = entry if isinstance(entry, tuple) else (entry, True)
            bars.append(bar(key, session, close, valid=valid))
        ledger.ingest(session, bars)
    return ledger


def window_ledger(case):
    valid = set(case["valid_indices"])
    closes = {int(index): close for index, close in case["closes"].items()}
    entries = [closes.get(i, "100") if i in valid else None for i in range(len(TEN))]
    return build({"AAA": entries}, TEN)


def window_spec(case):
    return MomentumSpec(lookback_sessions=case["lookback_sessions"],
                        min_observations=case["min_observations"], price_basis="close")


def generated_sessions(count, start=date(2024, 1, 1)):
    return tuple(start + timedelta(days=i) for i in range(count))


def two_session_ledger():
    return build({"K": ("10", "11")}, DAYS[:2])


# --- T2.1/T2.2: window-scoped observation counts -------------------------------


def test_min_observations_counts_window_not_history():
    case = GOLDEN["window_cases"]["window_short_of_minimum"]
    ledger = window_ledger(case)
    ranks = rank_momentum(ledger, TEN[9], ("AAA",), window_spec(case))
    counts = ranks.count_for("AAA")
    assert (ranks.as_of, ranks.anchor) == (TEN[9], TEN[case["anchor_index"]])
    assert ranks.window_sessions == counts.window_sessions == case["window_sessions"]
    assert counts.window_valid_observations == case["window_valid_observations"]
    assert counts.valid_observations == case["window_valid_observations"]
    assert counts.history_valid_observations == case["history_valid_observations"]
    assert ledger.valid_count("AAA", TEN[9]) == case["history_valid_observations"]
    assert case["window_valid_observations"] < case["history_valid_observations"]
    assert ranks.ranked == ()
    assert ranks.order == ()
    assert ranks.unrankable == (("AAA", case["reason"]),)
    assert case["reason"] == "insufficient_window_observations"
    assert "_censored" not in ranks.unrankable[0][1]


def test_exact_window_minimum_and_none_full_window():
    ranked_case = GOLDEN["window_cases"]["exact_minimum_ranked"]
    ranks = rank_momentum(window_ledger(ranked_case), TEN[9], ("AAA",), window_spec(ranked_case))
    assert ranks.order == ("AAA",)
    assert ranks.ranked[0].momentum == Fraction(ranked_case["momentum"])
    assert ranks.ranked[0].price == D(ranked_case["as_of_price"])
    assert ranks.ranked[0].anchor_price == D(ranked_case["anchor_price"])
    assert ranks.ranked[0].valid_observations == ranked_case["window_valid_observations"] == 3
    assert ranks.ranked[0].history_valid_observations == ranked_case["history_valid_observations"] == 9
    assert ranks.count_for("AAA").window_valid_observations == 3
    assert ranks.count_for("AAA").history_valid_observations == 9

    short_case = GOLDEN["window_cases"]["none_requires_full_window"]
    short = rank_momentum(window_ledger(short_case), TEN[9], ("AAA",), window_spec(short_case))
    assert short.ranked == ()
    assert short.unrankable == (("AAA", short_case["reason"]),)
    assert short.count_for("AAA").window_valid_observations == short_case["window_valid_observations"] == 3
    assert short.count_for("AAA").history_valid_observations == short_case["history_valid_observations"] == 9

    full_case = GOLDEN["window_cases"]["none_with_full_window"]
    full = rank_momentum(window_ledger(full_case), TEN[9], ("AAA",), window_spec(full_case))
    assert full.order == ("AAA",)
    assert full.ranked[0].momentum == Fraction(full_case["momentum"]) == Fraction(1, 10)
    assert full.count_for("AAA").window_valid_observations == full_case["window_valid_observations"] == 4

    bounds = GOLDEN["schema"]["range_case"]
    for value in bounds["valid"]:
        MomentumSpec(lookback_sessions=bounds["lookback_sessions"], min_observations=value)
    for value in bounds["invalid"]:
        with pytest.raises(ValueError, match="min_observations"):
            MomentumSpec(lookback_sessions=bounds["lookback_sessions"], min_observations=value)


# --- T2.3: exact anchor and schema 2 -------------------------------------------


def test_anchor_is_exact_and_schema_two_only():
    case = GOLDEN["anchor_cases"]["invalid_anchor_not_substituted"]
    sessions = generated_sessions(case["session_count"], date.fromisoformat(case["session_start"]))
    entries = []
    for i in range(case["session_count"]):
        if i in case["absent_indices"]:
            entries.append(None)
        elif i in case["invalid_indices"]:
            entries.append(("100", False))
        elif i == case["as_of_index"]:
            entries.append("110")
        else:
            entries.append("100")
    ranks = rank_momentum(build({"AAA": entries}, sessions), sessions[case["as_of_index"]],
                          ("AAA",), window_spec(case))
    assert ranks.anchor == sessions[case["anchor_index"]]
    assert ranks.anchor != sessions[case["anchor_index"] - 1]
    assert ranks.ranked == ()
    assert ranks.unrankable == (("AAA", case["reason"]),)
    assert case["reason"] == "no_valid_bar_on_anchor"
    assert ranks.count_for("AAA").window_valid_observations == case["window_valid_observations"]
    assert ranks.count_for("AAA").history_valid_observations == case["history_valid_observations"]

    extension = case["extension"]
    extra = extension["extra_leading_sessions"]
    extended_sessions = generated_sessions(extension["session_count"], date.fromisoformat(case["session_start"]) - timedelta(days=extra))
    extended = []
    for i in range(extension["session_count"]):
        original = i - extra
        if original in case["absent_indices"]:
            extended.append(None)
        elif original in case["invalid_indices"]:
            extended.append(("100", False))
        elif original == case["as_of_index"]:
            extended.append("110")
        else:
            extended.append("100")
    widened = rank_momentum(build({"AAA": extended}, extended_sessions),
                            extended_sessions[extension["as_of_index"]], ("AAA",), window_spec(case))
    assert widened.anchor == extended_sessions[extension["anchor_index"]]
    assert widened.unrankable == ranks.unrankable == (("AAA", case["reason"]),)
    assert widened.ranked == ()

    missing_case = GOLDEN["anchor_cases"]["no_anchor_session"]
    short_sessions = generated_sessions(missing_case["session_count"],
                                        date.fromisoformat(missing_case["session_start"]))
    short = build({"AAA": ["100"] * missing_case["session_count"]}, short_sessions)
    no_anchor = rank_momentum(short, short_sessions[missing_case["as_of_index"]], ("AAA",),
                              window_spec(missing_case))
    assert no_anchor.anchor is None
    assert no_anchor.window_sessions == missing_case["window_sessions"]
    assert no_anchor.ranked == ()
    assert no_anchor.unrankable == (("AAA", missing_case["reason"]),)
    assert missing_case["reason"] == "no_anchor_session"

    schema = GOLDEN["schema"]
    assert MomentumSpec().schema_version == schema["current_version"] == 2
    with pytest.raises(ValueError) as raised:
        MomentumSpec.from_json(schema["v1_payload"])
    message = str(raised.value)
    assert schema["migration_error_token"] in message
    for fragment in schema["migration_message_contains"]:
        assert fragment in message.lower()
    bounds = schema["range_case"]
    with pytest.raises(ValueError, match="min_observations"):
        MomentumSpec(lookback_sessions=bounds["lookback_sessions"], min_observations=bounds["invalid"][-1])


# --- T2.4: single-value dispatch and deterministic tie order --------------------


RANK_SESSIONS = {
    "AAA": ("100", "100", "105", "110"),
    "BBB": ("50", "50", "55", "60"),
    "CCC": ("100", "100", "100", "100"),
    "DDD": ("100", "100", "95", "90"),
    "EEE": (None, "200", "205", "210"),
    "TIE_A": ("150", "160", "170", "180"),
    "TIE_Z": ("75", "80", "85", "90"),
}
ELIGIBLE = ("DDD", "TIE_Z", "AAA", "BBB", "TIE_A", "CCC", "EEE")


def four_session_ledger(patterns=None):
    source = patterns or RANK_SESSIONS
    return build({key: entries for key, entries in source.items()}, DAYS[:4])


def momentum_spec(lookback=2, min_observations=3):
    return MomentumSpec(lookback_sessions=lookback, price_basis="close",
                        min_observations=min_observations)


def test_momentum_controls_are_implemented_single_value():
    dispatch = GOLDEN["dispatch"]
    defaults = MomentumSpec()
    assert defaults.anchor_rule == dispatch["anchor_rule"]["accepted"] == "exact_session_offset"
    assert defaults.observation_rule == dispatch["observation_rule"]["accepted"] == "valid_observations"
    assert defaults.tie_break == dispatch["tie_break"]["accepted"] == "key_asc"

    ledger = four_session_ledger()
    ranks = rank_momentum(ledger, DAYS[3], ELIGIBLE, momentum_spec())
    assert ranks.anchor == DAYS[1]  # exact-session-offset dispatch
    assert ranks.count_for("AAA").window_sessions == 3  # window-count dispatch

    tie = dispatch["tie_vector"]
    forward = rank_momentum(ledger, DAYS[3], (tie["keys"][0], tie["keys"][1]), momentum_spec())
    backward = rank_momentum(ledger, DAYS[3], tuple(reversed(tie["keys"])), momentum_spec())
    assert forward == backward
    assert forward.order == tuple(tie["order"]) == ("TIE_A", "TIE_Z")
    assert [r.momentum for r in forward.ranked] == [Fraction(tie["momentum"])] * 2

    for field in ("anchor_rule", "observation_rule", "tie_break"):
        expected = dispatch[field]
        assert expected["field"] == field
        with pytest.raises(ValueError, match=field):
            MomentumSpec(**{field: expected["second"]})
        injected = momentum_spec()
        object.__setattr__(injected, field, expected["second"])
        with pytest.raises(ValueError, match=field):
            rank_momentum(ledger, DAYS[3], ("AAA",), injected)


# --- T2.16-T2.18: basis provenance --------------------------------------------


def _expected_record(parts):
    return {name: parts[name] for name in
            ("kind", "basis_id", "name", "version", "source_digest", "parameters", "price_basis")}


def test_callable_basis_provenance_is_identified():
    identity = GOLDEN["basis_identity"]
    builtin = identity["builtin_close"]
    ledger = two_session_ledger()
    ranks = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2))
    provenance = ranks.basis_provenance
    assert isinstance(provenance, signals_module.BasisProvenance)
    assert provenance == signals_module.BasisProvenance(
        kind=builtin["kind"], basis_id=None, name=builtin["name"], version=None,
        source_digest=None, parameters={})
    assert provenance.price_basis == ranks.price_basis == builtin["price_basis"] == "close"
    assert provenance.basis_id is None and provenance.source_digest is None
    assert dict(provenance.parameters) == builtin["parameters"] == {}

    custom = identity["callable_custom_basis"]
    callable_ranks = rank_momentum(
        ledger, DAYS[1], ("K",), momentum_spec(1, 2), custom_basis,
        basis_id=custom["basis_id"], basis_version=custom["version"],
        basis_parameters=custom["parameters"])
    record = callable_ranks.basis_provenance
    assert record.name == custom["name"] == f"{custom_basis.__module__}.{custom_basis.__qualname__}"
    assert record.source_digest == custom["source_digest"]
    assert record.kind == custom["kind"] == "callable"
    assert record.basis_id == custom["basis_id"] == "custom_basis_v1"
    assert record.version == custom["version"] == "1.0"
    assert record.reproducible is custom["reproducible"] is True
    assert dict(record.parameters) == custom["parameters"] == {}
    with pytest.raises(TypeError):
        record.parameters["not_allowed"] = 1
    assert callable_ranks.price_basis == custom["price_basis"] == "custom:custom_basis_v1"
    assert callable_ranks.price_basis not in ("close", "custom", custom_basis.__name__)
    assert _expected_record({**record.__dict__, "price_basis": record.price_basis}) == _expected_record(custom)

    again = rank_momentum(
        ledger, DAYS[1], ("K",), momentum_spec(1, 2), custom_basis,
        basis_id=custom["basis_id"], basis_version=custom["version"],
        basis_parameters=custom["parameters"])
    assert again.basis_provenance == record

    expected = _expected_record(custom)
    code = (
        "import json;"
        "from test_accounting_signals import custom_basis;"
        "from nse_pipeline.backtest.signals import basis_provenance;"
        "from nse_pipeline.specs import MomentumSpec;"
        "p=basis_provenance(MomentumSpec(lookback_sessions=1,min_observations=2),custom_basis,"
        "basis_id='custom_basis_v1',basis_version='1.0',basis_parameters={});"
        "print(json.dumps({'kind':p.kind,'basis_id':p.basis_id,'name':p.name,'version':p.version,"
        "'source_digest':p.source_digest,'parameters':dict(p.parameters),"
        "'price_basis':p.price_basis},sort_keys=True))"
    )
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), str(Path(__file__).parent), environment.get("PYTHONPATH", "")])
    completed = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent,
                               capture_output=True, text=True, env=environment, timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == expected


def test_global_dependent_named_callable_provenance_tracks_runtime_global_value():
    ledger = two_session_ledger()
    basis_low = types.FunctionType(
        _global_multiplier_basis.__code__,
        {**_global_multiplier_basis.__globals__, "_PROVENANCE_GLOBAL_MULTIPLIER": 11},
        _global_multiplier_basis.__name__)
    basis_high = types.FunctionType(
        _global_multiplier_basis.__code__,
        {**_global_multiplier_basis.__globals__, "_PROVENANCE_GLOBAL_MULTIPLIER": 12},
        _global_multiplier_basis.__name__)

    low = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2), basis_low)
    high = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2), basis_high)

    assert low.ranked[0].price != high.ranked[0].price
    assert low.basis_provenance.source_digest != high.basis_provenance.source_digest
    assert low.basis_provenance != high.basis_provenance
    assert low.basis_provenance.reproducible and high.basis_provenance.reproducible


def test_unsafe_global_callable_fails_closed():
    # The former attested escape hatch could not prove that live global state matched
    # the caller's declaration, so mutable global dependencies now fail closed even
    # when a full attestation is supplied.
    spec = momentum_spec(1, 2)
    with pytest.raises(ValueError, match="rank_admitted_momentum") as unpinned:
        signals_module.basis_provenance(spec, _global_mapping_basis)
    assert "callable_basis_state_not_freezable" in str(unpinned.value)
    with pytest.raises(ValueError, match="rank_formula_momentum"):
        signals_module.basis_provenance(spec, _global_mapping_basis, basis_id="map_basis_v1",
                                        basis_parameters={"mapping_version": "v1"})
    with pytest.raises(ValueError, match="rank_admitted_momentum"):
        rank_momentum(two_session_ledger(), DAYS[1], ("K",), spec, _global_mapping_basis,
                      basis_id="map_basis_v1", basis_parameters={"mapping_version": "v1"})


def test_mutable_module_callable_fails_closed():
    with pytest.raises(ValueError, match="rank_admitted_momentum"):
        signals_module.basis_provenance(momentum_spec(1, 2), _mutable_module_basis)


def test_partial_wrapped_unsafe_global_fails_closed():
    spec = momentum_spec(1, 2)
    mapping = functools.partial(_global_mapping_basis)
    nested = functools.partial(functools.partial(_global_mapping_basis))
    for basis in (mapping, nested):
        with pytest.raises(ValueError, match="rank_admitted_momentum"):
            signals_module.basis_provenance(spec, basis, basis_id="map_partial_v1",
                                            basis_parameters={"mapping_version": "v1"})
        with pytest.raises(ValueError, match="rank_formula_momentum"):
            rank_momentum(two_session_ledger(), DAYS[1], ("K",), spec, basis,
                          basis_id="map_partial_v1",
                          basis_parameters={"mapping_version": "v1"})


def test_partial_wrapped_scalar_global_is_fingerprinted():
    spec = momentum_spec(1, 2)
    multiplier = functools.partial(_global_multiplier_basis)
    with pytest.raises(ValueError, match="basis_id_required"):
        signals_module.basis_provenance(spec, multiplier)
    record = signals_module.basis_provenance(spec, multiplier, basis_id="mult_partial_v1")
    assert record.name == "test_accounting_signals._global_multiplier_basis"
    assert record.source_digest is not None and len(record.source_digest) == 64
    assert record.reproducible is True


def test_partial_arguments_and_keywords_differentiate_provenance():
    spec = momentum_spec(1, 2)
    positional_two = signals_module.basis_provenance(
        spec, functools.partial(_partial_basis, 2), basis_id="partial")
    positional_three = signals_module.basis_provenance(
        spec, functools.partial(_partial_basis, 3), basis_id="partial")
    keyword_two = signals_module.basis_provenance(
        spec, functools.partial(_partial_basis, scale=2), basis_id="partial")
    keyword_three = signals_module.basis_provenance(
        spec, functools.partial(_partial_basis, scale=3), basis_id="partial")
    repeated = signals_module.basis_provenance(
        spec, functools.partial(_partial_basis, 2), basis_id="partial")

    assert positional_two.source_digest != positional_three.source_digest
    assert keyword_two.source_digest != keyword_three.source_digest
    assert positional_two.source_digest != keyword_two.source_digest
    assert positional_two == repeated
    assert positional_two.source_digest == repeated.source_digest


def test_partial_wrapped_safe_function_is_admitted_without_parameters():
    spec = momentum_spec(1, 2)
    ledger = two_session_ledger()
    basis = functools.partial(_partial_basis, 2)
    with pytest.raises(ValueError, match="basis_id_required"):
        rank_momentum(ledger, DAYS[1], ("K",), spec, basis)
    record = signals_module.basis_provenance(spec, basis, basis_id="partial_scale_2")
    assert record.kind == "callable"
    assert record.basis_id == "partial_scale_2"
    assert record.name == "test_accounting_signals._partial_basis"
    assert dict(record.parameters) == {}
    assert record.source_digest is not None and len(record.source_digest) == 64
    assert record.reproducible is True
    ranks = rank_momentum(ledger, DAYS[1], ("K",), spec, basis, basis_id="partial_scale_2")
    assert ranks.ranked[0].momentum == Fraction(1)
    assert ranks.basis_provenance == record


def test_bound_method_provenance_fingerprints_live_state():
    spec = momentum_spec(1, 2)
    ledger = two_session_ledger()
    basis = _MethodBasisHolder(D("11")).price
    with pytest.raises(ValueError, match="basis_id_required"):
        signals_module.basis_provenance(spec, basis)
    record = signals_module.basis_provenance(spec, basis, basis_id="method_basis_v1")
    assert record.kind == "callable"
    assert record.name == f"{_MethodBasisHolder.__module__}._MethodBasisHolder.price"
    assert record.source_digest is not None and len(record.source_digest) == 64
    assert record.reproducible is True
    same_state = signals_module.basis_provenance(
        spec, _MethodBasisHolder(D("11")).price, basis_id="method_basis_v1")
    different_state = signals_module.basis_provenance(
        spec, _MethodBasisHolder(D("12")).price, basis_id="method_basis_v1")
    assert same_state == record
    assert different_state != record
    assert different_state.source_digest != record.source_digest
    ranks = rank_momentum(ledger, DAYS[1], ("K",), spec, basis, basis_id="method_basis_v1")
    assert ranks.ranked[0].momentum == Fraction(1, 10)
    assert ranks.basis_provenance == record
    attested = signals_module.basis_provenance(
        spec, basis, basis_id="method_basis_v1", basis_parameters={"holder": "v1"})
    assert dict(attested.parameters) == {"holder": "v1"}
    assert attested != record


def test_slots_instance_state_is_fingerprinted():
    spec = momentum_spec(1, 2)
    low = signals_module.basis_provenance(
        spec, _SlottedBasisHolder(D("11")).price, basis_id="slotted")
    high = signals_module.basis_provenance(
        spec, _SlottedBasisHolder(D("12")).price, basis_id="slotted")
    same = signals_module.basis_provenance(
        spec, _SlottedBasisHolder(D("11")).price, basis_id="slotted")
    assert low != high
    assert low.source_digest != high.source_digest
    assert same == low


def test_class_level_state_is_fingerprinted_for_bound_methods():
    spec = momentum_spec(1, 2)
    original = _ClassStateBasisHolder.as_of_price
    try:
        _ClassStateBasisHolder.as_of_price = D("11")
        low = signals_module.basis_provenance(
            spec, _ClassStateBasisHolder().price, basis_id="class_state")
        _ClassStateBasisHolder.as_of_price = D("12")
        high = signals_module.basis_provenance(
            spec, _ClassStateBasisHolder().price, basis_id="class_state")
        _ClassStateBasisHolder.as_of_price = D("11")
        same = signals_module.basis_provenance(
            spec, _ClassStateBasisHolder().price, basis_id="class_state")
    finally:
        _ClassStateBasisHolder.as_of_price = original
    assert low != high
    assert low.source_digest != high.source_digest
    assert same == low


def test_closure_values_differentiate_provenance():
    spec = momentum_spec(1, 2)
    low = signals_module.basis_provenance(spec, _scale_factory(2), basis_id="closure")
    high = signals_module.basis_provenance(spec, _scale_factory(3), basis_id="closure")
    same = signals_module.basis_provenance(spec, _scale_factory(2), basis_id="closure")
    assert low.name == high.name
    assert low != high
    assert low.source_digest != high.source_digest
    assert same == low


def test_callable_object_live_state_differentiates_provenance():
    spec = momentum_spec(1, 2)
    low = signals_module.basis_provenance(spec, _StatefulBasis(D("1")), basis_id="stateful")
    high = signals_module.basis_provenance(spec, _StatefulBasis(D("2")), basis_id="stateful")
    same = signals_module.basis_provenance(spec, _StatefulBasis(D("1")), basis_id="stateful")
    assert low != high
    assert low.source_digest != high.source_digest
    assert same == low


def test_opaque_live_state_is_rejected_before_any_bar_is_read():
    spec = momentum_spec(1, 2)
    ledger = two_session_ledger()
    opaque_values = (object(), (value for value in (1,)), types.ModuleType("opaque"),
                     io.BytesIO(b"opaque"))
    for opaque in opaque_values:
        basis = _OpaqueStateBasis(opaque)
        with pytest.raises(ValueError, match="rank_admitted_momentum") as raised:
            rank_momentum(ledger, DAYS[1], ("K",), spec, basis, basis_id="opaque")
        message = str(raised.value)
        assert "callable_basis_state_not_freezable" in message
        assert "rank_formula_momentum" in message
        assert basis.calls == []


def test_state_mutation_during_ranking_cannot_publish_stale_provenance():
    class _MutatingBasis:
        def __init__(self):
            self.calls = 0

        def __call__(self, key, session, as_of):
            self.calls += 1
            return D("10") + self.calls

    basis = _MutatingBasis()
    with pytest.raises(ValueError, match="callable_basis_state_changed_during_ranking"):
        rank_momentum(two_session_ledger(), DAYS[1], ("K",), momentum_spec(1, 2),
                      basis, basis_id="mutating_basis")
    assert basis.calls == 2


def test_dynamic_global_lookup_is_rejected_as_unfreezable():
    # globals()["NAME"] is an arbitrary mutable global even though no direct
    # LOAD_GLOBAL points at NAME, so the legacy path must not publish a digest.
    spec = momentum_spec(1, 2)
    with pytest.raises(ValueError, match="dynamic namespace access"):
        signals_module.basis_provenance(spec, _dynamic_lookup_basis)
    with pytest.raises(ValueError, match="rank_admitted_momentum"):
        rank_momentum(two_session_ledger(), DAYS[1], ("K",), spec, _dynamic_lookup_basis,
                      basis_id="dynamic_lookup")


def test_same_name_callables_and_parameterized_transforms_are_distinct():
    identity = GOLDEN["basis_identity"]
    same_name = identity["same_name_different_modules"]
    ledger = two_session_ledger()
    records = []
    momenta = []
    for row, source in zip(same_name["rows"], (_alpha_basis, _beta_basis)):
        clone = types.FunctionType(source.__code__, source.__globals__, "basis")
        clone.__module__ = row["module"]
        clone.__qualname__ = "basis"
        assert clone.__name__ == "basis"
        ranks = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2), clone,
                              basis_id=row["basis_id"])
        record = ranks.basis_provenance
        assert record.name == row["name"]
        assert record.price_basis == row["price_basis"] == ranks.price_basis
        assert record.price_basis not in ("basis", "custom", "close")
        assert ranks.ranked[0].momentum == Fraction(row["momentum"])
        assert record.source_digest is not None and len(record.source_digest) == 64
        records.append(record)
        momenta.append(ranks.ranked[0].momentum)
    assert records[0] != records[1]
    assert records[0].source_digest != records[1].source_digest
    assert momenta == [Fraction(row["momentum"]) for row in same_name["rows"]]
    assert same_name["digests_differ"] is True

    closures = identity["parameterized_closures"]
    closure_records = []
    closure_momenta = []
    for row in closures["rows"]:
        basis = _scale_factory(row["parameters"]["scale"])
        ranks = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2), basis,
                              basis_id=row["basis_id"], basis_parameters=row["parameters"])
        record = ranks.basis_provenance
        assert record.basis_id == row["basis_id"]
        assert dict(record.parameters) == row["parameters"]
        assert record.price_basis == row["price_basis"] == ranks.price_basis
        assert record.source_digest is not None
        assert ranks.ranked[0].momentum == Fraction(row["momentum"])
        closure_records.append(record)
        closure_momenta.append(ranks.ranked[0].momentum)
    assert closure_records[0].name == closure_records[1].name
    # Live closure values are now part of the digest. The golden fixture's historical
    # shared_source_digest field described the superseded source-only rule.
    assert closure_records[0].source_digest != closure_records[1].source_digest
    assert closure_records[0].basis_id != closure_records[1].basis_id
    assert closure_records[0].parameters != closure_records[1].parameters
    assert closure_records[0] != closure_records[1]
    assert closure_momenta == [Fraction(row["momentum"]) for row in closures["rows"]]
    assert closures["shared_name"] is True


def test_callable_without_stable_identity_requires_basis_id():
    identity = GOLDEN["basis_identity"]
    unstable = identity["unstable_identity"]
    ledger = two_session_ledger()
    closure = _scale_factory(2)
    stateful = _StatefulBasis(1)
    callables = {
        "lambda": (lambda key, session, as_of: D("10") + (1 if session == as_of else 0)),
        "closure": closure,
        "partial": functools.partial(_partial_basis, 2),
        "stateful": stateful,
    }
    for row in unstable["rows"]:
        basis = callables[row["kind"]]
        with pytest.raises(ValueError, match="basis_id_required"):
            rank_momentum(ledger, DAYS[1], (), momentum_spec(1, 2), basis)
        ranks = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2), basis,
                              basis_id=row["basis_id"])
        record = ranks.basis_provenance
        assert record.kind == "callable"
        assert record.basis_id == row["basis_id"]
        assert ranks.price_basis == f"custom:{row['basis_id']}"
        # All four formerly unstable kinds now carry frozen live state, so each
        # admitted legacy callable has a reproducible digest. The historical golden
        # fields source_digest_is_none/reproducible record the superseded rule.
        assert record.source_digest is not None
        assert len(record.source_digest) == 64
        assert record.reproducible is True

    derived = identity["derived_named_function"]
    control = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2), module_level_basis)
    record = control.basis_provenance
    assert record.basis_id == derived["basis_id"] == "test_accounting_signals.module_level_basis"
    assert record.name == derived["name"]
    assert record.source_digest == derived["source_digest"]
    assert record.version is None
    assert control.price_basis == derived["price_basis"]


def test_callable_provenance_uses_custom_basis_id_never_the_spec_label():
    golden = GOLDEN["basis_identity"]["callable_custom_basis"]
    ledger = two_session_ledger()
    spec = momentum_spec(1, 2)
    object.__setattr__(spec, "price_basis", "bogus_basis")
    ranks = rank_momentum(ledger, DAYS[1], ("K",), spec, custom_basis,
                          basis_id=golden["basis_id"])
    assert ranks.basis_provenance.kind == "callable"
    assert ranks.price_basis == golden["price_basis"] == "custom:custom_basis_v1"
    assert ranks.price_basis != "bogus_basis"
    assert ranks.price_basis != "close"
    assert ranks.price_basis != "custom"


# --- Causal as-of properties and preserved signal behavior ---------------------


def test_momentum_is_an_exact_fraction_and_ranks_descending():
    ranks = rank_momentum(four_session_ledger(), DAYS[3], ELIGIBLE, momentum_spec())
    assert isinstance(ranks, MomentumRanks)
    assert (ranks.as_of, ranks.anchor, ranks.price_basis) == (DAYS[3], DAYS[1], "close")
    assert ranks.order == ("BBB", "TIE_A", "TIE_Z", "AAA", "EEE", "CCC", "DDD")
    assert [r.momentum for r in ranks.ranked] == [Fraction(1, 5), Fraction(1, 8), Fraction(1, 8),
                                                 Fraction(1, 10), Fraction(1, 20), Fraction(0),
                                                 Fraction(-1, 10)]
    assert [(r.key, r.price, r.anchor_price) for r in ranks.ranked] == [
        ("BBB", D("60"), D("50")), ("TIE_A", D("180"), D("160")), ("TIE_Z", D("90"), D("80")),
        ("AAA", D("110"), D("100")), ("EEE", D("210"), D("200")), ("CCC", D("100"), D("100")),
        ("DDD", D("90"), D("100"))]
    assert ranks.unrankable == ()


def test_every_unrankable_key_carries_a_window_reason():
    patterns = {
        "ANCHOR_INVALID": ("100", ("100", False), "100", "110"),
        "CENSORED_THIN": ("100", "100", ("100", False), "110"),
        "INVALID": ("100", "100", "100", ("110", False)),
        "MISSING_ANCHOR": ("100", None, "100", "110"),
        "NO_BAR": ("100", "100", "100", None),
        "OK": ("100", "100", "100", "110"),
        "THIN": (None, "100", ("100", False), "110"),
    }
    ranks = rank_momentum(four_session_ledger(patterns), DAYS[3], tuple(sorted(patterns)),
                          momentum_spec(min_observations=3))
    assert ranks.order == ("OK",)
    assert ranks.unrankable == (
        ("ANCHOR_INVALID", "no_valid_bar_on_anchor"),
        ("CENSORED_THIN", "insufficient_window_observations"),
        ("INVALID", "no_valid_bar_on_session"),
        ("MISSING_ANCHOR", "no_valid_bar_on_anchor"),
        ("NO_BAR", "no_valid_bar_on_session"),
        ("THIN", "insufficient_window_observations"))
    assert ranks.ranked[0].valid_observations == 3
    assert ranks.ranked[0].history_valid_observations == 4
    assert ranks.ranked[0].observations_lower_bound is True


def test_invalid_bars_are_excluded_from_the_window_count():
    ranks = rank_momentum(four_session_ledger({"MIXED": ("100", "100", ("100", False), "110")}),
                          DAYS[3], ("MIXED",), momentum_spec(min_observations=2))
    assert ranks.order == ("MIXED",)
    assert ranks.ranked[0].valid_observations == 2
    assert ranks.ranked[0].history_valid_observations == 3
    short = rank_momentum(four_session_ledger({"MIXED": ("100", "100", ("100", False), "110")}),
                          DAYS[3], ("MIXED",), momentum_spec(min_observations=3))
    assert short.order == ()
    assert short.unrankable == (("MIXED", "insufficient_window_observations"),)


def test_a_key_without_an_anchor_session_is_unrankable():
    ranks = rank_momentum(four_session_ledger(), DAYS[1], ("AAA", "BBB"), momentum_spec())
    assert ranks.anchor is None
    assert ranks.ranked == ()
    assert ranks.unrankable == (("AAA", "no_anchor_session"), ("BBB", "no_anchor_session"))


def test_a_caller_supplied_basis_reports_unavailable_and_nonpositive_prices():
    patterns = {key: ("100", "100", "100", "100")
                for key in ("FINE", "NO_ANCHOR", "NO_SESSION", "ZERO")}
    ledger = four_session_ledger(patterns)

    def basis(key, session, as_of):
        if key == "NO_ANCHOR" and session == DAYS[1]:
            return None
        if key == "NO_SESSION" and session == as_of:
            return None
        if key == "ZERO" and session == as_of:
            return D("0")
        return D("100")

    ranks = rank_momentum(ledger, DAYS[3], ("NO_ANCHOR", "NO_SESSION", "ZERO", "FINE"),
                          momentum_spec(), basis, basis_id="scenario_basis_v1")
    assert ranks.order == ("FINE",)
    assert ranks.ranked[0].momentum == Fraction(0)
    assert ranks.unrankable == (
        ("NO_ANCHOR", "price_unavailable_on_anchor"),
        ("NO_SESSION", "price_unavailable_on_session"),
        ("ZERO", "nonpositive_price"))


def test_the_basis_is_only_asked_for_sessions_up_to_as_of(capsys):
    ledger = four_session_ledger()

    # Instrumentation must not mutate the callable's fingerprinted live state;
    # stdout is outside the frozen graph.
    def basis(key, session, as_of):
        print(key, session.isoformat(), as_of.isoformat())
        return D("100")

    rank_momentum(ledger, DAYS[3], ("AAA",), momentum_spec(), basis, basis_id="asked_basis_v1")
    assert capsys.readouterr().out.splitlines() == [
        f"AAA {DAYS[3].isoformat()} {DAYS[3].isoformat()}",
        f"AAA {DAYS[1].isoformat()} {DAYS[3].isoformat()}",
    ]


def test_close_basis_refuses_a_price_after_the_decision_session():
    ledger = build({"OK": ("100", "100", "100", "110"), "NO_BAR": ("100", "100", "100", None),
                    "INVALID": ("100", "100", "100", ("110", False))}, DAYS[:4])
    basis = close_basis(ledger)
    assert basis("OK", DAYS[1], DAYS[3]) == D("100")
    assert basis("OK", DAYS[3], DAYS[3]) == D("110")
    assert basis("NO_BAR", DAYS[3], DAYS[3]) is None
    assert basis("INVALID", DAYS[3], DAYS[3]) is None
    with pytest.raises(ValueError, match="look-ahead"):
        basis("OK", DAYS[3], DAYS[1])
    with pytest.raises(ValueError):
        basis("OK", UNOBSERVED, DAYS[3])


def test_rank_as_of_is_append_invariant():
    case = GOLDEN["window_cases"]["exact_minimum_ranked"]
    ledger = window_ledger(case)
    before_nine = rank_momentum(ledger, TEN[9], ("AAA",), window_spec(case))
    before_seven = rank_momentum(ledger, TEN[7], ("AAA",), window_spec(case))
    future = TEN[9] + timedelta(days=1)
    for i in range(3):
        session = future + timedelta(days=i)
        ledger.stream.observe(session)
        ledger.ingest(session, [bar("AAA", session, "999"), bar("LATE", session, "1")])
    assert rank_momentum(ledger, TEN[9], ("AAA",), window_spec(case)) == before_nine
    assert rank_momentum(ledger, TEN[7], ("AAA",), window_spec(case)) == before_seven


@pytest.mark.parametrize("call", (
    lambda ledger: rank_momentum(ledger, DAYS[3], "AAA", momentum_spec()),
    lambda ledger: rank_momentum(ledger, DAYS[3], ("AAA", "AAA"), momentum_spec()),
    lambda ledger: rank_momentum(ledger, DAYS[3], ("AAA",), momentum_spec(), "not callable"),
    lambda ledger: rank_momentum(ledger, DAYS[3], ("AAA",),
                                 MomentumSpec(lookback_sessions=2, min_observations=2,
                                              price_basis="adj_close")),
    lambda ledger: rank_momentum(None, DAYS[3], ("AAA",), momentum_spec()),
    lambda ledger: rank_momentum(ledger, DAYS[3], ("AAA",), object()),
    lambda ledger: rank_momentum(ledger, UNOBSERVED, ("AAA",), momentum_spec()),
    lambda ledger: rank_momentum(ledger, DAYS[3], ("AAA",), momentum_spec(),
                                 lambda k, s, a: 1.5, basis_id="bad_return"),
    lambda ledger: rank_momentum(ledger, DAYS[3], ("AAA",), momentum_spec(),
                                 lambda k, s, a: D("NaN"), basis_id="nan_return"),
))
def test_malformed_inputs_raise(call):
    with pytest.raises(ValueError):
        call(four_session_ledger())
