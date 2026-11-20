"""Production target composition over the public in-memory accounting components."""
from dataclasses import FrozenInstanceError
from datetime import date, timedelta
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.backtest.targets import (
    CAPACITY_UNKNOWN_REASON,
    TargetRequest,
    ScreenMark,
    compose_targets,
)
from nse_pipeline.specs import (
    CostSpec,
    ExecutionSpec,
    MomentumSpec,
    PortfolioSpec,
    UniverseSpec,
    ValuationPolicy,
)

GOLDEN = json.loads((Path(__file__).parent / "golden/target_composition_cases.json")
                    .read_text(encoding="utf-8"))
DAYS = tuple(date(2024, 1, 2) + timedelta(days=i) for i in range(4))
AS_OF = DAYS[2]


def bar(key, session, close, traded_value="100", *, series="EQ", valid=True, volume=100):
    price = D(close) if valid else None
    return Bar(key, key, series, session, price, price, price, price, volume, valid,
               "INE000000001", D(traded_value), 10)


def ledger(frame, *, extra_sessions=()):
    stream = SessionStream()
    result = ObservationLedger(stream)
    sessions = tuple(DAYS[:len(frame)]) + tuple(extra_sessions)
    for on, bars in zip(sessions, frame):
        stream.observe(on)
        result.ingest(on, bars)
    return result


def frame(*, invalid=None, omit=(), future=False):
    """Three fixed sessions plus optional future session; omit is key/session-index."""
    closes = {
        "A": ("10", "12", "15", "16"),
        "B": ("10", "11", "12", "13"),
        "C": ("20", "15", "14", "13"),
        "X": ("10", "11", "12", "13"),  # excluded BE series
        "Q": (None, "11", "12", "13"),   # eligible, but no valid anchor bar
        "U": ("10", None, "12", "13"),   # partial ADV -> unknown capacity
        "V": ("10", "11", "12", "13"),  # fully observed zero ADV
        "I": ("10", "11", "12", "13"),  # invalid on decision session
    }
    values = {
        "A": ("100", "100", "300", "500"),
        "B": ("80", "80", "120", "200"),
        "C": ("100", "100", "100", "100"),
        "X": ("100", "100", "100", "100"),
        "Q": ("100", "100", "100", "100"),
        "U": ("100", "100", "100", "100"),
        "V": ("0", "0", "0", "0"),
        "I": ("100", "100", "100", "100"),
    }
    result = []
    count = 4 if future else 3
    for index in range(count):
        session_bars = []
        for key in closes:
            if (key, index) in omit:
                continue
            is_valid = not (key == "I" and index == 2)
            if invalid == (key, index):
                is_valid = False
            close = closes[key][index]
            if close is None:
                continue
            session_bars.append(bar(key, DAYS[index], close, values[key][index],
                                    series="BE" if key == "X" else "EQ", valid=is_valid))
        result.append(tuple(session_bars))
    return result


def specs(*, policy="P2_size_excluding_unresolved", cash=120, positions=8,
          rate=0.5, adv_window=2, costs=None):
    universe = UniverseSpec(series=("EQ",), isin_policy="any", min_price=0,
                            min_volume=0, min_adv_traded_value=0,
                            min_adv_window_sessions=2, min_avg_trades=0,
                            min_listed_sessions=1, label="synthetic composition screen")
    momentum = MomentumSpec(lookback_sessions=2, min_observations=2)
    execution = ExecutionSpec(trade_settlement_lag_sessions=1,
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0,
                              sizing_participation_rate=rate,
                              max_participation_rate=max(rate, 0.05),
                              adv_window_sessions=adv_window)
    costs = costs or CostSpec(brokerage_bps=0, brokerage_cap_per_order=0,
                              stt_bps_buy=0, stt_bps_sell=0, stamp_bps_buy=0,
                              exchange_bps=0, gst_on_brokerage_pct=0)
    portfolio = PortfolioSpec(max_positions=positions, sizing_policy=policy)
    valuation = ValuationPolicy(fallback_lag=1, lag_unit="exchange_sessions",
                                sizing_uses_screen_value=(policy == "P3_size_including_unresolved_at_screen"))
    return universe, momentum, execution, costs, portfolio, valuation


