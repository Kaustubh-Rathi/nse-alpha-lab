"""CSV conventions for the expanded sweep merge.

The sweep stores stop/target thresholds as decimal fractions because that is
what the engine consumes, while every ``*_pct`` column in the published CSV is
in percent units. These tests pin that conversion, the rounding rules and the
deterministic Sharpe rank.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The sweep code ships in the public research/ tree; the private
# verification/research/alpha-backtest copy is not part of the public export.
sys.path.insert(0, str(ROOT / "research"))

import merge_sweep  # noqa: E402


def raw_row(**overrides) -> dict:
    row = {
        "config_id": "nifty500_top_N10_L63_monthly_sl10",
        "universe": "nifty500",
        "direction": "top",
        "top_n": 10,
        "cadence": "monthly",
        "exit_overlay": "sl10",
        "stop_loss_pct": "0.1",
        "take_profit_pct": None,
        "charges_pct_of_initial_cash": 1.5,
        "performance_start": "2013-05-31",
        "performance_end": "2026-09-30",
        "sharpe_rf0": 0.8,
        "cagr_pct": 18.0,
        "notes": "",
    }
    row.update(overrides)
    return row


def test_enrich_converts_exit_thresholds_to_percent_units():
    row = raw_row()
    merge_sweep.enrich(row)
    assert row["stop_loss_pct"] == 10.0
    assert row["take_profit_pct"] is None
    assert row["charges_pct_of_initial_cash"] == 150.0


def test_enrich_adds_calendar_days_and_cash_flag():
    row = raw_row(cadence="3y", exit_overlay="tp05")
    merge_sweep.enrich(row)
    assert row["calendar_days"] == 4870
    assert row["flag_exit_cash_until_rebalance"] is True
    assert "cash" in row["notes"]


def test_no_overlay_is_not_flagged_as_cash_heavy():
    row = raw_row(exit_overlay="none", stop_loss_pct=None)
    merge_sweep.enrich(row)
    assert row["flag_exit_cash_until_rebalance"] is False
    assert row["notes"] == ""


def test_format_value_uses_published_precision():
    assert merge_sweep.format_value("cagr_pct", 19.2963) == "19.30"
    assert merge_sweep.format_value("final_equity_inr", 85_217_796.54) == \
        "85217796.54"
    assert merge_sweep.format_value("charges_pct_of_initial_cash", 151.789) == \
        "151.79"
    assert merge_sweep.format_value("sharpe_rf0", 0.778224) == "0.7782"
    assert merge_sweep.format_value("publishable", True) == "true"
    assert merge_sweep.format_value("publishable", False) == "false"
    assert merge_sweep.format_value("stop_loss_pct", None) == ""


def test_rank_is_descending_and_contiguous():
    rows = [
        raw_row(config_id="a", sharpe_rf0=0.5),
        raw_row(config_id="b", sharpe_rf0=1.2),
        raw_row(config_id="c", sharpe_rf0=0.9),
    ]
    merge_sweep.assign_rank(rows, "sharpe_rf0", "rank_by_sharpe")
    ranked = sorted(rows, key=lambda row: row["rank_by_sharpe"])
    assert [row["config_id"] for row in ranked] == ["b", "c", "a"]
    assert [row["rank_by_sharpe"] for row in ranked] == [1, 2, 3]
