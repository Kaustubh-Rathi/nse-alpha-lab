"""Batch 5 lifecycle: dated authoritative states, unknown by default, no synthetic disposal."""
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden/lifecycle_cases.json"
RECORDS = json.loads(GOLDEN.read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in RECORDS["cases"]}


def event(payload):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    return EventTime(date.fromisoformat(payload["session"]), Phase(payload["phase"]),
                     payload.get("sequence", 0))


def record(payload):
    from nse_pipeline.backtest.lifecycle import LifecycleRecord, LifecycleState
    return LifecycleRecord(
        security_key=payload["security_key"],
        state=LifecycleState(payload["state"]),
        effective_from=date.fromisoformat(payload["effective_from"]),
        available_at=event(payload["available_at"]),
        citation=payload["citation"],
        terminal_proceeds=(None if payload["terminal_proceeds"] is None
                           else Decimal(payload["terminal_proceeds"])),
        effective_to=(None if payload["effective_to"] is None
                      else date.fromisoformat(payload["effective_to"])),
        proceeds_verified=payload["proceeds_verified"])


@pytest.mark.parametrize("case_id", tuple(CASES))
def test_golden_lifecycle_cases_match_hand_derived_states(case_id):
    from nse_pipeline.backtest.lifecycle import LifecycleState, resolve_lifecycle
    case = CASES[case_id]
    status = resolve_lifecycle(tuple(record(row) for row in case["records"]),
                               case["query"]["security_key"],
                               date.fromisoformat(case["query"]["on"]),
                               decision_at=event(case["decision_at"]))
    expected = case["expected"]
    assert status.state == LifecycleState(expected["state"]), case_id
    assert status.reason == expected["reason"], case_id
    assert status.terminal_proceeds == (
        None if expected["terminal_proceeds"] is None else Decimal(expected["terminal_proceeds"]))
    assert status.effective_from == (None if expected["effective_from"] is None
                                     else date.fromisoformat(expected["effective_from"]))


def test_missing_bars_alone_never_establish_a_state():
    from nse_pipeline.backtest.evidence import ObservationLedger
    from nse_pipeline.backtest.lifecycle import LifecycleState, resolve_lifecycle
    from nse_pipeline.backtest.observations import SessionStream
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    stream = SessionStream()
    ledger = ObservationLedger(stream)
    for day in (date(2024, 6, 3), date(2024, 6, 4)):
        stream.observe(day)
        ledger.ingest(day, [])
    assert ledger.history("AAA", date(2024, 6, 4)).bar_observations == 0
    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    unknown = resolve_lifecycle((), "AAA", date(2024, 6, 4), decision_at=at)
    assert unknown.state == LifecycleState.UNKNOWN
    assert unknown.reason == "no_authoritative_evidence"

    listed = record(CASES["listed_evidence"]["records"][0])
    assert resolve_lifecycle((listed,), "AAA", date(2024, 6, 4),
                             decision_at=at).state == LifecycleState.LISTED


def test_unknown_state_retains_position_slot_and_blocks_nav_without_disposal():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.lifecycle import (
        LifecycleState, blocking_reason, require_negotiable, resolve_lifecycle)
    from nse_pipeline.backtest.valuation import Mark, value

    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    status = resolve_lifecycle((), "AAA", at.session, decision_at=at)
    assert status.state == LifecycleState.UNKNOWN and status.blocks_nav
    book = Portfolio("1000", on=at.session,
                     positions=(Position("p", "AAA", 10, date(2024, 5, 31),
                                         blocked_reason=blocking_reason(status)),))
    result = value(book.snapshot, at, marks=[Mark("p", "100", at.session, at)])
    assert result.nav is None
    assert tuple(p.position_id for p in book.snapshot.positions) == ("p",)
    assert book.positions["p"].shares == 10
    assert book.balances.cash == 1000 and book.snapshot.claims == ()
    with pytest.raises(ValueError, match="lifecycle_unknown"):
        require_negotiable(status)


def test_listed_state_does_not_block_nav():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase, Position
    from nse_pipeline.backtest.core import Portfolio
    from nse_pipeline.backtest.lifecycle import blocking_reason, resolve_lifecycle
    from nse_pipeline.backtest.valuation import Mark, value

    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    status = resolve_lifecycle((record(CASES["listed_evidence"]["records"][0]),),
                               "AAA", at.session, decision_at=at)
    assert blocking_reason(status) == ""
    book = Portfolio("1000", on=at.session,
                     positions=(Position("p", "AAA", 10, date(2024, 5, 31)),))
    assert value(book.snapshot, at, marks=[Mark("p", "100", at.session, at)]).nav == 2000


def test_terminal_proceeds_require_verified_terminal_evidence():
    from nse_pipeline.backtest.lifecycle import (
        LifecycleRecord, LifecycleState, require_terminal_proceeds, resolve_lifecycle)
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    base = record(CASES["delisted_without_verified_proceeds"]["records"][0])
    assert base.terminal_proceeds is None
    status = resolve_lifecycle((base,), "AAA", at.session, decision_at=at)
    assert status.terminal_proceeds is None
    with pytest.raises(ValueError, match="terminal_proceeds_unknown"):
        require_terminal_proceeds(status)

    with pytest.raises(ValueError):
        replace(base, terminal_proceeds=Decimal("99"))
    with pytest.raises(ValueError):
        replace(base, state=LifecycleState.LISTED, terminal_proceeds=Decimal("99"),
                proceeds_verified=True)
    with pytest.raises(ValueError):
        replace(base, terminal_proceeds=Decimal("99"), proceeds_verified=True,
                state=LifecycleState.SUSPENDED)
    verified = replace(base, terminal_proceeds=Decimal("123.45"), proceeds_verified=True)
    assert require_terminal_proceeds(
        resolve_lifecycle((verified,), "AAA", at.session, decision_at=at)) == Decimal("123.45")


def test_conflicting_same_date_states_raise_and_do_not_guess():
    from nse_pipeline.backtest.lifecycle import resolve_lifecycle
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    listed = record(CASES["listed_evidence"]["records"][0])
    suspended = record(CASES["suspension_supersedes_listing"]["records"][1])
    suspended = replace(suspended, effective_from=listed.effective_from)
    with pytest.raises(ValueError, match="conflicting_lifecycle_evidence"):
        resolve_lifecycle((listed, suspended), "AAA", date(2024, 6, 4),
                          decision_at=EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION))


def test_future_records_do_not_change_an_earlier_status():
    from nse_pipeline.backtest.lifecycle import resolve_lifecycle
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    listed = record(CASES["listed_evidence"]["records"][0])
    future = record(CASES["future_suspension_is_invisible"]["records"][1])
    decision = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    before = resolve_lifecycle((listed,), "AAA", date(2024, 6, 4), decision_at=decision)
    after = resolve_lifecycle((listed, future), "AAA", date(2024, 6, 4), decision_at=decision)
    assert before == after
    assert after.state.value == "listed"


def test_lifecycle_queries_require_explicit_typed_dates():
    from nse_pipeline.backtest.lifecycle import resolve_lifecycle
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    with pytest.raises(ValueError):
        resolve_lifecycle((), "AAA", "2024-06-04", decision_at=at)
    with pytest.raises(ValueError):
        resolve_lifecycle((), "AAA", at.session, decision_at="2024-06-04")
    with pytest.raises(ValueError):
        resolve_lifecycle((), "", at.session, decision_at=at)
