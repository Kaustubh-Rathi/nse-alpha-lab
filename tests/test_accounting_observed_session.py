from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal as D

import pytest


def source(phase, *, citation="fixture document"):
    from nse_pipeline.backtest.accounting_types import EventTime
    from nse_pipeline.backtest.observed_session import InputProvenance
    return InputProvenance(citation, "a" * 64, EventTime(date(2024, 6, 3), phase), "supplied fixture; not authenticated")


def components():
    from nse_pipeline.specs import CostSpec, ExecutionSpec
    # Every modelled assumption is spelled out so no spec default is inherited silently.
    costs = CostSpec(brokerage_bps=100, brokerage_cap_per_order=1000, stt_bps_buy=0,
                     stt_bps_sell=0, stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0,
                     schedule_basis="flat_research_assumption",
                     monetary_rounding="paise_half_up_per_component")
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, fill_timing="next_session_open",
                              adverse_slippage_bps_buy=0, adverse_slippage_bps_sell=0,
                              sizing_participation_rate=0.05, max_participation_rate=0.05,
                              adv_window_sessions=20, max_order_notional=None,
                              min_order_notional=0.0, partial_fill_policy="partial",
                              settlement_funding="settled_cash_only", unfilled_remainder="expire",
                              circuit_limit_pct=0.20, hold_suspended_positions=True)
    return costs, execution


# The port fixtures below are synthetic regression tools, not historical evidence.
CALENDAR_SOURCE = b"hand-literal reviewed-input calendar fixture; not a historical source"
MEMBERSHIP_SOURCE = b"hand-literal reviewed-input membership fixture; not a historical source"


def calendar_fixture(sessions=(), holidays=(), as_of=date(2024, 6, 3),
                     decision=date(2024, 12, 31)):
    from hashlib import sha256
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.nifty.calendar_port import (
        CalendarAvailabilityEvidence, CalendarDocumentDeclaration, CalendarEvidencePort,
    )
    decl = CalendarDocumentDeclaration("NSE_CM", as_of, sha256(CALENDAR_SOURCE).hexdigest(),
                                       tuple(sessions), tuple(holidays))
    record = CalendarAvailabilityEvidence(
        "NSE_CM", as_of, EventTime(as_of, Phase.PRE_OPEN_EVIDENCE),
        "reviewer availability attestation", "fixture publication basis",
        decl.declared_source_sha256)
    return CalendarEvidencePort((decl,), {decl.identity: CALENDAR_SOURCE}, (record,),
                                decision_at=EventTime(decision, Phase.PRE_OPEN_EVIDENCE))


def membership_fixture(snapshots_by_day, key_map, decision=date(2024, 12, 31)):
    from hashlib import sha256
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.nifty.snapshots import Member, Snapshot
    from nse_pipeline.nifty.membership_port import (
        AvailabilityEvidence, SnapshotMembershipEvidence,
    )
    snapshots = tuple(
        Snapshot("NIFTY50", day, tuple(Member(symbol) for symbol in symbols), {
            "index_code": "NIFTY50", "as_of": day.isoformat(),
            "membership_kind": "dated_official_snapshot",
            "as_of_basis": "document_explicit_date", "as_of_evidence": "fixture date",
            "expected_security_count": len(symbols),
            "source_url": "https://www.niftyindices.com/reviewed-fixture.pdf",
            "source_sha256": sha256(MEMBERSHIP_SOURCE).hexdigest(),
            "retrieved_at_utc": "2024-06-05T12:00:00+00:00",
        }) for day, symbols in snapshots_by_day.items())
    records = [AvailabilityEvidence(
        "NIFTY50", day, EventTime(day, Phase.PRE_OPEN_EVIDENCE),
        "reviewer availability attestation", "fixture publication basis",
        sha256(MEMBERSHIP_SOURCE).hexdigest()) for day in snapshots_by_day]
    return SnapshotMembershipEvidence(
        snapshots, key_map,
        {("NIFTY50", day): MEMBERSHIP_SOURCE for day in snapshots_by_day},
        records, decision_at=EventTime(decision, Phase.PRE_OPEN_EVIDENCE),
        index_code="NIFTY50")


