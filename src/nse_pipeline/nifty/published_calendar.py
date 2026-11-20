"""Build a causal ``published_exchange`` calendar from bound dated evidence.

This adapter does not decide whether a date is a session by weekday arithmetic.
The caller must supply exact session, closure and special-session classifications,
each bound to at least one dated source artifact. The adapter validates that every
claimed complete month is fully classified, that no date is both a session and a
closure, and that every weekend session is explicitly cited as a special session.

For production use the adapter emits one :class:`~nse_pipeline.backtest.boundary.KnownCalendar`
per complete month. A month's calendar becomes admissible at the close of that
month's last evidenced session, so a month-end decision can use it without seeing
a later month. A single retrospective calendar is also available for diagnostics
and reconciliation, but it is not the production registration path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
import json

from nse_pipeline.backtest.accounting_types import EventTime, Phase, identifier, sha256_digest
from nse_pipeline.backtest.boundary import PUBLISHED_EXCHANGE, KnownCalendar

SESSION_SERIES = "published_session_series"
HOLIDAY_LIST = "published_holiday_list"
SPECIAL_SESSION_NOTICE = "published_special_session_notice"
SOURCE_KINDS = frozenset({SESSION_SERIES, HOLIDAY_LIST, SPECIAL_SESSION_NOTICE})


def _date(value, name: str) -> date:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")
    return value


def _month(value) -> tuple[int, int]:
    if type(value) not in (tuple, list) or len(value) != 2:
        raise ValueError("A complete month must be a (year, month) pair")
    year, month = value
    if type(year) is not int or type(month) is not int or year < 1 or not 1 <= month <= 12:
        raise ValueError("A complete month must be a valid (year, month) pair")
    return year, month


def _dates(values, name: str) -> tuple[date, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a collection of dates")
    try:
        frozen = tuple(_date(value, name) for value in values)
    except TypeError:
        raise ValueError(f"{name} must be a collection of dates") from None
    if len(set(frozen)) != len(frozen):
        raise ValueError(f"{name} must be unique")
    return tuple(sorted(frozen))


@dataclass(frozen=True)
class CalendarSource:
    """One dated artifact contributing session, closure or special-session evidence."""

    citation: str
    sha256: str
    kind: str
    published_at: date
    available_at: EventTime
    effective_from: date
    effective_to: date | None = None

    def __post_init__(self):
        identifier(self.citation)
        sha256_digest(self.sha256, "calendar source digest")
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"unknown calendar source kind: {self.kind!r}")
        _date(self.published_at, "published_at")
        _date(self.effective_from, "effective_from")
        if not isinstance(self.available_at, EventTime):
            raise ValueError("available_at must be a typed EventTime")
        if self.published_at > self.available_at.session:
            raise ValueError("A calendar source cannot become available before publication")
        if self.effective_to is not None:
            _date(self.effective_to, "effective_to")
            if self.effective_to < self.effective_from:
                raise ValueError("effective_to cannot precede effective_from")

    def covers(self, on: date) -> bool:
        return (self.effective_from <= on
                and (self.effective_to is None or on <= self.effective_to))


@dataclass(frozen=True)
class PublishedExchangeCalendar:
    """Exact dated classifications with complete-month and special-session proofs."""

    citation: str
    sources: tuple[CalendarSource, ...]
    sessions: tuple[date, ...]
    closures: tuple[date, ...]
    special_sessions: tuple[date, ...]
    special_session_sources: tuple[tuple[date, str], ...]
    complete_months: tuple[tuple[int, int], ...]
    coverage_start: date
    coverage_end: date

    def __post_init__(self):
        identifier(self.citation)
        if not isinstance(self.sources, tuple) or not self.sources:
            raise ValueError("At least one dated calendar source is required")
        if any(not isinstance(source, CalendarSource) for source in self.sources):
            raise ValueError("Every calendar source must be a CalendarSource")
        citations = [source.citation for source in self.sources]
        if len(set(citations)) != len(citations):
            raise ValueError("Calendar source citations must be unique")
        by_citation = {source.citation: source for source in self.sources}

        sessions = _dates(self.sessions, "sessions")
        closures = _dates(self.closures, "closures")
        special = _dates(self.special_sessions, "special_sessions")
        if not sessions:
            raise ValueError("A published calendar needs at least one session")
        if set(sessions) & set(closures):
            date_overlap = min(set(sessions) & set(closures))
            raise ValueError(f"A date cannot be both session and closure: {date_overlap}")
        if not set(special) <= set(sessions):
            raise ValueError("A special session must also be an evidenced session")

        start = _date(self.coverage_start, "coverage_start")
        end = _date(self.coverage_end, "coverage_end")
        if start > end:
            raise ValueError("coverage_start cannot follow coverage_end")
        if sessions[0] < start or sessions[-1] > end:
            raise ValueError("A session falls outside the declared coverage")
        if any(on < start or on > end for on in closures + special):
            raise ValueError("A closure or special session falls outside the declared coverage")

        complete = tuple(sorted({_month(value) for value in self.complete_months}))
        if not complete:
            raise ValueError("A published calendar needs at least one complete month")
        listed_months = {(on.year, on.month) for on in sessions}
        if not set(complete) <= listed_months:
            raise ValueError("A complete month must have evidenced sessions")

        special_pairs = []
        if isinstance(self.special_session_sources, (str, bytes)):
            raise ValueError("special_session_sources must be (date, citation) pairs")
        for pair in self.special_session_sources:
            if type(pair) not in (tuple, list) or len(pair) != 2:
                raise ValueError("special_session_sources must be (date, citation) pairs")
            on, citation = _date(pair[0], "special session date"), pair[1]
            if not isinstance(citation, str) or citation not in by_citation:
                raise ValueError("Every special session needs a known source citation")
            special_pairs.append((on, citation))
        if len({on for on, _ in special_pairs}) != len(special_pairs):
            raise ValueError("A special session may cite only one source in this manifest")
        if {on for on, _ in special_pairs} != set(special):
            raise ValueError("Every special session needs an explicit citation")
        if any(not by_citation[citation].covers(on) for on, citation in special_pairs):
            raise ValueError("A special-session citation does not cover its date")

        for on in sessions:
            if not any(source.kind == SESSION_SERIES and source.covers(on)
                       for source in self.sources):
                raise ValueError(f"Session {on} has no session-series source")
        for on in closures:
            if not any(source.kind == HOLIDAY_LIST and source.covers(on)
                       for source in self.sources):
                raise ValueError(f"Closure {on} has no holiday-list source")

        for year, month in complete:
            _validate_complete_month(year, month, set(sessions), set(closures),
                                     set(special), start, end)

        object.__setattr__(self, "sessions", sessions)
        object.__setattr__(self, "closures", closures)
        object.__setattr__(self, "special_sessions", special)
        object.__setattr__(self, "special_session_sources",
                           tuple(sorted(special_pairs, key=lambda pair: pair[0])))
        object.__setattr__(self, "complete_months", complete)
        object.__setattr__(self, "coverage_start", start)
        object.__setattr__(self, "coverage_end", end)

    @property
    def manifest_sha256(self) -> str:
        """Digest of the canonical, complete classification manifest."""
        return sha256(self._manifest(None)).hexdigest()

    def _manifest(self, month: tuple[int, int] | None) -> bytes:
        sessions = self.sessions
        closures = self.closures
        special = self.special_sessions
        months = self.complete_months
        sources = self.sources
        if month is not None:
            sessions = tuple(on for on in sessions if (on.year, on.month) == month)
            closures = tuple(on for on in closures if (on.year, on.month) == month)
            special = tuple(on for on in special if (on.year, on.month) == month)
            months = (month,)
            first = date(month[0], month[1], 1)
            following = date(month[0] + (month[1] == 12),
                             1 if month[1] == 12 else month[1] + 1, 1)
            sources = tuple(
                source for source in sources
                if source.effective_from < following
                and (source.effective_to is None or source.effective_to >= first))
        payload = {
            "schema_version": 1,
            "citation": self.citation,
            "sources": [
                {
                    "citation": source.citation,
                    "sha256": source.sha256,
                    "kind": source.kind,
                    "published_at": source.published_at.isoformat(),
                    "available_at": {
                        "session": source.available_at.session.isoformat(),
                        "phase": int(source.available_at.phase),
                        "sequence": source.available_at.sequence,
                    },
                    "effective_from": source.effective_from.isoformat(),
                    "effective_to": (source.effective_to.isoformat()
                                     if source.effective_to is not None else None),
                }
                for source in sources
            ],
            "sessions": [on.isoformat() for on in sessions],
            "closures": [on.isoformat() for on in closures],
            "special_sessions": [on.isoformat() for on in special],
            "special_session_sources": sorted(
                (on.isoformat(), citation) for on, citation in self.special_session_sources
                if month is None or (on.year, on.month) == month),
            "complete_months": [[year, value] for year, value in months],
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_monthly_calendars(self) -> tuple[KnownCalendar, ...]:
        """One authoritative calendar per complete month, admissible at its close."""
        calendars = []
        for month in self.complete_months:
            sessions = tuple(on for on in self.sessions if (on.year, on.month) == month)
            special = tuple(on for on in self.special_sessions if (on.year, on.month) == month)
            last = sessions[-1]
            available_at = EventTime(last, Phase.CLOSE_ORDERS)
            first = date(month[0], month[1], 1)
            following = date(month[0] + (month[1] == 12),
                             1 if month[1] == 12 else month[1] + 1, 1)
            for source in self.sources:
                if (source.effective_from < following
                        and (source.effective_to is None or source.effective_to >= first)
                        and source.available_at > available_at):
                    raise ValueError(
                        f"Calendar source {source.citation} for {month} was not "
                        "available at the month-end decision")
            digest = sha256(self._manifest(month)).hexdigest()
            calendars.append(KnownCalendar(
                citation=f"{self.citation}/{month[0]:04d}-{month[1]:02d}",
                available_at=available_at,
                sessions=sessions,
                authority=PUBLISHED_EXCHANGE,
                complete_months=(month,),
                special_sessions=special,
                source_sha256=digest,
                coverage_end=last,
            ))
        return tuple(calendars)

    def to_retrospective_calendar(self) -> KnownCalendar:
        """One diagnostic calendar; not the production registration path.

        Its availability is the latest source availability, so it cannot be used
        to claim a decision that predates the full evidence set.
        """
        available = max(source.available_at for source in self.sources)
        return KnownCalendar(
            citation=self.citation,
            available_at=available,
            sessions=self.sessions,
            authority=PUBLISHED_EXCHANGE,
            complete_months=self.complete_months,
            special_sessions=self.special_sessions,
            source_sha256=self.manifest_sha256,
            coverage_end=self.coverage_end,
        )


def _validate_complete_month(year: int, month: int, sessions: set[date], closures: set[date],
                             special: set[date], start: date, end: date) -> None:
    first = date(year, month, 1)
    following = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    on = first
    while on < following:
        if on < start or on > end:
            raise ValueError(f"Complete month {(year, month)} falls outside coverage")
        if on.weekday() < 5 and on not in sessions and on not in closures:
            raise ValueError(
                f"Unclassified weekday {on} in complete month {(year, month)}")
        if on.weekday() >= 5 and on in sessions and on not in special:
            raise ValueError(f"Uncited weekend session {on} in complete month {(year, month)}")
        on += timedelta(days=1)
    if not any((on.year, on.month) == (year, month) for on in sessions):
        raise ValueError(f"Complete month {(year, month)} has no evidenced session")
