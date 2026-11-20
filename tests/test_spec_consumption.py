"""T3.6/T3.7: the D-16 four-state field-disposition registry is exact and bounded.

The registry is an independent research artefact: it names production readers and
behaviour tests without importing the concurrent sizing/validation modules. The
configuration row is a local gate, not contract evidence, so it stays
``CONTRACT_PARTIAL`` even when the disconnected count reaches zero.
"""
import pytest


# D-16's 27-field table, copied as an independent golden. The order follows the
# decision register; comparisons are keyed so a harmless reordering is not a failure.
D16_TABLE = (
    ("MomentumSpec", "anchor_rule", "IMPLEMENTED"),
    ("MomentumSpec", "observation_rule", "IMPLEMENTED"),
    ("MomentumSpec", "tie_break", "IMPLEMENTED"),
    ("ExecutionSpec", "fill_timing", "RESERVED"),
    ("ExecutionSpec", "max_participation_rate", "RESERVED"),
    ("ExecutionSpec", "settlement_funding", "IMPLEMENTED"),
    ("ExecutionSpec", "unfilled_remainder", "RESERVED"),
    ("ExecutionSpec", "circuit_limit_pct", "RESERVED"),
    ("ExecutionSpec", "hold_suspended_positions", "RESERVED"),
    ("ValuationPolicy", "dividend_cash_phase", "IMPLEMENTED"),
    ("ValuationPolicy", "series_precedence", "RESERVED"),
    ("ValuationPolicy", "carried_position_is", "RESERVED"),
    ("ValuationPolicy", "absence_review_sessions", "RESERVED"),
    ("ValuationPolicy", "sizing_uses_screen_value", "DISCONNECTED"),
    ("ValuationPolicy", "allow_scenario_statistics", "IMPLEMENTED"),
    ("CostSpec", "schedule_basis", "IMPLEMENTED"),
    ("CostSpec", "monetary_rounding", "IMPLEMENTED"),
    ("PortfolioSpec", "max_positions", "DISCONNECTED"),
    ("PortfolioSpec", "initial_cash", "RESERVED"),
    ("PortfolioSpec", "weighting", "RESERVED"),
    ("PortfolioSpec", "sizing_policy", "DISCONNECTED"),
    ("PortfolioSpec", "allocation_order", "IMPLEMENTED"),
    ("PortfolioSpec", "disposal_policy", "RESERVED"),
    ("PortfolioSpec", "scenario_only", "IMPLEMENTED"),
    ("PortfolioSpec", "rebalance_frequency", "DISCONNECTED"),
    ("PortfolioSpec", "rebalance_anchor", "DISCONNECTED"),
    ("PortfolioSpec", "trim_overweight_targets", "RESERVED"),
)

D16_KEYS = tuple((spec, field) for spec, field, _ in D16_TABLE)
D16_DISPOSITIONS = {(spec, field): state for spec, field, state in D16_TABLE}
DISCONNECTED_KEYS = frozenset(
    key for key, state in D16_DISPOSITIONS.items() if state == "DISCONNECTED"
)
WIRED_READERS = {
    ("ValuationPolicy", "sizing_uses_screen_value"): "resolve_sizing_policy",
    ("PortfolioSpec", "sizing_policy"): "resolve_sizing_policy",
    ("PortfolioSpec", "max_positions"): "size_portfolio_orders",
    ("PortfolioSpec", "rebalance_frequency"): "resolve_rebalance_schedule",
    ("PortfolioSpec", "rebalance_anchor"): "resolve_rebalance_schedule",
}


def _registry():
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS, CURRENT_DISPOSITIONS

    return BASELINE_DISPOSITIONS, CURRENT_DISPOSITIONS


def _by_key(entries):
    return {(entry.spec, entry.field): entry for entry in entries}


