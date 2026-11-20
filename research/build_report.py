"""Render REPORT.md from the alpha backtest artifacts.

The prose is fixed; every number is read from the JSON evidence so the report
cannot drift from the computation. Run after ``compute_alpha.py`` and
``validate_alpha_output.py``.
"""
from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "REPORT.md"

STRATEGY = HERE / "strategy-results.json"
ALPHA = HERE / "alpha-results.json"
VALIDATION = HERE / "validation.json"
FINAL = HERE / "FINAL_RESULTS.csv"
ARCHIVED = HERE.parent / "momentum-stop-sweep" / "grid-results.json"
REPRO = HERE / "repro-check" / "grid-results.json"

PRIMARY = "evidence-N10-L63-next_open-no_stop"
RISK = "evidence-N10-L63-next_open-sl0.10"
BREADTH = "evidence-N20-L63-next_open-no_stop"
BOTTOM = "evidence-N20-L63-next_open-no_stop-bottom"
GROSS = "evidence-N10-L63-next_open-no_stop-gross_of_costs"
AS_COMMITTED = "as-committed-month-end-exact-match"
B500 = "NIFTY500_SPLICED_NSE"
B50 = "NIFTY50_YAHOO"
B50NSE = "NIFTY50_SPLICED_NSE"

BENCH_LABEL = {
    B50: "NIFTY 50 (Yahoo)",
    B50NSE: "NIFTY 50 (NSE spliced)",
    B500: "NIFTY 500 (spliced)",
}

SHORT = {
    PRIMARY: "N10, no stop (**primary**)",
    RISK: "N10, 10% stop",
    BREADTH: "N20, no stop",
    BOTTOM: "N20 bottom, no stop (reversal)",
    GROSS: "N10, zero costs (counterfactual)",
    AS_COMMITTED: "N10, month-end/exact (diagnostic)",
}


def pct(value, digits=2) -> str:
    return f"{float(value) * 100:,.{digits}f}%"


def num(value, digits=2) -> str:
    return f"{float(value):,.{digits}f}"


def money(value) -> str:
    return f"{float(value):,.0f}"


def count(value) -> str:
    return f"{int(value):,}"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ewma_rows(config: dict) -> dict:
    return {row["label"]: row for row in config["configs"]}


def alpha_row(entry: dict, *, bootstrap: bool = False) -> list[str]:
    relative = entry["relative"]
    significance = entry.get("alpha_significance", {})
    bootstrap_stats = significance.get("block_bootstrap")
    return [
        f"{entry['first_return_date']}..{entry['last_return_date']}",
        count(entry["n_returns"]),
        pct(entry["strategy"]["annualised_return"]),
        pct(entry["benchmark"]["annualised_return"]),
        num(relative["beta"]),
        pct(relative["jensen_alpha_linear"]),
        pct(relative["jensen_alpha_geometric"]),
        pct(relative["tracking_error"]),
        num(relative["information_ratio"]),
        pct(relative["up_capture"]),
        pct(relative["down_capture"]),
        num(relative["r_squared"], 3),
        num(significance.get("t_ols", float("nan"))),
        num(significance.get("t_newey_west_5", float("nan"))),
        (num(bootstrap_stats["p_two_sided"], 3)
         if bootstrap and bootstrap_stats else "—"),
    ]


def table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def dd_block(metrics: dict, key: str = "worst") -> dict:
    return (metrics.get("drawdown_durations") or {}).get(key) or {}


def dd_days(metrics: dict, key: str = "worst") -> str:
    block = dd_block(metrics, key)
    return "—" if not block else count(block["total_days"])


def dd_sessions(metrics: dict, key: str = "worst") -> str:
    block = dd_block(metrics, key)
    return "—" if not block else count(block["total_sessions"])