def test_close_reserves_fee_inclusive_explicit_target_as_immutable_next_session_order():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("1000", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    result = runner.step(SessionInput(
        day, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(CloseBar(Bar("AAA", "AAA", "EQ", day, "100", "101", "99", "100", 100,
                                 True, traded_value="10000"), source(Phase.CLOSE_VALUATION)),),
        buy_targets=(BuyTarget("order-a", "AAA", 10, "1000",
                               CapacityInput("1000", source(Phase.CLOSE_VALUATION)), 1,
                               source(Phase.CLOSE_VALUATION)),),
    ))

    assert result.next_orders[0].quantity == 9
    assert result.next_orders[0].reserved == D("909.00")
    assert result.next_orders[0].capacity_notional == D("1000.00")
    assert result.events[-1].amount == D("909.00")
    assert result.valuation.nav == D("1000.00")
    assert tuple(state.at.phase for state in result.phase_states) == tuple(Phase)
    with pytest.raises(FrozenInstanceError):
        result.next_orders[0].quantity = 10
    assert runner.result == (result,)


def test_opening_gap_and_same_open_sale_cannot_expand_frozen_buy_reserve():
    from nse_pipeline.backtest.accounting_types import OpeningPrint, Phase, Position
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, OpeningObservation, SellTarget,
        SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar
    from nse_pipeline.specs import CostSpec, ExecutionSpec

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs = CostSpec(brokerage_bps=0, stt_bps_buy=0, stt_bps_sell=0,
                     stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                              adverse_slippage_bps_sell=0)
    runner = SessionRunner("100", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("old", "AAA", 2, date(2024, 5, 31)),),
                           costs=costs, execution=execution)
    bars = (
        CloseBar(Bar("AAA", "AAA", "EQ", day1, "100", "101", "99", "100", 100, True),
                 source(Phase.CLOSE_VALUATION)),
        CloseBar(Bar("BBB", "BBB", "EQ", day1, "50", "51", "49", "50", 100, True),
                 source(Phase.CLOSE_VALUATION)),
    )
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE), close_bars=bars,
        buy_targets=(BuyTarget("buy-b", "BBB", 2, "100",
                               CapacityInput("1000", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
        sell_targets=(SellTarget("sell-a", "old", 2, 1, source(Phase.CLOSE_ORDERS)),),
    ))
    result = runner.step(SessionInput(
        day2, source(Phase.PRE_OPEN_EVIDENCE), opening_prints=(
            OpeningObservation(OpeningPrint("AAA", day2, "200"), source(Phase.OPEN_FILLS)),
            OpeningObservation(OpeningPrint("BBB", day2, "60"), source(Phase.OPEN_FILLS)),
        ),
    ))
    sell = next(event for event in result.events if event.kind.value == "sell")
    buy = next(event for event in result.events if event.kind.value == "buy")
    assert sell.at.phase == buy.at.phase == Phase.OPEN_FILLS
    assert result.events.index(sell) < result.events.index(buy)
    assert sell.amount == D("400.00") and buy.quantity == 1 and buy.amount == D("60.00")
    assert result.next_orders == ()
    assert runner.snapshot.balances.sale_receivable == D("400.00")
    assert runner.snapshot.balances.cash == D("100.00")
    assert runner.snapshot.balances.cash_available == D("40.00")


def test_opening_fill_cannot_use_close_or_intraday_bar_ohlcv_as_its_price():
    from nse_pipeline.backtest.accounting_types import OpeningPrint, Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, OpeningObservation, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("100", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(CloseBar(Bar("AAA", "AAA", "EQ", day1, "10", "999", "1", "10", 100, True),
                             source(Phase.CLOSE_VALUATION)),),
        buy_targets=(BuyTarget("buy-a", "AAA", 10, "100", CapacityInput("100", source(Phase.CLOSE_ORDERS)), 
                               1, source(Phase.CLOSE_ORDERS)),),
    ))
    result = runner.step(SessionInput(
        day2, source(Phase.PRE_OPEN_EVIDENCE),
        opening_prints=(OpeningObservation(OpeningPrint("AAA", day2, "60"), source(Phase.OPEN_FILLS)),),
        close_bars=(CloseBar(Bar("AAA", "AAA", "EQ", day2, "700", "900", "2", "800", 100, True),
                             source(Phase.CLOSE_VALUATION)),),
    ))
    buy = next(event for event in result.events if event.kind.value == "buy")
    assert buy.reference_price == D("60") and buy.price == D("60")
    assert result.valuation.nav == D("839.40")  # 800 close mark + 39.40 settled-cash residual