def test_field_disposition_registry_is_complete_and_exact():
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS, counts

    entries = tuple(BASELINE_DISPOSITIONS)
    observed = {(entry.spec, entry.field): entry.disposition for entry in entries}
    assert len(entries) == 27
    assert len(observed) == 27  # no duplicated (spec, field)
    assert observed == D16_DISPOSITIONS
    assert tuple(sorted(observed)) == tuple(sorted(D16_KEYS))
    assert counts(entries) == {
        "implemented": 10,
        "reserved": 12,
        "obsolete": 0,
        "disconnected": 5,
    }
    with pytest.raises(ValueError):
        counts((*entries, entries[0]))  # a duplicated field is a registry failure


def test_configuration_row_fails_while_disconnected():
    from nse_pipeline.specs.consumption import (
        BASELINE_DISPOSITIONS,
        CURRENT_DISPOSITIONS,
        configuration_row,
        survey,
    )

    baseline = configuration_row(BASELINE_DISPOSITIONS, D16_KEYS)
    assert baseline["status"] == "FAIL"
    assert baseline["contract_status"] == "CONTRACT_PARTIAL"
    assert baseline["reason"] == "disconnected_fields=5"
    assert baseline["disconnected_fields"] == 5
    assert set(baseline["disconnected"]) == {
        f"{spec}.{field}" for spec, field in DISCONNECTED_KEYS
    }

    ghost = ("MomentumSpec", "ghost")
    report = survey(BASELINE_DISPOSITIONS, (*D16_KEYS, ghost))
    assert "MomentumSpec.ghost" in report["unregistered"]
    assert report["missing"] == []
    assert report["complete"] is False

    ghost_row = configuration_row(CURRENT_DISPOSITIONS, (*D16_KEYS, ghost))
    assert ghost_row["status"] == "FAIL"
    assert "MomentumSpec.ghost" in ghost_row["unregistered"]

    wired = configuration_row(CURRENT_DISPOSITIONS, D16_KEYS)
    assert wired["status"] == "PASS"
    assert wired["contract_status"] == "CONTRACT_PARTIAL"


def test_every_spec_field_has_one_of_four_disposition_states():
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS, DISPOSITION_STATES

    assert DISPOSITION_STATES == (
        "IMPLEMENTED",
        "RESERVED",
        "OBSOLETE",
        "DISCONNECTED",
    )
    seen = set()
    for entry in BASELINE_DISPOSITIONS:
        assert entry.disposition in DISPOSITION_STATES
        assert entry.key not in seen
        seen.add(entry.key)
    assert seen == set(D16_KEYS)


def test_disposition_counts_are_10_implemented_12_reserved_0_obsolete_5_disconnected():
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS, counts

    assert counts(BASELINE_DISPOSITIONS) == {
        "implemented": 10,
        "reserved": 12,
        "obsolete": 0,
        "disconnected": 5,
    }


def test_momentum_fields_are_implemented_not_reserved():
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS

    registry = _by_key(BASELINE_DISPOSITIONS)
    for field in ("anchor_rule", "observation_rule", "tie_break"):
        entry = registry[("MomentumSpec", field)]
        assert entry.disposition == "IMPLEMENTED"
        assert entry.disposition != "RESERVED"
        assert entry.reader
        assert "signals" in entry.reader
        assert entry.behaviour_test


def test_reserved_field_has_exactly_one_legal_value():
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS, FieldDisposition

    reserved = [entry for entry in BASELINE_DISPOSITIONS if entry.disposition == "RESERVED"]
    assert len(reserved) == 12
    for entry in reserved:
        assert entry.legal_value is not None
        assert not isinstance(entry.legal_value, (list, tuple, set, frozenset, dict))
        assert entry.contract and entry.owner and entry.rationale and entry.behaviour_test
        assert entry.reader is None

    # A reservation is a single value, not a bag of alternatives.
    with pytest.raises(ValueError):
        FieldDisposition(
            spec="SyntheticSpec",
            field="reserved",
            disposition="RESERVED",
            legal_value=("a", "b"),
            contract="SYNTH-RESERVED",
            owner="stage1-specs",
            rationale="two values is not a reservation",
            behaviour_test="test_reserved_field_has_exactly_one_legal_value",
        )