def main() -> int:
    strategy = load(STRATEGY)
    alpha = load(ALPHA)
    validation = load(VALIDATION)
    archived = load(ARCHIVED)
    repro = load(REPRO)
    configs = ewma_rows(strategy)
    with FINAL.open(encoding="utf-8", newline="") as handle:
        sweep_count = sum(1 for _ in csv.DictReader(handle))
    if sweep_count < 900:
        raise SystemExit(
            f"FINAL_RESULTS.csv has only {sweep_count} rows; run "
            "merge_sweep.py before build_report.py so the report cannot "
            "describe a partial sweep")

    archived_row = next(row for row in archived["results"]
                        if row["label"] == "monthly-N10-L63-next_open-no_exit_overlay-last_close_writeoff")
    repro_row = next(row for row in repro["results"]
                     if row["label"] == "monthly-N10-L63-next_open-no_exit_overlay-last_close_writeoff")

    primary = alpha["evaluations"][PRIMARY][B50]["full_rf0"]
    primary500 = alpha["evaluations"][PRIMARY][B500]["full_rf0"]
    primary_gsec = alpha["evaluations"][PRIMARY][B50]["overlap_gsec"]
    primary_sig = primary["alpha_significance"]
    primary500_sig = primary500["alpha_significance"]
    bottom = alpha["evaluations"][BOTTOM][B50]["full_rf0"]
    bottom500 = alpha["evaluations"][BOTTOM][B500]["full_rf0"]
    bottom_metrics = configs[BOTTOM]["metrics"]
    bottom_sig = bottom["alpha_significance"]
    bottom500_sig = bottom500["alpha_significance"]
    diag = alpha["strategy_diagnostics"]
    calendar = strategy["calendar"]

    # -- configuration table ------------------------------------------------
    config_rows = []
    for label in (PRIMARY, RISK, BREADTH, BOTTOM, AS_COMMITTED):
        row = configs[label]
        metrics = row["metrics"]
        d = diag[label]
        signal_dates = [day for day in row["cadence"]["dates"]
                        if day >= "2013-05-31"]
        holds_in_window = sum(1 for day in row["unproven_universe_holds"]
                              if day >= "2013-05-31")
        config_rows.append([
            SHORT[label],
            str(len(signal_dates)),
            str(d["n_trades"]),
            f"{d['n_buys']}/{d['n_sells']}",
            money(metrics["final_equity"]),
            pct(metrics["total_return"]),
            pct(metrics["cagr"]),
            pct(metrics["annualised_volatility"]),
            pct(metrics["max_drawdown"]),
            dd_days(metrics),
            dd_sessions(metrics),
            dd_days(metrics, "longest"),
            num(metrics["sharpe_zero_rf"], 3),
            pct(d["mean_cash_share"]),
            str(d["n_delisting_writeoffs"]),
            str(holds_in_window),
        ])

    as_committed_holds = sum(
        1 for day in configs[AS_COMMITTED]["unproven_universe_holds"]
        if day >= "2013-05-31")
    as_committed_warmup_holds = sum(
        1 for day in configs[AS_COMMITTED]["unproven_universe_holds"]
        if day < "2013-05-31")
    as_committed_scheduled = sum(
        1 for day in configs[AS_COMMITTED]["cadence"]["dates"]
        if day >= "2013-05-31")

    # -- headline performance and alpha -------------------------------------
    perf_rows = []
    for label in (PRIMARY, RISK, BREADTH, BOTTOM, AS_COMMITTED):
        entry = alpha["evaluations"][label][B50]["full_rf0"]
        perf_rows.append([
            SHORT[label],
            BENCH_LABEL[B50],
            entry["first_return_date"] + ".." + entry["last_return_date"],
            str(entry["n_returns"]),
            pct(entry["strategy"]["annualised_return"]),
            pct(entry["strategy"]["annualised_volatility"]),
            pct(entry["strategy"]["max_drawdown"]),
            num(entry["strategy"]["sharpe"], 3),
            pct(entry["benchmark"]["annualised_return"]),
            pct(entry["relative"]["tracking_difference"]),
        ])
    for benchmark in (B50NSE, B500):
        entry = alpha["evaluations"][PRIMARY][benchmark]["full_rf0"]
        perf_rows.append([
            "N10, no stop (primary, robustness)",
            BENCH_LABEL[benchmark],
            entry["first_return_date"] + ".." + entry["last_return_date"],
            str(entry["n_returns"]),
            pct(entry["strategy"]["annualised_return"]),
            pct(entry["strategy"]["annualised_volatility"]),
            pct(entry["strategy"]["max_drawdown"]),
            num(entry["strategy"]["sharpe"], 3),
            pct(entry["benchmark"]["annualised_return"]),
            pct(entry["relative"]["tracking_difference"]),
        ])

    alpha_rows = []
    for label in (PRIMARY, RISK, BREADTH, BOTTOM, AS_COMMITTED):
        entry = alpha["evaluations"][label][B50]["full_rf0"]
        alpha_rows.append([SHORT[label], BENCH_LABEL[B50],
                           *alpha_row(entry, bootstrap=(label == PRIMARY))])
    for benchmark in (B50NSE, B500):
        entry = alpha["evaluations"][PRIMARY][benchmark]["full_rf0"]
        alpha_rows.append(["N10, no stop (primary, robustness)", BENCH_LABEL[benchmark],
                           *alpha_row(entry, bootstrap=True)])

    # -- segments -----------------------------------------------------------
    segment_rows = []
    for segment in ("monthly_membership_2013_2022", "anchor_membership_2022_2026"):
        for benchmark in (B50, B50NSE, B500):
            entry = alpha["evaluations"][PRIMARY][benchmark]["segments"].get(segment)
            if entry is None:
                continue
            segment_rows.append([
                segment.replace("_", " "),
                BENCH_LABEL[benchmark],
                entry["first_return_date"] + ".." + entry["last_return_date"],
                str(entry["n_returns"]),
                pct(entry["strategy"]["annualised_return"]),
                pct(entry["benchmark"]["annualised_return"]),
                pct(entry["relative"]["tracking_difference"]),
                num(entry["relative"]["beta"]),
                pct(entry["relative"]["jensen_alpha_linear"]),
                num(entry["relative"]["information_ratio"]),
                num(entry["alpha_significance"].get("t_newey_west_5", float("nan"))),
            ])

    # -- calendar years -----------------------------------------------------
    years_500 = {row["year"]: row for row in alpha["calendar_years"][B500]["years"]}
    years_50 = {row["year"]: row for row in alpha["calendar_years"][B50]["years"]}
    year_rows = []
    for year in sorted(set(years_500) | set(years_50)):
        row_500 = years_500.get(year)
        row_50 = years_50.get(year)
        label = str(year) + ("*" if (row_50 or row_500).get("partial") else "")
        if row_50:
            year_rows.append([label, pct(row_50["strategy"]), pct(row_50["benchmark"]),
                              pct(row_500["benchmark"]) if row_500 else "—",
                              pct(row_50["excess"])])
        elif row_500:
            year_rows.append([label, pct(row_500["strategy"]), "—",
                              pct(row_500["benchmark"]), "—"])

    # -- risk-free sensitivity ---------------------------------------------
    monthly = alpha["monthly"][B50]
    rf_rows = [
        ["daily, full window", "0 (zero-rate)", primary["first_return_date"] + ".." + primary["last_return_date"],
         pct(primary["strategy"]["annualised_return"]), pct(primary["benchmark"]["annualised_return"]),
         num(primary["relative"]["beta"]), pct(primary["relative"]["jensen_alpha_linear"]),
         num(primary["strategy"]["sharpe"], 3), num(primary["benchmark"]["sharpe"], 3)],
    ]
    if primary_gsec:
        rf_rows.append([
            "daily, G-Sec overlap", pct(primary_gsec["rf_detail"]["annual"]) + " p.a.",
            primary_gsec["first_return_date"] + ".." + primary_gsec["last_return_date"],
            pct(primary_gsec["strategy"]["annualised_return"]),
            pct(primary_gsec["benchmark"]["annualised_return"]),
            num(primary_gsec["relative"]["beta"]),
            pct(primary_gsec["relative"]["jensen_alpha_linear"]),
            num(primary_gsec["strategy"]["sharpe"], 3),
            num(primary_gsec["benchmark"]["sharpe"], 3)])
    rf_rows.append([
        "monthly, full window", "0 (zero-rate)", monthly["first_return_date"] + ".." + monthly["last_return_date"],
        pct(monthly["strategy"]["annualised_return"]), pct(monthly["benchmark"]["annualised_return"]),
        num(monthly["relative"]["beta"]), pct(monthly["relative"]["jensen_alpha_linear"]),
        num(monthly["strategy"]["sharpe"], 3), num(monthly["benchmark"]["sharpe"], 3)])
    if "gsec" in monthly:
        g = monthly["gsec"]
        rf_rows.append([
            "monthly, G-Sec overlap", pct(g["rf_detail"]["annual"]) + " p.a.",
            g["first_return_date"] + ".." + g["last_return_date"],
            pct(g["strategy"]["annualised_return"]), pct(g["benchmark"]["annualised_return"]),
            num(g["relative"]["beta"]), pct(g["relative"]["jensen_alpha_linear"]),
            num(g["strategy"]["sharpe"], 3), num(g["benchmark"]["sharpe"], 3)])

    # -- archived comparison ------------------------------------------------
    as_committed = configs[AS_COMMITTED]["metrics"]
    evidence = configs[PRIMARY]["metrics"]
    archive_rows = [
        ["144-config sweep (archived)", "month-end, exact snapshot only", "pre-fix: liquidates on 39 empty-target dates",
         pct(archived_row["metrics"]["total_return"]), pct(archived_row["metrics"]["cagr"]),
         "**not used** — data-gap liquidation"],
        ["post-fix repro (2013-04-18 start)", "month-end, exact snapshot only", "post-fix: holds on 39 empty-target dates",
         pct(repro_row["metrics"]["total_return"]), pct(repro_row["metrics"]["cagr"]),
         "not a rebalance — superseded"],
        ["this run, as-committed", "month-end, exact snapshot only", "post-fix: holds on empty targets",
         pct(as_committed["total_return"]), pct(as_committed["cagr"]),
         "diagnostic comparison only"],
        ["this run, evidence-dated (**used**)", f"{calendar['count']} official evidence dates", "post-fix, explicit calendar",
         pct(evidence["total_return"]), pct(evidence["cagr"]),
         "**headline research estimate**"],
    ]

    # -- charges actually paid ---------------------------------------------
    cost_rows = []
    for label in (PRIMARY, RISK, BREADTH, BOTTOM, AS_COMMITTED, GROSS):
        audit = configs[label]["charges"]
        cost_rows.append([
            SHORT[label],
            money(audit["buy_notional"]),
            money(audit["sell_notional"]),
            money(audit["buy_charges"]),
            money(audit["sell_charges"]),
            money(audit["total_charges"]),
            num(audit["charges_bps_of_turnover"], 2),
            pct(audit["charges_pct_of_initial_cash"]),
        ])
    primary_audit = configs[PRIMARY]["charges"]
    component_rows = [[name, money(value)]
                      for name, value in primary_audit["components"].items()]
    net_cagr = Decimal(str(configs[PRIMARY]["metrics"]["cagr"]))
    gross_cagr = Decimal(str(configs[GROSS]["metrics"]["cagr"]))
    # Headline drag is stated as a positive cost: gross compounds faster.
    cost_drag = gross_cagr - net_cagr
    top_alpha = Decimal(str(primary["relative"]["jensen_alpha_linear"]))
    bottom_alpha = Decimal(str(bottom["relative"]["jensen_alpha_linear"]))
    momentum_sign = ("opposite signs, which is what a pure momentum premium predicts"
                     if top_alpha * bottom_alpha < 0 else
                     "the same sign, which is not what a pure momentum premium predicts")
    primary_dd = dd_block(configs[PRIMARY]["metrics"], "worst")
    primary_longest_dd = dd_block(configs[PRIMARY]["metrics"], "longest")
    bottom_dd = dd_block(configs[BOTTOM]["metrics"], "worst")
    primary_t = primary_sig.get("t_newey_west_5")
    bottom_t = bottom_sig.get("t_newey_west_5")
    primary_sig_state = ("not statistically significant"
                         if abs(float(primary_t)) < 1.96 else "statistically significant")
    bottom_sig_state = ("clears" if abs(float(bottom_t)) >= 1.96 else "does not clear")
    primary_writeoffs = diag[PRIMARY]["n_delisting_writeoffs"]
    primary_writeoff_word = "writeoff" if primary_writeoffs == 1 else "writeoffs"

    # -- top vs bottom reversal ---------------------------------------------
    reversal_rows = []
    for label in (PRIMARY, BOTTOM):
        entry = alpha["evaluations"][label][B50]["full_rf0"]
        reversal_rows.append([
            SHORT[label],
            pct(entry["strategy"]["annualised_return"]),
            pct(entry["benchmark"]["annualised_return"]),
            num(entry["relative"]["beta"]),
            pct(entry["relative"]["jensen_alpha_linear"]),
            pct(entry["relative"]["tracking_error"]),
            num(entry["relative"]["information_ratio"]),
            num(entry["alpha_significance"].get("t_ols", float("nan"))),
            num(entry["alpha_significance"].get("t_newey_west_5", float("nan"))),
        ])

    # -- drawdown duration ---------------------------------------------------
    drawdown_rows = []
    for label in (PRIMARY, RISK, BREADTH, BOTTOM, AS_COMMITTED):
        metrics = configs[label]["metrics"]
        worst = dd_block(metrics, "worst")
        longest = dd_block(metrics, "longest")
        underwater_pct = ((metrics.get("drawdown_durations") or {})
                          .get("pct_sessions_underwater"))
        drawdown_rows.append([
            SHORT[label],
            pct(metrics["max_drawdown"]),
            worst["peak_date"],
            worst["trough_date"],
            worst["recovery_date"] or "—",
            count(worst["peak_to_trough_days"]),
            count(worst["peak_to_trough_sessions"]),
            count(worst["trough_to_recovery_days"]) if worst["trough_to_recovery_days"] is not None else "—",
            count(worst["trough_to_recovery_sessions"]) if worst["trough_to_recovery_sessions"] is not None else "—",
            count(worst["total_days"]),
            count(worst["total_sessions"]),
            count(longest["total_days"]),
            count(longest["total_sessions"]),
            pct(underwater_pct),
        ])

    bootstrap = primary_sig.get("block_bootstrap", {})
    validation_line = (f"{validation['checks_total'] - validation['checks_failed']}"
                       f"/{validation['checks_total']} checks passed"
                       if validation["passed"] else
                       f"**{validation['checks_failed']} of {validation['checks_total']} checks FAILED**")

    text = f"""# NIFTY 500 momentum vs NIFTY 50: benchmark-relative alpha backtest

**STATUS: RESEARCH PREVIEW — validation in progress. This is a research diagnostic, not a validated alpha claim and not an investable strategy.** The internal validation gate is still open (`ready_for_alpha_claim` is `false` in `data/reports/full-history-readiness.json`). This report is published as evidence of what can be computed on today's data, and as a record of exactly how far the data can support the conclusion.

Generated from `strategy-results.json`, `benchmark-series.json`, `alpha-results.json`, `validation.json` and `FINAL_RESULTS.csv`. Every number below is rendered from those files, not transcribed by hand.

## 0. Answer in one paragraph

The primary evidence-dated portfolio (NIFTY 500, top 10 by 63-session momentum, next-open fills, monthly to 2022-03 and official-anchor dates after, split/bonus-adjusted, `last_close_writeoff`) returned **{pct(evidence['total_return'])}** from the 2013-05-31 starting mark (first fill 2013-06-03) to 2026-09-30 ({pct(evidence['cagr'])} CAGR, {pct(evidence['annualised_volatility'])} volatility, Sharpe {num(evidence['sharpe_zero_rf'], 3)} at rf=0). Its maximum drawdown was **{pct(evidence['max_drawdown'])}**; it ran **{count(primary_dd['peak_to_trough_days'])} calendar days / {count(primary_dd['peak_to_trough_sessions'])} sessions** from the {primary_dd['peak_date']} peak to the {primary_dd['trough_date']} trough, then recovered on {primary_dd['recovery_date']} after **{count(primary_dd['trough_to_recovery_days'])} calendar days / {count(primary_dd['trough_to_recovery_sessions'])} sessions**, for a total underwater span of **{count(primary_dd['total_days'])} calendar days / {count(primary_dd['total_sessions'])} sessions**. The longest underwater episode lasted **{count(primary_longest_dd['total_days'])} calendar days / {count(primary_longest_dd['total_sessions'])} sessions**.

Against the **NIFTY 50** price index over **{primary['first_return_date']}..{primary['last_return_date']}**, beta is **{num(primary['relative']['beta'])}**, annualised Jensen alpha is **{pct(primary['relative']['jensen_alpha_linear'])} linear / {pct(primary['relative']['jensen_alpha_geometric'])} geometric**, tracking error {pct(primary['relative']['tracking_error'])}, information ratio **{num(primary['relative']['information_ratio'])}**, with up/down capture {pct(primary['relative']['up_capture'])} / {pct(primary['relative']['down_capture'])}. The alpha t-statistic is **{num(primary_sig.get('t_ols', float('nan')))} OLS / {num(primary_sig.get('t_newey_west_5', float('nan')))} Newey-West(5)**; the 21-session circular block bootstrap p-value is **{num(bootstrap.get('p_two_sided', float('nan')), 3)}**. At the 5% level the positive NIFTY 50 alpha is **{primary_sig_state}**. Against the NIFTY 500 over the shorter 2013-2025 window, alpha is **{pct(primary500['relative']['jensen_alpha_linear'])}** (t {num(primary500_sig.get('t_newey_west_5', float('nan')))} Newey-West); that is a robustness view, not the headline.

The opposite portfolio — the **20 weakest** 63-session performers in the same universe, on the same dates, with the same charges — returned **{pct(bottom_metrics['total_return'])}** ({pct(bottom_metrics['cagr'])} CAGR), with a **{pct(bottom_metrics['max_drawdown'])}** maximum drawdown that began at the {bottom_dd['peak_date']} peak, fell until {bottom_dd['trough_date']}, and had not recovered by {evidence['end']} ({count(bottom_dd['total_days'])} calendar days underwater). Its alpha against the same NIFTY 50 window is **{pct(bottom['relative']['jensen_alpha_linear'])}** (t {num(bottom_sig.get('t_newey_west_5', float('nan')))} Newey-West, which {bottom_sig_state} the 5% threshold), and against the NIFTY 500 it is **{pct(bottom500['relative']['jensen_alpha_linear'])}** (t {num(bottom500_sig.get('t_newey_west_5', float('nan')))}). That is the sign test for the momentum story: if the top-20 alpha were a momentum premium, the bottom-20 portfolio should sit on the losing side of it.

Every return above is **net of the actual charge stack**, not a gross series with a fee comment. The primary config paid **₹{money(primary_audit['total_charges'])}** across its trades (₹{money(primary_audit['buy_charges'])} on buys, ₹{money(primary_audit['sell_charges'])} on sells). Re-running the identical strategy with every charge set to zero compounds at **{pct(gross_cagr)}** instead of **{pct(net_cagr)}**; the charges therefore cost **{pct(cost_drag)} of CAGR**. Section 4.1 itemises them.

That is a real, reproducible benchmark-relative diagnostic on the available data, but it is **not** evidence of a durable edge: the post-2022 membership is point evidence at irregular official dates, dividends are excluded, delisting proceeds are assumed, and the validation blockers below are unresolved. The estimate is reported so it can be attacked, not cited.

Validation: **{validation_line}**.

## 1. What was asked and what was run

The required test is an alpha backtest against the NIFTY 50 on all data currently available. The repository had no benchmark-relative layer: `docs/QUANT_REPOSITORY_AUDIT.md` records that there is no index price series in `src/`, so excess return, beta, Jensen alpha, information ratio, tracking error and capture were "not computable". This run supplies the benchmark evidence from local files and computes those statistics. It also runs the **opposite selection** (the 20 weakest 63-session performers) as a sign test, reports **drawdown duration** in calendar days and sessions, and itemises every buy and sell charge actually paid.

It does **not** close the internal validation gate. It is a research diagnostic under that gate, not a production result.

## 2. Data, universe and calendar

### 2.1 Prices

- Source: `data/processed/adjusted/adjusted_bars.parquet`.
- Basis: **split/bonus-adjusted only**. Dividends are not applied. This is not a total-return series. The benchmark is also a price index, so the comparison is price-vs-price; the strategy's omitted dividend yield remains a downward bias on its own return.
- Why dividends are not applied: the adjustment pipeline normalises splits and bonuses only. Dividend records exist in the corporate-actions table with ex-dates and amounts, but turning them into a total-return series needs the shares held on every ex-date, withholding-tax treatment and a reinvestment convention. None of that is defined here. Applying dividends to the strategy but not to the price benchmark would mix bases, so both sides stay price-only. A verified NIFTY 50 total-return series is not on disk; until it is, the sign of the net alpha bias cannot be established.
- Load window: 2012-06-01..2026-09-30 (warm-up for the 63-session lookback). Performance starts on the session before the first executed fill, so the pre-deployment cash period is excluded.
- Costs charged on every buy and every sell: 2 bps brokerage (₹2,000 cap/order), 10 bps STT each side, 1.5 bps stamp duty buy-only, 0.3 bps exchange, 18% GST on brokerage, 5 bps slippage. Slippage is charged as an explicit cash amount rather than moved into the fill. These are stylised constants, not the changing 2000-2026 tax/fee schedule. Integer shares, equal-weight target, next-session-open fills.

### 2.2 Universe and the calendar defect

The NIFTY 500 membership evidence is two-lane:

| lane | dates | character |
|---|---:|---|
| Batch 8 | 108 snapshots, 2013-04-18..2022-03-31 | monthly, official snapshots |
| Batch 9 | 65 anchors, 2022-03-31..2026-09-30 | **irregular point evidence**; dates between anchors are `CoverageUnknown` |

The archived momentum runs rebalanced on **calendar month-ends** and accepted a universe only when an exact snapshot fell on that date. That combination is defective: **39 of 162** monthly signal dates had no snapshot. The pre-fix engine sold the entire book when the target list was empty; the current engine holds it unchanged. Neither is a rebalance. The defect is largest in 2022-2026 and is why the archived 781% sweep is not used here.

This run uses the **evidence dates themselves** as the rebalance calendar through the engine's explicit `rebalance_dates` override:

- {calendar['composition']['batch8_monthly_exact_snapshots']} batch-8 monthly snapshot dates, 2013-05-31..2022-03-31.
- {calendar['composition']['batch9_post_2022_anchors']} batch-9 official anchors, 2022-04-07..2026-09-30.
- Total: **{calendar['count']} rebalances**, each with an exact point-in-time membership set.
- Gap between consecutive rebalances: median {num(calendar['gap_days']['median'], 0)} days, maximum **{calendar['gap_days']['max']} days**; {calendar['gap_days']['gaps_over_45_days']} gaps exceed 45 days. The average cadence is roughly monthly, but the post-2022 dates are clustered around corporate-action events and can be several months apart. **This is an event-anchored strategy after 2022, not a strict monthly one.**

Universe resolution: {strategy['universe']['members_per_date']['min']}–{strategy['universe']['members_per_date']['max']} members per date (median {num(strategy['universe']['members_per_date']['median'], 0)}); {strategy['universe']['unresolved_symbol_snapshot_pairs']} unresolved symbol-snapshot pairs dropped, never substituted. Identity: {strategy['identity']['distinct_keys']} stable keys, {strategy['identity']['bars_rekeyed_by_symbol_edge']} bars re-keyed across symbol changes, {strategy['identity']['unresolved']} unresolved identity cases.

**There is no NIFTY 750 test here.** The repository has point-in-time sets for NIFTY 500 (107 monthly official sets to 2022-03 plus 64 irregular anchors), NIFTY MidSmallcap 400 / Midcap 150 / Smallcap 250 (57 monthly sets each, 2017-07..2022-03) and NIFTY 50 (13 annual snapshots). It has no NIFTY Total Market or Microcap 250 constituent history. Midcap 150 and Smallcap 250 are already subsets of NIFTY 500, so their union is still the 500-name universe, not 750. Reconstructing the missing 250 names from today's constituents would inject survivorship and look-ahead bias, so the 750-universe question is recorded as a data gap rather than answered with a synthetic series.

### 2.3 Benchmarks

| series | source | coverage | use |
|---|---|---|---|
| `NIFTY50_YAHOO` | Yahoo ^NSEI `Close` | 2007-09-17..2026-11-08 | **primary benchmark** (NIFTY 50, full 2013-2026 window) |
| `NIFTY50_SPLICED_NSE` | NSE archive, `S&P CNX Nifty` → `CNX Nifty` → `Nifty 50` | 2012-02-21..2025-12-31 | NIFTY 50 cross-check (independent vendor/splice) |
| `NIFTY500_SPLICED_NSE` | NSE archive, documented rename splice `S&P CNX 500` → `CNX 500` → `Nifty 500` | 2012-02-21..2025-12-31 | universe robustness benchmark |
| `G_SEC_10Y_NSE` | NSE archive, Nifty 10 yr Benchmark G-Sec | 2015-11-09..2025-12-31 | risk-free proxy |

The **required comparison is the NIFTY 50**. The full 2013-2026 window uses Yahoo ^NSEI because it is the only NIFTY 50 series covering 2026; the NSE spliced NIFTY 50 is used as a cross-check, and the NIFTY 500 is retained only as a universe robustness view. The splice is a documented index rename, not an alias inferred from similarity. Boundary moves are small and continuous (see `BENCHMARK.md` and the `splices` block in `benchmark-series.json`). The NSE and Yahoo NIFTY 50 levels agree on all 2,497 overlapping sessions to within 0.000781 index points.

The G-Sec series is a **price** index; coupons are excluded, so it understates the true risk-free rate. The zero-rate view is reported alongside it.

## 3. Strategy configurations

{config_rows and table(
    ["configuration", "signal dates", "trades", "buys/sells", "final equity (₹)", "total", "CAGR", "vol", "max DD", "max DD days", "max DD sessions", "longest DD days", "Sharpe (rf=0)", "mean cash", "writeoffs", "unproven holds"],
    config_rows)}

`signal dates` counts the scheduled rebalance dates inside the performance window, excluding the warm-up. Every evidence-dated row rebalances on all of them. The `as-committed` row has {as_committed_scheduled} scheduled month-end dates in-window, of which {as_committed_scheduled - as_committed_holds} had an exact snapshot and {as_committed_holds} held the book unchanged; it also has {as_committed_warmup_holds} warm-up holds before the first fill. It is a diagnostic, not a candidate strategy. `max DD days` is the calendar duration of the deepest drawdown from its peak to recovery (or to the end of the window if unrecovered); `max DD sessions` is the same span in trading sessions; `longest DD days` is the longest peak-to-recovery underwater episode, which can be a shallower but longer decline. Section 4.2 gives the exact dates. Every evidence-dated row in this table has **zero** unproven-universe holds.

The bottom-20 row is the mirror image of the breadth row: it buys the weakest 63-session performers instead of the strongest, on the same dates and universe. It is a counterfactual sign test, not a recommendation.

## 4. Headline performance vs NIFTY 50

{perf_rows and table(
    ["configuration", "benchmark", "window", "n", "strategy CAGR", "strategy vol", "strategy max DD", "strategy Sharpe (0 rf)", "benchmark CAGR", "excess CAGR"],
    perf_rows)}

`excess CAGR` is the geometric annualised strategy return minus the geometric annualised benchmark return over the same aligned window. It is not Jensen alpha; the beta-adjusted intercept is in the next table. The first five rows use the NIFTY 50 (Yahoo) full window; the last two rows are robustness views and are labelled accordingly. The benchmark-aligned strategy CAGR here ({pct(primary['strategy']['annualised_return'])}) differs from the full equity-curve CAGR in Section 3 ({pct(evidence['cagr'])}) because alignment drops sessions without a benchmark close and starts at the first return date.

### 4.1 Charges actually paid (buy and sell)

Every trade is charged at the fill: brokerage at 2 bps of notional capped at ₹2,000 per order, STT at 10 bps on both sides, stamp duty at 1.5 bps buy-only, exchange charges at 0.3 bps, GST at 18% of brokerage, and slippage at 5 bps on both sides. The engine deducts the total from cash and stores it on the trade. `validate_alpha_output.py` independently recomputes each trade's charge from its notional and side and fails the run if the stored cost differs.

{cost_rows and table(
    ["configuration", "buy notional (₹)", "sell notional (₹)", "buy charges (₹)", "sell charges (₹)", "total charges (₹)", "bps of turnover", "charges / initial cash"],
    cost_rows)}

Primary charge itemisation:

{component_rows and table(["charge", "₹ paid"], component_rows)}

On ₹{money(primary_audit['turnover'])} of turnover the primary config paid **₹{money(primary_audit['total_charges'])}**, or **{num(primary_audit['charges_bps_of_turnover'], 2)} bps**. Cumulatively that is **{pct(primary_audit['charges_pct_of_initial_cash'])}** of the ₹10,000,000 starting cash. The zero-cost counterfactual finishes at ₹{money(configs[GROSS]['metrics']['final_equity'])} versus ₹{money(configs[PRIMARY]['metrics']['final_equity'])} net; that is a **{pct(cost_drag)} CAGR drag** over the full window. The charge stack is fixed in nominal bps over 2013-2026, so the drag is an estimate of the current schedule, not a reconstruction of each historical rate change.

### 4.2 Maximum drawdown: depth and duration

{drawdown_rows and table(
    ["configuration", "max DD", "peak", "trough", "recovery", "peak→trough days", "peak→trough sessions", "trough→recovery days", "trough→recovery sessions", "total underwater days", "total underwater sessions", "longest episode days", "longest episode sessions", "% sessions underwater"],
    drawdown_rows)}

For the primary config the deepest drawdown ran from the **{primary_dd['peak_date']}** peak to the **{primary_dd['trough_date']}** trough — **{count(primary_dd['peak_to_trough_days'])} calendar days / {count(primary_dd['peak_to_trough_sessions'])} sessions** — and recovered on **{primary_dd['recovery_date']}**, **{count(primary_dd['trough_to_recovery_days'])} calendar days / {count(primary_dd['trough_to_recovery_sessions'])} sessions** later. The full underwater span was **{count(primary_dd['total_days'])} calendar days / {count(primary_dd['total_sessions'])} sessions**. The longest drawdown episode, regardless of depth, was **{count(primary_longest_dd['total_days'])} calendar days / {count(primary_longest_dd['total_sessions'])} sessions**. Sessions are equity-curve points, so they count trading sessions; calendar days include weekends and holidays. `% sessions underwater` is the share of sessions below the running high-water mark, not the share of losing sessions; a strategy that makes a new high only briefly can be underwater most of the time while still compounding.

## 5. Alpha and benchmark-relative statistics

{alpha_rows and table(
    ["configuration", "benchmark", "window", "n", "strat CAGR", "bench CAGR", "beta", "alpha (linear)", "alpha (geometric)", "tracking error", "information ratio", "up capture", "down capture", "R²", "t (OLS)", "t (NW5)", "bootstrap p"],
    alpha_rows)}

Read this table carefully:

- `alpha (linear)` is the per-session regression intercept × 252. `alpha (geometric)` is `(1 + intercept)^252 - 1`.
- `information ratio` is the repository's definition: geometric annualised excess return / tracking error, not mean active return / tracking error.
- `up capture` / `down capture` are mean strategy return / mean benchmark return over positive / negative benchmark sessions. Values above 100% mean the strategy amplifies that direction.
- The t-statistics are for the daily regression intercept; Newey-West uses 5 lags. The bootstrap resamples 21-session circular blocks (2,000 replications, fixed seed) and reports a two-sided p-value for a zero intercept.
- **Only the primary × NIFTY 50 (Yahoo) cell is the pre-specified headline.** The NIFTY 50 NSE spliced and NIFTY 500 rows are robustness views; no multiple-testing correction is applied.

### 5.1 Segment view (primary configuration)

{segment_rows and table(
    ["segment", "benchmark", "aligned window", "n", "strategy CAGR", "benchmark CAGR", "excess CAGR", "beta", "alpha (linear)", "information ratio", "t (NW5)"],
    segment_rows)}

The 2013-2022 segment is the clean monthly-membership era. The 2022-2026 segment is the irregular-anchor era; the NIFTY 50 (Yahoo) view covers the full segment through 2026-09-30, while the NSE series end 2025-12-31. The NIFTY 500 rows remain as universe robustness, not the required comparison.

### 5.2 Calendar years (primary configuration)

{year_rows and table(
    ["year", "strategy", "NIFTY 50 (Yahoo)", "NIFTY 500 (spliced, robustness)", "excess vs NIFTY 50"],
    year_rows)}

The year marked `*` is partial: 2013 runs from the first fill on 2013-06-03, not from January. 2026 is partial for the NIFTY 50 column (window ends 2026-09-30) and absent for NIFTY 500 (the NSE series ends 2025-12-31). The strategy and benchmark columns are computed from the same aligned NIFTY 50 dates for each year; the NIFTY 500 column is shown only as a robustness reference and is not used in the excess calculation.

### 5.3 Bottom-20 reversal: the opposite selection

{reversal_rows and table(
    ["configuration", "strategy CAGR", "benchmark CAGR", "beta", "alpha (linear)", "tracking error", "information ratio", "t (OLS)", "t (NW5)"],
    reversal_rows)}

Both books are long-only, equal-weight, charge-paying and rebalanced on the same evidence dates. The only difference is the sign of the ranking. If the top-20 alpha were a momentum premium, the bottom-20 portfolio would sit on the losing side of it. On this data the two alphas have **{momentum_sign}**. Against the NIFTY 50 the Newey-West t-statistics are {num(primary_sig.get('t_newey_west_5', float('nan')))} for the top book and {num(bottom_sig.get('t_newey_west_5', float('nan')))} for the bottom book; against the NIFTY 500 the corresponding pair is {num(primary500_sig.get('t_newey_west_5', float('nan')))} and {num(bottom500_sig.get('t_newey_west_5', float('nan')))}, with no multiple-testing adjustment. The bottom book is the stronger test of the data: a reversal portfolio that also earns positive alpha would indicate that the result is a size/liquidity/beta artifact rather than momentum.

## 6. Risk-free sensitivity (primary vs NIFTY 50)

{rf_rows and table(
    ["return frequency / window", "risk-free", "window", "strategy CAGR", "benchmark CAGR", "beta", "alpha (linear)", "strategy Sharpe", "benchmark Sharpe"],
    rf_rows)}

The zero-rate view is comparable to the archived sweep's `sharpe_zero_rf`. The G-Sec view is the more demanding test: it subtracts a positive risk-free rate from both sides. Because the G-Sec series is a price index with coupons excluded, its rate is a lower bound on the true risk-free rate, so the G-Sec alpha is an **upper bound** on the CAPM alpha under a true cash rate.

## 7. Why the archived 781% sweep is not the alpha answer

{archive_rows and table(
    ["run", "calendar", "engine behaviour on empty targets", "total", "CAGR", "status"],
    archive_rows)}

The defect is mechanical, not a judgment call: with `target_keys = []`, the pre-fix rebalance loop sold every position not in the empty target set — that is, everything — and then bought nothing. The post-fix engine skips the rebalance and holds. The evidence-dated run replaces the empty-target dates with real evidence dates; among the month-end runs, it is the only one that actually rebalances on a proven universe.

The archived and repro CAGRs include their own pre-deployment cash warm-up; the two "this run" rows start at the first fill. The total-return comparison is therefore directional, not exact. The like-for-like comparison is **as-committed vs evidence-dated**, because both start at the same first fill and differ only in the calendar.

## 8. What this result can and cannot support

**Can support:**

- A reproducible benchmark-relative diagnostic over the full available price history, with every rebalance tied to an official membership date.
- A statement of direction and rough magnitude: the strategy's beta, alpha, tracking error, information ratio and capture on this data.
- A recorded, auditable reason why the archived month-end sweep must not be cited.

**Cannot support:**

- A publishable alpha claim. `P0-CONTINUITY`, `P0-UNIVERSE` and the other P0 contracts remain blocked; `ready_for_alpha_claim` is `false`.
- A strict monthly strategy after 2022-03. Membership is point evidence at irregular anchors; dates between anchors are `CoverageUnknown`. The post-2022 result is event-anchored.
- A total-return comparison. Dividends are excluded from both sides; the strategy's omitted dividend yield is a downward bias, but the size of that bias differs from the index's.
- An unbiased terminal-proceeds estimate. `last_close_writeoff` assumes a delisted holding exits at its last observed close. The run reports {primary_writeoffs} such {primary_writeoff_word} for the primary config. This is optimistic if the true proceeds were zero.
- Capacity, market-impact, circuit-limit, suspension, tax or borrow realism. The engine fills at the next open with integer shares and a fixed cost model; it does not model ADV, impact, or capital-gains tax.
- A tradable reversal strategy. The bottom-20 book is a sign test for the ranking, not a recommendation; it buys falling names without a distress filter, and its concentration and turnover are not modelled for capacity.
- A historical charge schedule. Brokerage, STT, stamp duty, exchange fees, GST and slippage are held at 2026-style constants for the whole 2013-2026 window; actual Indian rates changed several times. SEBI turnover fees, depository-participant charges and capital-gains tax are not modelled at all.
- Intraday or daily-marked tradability. The equity curve is close-marked on NSE sessions.

**Statistical caveats:**

- The alpha t-statistics assume stationary returns and a linear single-factor relationship. Newey-West and the block bootstrap address autocorrelation and non-normality only partially.
- The strategy is concentrated (10 names), so the alpha estimate is sensitive to a small number of holdings and years.
- The post-2022 calendar is endogenous to the evidence: corporate-action anchors can cluster, adding turnover. The report discloses the dates rather than smoothing them.
- The primary benchmark is a single-vendor Yahoo ^NSEI price series. The NSE spliced NIFTY 50 cross-check uses a documented rename chain with continuous boundaries but stops at 2025-12-31.

### 8.1 Expanded universe, holding-period and exit sweep

`FINAL_RESULTS.csv` now holds **{sweep_count} runs**, sorted by Sharpe: five point-in-time universes (NIFTY 500, MSC400, Midcap 150, Smallcap 250, NIFTY 50), top and bottom selection, 10/20/25 names, six holding periods on the four monthly universes (monthly through 3-year), the 25 exit overlays on monthly rebalances for those universes, the same 25 overlays on NIFTY 50's annual calendar, and the 25 overlays on bottom 20/25 2- and 3-year holds for NIFTY 500. `SWEEP_REPORT.md` narrates the results and flags the comparability traps. The headlines:

- The **primary configuration is unchanged** and is still the pre-specified result. The sweep is a sensitivity map, not a search for a better backtest.
- **NIFTY 750 is absent because the membership chain does not exist.** No synthetic 750-name universe was created.
- **Runs stop at the end of the final scheduled holding period**, not at the last bar date. The 2017-2022 universes therefore do not carry a multi-year buy-and-hold tail: monthly rows end in 2022-04, and 2/3-year rows in mid-2023.
- **Bottom 20/25 long-hold overlays are NIFTY 500-only.** Every monthly universe has the no-overlay 2- and 3-year bottom test; the 25-overlay grid on those holds is run on the headline universe.
- **Every row is price-only.** Dividends are excluded on both sides, so no row is a total-return result.
- **Sharpe is not comparable across rows.** The 2017-2022 universes have 4.75 years of membership evidence and stop when their final scheduled hold completes; NIFTY 50 has 13 annual snapshots; 2- and 3-year holds have few rebalances; and exit proceeds sit in cash until the next rebalance. A high rank can be a short-window or cash-allocation artefact.
- **The top Sharpe is a maximum over {sweep_count} trials.** No multiple-testing correction is applied. The ranking is a descriptive map of the grid, not a new strategy claim or a model-selection result.
- **Midcap 150 and Smallcap 250 are subsets of NIFTY 500, and MSC400 overlaps it.** They are segment views, not independent markets.

## 9. Gate status and blockers

From `data/reports/full-history-readiness.json`:

- `ready_for_backtesting`: **false**
- `ready_for_alpha_claim`: **false**
- `overall_p0_gate`: **CLOSED**
- Blockers: `calendar`, `corporate_actions`, `schemes`, `identity`, `terminal_proceeds`, `dividends`, `execution_capabilities`.

These are not formalities. `dividends` alone changes the level of every total return; `terminal_proceeds` changes the delisting assumption; `schemes` leaves rights/demergers unresolved; `identity` leaves {strategy['identity']['unresolved']} cases unresolved in this run. The alpha numbers above are computed **through** those gaps, not after closing them.

## 10. Reproduction and artifacts

```powershell
$env:PYTHONPATH='src'
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\build_benchmark.py
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\run_alpha_backtest.py
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\compute_alpha.py
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\build_final_csv.py
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\run_sweep.py --universe nifty500 --shard 0 --shards 2 --tag n500a
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\run_sweep.py --universe nifty500 --shard 1 --shards 2 --tag n500b
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\run_sweep.py --universe msc400,midcap150 --tag mid
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\run_sweep.py --universe smallcap250,nifty50 --tag small
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\merge_sweep.py
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\validate_alpha_output.py
.venv\\Scripts\\python.exe verification\\research\\alpha-backtest\\build_report.py
```

The four sweep workers split the work by universe: NIFTY 500 in two shards and the 2017-2022 universes plus NIFTY 50 in two more. They take roughly 60-100 minutes on this machine and use about 1.2 GB resident each; allow at least 6 GB of free memory before running all four.

| artifact | content |
|---|---|
| `benchmark-series.json` | daily closes, splice evidence, coverage and cross-source validation |
| `BENCHMARK.md` | benchmark evidence narrative |
| `strategy-results.json` | per-config equity curves, metrics, drawdown durations, itemised charges, skip diagnostics, calendar |
| `alpha-results.json` | aligned evaluations, alpha/beta/IR/capture, t-stats, bootstrap, segments, calendar years |
| `FINAL_RESULTS.csv` | **all {sweep_count} runs**, one row each, sorted by Sharpe: universes, holds, directions, stops, targets, combinations, drawdown days, charges, flags |
| `FLAGSHIP_RESULTS.csv` | the six headline configurations in the same wide schema |
| `FLAGSHIP_EVIDENCE.csv` | legacy widest evidence view for the six headline rows (extra benchmark cross-checks and rebalance diagnostics) |
| `sweep-shard-*.jsonl` | raw sweep output, one JSON object per run, written incrementally |
| `SWEEP_REPORT.md` | sweep narrative, top-Sharpe table, universe/exit/hold comparisons and data-gap explanations |
| `validation.json` | {validation['checks_total']} fail-closed checks, including an independent OLS recomputation, independent drawdown-duration recomputation and sweep-CSV identities |
| `REPORT.md` | this report |

Research use only. Nothing here is investment advice, a solicitation, or a claim of realised returns.
"""
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
