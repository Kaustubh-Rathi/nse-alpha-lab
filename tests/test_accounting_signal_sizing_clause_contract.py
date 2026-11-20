"""Direct clause contract for P0-SIGNALS, P0-SIZING and P0-POLICIES.

This file is an evidence lane, not a status promotion. It adds only the direct
clause assertions that were missing at HEAD; broad component coverage remains in
``test_accounting_signals.py``, ``test_accounting_sizing.py`` and
``test_specs.py`` and is not duplicated here.

Every test docstring names the matrix acceptance clause it supports.
"""
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

POLICY_CASES = json.loads(
    (Path(__file__).parent / "golden/policy_cases.json").read_text(encoding="utf-8")
)


# AT-POL-02 requires these seven hand-derived D-14 goldens. Four ids in the
# fixture use the batch-3 vocabulary rather than the research-memo shorthand;
# the expected values below are derived from the D-14 policy meanings, not read
# from the fixture.
D14_SEVEN = {
    "rank0_unresolved_cash900_p1": {
        "semantic": "P1 freezes the whole round when rank 0 is unresolved",
        "policy": "P1_freeze_on_unresolved",
        "filled": ("0", "0"),
        "cash": "900",
        "fees": "0",
        "reasons": ("frozen_unresolved", "frozen_unresolved"),
    },
    "rank0_unresolved_cash900_p2": {
        "semantic": "P2 excludes unresolved rank 0 and sizes rank 1",
        "policy": "P2_size_excluding_unresolved",
        "filled": ("0", "450"),
        "cash": "450",
        "fees": "0",
        "reasons": ("excluded_unresolved", "filled"),
    },
    "sizing_p3_screen_clip": {
        "semantic": "P3 clips an unresolved screen value to settled cash",
        "policy": "P3_size_including_unresolved_at_screen",
        "filled": ("100000", "0"),
        "cash": "0",
        "fees": "0",
        "reasons": ("filled_screen_value", "no_cash"),
    },
    "sizing_p3_slot": {
        "semantic": "P3 spends a slot on a filled screen-valued rank",
        "policy": "P3_size_including_unresolved_at_screen",
        "filled": ("40000", "40000", "0"),
        "cash": "20000",
        "fees": "0",
        "reasons": ("filled", "filled_screen_value", "no_slot"),
    },
    "sizing_p1_not_first": {
        "semantic": "P1 freezes rank 0 when only rank 1 is unresolved",
        "policy": "P1_freeze_on_unresolved",
        "filled": ("0", "0"),
        "cash": "900",
        "fees": "0",
        "reasons": ("frozen_unresolved", "frozen_unresolved"),
    },
    "sizing_p3_unavailable": {
        "semantic": "P3 refuses to infer a missing screen value",
        "policy": "P3_size_including_unresolved_at_screen",
        "filled": ("0", "50000"),
        "cash": "50000",
        "fees": "0",
        "reasons": ("screen_value_unavailable", "filled"),
    },
    "sizing_p2_fees": {
        "semantic": "P2 fee-inclusive sizing respects the brokerage cap",
        "policy": "P2_size_excluding_unresolved",
        "filled": ("99900",),
        "cash": "0",
        "fees": "100",
        "reasons": ("filled",),
    },
}


def _zero_costs():
    from nse_pipeline.specs import CostSpec

    return CostSpec(brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0,
                    stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0,
                    gst_on_brokerage_pct=0)


def _zero_execution():
    from nse_pipeline.specs import ExecutionSpec

    return ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                         adverse_slippage_bps_sell=0)


def _valuation(screen_value=False):
    from nse_pipeline.specs import ValuationPolicy

    return ValuationPolicy(fallback_lag=30, lag_unit="calendar_days",
                           sizing_uses_screen_value=screen_value)


def _run_policy_case(case):
    from nse_pipeline.backtest.sizing import size_orders
    from nse_pipeline.specs import CostSpec, ExecutionSpec

    screen = {}
    if case.get("screen_values") is not None:
        screen["screen_values"] = case["screen_values"]
    if case.get("screen_prices") is not None:
        screen["screen_prices"] = case["screen_prices"]
    return size_orders(
        case["policy"], case["cash"], case["desired_in_rank_order"],
        price=case["price"], slots=case["slots"],
        costs=CostSpec(**POLICY_CASES["cost_models"][case["cost_model"]]),
        execution=ExecutionSpec(**POLICY_CASES["execution_model"]),
        unresolved_ranks=case["unresolved_ranks"], **screen)


