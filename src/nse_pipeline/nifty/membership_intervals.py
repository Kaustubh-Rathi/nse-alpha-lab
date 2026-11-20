"""Point-in-time NIFTY 500 membership intervals with explicit coverage windows.

A constituent interval proves that a symbol was a member on each date in its
effective range. Absence from the interval set is *not* evidence of non-membership
unless the query date lies inside a separate, digest-bound coverage window that
certifies the change set is complete for that range. Outside every admissible
window the port raises :class:`CoverageUnknown`; it never forward-fills a snapshot,
uses current constituents, or infers membership from prices.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType

from nse_pipeline.backtest.accounting_types import EventTime, identifier, sha256_digest
from nse_pipeline.nifty.membership_port import CoverageUnknown
from nse_pipeline.nifty.snapshots import SYMBOL

SNAPSHOT_EVIDENCE = "published_constituent_snapshot"
CHANGE_NOTICE_EVIDENCE = "published_change_notice"
EVIDENCE_KINDS = frozenset({SNAPSHOT_EVIDENCE, CHANGE_NOTICE_EVIDENCE})


def _date(value, name: str) -> date:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")
    return value


def _window(value, name: str) -> tuple[date, date]:
    if type(value) not in (tuple, list) or len(value) != 2:
        raise ValueError(f"{name} must be an inclusive (first, last) date pair")
    first, last = _date(value[0], name), _date(value[1], name)
    if first > last:
        raise ValueError(f"{name} cannot end before it starts")
    return first, last


@dataclass(frozen=True)
class MembershipInterval:
    """One symbol's evidenced membership interval; endpoints are inclusive."""

    symbol: str
    effective_from: date
    effective_to: date
    available_at: EventTime
    citation: str
    source_sha256: str
    evidence_kind: str

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not SYMBOL.fullmatch(self.symbol):
            raise ValueError("membership interval requires a valid exact symbol")
        first, last = _window((self.effective_from, self.effective_to), "interval")
        if not isinstance(self.available_at, EventTime):
            raise ValueError("available_at must be a typed EventTime")
        identifier(self.citation)
        sha256_digest(self.source_sha256, "membership source digest")
        if self.evidence_kind not in EVIDENCE_KINDS:
            raise ValueError(f"unknown membership evidence kind: {self.evidence_kind!r}")
        object.__setattr__(self, "effective_from", first)
        object.__setattr__(self, "effective_to", last)

    def covers(self, on: date) -> bool:
        return self.effective_from <= on <= self.effective_to


@dataclass(frozen=True)
class MembershipCoverageWindow:
    """A range in which the complete change set is evidenced, not merely sampled."""

    effective_from: date
    effective_to: date
    available_at: EventTime
    citation: str
    source_sha256: str

    def __post_init__(self):
        first, last = _window((self.effective_from, self.effective_to), "coverage window")
        if not isinstance(self.available_at, EventTime):
            raise ValueError("available_at must be a typed EventTime")
        identifier(self.citation)
        sha256_digest(self.source_sha256, "coverage source digest")
        object.__setattr__(self, "effective_from", first)
        object.__setattr__(self, "effective_to", last)

    def covers(self, on: date) -> bool:
        return self.effective_from <= on <= self.effective_to