def test_implemented_field_has_a_production_reader_and_effect_test():
    from nse_pipeline.specs.consumption import (
        BASELINE_DISPOSITIONS,
        FieldDisposition,
        is_production_reader,
    )

    implemented = [
        entry for entry in BASELINE_DISPOSITIONS if entry.disposition == "IMPLEMENTED"
    ]
    assert len(implemented) == 10
    for entry in implemented:
        assert entry.reader
        assert is_production_reader(entry.reader)
        assert "tests/" not in entry.reader
        assert entry.behaviour_test
        assert entry.contract and entry.owner

    registry = _by_key(BASELINE_DISPOSITIONS)
    for field in ("settlement_funding",):
        assert "validate_run_specs" in registry[("ExecutionSpec", field)].reader
    for field in ("dividend_cash_phase",):
        assert "validate_run_specs" in registry[("ValuationPolicy", field)].reader
    for field in ("schedule_basis", "monetary_rounding"):
        assert "validate_run_specs" in registry[("CostSpec", field)].reader
    assert "size_portfolio_orders" in registry[
        ("PortfolioSpec", "allocation_order")
    ].reader
    assert "ValuationPolicy" in registry[
        ("ValuationPolicy", "allow_scenario_statistics")
    ].reader
    assert "PortfolioSpec" in registry[("PortfolioSpec", "scenario_only")].reader

    with pytest.raises(ValueError):
        FieldDisposition(
            spec="SyntheticSpec",
            field="implemented",
            disposition="IMPLEMENTED",
            reader="tests/test_synthetic.py::test_reader",
            behaviour_test="test_implemented_field_has_a_production_reader_and_effect_test",
        )


def test_disconnected_field_allowlist_is_bounded_and_blocks_configuration_pass():
    from nse_pipeline.specs.consumption import (
        BASELINE_DISPOSITIONS,
        FieldDisposition,
        configuration_row,
        disconnected_allowlist,
    )

    disconnected = [
        entry for entry in BASELINE_DISPOSITIONS if entry.disposition == "DISCONNECTED"
    ]
    assert {entry.key for entry in disconnected} == DISCONNECTED_KEYS
    assert len(disconnected) == 5
    for entry in disconnected:
        assert entry.counterpart
        assert entry.owner
        assert entry.issue
        assert entry.expiry

    allowlist = disconnected_allowlist(BASELINE_DISPOSITIONS)
    assert set(allowlist) == {f"{spec}.{field}" for spec, field in DISCONNECTED_KEYS}
    for record in allowlist.values():
        assert set(record) >= {"counterpart", "owner", "issue", "expiry"}

    # No DISCONNECTED entry may be constructed without its bounded allowlist data.
    with pytest.raises(ValueError):
        FieldDisposition(
            spec="SyntheticSpec",
            field="unbounded",
            disposition="DISCONNECTED",
            counterpart="size_portfolio_orders",
            owner="stage1-specs",
            issue="F14/SYNTH",
        )

    row = configuration_row(BASELINE_DISPOSITIONS, D16_KEYS)
    assert row["status"] != "PASS"
    assert row["status"] == "FAIL"
    assert row["contract_status"] == "CONTRACT_PARTIAL"
    assert row["disconnected_fields"] > 0