def test_AT_POL_02_seven_d14_goldens_are_present_and_exact():
    """AT-POL-02: the seven D-14 goldens exist and reproduce their hand-derived values.

    The existing broad golden loop reproduces whatever cases the fixture contains.
    This clause test pins the seven required P1 freeze, P2 exclude/fees, and P3
    screen clip/slot/unavailable cases so deleting a required golden fails here
    instead of silently shrinking the fixture.
    """
    cases = {case["id"]: case for case in POLICY_CASES["cases"]}
    assert len(D14_SEVEN) == 7
    assert {expected["policy"] for expected in D14_SEVEN.values()} == {
        "P1_freeze_on_unresolved",
        "P2_size_excluding_unresolved",
        "P3_size_including_unresolved_at_screen",
    }

    for case_id, expected in D14_SEVEN.items():
        assert case_id in cases, f"missing required D-14 golden: {case_id}"
        case = cases[case_id]
        result = _run_policy_case(case)

        assert case["policy"] == expected["policy"], case_id
        assert result.policy == expected["policy"], case_id
        assert result.filled == tuple(D(value) for value in expected["filled"]), case_id
        assert result.remaining_cash == D(expected["cash"]), case_id
        assert result.fees == D(expected["fees"]), case_id
        assert result.reasons == expected["reasons"], case_id

        # The fixture is the independent hand-derived golden, not the oracle for
        # this test; both it and the module must agree with the D-14 values above.
        assert tuple(D(value) for value in case["expected_filled"]) == result.filled, case_id
        assert D(case["expected_cash"]) == result.remaining_cash, case_id
        assert D(case["expected_fees"]) == result.fees, case_id
        assert tuple(case["expected_reasons"]) == result.reasons, case_id


def test_AT_POL_03_AT_SIZ_05_short_aliases_stay_at_the_size_orders_boundary():
    """AT-POL-03 / AT-SIZ-05: one resolver accepts long names only.

    ``PortfolioSpec`` rejects short aliases at construction, the single
    ``resolve_sizing_policy`` resolver rejects an injected short alias, and a
    short alias remains usable only at the ``size_orders`` boundary where it is
    normalized once to the canonical long name.
    """
    from nse_pipeline.backtest.sizing import size_orders
    from nse_pipeline.specs import (CANONICAL_POLICIES, PortfolioSpec,
                                    resolve_sizing_policy)

    for long_name in CANONICAL_POLICIES:
        valuation = _valuation(screen_value=long_name == "P3_size_including_unresolved_at_screen")
        assert resolve_sizing_policy(PortfolioSpec(sizing_policy=long_name),
                                     valuation) == long_name

    valuation = _valuation()

    with pytest.raises(ValueError, match="unknown_sizing_policy"):
        PortfolioSpec(sizing_policy="P2")

    injected = PortfolioSpec()
    object.__setattr__(injected, "sizing_policy", "P2")
    with pytest.raises(ValueError, match="unknown_sizing_policy"):
        resolve_sizing_policy(injected, valuation)

    boundary = size_orders("P2", "900", ["550", "450"], price="1", slots=2,
                           costs=_zero_costs(), execution=_zero_execution(),
                           unresolved_ranks=(0,))
    assert boundary.policy == "P2_size_excluding_unresolved"
    assert boundary.filled == (D("0"), D("450"))
    assert boundary.remaining_cash == D("450")
    assert boundary.reasons == ("excluded_unresolved", "filled")


def test_AT_SIG_05_same_method_attestation_does_not_capture_different_live_state():
    """AT-SIG-05: identical attestation cannot collapse different live method state.

    The former counterexample is closed. Bound-method receiver state is part of the
    callable identity, so two holders with different prices now publish different
    source digests even under the same basis_id and basis_parameters. Two holders
    with identical state still share provenance.
    """
    from nse_pipeline.backtest.signals import rank_momentum

    from tests.test_accounting_signals import (
        DAYS, _MethodBasisHolder, momentum_spec, two_session_ledger)

    ledger = two_session_ledger()
    attestation = {"declared": 1}
    common = {"basis_id": "same", "basis_parameters": attestation}
    low = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2),
                       _MethodBasisHolder(D("11")).price, **common)
    high = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2),
                        _MethodBasisHolder(D("12")).price, **common)
    same = rank_momentum(ledger, DAYS[1], ("K",), momentum_spec(1, 2),
                        _MethodBasisHolder(D("11")).price, **common)

    assert low.ranked[0].price == D("11")
    assert high.ranked[0].price == D("12")
    assert low.basis_provenance != high.basis_provenance
    assert low.basis_provenance.source_digest != high.basis_provenance.source_digest
    assert low.basis_provenance.basis_id == high.basis_provenance.basis_id == "same"
    assert dict(low.basis_provenance.parameters) == attestation
    assert low.basis_provenance.reproducible and high.basis_provenance.reproducible
    assert same.basis_provenance == low.basis_provenance