class IntervalMembershipEvidence:
    """Answer point-in-time membership only inside complete evidenced windows."""

    def __init__(self, intervals: Sequence[MembershipInterval],
                 coverage_windows: Sequence[MembershipCoverageWindow],
                 key_to_symbol: Mapping[str, str], *, decision_at: EventTime):
        if (not isinstance(intervals, Sequence) or isinstance(intervals, (str, bytes))
                or not intervals or not all(isinstance(row, MembershipInterval) for row in intervals)):
            raise ValueError("at least one MembershipInterval is required")
        if (not isinstance(coverage_windows, Sequence) or isinstance(coverage_windows, (str, bytes))
                or not coverage_windows
                or not all(isinstance(row, MembershipCoverageWindow) for row in coverage_windows)):
            raise ValueError("at least one MembershipCoverageWindow is required")
        if not isinstance(decision_at, EventTime):
            raise ValueError("decision_at must be an explicit EventTime")
        if not isinstance(key_to_symbol, Mapping) or not key_to_symbol:
            raise ValueError("an exact key-to-symbol mapping is required")

        frozen_map, symbols = {}, set()
        for key, symbol in key_to_symbol.items():
            if not isinstance(key, str) or not key or not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
                raise ValueError("key mapping requires nonempty exact keys and valid symbols")
            if symbol in symbols:
                raise ValueError("key mapping symbol collision")
            frozen_map[key] = symbol
            symbols.add(symbol)

        ordered_windows = tuple(sorted(
            (row for row in coverage_windows if row.available_at <= decision_at),
            key=lambda row: (row.effective_from, row.effective_to, row.citation)))
        for before, after in zip(ordered_windows, ordered_windows[1:]):
            if after.effective_from <= before.effective_to:
                raise ValueError("membership coverage windows overlap")
        # Evidence unavailable at the bound decision is invisible: it may neither
        # answer a query, be exposed by this port, nor make construction fail.
        # Only intervals the decision could actually see are checked against the
        # windows that certify them.
        visible = tuple(row for row in intervals if row.available_at <= decision_at)
        if not visible:
            raise ValueError("no membership interval is available at the bound decision")
        for row in ordered_windows:
            if row.available_at > decision_at:
                continue
            for interval in visible:
                if (row.effective_from <= interval.effective_from
                        and interval.effective_to <= row.effective_to
                        and interval.available_at > row.available_at):
                    raise ValueError(
                        "an interval cannot become available after the window that certifies it")

        by_symbol: dict[str, list[MembershipInterval]] = {}
        for interval in intervals:
            if interval.available_at > decision_at:
                continue
            if not _covered_by_windows(interval, ordered_windows, decision_at):
                raise ValueError(
                    f"interval {interval.symbol} {interval.effective_from}..{interval.effective_to} "
                    "is not covered by a complete window")
            by_symbol.setdefault(interval.symbol, []).append(interval)
        for symbol, rows in by_symbol.items():
            rows.sort(key=lambda row: (row.effective_from, row.effective_to, row.citation))
            for before, after in zip(rows, rows[1:]):
                if after.effective_from <= before.effective_to:
                    raise ValueError(f"overlapping membership intervals for {symbol}")

        self._intervals = tuple(sorted(visible, key=lambda row: (
            row.symbol, row.effective_from, row.effective_to, row.citation)))
        self._by_symbol = {symbol: tuple(rows) for symbol, rows in by_symbol.items()}
        self._windows = ordered_windows
        self._key_to_symbol = dict(frozen_map)
        self._decision_at = decision_at

    @property
    def coverage_windows(self) -> tuple[MembershipCoverageWindow, ...]:
        return self._windows

    @property
    def intervals(self) -> tuple[MembershipInterval, ...]:
        return self._intervals

    @property
    def key_to_symbol(self) -> Mapping[str, str]:
        """The exact key-to-symbol mapping supplied at construction."""
        return MappingProxyType(self._key_to_symbol)

    def is_member(self, key: str, as_of: date) -> bool:
        """True/False only inside an admissible complete window; else raise."""
        if not isinstance(key, str) or not key or key not in self._key_to_symbol:
            raise ValueError("key must be an exact key present in the mapping")
        _date(as_of, "as_of")
        if as_of > self._decision_at.session:
            raise CoverageUnknown("query date is after the bound decision_at")
        window = next((row for row in self._windows
                       if row.covers(as_of) and row.available_at <= self._decision_at), None)
        if window is None:
            raise CoverageUnknown(
                f"membership coverage is unknown: no complete window for {as_of}")
        symbol = self._key_to_symbol[key]
        matches = tuple(row for row in self._by_symbol.get(symbol, ())
                        if row.covers(as_of) and row.available_at <= self._decision_at)
        if len(matches) > 1:
            raise ValueError(f"ambiguous membership intervals for {symbol} on {as_of}")
        if matches:
            return True
        # A complete window certifies that the evidenced change set is exhausted,
        # so absence inside it is a known non-membership, not an unknown.
        return False

    def require_coverage(self, first: date, last: date) -> MembershipCoverageWindow:
        """Fail closed unless a complete window covers the whole requested range."""
        first, last = _window((first, last), "requested range")
        if last > self._decision_at.session:
            raise CoverageUnknown("requested range is after the bound decision_at")
        window = next((row for row in self._windows
                       if row.effective_from <= first and last <= row.effective_to
                       and row.available_at <= self._decision_at), None)
        if window is None:
            raise CoverageUnknown(f"no complete membership window covers {first}..{last}")
        return window


def _covered_by_windows(interval: MembershipInterval,
                        windows: tuple[MembershipCoverageWindow, ...],
                        decision_at: EventTime) -> bool:
    """Is the interval continuously covered by admissible windows?"""
    on = interval.effective_from
    for window in windows:
        if window.available_at > decision_at or window.effective_to < on:
            continue
        if window.effective_from > on or interval.available_at > window.available_at:
            return False
        if window.effective_to >= interval.effective_to:
            return True
        on = window.effective_to + timedelta(days=1)
    return False
