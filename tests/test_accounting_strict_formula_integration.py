"""Integration of admitted strict formulas with signals and target composition.

These tests exercise production admission only. The legacy callable ranker is
checked once to document that its experimental behaviour still exists; it is not
used by target composition.
"""
from datetime import date
from decimal import Decimal
from fractions import Fraction
import inspect
import json

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.formulas import (
    AdjustmentEvidence, AdjustmentEvidenceSet, Formula, FormulaPriceInputs,
    add, admit_price_inputs, constant, evaluate_formula_prices, multiply, parameter,
    raw_close, scoped_adjusted_close,
)
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.backtest.scoped_adjustment import AdjustmentFactor
from nse_pipeline.backtest.signals import (
    rank_admitted_momentum, rank_formula_momentum, rank_momentum,
)
from nse_pipeline.backtest.targets import TargetRequest, compose_targets
from nse_pipeline.specs import (
    CostSpec, ExecutionSpec, MomentumSpec, PortfolioSpec, UniverseSpec, ValuationPolicy,
)

DAYS = (date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 6, 6))
AS_OF = DAYS[2]
AAA = "AAA|INE000000001|2019-01-01"
BBB = "BBB|INE000000002|2019-01-01"
CLOSES = {AAA: ("100", "102", "98", "99"), BBB: ("50", "40", "44", "45")}


def make_ledger(closes=None, *, extra_session=False):
    closes = CLOSES if closes is None else closes
    stream = SessionStream()
    ledger = ObservationLedger(stream)
    sessions = DAYS[:4] if extra_session else DAYS[:3]
    for index, on in enumerate(sessions):
        stream.observe(on)
        bars = []
        for key, series in sorted(closes.items()):
            close = Decimal(series[index])
            bars.append(Bar(key, key.split("|")[0], "EQ", on, close, close, close, close,
                            100, True, key.split("|")[1], Decimal("100000"), 10))
        ledger.ingest(on, bars)
    return ledger


def factor(security_key, *, action, value, available=DAYS[0], scope_to=DAYS[1],
           version=1, citation=None):
    return AdjustmentFactor(
        security_key=security_key, action_id=action, effective_date=DAYS[1],
        available_at=EventTime(available, Phase.PRE_OPEN_EVIDENCE), factor=value,
        scope_from=DAYS[0], scope_to=scope_to, version=version,
        citation=citation or f"{action}-evidence")


def momentum_spec(basis="close", lookback=2):
    return MomentumSpec(lookback_sessions=lookback, price_basis=basis,
                        min_observations=lookback + 1)


def test_exact_formula_prices_rank_without_decimal_rounding():
    formula = Formula(multiply(raw_close(), constant("1/3")))
    ranks = rank_formula_momentum(make_ledger(), AS_OF, (AAA,), formula,
                                  spec=momentum_spec("close"))

    row = ranks.ranked[0]
    # Exact 100/3 and 98/3 inputs; momentum is exactly -1/50, not a rounded decimal.
    assert row.anchor_price == Fraction(100, 3)
    assert row.price == Fraction(98, 3)
    assert row.momentum == Fraction(-1, 50)
    assert isinstance(row.price, Fraction)
    assert ranks.formula_basis.formula_sha256 == formula.sha256
    assert ranks.price_basis == f"formula:{formula.sha256}"


def test_adjusted_split_and_bonus_rank_across_keys_with_intrinsic_provenance():
    formula = Formula(scoped_adjusted_close())
    split = factor(AAA, action="split", value="1/2", citation="aaa-split")
    bonus = factor(BBB, action="bonus", value="3/2", citation="bbb-bonus")
    evidence = AdjustmentEvidenceSet({
        AAA: AdjustmentEvidence("verified", (split,)),
        BBB: AdjustmentEvidence("verified", (bonus,)),
    })
    ranks = rank_formula_momentum(make_ledger(), AS_OF, (AAA, BBB), formula,
                                  spec=momentum_spec("adj_close"),
                                  adjustment_evidence=evidence)

    by_key = {row.key: row for row in ranks.ranked}
    # AAA: 98 / (100 * 1/2) - 1 = 24/25.  BBB: 44 / (50 * 3/2) - 1 = -31/75.
    assert by_key[AAA].momentum == Fraction(24, 25)
    assert by_key[BBB].momentum == Fraction(-31, 75)
    assert by_key[AAA].anchor_price == Fraction(50)
    assert by_key[BBB].anchor_price == Fraction(75)
    assert ranks.order == (AAA, BBB)
    assert ranks.formula_basis.formula_sha256 == formula.sha256
    encoded = ranks.formula_basis.canonical_json
    assert "aaa-split" in encoded and "bbb-bonus" in encoded
    assert {row[0] for row in ranks.formula_basis.adjustment_scope} == {AAA, BBB}
    assert json.loads(encoded)["formula_sha256"] == formula.sha256