def test_phase_snapshots_do_not_claim_audit_time_as_book_accounting_time():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput, SessionRunner

    day = date(2024, 6, 3)
    costs, execution = components()
    result = SessionRunner("100", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution).step(
        SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE)))
    audit_wrapper = result.phase_states[-1]
    assert audit_wrapper.at.phase == Phase.CLOSE_AUDIT
    assert audit_wrapper.state.as_of.phase == Phase.PRE_OPEN_EVIDENCE


def test_invalid_or_wrong_phase_inputs_are_atomic_and_do_not_authenticate():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import (
        CloseBar, InputProvenance, OpeningObservation, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("100", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    bad_timing = InputProvenance("future close", "b" * 64,
                                 __import__("nse_pipeline.backtest.accounting_types", fromlist=["EventTime"]).EventTime(
                                     day, Phase.CLOSE_VALUATION), "caller disclosed; not authenticated")
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(ValueError, match="unavailable"):
        runner.step(SessionInput(day, bad_timing))
    from nse_pipeline.backtest.accounting_types import OpeningPrint
    with pytest.raises(ValueError, match="unavailable"):
        runner.step(SessionInput(
            day, source(Phase.PRE_OPEN_EVIDENCE),
            opening_prints=(OpeningObservation(OpeningPrint("AAA", day, "1"),
                                               source(Phase.CLOSE_VALUATION)),),
        ))
    assert (runner.snapshot, runner.events, runner.result) == before
    # Provenance assertions are intentionally limited to structure/timing; no
    # digest or citation is treated as authenticated or complete real-world data.
    valid = runner.step(SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE)))
    assert "not authenticate sources" in " ".join(valid.limitations)


def test_unavailable_future_provenance_is_not_exposed_in_earlier_result():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.observed_session import InputProvenance, SessionInput, SessionRunner

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("100", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    future = InputProvenance("future only", "c" * 64,
                             EventTime(day2, Phase.CLOSE_VALUATION), "not yet available")
    result = runner.step(SessionInput(day1, source(Phase.PRE_OPEN_EVIDENCE)))
    assert future not in result.consumed_provenance


def test_result_prefix_is_unchanged_when_future_sessions_are_appended():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import CloseBar, SessionInput, SessionRunner
    from nse_pipeline.backtest.observations import Bar

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()

    def runner():
        return SessionRunner("100", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                             costs=costs, execution=execution)

    first_input = SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(CloseBar(Bar("AAA", "AAA", "EQ", day1, "10", "11", "9", "10", 10, True),
                             source(Phase.CLOSE_VALUATION)),),
    )
    shorter, longer = runner(), runner()
    expected_prefix = shorter.step(first_input)
    actual_prefix = longer.step(first_input)
    longer.step(SessionInput(day2, source(Phase.PRE_OPEN_EVIDENCE)))
    assert actual_prefix == expected_prefix == longer.result[0]


def test_duplicate_order_ids_reject_atomically():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import BuyTarget, CapacityInput, SessionInput, SessionRunner

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("100", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    duplicate_a = BuyTarget("same", "AAA", 1, "10", CapacityInput("10", source(Phase.CLOSE_ORDERS)),
                            0, source(Phase.CLOSE_ORDERS))
    duplicate_b = BuyTarget("same", "BBB", 1, "10", CapacityInput("10", source(Phase.CLOSE_ORDERS)),
                            1, source(Phase.CLOSE_ORDERS))
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(ValueError, match="unique"):
        runner.step(SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE), buy_targets=(duplicate_a, duplicate_b)))
    assert (runner.snapshot, runner.events, runner.result) == before


