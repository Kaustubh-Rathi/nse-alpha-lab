"""P0-BOUNDARY: causal month-end recognition with hand-written NSE date fixtures."""
from datetime import date, datetime, timedelta

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.boundary import (
    KNOWN_CALENDAR, OBSERVED_MONTH_CHANGE, PUBLISHED_EXCHANGE, CalendarDiagnostic,
    KnownCalendar, MonthOmission, RebalanceSchedule, SignalDecision)
from nse_pipeline.backtest.observations import SessionStream


def weekdays(year, month, holidays=()):
    on, out = date(year, month, 1), []
    while on.month == month:
        if on.weekday() < 5 and on not in holidays:
            out.append(on)
        on += timedelta(days=1)
    return out


# Mahashivratri (8th), Holi (25th) and Good Friday (29th): last weekday 29 Mar is not a session.
MARCH_2024 = weekdays(2024, 3, {date(2024, 3, 8), date(2024, 3, 25), date(2024, 3, 29)})
DECEMBER_2024 = weekdays(2024, 12, {date(2024, 12, 25)})
APRIL_START = [date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)]


def close(on, sequence=0):
    return EventTime(on, Phase.CLOSE_ORDERS, sequence)


def calendar(sessions, available_at=EventTime(date(2023, 12, 15), Phase.CLOSE_AUDIT),
             citation="NSE/CMTR/2024-holidays", *, complete_months=None, special_sessions=()):
    sessions = tuple(sessions)
    months = tuple(sorted({(on.year, on.month) for on in sessions}))
    return KnownCalendar(
        citation, available_at, sessions,
        authority=PUBLISHED_EXCHANGE,
        complete_months=months if complete_months is None else complete_months,
        special_sessions=special_sessions,
        source_sha256="0" * 64,
        coverage_end=max(sessions))


def run(sessions, calendars=()):
    schedule = RebalanceSchedule(calendars)
    for on in sessions:
        schedule.observe(on)
    return schedule


def state(schedule):
    return (schedule.decisions, schedule.calendars, schedule.sessions, schedule.omissions,
            schedule.history_ended, schedule.diagnostics, schedule.pending_month)


def test_fixture_dates_are_hand_checked():
    assert len(MARCH_2024) == 18 and MARCH_2024[-1] == date(2024, 3, 28)
    assert date(2024, 3, 8) not in MARCH_2024 and date(2024, 3, 25) not in MARCH_2024
    assert DECEMBER_2024[-1] == date(2024, 12, 31) and len(DECEMBER_2024) == 21


def test_known_calendar_fires_on_last_trading_date_and_new_month_does_not_refire():
    schedule = run(MARCH_2024 + APRIL_START, [calendar(MARCH_2024)])
    signals = [d for d in schedule.decisions if d.signal]
    assert signals == [SignalDecision(date(2024, 3, 28), close(date(2024, 3, 28)), KNOWN_CALENDAR,
                                      "NSE/CMTR/2024-holidays", (2024, 3), date(2024, 3, 28), False, None)]
    assert schedule.decisions[0] == SignalDecision(date(2024, 3, 1), close(date(2024, 3, 1)), KNOWN_CALENDAR,
                                                   "NSE/CMTR/2024-holidays", None, None, False, (2024, 3))
    assert schedule.decisions[18] == SignalDecision(date(2024, 4, 1), close(date(2024, 4, 1)),
                                                    OBSERVED_MONTH_CHANGE, None, None, None, False, (2024, 4))


def test_fallback_fires_late_at_first_observed_session_of_next_month():
    schedule = run([date(2024, 3, 27), date(2024, 3, 28), date(2024, 4, 1), date(2024, 4, 2)])
    assert [d.signal for d in schedule.decisions] == [False, False, True, False]
    assert schedule.decisions[1] == SignalDecision(date(2024, 3, 28), close(date(2024, 3, 28)),
                                                   OBSERVED_MONTH_CHANGE, None, None, None, False, (2024, 3))
    assert schedule.decisions[2] == SignalDecision(date(2024, 4, 1), close(date(2024, 4, 1)),
                                                   OBSERVED_MONTH_CHANGE, None, (2024, 3), date(2024, 3, 28), True,
                                                   (2024, 4))


