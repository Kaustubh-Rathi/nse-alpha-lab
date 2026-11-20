"""Causal monthly signal sessions from observed sessions and admissible published calendars; no I/O or data loading."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date
import re

from .accounting_types import EventTime, Phase, identifier
from .observations import SessionStream

KNOWN_CALENDAR = "known_calendar"
OBSERVED_MONTH_CHANGE = "observed_month_change"

# D-11: only a dated, digest-bound published exchange artifact may fire a signal.
PUBLISHED_EXCHANGE = "published_exchange"
CALLER_ASSERTED = "caller_asserted"
INFERRED_WEEKDAY = "inferred_weekday"
DIAGNOSTIC_AUTHORITIES = frozenset({CALLER_ASSERTED, INFERRED_WEEKDAY})
CALENDAR_AUTHORITIES = frozenset({PUBLISHED_EXCHANGE}) | DIAGNOSTIC_AUTHORITIES

# D-12: skipped months are recorded, never synthesised into signals.
NO_OBSERVED_SESSIONS = "no_observed_sessions"
HISTORY_ENDED = "history_ended"
OMISSION_REASONS = frozenset({NO_OBSERVED_SESSIONS, HISTORY_ENDED})

# Diagnostic codes for a published calendar that cannot cover a month it claims.
CALENDAR_INCOMPLETE = "calendar_incomplete"
MONTH_NOT_COMPLETE = "month_not_complete"
OBSERVED_STREAM_GAP = "observed_stream_gap"
DIAGNOSTIC_CODES = frozenset({CALENDAR_INCOMPLETE, MONTH_NOT_COMPLETE, OBSERVED_STREAM_GAP})

_SHA256 = re.compile(r"[0-9a-f]{64}")


def month_of(on: date) -> tuple[int, int]:
    return on.year, on.month


def _session(on) -> date:
    if type(on) is not date:
        raise ValueError("Sessions must be datetime.date values")
    return on


def _month(value, label: str = "month") -> tuple[int, int]:
    if type(value) not in (tuple, list) or len(value) != 2:
        raise ValueError(f"A {label} must be a (year, month) pair")
    year, month = value
    if type(year) is not int or type(month) is not int or year < 1 or not 1 <= month <= 12:
        raise ValueError(f"A {label} must be a valid (year, month) pair")
    return year, month


def _months_between(after: tuple[int, int], before: tuple[int, int]):
    """Yield months strictly between two increasing month keys."""
    if after >= before:
        return
    year, month = after
    while True:
        month += 1
        if month == 13:
            year, month = year + 1, 1
        if (year, month) == before:
            return
        yield year, month


@dataclass(frozen=True)
class KnownCalendar:
    """A calendar artifact; only ``published_exchange`` is authoritative.

    Caller-asserted and inferred-weekday calendars may be constructed for diagnostics,
    but they can never be registered or fire a signal (D-11).
    """
    citation: str
    available_at: EventTime
    sessions: tuple[date, ...]
    authority: str | None = None
    complete_months: tuple[tuple[int, int], ...] = ()
    special_sessions: tuple[date, ...] = ()
    source_sha256: str | None = None
    coverage_end: date | None = None

    def __post_init__(self):
        if not isinstance(self.citation, str) or not self.citation.strip():
            raise ValueError("missing_calendar_citation: a nonempty citation is required")
        identifier(self.citation)
        if self.authority is None:
            raise ValueError("missing_calendar_citation: authority is required")
        if not isinstance(self.authority, str) or self.authority.strip().lower() not in CALENDAR_AUTHORITIES:
            raise ValueError("unknown_calendar_authority: expected published_exchange, caller_asserted or inferred_weekday")
        authority = self.authority.strip().lower()
        object.__setattr__(self, "authority", authority)
        if not isinstance(self.available_at, EventTime):
            raise ValueError("A calendar needs an explicit availability EventTime")
        if isinstance(self.sessions, (str, bytes)):
            raise ValueError("Calendar sessions must be a collection of dates")
        try:
            sessions = tuple(_session(on) for on in self.sessions)
        except TypeError:
            raise ValueError("Calendar sessions must be a collection of dates") from None
        if not sessions or len(set(sessions)) != len(sessions):
            raise ValueError("A calendar needs nonempty, unique trading dates")
        sessions = tuple(sorted(sessions))
        object.__setattr__(self, "sessions", sessions)

        if isinstance(self.special_sessions, (str, bytes)):
            raise ValueError("Special sessions must be a collection of dates")
        try:
            special = tuple(_session(on) for on in self.special_sessions)
        except TypeError:
            raise ValueError("Special sessions must be a collection of dates") from None
        if len(set(special)) != len(special):
            raise ValueError("Special sessions must be unique")
        special = tuple(sorted(special))
        if not set(special) <= set(sessions):
            raise ValueError("A special session must also be a listed trading date")
        object.__setattr__(self, "special_sessions", special)

        if isinstance(self.complete_months, (str, bytes)):
            raise ValueError("complete_months must be a collection of (year, month) pairs")
        try:
            complete = tuple(sorted({_month(value, "complete month") for value in self.complete_months}))
        except TypeError:
            raise ValueError("complete_months must be a collection of (year, month) pairs") from None
        object.__setattr__(self, "complete_months", complete)

        if self.source_sha256 is not None:
            if not isinstance(self.source_sha256, str) or not _SHA256.fullmatch(self.source_sha256):
                raise ValueError("source_sha256 must be a lowercase 64-character hex digest")
        if self.coverage_end is not None:
            if type(self.coverage_end) is not date:
                raise ValueError("coverage_end must be a date")
            if sessions[-1] > self.coverage_end:
                raise ValueError("coverage_end precedes a listed trading date")

        if authority == PUBLISHED_EXCHANGE:
            if self.coverage_end is None:
                raise ValueError("missing_calendar_citation: coverage_end is required")
            if self.source_sha256 is None:
                raise ValueError("missing_calendar_citation: source_sha256 is required")
            if not complete:
                raise ValueError("missing_calendar_citation: complete_months is required")
            listed = set(self.months)
            if not set(complete) <= listed:
                raise ValueError("A complete month must have listed trading dates")
            uncited = [on for on in sessions if on.weekday() >= 5 and on not in special]
            if uncited:
                raise ValueError(
                    f"missing_special_session_citation: {uncited[0]} is a weekend session; list it in special_sessions")

    @property
    def authoritative(self) -> bool:
        return self.authority == PUBLISHED_EXCHANGE

    def require_authoritative(self) -> None:
        if not self.authoritative:
            raise ValueError(
                f"calendar_not_authoritative: {self.authority} calendars are diagnostic and cannot be registered")

    @property
    def months(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted({month_of(on) for on in self.sessions}))

    def dates_in(self, month: tuple[int, int]) -> tuple[date, ...]:
        return tuple(on for on in self.sessions if month_of(on) == month)

    def last_session(self, month: tuple[int, int]) -> date:
        dates = self.dates_in(month)
        if not dates:
            raise ValueError(f"Calendar {self.citation} does not cover {month}")
        return dates[-1]

    def is_complete(self, month: tuple[int, int]) -> bool:
        return month in self.complete_months

    def unobserved_sessions(self, observed: tuple[date, ...]) -> tuple[date, ...]:
        """Published sessions strictly between consecutive observed sessions."""
        missing = []
        for before, after in zip(observed, observed[1:]):
            i = bisect_right(self.sessions, before)
            while i < len(self.sessions) and self.sessions[i] < after:
                missing.append(self.sessions[i])
                i += 1
        return tuple(missing)

    def contradictions(self, observed: tuple[date, ...]) -> tuple[tuple[date, tuple[int, int]], ...]:
        """Observed sessions in a covered month that this calendar does not list."""
        dates, months = set(self.sessions), set(self.months)
        return tuple((on, month_of(on)) for on in observed
                     if month_of(on) in months and on not in dates)

@dataclass(frozen=True)
class MonthOmission:
    """A month with no observed session (D-12), or the end-of-history gap."""
    month: tuple[int, int]
    reason: str
    first_observed_after: date | None
    calendar_coverage: tuple[str, ...] | None

    def __post_init__(self):
        _month(self.month, "omitted month")
        if self.reason not in OMISSION_REASONS:
            raise ValueError("Unknown month-omission reason")
        if self.calendar_coverage is not None:
            if (not isinstance(self.calendar_coverage, tuple) or not self.calendar_coverage
                    or any(not isinstance(citation, str) or not citation for citation in self.calendar_coverage)):
                raise ValueError("calendar_coverage must be None or a nonempty tuple of citations")
            if tuple(sorted(set(self.calendar_coverage))) != self.calendar_coverage:
                raise ValueError("calendar_coverage must be sorted and unique")
        if self.reason == NO_OBSERVED_SESSIONS:
            if type(self.first_observed_after) is not date:
                raise ValueError("A skipped month needs the first observed session after it")
            if self.month >= month_of(self.first_observed_after):
                raise ValueError("A skipped month must precede its first observed session")
        elif self.first_observed_after is not None:
            raise ValueError("An ended history has no later observed session")


@dataclass(frozen=True)
class CalendarDiagnostic:
    """Why a published calendar could not supply a month-end for an observed session."""
    session: date
    month: tuple[int, int]
    code: str
    citation: str
    detail: str

    def __post_init__(self):
        _session(self.session)
        _month(self.month)
        identifier(self.citation)
        if self.code not in DIAGNOSTIC_CODES:
            raise ValueError("Unknown calendar-diagnostic code")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("A calendar diagnostic needs a detail")


@dataclass(frozen=True)
class SignalDecision:
    """Phase-70 decision for one observed session; basis/citation explain the signal, else the session's month."""
    session: date
    decided_at: EventTime
    basis: str
    citation: str | None
    rebalance_month: tuple[int, int] | None
    month_last_session: date | None
    late: bool
    pending_month: tuple[int, int] | None = None

    def __post_init__(self):
        if self.pending_month is not None:
            _month(self.pending_month, "pending month")

    @property
    def signal(self) -> bool:
        return self.rebalance_month is not None


