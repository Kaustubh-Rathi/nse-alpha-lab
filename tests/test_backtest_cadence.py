"""Rebalance cadence: last observed session per period, no invented dates."""
from datetime import date

import pytest

from nse_pipeline.backtest.cadence import (
    VALID_CADENCES,
    select_rebalance_dates,
)


def sessions(*iso: str) -> list[date]:
    return [date.fromisoformat(value) for value in iso]


class TestMonthly:
    def test_picks_last_observed_session_of_each_month(self):
        result = select_rebalance_dates(sessions(
            "2024-01-05", "2024-01-31", "2024-02-15", "2024-02-28",
        ), "monthly")
        assert result.dates == (date(2024, 1, 31), date(2024, 2, 28))

    def test_never_invents_a_missing_month_end(self):
        # No sessions at all in March: the period is reported, not filled.
        result = select_rebalance_dates(sessions(
            "2024-01-31", "2024-02-29", "2024-04-30",
        ), "monthly")
        assert date(2024, 3, 31) not in result.dates
        assert result.skipped_periods == ((2024, 3),)
        assert result.complete is False

    def test_duplicates_and_unordered_input_tolerated(self):
        result = select_rebalance_dates(sessions(
            "2024-02-28", "2024-01-31", "2024-01-31",
        ), "monthly")
        assert result.dates == (date(2024, 1, 31), date(2024, 2, 28))


class TestQuarterly:
    def test_picks_march_june_september_december(self):
        result = select_rebalance_dates(sessions(
            "2024-01-31", "2024-03-28", "2024-06-28", "2024-09-30", "2024-12-31",
        ), "quarterly")
        assert result.dates == (date(2024, 3, 28), date(2024, 6, 28),
                                date(2024, 9, 30), date(2024, 12, 31))

    def test_january_session_does_not_create_a_q1_date(self):
        # January belongs to the Q1 bucket but March overrides it.
        result = select_rebalance_dates(sessions(
            "2024-01-31", "2024-03-28",
        ), "quarterly")
        assert result.dates == (date(2024, 3, 28),)


class TestAnnual:
    def test_picks_last_session_of_december(self):
        result = select_rebalance_dates(sessions(
            "2023-12-29", "2024-06-28", "2024-12-31",
        ), "annual")
        assert result.dates == (date(2023, 12, 29), date(2024, 12, 31))

    def test_missing_december_is_reported(self):
        result = select_rebalance_dates(sessions(
            "2023-12-29", "2024-06-28",
        ), "annual")
        assert result.dates == (date(2023, 12, 29),)
        assert result.skipped_periods == ((2024, 12),)


class TestSemiannual:
    def test_picks_june_and_december(self):
        result = select_rebalance_dates(sessions(
            "2024-03-28", "2024-06-28", "2024-09-30", "2024-12-31",
        ), "semiannual")
        assert result.dates == (date(2024, 6, 28), date(2024, 12, 31))


class TestEdgeCases:
    def test_empty_input(self):
        result = select_rebalance_dates([], "monthly")
        assert result.dates == ()
        assert result.complete is True

    def test_single_session(self):
        result = select_rebalance_dates(sessions("2024-01-31"), "monthly")
        assert result.dates == (date(2024, 1, 31),)

    def test_unknown_cadence_rejected(self):
        with pytest.raises(ValueError, match="cadence must be one of"):
            select_rebalance_dates(sessions("2024-01-31"), "weekly")

    def test_all_cadences_accepted(self):
        for cadence in VALID_CADENCES:
            select_rebalance_dates(sessions("2024-12-31"), cadence)

    def test_dates_are_strictly_increasing(self):
        result = select_rebalance_dates(sessions(
            "2023-01-31", "2023-02-28", "2023-03-31", "2023-04-28",
        ), "monthly")
        assert list(result.dates) == sorted(result.dates)
        assert len(set(result.dates)) == len(result.dates)