def test_year_boundary_under_both_bases():
    sessions = [date(2024, 12, 30), date(2024, 12, 31), date(2025, 1, 1), date(2025, 1, 2)]
    fallback = run(sessions).decisions
    assert [(d.session, d.rebalance_month, d.late) for d in fallback if d.signal] == [
        (date(2025, 1, 1), (2024, 12), True)]
    known = run(sessions, [calendar(DECEMBER_2024)]).decisions
    assert [(d.session, d.rebalance_month, d.basis, d.late) for d in known if d.signal] == [
        (date(2024, 12, 31), (2024, 12), KNOWN_CALENDAR, False)]


def test_month_without_observed_sessions_has_no_signal():
    schedule = run([date(2024, 1, 31), date(2024, 3, 1), date(2024, 4, 1)])
    assert [(d.session, d.rebalance_month) for d in schedule.decisions if d.signal] == [
        (date(2024, 3, 1), (2024, 1)), (date(2024, 4, 1), (2024, 3))]
    assert schedule.omissions == (
        MonthOmission((2024, 2), "no_observed_sessions", date(2024, 3, 1), None),)


def test_unobserved_months_emit_omission_records():
    schedule = run([date(2024, 1, 31), date(2024, 2, 29),
                    date(2024, 5, 31), date(2024, 6, 28)])
    assert [(d.session, d.rebalance_month) for d in schedule.decisions] == [
        (date(2024, 1, 31), None),
        (date(2024, 2, 29), (2024, 1)),
        (date(2024, 5, 31), (2024, 2)),
        (date(2024, 6, 28), (2024, 5)),
    ]
    assert schedule.omissions == (
        MonthOmission((2024, 3), "no_observed_sessions", date(2024, 5, 31), None),
        MonthOmission((2024, 4), "no_observed_sessions", date(2024, 5, 31), None),
    )
    assert [o.month for o in schedule.omissions] == [(2024, 3), (2024, 4)]
    assert all(d.rebalance_month not in {(2024, 3), (2024, 4)} for d in schedule.decisions)
    assert not {(2024, 3), (2024, 4)} & schedule._signaled
    assert schedule.pending_month == (2024, 6)
    assert [d.session for d in schedule.decisions if d.pending_month == (2024, 6)] == [date(2024, 6, 28)]


def test_pending_month_distinguishes_in_progress_from_signal():
    schedule = run([date(2024, 1, 31)])
    assert schedule.decisions[-1] == SignalDecision(
        date(2024, 1, 31), close(date(2024, 1, 31)), OBSERVED_MONTH_CHANGE,
        None, None, None, False, (2024, 1))
    assert schedule.pending_month == (2024, 1)
    assert schedule.omissions == ()

    schedule.observe(date(2024, 2, 15))
    assert schedule.decisions[-1] == SignalDecision(
        date(2024, 2, 15), close(date(2024, 2, 15)), OBSERVED_MONTH_CHANGE,
        None, (2024, 1), date(2024, 1, 31), True, (2024, 2))
    assert schedule.pending_month == (2024, 2)
    assert schedule.omissions == ()

    schedule.observe(date(2024, 5, 31))
    assert schedule.decisions[-1] == SignalDecision(
        date(2024, 5, 31), close(date(2024, 5, 31)), OBSERVED_MONTH_CHANGE,
        None, (2024, 2), date(2024, 2, 15), True, (2024, 5))
    assert schedule.pending_month == (2024, 5)
    assert [o.month for o in schedule.omissions] == [(2024, 3), (2024, 4)]
    assert not any(d.rebalance_month == (2024, 5) for d in schedule.decisions)

    before = schedule.decisions
    schedule.observe(date(2024, 6, 28))
    assert schedule.decisions[:3] == before
    assert schedule.decisions[-1].rebalance_month == (2024, 5)
    assert schedule.decisions[-1].late is True
    assert schedule.decisions[-1].pending_month == (2024, 6)
    assert schedule.pending_month == (2024, 6)
    assert not any(d.rebalance_month == (2024, 6) for d in schedule.decisions)
    assert [o.month for o in schedule.omissions] == [(2024, 3), (2024, 4)]