def test_disconnected_count_reaches_zero_before_configuration_pass():
    from dataclasses import replace

    from nse_pipeline.specs.consumption import (
        BASELINE_DISPOSITIONS,
        CURRENT_DISPOSITIONS,
        configuration_row,
        counts,
        is_production_reader,
    )

    baseline_counts = counts(BASELINE_DISPOSITIONS)
    current_counts = counts(CURRENT_DISPOSITIONS)
    assert baseline_counts["disconnected"] == 5
    assert current_counts == {
        "implemented": 16,
        "reserved": 11,
        "obsolete": 0,
        "disconnected": 0,
    }

    assert configuration_row(BASELINE_DISPOSITIONS, D16_KEYS)["status"] == "FAIL"
    current_row = configuration_row(CURRENT_DISPOSITIONS, D16_KEYS)
    assert current_row["status"] == "PASS"
    assert current_row["contract_status"] == "CONTRACT_PARTIAL"

    baseline_registry = _by_key(BASELINE_DISPOSITIONS)
    current_registry = _by_key(CURRENT_DISPOSITIONS)
    assert set(current_registry) == set(baseline_registry) == set(D16_KEYS)
    for key, reader_fragment in WIRED_READERS.items():
        entry = current_registry[key]
        former = baseline_registry[key]
        assert former.disposition == "DISCONNECTED"
        assert entry.disposition == "IMPLEMENTED"
        assert entry.reader
        assert reader_fragment in entry.reader
        assert is_production_reader(entry.reader)
        assert entry.behaviour_test
        # Relabelling without a reader is not a valid way to lower the count.
        with pytest.raises(ValueError):
            replace(former, disposition="IMPLEMENTED")


def test_survey_reports_missing_and_unregistered_without_an_allowlist():
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS, survey

    omitted = ("CostSpec", "schedule_basis")
    ghost = ("MomentumSpec", "ghost")
    declared = tuple(key for key in D16_KEYS if key != omitted) + (ghost,)

    report = survey(BASELINE_DISPOSITIONS, declared)
    assert report["unregistered"] == ["MomentumSpec.ghost"]
    assert report["missing"] == ["CostSpec.schedule_basis"]
    assert report["complete"] is False

    complete = survey(BASELINE_DISPOSITIONS, D16_KEYS)
    assert complete["unregistered"] == []
    assert complete["missing"] == []
    assert complete["complete"] is True


def test_reserved_field_has_no_runtime_effect():
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS

    reserved = [entry for entry in BASELINE_DISPOSITIONS if entry.disposition == "RESERVED"]
    assert reserved
    for entry in reserved:
        assert entry.reader is None
        assert entry.legal_value is not None
        assert entry.contract and entry.behaviour_test
        assert entry.disposition != "IMPLEMENTED"


def test_unknown_and_obsolete_dispositions_fail_closed():
    from nse_pipeline.specs.consumption import FieldDisposition

    with pytest.raises(ValueError):
        FieldDisposition(spec="SyntheticSpec", field="unknown", disposition="DEAD")
    with pytest.raises(ValueError):
        FieldDisposition(spec="SyntheticSpec", field="obsolete", disposition="OBSOLETE")
    obsolete = FieldDisposition(
        spec="SyntheticSpec",
        field="obsolete",
        disposition="OBSOLETE",
        notes="schema_version 2 rejects this field",
    )
    assert obsolete.disposition == "OBSOLETE"


def test_current_implemented_fields_name_importable_production_readers():
    """A registry label is only IMPLEMENTED if the named production symbol resolves."""
    import importlib

    from nse_pipeline.specs.consumption import CURRENT_DISPOSITIONS, IMPLEMENTED

    def resolve(reader):
        name = reader.split("::", 1)[0]
        if "." not in name:
            for module_name in ("nse_pipeline.specs", "nse_pipeline.backtest.sizing"):
                module = importlib.import_module(module_name)
                if hasattr(module, name):
                    return getattr(module, name)
            raise AssertionError(f"unresolvable production reader: {reader!r}")
        parts = name.split(".")
        for stop in range(len(parts), 0, -1):
            try:
                value = importlib.import_module(".".join(parts[:stop]))
            except ModuleNotFoundError:
                continue
            for attribute in parts[stop:]:
                value = getattr(value, attribute)
            return value
        raise AssertionError(f"unresolvable production reader: {reader!r}")

    implemented = [
        entry for entry in CURRENT_DISPOSITIONS if entry.disposition == IMPLEMENTED
    ]
    assert len(implemented) == 16
    for entry in implemented:
        assert callable(resolve(entry.reader)), entry.id