def compose(source, requests, *, settled_cash=120, as_of=AS_OF, policy="P2_size_excluding_unresolved",
            unresolved=(), marks=(), membership_kind="research_eligibility",
            membership_evidence=None, security_status="listed", allow_partial_adv=False,
            rate=0.5, costs=None, adv_window=2):
    universe, momentum, execution, costs, portfolio, valuation = specs(
        policy=policy, cash=settled_cash, rate=rate, costs=costs, adv_window=adv_window)
    return compose_targets(
        source, as_of, universe=universe, momentum=momentum, execution=execution,
        costs=costs, portfolio=portfolio, valuation=valuation, requests=requests,
        settled_cash=settled_cash, security_status=security_status,
        allow_partial_adv=allow_partial_adv, membership_kind=membership_kind,
        membership_evidence=membership_evidence, unresolved_keys=unresolved,
        screen_marks=marks)


def main_plan(source=None):
    requests = (TargetRequest("A", "80"), TargetRequest("B", "100"),
                TargetRequest("C", "100"), TargetRequest("X", "100"),
                TargetRequest("Q", "100"), TargetRequest("U", "100"),
                TargetRequest("V", "100"))
    return compose(ledger(frame()) if source is None else source, requests)


def test_hand_derived_plan_matches_fixed_golden():
    plan = main_plan()
    expected = GOLDEN["case"]
    assert plan.order == tuple(expected["rank_order"])
    assert [(row.key, str(row.allocated_notional), row.requested_quantity,
             str(row.capacity_notional), str(row.decision_close)) for row in plan.targets] == [
        (row["key"], row["allocated_notional"], row["requested_quantity"],
         row["capacity_notional"], row["decision_close"])
        for row in expected["targets"]
    ]
    assert plan.sizing.remaining_cash == D(expected["remaining_cash"])
    assert plan.sizing.fees == D("0")
    assert plan.membership.label == "RESEARCH_ELIGIBILITY_ONLY"
    assert "No strategy returns or performance statistics are produced" in plan.limitations[0]


def test_requested_universe_exclusion_and_unrankable_reason_are_explicit():
    plan = main_plan()
    assert plan.exclusion_for("X").reasons == ("series_excluded",)
    assert plan.exclusion_for("Q").reasons == ("no_valid_bar_on_anchor",)
    assert plan.exclusion_for("X").decision_close == D("12")
    assert plan.exclusion_for("Q").decision_close == D("12")


def test_unknown_and_zero_adv_capacity_never_become_targets():
    plan = main_plan()
    assert plan.exclusion_for("U").reasons == (CAPACITY_UNKNOWN_REASON,)
    assert plan.exclusion_for("U").capacity_notional is None
    assert plan.exclusion_for("V").reasons == ("capacity_zero",)
    assert plan.exclusion_for("V").capacity_notional == D("0.00")
    assert "U" not in plan.target_keys and "V" not in plan.target_keys


def test_known_adv_capacity_clips_request_before_rank_sequential_sizing():
    plan = main_plan()
    assert plan.intent_for("A").capacity_notional == D("100.00")
    assert plan.intent_for("A").sizing_base_notional == D("80.00")
    assert plan.intent_for("B").capacity_notional == D("50.00")
    assert plan.intent_for("B").sizing_base_notional == D("50.00")
    assert plan.intent_for("B").allocated_notional == D("36.00")
    assert plan.intent_for("C").sizing_reason == "unaffordable_or_partial_disallowed"


def test_p2_excludes_unresolved_and_later_rank_uses_cash():
    plan = compose(ledger(frame()),
                   (TargetRequest("A", "80"), TargetRequest("B", "50")),
                   unresolved=("A",), settled_cash=100)
    assert plan.order == ("A", "B")
    assert plan.intent_for("A").sizing_reason == "excluded_unresolved"
    assert plan.intent_for("A").allocated_notional == 0
    assert plan.intent_for("B").allocated_notional == D("48.00")
    assert plan.sizing.remaining_cash == D("52.00")


def test_p3_uses_only_the_explicit_screen_value_and_price():
    plan = compose(ledger(frame()),
                   (TargetRequest("A", "80"), TargetRequest("B", "50")),
                   policy="P3_size_including_unresolved_at_screen", unresolved=("A",),
                   marks=(ScreenMark("A", "60", "20"),), settled_cash=100)
    assert plan.intent_for("A").sizing_base_notional == D("60.00")
    assert plan.intent_for("A").allocated_notional == D("60.00")
    assert plan.intent_for("A").sizing_reason == "filled_screen_value"
    assert plan.intent_for("A").requested_quantity == 4  # uses decision close 15, not screen price 20
    assert plan.intent_for("B").allocated_notional == D("36.00")