def test_only_published_calendar_may_register_or_fire():
    available = EventTime(date(2023, 12, 15), Phase.CLOSE_AUDIT)
    caller = KnownCalendar("caller/2024", available, MARCH_2024, authority="CALLER_ASSERTED")
    inferred = KnownCalendar("inferred/2024", available,
                             (date(2024, 3, 28), date(2024, 4, 30), date(2024, 5, 31)),
                             authority="inferred_weekday")
    assert caller.authority == "caller_asserted" and inferred.authority == "inferred_weekday"
    schedule = RebalanceSchedule()
    for diagnostic in (caller, inferred):
        with pytest.raises(ValueError, match="calendar_not_authoritative"):
            schedule.add_calendar(diagnostic)
        assert schedule.calendars == ()
    for on in MARCH_2024 + APRIL_START:
        assert schedule.observe(on).basis != KNOWN_CALENDAR
    assert not any(d.basis == KNOWN_CALENDAR for d in schedule.decisions)

    published = calendar(MARCH_2024)
    schedule = RebalanceSchedule([published])
    assert schedule.calendars == (published,)
    for on in MARCH_2024:
        schedule.observe(on)
    assert [(d.session, d.rebalance_month, d.basis, d.citation, d.late)
            for d in schedule.decisions if d.signal] == [
        (date(2024, 3, 28), (2024, 3), KNOWN_CALENDAR, "NSE/CMTR/2024-holidays", False)]

    evidence = dict(authority=PUBLISHED_EXCHANGE, complete_months=((2024, 3),),
                    special_sessions=(), source_sha256="0" * 64, coverage_end=date(2024, 3, 28))
    for missing in ("authority", "source_sha256", "coverage_end", "complete_months"):
        withheld = {key: value for key, value in evidence.items() if key != missing}
        with pytest.raises(ValueError, match="missing_calendar_citation"):
            KnownCalendar("NSE/CMTR/2024-holidays", available, MARCH_2024, **withheld)
    with pytest.raises(ValueError, match="missing_calendar_citation"):
        KnownCalendar("", available, MARCH_2024, **evidence)


def test_special_sessions_and_incomplete_months_require_explicit_citation():
    june = weekdays(2024, 6)
    saturday = date(2024, 6, 29)
    assert saturday.weekday() == 5
    with pytest.raises(ValueError, match="missing_special_session_citation"):
        calendar(june + [saturday], complete_months=((2024, 6),))

    cited = calendar(june + [saturday], citation="NSE/CMTR/2024-june-special",
                     special_sessions=(saturday,))
    schedule = run(june + [saturday], [cited])
    assert [(d.session, d.rebalance_month, d.basis, d.late)
            for d in schedule.decisions if d.signal] == [
        (saturday, (2024, 6), KNOWN_CALENDAR, False)]

    omitted = date(2024, 6, 17)
    incomplete = calendar([on for on in june if on != omitted], citation="NSE/CMTR/2024-june")
    schedule = run(june + [date(2024, 7, 1)], [incomplete])
    assert [(d.session, d.rebalance_month, d.basis, d.late)
            for d in schedule.decisions if d.signal] == [
        (date(2024, 7, 1), (2024, 6), OBSERVED_MONTH_CHANGE, True)]
    diagnostic = next(d for d in schedule.diagnostics if d.session == omitted)
    assert diagnostic == CalendarDiagnostic(
        omitted, (2024, 6), "calendar_incomplete", incomplete.citation,
        f"{omitted} observed but not listed in {incomplete.citation}")
    assert schedule.decisions[june.index(omitted)].basis == OBSERVED_MONTH_CHANGE
    assert not any(d.basis == KNOWN_CALENDAR for d in schedule.decisions[june.index(omitted):])

    late = run(june + [date(2024, 7, 1)])
    prior = late.decisions
    corrected = calendar(june, citation="NSE/CMTR/late-june",
                         available_at=EventTime(date(2024, 7, 2), Phase.PRE_OPEN_EVIDENCE))
    late.add_calendar(corrected)
    assert late.decisions == prior
    late.observe(date(2024, 7, 2))
    assert late.decisions[:-1] == prior
    assert late.decisions[-1].rebalance_month is None
    assert not any(d.rebalance_month == (2024, 6) and d.session > date(2024, 7, 1)
                   for d in late.decisions)
    assert not any(d.basis == KNOWN_CALENDAR for d in late.decisions)

    partial = calendar([date(2024, 5, 31)] + [on for on in june if on <= date(2024, 6, 27)],
                       citation="NSE/CMTR/partial-june", complete_months=((2024, 5),))
    ended = run(june, [partial])
    assert not any(d.signal for d in ended.decisions)
    assert ended.omissions == ()
    assert ended.decisions[-1].pending_month == (2024, 6)
    assert any(d.month == (2024, 6) and d.code == "month_not_complete" for d in ended.diagnostics)
    finalized = ended.finalize()
    assert finalized == (
        MonthOmission((2024, 6), "history_ended", None, ("NSE/CMTR/partial-june",)),)
    assert ended.history_ended == finalized
    assert ended.finalize() == finalized
    assert not any(d.signal for d in ended.decisions)