def test_trim_admission_is_implemented_without_rewriting_baseline_dispositions():
    baseline, current = map(_by_key, _registry())
    key = ("PortfolioSpec", "trim_overweight_targets")
    assert baseline[key].disposition == "RESERVED"
    assert baseline[key].reader is None
    assert current[key].disposition == "IMPLEMENTED"
    assert current[key].reader == "nse_pipeline.backtest.trims.plan_trims"
    assert current[key].legal_value is None
    assert current[key].behaviour_test == (
        "tests/test_accounting_trims.py::test_disabled_trims_reject_explicit_requests")


def test_reserved_fields_cannot_change_size_portfolio_orders():
    """Every RESERVED field is either schema-pinned or a sizing no-op."""
    from dataclasses import replace

    from nse_pipeline.backtest.sizing import size_portfolio_orders
    from nse_pipeline.specs import (
        CostSpec,
        ExecutionSpec,
        PortfolioSpec,
        ValuationPolicy,
    )
    from nse_pipeline.specs.consumption import BASELINE_DISPOSITIONS, RESERVED

    portfolio = PortfolioSpec(max_positions=3)
    valuation = ValuationPolicy(fallback_lag=2, lag_unit="exchange_sessions")
    execution = ExecutionSpec(
        trade_settlement_lag_sessions=1,
        adverse_slippage_bps_buy=0,
        adverse_slippage_bps_sell=0,
    )
    costs = CostSpec()

    def result(
        *,
        portfolio_override=None,
        valuation_override=None,
        execution_override=None,
    ):
        return size_portfolio_orders(
            portfolio if portfolio_override is None else portfolio_override,
            valuation if valuation_override is None else valuation_override,
            "1000",
            ["400", "400", "200"],
            price="1",
            costs=costs,
            execution=execution if execution_override is None else execution_override,
        )

    baseline = result()

    alternates = {
        ("ExecutionSpec", "max_participation_rate"): (
            "execution_override",
            replace(execution, max_participation_rate=0.5),
        ),
        ("ExecutionSpec", "circuit_limit_pct"): (
            "execution_override",
            replace(execution, circuit_limit_pct=0.1),
        ),
        ("ExecutionSpec", "hold_suspended_positions"): (
            "execution_override",
            replace(execution, hold_suspended_positions=False),
        ),
        ("ValuationPolicy", "series_precedence"): (
            "valuation_override",
            replace(valuation, series_precedence=("EQ",)),
        ),
        ("ValuationPolicy", "absence_review_sessions"): (
            "valuation_override",
            replace(valuation, absence_review_sessions=1),
        ),
        ("PortfolioSpec", "initial_cash"): (
            "portfolio_override",
            replace(portfolio, initial_cash=1.0),
        ),
        ("PortfolioSpec", "disposal_policy"): (
            "portfolio_override",
            replace(portfolio, disposal_policy="retire_zero", scenario_only=True),
        ),
        ("PortfolioSpec", "trim_overweight_targets"): (
            "portfolio_override",
            replace(portfolio, trim_overweight_targets=False),
        ),
    }
    pinned = {
        ("ExecutionSpec", "fill_timing"),
        ("ExecutionSpec", "unfilled_remainder"),
        ("ValuationPolicy", "carried_position_is"),
        ("PortfolioSpec", "weighting"),
    }
    reserved = {
        entry.key
        for entry in BASELINE_DISPOSITIONS
        if entry.disposition == RESERVED
    }
    assert set(alternates) | pinned == reserved

    for key, (override, value) in alternates.items():
        assert result(**{override: value}) == baseline, key

    with pytest.raises(ValueError):
        ExecutionSpec(
            trade_settlement_lag_sessions=1, fill_timing="same_session_close"
        )
    with pytest.raises(ValueError):
        ExecutionSpec(trade_settlement_lag_sessions=1, unfilled_remainder="reissue")
    with pytest.raises(ValueError):
        ValuationPolicy(
            fallback_lag=2,
            lag_unit="exchange_sessions",
            carried_position_is="resolved",
        )
    with pytest.raises(ValueError):
        PortfolioSpec(weighting="market_cap")