def test_official_membership_intersects_the_research_screen_and_fails_closed_without_evidence():
    class MembershipFixture:
        def is_member(self, key, as_of):
            return key == "A"

    universe, momentum, execution, costs, portfolio, valuation = specs()
    kwargs = dict(universe=universe, momentum=momentum, execution=execution,
                  costs=costs, portfolio=portfolio, valuation=valuation,
                  requests=(TargetRequest("A", "10"), TargetRequest("B", "10"),
                            TargetRequest("X", "10")), settled_cash=120,
                  security_status="listed", allow_partial_adv=False,
                  membership_kind="official_membership")
    with pytest.raises(ValueError, match="unsupported_membership_kind"):
        compose_targets(ledger(frame()), AS_OF, **kwargs)
    official = compose_targets(ledger(frame()), AS_OF,
                               membership_evidence=MembershipFixture(), **kwargs)
    assert official.membership.official is True
    assert official.membership.eligible == ("A",)
    assert official.exclusion_for("B").reasons == ("not_a_member_as_of",)
    assert official.exclusion_for("X").reasons == ("series_excluded",)


def test_appending_later_sessions_does_not_change_the_as_of_plan():
    prefix = ledger(frame())
    extended = ledger(frame(future=True))
    assert main_plan(prefix) == main_plan(extended)


def test_invalid_or_missing_as_of_bar_never_uses_a_later_close():
    bad_frame = frame(future=True)
    bad_frame[2] = tuple(row for row in bad_frame[2] if row.key != "A")
    plan = compose(ledger(bad_frame), (TargetRequest("A", "50"),), settled_cash=100)
    exclusion = plan.exclusion_for("A")
    assert exclusion.reasons == ("no_bar_on_session",)
    assert exclusion.decision_close is None

    invalid_source = ledger(frame())
    invalid_plan = compose(invalid_source, (TargetRequest("I", "50"),), settled_cash=100)
    assert invalid_plan.exclusion_for("I").reasons == ("invalid_bar",)
    assert invalid_plan.exclusion_for("I").decision_close is None


def test_malformed_requests_and_cross_spec_inputs_reject():
    with pytest.raises(ValueError):
        TargetRequest("A", 0.1)
    with pytest.raises(ValueError):
        TargetRequest("A", "-1")
    with pytest.raises(ValueError, match="once"):
        compose(ledger(frame()), (TargetRequest("A", "10"), TargetRequest("A", "20")))
    with pytest.raises(ValueError, match="security_status"):
        compose(ledger(frame()), (TargetRequest("A", "10"),), security_status="unknownish")
    with pytest.raises(ValueError, match="allow_partial_adv"):
        compose(ledger(frame()), (TargetRequest("A", "10"),), allow_partial_adv=1)
    with pytest.raises(ValueError, match="Amounts must be exact decimals"):
        compose(ledger(frame()), (TargetRequest("A", "10"),), settled_cash=10.5)
    with pytest.raises(ValueError, match="momentum.price_basis"):
        universe, momentum, execution, costs, portfolio, valuation = specs()
        compose_targets(ledger(frame()), AS_OF, universe=universe,
                        momentum=MomentumSpec(lookback_sessions=2, min_observations=2,
                                              price_basis="adj_close"),
                        execution=execution, costs=costs, portfolio=portfolio,
                        valuation=valuation, requests=(TargetRequest("A", "10"),),
                        settled_cash=100, security_status="listed", allow_partial_adv=False)
    with pytest.raises(ValueError, match="unresolved key"):
        compose(ledger(frame()), (TargetRequest("A", "10"),), unresolved=("OTHER",))


def test_plan_and_intents_are_immutable_and_zero_allocation_is_not_a_target():
    plan = main_plan()
    with pytest.raises(FrozenInstanceError):
        plan.as_of = DAYS[1]
    with pytest.raises(FrozenInstanceError):
        plan.intent_for("A").allocated_notional = D("0")
    assert plan.intent_for("C").allocated_notional == 0
    assert "C" not in plan.target_keys