def test_stream_may_start_mid_month_under_a_calendar():
    schedule = run([date(2024, 3, 27), date(2024, 3, 28)], [calendar(MARCH_2024)])
    assert [d.signal for d in schedule.decisions] == [False, True]


def test_calendar_availability_boundary_is_close_orders_phase():
    sessions = [date(2024, 3, 27), date(2024, 3, 28), date(2024, 4, 1)]
    on_time = run(sessions, [calendar(MARCH_2024, close(date(2024, 3, 28)))]).decisions
    assert on_time[1].rebalance_month == (2024, 3) and on_time[1].basis == KNOWN_CALENDAR
    assert not on_time[2].signal
    for late_at in (close(date(2024, 3, 28), 1), EventTime(date(2024, 3, 28), Phase.CLOSE_AUDIT)):
        late = run(sessions, [calendar(MARCH_2024, late_at)]).decisions
        assert late[0].basis == late[1].basis == OBSERVED_MONTH_CHANGE and not late[1].signal
        assert late[2] == SignalDecision(date(2024, 4, 1), close(date(2024, 4, 1)), OBSERVED_MONTH_CHANGE,
                                         None, (2024, 3), date(2024, 3, 28), True, (2024, 4))


def test_calendar_published_mid_month_applies_from_its_availability():
    published = EventTime(date(2024, 3, 27), Phase.PRE_OPEN_EVIDENCE)
    decisions = run([date(2024, 3, 26), date(2024, 3, 27), date(2024, 3, 28)],
                    [calendar(MARCH_2024, published)]).decisions
    assert [d.basis for d in decisions] == [OBSERVED_MONTH_CHANGE, KNOWN_CALENDAR, KNOWN_CALENDAR]
    assert [d.signal for d in decisions] == [False, False, True]


def test_backdated_calendar_registration_is_rejected_atomically():
    schedule = run([date(2024, 3, 27)])
    before = state(schedule)
    for available in (close(date(2024, 3, 27)), EventTime(date(2024, 3, 1), Phase.PRE_OPEN_EVIDENCE)):
        with pytest.raises(ValueError, match="registered before"):
            schedule.add_calendar(calendar(MARCH_2024, available))
        assert state(schedule) == before
    schedule.add_calendar(calendar(MARCH_2024, EventTime(date(2024, 3, 27), Phase.CLOSE_AUDIT)))
    assert schedule.observe(date(2024, 3, 28)).rebalance_month == (2024, 3)


def test_prefix_stability_across_bases_and_late_calendar():
    sessions = (MARCH_2024[-3:] + APRIL_START + [date(2024, 4, 30), date(2024, 5, 2)]
                + DECEMBER_2024 + [date(2025, 1, 1)])
    calendars = [calendar(MARCH_2024, EventTime(date(2024, 3, 1), Phase.PRE_OPEN_EVIDENCE), "march"),
                 calendar(DECEMBER_2024, EventTime(date(2024, 12, 31), Phase.CLOSE_AUDIT), "december-late")]
    full = run(sessions, calendars).decisions
    assert [(d.session, d.rebalance_month, d.basis, d.late) for d in full if d.signal] == [
        (date(2024, 3, 28), (2024, 3), KNOWN_CALENDAR, False),
        (date(2024, 5, 2), (2024, 4), OBSERVED_MONTH_CHANGE, True),
        (date(2024, 12, 2), (2024, 5), OBSERVED_MONTH_CHANGE, True),
        (date(2025, 1, 1), (2024, 12), OBSERVED_MONTH_CHANGE, True)]
    without_calendars = run(sessions).decisions
    last_admissible_registration = sessions.index(date(2024, 12, 31)) + 1
    for k in range(len(sessions) + 1):
        assert run(sessions[:k], calendars).decisions == full[:k]
        assert run(sessions[:k]).decisions == without_calendars[:k]
        incremental = run(sessions[:k], [calendars[0]])
        if k <= last_admissible_registration:
            incremental.add_calendar(calendars[1])
            for on in sessions[k:]:
                incremental.observe(on)
            assert incremental.decisions == full
        else:
            with pytest.raises(ValueError, match="registered before"):
                incremental.add_calendar(calendars[1])


