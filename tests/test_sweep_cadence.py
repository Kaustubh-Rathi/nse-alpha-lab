"""Cadence selection for the expanded sweep: anchor at the first evidence date.

The first implementation took the *last* evidence date in each calendar bucket,
so a 2-year hold skipped its first 24 months. These tests pin the corrected
behaviour: each 3-, 6-, 12-, 24- or 36-month block starts at the first actual
membership date in that block, and no date is invented.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The sweep code ships in the public research/ tree; the private
# verification/research/alpha-backtest copy is not part of the public export.
sys.path.insert(0, str(ROOT / "research"))

from run_sweep import (  # noqa: E402
    add_months,
    performance_horizon,
    select_cadence_dates,
)


def monthly_dates(first: date, months: int) -> list[date]:
    """Month-end-ish evidence dates, one per month, starting at ``first``."""
    result = [first]
    year, month = first.year, first.month
    for _ in range(months - 1):
        month += 1
        if month == 13:
            year += 1
            month = 1
        result.append(date(year, month, 28))
    return result


def test_monthly_keeps_every_evidence_date():
    dates = monthly_dates(date(2017, 7, 31), 57)
    selected = select_cadence_dates(dates, "monthly")
    assert selected == sorted(set(dates))
    assert selected[0] == date(2017, 7, 31)
    assert selected[-1] == date(2022, 3, 28)


def test_longer_holds_anchor_at_the_first_evidence_date():
    dates = monthly_dates(date(2017, 7, 31), 57)

    quarterly = select_cadence_dates(dates, "quarterly")
    semiannual = select_cadence_dates(dates, "semiannual")
    annual = select_cadence_dates(dates, "annual")
    two_year = select_cadence_dates(dates, "2y")
    three_year = select_cadence_dates(dates, "3y")

    # The first rebalance must be the universe's first date, not the end of
    # the first bucket (the old bug returned 2017-09-28 / 2017-12-28 / ...).
    for selected in (quarterly, semiannual, annual, two_year, three_year):
        assert selected[0] == date(2017, 7, 31)

    assert quarterly == [date(2017, 7, 31)] + [
        date(2017, 10, 28), date(2018, 1, 28), date(2018, 4, 28),
        date(2018, 7, 28), date(2018, 10, 28), date(2019, 1, 28),
        date(2019, 4, 28), date(2019, 7, 28), date(2019, 10, 28),
        date(2020, 1, 28), date(2020, 4, 28), date(2020, 7, 28),
        date(2020, 10, 28), date(2021, 1, 28), date(2021, 4, 28),
        date(2021, 7, 28), date(2021, 10, 28), date(2022, 1, 28)]
    assert semiannual == [
        date(2017, 7, 31), date(2018, 1, 28), date(2018, 7, 28),
        date(2019, 1, 28), date(2019, 7, 28), date(2020, 1, 28),
        date(2020, 7, 28), date(2021, 1, 28), date(2021, 7, 28),
        date(2022, 1, 28)]
    assert annual == [
        date(2017, 7, 31), date(2018, 7, 28), date(2019, 7, 28),
        date(2020, 7, 28), date(2021, 7, 28)]
    assert two_year == [
        date(2017, 7, 31), date(2019, 7, 28), date(2021, 7, 28)]
    assert three_year == [date(2017, 7, 31), date(2020, 7, 28)]


def test_block_uses_first_date_when_several_are_present():
    dates = [date(2017, 1, 31), date(2017, 2, 28),
             date(2017, 4, 28), date(2017, 6, 30),
             date(2017, 7, 31)]
    assert select_cadence_dates(dates, "quarterly") == [
        date(2017, 1, 31), date(2017, 4, 28), date(2017, 7, 31)]


def test_missing_dates_are_not_invented():
    dates = [date(2017, 1, 31), date(2017, 7, 31)]
    assert select_cadence_dates(dates, "semiannual") == [
        date(2017, 1, 31), date(2017, 7, 31)]
    assert select_cadence_dates([], "2y") == []


def test_unknown_cadence_rejected():
    with pytest.raises(ValueError, match="unknown cadence"):
        select_cadence_dates([date(2017, 1, 31)], "weekly")


def test_add_months_clamps_to_month_end():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
    assert add_months(date(2022, 3, 31), 36) == date(2025, 3, 31)


def test_performance_horizon_completes_final_hold_and_caps_at_data_end():
    assert performance_horizon(date(2022, 3, 31), "monthly") == \
        date(2022, 4, 30)
    assert performance_horizon(date(2021, 7, 30), "2y") == date(2023, 7, 30)
    assert performance_horizon(date(2020, 7, 31), "3y") == date(2023, 7, 31)
    # NIFTY 50's last annual snapshot is 2025-10-31; the stated 12-month hold
    # would run past the bar data, so it ends at the last available session.
    assert performance_horizon(date(2025, 10, 31), "annual") == \
        date(2026, 9, 30)
