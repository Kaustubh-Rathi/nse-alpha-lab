"""Fail-closed validation of the alpha backtest artifacts.

Independent of ``compute_alpha.py``: this script recomputes the headline
regression from the raw equity curve and benchmark closes with numpy, then
compares it to the Decimal metrics in ``alpha-results.json``. It also checks
that every published configuration had a fully known equity curve, that the
evidence-dated calendar really is evidence-dated, and that no explicit-calendar
rebalance silently held an unproven universe.

Writes ``validation.json``. Exit code 1 on any failed check.
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
STRATEGY = HERE / "strategy-results.json"
BENCHMARK = HERE / "benchmark-series.json"
ALPHA = HERE / "alpha-results.json"
FINAL_CSV = HERE / "FINAL_RESULTS.csv"
FLAGSHIP_CSV = HERE / "FLAGSHIP_RESULTS.csv"
OUT = HERE / "validation.json"

PPY = 252
CHECKS = []

PRIMARY = "evidence-N10-L63-next_open-no_stop"
BOTTOM = "evidence-N20-L63-next_open-no_stop-bottom"
GROSS = "evidence-N10-L63-next_open-no_stop-gross_of_costs"
B50 = "NIFTY50_YAHOO"
EXPECTED = {
    PRIMARY,
    "evidence-N10-L63-next_open-sl0.10",
    "evidence-N20-L63-next_open-no_stop",
    BOTTOM,
    GROSS,
    "as-committed-month-end-exact-match",
}

EVIDENCE_BY_CONFIG = {
    "nifty500_top_N10_L63_monthly_none": PRIMARY,
    "nifty500_top_N10_L63_monthly_sl10":
        "evidence-N10-L63-next_open-sl0.10",
    "nifty500_top_N20_L63_monthly_none":
        "evidence-N20-L63-next_open-no_stop",
    "nifty500_bottom_N20_L63_monthly_none": BOTTOM,
    "nifty500_top_N10_L63_monthly_none_zero_cost": GROSS,
    "nifty500_top_N10_L63_monthly_none_month_end_exact":
        "as-committed-month-end-exact-match",
}


def check(name: str, ok: bool, detail) -> None:
    CHECKS.append({"name": name, "ok": bool(ok), "detail": detail})


def load_equity(config: dict) -> dict[date, Decimal]:
    return {date.fromisoformat(row["date"]): Decimal(row["equity"])
            for row in config["equity"]}


def manual_episodes(curve: list[tuple[date, Decimal]]) -> list[dict]:
    """Independent peak-to-trough-to-recovery decomposition for validation."""
    episodes: list[dict] = []
    peak_date, peak_equity = curve[0]
    trough_date, trough_equity = peak_date, peak_equity
    in_episode = False
    for point_date, equity in curve[1:]:
        if equity >= peak_equity:
            if in_episode:
                episodes.append({
                    "peak_date": peak_date, "trough_date": trough_date,
                    "recovery_date": point_date,
                    "depth": trough_equity / peak_equity - 1,
                })
                in_episode = False
            peak_date, peak_equity = point_date, equity
            trough_date, trough_equity = point_date, equity
            continue
        if equity < trough_equity:
            trough_date, trough_equity = point_date, equity
            in_episode = True
    if in_episode:
        episodes.append({
            "peak_date": peak_date, "trough_date": trough_date,
            "recovery_date": None,
            "depth": trough_equity / peak_equity - 1,
        })
    return episodes


def duration_record(episode: dict, curve: list[tuple[date, Decimal]]) -> dict:
    indexes = {day: index for index, (day, _) in enumerate(curve)}
    end_date = episode["recovery_date"] or curve[-1][0]
    return {
        "peak_date": episode["peak_date"].isoformat(),
        "trough_date": episode["trough_date"].isoformat(),
        "recovery_date": (episode["recovery_date"].isoformat()
                          if episode["recovery_date"] else None),
        "depth": str(episode["depth"]),
        "recovered": episode["recovery_date"] is not None,
        "peak_to_trough_days": (episode["trough_date"] - episode["peak_date"]).days,
        "peak_to_trough_sessions": (indexes[episode["trough_date"]]
                                    - indexes[episode["peak_date"]]),
        "trough_to_recovery_days": ((episode["recovery_date"]
                                     - episode["trough_date"]).days
                                    if episode["recovery_date"] else None),
        "trough_to_recovery_sessions": ((indexes[episode["recovery_date"]]
                                         - indexes[episode["trough_date"]])
                                        if episode["recovery_date"] else None),
        "total_days": (end_date - episode["peak_date"]).days,
        "total_sessions": indexes[end_date] - indexes[episode["peak_date"]],
    }


def load_closes(payload: dict, key: str) -> dict[date, float]:
    return {date.fromisoformat(day): float(close)
            for day, close in payload["series"][key]}


def paired_returns(equity: dict[date, Decimal], closes: dict[date, float]):
    common = sorted(set(equity) & set(closes))
    s, b = [], []
    for previous, current in zip(common, common[1:]):
        s.append(float(equity[current] / equity[previous] - 1))
        b.append(closes[current] / closes[previous] - 1.0)
    return np.array(s), np.array(b), common[1:]


def ols(y, x):
    design = np.column_stack([np.ones(len(x)), x])
    coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
    residuals = y - design @ coefficients
    dof = len(x) - 2
    sigma2 = float(residuals @ residuals) / dof
    xtx_inv = np.linalg.inv(design.T @ design)
    se = math.sqrt(max(sigma2 * float(xtx_inv[0, 0]), 0.0))
    return coefficients, se


def close_enough(actual, expected, *, rel=1e-6, absolute=1e-12) -> bool:
    actual = float(actual)
    expected = float(expected)
    return abs(actual - expected) <= max(absolute, rel * max(abs(actual), abs(expected)))


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


def main() -> int:
    strategy = json.loads(STRATEGY.read_text(encoding="utf-8"))
    benchmark = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    alpha = json.loads(ALPHA.read_text(encoding="utf-8"))

    configs = {row["label"]: row for row in strategy["configs"]}
    check("expected_configs_present", set(configs) == EXPECTED, sorted(configs))

    for label, config in configs.items():
        metrics = config["metrics"]
        check(f"{label}:publishable", metrics.get("publishable") is True,
              metrics.get("refusal"))
        check(f"{label}:no_unknown_equity",
              metrics.get("sessions_unknown_equity") == 0,
              metrics.get("sessions_unknown_equity"))
        rows = config["equity"]
        check(f"{label}:equity_rows", len(rows) == metrics.get("sessions_known_equity"),
              [len(rows), metrics.get("sessions_known_equity")])
        check(f"{label}:equity_non_null", all(row["equity"] is not None for row in rows),
              sum(1 for row in rows if row["equity"] is None))
        dates = [row["date"] for row in rows]
        check(f"{label}:dates_sorted", dates == sorted(set(dates)),
              [len(dates), len(set(dates))])
        growth = Decimal(rows[-1]["equity"]) / Decimal(rows[0]["equity"]) - 1
        check(f"{label}:total_return_identity",
              close_enough(growth, metrics["total_return"]),
              [str(growth), metrics["total_return"]])

        curve = [(date.fromisoformat(row["date"]), Decimal(row["equity"]))
                 for row in rows]
        episodes = manual_episodes(curve)
        details = metrics.get("drawdown_durations")
        check(f"{label}:drawdown_details_present",
              isinstance(details, dict), type(details).__name__)
        if not isinstance(details, dict):
            continue
        if not episodes:
            check(f"{label}:no_drawdown_matches",
                  details["episodes"] == 0 and details["worst"] is None,
                  [details.get("episodes"), details.get("worst")])
            continue

        records = [duration_record(episode, curve) for episode in episodes]
        worst = min(records, key=lambda record: Decimal(record["depth"]))
        longest = max(records, key=lambda record: record["total_days"])
        reported_worst = details["worst"]
        reported_longest = details["longest"]
        check(f"{label}:drawdown_episode_count",
              details["episodes"] == len(episodes),
              [details["episodes"], len(episodes)])
        check(f"{label}:max_drawdown_depth",
              close_enough(metrics["max_drawdown"], worst["depth"]),
              [metrics["max_drawdown"], worst["depth"]])
        for field in ("peak_date", "trough_date", "recovery_date",
                      "peak_to_trough_days", "peak_to_trough_sessions",
                      "trough_to_recovery_days", "trough_to_recovery_sessions",
                      "total_days", "total_sessions", "recovered"):
            check(f"{label}:worst_drawdown_{field}",
                  reported_worst.get(field) == worst[field],
                  [reported_worst.get(field), worst[field]])
        for field in ("peak_date", "trough_date", "recovery_date",
                      "total_days", "total_sessions"):
            check(f"{label}:longest_drawdown_{field}",
                  reported_longest.get(field) == longest[field],
                  [reported_longest.get(field), longest[field]])

        running_peak = curve[0][1]
        underwater = 0
        for _, equity in curve:
            if equity > running_peak:
                running_peak = equity
            elif equity < running_peak:
                underwater += 1
        check(f"{label}:sessions_underwater",
              details["sessions_underwater"] == underwater,
              [details["sessions_underwater"], underwater])
        check(f"{label}:pct_sessions_underwater",
              close_enough(details["pct_sessions_underwater"],
                           Decimal(underwater) / Decimal(len(curve))),
              [details["pct_sessions_underwater"], underwater, len(curve)])

    for label in (PRIMARY,
                  "evidence-N10-L63-next_open-sl0.10",
                  "evidence-N20-L63-next_open-no_stop",
                  BOTTOM,
                  GROSS):
        check(f"{label}:no_unproven_universe_holds",
              configs[label]["unproven_universe_holds"] == [],
              configs[label]["unproven_universe_holds"][:5])

    check("primary_direction_top",
          configs[PRIMARY]["config"]["selection_direction"] == "top",
          configs[PRIMARY]["config"]["selection_direction"])
    check("bottom_direction_bottom",
          configs[BOTTOM]["config"]["selection_direction"] == "bottom",
          configs[BOTTOM]["config"]["selection_direction"])
    check("bottom_engine_label_marked",
          "-bottom" in configs[BOTTOM]["engine_label"],
          configs[BOTTOM]["engine_label"])
    check("bottom_and_top_select_different_books",
          configs[BOTTOM]["final_positions"] != configs[PRIMARY]["final_positions"],
          [len(configs[BOTTOM]["final_positions"]),
           len(configs[PRIMARY]["final_positions"])])

    # Charges: every costed run must have paid a positive amount on both sides,
    # the itemisation must reconcile to the stored total, and the zero-cost
    # counterfactual must end with more money than the net run.
    for label, config in configs.items():
        audit = config["charges"]
        total = Decimal(audit["total_charges"])
        component_sum = sum((Decimal(value)
                             for value in audit["components"].values()), Decimal(0))
        check(f"{label}:charges_reconcile", total == component_sum,
              [str(total), str(component_sum)])
        if label == GROSS:
            check(f"{label}:zero_charges", total == Decimal(0), str(total))
        else:
            check(f"{label}:buy_charges_positive",
                  Decimal(audit["buy_charges"]) > 0, audit["buy_charges"])
            check(f"{label}:sell_charges_positive",
                  Decimal(audit["sell_charges"]) > 0, audit["sell_charges"])
    check("gross_costs_raise_final_equity",
          Decimal(configs[GROSS]["metrics"]["final_equity"])
          > Decimal(configs[PRIMARY]["metrics"]["final_equity"]),
          [configs[GROSS]["metrics"]["final_equity"],
           configs[PRIMARY]["metrics"]["final_equity"]])

    calendar = strategy["calendar"]
    check("calendar_count", calendar["count"] == 171, calendar["count"])
    check("calendar_composition",
          calendar["composition"]["batch8_monthly_exact_snapshots"] == 107
          and calendar["composition"]["batch9_post_2022_anchors"] == 64,
          calendar["composition"])
    check("universe_no_empty_dates", strategy["universe"]["dates_with_empty_universe"] == [],
          strategy["universe"]["dates_with_empty_universe"])
    check("universe_min_members", strategy["universe"]["members_per_date"]["min"] >= 450,
          strategy["universe"]["members_per_date"])

    closes = {
        "NIFTY500_SPLICED_NSE": load_closes(benchmark, "NIFTY500_SPLICED_NSE"),
        "NIFTY50_SPLICED_NSE": load_closes(benchmark, "NIFTY50_SPLICED_NSE"),
        "NIFTY50_YAHOO": load_closes(benchmark, "NIFTY50_YAHOO"),
    }
    for name, series in closes.items():
        days = sorted(series)
        check(f"benchmark:{name}:sorted_unique", len(days) == len(set(days)),
              [len(days), len(set(days))])
        check(f"benchmark:{name}:positive", all(value > 0 for value in series.values()),
              min(series.values()))
    check("benchmark:nifty500_spliced_coverage",
          min(closes["NIFTY500_SPLICED_NSE"]) == date(2012, 2, 21)
          and max(closes["NIFTY500_SPLICED_NSE"]) == date(2025, 12, 31),
          [min(closes["NIFTY500_SPLICED_NSE"]).isoformat(),
           max(closes["NIFTY500_SPLICED_NSE"]).isoformat()])

    for label in (PRIMARY, BOTTOM):
        equity = load_equity(configs[label])
        for name, series in closes.items():
            s, b, _ = paired_returns(equity, series)
            coefficients, se = ols(s, b)
            alpha_daily, beta = float(coefficients[0]), float(coefficients[1])
            alpha_linear = alpha_daily * PPY
            alpha_geometric = (1.0 + alpha_daily) ** PPY - 1.0
            active = s - b
            tracking_error = float(np.std(active, ddof=1)) * math.sqrt(PPY)

            reported = alpha["evaluations"][label][name]["full_rf0"]
            relative = reported["relative"]
            check(f"recompute:{label}:{name}:n", reported["n_returns"] == len(s),
                  [reported["n_returns"], len(s)])
            check(f"recompute:{label}:{name}:beta",
                  close_enough(relative["beta"], beta),
                  [relative["beta"], beta])
            check(f"recompute:{label}:{name}:alpha_linear",
                  close_enough(relative["jensen_alpha_linear"], alpha_linear),
                  [relative["jensen_alpha_linear"], alpha_linear])
            check(f"recompute:{label}:{name}:alpha_geometric",
                  close_enough(relative["jensen_alpha_geometric"], alpha_geometric),
                  [relative["jensen_alpha_geometric"], alpha_geometric])
            check(f"recompute:{label}:{name}:tracking_error",
                  close_enough(relative["tracking_error"], tracking_error),
                  [relative["tracking_error"], tracking_error])
            check(f"recompute:{label}:{name}:alpha_t_ols",
                  close_enough(reported["alpha_significance"]["t_ols"],
                               alpha_daily / se if se else 0.0),
                  [reported["alpha_significance"]["t_ols"],
                   alpha_daily / se if se else None])

    # The risk-free proxy must be a sane annual rate, not a decimal/percent bug.
    gsec = alpha["evaluations"][PRIMARY]["NIFTY500_SPLICED_NSE"].get("overlap_gsec")
    if gsec:
        annual = Decimal(gsec["rf_detail"]["annual"])
        check("gsec:plausible_rate", Decimal("0.01") <= annual <= Decimal("0.15"),
              str(annual))

    # The flagship CSV must remain a faithful view of the pre-specified six
    # configurations, and the full sweep CSV must contain every run, sorted by
    # Sharpe, with internally reconciling metrics.
    check("flagship_csv_exists", FLAGSHIP_CSV.is_file(), str(FLAGSHIP_CSV))
    check("final_csv_exists", FINAL_CSV.is_file(), str(FINAL_CSV))
    flagship_rows: dict[str, dict] = {}
    if FLAGSHIP_CSV.is_file():
        f_fields, f_rows = read_csv(FLAGSHIP_CSV)
        if "config_id" not in f_fields:
            check("flagship_csv:schema", False,
                  f"missing config_id column: {f_fields[:4]}")
            f_rows = []
        flagship_rows = {row["config_id"]: row for row in f_rows
                         if row.get("config_id")}
        check("flagship_csv:header_unique", len(f_fields) == len(set(f_fields)),
              len(f_fields))
        check("flagship_csv:row_count", len(f_rows) == len(EVIDENCE_BY_CONFIG),
              len(f_rows))
        check("flagship_csv:config_ids", set(flagship_rows) == set(EVIDENCE_BY_CONFIG),
              sorted(flagship_rows))

        for config_id, label in EVIDENCE_BY_CONFIG.items():
            row = flagship_rows.get(config_id)
            if row is None:
                continue
            metrics = configs[label]["metrics"]
            durations = metrics.get("drawdown_durations") or {}
            worst = durations.get("worst") or {}
            entry = alpha["evaluations"][label][B50]["full_rf0"]
            # CSV values are rounded for publication: percent and money
            # columns to 2 dp, four-decimal ratio columns to 4 dp. Compare at
            # half a unit of the published precision, not at full float
            # precision, or every correct rounded row would fail.
            check(f"flagship_csv:{config_id}:final_equity",
                  close_enough(row["final_equity_inr"], metrics["final_equity"],
                               absolute=0.01),
                  [row["final_equity_inr"], metrics["final_equity"]])
            check(f"flagship_csv:{config_id}:cagr",
                  close_enough(row["cagr_pct"], float(metrics["cagr"]) * 100,
                               absolute=0.01),
                  [row["cagr_pct"], metrics["cagr"]])
            check(f"flagship_csv:{config_id}:max_drawdown",
                  close_enough(row["max_drawdown_pct"],
                               float(metrics["max_drawdown"]) * 100,
                               absolute=0.01),
                  [row["max_drawdown_pct"], metrics["max_drawdown"]])
            check(f"flagship_csv:{config_id}:max_dd_total_days",
                  int(row["max_dd_total_days"]) == worst.get("total_days"),
                  [row["max_dd_total_days"], worst.get("total_days")])
            check(f"flagship_csv:{config_id}:max_dd_total_sessions",
                  int(row["max_dd_total_sessions"]) == worst.get("total_sessions"),
                  [row["max_dd_total_sessions"], worst.get("total_sessions")])
            check(f"flagship_csv:{config_id}:pct_sessions_underwater",
                  close_enough(row["pct_sessions_underwater"],
                               float(durations["pct_sessions_underwater"]) * 100,
                               absolute=0.01),
                  [row["pct_sessions_underwater"],
                   durations["pct_sessions_underwater"]])
            scheduled = sum(1 for day in configs[label]["cadence"]["dates"]
                            if day >= "2013-05-31")
            check(f"flagship_csv:{config_id}:scheduled_rebalances",
                  int(row["scheduled_rebalances"]) == scheduled,
                  [row["scheduled_rebalances"], scheduled])
            check(f"flagship_csv:{config_id}:alpha_linear",
                  close_enough(row["alpha_linear_pct"],
                               float(entry["relative"]["jensen_alpha_linear"]) * 100,
                               absolute=0.01),
                  [row["alpha_linear_pct"], entry["relative"]["jensen_alpha_linear"]])
            check(f"flagship_csv:{config_id}:alpha_t_nw5",
                  close_enough(row["t_newey_west_5"],
                               entry["alpha_significance"]["t_newey_west_5"],
                               absolute=0.0001),
                  [row["t_newey_west_5"],
                   entry["alpha_significance"]["t_newey_west_5"]])
            check(f"flagship_csv:{config_id}:total_charges",
                  close_enough(row["total_charges_inr"],
                               configs[label]["charges"]["total_charges"],
                               absolute=0.01),
                  [row["total_charges_inr"],
                   configs[label]["charges"]["total_charges"]])
            check(f"flagship_csv:{config_id}:benchmark",
                  row["benchmark"] == "NIFTY 50 price index (Yahoo ^NSEI)",
                  row["benchmark"])

        primary_row = flagship_rows.get("nifty500_top_N10_L63_monthly_none")
        gross_row = flagship_rows.get(
            "nifty500_top_N10_L63_monthly_none_zero_cost")
        if primary_row and gross_row:
            check("flagship_csv:primary_cost_drag",
                  close_enough(float(gross_row["cagr_pct"])
                               - float(primary_row["cagr_pct"]),
                               (float(configs[GROSS]["metrics"]["cagr"])
                                - float(configs[PRIMARY]["metrics"]["cagr"])) * 100,
                               absolute=0.011),
                  [gross_row["cagr_pct"], primary_row["cagr_pct"],
                   configs[GROSS]["metrics"]["cagr"],
                   configs[PRIMARY]["metrics"]["cagr"]])

    final_fields: list[str] = []
    if FINAL_CSV.is_file():
        final_fields, _ = read_csv(FINAL_CSV)
    if FINAL_CSV.is_file() and "config_id" not in final_fields:
        check("sweep_csv:schema", False,
              f"missing config_id column; run merge_sweep.py: {final_fields[:4]}")
    elif FINAL_CSV.is_file():
        fields, csv_rows = read_csv(FINAL_CSV)
        by_id = {row["config_id"]: row for row in csv_rows}
        check("sweep_csv:header_unique", len(fields) == len(set(fields)),
              len(fields))
        check("sweep_csv:row_count", len(csv_rows) == 968, len(csv_rows))
        check("sweep_csv:unique_configs", len(by_id) == len(csv_rows),
              [len(by_id), len(csv_rows)])
        check("sweep_csv:no_nifty750",
              not any("750" in row["config_id"] or "total_market" in row["config_id"]
                      for row in csv_rows),
              [row["config_id"] for row in csv_rows
               if "750" in row["config_id"] or "total_market" in row["config_id"]])
        check("sweep_csv:family_counts",
              Counter(row["family"] for row in csv_rows) == Counter({
                  "universe_core": 150, "exit_grid_monthly": 576,
                  "bottom_hold_exit": 96, "nifty50_exit_annual": 144,
                  "counterfactual_zero_cost": 1, "superseded_diagnostic": 1}),
              dict(Counter(row["family"] for row in csv_rows)))
        check("sweep_csv:universe_counts",
              Counter(row["universe"] for row in csv_rows) == Counter({
                  "nifty500": 278, "msc400": 180, "midcap150": 180,
                  "smallcap250": 180, "nifty50": 150}),
              dict(Counter(row["universe"] for row in csv_rows)))
        check("sweep_csv:cadences",
              {row["cadence"] for row in csv_rows} ==
              {"monthly", "quarterly", "semiannual", "annual", "2y", "3y"},
              sorted({row["cadence"] for row in csv_rows}))
        check("sweep_csv:directions",
              {row["direction"] for row in csv_rows} == {"top", "bottom"},
              sorted({row["direction"] for row in csv_rows}))
        check("sweep_csv:top_n", {row["top_n"] for row in csv_rows} ==
              {"10", "20", "25"}, sorted({row["top_n"] for row in csv_rows}))
        check("sweep_csv:flagship_rows",
              sum(1 for row in csv_rows if row["is_flagship"] == "true") == 6,
              sum(1 for row in csv_rows if row["is_flagship"] == "true"))
        check("sweep_csv:superseded_rows",
              sum(1 for row in csv_rows if row["flag_superseded"] == "true") == 1,
              sum(1 for row in csv_rows if row["flag_superseded"] == "true"))
        check("sweep_csv:zero_cost_rows",
              sum(1 for row in csv_rows if row["costs"] == "zero") == 1,
              sum(1 for row in csv_rows if row["costs"] == "zero"))
        alpha_errors = [row["config_id"] for row in csv_rows
                        if row["alpha_error"]]
        check("sweep_csv:alpha_available", not alpha_errors, alpha_errors[:10])

        expected_overlays = {"none"} | {
            f"sl{tag}" for tag in ("05", "10", "15", "20")} | {
            f"tp{tag}" for tag in ("05", "10", "15", "20")} | {
            f"sl{s}_tp{t}" for s in ("05", "10", "15", "20")
            for t in ("05", "10", "15", "20")}
        monthly_rows = [row for row in csv_rows
                        if row["cadence"] == "monthly"
                        and row["family"] in ("universe_core", "exit_grid_monthly")
                        and row["universe"] in
                        ("nifty500", "msc400", "midcap150", "smallcap250")]
        check("sweep_csv:monthly_exit_grid_rows", len(monthly_rows) == 600,
              len(monthly_rows))
        check("sweep_csv:monthly_overlays_complete",
              {row["exit_overlay"] for row in monthly_rows} == expected_overlays,
              sorted({row["exit_overlay"] for row in monthly_rows}))
        hold_rows = [row for row in csv_rows
                     if row["universe"] == "nifty500"
                     and row["direction"] == "bottom"
                     and row["top_n"] in ("20", "25")
                     and row["cadence"] in ("2y", "3y")]
        check("sweep_csv:bottom_hold_exit_rows", len(hold_rows) == 100,
              len(hold_rows))
        check("sweep_csv:bottom_hold_overlays_complete",
              {row["exit_overlay"] for row in hold_rows} == expected_overlays,
              sorted({row["exit_overlay"] for row in hold_rows}))
        bottom_long_core = [
            row for row in csv_rows
            if row["direction"] == "bottom"
            and row["top_n"] in ("20", "25")
            and row["cadence"] in ("2y", "3y")
            and row["exit_overlay"] == "none"]
        check("sweep_csv:bottom_long_holds_all_monthly_universes",
              Counter(row["universe"] for row in bottom_long_core) == Counter({
                  "nifty500": 4, "msc400": 4, "midcap150": 4,
                  "smallcap250": 4}),
              dict(Counter(row["universe"] for row in bottom_long_core)))
        check("sweep_csv:nifty50_rows",
              [row for row in csv_rows if row["universe"] == "nifty50"] and
              all(row["cadence"] == "annual" for row in csv_rows
                  if row["universe"] == "nifty50") and
              len([row for row in csv_rows if row["universe"] == "nifty50"]) == 150,
              len([row for row in csv_rows if row["universe"] == "nifty50"]))

        ranked = [row for row in csv_rows if row["rank_by_sharpe"]]
        check("sweep_csv:rank_contiguous",
              [int(row["rank_by_sharpe"]) for row in ranked] ==
              list(range(1, len(ranked) + 1)),
              [len(ranked), ranked[0]["rank_by_sharpe"] if ranked else None,
               ranked[-1]["rank_by_sharpe"] if ranked else None])
        sharpes = [float(row["sharpe_rf0"]) for row in ranked]
        check("sweep_csv:sorted_by_sharpe_desc",
              all(a >= b for a, b in zip(sharpes, sharpes[1:])),
              [sharpes[:3], sharpes[-3:]])
        check("sweep_csv:unranked_rows_last",
              all(not row["rank_by_sharpe"] for row in csv_rows[len(ranked):]),
              len(csv_rows) - len(ranked))

        failures: dict[str, list] = {
            "publishable": [], "total_return": [], "cagr": [], "window_years": [],
            "calendar_days": [], "pct_underwater": [], "drawdown_bounds": [],
            "drawdown_sessions": [], "drawdown_days": [], "charges_total": [],
            "charges_components": [], "turnover": [], "charges_bps": [],
            "alpha_geometric": [], "flags": [], "basis": [], "fills": [],
            "horizon": [],
        }
        for row in csv_rows:
            config_id = row["config_id"]
            if row["publishable"] != "true":
                failures["publishable"].append(config_id)
                continue
            initial = float(row["initial_equity_inr"])
            final = float(row["final_equity_inr"])
            total_pct = float(row["total_return_pct"])
            cagr_pct = float(row["cagr_pct"])
            sessions = int(row["sessions"])
            growth = final / initial
            if abs(total_pct - (growth - 1.0) * 100) > 0.01:
                failures["total_return"].append(config_id)
            if abs(cagr_pct - (growth ** (252 / sessions) - 1.0) * 100) > 0.02:
                failures["cagr"].append(config_id)
            if abs(float(row["window_years"]) - sessions / 252) > 0.01:
                failures["window_years"].append(config_id)
            span = (date.fromisoformat(row["performance_end"])
                    - date.fromisoformat(row["performance_start"])).days
            if int(row["calendar_days"]) != span:
                failures["calendar_days"].append(config_id)
            if abs(float(row["pct_sessions_underwater"]) -
                   int(row["sessions_underwater"]) / sessions * 100) > 0.01:
                failures["pct_underwater"].append(config_id)
            if not (-100.0 <= float(row["max_drawdown_pct"]) <= 0.0):
                failures["drawdown_bounds"].append(config_id)
            if not row["max_dd_peak_date"]:
                if int(row["drawdown_episodes"]) != 0:
                    failures["drawdown_bounds"].append(config_id)
            else:
                peak = date.fromisoformat(row["max_dd_peak_date"])
                trough = date.fromisoformat(row["max_dd_trough_date"])
                recovery = row["max_dd_recovery_date"]
                if (trough - peak).days != int(row["max_dd_peak_to_trough_days"]):
                    failures["drawdown_days"].append(config_id)
                if row["max_dd_recovered"] == "true":
                    recovered_on = date.fromisoformat(recovery)
                    if ((recovered_on - trough).days
                            != int(row["max_dd_trough_to_recovery_days"])):
                        failures["drawdown_days"].append(config_id)
                    if (int(row["max_dd_peak_to_trough_sessions"])
                            + int(row["max_dd_trough_to_recovery_sessions"])
                            != int(row["max_dd_total_sessions"])):
                        failures["drawdown_sessions"].append(config_id)
                    if (int(row["max_dd_peak_to_trough_days"])
                            + int(row["max_dd_trough_to_recovery_days"])
                            != int(row["max_dd_total_days"])):
                        failures["drawdown_days"].append(config_id)
                elif recovery:
                    failures["drawdown_days"].append(config_id)
            buy = float(row["buy_charges_inr"])
            sell = float(row["sell_charges_inr"])
            total = float(row["total_charges_inr"])
            if abs(total - (buy + sell)) > 0.05:
                failures["charges_total"].append(config_id)
            expected_charge_pct = total / initial * 100
            if (abs(float(row["charges_pct_of_initial_cash"])
                    - expected_charge_pct) > 0.01):
                failures["charges_total"].append(config_id)
            components = sum(float(row[f"{name}_inr"]) for name in
                             ("brokerage", "gst", "stt", "stamp", "exchange",
                              "slippage"))
            if abs(total - components) > 0.05:
                failures["charges_components"].append(config_id)
            turnover = float(row["turnover_inr"])
            if abs(turnover - (float(row["buy_notional_inr"])
                               + float(row["sell_notional_inr"]))) > 0.05:
                failures["turnover"].append(config_id)
            if turnover and abs(float(row["charges_bps_of_turnover"])
                                - total / turnover * 10_000) > 0.02:
                failures["charges_bps"].append(config_id)
            if row["alpha_error"] == "" and row["alpha_linear_pct"]:
                linear = float(row["alpha_linear_pct"]) / 100
                geometric = (1 + linear / 252) ** 252 - 1
                if abs(geometric * 100 - float(row["alpha_geometric_pct"])) > 0.02:
                    failures["alpha_geometric"].append(config_id)
            if (row["flag_few_rebalances"] !=
                    str(int(row["scheduled_rebalances"]) < 6).lower()):
                failures["flags"].append(config_id)
            if (row["flag_short_window"] !=
                    str(float(row["window_years"]) < 6).lower()):
                failures["flags"].append(config_id)
            cash_flag = (row["exit_overlay"] != "none"
                         and row["cadence"] in ("annual", "2y", "3y"))
            if (row["flag_exit_cash_until_rebalance"]
                    != str(cash_flag).lower()):
                failures["flags"].append(config_id)
            if row["benchmark"] != "NIFTY 50 price index (Yahoo ^NSEI)":
                failures["basis"].append(config_id)
            if "research_preview" not in row["gate_status"]:
                failures["basis"].append(config_id)
            if not row["price_basis"].startswith("split/bonus adjusted"):
                failures["basis"].append(config_id)
            if row["first_fill"] and row["last_fill"] and (
                    row["first_fill"] < row["performance_start"]
                    or row["last_fill"] > row["performance_end"]):
                failures["fills"].append(config_id)
            if row["scheduled_horizon_end"]:
                horizon = date.fromisoformat(row["scheduled_horizon_end"])
                end = date.fromisoformat(row["performance_end"])
                if horizon > date(2026, 9, 30) or end > horizon:
                    failures["horizon"].append(config_id)
                elif (horizon - end).days > 10:
                    failures["horizon"].append(config_id)
                if (row["last_signal_date"]
                        and date.fromisoformat(row["last_signal_date"]) > end):
                    failures["horizon"].append(config_id)
            else:
                failures["horizon"].append(config_id)

        for name, failed_rows in failures.items():
            check(f"sweep_csv:all_rows:{name}", not failed_rows,
                  failed_rows[:10])

        primary_row = by_id.get("nifty500_top_N10_L63_monthly_none")
        if primary_row is not None:
            metrics = configs[PRIMARY]["metrics"]
            entry = alpha["evaluations"][PRIMARY][B50]["full_rf0"]
            check("sweep_csv:primary_total_return",
                  close_enough(primary_row["total_return_pct"],
                               float(metrics["total_return"]) * 100,
                               absolute=0.01),
                  [primary_row["total_return_pct"], metrics["total_return"]])
            check("sweep_csv:primary_max_dd_duration",
                  int(primary_row["max_dd_total_days"]) ==
                  metrics["drawdown_durations"]["worst"]["total_days"],
                  [primary_row["max_dd_total_days"],
                   metrics["drawdown_durations"]["worst"]["total_days"]])
            check("sweep_csv:primary_alpha",
                  close_enough(primary_row["alpha_linear_pct"],
                               float(entry["relative"]["jensen_alpha_linear"]) * 100,
                               absolute=0.01),
                  [primary_row["alpha_linear_pct"],
                   entry["relative"]["jensen_alpha_linear"]])
            check("sweep_csv:primary_evidence_label",
                  primary_row["evidence_label"] == PRIMARY,
                  primary_row["evidence_label"])

    failed = [row for row in CHECKS if not row["ok"]]
    payload = {
        "generated_at_utc": strategy["generated_at_utc"],
        "passed": not failed,
        "checks_total": len(CHECKS),
        "checks_failed": len(failed),
        "checks": CHECKS,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    for row in failed:
        print(f"FAIL {row['name']}: {row['detail']}")
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed -> {OUT}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