def test_explicit_buy_targets_freeze_in_rank_then_key_then_order_id_order():
    from nse_pipeline.backtest.accounting_types import OpeningPrint, Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, OpeningObservation, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar
    from nse_pipeline.specs import CostSpec, ExecutionSpec

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs = CostSpec(brokerage_bps=0, stt_bps_buy=0, stt_bps_sell=0,
                     stamp_bps_buy=0, exchange_bps=0, gst_on_brokerage_pct=0)
    execution = ExecutionSpec(trade_settlement_lag_sessions=1, adverse_slippage_bps_buy=0,
                              adverse_slippage_bps_sell=0)
    runner = SessionRunner("300", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    targets = (
        BuyTarget("z-order", "AAA", 1, "100", CapacityInput("100", source(Phase.CLOSE_ORDERS)),
                  1, source(Phase.CLOSE_ORDERS)),
        BuyTarget("a-order", "AAA", 1, "100", CapacityInput("100", source(Phase.CLOSE_ORDERS)),
                  1, source(Phase.CLOSE_ORDERS)),
        BuyTarget("first-rank", "BBB", 1, "100", CapacityInput("100", source(Phase.CLOSE_ORDERS)),
                  0, source(Phase.CLOSE_ORDERS)),
    )
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=tuple(CloseBar(Bar(key, key, "EQ", day1, "100", "101", "99", "100", 10, True),
                                  source(Phase.CLOSE_VALUATION)) for key in ("AAA", "BBB")),
        buy_targets=targets,
    ))
    result = runner.step(SessionInput(
        day2, source(Phase.PRE_OPEN_EVIDENCE),
        opening_prints=tuple(OpeningObservation(OpeningPrint(key, day2, "100"), source(Phase.OPEN_FILLS))
                             for key in ("AAA", "BBB")),
    ))
    buys = [event.order_id for event in result.events if event.kind.value == "buy"]
    assert buys == ["first-rank", "a-order", "z-order"]


def test_missing_opening_print_rejects_entire_step_without_changing_runner_prefix():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    first = runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(CloseBar(Bar("AAA", "AAA", "EQ", day1, "100", "101", "99", "100", 100, True),
                             source(Phase.CLOSE_VALUATION)),),
        buy_targets=(BuyTarget("order-a", "AAA", 1, "101",
                               CapacityInput("100", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(ValueError, match="Missing opening print"):
        runner.step(SessionInput(day2, source(Phase.PRE_OPEN_EVIDENCE)))
    assert (runner.snapshot, runner.events, runner.result) == before == (runner.snapshot, runner.events, (first,))


def test_aggregate_sell_targets_over_one_positions_delivered_shares_reject_at_freeze():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        OpeningObservation, SellTarget, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.accounting_types import OpeningPrint

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("100", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("held", "AAA", 10, date(2024, 5, 31)),),
                           costs=costs, execution=execution)
    oversell = (SellTarget("sell-a", "held", 8, 0, source(Phase.CLOSE_ORDERS)),
                SellTarget("sell-b", "held", 8, 1, source(Phase.CLOSE_ORDERS)))
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(ValueError, match="exceed .*delivered shares"):
        runner.step(SessionInput(day1, source(Phase.PRE_OPEN_EVIDENCE), sell_targets=oversell))
    assert (runner.snapshot, runner.events, runner.result) == before
    # Individually valid siblings must still freeze once the aggregate conflict is rejected.
    result = runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        sell_targets=(SellTarget("sell-a", "held", 8, 0, source(Phase.CLOSE_ORDERS)),
                      SellTarget("sell-b", "held", 2, 1, source(Phase.CLOSE_ORDERS))),
    ))
    assert len(result.next_sells) == 2
    assert runner.step(SessionInput(
        day2, source(Phase.PRE_OPEN_EVIDENCE),
        opening_prints=(OpeningObservation(OpeningPrint("AAA", day2, "100"),
                                           source(Phase.OPEN_FILLS)),),
    )) is not None


