"""Merge sweep shards into FINAL_RESULTS.csv, ordered by Sharpe.

The full table is deliberately one row per run: 968 configurations spanning
five universes, six holding periods, both momentum directions and 25 exit
overlays. Percentages are in percent units (``19.30`` means 19.30%), money is
in INR, ratios are decimals.

A high Sharpe rank is **not** a strategy recommendation. Short windows,
two-rebalance 3-year holds and cash-heavy stop/target combinations are not
comparable to the full 13-year NIFTY 500 row. The flags and the notes column
carry that warning into every row.

Usage::

    python verification/research/alpha-backtest/merge_sweep.py
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "FINAL_RESULTS.csv"
FLAGSHIP = HERE / "FLAGSHIP_RESULTS.csv"
REPORT = HERE / "SWEEP_REPORT.md"

HEADER = [
    "rank_by_sharpe", "rank_by_cagr", "rank_by_alpha",
    "config_id", "family", "universe", "universe_label", "direction", "top_n",
    "universe_snapshots", "universe_first_snapshot", "universe_last_snapshot",
    "universe_members_min", "universe_members_max", "membership_note",
    "is_flagship", "evidence_label",
    "lookback_sessions", "cadence", "holding_period_months", "calendar_mode",
    "stop_loss_pct", "take_profit_pct", "exit_overlay", "costs",
    "scheduled_rebalances", "signal_dates_with_trades", "unproven_universe_holds",
    "last_signal_date", "first_fill", "last_fill",
    "performance_start", "performance_end", "scheduled_horizon_end",
    "sessions", "window_years", "calendar_days", "publishable", "refusal",
    "trades", "buys", "sells", "skips", "skip_reasons", "delisting_writeoffs",
    "initial_equity_inr", "final_equity_inr",
    "total_return_pct", "cagr_pct", "annualised_volatility_pct", "sharpe_rf0",
    "max_drawdown_pct",
    "max_dd_peak_date", "max_dd_trough_date", "max_dd_recovery_date",
    "max_dd_peak_to_trough_days", "max_dd_peak_to_trough_sessions",
    "max_dd_trough_to_recovery_days", "max_dd_trough_to_recovery_sessions",
    "max_dd_total_days", "max_dd_total_sessions", "max_dd_recovered",
    "longest_dd_peak_date", "longest_dd_trough_date", "longest_dd_recovery_date",
    "longest_dd_total_days", "longest_dd_total_sessions",
    "drawdown_episodes", "sessions_underwater", "pct_sessions_underwater",
    "benchmark", "benchmark_window_start", "benchmark_window_end",
    "benchmark_sessions",
    "benchmark_cagr_pct", "strategy_cagr_aligned_pct", "excess_cagr_pct",
    "beta", "alpha_linear_pct", "alpha_geometric_pct", "tracking_error_pct",
    "information_ratio", "up_capture_pct", "down_capture_pct", "r_squared",
    "t_ols", "t_newey_west_5", "alpha_error",
    "nifty500_benchmark", "nifty500_window_start", "nifty500_window_end",
    "nifty500_sessions", "nifty500_benchmark_cagr_pct",
    "nifty500_strategy_cagr_aligned_pct", "nifty500_excess_cagr_pct",
    "nifty500_beta", "nifty500_alpha_linear_pct", "nifty500_alpha_geometric_pct",
    "nifty500_tracking_error_pct", "nifty500_information_ratio",
    "nifty500_up_capture_pct", "nifty500_down_capture_pct", "nifty500_r_squared",
    "nifty500_t_ols", "nifty500_t_newey_west_5", "nifty500_alpha_error",
    "buy_notional_inr", "sell_notional_inr", "turnover_inr",
    "buy_charges_inr", "sell_charges_inr", "total_charges_inr",
    "brokerage_inr", "gst_inr", "stt_inr", "stamp_inr", "exchange_inr",
    "slippage_inr", "charges_bps_of_turnover", "charges_pct_of_initial_cash",
    "flag_few_rebalances", "flag_short_window", "flag_exit_cash_until_rebalance",
    "flag_universe_overlap", "flag_superseded", "price_basis", "benchmark_basis",
    "dividend_treatment", "gate_status", "notes",
]

FLAGSHIP_IDS = (
    "nifty500_top_N10_L63_monthly_none",
    "nifty500_top_N10_L63_monthly_sl10",
    "nifty500_top_N20_L63_monthly_none",
    "nifty500_bottom_N20_L63_monthly_none",
    "nifty500_top_N10_L63_monthly_none_zero_cost",
    "nifty500_top_N10_L63_monthly_none_month_end_exact",
)

EVIDENCE_LABELS = {
    "nifty500_top_N10_L63_monthly_none": "evidence-N10-L63-next_open-no_stop",
    "nifty500_top_N10_L63_monthly_sl10": "evidence-N10-L63-next_open-sl0.10",
    "nifty500_top_N20_L63_monthly_none": "evidence-N20-L63-next_open-no_stop",
    "nifty500_bottom_N20_L63_monthly_none":
        "evidence-N20-L63-next_open-no_stop-bottom",
    "nifty500_top_N10_L63_monthly_none_zero_cost":
        "evidence-N10-L63-next_open-no_stop-gross_of_costs",
    "nifty500_top_N10_L63_monthly_none_month_end_exact":
        "as-committed-month-end-exact-match",
}

# Raw point-in-time membership coverage on disk. These are counts of snapshot
# files and named constituents, before symbol-to-key resolution. They are here
# so a reader can see how much history a row actually rests on.
UNIVERSE_META = {
    "nifty500": {
        "snapshots": 171,
        "first": "2013-05-31",
        "last": "2026-09-30",
        "members_min": 500,
        "members_max": 506,
        "note": ("107 monthly official sets to 2022-03, then 64 irregular "
                 "post-2022 anchors; not strict monthly after 2022-03"),
    },
    "msc400": {
        "snapshots": 57,
        "first": "2017-07-31",
        "last": "2022-03-31",
        "members_min": 400,
        "members_max": 400,
        "note": "57 monthly official sets, 2017-07..2022-03 only",
    },
    "midcap150": {
        "snapshots": 57,
        "first": "2017-07-31",
        "last": "2022-03-31",
        "members_min": 150,
        "members_max": 150,
        "note": ("57 monthly official sets, 2017-07..2022-03 only; a subset of "
                 "NIFTY 500 ranks 101-250"),
    },
    "smallcap250": {
        "snapshots": 57,
        "first": "2017-07-31",
        "last": "2022-03-31",
        "members_min": 250,
        "members_max": 250,
        "note": ("57 monthly official sets, 2017-07..2022-03 only; a subset of "
                 "NIFTY 500 ranks 251-500"),
    },
    "nifty50": {
        "snapshots": 13,
        "first": "2013-10-31",
        "last": "2025-10-31",
        "members_min": 50,
        "members_max": 50,
        "note": "13 annual exact snapshots, not a monthly membership chain",
    },
}

TWO_DECIMAL = {"window_years", "charges_bps_of_turnover"}
FOUR_DECIMAL = {
    "sharpe_rf0", "beta", "information_ratio", "r_squared",
    "t_ols", "t_newey_west_5", "nifty500_beta", "nifty500_information_ratio",
    "nifty500_r_squared", "nifty500_t_ols", "nifty500_t_newey_west_5",
    "charges_pct_of_initial_cash",
}
PCT_COLUMNS = {key for key in HEADER if key.endswith("_pct")}
PCT_COLUMNS.add("pct_sessions_underwater")
PCT_COLUMNS.add("charges_pct_of_initial_cash")
MONEY_COLUMNS = {key for key in HEADER if key.endswith("_inr")}


def load_rows() -> list[dict]:
    # Oldest file first, newest last: a tagged correction shard written after a
    # main shard must win when both contain the same config_id.
    paths = sorted(HERE.glob("sweep-shard-*.jsonl"),
                   key=lambda path: (path.stat().st_mtime, path.name))
    if not paths:
        raise SystemExit("no sweep-shard-*.jsonl files found")
    by_id: dict[str, dict] = {}
    duplicates = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row["config_id"] in by_id:
                duplicates += 1
            by_id[row["config_id"]] = row
    if duplicates:
        print(f"note: {duplicates} duplicate row(s) replaced by the newest shard")
    return list(by_id.values())


def enrich(row: dict) -> None:
    """Add coverage metadata and flags that the raw runner does not know."""
    meta = UNIVERSE_META[row["universe"]]
    row["universe_snapshots"] = meta["snapshots"]
    row["universe_first_snapshot"] = meta["first"]
    row["universe_last_snapshot"] = meta["last"]
    row["universe_members_min"] = meta["members_min"]
    row["universe_members_max"] = meta["members_max"]
    row["membership_note"] = meta["note"]
    row["is_flagship"] = row["config_id"] in FLAGSHIP_IDS
    row["evidence_label"] = EVIDENCE_LABELS.get(row["config_id"])

    start, end = row.get("performance_start"), row.get("performance_end")
    row["calendar_days"] = ((date.fromisoformat(end) - date.fromisoformat(start)).days
                            if start and end else None)

    # The raw runner carries stop/target as decimal fractions because that is
    # what the engine expects, and the cost audit returns charges as a fraction
    # of initial cash. The CSV schema says every *_pct column is in percent
    # units, so convert here instead of emitting 0.10 under a "%" name.
    for key in ("stop_loss_pct", "take_profit_pct"):
        value = row.get(key)
        if value not in (None, ""):
            row[key] = float(value) * 100
    value = row.get("charges_pct_of_initial_cash")
    if value not in (None, ""):
        row["charges_pct_of_initial_cash"] = float(value) * 100

    long_hold_exit = (row["exit_overlay"] != "none"
                      and row["cadence"] in ("annual", "2y", "3y"))
    row["flag_exit_cash_until_rebalance"] = long_hold_exit

    notes = [row.get("notes", "")] if row.get("notes") else []
    if long_hold_exit:
        notes.append("exit proceeds sit in cash until the next scheduled "
                     "rebalance; high Sharpe can be a cash artefact")
    row["notes"] = "; ".join(notes)


def assign_rank(rows: list[dict], field: str, rank_field: str) -> None:
    ranked = sorted(
        (row for row in rows if row.get(field) is not None),
        key=lambda row: (
            -float(row[field]),
            -(row.get("cagr_pct") if row.get("cagr_pct") is not None else float("-inf")),
            row["config_id"],
        ),
    )
    for rank, row in enumerate(ranked, start=1):
        row[rank_field] = rank


def format_value(key: str, value):
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if key == "skip_reasons" and isinstance(value, dict):
        return "; ".join(f"{name}={count}"
                         for name, count in sorted(value.items()))
    if key in MONEY_COLUMNS or key in PCT_COLUMNS or key in TWO_DECIMAL:
        return f"{float(value):.2f}"
    if key in FOUR_DECIMAL:
        return f"{float(value):.4f}"
    return value


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: format_value(key, row.get(key))
                             for key in HEADER})


def row_for(rows: list[dict], config_id: str) -> dict | None:
    return next((row for row in rows if row["config_id"] == config_id), None)


def pct(value) -> str:
    return "" if value is None else f"{float(value):.2f}%"


def num(value, digits: int = 2) -> str:
    return "" if value is None else f"{float(value):.{digits}f}"


def table(rows: list[dict], columns: list[tuple[str, str]]) -> list[str]:
    lines = [
        "| " + " | ".join(label for _key, label in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in rows:
        cells = []
        for key, _label in columns:
            value = row.get(key)
            if key.endswith("_pct"):
                cells.append(pct(value))
            elif key.endswith("_inr"):
                cells.append("" if value is None else f"{float(value):,.0f}")
            elif key in ("sharpe_rf0", "beta", "information_ratio", "t_ols",
                         "t_newey_west_5"):
                cells.append(num(value, 4))
            elif key == "window_years":
                cells.append(num(value, 2))
            elif key == "config_id":
                cells.append(f"`{value}`")
            else:
                cells.append("" if value is None else str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def build_report(rows: list[dict]) -> str:
    publishable = [row for row in rows if row.get("publishable")]
    top_sharpe = publishable[:25]
    families = Counter(row["family"] for row in rows)
    universes = Counter(row["universe"] for row in rows)
    cadences = Counter(row["cadence"] for row in rows)

    lines = [
        "# Expanded momentum sweep: universes, holds, stops and profit cut-offs",
        "",
        "Research diagnostics only. The internal validation gate is still **open**; these are not "
        "published returns and the Sharpe ranking is **not** a model-selection result.",
        "",
        "## What was run",
        "",
        f"- **{len(rows)} configurations**, {len(publishable)} publishable, "
        f"{len(rows) - len(publishable)} refused.",
        f"- Families: " + ", ".join(f"{name} ({count})"
                                    for name, count in sorted(families.items())) + ".",
        f"- Universes: " + ", ".join(f"{name} ({count})"
                                     for name, count in sorted(universes.items())) + ".",
        f"- Cadences: " + ", ".join(f"{name} ({count})"
                                   for name, count in sorted(cadences.items())) + ".",
        "- Exits: no overlay, 5/10/15/20 stop-loss, 5/10/15/20 take-profit and all "
        "16 stop x target combinations. The full grid is run on the four monthly "
        "universes; NIFTY 50 gets the same grid on its annual rebalance calendar.",
        "- Bottom 20/25 on 2- and 3-year holds is run on every monthly universe "
        "with no overlay, and with every exit overlay on NIFTY 500.",
        "- Every evidence-dated rebalance is an exact membership date; the one "
        "superseded month-end diagnostic is the exception. Quarterly and longer "
        "holdings take the first evidence date at or after each 3-, 6-, 12-, "
        "24- or 36-month boundary, so the first rebalance is the universe's "
        "first date and no early years are skipped.",
        "- **Each run ends when its final scheduled hold completes**, not at the "
        "last bar date. Monthly 2017-2022 rows end in 2022-04; 2- and 3-year rows "
        "end in mid-2023. `last_signal_date` and `scheduled_horizon_end` in the "
        "CSV show the exact boundary.",
        "",
        "## Read this before the ranking",
        "",
        "- **The full table is sorted by Sharpe**, as requested. Sharpe is not "
        "comparable across windows: a mid/small-cap run built on 4.75 years of "
        "membership evidence (extended only long enough to finish its final "
        "hold) and a 13-year NIFTY 500 run are different samples.",
        f"- **The top Sharpe is a maximum over {len(rows)} trials.** No "
        "multiple-testing correction is applied; the ranking is a descriptive "
        "grid summary, not a new strategy claim.",
        "- **Short windows and few rebalances are flagged.** A 3-year hold on a "
        "universe with only 4.75 years of membership evidence has two or three "
        "rebalances; a 3-year hold on the full window has five. Those rows are "
        "diagnostics, not strategies.",
        "- **Stop/target proceeds sit in cash until the next scheduled rebalance.** "
        "With a 2- or 3-year hold, a 5% target can leave most of the book in cash "
        "for years; on the NIFTY 50 annual calendar it can sit for up to a year. "
        "A high Sharpe there is often a cash-allocation artefact.",
        "- **Midcap 150 and Smallcap 250 are subsets of NIFTY 500.** MSC400 overlaps "
        "the same market. They are segment views, not independent universes.",
        "- **NIFTY 50 is annual-snapshot only.** It has 13 rebalances and is a "
        "large-cap comparison, not a monthly index strategy.",
        "- **No NIFTY 750.** The repo has no point-in-time NIFTY Total Market / "
        "Microcap 250 membership chain. NIFTY 500 plus Midcap 150 plus Smallcap 250 "
        "is still NIFTY 500, not 750 names. A 750-name test needs new constituent "
        "data and is not fabricated here.",
        "- **No dividend adjustment.** Prices are split/bonus-adjusted only. The "
        "benchmark is a price index. A clean total-return alpha needs a verified "
        "NIFTY 50 total-return series, which is not on disk.",
        "",
        "## Why there is no NIFTY 750 row",
        "",
        "A 750-name universe needs a point-in-time constituent chain for the "
        "NIFTY Total Market (or NIFTY 500 plus Microcap 250). This repository has:",
        "",
        "- NIFTY 500: 171 exact snapshots (107 monthly official sets through "
        "2022-03, then 64 irregular anchors);",
        "- NIFTY MidSmallcap 400, Midcap 150 and Smallcap 250: 57 monthly official "
        "sets each, 2017-07..2022-03;",
        "- NIFTY 50: 13 annual exact snapshots, 2013-10..2025-10.",
        "",
        "There is no Microcap 250 or Total Market membership file, and no "
        "point-in-time chain for the 250 names outside NIFTY 500. Midcap 150 and "
        "Smallcap 250 are already subsets of NIFTY 500, so their union is still "
        "the 500-name universe, not 750. Inventing the missing 250 from today's "
        "constituents would inject survivorship and look-ahead bias, so no such "
        "row is produced.",
        "",
        "## Why the prices are not dividend-adjusted",
        "",
        "The prepared price series adjusts for splits and bonuses only. Dividends "
        "are recorded in the corporate-actions table with ex-dates and amounts, "
        "but they are **not** subtracted from the historical price series and not "
        "credited as cash. Producing a total-return series would require, for "
        "every ex-date, the shares held, the dividend per share, withholding-tax "
        "treatment and reinvestment convention. The pipeline does not define that "
        "calculation, so applying dividends only to the strategy would mix bases.",
        "",
        "The benchmarks are also price indices (Yahoo `^NSEI`, NSE NIFTY 500). "
        "Comparing strategy price returns with a price index is internally "
        "consistent on the benchmark axis, but both sides understate total return. "
        "The sign of the net alpha bias depends on the strategy's dividend yield "
        "relative to NIFTY 50, which is not verified here. A defensible "
        "total-return alpha needs a verified NIFTY 50 total-return series and a "
        "dividend-reinvestment rule; neither is available in this repository, so "
        "the headline stays price-vs-price and every row says so.",
        "",
        "## Top 25 by Sharpe",
        "",
    ]
    lines += table(top_sharpe, [
        ("rank_by_sharpe", "rank"),
        ("config_id", "configuration"),
        ("universe", "universe"),
        ("cadence", "hold"),
        ("exit_overlay", "exit"),
        ("window_years", "years"),
        ("cagr_pct", "CAGR"),
        ("sharpe_rf0", "Sharpe"),
        ("max_drawdown_pct", "max DD"),
        ("alpha_linear_pct", "alpha vs N50"),
        ("t_newey_west_5", "t NW5"),
    ])
    lines += [
        "",
        "The table starts with short-window and cash-heavy rows. Check the `years`, "
        "`flag_short_window`, `flag_few_rebalances` and `notes` columns in "
        "`FINAL_RESULTS.csv` before reading anything into the order.",
        "",
        "## Universe comparison: top 10, monthly, no exit",
        "",
    ]
    comparison = [
        row_for(rows, {
            "nifty50": "nifty50_top_N10_L63_annual_none",
        }.get(universe, f"{universe}_top_N10_L63_monthly_none"))
        for universe in ("nifty500", "msc400", "midcap150", "smallcap250", "nifty50")
    ]
    comparison = [row for row in comparison if row is not None]
    lines += table(comparison, [
        ("config_id", "configuration"),
        ("universe_label", "universe"),
        ("performance_start", "start"),
        ("performance_end", "end"),
        ("cagr_pct", "CAGR"),
        ("sharpe_rf0", "Sharpe"),
        ("max_drawdown_pct", "max DD"),
        ("alpha_linear_pct", "alpha vs N50"),
        ("t_newey_west_5", "t NW5"),
    ])
    lines += [
        "",
        "NIFTY 50 is annual-snapshot only and covers 2013-2025. The other three "
        "are built from membership evidence covering 2017-07..2022-03; each run "
        "stops when its final scheduled hold completes (2022-04 for monthly, "
        "mid-2023 for 2/3-year), so their CAGRs and Sharpe ratios are not a "
        "like-for-like extension of the NIFTY 500 row.",
        "",
        "## Stop-loss grid: NIFTY 500, top 10, monthly, no target",
        "",
    ]
    stops = [row_for(rows, "nifty500_top_N10_L63_monthly_none")]
    stops += [row_for(rows, f"nifty500_top_N10_L63_monthly_sl{tag}")
              for tag in ("05", "10", "15", "20")]
    stops = [row for row in stops if row is not None]
    lines += table(stops, [
        ("config_id", "configuration"),
        ("stop_loss_pct", "stop"),
        ("cagr_pct", "CAGR"),
        ("sharpe_rf0", "Sharpe"),
        ("max_drawdown_pct", "max DD"),
        ("alpha_linear_pct", "alpha vs N50"),
        ("t_newey_west_5", "t NW5"),
        ("trades", "trades"),
    ])
    lines += [
        "",
        "## Take-profit grid: NIFTY 500, top 10, monthly, no stop",
        "",
    ]
    targets = [row_for(rows, "nifty500_top_N10_L63_monthly_none")]
    targets += [row_for(rows, f"nifty500_top_N10_L63_monthly_tp{tag}")
                for tag in ("05", "10", "15", "20")]
    targets = [row for row in targets if row is not None]
    lines += table(targets, [
        ("config_id", "configuration"),
        ("take_profit_pct", "target"),
        ("cagr_pct", "CAGR"),
        ("sharpe_rf0", "Sharpe"),
        ("max_drawdown_pct", "max DD"),
        ("alpha_linear_pct", "alpha vs N50"),
        ("t_newey_west_5", "t NW5"),
        ("trades", "trades"),
    ])
    lines += [
        "",
        "## Best stop x target combinations: NIFTY 500, top 10, monthly",
        "",
    ]
    combos = [row for row in publishable
              if row["universe"] == "nifty500"
              and row["direction"] == "top" and row["top_n"] == 10
              and row["cadence"] == "monthly"
              and row["stop_loss_pct"] is not None
              and row["take_profit_pct"] is not None]
    combos.sort(key=lambda row: -(row.get("sharpe_rf0") or float("-inf")))
    lines += table(combos[:10], [
        ("config_id", "configuration"),
        ("cagr_pct", "CAGR"),
        ("sharpe_rf0", "Sharpe"),
        ("max_drawdown_pct", "max DD"),
        ("alpha_linear_pct", "alpha vs N50"),
        ("t_newey_west_5", "t NW5"),
        ("trades", "trades"),
    ])
    lines += [
        "",
        "## Bottom 20/25 on 2- and 3-year holds",
        "",
    ]
    bottom_holds = [row for row in publishable
                    if row["universe"] == "nifty500"
                    and row["direction"] == "bottom"
                    and row["top_n"] in (20, 25)
                    and row["cadence"] in ("2y", "3y")]
    bottom_no_exit = [row for row in publishable
                      if row["direction"] == "bottom"
                      and row["top_n"] in (20, 25)
                      and row["cadence"] in ("2y", "3y")
                      and row["exit_overlay"] == "none"]
    bottom_no_exit.sort(key=lambda row: (row["universe"], row["top_n"],
                                         row["cadence"]))
    lines += table(bottom_no_exit, [
        ("config_id", "configuration"),
        ("universe", "universe"),
        ("scheduled_rebalances", "rebalances"),
        ("cagr_pct", "CAGR"),
        ("sharpe_rf0", "Sharpe"),
        ("max_drawdown_pct", "max DD"),
        ("alpha_linear_pct", "alpha vs N50"),
        ("t_newey_west_5", "t NW5"),
        ("trades", "trades"),
    ])
    lines += [
        "",
        "Every monthly universe therefore has a bottom 20/25 2- and 3-year "
        "result. The 2-year/3-year overlay grid is run on NIFTY 500, which is "
        "the headline universe; the other universes carry the no-overlay core "
        "rows so the holding-period comparison is not NIFTY 500-only. The "
        "best-Sharpe bottom 20/25 2- or 3-year row is not automatically the "
        "best reversal strategy. With only a handful of rebalances (five to "
        "seven on NIFTY 500; two to three on the 2017-2022 universes), the "
        "result is a few decisions, not a durable monthly signal. Here are the "
        "best NIFTY 500 overlay rows in that family:",
        "",
    ]
    best_bottom_overlay = [row for row in bottom_holds
                           if row["exit_overlay"] != "none"]
    best_bottom_overlay.sort(key=lambda row: -(row.get("sharpe_rf0")
                                               if row.get("sharpe_rf0") is not None
                                               else float("-inf")))
    lines += table(best_bottom_overlay[:10], [
        ("config_id", "configuration"),
        ("scheduled_rebalances", "rebalances"),
        ("cagr_pct", "CAGR"),
        ("sharpe_rf0", "Sharpe"),
        ("max_drawdown_pct", "max DD"),
        ("alpha_linear_pct", "alpha vs N50"),
        ("t_newey_west_5", "t NW5"),
        ("trades", "trades"),
    ])
    lines += [
        "",
        "Those rows are cash-heavy by construction: a 5% target on a 3-year hold "
        "can leave most of the book in cash for years. `flag_exit_cash_until_rebalance` "
        "marks every such row in the CSV.",
        "",
        "## Flagship rows in the full table",
        "",
    ]
    flagship_rows = [row for row in rows if row["config_id"] in FLAGSHIP_IDS]
    flagship_rows.sort(key=lambda row: FLAGSHIP_IDS.index(row["config_id"]))
    lines += table(flagship_rows, [
        ("config_id", "configuration"),
        ("performance_start", "start"),
        ("performance_end", "end"),
        ("total_return_pct", "total return"),
        ("cagr_pct", "CAGR"),
        ("sharpe_rf0", "Sharpe"),
        ("max_drawdown_pct", "max DD"),
        ("alpha_linear_pct", "alpha vs N50"),
        ("t_newey_west_5", "t NW5"),
        ("total_charges_inr", "charges"),
    ])
    lines += [
        "",
        "The primary is still `nifty500_top_N10_L63_monthly_none`. Its alpha is "
        "economically large but not statistically significant at 5% "
        "(t 1.62 Newey-West, bootstrap p 0.096). Nothing in this sweep changes "
        "that pre-specified result.",
        "",
        "## Files",
        "",
        "- `FINAL_RESULTS.csv` - all rows, sorted by Sharpe, with ranks and flags.",
        "- `FLAGSHIP_RESULTS.csv` - the six headline rows only.",
        "- `sweep-shard-*.jsonl` - raw shard output, one JSON object per run.",
        "- `SWEEP_REPORT.md` - this report.",
        "",
        "## Data limits",
        "",
        "- NIFTY 750 / NIFTY Total Market: no point-in-time membership chain on disk.",
        "- Dividends: not applied; no verified total-return benchmark.",
        "- Pre-2013: outside the backtest window.",
        "- Post-2022 NIFTY 500: irregular evidence anchors, not strict monthly.",
        "- Delisting: `last_close_writeoff`; terminal proceeds are unknown.",
        "- Benchmark: NIFTY 50 price index (Yahoo ^NSEI) for the headline; "
        "NIFTY 500 spliced NSE as a robustness column.",
        "- Status: research preview. Validation in progress.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    rows = load_rows()
    for row in rows:
        enrich(row)
    assign_rank(rows, "sharpe_rf0", "rank_by_sharpe")
    assign_rank(rows, "cagr_pct", "rank_by_cagr")
    assign_rank(rows, "alpha_linear_pct", "rank_by_alpha")
    rows.sort(key=lambda row: (
        row.get("rank_by_sharpe") is None,
        row.get("rank_by_sharpe") or 10**9,
        row["config_id"],
    ))
    write_csv(rows, OUT)

    flagship = [row for row in rows if row["config_id"] in FLAGSHIP_IDS]
    flagship.sort(key=lambda row: FLAGSHIP_IDS.index(row["config_id"]))
    write_csv(flagship, FLAGSHIP)

    REPORT.write_text(build_report(rows), encoding="utf-8")
    print(f"wrote {OUT} ({len(rows)} rows, {OUT.stat().st_size:,} bytes)")
    print(f"wrote {FLAGSHIP} ({len(flagship)} rows)")
    print(f"wrote {REPORT} ({REPORT.stat().st_size:,} bytes)")
    print(f"publishable: {sum(1 for row in rows if row.get('publishable'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
