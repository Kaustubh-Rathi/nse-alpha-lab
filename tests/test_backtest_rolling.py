"""Rolling returns and drawdown episodes: no padded or extrapolated windows."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from nse_pipeline.backtest.rolling import (
    SESSIONS_PER_YEAR,
    drawdown_durations,
    drawdown_episodes,
    rolling_returns,
    summarise_rolling,
    worst_drawdown_episode,
)

D = Decimal


def curve_from(values, start=date(2020, 1, 1)):
    """Build a curve with one calendar day per point."""
    return tuple((start + timedelta(days=index), D(str(value)))
                 for index, value in enumerate(values))


class TestRollingReturns:
    def test_short_curve_yields_no_windows(self):
        assert rolling_returns(curve_from([100, 110, 120]), 1) == ()

    def test_one_year_window_count(self):
        values = [100 + index for index in range(SESSIONS_PER_YEAR + 5)]
        windows = rolling_returns(curve_from(values), 1)
        # len - span windows: each start needs a point exactly span later.
        assert len(windows) == 5

    def test_window_return_is_exact(self):
        values = [100] * SESSIONS_PER_YEAR + [200]
        windows = rolling_returns(curve_from(values), 1)
        assert windows[0].total_return == D("1")

    def test_window_drawdown_uses_only_its_own_segment(self):
        # A crash before a window must not appear in that window's drawdown.
        values = [100, 50] + [100] * (SESSIONS_PER_YEAR * 2)
        windows = rolling_returns(curve_from(values), 1)
        # The first window contains the crash; a later window does not.
        assert windows[0].max_drawdown == D("-0.5")
        assert windows[-1].max_drawdown == D("0")

    def test_drawdown_inside_window_is_captured(self):
        values = [100] * 10 + [80] + [100] * (SESSIONS_PER_YEAR - 10)
        windows = rolling_returns(curve_from(values), 1)
        assert windows[0].max_drawdown == D("-0.2")

    def test_two_year_window_needs_two_years(self):
        values = [100] * (SESSIONS_PER_YEAR + 10)
        assert rolling_returns(curve_from(values), 2) == ()

    def test_invalid_years_rejected(self):
        with pytest.raises(ValueError, match="positive integer"):
            rolling_returns(curve_from([100, 110]), 0)

    def test_bool_years_rejected(self):
        with pytest.raises(ValueError, match="positive integer"):
            rolling_returns(curve_from([100, 110]), True)


class TestDrawdownEpisodes:
    def test_monotonic_rise_has_no_episode(self):
        assert drawdown_episodes(curve_from([100, 110, 120])) == ()

    def test_recovered_episode_is_closed(self):
        episodes = drawdown_episodes(curve_from([100, 80, 100]))
        assert len(episodes) == 1
        episode = episodes[0]
        assert episode.depth == D("-0.2")
        assert episode.recovered is True
        assert episode.recovery_date == date(2020, 1, 3)

    def test_unrecovered_episode_has_no_recovery_date(self):
        episodes = drawdown_episodes(curve_from([100, 80, 85]))
        assert len(episodes) == 1
        assert episodes[0].recovered is False
        assert episodes[0].recovery_date is None

    def test_two_separate_episodes(self):
        episodes = drawdown_episodes(curve_from([100, 80, 100, 90, 100]))
        assert len(episodes) == 2
        assert episodes[0].depth == D("-0.2")
        assert episodes[1].depth == D("-0.1")

    def test_worst_episode_selected(self):
        curve = curve_from([100, 70, 100, 95, 100])
        worst = worst_drawdown_episode(curve)
        assert worst.depth == D("-0.3")

    def test_no_drawdown_returns_none(self):
        assert worst_drawdown_episode(curve_from([100, 110])) is None

    def test_peak_and_trough_dates_are_exact(self):
        curve = curve_from([100, 90, 80, 100])
        episode = drawdown_episodes(curve)[0]
        assert episode.peak_date == date(2020, 1, 1)
        assert episode.trough_date == date(2020, 1, 3)


class TestDrawdownDurations:
    def test_recovered_episode_duration(self):
        details = drawdown_durations(curve_from([100, 80, 100]))
        worst = details["worst"]
        assert details["episodes"] == 1
        assert worst["peak_to_trough_days"] == 1
        assert worst["peak_to_trough_sessions"] == 1
        assert worst["trough_to_recovery_days"] == 1
        assert worst["trough_to_recovery_sessions"] == 1
        assert worst["total_days"] == 2
        assert worst["total_sessions"] == 2
        assert worst["recovered"] is True

    def test_unrecovered_episode_is_measured_to_the_end(self):
        details = drawdown_durations(curve_from([100, 80, 85]))
        worst = details["worst"]
        assert worst["recovered"] is False
        assert worst["trough_to_recovery_days"] is None
        assert worst["trough_to_recovery_sessions"] is None
        assert worst["total_days"] == 2
        assert worst["total_sessions"] == 2

    def test_worst_and_longest_can_be_different_episodes(self):
        # A shallow 3-day decline, then a deep 2-day decline.
        details = drawdown_durations(curve_from([100, 90, 95, 100, 100, 50, 100]))
        assert details["worst"]["depth"] == D("-0.5")
        assert details["worst"]["total_days"] == 2
        assert details["longest"]["depth"] == D("-0.1")
        assert details["longest"]["total_days"] == 3

    def test_no_drawdown_reports_no_episode(self):
        details = drawdown_durations(curve_from([100, 110, 120]))
        assert details["episodes"] == 0
        assert details["worst"] is None
        assert details["longest"] is None
        assert details["sessions_underwater"] == 0
        assert details["pct_sessions_underwater"] == D("0")

    def test_underwater_session_share(self):
        details = drawdown_durations(curve_from([100, 80, 90, 100]))
        assert details["sessions_underwater"] == 2
        assert details["pct_sessions_underwater"] == D("0.5")


class TestCurveValidation:
    def test_non_positive_equity_rejected(self):
        with pytest.raises(ValueError, match="non-positive equity"):
            drawdown_episodes(curve_from([100, 0]))

    def test_non_increasing_dates_rejected(self):
        curve = ((date(2020, 1, 2), D("100")), (date(2020, 1, 1), D("110")))
        with pytest.raises(ValueError, match="strictly increasing"):
            drawdown_episodes(curve)

    def test_float_equity_rejected(self):
        curve = ((date(2020, 1, 1), 100.0), (date(2020, 1, 2), 110.0))
        with pytest.raises(TypeError, match="must be Decimal"):
            drawdown_episodes(curve)

    def test_single_point_rejected(self):
        with pytest.raises(ValueError, match="at least 2 points"):
            drawdown_episodes(curve_from([100]))


class TestSummariseRolling:
    def test_empty_windows_yield_empty_summary(self):
        # An absent window is not a zero return.
        assert summarise_rolling(()) == {}

    def test_summary_statistics(self):
        values = [100] * SESSIONS_PER_YEAR + [120, 90, 150]
        windows = rolling_returns(curve_from(values), 1)
        summary = summarise_rolling(windows)
        assert summary["windows"] == len(windows)
        assert summary["min_return"] <= summary["median_return"] <= summary["max_return"]
        assert summary["positive_windows"] + summary["negative_windows"] <= summary["windows"]