def test_missing_opening_print_halt_is_explicit_evidence_failure_naming_the_security():
    from nse_pipeline.backtest.accounting_types import (
        OpeningPrint, Phase, Position,
    )
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, OpeningObservation, SellTarget,
        SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day1, day2, day3 = date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)
    costs, execution = components()
    runner = SessionRunner("1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("held", "ZZZ", 4, date(2024, 5, 31)),),
                           costs=costs, execution=execution)
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(CloseBar(Bar("AAA", "AAA", "EQ", day1, "100", "101", "99", "100", 100, True),
                             source(Phase.CLOSE_VALUATION)),),
        buy_targets=(BuyTarget("buy-aaa", "AAA", 1, "101",
                               CapacityInput("100", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
        sell_targets=(SellTarget("sell-held", "held", 1, 0, source(Phase.CLOSE_ORDERS)),),
    ))
    # The frozen sell names security ZZZ and the frozen buy names AAA; neither has a
    # supplied opening print on day2, so the step must halt rather than fabricate a fill.
    with pytest.raises(ValueError, match=r"Missing opening print for security ZZZ"):
        runner.step(SessionInput(day2, source(Phase.PRE_OPEN_EVIDENCE)))
    with pytest.raises(ValueError, match=r"security ZZZ[\s\S]*cannot fabricate a fill"):
        runner.step(SessionInput(day3, source(Phase.PRE_OPEN_EVIDENCE),
                                 opening_prints=(OpeningObservation(OpeningPrint("AAA", day3, "100"),
                                                                    source(Phase.OPEN_FILLS)),)))


def test_missing_opening_print_for_frozen_buy_halt_names_the_security():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(CloseBar(Bar("QQQ", "QQQ", "EQ", day1, "100", "101", "99", "100", 100, True),
                             source(Phase.CLOSE_VALUATION)),),
        buy_targets=(BuyTarget("buy-qqq", "QQQ", 1, "101",
                               CapacityInput("100", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    with pytest.raises(ValueError,
                       match=r"Missing opening print for security QQQ[\s\S]*cannot fabricate a fill"):
        runner.step(SessionInput(day2, source(Phase.PRE_OPEN_EVIDENCE)))


def test_unaffordable_lower_ranked_buy_target_rejects_the_whole_step_without_reranking():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("1000", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    bars = tuple(CloseBar(Bar(key, key, "EQ", day, "100", "101", "99", "100", 100, True),
                          source(Phase.CLOSE_VALUATION)) for key in ("AAA", "BBB"))
    affordable = BuyTarget("top-rank", "AAA", 9, "1000",
                           CapacityInput("1000", source(Phase.CLOSE_ORDERS)), 0,
                           source(Phase.CLOSE_ORDERS))
    unaffordable = BuyTarget("lower-rank", "BBB", 1, "1000",
                             CapacityInput("1000", source(Phase.CLOSE_ORDERS)), 1,
                             source(Phase.CLOSE_ORDERS))
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(ValueError, match="unaffordable at decision close"):
        runner.step(SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE), close_bars=bars,
                                 buy_targets=(affordable, unaffordable)))
    # The whole step is rejected: the affordable higher-ranked target is not silently kept.
    assert (runner.snapshot, runner.events, runner.result) == before
    assert runner.snapshot.orders == ()
    solo = runner.step(SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE), close_bars=bars,
                                    buy_targets=(affordable,)))
    assert [order.order_id for order in solo.next_orders] == ["top-rank"]


def test_date_due_claim_settles_at_phase_20_before_that_sessions_opening_fill():
    from nse_pipeline.backtest.accounting_types import (
        Claim, ClaimKind, Due, EventTime, OpeningPrint, Phase, Position,
    )
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, OpeningObservation, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner(
        "1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
        opening_claims=(Claim("dated-credit", ClaimKind.SALE, "50", "fixture credit",
                              Due(Phase.PRE_OPEN_SETTLEMENT, on=day2)),),
        opening_positions=(Position("held", "AAA", 1, date(2024, 5, 31)),),
        costs=costs, execution=execution,
    )
    close = CloseBar(Bar("AAA", "AAA", "EQ", day1, "100", "101", "99", "100", 100, True),
                     source(Phase.CLOSE_VALUATION))
    first = runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE), close_bars=(close,),
        buy_targets=(BuyTarget("b", "AAA", 1, "101", CapacityInput("100", source(Phase.CLOSE_VALUATION)), 
                               1, source(Phase.CLOSE_VALUATION)),),
    ))
    prior_events = first.events
    second = runner.step(SessionInput(
        day2, source(Phase.PRE_OPEN_EVIDENCE),
        opening_prints=(OpeningObservation(OpeningPrint("AAA", day2, "100"),
                                           source(Phase.OPEN_FILLS)),),
    ))

    settlement = next(e for e in second.events if e.claim_id == "dated-credit")
    fill = next(e for e in second.events if e.kind.value == "buy")
    assert settlement.at.phase == Phase.PRE_OPEN_SETTLEMENT and settlement.at.session == day2
    assert fill.at.phase == Phase.OPEN_FILLS and fill.at.session == day2
    assert second.events.index(settlement) < second.events.index(fill)
    assert first.events == prior_events