def test_observed_non_trading_day_marks_month_incomplete_and_never_fires():
    schedule = run([date(2024, 3, 7)], [calendar(MARCH_2024)])
    decision = schedule.observe(date(2024, 3, 8))
    assert decision == SignalDecision(date(2024, 3, 8), close(date(2024, 3, 8)), OBSERVED_MONTH_CHANGE,
                                      None, None, None, False, (2024, 3))
    assert CalendarDiagnostic(date(2024, 3, 8), (2024, 3), "calendar_incomplete",
                              "NSE/CMTR/2024-holidays",
                              "2024-03-08 observed but not listed in NSE/CMTR/2024-holidays") in schedule.diagnostics
    assert not schedule.observe(date(2024, 3, 11)).signal
    assert schedule.decisions[0].basis == KNOWN_CALENDAR
    assert all(d.basis != KNOWN_CALENDAR for d in schedule.decisions[1:])


def test_session_after_declared_last_date_marks_month_incomplete_and_never_refires():
    schedule = run(MARCH_2024[-2:], [calendar(MARCH_2024)])
    before = state(schedule)
    decision = schedule.observe(date(2024, 3, 29))
    assert decision == SignalDecision(date(2024, 3, 29), close(date(2024, 3, 29)), OBSERVED_MONTH_CHANGE,
                                      None, None, None, False, None)
    assert state(schedule)[0][:-1] == before[0]
    assert [(d.session, d.rebalance_month) for d in schedule.decisions if d.signal] == [
        (date(2024, 3, 28), (2024, 3))]
    assert any(d.session == date(2024, 3, 29) and d.code == "calendar_incomplete"
               for d in schedule.diagnostics)


def test_skipped_calendar_date_is_diagnosed_without_losing_the_month_end():
    schedule = run([date(2024, 3, 26)], [calendar(MARCH_2024)])
    decision = schedule.observe(date(2024, 3, 28))
    assert decision.rebalance_month == (2024, 3) and decision.basis == KNOWN_CALENDAR
    assert CalendarDiagnostic(date(2024, 3, 27), (2024, 3), "observed_stream_gap",
                              "NSE/CMTR/2024-holidays",
                              "2024-03-27 listed in NSE/CMTR/2024-holidays was not observed") in schedule.diagnostics
    assert not schedule.observe(date(2024, 4, 1)).signal
    assert [d.rebalance_month for d in schedule.decisions if d.signal] == [(2024, 3)]


def test_skipping_a_whole_calendar_month_is_recorded_as_an_omission():
    schedule = run([date(2024, 2, 29)], [calendar(MARCH_2024)])
    decision = schedule.observe(date(2024, 4, 1))
    assert decision.rebalance_month == (2024, 2) and decision.late is True
    assert schedule.sessions == (date(2024, 2, 29), date(2024, 4, 1))
    assert schedule.omissions == (
        MonthOmission((2024, 3), "no_observed_sessions", date(2024, 4, 1),
                      ("NSE/CMTR/2024-holidays",)),)
    assert any(d.month == (2024, 3) and d.code == "observed_stream_gap"
               for d in schedule.diagnostics)


def test_newly_admissible_calendar_contradicting_history_marks_month_incomplete():
    late = calendar(MARCH_2024, EventTime(date(2024, 3, 8), Phase.CLOSE_AUDIT))
    schedule = run([date(2024, 3, 7), date(2024, 3, 8)], [late])
    decision = schedule.observe(date(2024, 3, 11))
    assert decision == SignalDecision(date(2024, 3, 11), close(date(2024, 3, 11)), OBSERVED_MONTH_CHANGE,
                                      None, None, None, False, (2024, 3))
    assert any(d.session == date(2024, 3, 8) and d.code == "calendar_incomplete"
               for d in schedule.diagnostics)
    assert all(d.basis != KNOWN_CALENDAR for d in schedule.decisions)


def test_two_month_ends_at_one_close_are_ambiguous():
    single = calendar([date(2024, 5, 31)], citation="may-single-session")
    schedule = run([date(2024, 4, 30)], [single])
    before = state(schedule)
    with pytest.raises(ValueError, match="ambiguous"):
        schedule.observe(date(2024, 5, 31))
    assert state(schedule) == before


def test_conflicting_or_duplicate_calendars_are_rejected():
    schedule = RebalanceSchedule([calendar(MARCH_2024)])
    with pytest.raises(ValueError, match="disagree"):
        schedule.add_calendar(calendar(MARCH_2024 + [date(2024, 3, 29)], citation="other"))
    with pytest.raises(ValueError, match="Duplicate"):
        schedule.add_calendar(calendar(DECEMBER_2024))
    schedule.add_calendar(calendar(reversed(MARCH_2024), EventTime(date(2024, 3, 1), Phase.CLOSE_AUDIT), "copy"))
    schedule.add_calendar(calendar(DECEMBER_2024, citation="december"))
    assert [c.citation for c in schedule.calendars] == ["NSE/CMTR/2024-holidays", "copy", "december"]
    assert run([date(2024, 3, 28)], schedule.calendars).decisions[0].citation == "NSE/CMTR/2024-holidays"


