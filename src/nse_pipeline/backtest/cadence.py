"""Rebalance-cadence date selection over observed exchange sessions.

Policy: a cadence selects the last *observed* session of each period. It never
invents a session, never forward-fills a missing date and never uses a calendar
date that the exchange did not trade. If a period has no observed session it is
skipped and reported, not silently filled.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

VALID_CADENCES = ("monthly", "quarterly", "semiannual", "annual")

#: The month that closes each cadence period. A period ends in the last of these.
_PERIOD_END_MONTHS = {
    "monthly": tuple(range(1, 13)),
    "quarterly": (3, 6, 9, 12),
    "semiannual": (6, 12),
    "annual": (12,),
}


@dataclass(frozen=True)
class CadenceSelection:
    """Selected rebalance dates plus the periods that produced none."""

    dates: tuple[date, ...]
    skipped_periods: tuple[tuple[int, int], ...]

    @property
    def complete(self) -> bool:
        """True when every period in range produced a rebalance date."""
        return not self.skipped_periods


def _period_key(session: date, cadence: str) -> tuple[int, int]:
    """The (year, period-end-month) bucket a session belongs to."""
    ends = _PERIOD_END_MONTHS[cadence]
    for end_month in ends:
        if session.month <= end_month:
            # A session in a month after the previous period end belongs here.
            return (session.year, end_month)
    # Unreachable for the defined cadences because the last entry is 12.
    raise ValueError(f"no period end covers month {session.month} for {cadence}")


def select_rebalance_dates(sessions, cadence: str = "monthly") -> CadenceSelection:
    """Pick the last observed session of each period's closing month.

    A period only yields a rebalance date when it contains at least one session
    in its *closing* month. A quarter with no March session is reported as a
    skipped period rather than silently rebalanced in January, because a
    January fill is not a Q1 rebalance.

    ``sessions`` is any iterable of ``datetime.date``. Duplicates are tolerated;
    ordering is not assumed.
    """
    if cadence not in VALID_CADENCES:
        raise ValueError(f"cadence must be one of {VALID_CADENCES}, got {cadence!r}")

    ordered = sorted(set(sessions))
    if not ordered:
        return CadenceSelection((), ())

    ends = _PERIOD_END_MONTHS[cadence]
    last_of_period: dict[tuple[int, int], date] = {}
    for session in ordered:
        key = _period_key(session, cadence)
        # Only a session in the closing month can close the period.
        if session.month != key[1]:
            continue
        last_of_period[key] = session

    # Every period between the first and last observed session must be present.
    first, last = ordered[0], ordered[-1]
    expected: list[tuple[int, int]] = []
    for year in range(first.year, last.year + 1):
        for end_month in ends:
            key = (year, end_month)
            if key < _period_key(first, cadence) or key > _period_key(last, cadence):
                continue
            expected.append(key)

    skipped = tuple(key for key in expected if key not in last_of_period)
    dates = tuple(last_of_period[key] for key in expected if key in last_of_period)
    return CadenceSelection(dates, skipped)