def test_per_key_evidence_never_treats_another_keys_factor_as_applicable():
    formula = Formula(scoped_adjusted_close())
    foreign = factor(BBB, action="split", value="1/2")
    ranks = rank_formula_momentum(
        make_ledger(), AS_OF, (AAA,), formula, spec=momentum_spec("adj_close"),
        adjustment_evidence=AdjustmentEvidenceSet({
            AAA: AdjustmentEvidence("verified", (foreign,))}))

    assert ranks.ranked == ()
    reasons = dict(ranks.unrankable)
    assert "factor_security_scope_mismatch" in reasons[AAA]

    missing = rank_formula_momentum(
        make_ledger(), AS_OF, (AAA,), formula, spec=momentum_spec("adj_close"),
        adjustment_evidence=AdjustmentEvidenceSet({}))
    assert dict(missing.unrankable)[AAA] == "formula_adjustment_evidence_missing_for_key"


def test_future_action_evidence_does_not_change_earlier_value_or_provenance():
    formula = Formula(scoped_adjusted_close())
    first_as_of = DAYS[1]
    early = factor(AAA, action="split", value="1/2", scope_to=DAYS[0])
    later = factor(AAA, action="split", value="1/4", scope_to=DAYS[0],
                   available=DAYS[2], version=2, citation="future-v2")
    spec = momentum_spec("adj_close", lookback=1)

    before = rank_formula_momentum(
        make_ledger(), first_as_of, (AAA,), formula, spec=spec,
        adjustment_evidence=AdjustmentEvidenceSet({
            AAA: AdjustmentEvidence("verified", (early,))}))
    after = rank_formula_momentum(
        make_ledger(), first_as_of, (AAA,), formula, spec=spec,
        adjustment_evidence=AdjustmentEvidenceSet({
            AAA: AdjustmentEvidence("verified", (later, early))}))

    assert before.ranked == after.ranked
    assert before.formula_basis == after.formula_basis
    assert before.ranked[0].anchor_price == Fraction(50)
    assert before.ranked[0].price == Fraction(102)
    assert before.ranked[0].momentum == Fraction(26, 25)


def test_adjusted_ranking_ignores_later_ledger_appends():
    formula = Formula(scoped_adjusted_close())
    evidence = AdjustmentEvidenceSet({
        AAA: AdjustmentEvidence("verified", (
            factor(AAA, action="split", value="1/2", citation="aaa-split"),))})
    kwargs = dict(spec=momentum_spec("adj_close"), adjustment_evidence=evidence)
    before = rank_formula_momentum(make_ledger(), AS_OF, (AAA,), formula, **kwargs)
    after = rank_formula_momentum(make_ledger(extra_session=True), AS_OF, (AAA,),
                                  formula, **kwargs)
    assert before.ranked == after.ranked
    assert before.formula_basis == after.formula_basis
    assert before.ranked[0].momentum == Fraction(24, 25)