def test_AT_SIG_05_strict_paths_remain_intrinsic_and_reproducible():
    """AT-SIG-05: production admission is unchanged by legacy live-state freezing."""
    from fractions import Fraction

    from nse_pipeline.backtest.formulas import Formula, raw_close
    from nse_pipeline.backtest.signals import (
        rank_admitted_momentum, rank_formula_momentum)

    from tests.test_accounting_signals import DAYS, momentum_spec, two_session_ledger

    ledger = two_session_ledger()
    spec = momentum_spec(1, 2)
    admitted = rank_admitted_momentum(ledger, DAYS[1], ("K",), spec)
    admitted_again = rank_admitted_momentum(ledger, DAYS[1], ("K",), spec)
    assert admitted == admitted_again
    assert admitted.basis_provenance.kind == "builtin"
    assert admitted.basis_provenance.basis_id is None
    assert admitted.basis_provenance.source_digest is None
    assert admitted.formula_basis is None

    formula = Formula(raw_close())
    first = rank_formula_momentum(ledger, DAYS[1], ("K",), formula, spec=spec)
    second = rank_formula_momentum(ledger, DAYS[1], ("K",), formula, spec=spec)
    assert first == second
    assert first.basis_provenance is None
    assert first.formula_basis == second.formula_basis
    assert first.formula_basis.formula_sha256 == formula.sha256
    assert first.price_basis == f"formula:{formula.sha256}"
    assert first.ranked[0].momentum == Fraction(1, 10)


def test_AT_POL_05_slot_policy_comparison_and_trim_admission_are_direct():
    """AT-POL-05: the slot half is compared directly and trim admission is wired.

    The historical D-16 snapshot still records the pre-decision RESERVED state.
    The current registry wires ``trim_overweight_targets`` to the owner-approved
    explicit-target planner; the runner-level slot/trim comparison is
    ``tests/test_accounting_observed_session_trims.py::
    test_full_trim_frees_the_position_slot_before_a_preexisting_cash_buy``.
    """
    cases = {case["id"]: case for case in POLICY_CASES["cases"]}
    p3 = _run_policy_case(cases["sizing_p3_slot"])
    p2 = _run_policy_case(cases["sizing_p3_slot_p2"])

    assert p3.filled == (D("40000"), D("40000"), D("0"))
    assert p3.remaining_cash == D("20000")
    assert p3.fees == D("0")
    assert p3.reasons == ("filled", "filled_screen_value", "no_slot")

    assert p2.filled == (D("40000"), D("0"), D("40000"))
    assert p2.remaining_cash == D("20000")
    assert p2.fees == D("0")
    assert p2.reasons == ("filled", "excluded_unresolved", "filled")

    # The same two slots are allocated to different ranks: P3 spends slot 2 on
    # the unresolved screen-valued rank, while P2 defers that slot to rank 2.
    assert p3.filled != p2.filled
    assert p3.remaining_cash == p2.remaining_cash

    from nse_pipeline.specs.consumption import (
        BASELINE_DISPOSITIONS, CURRENT_DISPOSITIONS)

    baseline = next(entry for entry in BASELINE_DISPOSITIONS
                    if entry.key == ("PortfolioSpec", "trim_overweight_targets"))
    current = next(entry for entry in CURRENT_DISPOSITIONS
                   if entry.key == ("PortfolioSpec", "trim_overweight_targets"))
    assert baseline.disposition == "RESERVED"
    assert baseline.reader is None
    assert baseline.legal_value is True
    assert current.disposition == "IMPLEMENTED"
    assert current.reader == "nse_pipeline.backtest.trims.plan_trims"


def test_AT_POL_05_slot_policy_comparison_is_direct_but_trim_is_absent():
    """Historical node name cited by the readiness-reopen reconciliation.

    The name and its former gap claim are historical. Trim behaviour is now
    implemented, so this alias delegates to the current direct comparison rather
    than reasserting the obsolete absence claim.
    """
    test_AT_POL_05_slot_policy_comparison_and_trim_admission_are_direct()
