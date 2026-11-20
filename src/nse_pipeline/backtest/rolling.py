"""Rolling-window return and drawdown analytics over an equity curve.

Policy: a rolling window is only reported when the curve actually spans it.
Windows that would require data outside the curve are omitted, never padded,
never extrapolated and never reported as zero. Every function is pure and
operates on ``Decimal`` so results are reproducible bit-for-bit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

ZERO = Decimal(0)
ONE = Decimal(1)

#: Trading sessions per year used to translate a year count into a window length.
SESSIONS_PER_YEAR = 252


@dataclass(frozen=True)
class RollingWindow:
    """One realised rolling window over an equity curve."""

    start: date
    end: date
    start_equity: Decimal
    end_equity: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    sessions: int

    @property
    def years(self) -> Decimal:
        return Decimal(self.sessions) / Decimal(SESSIONS_PER_YEAR)


@dataclass(frozen=True)
class DrawdownEpisode:
    """A peak-to-trough-to-recovery drawdown episode."""

    peak_date: date
    trough_date: date
    recovery_date: date | None
    peak_equity: Decimal
    trough_equity: Decimal
    depth: Decimal

    @property
    def sessions_to_trough(self) -> int:
        return (self.trough_date - self.peak_date).days

    @property
    def recovered(self) -> bool:
        return self.recovery_date is not None


def _validate_curve(curve) -> None:
    if len(curve) < 2:
        raise ValueError(f"equity curve needs at least 2 points, got {len(curve)}")
    previous: date | None = None
    for point_date, equity in curve:
        if not isinstance(point_date, date):
            raise TypeError("curve dates must be datetime.date")
        if not isinstance(equity, Decimal):
            raise TypeError("curve equities must be Decimal")
        if not equity.is_finite():
            raise ValueError("curve contains a non-finite equity")
        if equity <= ZERO:
            raise ValueError(
                f"equity at {point_date} is {equity}; a non-positive equity "
                "cannot be compounded or drawn down")
        if previous is not None and point_date <= previous:
            raise ValueError("curve dates must be strictly increasing")
        previous = point_date


def _max_drawdown_within(equities) -> Decimal:
    peak = equities[0]
    worst = ZERO
    for equity in equities:
        if equity > peak:
            peak = equity
        depth = equity / peak - ONE
        if depth < worst:
            worst = depth
    return worst


def rolling_returns(curve, years: int) -> tuple[RollingWindow, ...]:
    """Every non-overlapping-eligible rolling window of ``years`` length.

    A window starts at each curve point that has a point at least
    ``years * SESSIONS_PER_YEAR`` sessions later. Windows are stepped one
    session at a time (overlapping), which is the standard rolling convention.
    """
    if not isinstance(years, int) or isinstance(years, bool) or years < 1:
        raise ValueError("years must be a positive integer")
    _validate_curve(curve)

    span = years * SESSIONS_PER_YEAR
    if len(curve) <= span:
        return ()

    windows: list[RollingWindow] = []
    for index in range(len(curve) - span):
        start_date, start_equity = curve[index]
        end_date, end_equity = curve[index + span]
        segment = [equity for _, equity in curve[index:index + span + 1]]
        windows.append(RollingWindow(
            start=start_date,
            end=end_date,
            start_equity=start_equity,
            end_equity=end_equity,
            total_return=end_equity / start_equity - ONE,
            max_drawdown=_max_drawdown_within(segment),
            sessions=span,
        ))
    return tuple(windows)


def drawdown_episodes(curve) -> tuple[DrawdownEpisode, ...]:
    """All peak-to-trough episodes, with recovery when the curve later regains the peak.

    An episode is recorded when the curve falls below a running peak and ends
    either at recovery or at the final point (unrecovered). Episodes are ordered
    by peak date.
    """
    _validate_curve(curve)

    episodes: list[DrawdownEpisode] = []
    peak_date, peak_equity = curve[0]
    trough_date, trough_equity = peak_date, peak_equity
    in_episode = False

    for point_date, equity in curve[1:]:
        if equity >= peak_equity:
            if in_episode:
                episodes.append(DrawdownEpisode(
                    peak_date=peak_date, trough_date=trough_date,
                    recovery_date=point_date, peak_equity=peak_equity,
                    trough_equity=trough_equity,
                    depth=trough_equity / peak_equity - ONE))
                in_episode = False
            peak_date, peak_equity = point_date, equity
            trough_date, trough_equity = point_date, equity
            continue
        if equity < trough_equity:
            trough_date, trough_equity = point_date, equity
            in_episode = True

    if in_episode:
        episodes.append(DrawdownEpisode(
            peak_date=peak_date, trough_date=trough_date, recovery_date=None,
            peak_equity=peak_equity, trough_equity=trough_equity,
            depth=trough_equity / peak_equity - ONE))
    return tuple(episodes)


def worst_drawdown_episode(curve) -> DrawdownEpisode | None:
    """The deepest episode, or ``None`` when the curve never drew down."""
    episodes = drawdown_episodes(curve)
    if not episodes:
        return None
    return min(episodes, key=lambda episode: episode.depth)


def drawdown_durations(curve) -> dict:
    """Depth and duration of the worst and longest drawdown episodes.

    Calendar days are actual date differences; sessions are curve points. The
    curve is one point per trading session, so session counts are the number of
    sessions spent under water, not elapsed calendar days. An unrecovered
    episode is measured to the final curve point and is not treated as if it
    had recovered.

    Returns ``date`` objects and ``Decimal`` values; serialisation is the
    caller's responsibility.
    """
    _validate_curve(curve)
    episodes = drawdown_episodes(curve)
    if not episodes:
        return {
            "episodes": 0,
            "worst": None,
            "longest": None,
            "sessions_underwater": 0,
            "pct_sessions_underwater": ZERO,
        }

    indexes = {point_date: index for index, (point_date, _) in enumerate(curve)}
    last_date = curve[-1][0]

    def describe(episode: DrawdownEpisode) -> dict:
        end_date = episode.recovery_date or last_date
        return {
            "peak_date": episode.peak_date,
            "trough_date": episode.trough_date,
            "recovery_date": episode.recovery_date,
            "depth": episode.depth,
            "recovered": episode.recovered,
            "peak_to_trough_days": (episode.trough_date - episode.peak_date).days,
            "peak_to_trough_sessions": (indexes[episode.trough_date]
                                        - indexes[episode.peak_date]),
            "trough_to_recovery_days": ((episode.recovery_date - episode.trough_date).days
                                        if episode.recovery_date else None),
            "trough_to_recovery_sessions": ((indexes[episode.recovery_date]
                                             - indexes[episode.trough_date])
                                            if episode.recovery_date else None),
            "total_days": (end_date - episode.peak_date).days,
            "total_sessions": indexes[end_date] - indexes[episode.peak_date],
        }

    worst = min(episodes, key=lambda episode: episode.depth)
    longest = max(episodes,
                  key=lambda episode: (episode.recovery_date or last_date)
                  - episode.peak_date)

    peak = curve[0][1]
    underwater = 0
    for _, equity in curve:
        if equity > peak:
            peak = equity
        elif equity < peak:
            underwater += 1

    return {
        "episodes": len(episodes),
        "worst": describe(worst),
        "longest": describe(longest),
        "sessions_underwater": underwater,
        "pct_sessions_underwater": Decimal(underwater) / Decimal(len(curve)),
    }


def summarise_rolling(windows) -> dict:
    """Aggregate a rolling-window tuple into reportable statistics.

    Returns an empty dict for no windows: an absent window is not a zero return.
    """
    windows = tuple(windows)
    if not windows:
        return {}
    returns = [window.total_return for window in windows]
    ordered = sorted(returns)
    count = len(ordered)
    midpoint = count // 2
    if count % 2:
        median = ordered[midpoint]
    else:
        median = (ordered[midpoint - 1] + ordered[midpoint]) / Decimal(2)
    mean = sum(returns, ZERO) / Decimal(count)
    return {
        "windows": count,
        "mean_return": mean,
        "median_return": median,
        "min_return": ordered[0],
        "max_return": ordered[-1],
        "positive_windows": sum(1 for value in returns if value > ZERO),
        "negative_windows": sum(1 for value in returns if value < ZERO),
        "worst_drawdown": min(window.max_drawdown for window in windows),
    }