def test_unchanged_formula_and_inputs_are_deterministic_but_bindings_change_identity():
    formula = Formula(add(raw_close(), parameter("x")), {"x": "1/3"})
    changed = Formula(add(raw_close(), parameter("x")), {"x": "1/2"})
    requests = ((AAA, DAYS[0]), (AAA, AS_OF))
    ledger = make_ledger()

    first = evaluate_formula_prices(formula, ledger, requests, AS_OF)
    second = evaluate_formula_prices(formula, ledger, requests, AS_OF)
    appended = evaluate_formula_prices(formula, make_ledger(extra_session=True),
                                       requests, AS_OF)
    different = evaluate_formula_prices(changed, ledger, requests, AS_OF)

    assert first.basis == second.basis == appended.basis
    assert first.values == second.values == appended.values
    assert different.basis.sha256 != first.basis.sha256
    assert different.basis.formula_sha256 != first.basis.formula_sha256
    assert admit_price_inputs(first) is first


def test_admitted_and_legacy_raw_ranking_share_canonical_economics():
    ledger = make_ledger({AAA: ("100", "105", "110"), BBB: ("50", "52.5", "55")})
    spec = momentum_spec("close")
    admitted = rank_admitted_momentum(ledger, AS_OF, (AAA, BBB), spec)
    legacy = rank_momentum(ledger, AS_OF, (AAA, BBB), spec)

    assert admitted.ranked == legacy.ranked
    assert admitted.unrankable == legacy.unrankable
    assert admitted.observations == legacy.observations
    assert admitted.anchor == legacy.anchor
    assert admitted.window_sessions == legacy.window_sessions
    # Equal exact momentum ties break by key ascending in both paths.
    assert admitted.order == (AAA, BBB)
    assert admitted.ranked[0].momentum == admitted.ranked[1].momentum == Fraction(1, 10)
    assert admitted.formula_basis is None


def test_legacy_callable_path_still_exists_but_is_not_production_admission():
    ledger = make_ledger()
    spec = momentum_spec("close")
    legacy = rank_momentum(
        ledger, AS_OF, (AAA,), spec,
        basis_price=lambda key, session, as_of: Decimal("1"),
        basis_id="caller-owned-basis")
    assert legacy.basis_provenance.kind == "callable"
    assert legacy.basis_provenance.basis_id == "caller-owned-basis"

    with pytest.raises(TypeError):
        rank_admitted_momentum(ledger, AS_OF, (AAA,), spec,
                               lambda key, session, as_of: Decimal("1"))
    with pytest.raises(TypeError):
        rank_formula_momentum(ledger, AS_OF, (AAA,), Formula(raw_close()),
                              spec=spec, basis_id="caller-owned-basis")
    assert "basis_price" not in inspect.signature(rank_admitted_momentum).parameters
    assert "basis_id" not in inspect.signature(rank_formula_momentum).parameters
    assert "basis_id" not in inspect.signature(compose_targets).parameters


def test_admission_rejects_unvalidated_formulas_and_basis_mislabels():
    ledger = make_ledger()
    with pytest.raises(TypeError, match="validated_formula"):
        rank_formula_momentum(ledger, AS_OF, (AAA,), object(), spec=momentum_spec("close"))
    with pytest.raises(ValueError, match="requires momentum.price_basis"):
        rank_formula_momentum(ledger, AS_OF, (AAA,), Formula(raw_close()),
                              spec=momentum_spec("adj_close"))
    with pytest.raises(ValueError, match="requires momentum.price_basis"):
        rank_formula_momentum(ledger, AS_OF, (AAA,), Formula(scoped_adjusted_close()),
                              spec=momentum_spec("close"))
    with pytest.raises(ValueError, match="builtin raw close basis only"):
        rank_admitted_momentum(ledger, AS_OF, (AAA,), momentum_spec("adj_close"))


def test_mixed_raw_and_adjusted_formula_basis_is_never_mislabeled():
    mixed = Formula(add(scoped_adjusted_close(), raw_close()))
    inputs = evaluate_formula_prices(mixed, make_ledger(), ((AAA, AS_OF),), AS_OF)
    assert inputs.price_basis == "mixed"
    with pytest.raises(ValueError, match="mixing raw and adjusted"):
        rank_formula_momentum(make_ledger(), AS_OF, (AAA,), mixed,
                              spec=momentum_spec("adj_close"))
    with pytest.raises(ValueError, match="mixes raw and adjusted"):
        compose(make_ledger(), basis="adj_close", formula=mixed)