def test_nonempty_plan_is_runnable_as_fee_inclusive_session_buy_targets():
    from nse_pipeline.backtest.accounting_types import EventKind, EventTime, OpeningPrint, Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, InputProvenance, OpeningObservation,
        SessionInput, SessionRunner,
    )

    costs = CostSpec(brokerage_bps=100, brokerage_cap_per_order=2000,
                     stt_bps_buy=0, stt_bps_sell=0, stamp_bps_buy=0,
                     exchange_bps=0, gst_on_brokerage_pct=0)
    source_provenance = InputProvenance(
        "synthetic caller-supplied target fixture", "a" * 64,
        EventTime(AS_OF, Phase.CLOSE_VALUATION), "not authenticated; test input")
    runner_initial = InputProvenance(
        "synthetic initial cash fixture", "b" * 64,
        EventTime(AS_OF, Phase.PRE_OPEN_EVIDENCE), "not authenticated; test input")
    plan = compose(ledger(frame()), (TargetRequest("A", "1000"),), settled_cash=1000,
                   costs=costs, rate=0.5)
    target = plan.targets[0]
    assert target.allocated_notional == D("90.00")
    assert plan.sizing.fees == D("0.90")
    assert target.requested_quantity == 6
    session_runner = SessionRunner(
        "1000", on=AS_OF, initial_provenance=runner_initial,
        costs=costs, execution=specs(cash=1000, rate=0.5, costs=costs)[2])
    bar_record = next(row for row in frame()[2] if row.key == target.key)
    result = session_runner.step(SessionInput(
        AS_OF,
        InputProvenance("synthetic session date", "c" * 64,
                        EventTime(AS_OF, Phase.PRE_OPEN_EVIDENCE), "not authenticated"),
        close_bars=(CloseBar(bar_record, source_provenance),),
        buy_targets=(BuyTarget(
            "composed-A", target.key, target.requested_quantity,
            target.allocated_notional + plan.sizing.fees,
            CapacityInput(target.capacity_notional, source_provenance),
            target.rank, source_provenance),),
    ))
    assert result.next_orders[0].key == "A"
    assert result.next_orders[0].quantity == target.requested_quantity
    assert result.next_orders[0].reserved == D("90.90")
    assert result.next_orders[0].capacity_notional == target.capacity_notional
    next_on = DAYS[3]
    filled = session_runner.step(SessionInput(
        next_on,
        InputProvenance("next synthetic session", "c" * 64,
                        EventTime(next_on, Phase.PRE_OPEN_EVIDENCE), "test input"),
        opening_prints=(OpeningObservation(
            OpeningPrint("A", next_on, target.decision_close),
            InputProvenance("next opening", "d" * 64,
                            EventTime(next_on, Phase.OPEN_FILLS), "test input")),),
        close_bars=(CloseBar(
            bar("A", next_on, "16"),
            InputProvenance("next completed bar", "e" * 64,
                            EventTime(next_on, Phase.CLOSE_VALUATION), "test input")),)))
    fill = next(event for event in filled.events if event.kind == EventKind.BUY)
    assert fill.quantity == target.requested_quantity
    assert fill.amount == D("90.90")
    assert fill.fee == D("0.90")
    assert filled.next_orders == ()
    assert filled.reconciliation.passed


def test_p1_freezes_every_admissible_rank_when_one_is_explicitly_unresolved():
    plan = compose(
        ledger(frame()), (TargetRequest("A", "80"), TargetRequest("B", "50")),
        policy="P1_freeze_on_unresolved", unresolved=("B",), settled_cash=100)
    assert plan.order == ("A", "B")
    assert plan.sizing.filled == (D("0"), D("0"))
    assert plan.sizing.reasons == ("frozen_unresolved", "frozen_unresolved")
    assert plan.sizing.remaining_cash == D("100")
    assert plan.targets == ()


def test_p3_screen_value_is_capacity_clipped_and_missing_screen_is_explicit():
    requests = (TargetRequest("A", "80"), TargetRequest("B", "50"))
    marked = compose(
        ledger(frame()), requests, policy="P3_size_including_unresolved_at_screen",
        unresolved=("A",), marks=(ScreenMark("A", "1000", "20"),), settled_cash=1000)
    assert marked.intent_for("A").sizing_base_notional == D("100")
    assert marked.intent_for("A").allocated_notional == D("100")
    assert marked.intent_for("A").requested_quantity == 6
    missing = compose(
        ledger(frame()), requests, policy="P3_size_including_unresolved_at_screen",
        unresolved=("A",), settled_cash=100)
    assert missing.intent_for("A").allocated_notional == D("0")
    assert missing.intent_for("A").sizing_reason == "screen_value_unavailable"
    assert missing.target_keys == ("B",)