def test_evidence_enabled_runner_admits_calendar_session_and_membership_target():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("1000", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution,
                           calendar_evidence=calendar_fixture(sessions=(day,)),
                           membership_evidence=membership_fixture({day: ("AAA",)}, {"AAA": "AAA"}))
    result = runner.step(SessionInput(
        day, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(CloseBar(Bar("AAA", "AAA", "EQ", day, "100", "101", "99", "100", 100,
                                 True, traded_value="10000"), source(Phase.CLOSE_VALUATION)),),
        buy_targets=(BuyTarget("order-a", "AAA", 10, "1000",
                               CapacityInput("1000", source(Phase.CLOSE_VALUATION)), 1,
                               source(Phase.CLOSE_VALUATION)),),
    ))
    assert result.next_orders[0].quantity == 9
    assert result.expired_orders == ()
    joined = " ".join(result.limitations)
    assert "admitted only through the supplied reviewed calendar port" in joined
    assert "admitted only through the supplied reviewed membership port" in joined
    assert "not enforced for this run" not in joined


def test_evidence_enabled_runner_raises_coverage_unknown_for_unlisted_session_date():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput, SessionRunner
    from nse_pipeline.nifty.calendar_port import CalendarCoverageUnknown

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("100", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution,
                           calendar_evidence=calendar_fixture(sessions=(date(2024, 6, 4),)))
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(CalendarCoverageUnknown):
        runner.step(SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE)))
    assert before == (runner.snapshot, runner.events, ())


def test_evidence_enabled_runner_rejects_admitted_non_session_date():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput, SessionRunner

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("100", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution,
                           calendar_evidence=calendar_fixture(holidays=(day,)))
    with pytest.raises(ValueError, match="not an admitted exchange session"):
        runner.step(SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE)))
    assert runner.result == ()


def test_evidence_enabled_runner_propagates_membership_coverage_unknown():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, SessionInput, SessionRunner,
    )
    from nse_pipeline.nifty.membership_port import CoverageUnknown

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("1000", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution,
                           membership_evidence=membership_fixture(
                               {date(2024, 6, 4): ("AAA",)}, {"AAA": "AAA"}))
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(CoverageUnknown):
        runner.step(SessionInput(
            day, source(Phase.PRE_OPEN_EVIDENCE),
            buy_targets=(BuyTarget("order-a", "AAA", 1, "100",
                                   CapacityInput("100", source(Phase.CLOSE_ORDERS)), 1,
                                   source(Phase.CLOSE_ORDERS)),),
        ))
    assert (runner.snapshot, runner.events, runner.result) == before


def test_evidence_enabled_runner_rejects_admitted_false_membership_for_buy_and_sell_targets():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, SellTarget, SessionInput, SessionRunner,
    )

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("1000", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("held", "BBB", 4, date(2024, 5, 31)),),
                           costs=costs, execution=execution,
                           membership_evidence=membership_fixture(
                               {day: ("AAA",)}, {"AAA": "AAA", "BBB": "BBB"}))
    with pytest.raises(ValueError, match="outside the admitted membership snapshot"):
        runner.step(SessionInput(
            day, source(Phase.PRE_OPEN_EVIDENCE),
            buy_targets=(BuyTarget("order-b", "BBB", 1, "100",
                                   CapacityInput("100", source(Phase.CLOSE_ORDERS)), 1,
                                   source(Phase.CLOSE_ORDERS)),),
        ))
    with pytest.raises(ValueError, match="outside the admitted membership snapshot"):
        runner.step(SessionInput(
            day, source(Phase.PRE_OPEN_EVIDENCE),
            sell_targets=(SellTarget("sell-held", "held", 1, 0, source(Phase.CLOSE_ORDERS)),),
        ))
    assert runner.result == ()