@pytest.mark.parametrize("on", (date(2024, 3, 28), date(2024, 3, 27), datetime(2024, 3, 29), "2024-03-29"))
def test_invalid_or_non_increasing_sessions_are_rejected(on):
    schedule = run([date(2024, 3, 28)])
    before = state(schedule)
    with pytest.raises(ValueError):
        schedule.observe(on)
    assert state(schedule) == before


@pytest.mark.parametrize("citation,available,sessions,overrides", (
    ("", close(date(2024, 1, 1)), MARCH_2024, {}),
    ("c", date(2024, 1, 1), MARCH_2024, {}),
    ("c", close(date(2024, 1, 1)), (), {}),
    ("c", close(date(2024, 1, 1)), MARCH_2024 + MARCH_2024[:1], {}),
    ("c", close(date(2024, 1, 1)), [datetime(2024, 3, 1)], {}),
    ("c", close(date(2024, 1, 1)), "2024-03-01", {}),
    ("c", close(date(2024, 1, 1)), None, {}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"authority": None}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"authority": "exchange-ish"}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"source_sha256": None}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"source_sha256": "not-a-digest"}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"coverage_end": None}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"coverage_end": date(2024, 3, 27)}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"complete_months": ()}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"complete_months": ((2024, 4),)}),
    ("c", close(date(2024, 1, 1)), MARCH_2024, {"special_sessions": (date(2024, 3, 29),)}),
    ("c", close(date(2024, 1, 1)), (date(2024, 6, 29),), {"complete_months": ((2024, 6),),
                                                          "coverage_end": date(2024, 6, 29)}),
))
def test_invalid_calendars_are_rejected(citation, available, sessions, overrides):
    evidence = dict(authority=PUBLISHED_EXCHANGE, complete_months=((2024, 3),),
                    special_sessions=(), source_sha256="0" * 64, coverage_end=date(2024, 3, 28))
    with pytest.raises(ValueError):
        KnownCalendar(citation, available, sessions, **{**evidence, **overrides})


def test_diagnostic_calendars_need_no_published_evidence_but_cannot_register():
    diagnostic = KnownCalendar("caller/2024", EventTime(date(2023, 12, 15), Phase.CLOSE_AUDIT),
                               MARCH_2024, authority="caller_asserted")
    assert diagnostic.authority == "caller_asserted" and not diagnostic.authoritative
    assert diagnostic.complete_months == () and diagnostic.source_sha256 is None
    with pytest.raises(ValueError, match="calendar_not_authoritative"):
        RebalanceSchedule([diagnostic])


def test_calendar_normalises_order_and_reports_months():
    cal = calendar(list(reversed(MARCH_2024)) + DECEMBER_2024)
    assert cal.sessions[0] == date(2024, 3, 1) and cal.months == ((2024, 3), (2024, 12))
    assert cal.last_session((2024, 12)) == date(2024, 12, 31)
    with pytest.raises(ValueError):
        cal.last_session((2024, 4))
    with pytest.raises(ValueError):
        RebalanceSchedule().add_calendar(object())


def test_catch_up_from_session_stream_is_all_or_nothing():
    stream = SessionStream(MARCH_2024[-2:] + APRIL_START[:1])
    schedule = RebalanceSchedule([calendar(MARCH_2024)])
    assert schedule.catch_up(stream) == run(stream.sessions, [calendar(MARCH_2024)]).decisions
    stream.observe(date(2024, 4, 2))
    assert [d.session for d in schedule.catch_up(stream)] == [date(2024, 4, 2)]
    assert schedule.catch_up(stream) == ()
    with pytest.raises(ValueError, match="prefix"):
        schedule.catch_up(SessionStream([date(2024, 3, 27), date(2024, 4, 2)]))
    bad = RebalanceSchedule([calendar([date(2024, 5, 31)], citation="may-single")])
    before = state(bad)
    with pytest.raises(ValueError, match="ambiguous"):
        bad.catch_up(SessionStream([date(2024, 4, 30), date(2024, 5, 31)]))
    assert state(bad) == before
    with pytest.raises(ValueError):
        bad.catch_up([date(2024, 3, 27)])