def test_malformed_materialized_inputs_cannot_enter_ranking():
    forged = FormulaPriceInputs("0" * 64, AS_OF, "close", (), (), None)
    with pytest.raises(ValueError, match="not_module_issued"):
        admit_price_inputs(forged)
    with pytest.raises(TypeError):
        rank_formula_momentum(make_ledger(), AS_OF, (AAA,), Formula(raw_close()),
                              spec=momentum_spec("close"), price_inputs=forged)


def target_specs(*, basis, lookback=2):
    universe = UniverseSpec(series=("EQ",), isin_policy="any", min_price=0,
                            min_volume=0, min_adv_traded_value=0,
                            min_adv_window_sessions=2, min_avg_trades=0,
                            min_listed_sessions=1, label="synthetic formula targets")
    momentum = MomentumSpec(lookback_sessions=lookback, price_basis=basis,
                            min_observations=lookback + 1)
    execution = ExecutionSpec(trade_settlement_lag_sessions=1,
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0,
                              sizing_participation_rate=0.5, max_participation_rate=0.5,
                              adv_window_sessions=2)
    costs = CostSpec(brokerage_bps=0, brokerage_cap_per_order=0, stt_bps_buy=0,
                     stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0,
                     gst_on_brokerage_pct=0)
    portfolio = PortfolioSpec(max_positions=8, sizing_policy="P2_size_excluding_unresolved")
    valuation = ValuationPolicy(fallback_lag=1, lag_unit="exchange_sessions",
                                sizing_uses_screen_value=False)
    return universe, momentum, execution, costs, portfolio, valuation


def compose(ledger, *, basis, formula=None, evidence=None):
    universe, momentum, execution, costs, portfolio, valuation = target_specs(basis=basis)
    return compose_targets(
        ledger, AS_OF, universe=universe, momentum=momentum, execution=execution,
        costs=costs, portfolio=portfolio, valuation=valuation,
        requests=(TargetRequest(AAA, "1000"), TargetRequest(BBB, "1000")),
        settled_cash=5000, security_status="listed", allow_partial_adv=False,
        formula=formula, adjustment_evidence=evidence)


def test_default_target_path_is_unchanged_legacy_raw_close():
    ledger = make_ledger()
    plan = compose(ledger, basis="close")
    legacy = rank_momentum(ledger, AS_OF, plan.membership.eligible,
                           momentum_spec("close"))
    assert plan.ranks.ranked == legacy.ranked
    assert plan.ranks.formula_basis is None
    assert plan.intent_for(AAA).decision_close == Decimal("98")
    assert plan.intent_for(BBB).decision_close == Decimal("44")


def test_adjusted_formula_ranks_never_change_the_raw_execution_close():
    formula = Formula(scoped_adjusted_close())
    evidence = AdjustmentEvidenceSet({
        AAA: AdjustmentEvidence("verified", (
            factor(AAA, action="split", value="1/2", citation="aaa-split"),)),
        BBB: AdjustmentEvidence("verified", (
            factor(BBB, action="bonus", value="3/2", citation="bbb-bonus"),)),
    })
    plan = compose(make_ledger(), basis="adj_close", formula=formula, evidence=evidence)

    assert plan.ranks.formula_basis.formula_sha256 == formula.sha256
    assert plan.ranks.ranked[0].momentum == Fraction(24, 25)
    # Ranks use adjusted formula prices, but execution prices decisions at the raw close.
    assert plan.intent_for(AAA).decision_close == Decimal("98")
    assert plan.intent_for(BBB).decision_close == Decimal("44")
    assert all(row.decision_close == Decimal("98") or row.decision_close == Decimal("44")
               for row in plan.intents)


def test_target_composition_rejects_callable_formula_and_evidence_without_formula():
    ledger = make_ledger()
    with pytest.raises(ValueError, match="validated strict Formula"):
        compose(ledger, basis="close", formula=lambda: None)
    with pytest.raises(ValueError, match="requires an explicit strict formula"):
        compose(ledger, basis="close",
                evidence=AdjustmentEvidenceSet({AAA: AdjustmentEvidence("verified")}))
    assert "basis_price" not in inspect.signature(compose_targets).parameters