def test_port_less_runner_discloses_calendar_and_membership_non_enforcement():
    from nse_pipeline.backtest.accounting_types import Phase
    from nse_pipeline.backtest.observed_session import SessionInput, SessionRunner

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("100", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    result = runner.step(SessionInput(day, source(Phase.PRE_OPEN_EVIDENCE)))
    joined = " ".join(result.limitations)
    assert "calendar admission is not enforced for this run" in joined
    assert "membership admission is not enforced for this run" in joined


def test_frozen_sell_without_print_plus_halt_evidence_expires_with_explicit_record():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.observed_session import (
        ExpiredOrder, HaltEvidence, SellTarget, SessionInput, SessionRunner,
    )

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("held", "ZZZ", 4, date(2024, 5, 31)),),
                           costs=costs, execution=execution)
    sell_prov = source(Phase.CLOSE_ORDERS)
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        sell_targets=(SellTarget("sell-held", "held", 1, 0, sell_prov),),
    ))
    halt_prov = source(Phase.OPEN_FILLS)
    result = runner.step(SessionInput(
        day2, source(Phase.PRE_OPEN_EVIDENCE),
        halt_evidence=(HaltEvidence("sell-held", halt_prov),),
    ))
    assert result.expired_orders == (
        ExpiredOrder("sell-held", "ZZZ", EventTime(day2, Phase.OPEN_FILLS, 0), True),)
    assert result.next_sells == ()
    assert not [e for e in result.events if e.kind.value in ("sell", "sell_skipped")]
    held = next(p for p in runner.snapshot.positions if p.position_id == "held")
    assert held.shares == 4 and held.delivered_shares == 4
    assert any(p is sell_prov for p in result.consumed_provenance)
    assert any(p is halt_prov for p in result.consumed_provenance)
    assert result.reconciliation.passed
    # The expired sell is gone: a later halt-free step no longer needs its evidence.
    assert runner.step(SessionInput(day2.replace(day=5), source(Phase.PRE_OPEN_EVIDENCE))) is not None


def test_frozen_buy_without_print_plus_halt_evidence_expires_via_kernel_cancel():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.observed_session import (
        BuyTarget, CapacityInput, CloseBar, ExpiredOrder, HaltEvidence,
        SessionInput, SessionRunner,
    )
    from nse_pipeline.backtest.observations import Bar

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           costs=costs, execution=execution)
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        close_bars=(CloseBar(Bar("QQQ", "QQQ", "EQ", day1, "100", "101", "99", "100", 100, True),
                             source(Phase.CLOSE_VALUATION)),),
        buy_targets=(BuyTarget("buy-qqq", "QQQ", 1, "101",
                               CapacityInput("100", source(Phase.CLOSE_ORDERS)), 1,
                               source(Phase.CLOSE_ORDERS)),),
    ))
    halt_prov = source(Phase.OPEN_FILLS)
    result = runner.step(SessionInput(
        day2, source(Phase.PRE_OPEN_EVIDENCE),
        halt_evidence=(HaltEvidence("buy-qqq", halt_prov),),
    ))
    assert result.expired_orders == (
        ExpiredOrder("buy-qqq", "QQQ", EventTime(day2, Phase.PRE_OPEN_ACTIONS, 0), True),)
    cancel = next(e for e in result.events if e.kind.value == "cancel")
    assert cancel.order_id == "buy-qqq" and cancel.at == EventTime(day2, Phase.PRE_OPEN_ACTIONS, 0)
    assert result.next_orders == ()
    assert not [e for e in result.events if e.kind.value in ("buy", "buy_skipped")]
    assert runner.snapshot.balances.order_reserved == D("0.00")
    assert runner.snapshot.balances.cash_available == D("1000.00")
    assert any(p is halt_prov for p in result.consumed_provenance)
    assert result.reconciliation.passed


