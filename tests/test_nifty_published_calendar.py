"""P0-BOUNDARY evidence adapter: exact classifications, never weekday inference."""
from datetime import date, timedelta

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.boundary import KNOWN_CALENDAR, RebalanceSchedule
from nse_pipeline.nifty.published_calendar import (
    HOLIDAY_LIST, SESSION_SERIES, SPECIAL_SESSION_NOTICE, CalendarSource,
    PublishedExchangeCalendar,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def weekdays(year, month, closures=()):
    on, out = date(year, month, 1), []
    while on.month == month:
        if on.weekday() < 5 and on not in closures:
            out.append(on)
        on += timedelta(days=1)
    return tuple(out)


MARCH_CLOSURES = (date(2024, 3, 8), date(2024, 3, 25), date(2024, 3, 29))
MARCH_SESSIONS = weekdays(2024, 3, MARCH_CLOSURES)

SESSION_SOURCE = CalendarSource(
    "NSE/index/2024-03", DIGEST_A, SESSION_SERIES, date(2024, 3, 28),
    EventTime(date(2024, 3, 28), Phase.CLOSE_ORDERS),
    date(2024, 3, 1), date(2024, 3, 31))
HOLIDAY_SOURCE = CalendarSource(
    "NSE/CMTR/2024", DIGEST_B, HOLIDAY_LIST, date(2023, 12, 12),
    EventTime(date(2023, 12, 12), Phase.CLOSE_AUDIT),
    date(2024, 1, 1), date(2024, 12, 31))


def source(citation, kind, digest, effective=(date(2024, 1, 1), date(2024, 12, 31)),
           published=date(2023, 12, 12), available=EventTime(date(2023, 12, 12), Phase.CLOSE_AUDIT)):
    return CalendarSource(citation, digest, kind, published, available,
                          effective[0], effective[1])


def evidence(**overrides):
    values = dict(
        citation="NSE/published-calendar/2024",
        sources=(SESSION_SOURCE, HOLIDAY_SOURCE),
        sessions=MARCH_SESSIONS,
        closures=MARCH_CLOSURES,
        special_sessions=(),
        special_session_sources=(),
        complete_months=((2024, 3),),
        coverage_start=date(2024, 3, 1),
        coverage_end=date(2024, 3, 31),
    )
    values.update(overrides)
    return PublishedExchangeCalendar(**values)


def test_complete_month_is_classified_and_emits_a_causal_monthly_calendar():
    manifest = evidence()
    calendars = manifest.to_monthly_calendars()
    assert len(calendars) == 1
    calendar = calendars[0]
    assert calendar.sessions == MARCH_SESSIONS
    assert calendar.complete_months == ((2024, 3),)
    assert calendar.special_sessions == ()
    assert calendar.available_at == EventTime(date(2024, 3, 28), Phase.CLOSE_ORDERS)
    assert len(calendar.source_sha256) == 64

    schedule = RebalanceSchedule([calendar])
    for on in MARCH_SESSIONS:
        schedule.observe(on)
    signals = [decision for decision in schedule.decisions if decision.signal]
    assert [(d.session, d.rebalance_month, d.basis, d.late) for d in signals] == [
        (date(2024, 3, 28), (2024, 3), KNOWN_CALENDAR, False)]


def test_retrospective_calendar_is_available_only_after_all_sources():
    manifest = evidence()
    retrospective = manifest.to_retrospective_calendar()
    assert retrospective.available_at == EventTime(date(2024, 3, 28), Phase.CLOSE_ORDERS)
    assert retrospective.coverage_end == date(2024, 3, 31)
    assert retrospective.complete_months == ((2024, 3),)


def test_monthly_calendar_refuses_a_source_not_available_at_month_end():
    late = CalendarSource(
        "NSE/index/2024", DIGEST_A, SESSION_SERIES, date(2024, 12, 31),
        EventTime(date(2024, 12, 31), Phase.CLOSE_AUDIT),
        date(2024, 1, 1), date(2024, 12, 31))
    with pytest.raises(ValueError, match="was not available at the month-end decision"):
        evidence(sources=(late, HOLIDAY_SOURCE)).to_monthly_calendars()


def test_unclassified_weekday_blocks_a_complete_month():
    missing = date(2024, 3, 27)
    with pytest.raises(ValueError, match=f"Unclassified weekday {missing}"):
        evidence(sessions=tuple(on for on in MARCH_SESSIONS if on != missing))


def test_a_date_cannot_be_both_session_and_closure():
    with pytest.raises(ValueError, match="both session and closure"):
        evidence(closures=MARCH_CLOSURES + (date(2024, 3, 28),))


def test_a_weekend_session_requires_an_explicit_special_session_citation():
    saturday = date(2024, 3, 30)
    with pytest.raises(ValueError, match="Uncited weekend session"):
        evidence(sessions=MARCH_SESSIONS + (saturday,))

    notice = CalendarSource(
        "NSE/CMTR/2024-special", DIGEST_C, SPECIAL_SESSION_NOTICE, date(2024, 3, 1),
        EventTime(date(2024, 3, 1), Phase.CLOSE_AUDIT),
        date(2024, 3, 30), date(2024, 3, 30))
    manifest = evidence(sessions=MARCH_SESSIONS + (saturday,),
                        special_sessions=(saturday,),
                        special_session_sources=((saturday, notice.citation),),
                        sources=(SESSION_SOURCE, HOLIDAY_SOURCE, notice))
    assert manifest.to_monthly_calendars()[0].special_sessions == (saturday,)


def test_special_session_must_be_listed_and_cite_a_covering_source():
    saturday = date(2024, 3, 30)
    notice = source("NSE/CMTR/2024-special", SPECIAL_SESSION_NOTICE, DIGEST_C,
                    effective=(date(2024, 3, 30), date(2024, 3, 30)))
    with pytest.raises(ValueError, match="Every special session"):
        evidence(sessions=MARCH_SESSIONS + (saturday,),
                 special_sessions=(saturday,),
                 sources=(SESSION_SOURCE, HOLIDAY_SOURCE, notice))
    with pytest.raises(ValueError, match="does not cover"):
        evidence(sessions=MARCH_SESSIONS + (saturday,),
                 special_sessions=(saturday,),
                 special_session_sources=((saturday, notice.citation),),
                 sources=(SESSION_SOURCE, HOLIDAY_SOURCE,
                          source("NSE/CMTR/2024-special", SPECIAL_SESSION_NOTICE, DIGEST_C,
                                 effective=(date(2024, 4, 1), date(2024, 4, 1)),
                                 published=date(2024, 3, 1),
                                 available=EventTime(date(2024, 3, 1), Phase.CLOSE_AUDIT))))


def test_every_session_and_closure_needs_a_matching_source_kind_and_interval():
    narrow = source("NSE/index/2024", SESSION_SERIES, DIGEST_A,
                    effective=(date(2024, 3, 1), date(2024, 3, 27)))
    with pytest.raises(ValueError, match="no session-series source"):
        evidence(sources=(narrow, HOLIDAY_SOURCE))
    narrow_holiday = source("NSE/CMTR/2024", HOLIDAY_LIST, DIGEST_B,
                            effective=(date(2024, 3, 1), date(2024, 3, 8)))
    with pytest.raises(ValueError, match="no holiday-list source"):
        evidence(sources=(SESSION_SOURCE, narrow_holiday))


def test_complete_month_must_lie_inside_declared_coverage():
    with pytest.raises(ValueError, match="outside the declared coverage"):
        evidence(coverage_start=date(2024, 3, 2))


def test_manifest_digest_changes_with_dates_and_month_digest_is_distinct():
    first = evidence()
    second = evidence(closures=MARCH_CLOSURES + (date(2024, 3, 28),),
                      sessions=tuple(on for on in MARCH_SESSIONS if on != date(2024, 3, 28)))
    assert first.manifest_sha256 != second.manifest_sha256
    assert first.manifest_sha256 == evidence().manifest_sha256
    monthly = first.to_monthly_calendars()[0]
    assert len(monthly.source_sha256) == 64
    assert monthly.source_sha256 == first.to_monthly_calendars()[0].source_sha256


APRIL_CLOSURES = (date(2024, 4, 11), date(2024, 4, 17))
APRIL_SESSIONS = weekdays(2024, 4, APRIL_CLOSURES)
APRIL_SOURCE = CalendarSource(
    "NSE/index/2024-04", DIGEST_C, SESSION_SERIES, date(2024, 4, 30),
    EventTime(date(2024, 4, 30), Phase.CLOSE_ORDERS),
    date(2024, 4, 1), date(2024, 4, 30))


def test_each_complete_month_emits_its_own_causally_available_calendar():
    manifest = evidence(
        sources=(SESSION_SOURCE, APRIL_SOURCE, HOLIDAY_SOURCE),
        sessions=MARCH_SESSIONS + APRIL_SESSIONS,
        closures=MARCH_CLOSURES + APRIL_CLOSURES,
        complete_months=((2024, 3), (2024, 4)),
        coverage_start=date(2024, 3, 1),
        coverage_end=date(2024, 4, 30),
    )
    march, april = manifest.to_monthly_calendars()
    assert march.available_at == EventTime(date(2024, 3, 28), Phase.CLOSE_ORDERS)
    assert april.available_at == EventTime(date(2024, 4, 30), Phase.CLOSE_ORDERS)
    assert march.coverage_end == date(2024, 3, 28)
    assert april.coverage_end == date(2024, 4, 30)
    assert march.months == ((2024, 3),) and april.months == ((2024, 4),)
    assert march.source_sha256 != april.source_sha256

    schedule = RebalanceSchedule([march, april])
    for on in MARCH_SESSIONS + APRIL_SESSIONS:
        schedule.observe(on)
    signals = [(d.session, d.rebalance_month, d.basis, d.citation, d.late)
               for d in schedule.decisions if d.signal]
    assert signals == [
        (date(2024, 3, 28), (2024, 3), KNOWN_CALENDAR, march.citation, False),
        (date(2024, 4, 30), (2024, 4), KNOWN_CALENDAR, april.citation, False),
    ]
    assert schedule.pending_month is None

    # The April calendar may not answer a March query: it becomes admissible
    # only at the April close, after the March decision is final.
    only_april = RebalanceSchedule([april])
    for on in MARCH_SESSIONS:
        only_april.observe(on)
    assert not any(d.basis == KNOWN_CALENDAR for d in only_april.decisions)
    assert not any(d.signal for d in only_april.decisions)
