"""Write FLAGSHIP_EVIDENCE.csv from the alpha backtest artifacts.

Superseded for the full table by ``run_sweep.py`` + ``merge_sweep.py``, which
write ``FINAL_RESULTS.csv`` (all runs, sorted by Sharpe) and
``FLAGSHIP_RESULTS.csv`` (the six headline rows). This script is kept because
it is the widest one-row-per-headline-config evidence view: full charge
itemisation, rebalance diagnostics and NIFTY 50 / NIFTY 500 / NSE cross-checks
in a single row.

One row per configuration. The benchmark-relative columns use the **NIFTY 50**
(Yahoo ^NSEI) full 2013-2026 window; the NSE NIFTY 50 and NIFTY 500 columns are
robustness views. Percentages are written in percent units (``19.30`` means
19.30%), money in INR, and ratios as decimals. Empty cells mean the metric does
not apply to that row (for example, the zero-cost counterfactual pays no
charges).

Run after ``run_alpha_backtest.py`` and ``compute_alpha.py``.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
STRATEGY = HERE / "strategy-results.json"
ALPHA = HERE / "alpha-results.json"
OUT = HERE / "FLAGSHIP_EVIDENCE.csv"

PRIMARY = "evidence-N10-L63-next_open-no_stop"
RISK = "evidence-N10-L63-next_open-sl0.10"
BREADTH = "evidence-N20-L63-next_open-no_stop"
BOTTOM = "evidence-N20-L63-next_open-no_stop-bottom"
GROSS = "evidence-N10-L63-next_open-no_stop-gross_of_costs"
AS_COMMITTED = "as-committed-month-end-exact-match"
ORDER = (PRIMARY, RISK, BREADTH, BOTTOM, GROSS, AS_COMMITTED)

B50 = "NIFTY50_YAHOO"
B50NSE = "NIFTY50_SPLICED_NSE"
B500 = "NIFTY500_SPLICED_NSE"

DESCRIPTION = {
    PRIMARY: "Top 10 by 63-session momentum, no stop (primary)",
    RISK: "Top 10 by 63-session momentum, 10% stop loss",
    BREADTH: "Top 20 by 63-session momentum, no stop",
    BOTTOM: "Bottom 20 by 63-session momentum, no stop (reversal)",
    GROSS: "Primary with every charge set to zero (counterfactual)",
    AS_COMMITTED: "Month-end exact-snapshot diagnostic (superseded)",
}

HEADER = [
    "configuration",
    "description",
    "direction",
    "top_n",
    "lookback_sessions",
    "stop_loss_pct",
    "rebalance_calendar",
    "rebalance_dates_count",
    "actual_rebalances",
    "first_fill",
    "last_fill",
    "performance_start",
    "performance_end",
    "sessions",
    "trades",
    "buys",
    "sells",
    "skips",
    "delisting_writeoffs",
    "unproven_universe_holds",
    "initial_equity_inr",
    "final_equity_inr",
    "total_return_pct",
    "cagr_pct",
    "annualised_volatility_pct",
    "sharpe_rf0",
    "max_drawdown_pct",
    "max_dd_peak_date",
    "max_dd_trough_date",
    "max_dd_recovery_date",
    "max_dd_peak_to_trough_days",
    "max_dd_peak_to_trough_sessions",
    "max_dd_trough_to_recovery_days",
    "max_dd_trough_to_recovery_sessions",
    "max_dd_total_days",
    "max_dd_total_sessions",
    "max_dd_recovered",
    "longest_dd_peak_date",
    "longest_dd_trough_date",
    "longest_dd_recovery_date",
    "longest_dd_total_days",
    "longest_dd_total_sessions",
    "drawdown_episodes",
    "sessions_underwater",
    "pct_sessions_underwater",
    "benchmark_primary",
    "benchmark_window_start",
    "benchmark_window_end",
    "benchmark_sessions",
    "benchmark_cagr_pct",
    "strategy_cagr_aligned_pct",
    "excess_cagr_pct",
    "beta",
    "alpha_linear_pct",
    "alpha_geometric_pct",
    "tracking_error_pct",
    "information_ratio",
    "up_capture_pct",
    "down_capture_pct",
    "r_squared",
    "t_ols",
    "t_newey_west_5",
    "bootstrap_p",
    "nifty50_nse_window",
    "nifty50_nse_benchmark_cagr_pct",
    "nifty50_nse_alpha_linear_pct",
    "nifty50_nse_t_newey_west_5",
    "nifty500_window",
    "nifty500_benchmark_cagr_pct",
    "nifty500_alpha_linear_pct",
    "nifty500_t_newey_west_5",
    "buy_notional_inr",
    "sell_notional_inr",
    "turnover_inr",
    "buy_charges_inr",
    "sell_charges_inr",
    "total_charges_inr",
    "charges_bps_of_turnover",
    "charges_pct_of_initial_cash",
    "brokerage_inr",
    "gst_inr",
    "stt_inr",
    "stamp_duty_inr",
    "exchange_inr",
    "slippage_inr",
    "zero_cost_counterfactual_cagr_pct",
    "cost_drag_cagr_pct",
    "p0_gate",
    "status",
]


def pct(value) -> str:
    return "" if value is None else f"{float(value) * 100:.6f}"


def ratio(value, digits: int = 6) -> str:
    return "" if value is None else f"{float(value):.{digits}f}"


def money(value) -> str:
    return "" if value is None else f"{float(value):.2f}"


def text(value) -> str:
    return "" if value is None else str(value)


def window(entry: dict) -> str:
    return f"{entry['first_return_date']}..{entry['last_return_date']}"


def main() -> int:
    strategy = json.loads(STRATEGY.read_text(encoding="utf-8"))
    alpha = json.loads(ALPHA.read_text(encoding="utf-8"))
    configs = {row["label"]: row for row in strategy["configs"]}

    net_cagr = float(configs[PRIMARY]["metrics"]["cagr"])
    gross_cagr = float(configs[GROSS]["metrics"]["cagr"])

    rows = []
    for label in ORDER:
        config = configs[label]
        metrics = config["metrics"]
        settings = config["config"]
        audit = config["charges"]
        components = audit["components"]
        evidence = alpha["evaluations"][label]

        primary = evidence[B50]["full_rf0"]
        nse = evidence[B50NSE]["full_rf0"]
        broad = evidence[B500]["full_rf0"]
        durations = metrics.get("drawdown_durations") or {}
        worst = durations.get("worst") or {}
        longest = durations.get("longest") or {}
        bootstrap = (primary["alpha_significance"].get("block_bootstrap") or {})

        row = {
            "configuration": label,
            "description": DESCRIPTION[label],
            "direction": config["direction"],
            "top_n": text(settings["top_n"]),
            "lookback_sessions": text(settings["lookback_sessions"]),
            "stop_loss_pct": pct(settings["exit_policy"].get("stop_loss_pct")),
            "rebalance_calendar": text(settings.get("rebalance_calendar")),
            "rebalance_dates_count": str(sum(
                1 for day in config["cadence"]["dates"] if day >= "2013-05-31")),
            "actual_rebalances": str(sum(
                1 for day in config["cadence"]["dates"] if day >= "2013-05-31")
                - sum(1 for day in config.get("unproven_universe_holds") or []
                      if day >= "2013-05-31")),
            "first_fill": text(config.get("first_trade")),
            "last_fill": text(config.get("last_trade")),
            "performance_start": text(metrics.get("start")),
            "performance_end": text(metrics.get("end")),
            "sessions": text(metrics.get("sessions_known_equity")),
            "trades": text(config.get("n_trades")),
            "buys": text(config.get("n_buys")),
            "sells": text(config.get("n_sells")),
            "skips": text(config.get("n_skips")),
            "delisting_writeoffs": text(len(config.get("delisting_writeoffs") or [])),
            "unproven_universe_holds": str(sum(
                1 for day in config.get("unproven_universe_holds") or []
                if day >= "2013-05-31")),
            "initial_equity_inr": money(metrics.get("initial_equity")),
            "final_equity_inr": money(metrics.get("final_equity")),
            "total_return_pct": pct(metrics.get("total_return")),
            "cagr_pct": pct(metrics.get("cagr")),
            "annualised_volatility_pct": pct(metrics.get("annualised_volatility")),
            "sharpe_rf0": ratio(metrics.get("sharpe_zero_rf")),
            "max_drawdown_pct": pct(metrics.get("max_drawdown")),
            "max_dd_peak_date": text(worst.get("peak_date")),
            "max_dd_trough_date": text(worst.get("trough_date")),
            "max_dd_recovery_date": text(worst.get("recovery_date")),
            "max_dd_peak_to_trough_days": text(worst.get("peak_to_trough_days")),
            "max_dd_peak_to_trough_sessions": text(worst.get("peak_to_trough_sessions")),
            "max_dd_trough_to_recovery_days": text(worst.get("trough_to_recovery_days")),
            "max_dd_trough_to_recovery_sessions": text(worst.get("trough_to_recovery_sessions")),
            "max_dd_total_days": text(worst.get("total_days")),
            "max_dd_total_sessions": text(worst.get("total_sessions")),
            "max_dd_recovered": text(worst.get("recovered")),
            "longest_dd_peak_date": text(longest.get("peak_date")),
            "longest_dd_trough_date": text(longest.get("trough_date")),
            "longest_dd_recovery_date": text(longest.get("recovery_date")),
            "longest_dd_total_days": text(longest.get("total_days")),
            "longest_dd_total_sessions": text(longest.get("total_sessions")),
            "drawdown_episodes": text(durations.get("episodes")),
            "sessions_underwater": text(durations.get("sessions_underwater")),
            "pct_sessions_underwater": pct(durations.get("pct_sessions_underwater")),
            "benchmark_primary": B50,
            "benchmark_window_start": primary["first_return_date"],
            "benchmark_window_end": primary["last_return_date"],
            "benchmark_sessions": text(primary["n_returns"]),
            "benchmark_cagr_pct": pct(primary["benchmark"]["annualised_return"]),
            "strategy_cagr_aligned_pct": pct(primary["strategy"]["annualised_return"]),
            "excess_cagr_pct": pct(primary["relative"]["tracking_difference"]),
            "beta": ratio(primary["relative"]["beta"]),
            "alpha_linear_pct": pct(primary["relative"]["jensen_alpha_linear"]),
            "alpha_geometric_pct": pct(primary["relative"]["jensen_alpha_geometric"]),
            "tracking_error_pct": pct(primary["relative"]["tracking_error"]),
            "information_ratio": ratio(primary["relative"]["information_ratio"]),
            "up_capture_pct": pct(primary["relative"]["up_capture"]),
            "down_capture_pct": pct(primary["relative"]["down_capture"]),
            "r_squared": ratio(primary["relative"]["r_squared"]),
            "t_ols": ratio(primary["alpha_significance"].get("t_ols")),
            "t_newey_west_5": ratio(primary["alpha_significance"].get("t_newey_west_5")),
            "bootstrap_p": ratio(bootstrap.get("p_two_sided")),
            "nifty50_nse_window": window(nse),
            "nifty50_nse_benchmark_cagr_pct": pct(nse["benchmark"]["annualised_return"]),
            "nifty50_nse_alpha_linear_pct": pct(nse["relative"]["jensen_alpha_linear"]),
            "nifty50_nse_t_newey_west_5": ratio(nse["alpha_significance"].get("t_newey_west_5")),
            "nifty500_window": window(broad),
            "nifty500_benchmark_cagr_pct": pct(broad["benchmark"]["annualised_return"]),
            "nifty500_alpha_linear_pct": pct(broad["relative"]["jensen_alpha_linear"]),
            "nifty500_t_newey_west_5": ratio(broad["alpha_significance"].get("t_newey_west_5")),
            "buy_notional_inr": money(audit["buy_notional"]),
            "sell_notional_inr": money(audit["sell_notional"]),
            "turnover_inr": money(audit["turnover"]),
            "buy_charges_inr": money(audit["buy_charges"]),
            "sell_charges_inr": money(audit["sell_charges"]),
            "total_charges_inr": money(audit["total_charges"]),
            "charges_bps_of_turnover": ratio(audit["charges_bps_of_turnover"], 4),
            "charges_pct_of_initial_cash": pct(audit["charges_pct_of_initial_cash"]),
            "brokerage_inr": money(components["brokerage"]),
            "gst_inr": money(components["gst"]),
            "stt_inr": money(components["stt"]),
            "stamp_duty_inr": money(components["stamp"]),
            "exchange_inr": money(components["exchange"]),
            "slippage_inr": money(components["slippage"]),
            "zero_cost_counterfactual_cagr_pct": pct(gross_cagr) if label == PRIMARY else "",
            "cost_drag_cagr_pct": pct(gross_cagr - net_cagr) if label == PRIMARY else "",
            "p0_gate": "CLOSED",
            "status": "research_diagnostic",
        }
        missing = set(HEADER) - set(row)
        extra = set(row) - set(HEADER)
        if missing or extra:
            raise AssertionError(f"row schema mismatch: missing={missing} extra={extra}")
        rows.append(row)

    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