class RebalanceSchedule:
    """Exactly one monthly signal per observed month; decisions are final when made.

    Only published exchange calendars may be registered. Skipped months are recorded as
    ``MonthOmission`` diagnostics and never synthesis a signal (D-11, D-12).
    """

    def __init__(self, calendars=()):
        self._calendars: tuple[KnownCalendar, ...] = ()
        self._active: frozenset[str] = frozenset()
        self._signaled: frozenset[tuple[int, int]] = frozenset()
        self._decisions: tuple[SignalDecision, ...] = ()
        self._omissions: tuple[MonthOmission, ...] = ()
        self._history_ended: tuple[MonthOmission, ...] = ()
        self._diagnostics: tuple[CalendarDiagnostic, ...] = ()
        self._broken: frozenset[tuple[str, tuple[int, int]]] = frozenset()
        self._diagnosed: frozenset[tuple[str, tuple[int, int], str]] = frozenset()
        for calendar in calendars:
            self.add_calendar(calendar)

    @property
    def decisions(self) -> tuple[SignalDecision, ...]:
        return self._decisions

    @property
    def sessions(self) -> tuple[date, ...]:
        return tuple(decision.session for decision in self._decisions)

    @property
    def calendars(self) -> tuple[KnownCalendar, ...]:
        return self._calendars

    @property
    def omissions(self) -> tuple[MonthOmission, ...]:
        return self._omissions

    @property
    def history_ended(self) -> tuple[MonthOmission, ...]:
        return self._history_ended

    @property
    def diagnostics(self) -> tuple[CalendarDiagnostic, ...]:
        return self._diagnostics

    @property
    def pending_month(self) -> tuple[int, int] | None:
        return self._decisions[-1].pending_month if self._decisions else None

    def add_calendar(self, calendar: KnownCalendar) -> None:
        if not isinstance(calendar, KnownCalendar):
            raise ValueError("Expected a KnownCalendar")
        calendar.require_authoritative()
        if self._decisions and calendar.available_at <= self._decisions[-1].decided_at:
            raise ValueError("A calendar must be registered before any decision at which it was admissible")
        for other in self._calendars:
            if other.citation == calendar.citation:
                raise ValueError(f"Duplicate calendar citation {calendar.citation}")
            for month in set(other.months) & set(calendar.months):
                if other.dates_in(month) != calendar.dates_in(month):
                    raise ValueError(f"Calendars {other.citation} and {calendar.citation} disagree on {month}")
        self._calendars += (calendar,)

    def observe(self, on: date) -> SignalDecision:
        on = _session(on)
        history = self.sessions
        if history and on <= history[-1]:
            raise ValueError("Observed sessions must be strictly increasing")
        at = EventTime(on, Phase.CLOSE_ORDERS)
        month = month_of(on)
        prior_month = month_of(history[-1]) if history else None

        admissible = sorted((c for c in self._calendars if c.available_at <= at),
                            key=lambda c: (c.available_at, c.citation))
        broken = set(self._broken)
        diagnostics = list(self._diagnostics)
        diagnosed = set(self._diagnosed)

        def diagnose(calendar: KnownCalendar, observed: date, observed_month: tuple[int, int],
                     code: str, detail: str) -> None:
            key = (calendar.citation, observed_month, code)
            if key not in diagnosed:
                diagnosed.add(key)
                diagnostics.append(CalendarDiagnostic(observed, observed_month, code, calendar.citation, detail))

        for calendar in admissible:
            active = calendar.citation in self._active
            scope = history[-1:] if active else history
            scope += (on,)
            for missing in calendar.unobserved_sessions(scope)[:1]:
                diagnose(calendar, missing, month_of(missing), OBSERVED_STREAM_GAP,
                         f"{missing} listed in {calendar.citation} was not observed")
            for observed, observed_month in calendar.contradictions(scope):
                broken.add((calendar.citation, observed_month))
                diagnose(calendar, observed, observed_month, CALENDAR_INCOMPLETE,
                         f"{observed} observed but not listed in {calendar.citation}")
            for observed in scope:
                observed_month = month_of(observed)
                if observed_month in calendar.months and not calendar.is_complete(observed_month):
                    diagnose(calendar, observed, observed_month, MONTH_NOT_COMPLETE,
                             f"{calendar.citation} does not declare {observed_month} complete")

        covering = next((c for c in admissible
                         if month in c.months and c.is_complete(month)
                         and (c.citation, month) not in broken), None)
        fired = []
        if prior_month is not None and prior_month != month and prior_month not in self._signaled:
            fired.append(SignalDecision(on, at, OBSERVED_MONTH_CHANGE, None, prior_month, history[-1], True))
        if covering is not None and on == covering.last_session(month):
            fired.append(SignalDecision(on, at, KNOWN_CALENDAR, covering.citation, month, on, False))
        if len(fired) > 1:
            raise ValueError(f"Two month-ends recognised at the close of {on}; ambiguous rebalance")
        if fired and fired[0].rebalance_month in self._signaled:
            raise ValueError(f"Month {fired[0].rebalance_month} already signaled")

        signaled = self._signaled | {fired[0].rebalance_month} if fired else self._signaled
        pending = None if month in signaled else month
        decision = (replace(fired[0], pending_month=pending) if fired else SignalDecision(
            on, at, KNOWN_CALENDAR if covering else OBSERVED_MONTH_CHANGE,
            covering.citation if covering else None, None, None, False, pending))

        omissions = []
        if prior_month is not None and prior_month != month:
            for skipped in _months_between(prior_month, month):
                if skipped not in self._signaled:
                    coverage = tuple(sorted({c.citation for c in admissible
                                             if skipped in c.months})) or None
                    omissions.append(MonthOmission(
                        skipped, NO_OBSERVED_SESSIONS, on, coverage))

        self._decisions += (decision,)
        self._active |= {c.citation for c in admissible}
        self._signaled = signaled
        self._broken = frozenset(broken)
        self._diagnostics = tuple(diagnostics)
        self._diagnosed = frozenset(diagnosed)
        self._omissions += tuple(omissions)
        return decision

    def finalize(self) -> tuple[MonthOmission, ...]:
        """Report the end-of-history gap; never signal and never rewrite a decision."""
        month = self.pending_month
        if month is None or any(omission.month == month for omission in self._history_ended):
            return self._history_ended
        coverage = tuple(sorted({c.citation for c in self._calendars if month in c.months})) or None
        self._history_ended += (MonthOmission(month, HISTORY_ENDED, None, coverage),)
        return self._history_ended

    def catch_up(self, stream: SessionStream) -> tuple[SignalDecision, ...]:
        """Observe the stream's unseen sessions; all-or-nothing."""
        if not isinstance(stream, SessionStream):
            raise ValueError("Expected a SessionStream")
        seen = self.sessions
        if stream.sessions[:len(seen)] != seen:
            raise ValueError("Schedule history is not a prefix of the session stream")
        state = (self._active, self._signaled, self._decisions, self._omissions,
                 self._history_ended, self._diagnostics, self._broken, self._diagnosed)
        try:
            return tuple(self.observe(on) for on in stream.sessions[len(seen):])
        except ValueError:
            (self._active, self._signaled, self._decisions, self._omissions,
             self._history_ended, self._diagnostics, self._broken, self._diagnosed) = state
            raise
