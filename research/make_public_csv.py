#!/usr/bin/env python
"""Build the public-facing results CSV from the internal research input.

Reads ``FINAL_RESULTS.csv`` (the internal artefact) and writes
``FINAL_RESULTS_public.csv``, a version of the same table that:

  1. Drops internal-only bookkeeping columns (gate flags, evidence labels,
     error placeholders) that describe *how the team worked* rather than
     *what the strategy did*.
  2. Replaces the internal ``gate_status`` workflow marker with a single
     plain-language ``validation_status = "research_preview"`` column, so
     readers see one honest public status instead of internal jargon.
  3. Renames columns to self-explanatory public names (e.g. ``t_newey_west_5``
     -> ``alpha_t_newey_west_5``) and maps abbreviated categorical values to
     readable words (``top`` -> ``strongest``, ``all_charges`` -> ``full_charges``).

Every change is verified before the file is written: row count, no new empty
cells, sort order, and a scan for absolute paths, e-mail addresses and any
lingering ``P0`` token.

Usage
-----
    python research/make_public_csv.py
    python research/make_public_csv.py --input FINAL_RESULTS.csv \
        --output FINAL_RESULTS_public.csv --docs CSV_COLUMNS.md

This script is read-only with respect to its input: it never writes back to
the source CSV, so the internal research artefacts stay untouched.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Public transform configuration
# --------------------------------------------------------------------------

#: Internal bookkeeping columns removed from the public release.
DROP_COLUMNS: tuple[str, ...] = (
    "gate_status",
    "publishable",
    "refusal",
    "alpha_error",
    "nifty500_alpha_error",
    "evidence_label",
    "is_flagship",
)

#: The single public status column added back in place of ``gate_status``.
VALIDATION_COLUMN = "validation_status"
VALIDATION_VALUE = "research_preview"

#: ``internal_name -> public_name``.
RENAME_COLUMNS: dict[str, str] = {
    "direction": "selection_side",
    "costs": "cost_model",
    "pct_sessions_underwater": "pct_sessions_below_running_peak",
    "t_ols": "alpha_t_ols",
    "t_newey_west_5": "alpha_t_newey_west_5",
    "alpha_linear_pct": "jensen_alpha_linear_pct",
    "alpha_geometric_pct": "jensen_alpha_geometric_pct",
    "initial_equity_inr": "initial_capital_inr",
    "final_equity_inr": "final_capital_inr",
    "delisting_writeoffs": "assumed_last_close_writeoffs",
    "unproven_universe_holds": "held_without_membership_snapshot",
    "family": "sweep_family",
    "flag_universe_overlap": "universe_overlap_note",
}

#: Value maps applied after the rename (keyed by *public* column name).
VALUE_MAPS: dict[str, dict[str, str]] = {
    "selection_side": {"top": "strongest", "bottom": "weakest"},
    "cost_model": {"all_charges": "full_charges", "zero": "zero_cost"},
}

#: Column used to order the public file, when present.
SORT_COLUMN = "rank_by_sharpe"

#: Patterns that must never appear in a public cell.
FORBIDDEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "absolute_path": re.compile(r"[A-Za-z]:\\|(?<![\w.])/(?:home|Users|mnt|var|opt|root)/|\\\\"),
    "email_address": re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"),
    "internal_p0_token": re.compile(r"\bP0\b"),
}

EXPECTED_ROWS = 968


# --------------------------------------------------------------------------
# Unit / meaning documentation for CSV_COLUMNS.md
# --------------------------------------------------------------------------

#: ``column -> (unit, meaning)``. Kept explicit so the docs stay reviewed
#: rather than generated from guesses.
COLUMN_DOCS: dict[str, tuple[str, str]] = {
    "rank_by_sharpe": ("rank", "Position of this configuration when all configurations are sorted by Sharpe ratio (risk-free rate 0), best first."),
    "rank_by_cagr": ("rank", "Position when sorted by compound annual growth rate, best first."),
    "rank_by_alpha": ("rank", "Position when sorted by annualised Jensen alpha versus the NIFTY 50, best first."),
    "config_id": ("identifier", "Deterministic readable identifier of the configuration: universe, selection side, position count, lookback, rebalance cadence and exit overlay."),
    "sweep_family": ("category", "Which sweep grid produced this configuration (for example 'exit_grid_monthly', 'universe_core'). Rows in the same family share a generation process."),
    "universe": ("category", "Stock universe key used to select candidates, e.g. 'nifty500', 'midcap150', 'smallcap250'."),
    "universe_label": ("text", "Human-readable description of the universe."),
    "selection_side": ("category", "'strongest' ranks by highest momentum (long the leaders); 'weakest' ranks by lowest momentum (long the laggards)."),
    "top_n": ("stocks", "Number of positions held at each rebalance."),
    "universe_snapshots": ("count", "Number of point-in-time membership snapshots used to define the investable universe."),
    "universe_first_snapshot": ("date (YYYY-MM-DD)", "Date of the earliest membership snapshot used."),
    "universe_last_snapshot": ("date (YYYY-MM-DD)", "Date of the latest membership snapshot used."),
    "universe_members_min": ("stocks", "Smallest number of constituents seen across the snapshots."),
    "universe_members_max": ("stocks", "Largest number of constituents seen across the snapshots."),
    "membership_note": ("text", "Caveat about how complete or regular the membership history is."),
    "lookback_sessions": ("trading sessions", "Momentum lookback length, in sessions, used to rank stocks."),
    "cadence": ("category", "Rebalance frequency: 'monthly', 'quarterly', 'semiannual', 'annual', '2y' or '3y'."),
    "holding_period_months": ("months", "Nominal holding period implied by the cadence."),
    "calendar_mode": ("category", "How rebalance dates were chosen: 'evidence' (from the recorded snapshot calendar) or 'month_end_exact'."),
    "stop_loss_pct": ("fraction", "Stop-loss trigger: exit a position once it falls this fraction below entry."),
    "take_profit_pct": ("fraction", "Take-profit trigger: exit a position once it rises this fraction above entry."),
    "exit_overlay": ("category", "Name of the stop-loss / take-profit pair applied, or 'none'."),
    "cost_model": ("category", "'full_charges' applies brokerage, taxes, stamp duty, exchange fees, GST and slippage; 'zero_cost' is a frictionless counterfactual and is not investable."),
    "scheduled_rebalances": ("count", "Number of rebalance dates the schedule fired."),
    "signal_dates_with_trades": ("count", "Number of rebalance dates on which at least one trade actually executed."),
    "held_without_membership_snapshot": ("count", "Times a holding was kept because no fresh membership snapshot existed for the date, rather than being sold."),
    "last_signal_date": ("date (YYYY-MM-DD)", "Date the most recent momentum ranking was computed."),
    "first_fill": ("date (YYYY-MM-DD)", "Date of the first executed trade."),
    "last_fill": ("date (YYYY-MM-DD)", "Date of the last executed trade."),
    "performance_start": ("date (YYYY-MM-DD)", "First date included in the performance measurement window."),
    "performance_end": ("date (YYYY-MM-DD)", "Last date included in the performance measurement window."),
    "scheduled_horizon_end": ("date (YYYY-MM-DD)", "Date the schedule intended to run to, before data availability was considered."),
    "sessions": ("trading sessions", "Number of trading sessions in the performance window."),
    "window_years": ("years", "Length of the performance window in calendar years."),
    "calendar_days": ("calendar days", "Length of the performance window in calendar days."),
    "trades": ("count", "Total executed trades (buys plus sells)."),
    "buys": ("count", "Executed buy orders."),
    "sells": ("count", "Executed sell orders."),
    "skips": ("count", "Orders that were generated but did not execute."),
    "skip_reasons": ("text", "Counts of skip causes, e.g. 'buy:insufficient_cash=2; sell:no_bar_delisted_or_suspended=8'."),
    "assumed_last_close_writeoffs": ("count", "Positions written off at the last available close because the stock stopped trading (delisting or long suspension)."),
    "initial_capital_inr": ("INR", "Starting portfolio value."),
    "final_capital_inr": ("INR", "Ending portfolio value, after costs and write-offs."),
    "total_return_pct": ("percent", "Simple return over the window, without annualisation."),
    "cagr_pct": ("percent per year", "Compound annual growth rate over the window."),
    "annualised_volatility_pct": ("percent per year", "Standard deviation of daily returns, annualised."),
    "sharpe_rf0": ("ratio", "Mean daily excess return (risk-free rate assumed 0) divided by daily volatility, annualised."),
    "max_drawdown_pct": ("percent", "Largest peak-to-trough decline of the equity curve."),
    "max_dd_peak_date": ("date (YYYY-MM-DD)", "Date of the peak before the largest drawdown."),
    "max_dd_trough_date": ("date (YYYY-MM-DD)", "Date of the trough of the largest drawdown."),
    "max_dd_recovery_date": ("date (YYYY-MM-DD)", "Date the equity curve regained the prior peak; blank if it never did."),
    "max_dd_peak_to_trough_days": ("calendar days", "Calendar days from peak to trough of the largest drawdown."),
    "max_dd_peak_to_trough_sessions": ("trading sessions", "Trading sessions from peak to trough of the largest drawdown."),
    "max_dd_trough_to_recovery_days": ("calendar days", "Calendar days from trough back to the prior peak; blank if unrecovered."),
    "max_dd_trough_to_recovery_sessions": ("trading sessions", "Trading sessions from trough back to the prior peak; blank if unrecovered."),
    "max_dd_total_days": ("calendar days", "Total calendar days under water for the largest drawdown."),
    "max_dd_total_sessions": ("trading sessions", "Total sessions under water for the largest drawdown."),
    "max_dd_recovered": ("boolean", "Whether the equity curve recovered to its prior peak after the largest drawdown."),
    "longest_dd_peak_date": ("date (YYYY-MM-DD)", "Peak date of the longest drawdown, which may differ from the deepest one."),
    "longest_dd_trough_date": ("date (YYYY-MM-DD)", "Trough date of the longest drawdown."),
    "longest_dd_recovery_date": ("date (YYYY-MM-DD)", "Recovery date of the longest drawdown; blank if unrecovered."),
    "longest_dd_total_days": ("calendar days", "Duration of the longest drawdown in calendar days."),
    "longest_dd_total_sessions": ("trading sessions", "Duration of the longest drawdown in sessions."),
    "drawdown_episodes": ("count", "Number of distinct drawdown episodes."),
    "sessions_underwater": ("trading sessions", "Sessions where the equity curve was below its running peak."),
    "pct_sessions_below_running_peak": ("percent", "Share of sessions where the equity curve was below its running peak."),
    "benchmark": ("text", "Benchmark used for beta and alpha, including its price or total-return basis."),
    "benchmark_window_start": ("date (YYYY-MM-DD)", "First date of the benchmark measurement window."),
    "benchmark_window_end": ("date (YYYY-MM-DD)", "Last date of the benchmark measurement window."),
    "benchmark_sessions": ("trading sessions", "Number of benchmark observations in the window."),
    "benchmark_cagr_pct": ("percent per year", "Benchmark CAGR over the same window."),
    "strategy_cagr_aligned_pct": ("percent per year", "Strategy CAGR recomputed over exactly the benchmark window, so the comparison is like-for-like."),
    "excess_cagr_pct": ("percent per year", "Aligned strategy CAGR minus benchmark CAGR."),
    "beta": ("ratio", "Sensitivity of strategy daily returns to benchmark daily returns."),
    "jensen_alpha_linear_pct": ("percent per year", "Jensen alpha from a linear regression of daily excess returns on the benchmark; annualised."),
    "jensen_alpha_geometric_pct": ("percent per year", "Jensen alpha computed on geometric (compounded) returns; differs from the linear figure when returns are volatile."),
    "tracking_error_pct": ("percent per year", "Annualised standard deviation of the strategy-minus-benchmark return."),
    "information_ratio": ("ratio", "Excess CAGR divided by tracking error."),
    "up_capture_pct": ("percent", "Strategy return captured in benchmark-up sessions relative to the benchmark."),
    "down_capture_pct": ("percent", "Strategy return captured in benchmark-down sessions relative to the benchmark; lower is better."),
    "r_squared": ("ratio 0-1", "Share of strategy return variance explained by the benchmark."),
    "alpha_t_ols": ("t-statistic", "Ordinary-least-squares t-statistic for the alpha term. Above about 2 is conventionally labelled significant, though this is only one window."),
    "alpha_t_newey_west_5": ("t-statistic", "t-statistic for alpha using Newey-West standard errors with 5 lags, which correct for autocorrelation and are the more conservative figure."),
    "nifty500_benchmark": ("text", "Secondary benchmark (NIFTY 500) used for the companion block of columns."),
    "nifty500_window_start": ("date (YYYY-MM-DD)", "First date of the NIFTY 500 measurement window."),
    "nifty500_window_end": ("date (YYYY-MM-DD)", "Last date of the NIFTY 500 measurement window."),
    "nifty500_sessions": ("trading sessions", "Number of NIFTY 500 observations in the window."),
    "nifty500_benchmark_cagr_pct": ("percent per year", "NIFTY 500 CAGR over the window."),
    "nifty500_strategy_cagr_aligned_pct": ("percent per year", "Strategy CAGR recomputed over the NIFTY 500 window."),
    "nifty500_excess_cagr_pct": ("percent per year", "Aligned strategy CAGR minus NIFTY 500 CAGR."),
    "nifty500_beta": ("ratio", "Sensitivity to NIFTY 500 daily returns."),
    "nifty500_alpha_linear_pct": ("percent per year", "Annualised Jensen alpha versus the NIFTY 500 (linear)."),
    "nifty500_alpha_geometric_pct": ("percent per year", "Annualised Jensen alpha versus the NIFTY 500 (geometric)."),
    "nifty500_tracking_error_pct": ("percent per year", "Annualised tracking error versus the NIFTY 500."),
    "nifty500_information_ratio": ("ratio", "Excess CAGR divided by tracking error, versus the NIFTY 500."),
    "nifty500_up_capture_pct": ("percent", "Up-capture versus the NIFTY 500."),
    "nifty500_down_capture_pct": ("percent", "Down-capture versus the NIFTY 500."),
    "nifty500_r_squared": ("ratio 0-1", "Share of return variance explained by the NIFTY 500."),
    "nifty500_t_ols": ("t-statistic", "OLS t-statistic for alpha versus the NIFTY 500."),
    "nifty500_t_newey_west_5": ("t-statistic", "Newey-West (5 lag) t-statistic for alpha versus the NIFTY 500."),
    "buy_notional_inr": ("INR", "Total value of executed buys."),
    "sell_notional_inr": ("INR", "Total value of executed sells."),
    "turnover_inr": ("INR", "Total traded value."),
    "buy_charges_inr": ("INR", "All transaction costs incurred on buys."),
    "sell_charges_inr": ("INR", "All transaction costs incurred on sells."),
    "total_charges_inr": ("INR", "Total transaction costs, equal to the sum of their components below."),
    "brokerage_inr": ("INR", "Brokerage paid."),
    "gst_inr": ("INR", "Goods and services tax on brokerage and exchange fees."),
    "stt_inr": ("INR", "Securities transaction tax."),
    "stamp_inr": ("INR", "Stamp duty."),
    "exchange_inr": ("INR", "Exchange transaction charges."),
    "slippage_inr": ("INR", "Assumed cost of filling at a worse price than the reference price."),
    "charges_bps_of_turnover": ("basis points", "Total costs expressed as basis points of turnover."),
    "charges_pct_of_initial_cash": ("percent", "Total costs expressed as a percentage of starting capital."),
    "flag_few_rebalances": ("boolean", "True when the run has too few rebalances for the ranking statistics to be reliable."),
    "flag_short_window": ("boolean", "True when the measurement window is short relative to the lookback and rebalance cadence."),
    "flag_exit_cash_until_rebalance": ("boolean", "True when a stop-loss or take-profit exit left the slot in cash until the next scheduled rebalance rather than redeploying immediately."),
    "universe_overlap_note": ("text", "How this universe overlaps the others in the sweep, so correlated rows are not mistaken for independent evidence."),
    "flag_superseded": ("boolean", "True when a later, corrected run replaced this configuration."),
    "price_basis": ("text", "How adjusted prices were built, e.g. split/bonus adjusted with dividends excluded."),
    "benchmark_basis": ("text", "Price or total-return basis of the benchmark index."),
    "dividend_treatment": ("text", "Whether dividends are included, and on which side of the comparison."),
    "validation_status": ("category", "Public validation state of this row. 'research_preview' means the numbers come from an internal research sweep, have not been independently replicated, and are published to invite scrutiny rather than as a track record."),
    "notes": ("text", "Free-text remarks about the run, including data caveats."),
}


def load_source(path: Path) -> pd.DataFrame:
    """Read the internal CSV, preserving the blank/empty distinction as empty text."""
    if not path.is_file():
        raise SystemExit(f"error: input CSV not found: {path}")
    # dtype=str + keep_default_na=False keeps every cell exactly as written,
    # so a blank stays "" instead of silently becoming NaN.
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[])


def transform(df: pd.DataFrame) -> pd.DataFrame:
    """Apply drops, renames, value maps and the public status column."""
    missing = [c for c in DROP_COLUMNS if c not in df.columns]
    if missing:
        raise SystemExit(f"error: expected internal columns are absent: {missing}")

    absent_renames = [c for c in RENAME_COLUMNS if c not in df.columns]
    if absent_renames:
        raise SystemExit(f"error: expected internal columns are absent: {absent_renames}")

    clashes = set(RENAME_COLUMNS.values()) & set(df.columns)
    if clashes:
        raise SystemExit(f"error: rename would collide with existing columns: {sorted(clashes)}")

    out = df.drop(columns=list(DROP_COLUMNS)).rename(columns=RENAME_COLUMNS)

    for column, mapping in VALUE_MAPS.items():
        unknown = sorted(set(out[column].unique()) - set(mapping))
        if unknown:
            raise SystemExit(f"error: unexpected {column} values {unknown}; refusing to publish")
        out[column] = out[column].map(mapping)

    out[VALIDATION_COLUMN] = VALIDATION_VALUE

    if SORT_COLUMN in out.columns:
        out = out.sort_values(SORT_COLUMN, key=lambda s: pd.to_numeric(s), kind="mergesort")
        out = out.reset_index(drop=True)

    return out


def verify(source: pd.DataFrame, public: pd.DataFrame, expected_rows: int = EXPECTED_ROWS) -> list[str]:
    """Run every acceptance check; return a list of human-readable lines."""
    checks: list[str] = []

    # 1. Row count.
    if len(public) != len(source):
        raise SystemExit(f"error: row count changed {len(source)} -> {len(public)}")
    if len(public) != expected_rows:
        raise SystemExit(f"error: expected {expected_rows} rows, got {len(public)}")
    checks.append(f"rows preserved: {len(public)} == {expected_rows}")

    # 2. Every dropped column (and its public replacement semantics) is gone.
    still_there = [c for c in DROP_COLUMNS if c in public.columns]
    if still_there:
        raise SystemExit(f"error: internal columns survived the drop: {still_there}")
    checks.append(f"dropped {len(DROP_COLUMNS)} internal columns: {', '.join(DROP_COLUMNS)}")

    old_names = [c for c in RENAME_COLUMNS if c in public.columns]
    if old_names:
        raise SystemExit(f"error: pre-rename names survived: {old_names}")
    checks.append(f"renamed {len(RENAME_COLUMNS)} columns; none of the old names remain")

    # 3. validation_status present and uniform.
    if VALIDATION_COLUMN not in public.columns:
        raise SystemExit(f"error: missing {VALIDATION_COLUMN}")
    values = set(public[VALIDATION_COLUMN].unique())
    if values != {VALIDATION_VALUE}:
        raise SystemExit(f"error: {VALIDATION_COLUMN} values are {values}")
    checks.append(f"{VALIDATION_COLUMN} = '{VALIDATION_VALUE}' on all {len(public)} rows")

    # 4. No empty cell introduced anywhere.
    new_empty: dict[str, int] = {}
    for column in public.columns:
        if column == VALIDATION_COLUMN:
            continue
        new_count = int((public[column] == "").sum())
        old_name = {v: k for k, v in RENAME_COLUMNS.items()}.get(column, column)
        old_count = int((source[old_name] == "").sum())
        if new_count > old_count:
            new_empty[column] = new_count - old_count
    if new_empty:
        raise SystemExit(f"error: transform introduced empty cells: {new_empty}")
    checks.append("no empty/NaN cell introduced (blank counts match the source column by column)")

    # 5. Sort order.
    if SORT_COLUMN in public.columns:
        ranks = pd.to_numeric(public[SORT_COLUMN])
        if not ranks.is_monotonic_increasing:
            raise SystemExit(f"error: output is not sorted by {SORT_COLUMN}")
        checks.append(f"sorted ascending by {SORT_COLUMN} (1..{int(ranks.max())})")
    else:
        checks.append(f"no {SORT_COLUMN} column; sort check skipped")

    # 6. Forbidden tokens in any cell (and in the header itself).
    offenders: dict[str, list[str]] = {}
    scan_frame = public.astype(str)
    for label, pattern in FORBIDDEN_PATTERNS.items():
        for column in scan_frame.columns:
            if pattern.search(column):
                offenders.setdefault(label, []).append(f"header={column!r}")
            for value in pd.unique(scan_frame[column]):
                if pattern.search(value):
                    offenders.setdefault(label, []).append(f"{column}={value!r}")
    if offenders:
        raise SystemExit(f"error: forbidden content found: {offenders}")
    checks.append(
        "no absolute paths, e-mail addresses or 'P0' tokens in any cell or header "
        f"({len(public.columns)} columns x {len(public)} rows scanned)"
    )

    return checks


def write_docs(public: pd.DataFrame, path: Path) -> int:
    """Emit CSV_COLUMNS.md documenting every public column."""
    undocumented = [c for c in public.columns if c not in COLUMN_DOCS]
    if undocumented:
        raise SystemExit(f"error: public columns lack documentation: {undocumented}")

    lines: list[str] = [
        "# Results CSV columns",
        "",
        "Documentation for `FINAL_RESULTS_public.csv`, the public-facing results table",
        "produced from the internal research sweep by `research/make_public_csv.py`.",
        "",
        "## Status of these numbers",
        "",
        f"Every row carries `validation_status = \"{VALIDATION_VALUE}\"`. That means:",
        "",
        "- the figures come from an internal research sweep, not a live or audited track record;",
        "- they have not been independently replicated;",
        "- they are published so that others can inspect and refute them, not as a recommendation.",
        "",
        "Costs assume a fixed slippage model and static brokerage assumptions rather than real fills.",
        "Dividends are excluded on both sides and the benchmarks are price indices, so reported",
        "alpha is measured against price returns, not total returns.",
        "",
        "## Conventions",
        "",
        "- **Percent** columns are plain numbers where `12.5` means 12.5%, not a 0.125 fraction.",
        "- **Fraction** columns are where `0.15` means 15%.",
        "- **INR** columns are Indian rupees.",
        "- **Dates** are `YYYY-MM-DD`. A blank means the event did not happen, for example a",
        "  drawdown that was never recovered.",
        "- **Blank is meaningful**: blanks are left blank rather than filled with zero, because",
        "  zero would misstate the result.",
        "",
        f"The file has **{len(public)} rows** and **{len(public.columns)} columns**.",
        "",
        "## Columns",
        "",
        "| # | Column | Unit | Meaning |",
        "|---:|---|---|---|",
    ]

    for index, column in enumerate(public.columns, start=1):
        unit, meaning = COLUMN_DOCS[column]
        lines.append(f"| {index} | `{column}` | {unit} | {meaning} |")

    dropped = ", ".join(f"`{c}`" for c in DROP_COLUMNS)
    renames = "; ".join(f"`{a}` to `{b}`" for a, b in RENAME_COLUMNS.items())
    lines += [
        "",
        "## What differs from the internal file",
        "",
        f"Removed internal bookkeeping columns: {dropped}.",
        "",
        "Those columns described the team's internal review process rather than the strategies,",
        f"so they are dropped. `validation_status` replaces them with one plain public status.",
        "",
        f"Renamed for clarity: {renames}.",
        "",
        "Values made readable: `top` becomes `strongest` and `bottom` becomes `weakest` in",
        "`selection_side`; `all_charges` becomes `full_charges` and `zero` becomes `zero_cost`",
        "in `cost_model`.",
        "",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    return len(public.columns)


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default=str(here / "FINAL_RESULTS.csv"), help="internal results CSV to read")
    parser.add_argument("--output", default=str(here / "FINAL_RESULTS_public.csv"), help="public CSV to write")
    parser.add_argument("--docs", default=str(here / "CSV_COLUMNS.md"), help="markdown column reference to write")
    parser.add_argument(
        "--expected-rows",
        type=int,
        default=None,
        help="assert the row count (default: 968 for FINAL_RESULTS, otherwise the input's own count)",
    )
    args = parser.parse_args(argv)

    source_path = Path(args.input)
    output_path = Path(args.output)
    docs_path = Path(args.docs)

    source = load_source(source_path)
    print(f"input : {source_path.name}")
    print(f"shape before: {source.shape[0]} rows x {source.shape[1]} columns")

    # An explicit flag wins; else the flagship file's own count; else the sweep's 968.
    expected_rows = (
        args.expected_rows
        if args.expected_rows is not None
        else (len(source) if "FLAGSHIP" in source_path.name.upper() else EXPECTED_ROWS)
    )

    public = transform(source)
    checks = verify(source, public, expected_rows)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    public.to_csv(output_path, index=False, encoding="utf-8")
    documented = write_docs(public, docs_path)

    print(f"shape after : {public.shape[0]} rows x {public.shape[1]} columns")
    print()
    for line in checks:
        print(f"  ok  {line}")
    print()
    print(f"wrote {output_path}  ({public.shape[0]} rows x {public.shape[1]} columns)")
    print(f"wrote {docs_path}  ({documented} columns documented)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