def test_frozen_sell_without_print_and_without_halt_evidence_still_halts():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        SellTarget, SessionInput, SessionRunner,
    )

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("held", "ZZZ", 4, date(2024, 5, 31)),),
                           costs=costs, execution=execution)
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        sell_targets=(SellTarget("sell-held", "held", 1, 0, source(Phase.CLOSE_ORDERS)),),
    ))
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(ValueError, match=r"Missing opening print for security ZZZ[\s\S]*cannot fabricate a fill"):
        runner.step(SessionInput(day2, source(Phase.PRE_OPEN_EVIDENCE)))
    assert (runner.snapshot, runner.events, runner.result) == before


def test_halt_evidence_for_the_wrong_order_does_not_release_the_missing_print_halt():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        HaltEvidence, SellTarget, SessionInput, SessionRunner,
    )

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("held", "ZZZ", 4, date(2024, 5, 31)),),
                           costs=costs, execution=execution)
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        sell_targets=(SellTarget("sell-held", "held", 1, 0, source(Phase.CLOSE_ORDERS)),),
    ))
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(ValueError, match=r"Missing opening print for security ZZZ"):
        runner.step(SessionInput(
            day2, source(Phase.PRE_OPEN_EVIDENCE),
            halt_evidence=(HaltEvidence("other-order", source(Phase.OPEN_FILLS)),),
        ))
    assert (runner.snapshot, runner.events, runner.result) == before


def test_halt_evidence_without_a_missing_print_order_rejects_the_step():
    from nse_pipeline.backtest.accounting_types import Phase, Position
    from nse_pipeline.backtest.observed_session import (
        HaltEvidence, SessionInput, SessionRunner,
    )

    day = date(2024, 6, 3)
    costs, execution = components()
    runner = SessionRunner("100", on=day, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("held", "AAA", 4, date(2024, 5, 31)),),
                           costs=costs, execution=execution)
    before = runner.snapshot, runner.events, runner.result
    with pytest.raises(ValueError, match="must name a frozen order lacking its opening print"):
        runner.step(SessionInput(
            day, source(Phase.PRE_OPEN_EVIDENCE),
            halt_evidence=(HaltEvidence("no-such-order", source(Phase.OPEN_FILLS)),),
        ))
    assert (runner.snapshot, runner.events, runner.result) == before


def test_duplicate_and_future_halt_evidence_are_rejected_atomically():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.observed_session import (
        HaltEvidence, InputProvenance, SellTarget, SessionInput, SessionRunner,
    )

    day1, day2 = date(2024, 6, 3), date(2024, 6, 4)
    costs, execution = components()
    runner = SessionRunner("1000", on=day1, initial_provenance=source(Phase.PRE_OPEN_EVIDENCE),
                           opening_positions=(Position("held", "ZZZ", 4, date(2024, 5, 31)),),
                           costs=costs, execution=execution)
    runner.step(SessionInput(
        day1, source(Phase.PRE_OPEN_EVIDENCE),
        sell_targets=(SellTarget("sell-held", "held", 1, 0, source(Phase.CLOSE_ORDERS)),),
    ))
    before = runner.snapshot, runner.events, runner.result
    duplicate = HaltEvidence("sell-held", source(Phase.OPEN_FILLS))
    with pytest.raises(ValueError, match="Halt order identifiers must be unique"):
        runner.step(SessionInput(day2, source(Phase.PRE_OPEN_EVIDENCE),
                                 halt_evidence=(duplicate, duplicate)))
    late = InputProvenance("closed-later halt", "d" * 64,
                           EventTime(day2, Phase.CLOSE_ORDERS), "fixture; not authenticated")
    with pytest.raises(ValueError, match="unavailable"):
        runner.step(SessionInput(day2, source(Phase.PRE_OPEN_EVIDENCE),
                                 halt_evidence=(HaltEvidence("sell-held", late),)))
    assert (runner.snapshot, runner.events, runner.result) == before
