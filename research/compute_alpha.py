"""Benchmark-relative alpha for the evidence-dated momentum backtest.

Reads the driver output (``strategy-results.json``) and the independently built
benchmark evidence (``benchmark-series.json``), aligns them on common trading
dates, and computes:

  total return, CAGR, annualised volatility, max drawdown, Sharpe
  beta, Jensen alpha (linear and geometric), information ratio, tracking error,
  up/down capture, R^2, tracking difference
  OLS and Newey-West(5) t-statistics for the alpha intercept
  a circular block-bootstrap p-value for the headline alpha
  monthly-return and calendar-year views

Conventions, stated rather than hidden:
  * Alignment is a strict date intersection. A strategy return is only paired
    with a benchmark return when both endpoints exist in both series. Nothing
    is forward-filled. Dropped dates and return spans are reported.
  * ``risk_free_rate`` for the zero-rate view is exactly 0 and is labelled.
    The G-Sec view derives a compounded annual rate from the Nifty 10 yr
    Benchmark G-Sec index over the same window, then converts it to a
    per-session rate.
  * ``information_ratio`` follows the repo's definition in
    ``nse_pipeline.analytics.metrics``: geometric annualised excess return
    divided by tracking error.
  * Jensen alpha is the regression intercept, annualised linearly (x252) and
    geometrically ((1+i)^252-1). It is not a claim of skill; the validation gate
    is still open and this is a research diagnostic.
"""
from __future__ import annotations

import json
import math
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import numpy as np

from nse_pipeline.analytics import metrics as m

HERE = Path(__file__).resolve().parent
STRATEGY = HERE / "strategy-results.json"
BENCHMARK = HERE / "benchmark-series.json"
OUT = HERE / "alpha-results.json"

PPY = 252
ZERO = Decimal(0)
ONE = Decimal(1)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------

def load_closes(payload: dict, key: str) -> dict[date, Decimal]:
    series = payload["series"][key]
    return {date.fromisoformat(day): Decimal(str(close)) for day, close in series}


def load_equity(config: dict) -> dict[date, Decimal]:
    return {date.fromisoformat(row["date"]): Decimal(row["equity"])
            for row in config["equity"]}


# --------------------------------------------------------------------------
# alignment and returns
# --------------------------------------------------------------------------

def align(equity: dict[date, Decimal], benchmark: dict[date, Decimal]):
    common = sorted(set(equity) & set(benchmark))
    spans = []
    s_ret, b_ret = [], []
    for previous, current in zip(common, common[1:]):
        spans.append((current - previous).days)
        s_ret.append(equity[current] / equity[previous] - ONE)
        b_ret.append(benchmark[current] / benchmark[previous] - ONE)
    return {
        "dates": common[1:],
        "strategy": s_ret,
        "benchmark": b_ret,
        "spans": spans,
        "dropped_strategy_dates": len(set(equity) - set(benchmark)),
        "dropped_benchmark_dates": len(set(benchmark) - set(equity)),
        "max_span_days": max(spans) if spans else None,
        "spans_over_7_days": sum(1 for span in spans if span > 7),
    }


def filter_rows(aligned: dict, start: date | None = None,
                end: date | None = None) -> dict:
    keep = [index for index, day in enumerate(aligned["dates"])
            if (start is None or day >= start) and (end is None or day <= end)]
    spans = [aligned["spans"][index] for index in keep] if aligned.get("spans") else []
    return {
        **aligned,
        "dates": [aligned["dates"][index] for index in keep],
        "strategy": [aligned["strategy"][index] for index in keep],
        "benchmark": [aligned["benchmark"][index] for index in keep],
        "spans": spans,
        "max_span_days": max(spans) if spans else None,
        "spans_over_7_days": sum(1 for span in spans if span > 7),
    }


def monthly_rows(aligned: dict) -> dict:
    """Last common observation of each month, then month-on-month returns."""
    last_by_month: dict[tuple[int, int], int] = {}
    for index, day in enumerate(aligned["dates"]):
        last_by_month[(day.year, day.month)] = index
    indices = [last_by_month[key] for key in sorted(last_by_month)]
    # Rebuild from levels: aligned["strategy"] stores returns, so compound them.
    levels_s, levels_b = [ONE], [ONE]
    for value_s, value_b in zip(aligned["strategy"], aligned["benchmark"]):
        levels_s.append(levels_s[-1] * (ONE + value_s))
        levels_b.append(levels_b[-1] * (ONE + value_b))
    s_ret, b_ret, days = [], [], []
    for previous, current in zip(indices, indices[1:]):
        s_ret.append(levels_s[current + 1] / levels_s[previous + 1] - ONE)
        b_ret.append(levels_b[current + 1] / levels_b[previous + 1] - ONE)
        days.append(aligned["dates"][current])
    return {"dates": days, "strategy": s_ret, "benchmark": b_ret, "spans": []}


def annual_rows(aligned: dict) -> dict:
    """Calendar-year returns, including the first partial year.

    The first row runs from the first common observation to the last common
    observation of that calendar year, so it is flagged ``partial``. Dropping
    it would hide the deployment year entirely.
    """
    last_by_year: dict[int, int] = {}
    for index, day in enumerate(aligned["dates"]):
        last_by_year[day.year] = index
    years = sorted(last_by_year)
    levels_s, levels_b = [ONE], [ONE]
    for value_s, value_b in zip(aligned["strategy"], aligned["benchmark"]):
        levels_s.append(levels_s[-1] * (ONE + value_s))
        levels_b.append(levels_b[-1] * (ONE + value_b))
    rows = []
    first = last_by_year[years[0]] + 1
    if first > 1:
        rows.append({
            "year": years[0],
            "partial": True,
            "strategy": str(levels_s[first] - ONE),
            "benchmark": str(levels_b[first] - ONE),
            "excess": str(levels_s[first] - levels_b[first]),
        })
    for previous_year, current_year in zip(years, years[1:]):
        previous = last_by_year[previous_year] + 1
        current = last_by_year[current_year] + 1
        rows.append({
            "year": current_year,
            "partial": False,
            "strategy": str(levels_s[current] / levels_s[previous] - ONE),
            "benchmark": str(levels_b[current] / levels_b[previous] - ONE),
            "excess": str((levels_s[current] / levels_s[previous])
                          - (levels_b[current] / levels_b[previous])),
        })
    return {"years": rows,
            "first_partial_year": years[0] if len(years) > 1 else None}


# --------------------------------------------------------------------------
# risk-free
# --------------------------------------------------------------------------

def gsec_rate(gsec: dict[date, Decimal], start: date, end: date):
    points = [(day, value) for day, value in sorted(gsec.items())
              if start <= day <= end]
    if len(points) < 2:
        return None
    days = (points[-1][0] - points[0][0]).days
    if days < 365:
        return None
    years = Decimal(days) / Decimal("365.25")
    annual = (points[-1][1] / points[0][1]) ** (ONE / years) - ONE
    daily = (ONE + annual) ** (ONE / Decimal(PPY)) - ONE
    return {
        "kind": "nifty_10y_gsec_price_index",
        "first": points[0][0].isoformat(),
        "last": points[-1][0].isoformat(),
        "annual": str(annual),
        "per_session": str(daily),
    }


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------

def alpha_tstats(strategy, benchmark, risk_free_rate: Decimal) -> dict:
    s = np.array([float(value) for value in strategy])
    b = np.array([float(value) for value in benchmark])
    rf = float(risk_free_rate)
    y = s - rf
    x = b - rf
    n = len(y)
    design = np.column_stack([np.ones(n), x])
    coefficients, *_ = np.linalg.lstsq(design, y, rcond=None)
    residuals = y - design @ coefficients
    dof = n - 2
    if dof <= 0:
        return {"error": "fewer than 3 observations"}
    sigma2 = float(residuals @ residuals) / dof
    xtx_inv = np.linalg.inv(design.T @ design)

    se_ols = math.sqrt(max(sigma2 * float(xtx_inv[0, 0]), 0.0))

    lags = 5
    scores = design * residuals[:, None]
    meat = scores.T @ scores
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        gamma = scores[lag:].T @ scores[:-lag]
        meat += weight * (gamma + gamma.T)
    covariance = xtx_inv @ meat @ xtx_inv * (n / dof)
    se_nw = math.sqrt(max(float(covariance[0, 0]), 0.0))

    return {
        "intercept_per_session": float(coefficients[0]),
        "se_ols": se_ols,
        "t_ols": float(coefficients[0]) / se_ols if se_ols else None,
        "se_newey_west_5": se_nw,
        "t_newey_west_5": (float(coefficients[0]) / se_nw) if se_nw else None,
        "n": n,
    }


def block_bootstrap_p(strategy, benchmark, risk_free_rate: Decimal, *,
                      block: int = 21, reps: int = 2000, seed: int = 20261119) -> dict:
    """Circular block bootstrap p-value for a non-zero regression intercept."""
    s = np.array([float(value) for value in strategy])
    b = np.array([float(value) for value in benchmark])
    rf = float(risk_free_rate)
    y, x = s - rf, b - rf
    n = len(y)
    rng = np.random.default_rng(seed)
    negative = 0
    positive = 0
    done = 0
    chunk = 200
    while done < reps:
        size = min(chunk, reps - done)
        starts = rng.integers(0, n, size=(size, math.ceil(n / block)))
        offsets = np.arange(block)
        indices = (starts[:, :, None] + offsets[None, None, :]).reshape(size, -1)[:, :n]
        indices %= n
        ys, xs = y[indices], x[indices]
        x_centered = xs - xs.mean(axis=1, keepdims=True)
        y_centered = ys - ys.mean(axis=1, keepdims=True)
        beta = (x_centered * y_centered).sum(axis=1) / (x_centered ** 2).sum(axis=1)
        intercept = ys.mean(axis=1) - beta * xs.mean(axis=1)
        negative += int((intercept <= 0).sum())
        positive += int((intercept >= 0).sum())
        done += size
    negative /= reps
    positive /= reps
    return {"reps": reps, "block_sessions": block,
            "p_two_sided": min(1.0, 2.0 * min(negative, positive)),
            "share_le_zero": negative}


def evaluate(aligned: dict, risk_free_rate: Decimal, rf_label: str,
             *, with_bootstrap: bool = False,
             periods_per_year: int = PPY) -> dict:
    s_ret, b_ret = aligned["strategy"], aligned["benchmark"]
    result = {
        "rf": rf_label,
        "periods_per_year": periods_per_year,
        "n_returns": len(s_ret),
        "first_return_date": aligned["dates"][0].isoformat() if aligned["dates"] else None,
        "last_return_date": aligned["dates"][-1].isoformat() if aligned["dates"] else None,
        "dropped_strategy_dates": aligned.get("dropped_strategy_dates", 0),
        "dropped_benchmark_dates": aligned.get("dropped_benchmark_dates", 0),
        "max_span_days": aligned.get("max_span_days"),
        "spans_over_7_days": aligned.get("spans_over_7_days", 0),
    }
    try:
        with localcontext() as ctx:
            ctx.prec = 34
            summary = m.benchmark_summary(
                s_ret, b_ret, risk_free_rate=risk_free_rate,
                periods_per_year=periods_per_year)
            strategy_return = m.annualised_return(s_ret, periods_per_year=periods_per_year)
            benchmark_return = m.annualised_return(b_ret, periods_per_year=periods_per_year)
            result.update({
                "strategy": {
                    "annualised_return": str(strategy_return),
                    "annualised_volatility": str(m.annualised_volatility(
                        s_ret, periods_per_year=periods_per_year)),
                    "max_drawdown": str(m.max_drawdown(s_ret)),
                    "sharpe": str(m.sharpe_ratio(
                        s_ret, risk_free_rate=risk_free_rate,
                        periods_per_year=periods_per_year)),
                },
                "benchmark": {
                    "annualised_return": str(benchmark_return),
                    "annualised_volatility": str(m.annualised_volatility(
                        b_ret, periods_per_year=periods_per_year)),
                    "max_drawdown": str(m.max_drawdown(b_ret)),
                    "sharpe": str(m.sharpe_ratio(
                        b_ret, risk_free_rate=risk_free_rate,
                        periods_per_year=periods_per_year)),
                },
                "relative": {
                    "beta": str(summary.beta),
                    "jensen_alpha_linear": str(summary.jensen_alpha_linear),
                    "jensen_alpha_geometric": str(summary.jensen_alpha_geometric),
                    "tracking_error": str(summary.tracking_error),
                    "information_ratio": str(summary.information_ratio),
                    "up_capture": str(summary.up_capture),
                    "down_capture": str(summary.down_capture),
                    "r_squared": str(summary.r_squared),
                    "tracking_difference": str(strategy_return - benchmark_return),
                },
            })
    except (ValueError, TypeError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
        return result

    result["alpha_significance"] = alpha_tstats(s_ret, b_ret, risk_free_rate)
    if with_bootstrap:
        result["alpha_significance"]["block_bootstrap"] = block_bootstrap_p(
            s_ret, b_ret, risk_free_rate)
    return result


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    strategy_payload = json.loads(STRATEGY.read_text(encoding="utf-8"))
    benchmark_payload = json.loads(BENCHMARK.read_text(encoding="utf-8"))

    benchmarks = {}
    for key in ("NIFTY500_SPLICED_NSE", "NIFTY50_SPLICED_NSE", "NIFTY50_YAHOO"):
        if key in benchmark_payload["series"]:
            benchmarks[key] = load_closes(benchmark_payload, key)
    if not benchmarks:
        raise SystemExit("benchmark-series.json has none of the required series")
    gsec = load_closes(benchmark_payload, "G_SEC_10Y_NSE")

    # Bayesian-free, explicit segment boundaries.
    segments = {
        "monthly_membership_2013_2022": (date(2013, 5, 31), date(2022, 3, 31)),
        "anchor_membership_2022_2026": (date(2022, 4, 1), date(2026, 9, 30)),
    }

    output = {
        "schema_version": 1,
        "status": "research_diagnostic_p0_closed",
        "primary_config": "evidence-N10-L63-next_open-no_stop",
        "strategy_window": strategy_payload["window"],
        "calendar": strategy_payload["calendar"],
        "benchmark_coverage": {
            name: {"first": min(series).isoformat(), "last": max(series).isoformat(),
                   "rows": len(series)}
            for name, series in benchmarks.items()
        },
        "strategy_diagnostics": {},
        "evaluations": {},
        "monthly": {},
        "calendar_years": {},
    }

    for config in strategy_payload["configs"]:
        label = config["label"]
        equity = load_equity(config)
        cash_share = [float(Decimal(row["cash"]) / Decimal(row["equity"]))
                      for row in config["equity"] if Decimal(row["equity"]) > 0]
        positions = [row["n_positions"] for row in config["equity"]]
        output["strategy_diagnostics"][label] = {
            "sessions": len(config["equity"]),
            "first": config["equity"][0]["date"],
            "last": config["equity"][-1]["date"],
            "first_trade": config["first_trade"],
            "last_trade": config["last_trade"],
            "n_trades": config["n_trades"],
            "n_buys": config["n_buys"],
            "n_sells": config["n_sells"],
            "n_delisting_writeoffs": len(config["delisting_writeoffs"]),
            "mean_cash_share": sum(cash_share) / len(cash_share),
            "mean_positions": sum(positions) / len(positions),
            "min_positions": min(positions),
            "max_positions": max(positions),
            "sessions_fully_cash": sum(1 for value in cash_share if value > 0.9999),
            "unproven_universe_holds": len(config["unproven_universe_holds"]),
        }
        output["evaluations"][label] = {}
        for name, series in benchmarks.items():
            aligned = align(equity, series)
            if len(aligned["dates"]) < 3:
                output["evaluations"][label][name] = {"error": "insufficient overlap"}
                continue
            entry = {"alignment": {
                "first": aligned["dates"][0].isoformat(),
                "last": aligned["dates"][-1].isoformat(),
                "common_return_dates": len(aligned["dates"]),
                "dropped_strategy_dates": aligned["dropped_strategy_dates"],
                "dropped_benchmark_dates": aligned["dropped_benchmark_dates"],
                "max_span_days": aligned["max_span_days"],
                "spans_over_7_days": aligned["spans_over_7_days"],
            }}

            entry["full_rf0"] = evaluate(
                aligned, ZERO, "zero",
                with_bootstrap=(label == output["primary_config"]))

            gsec_window = gsec_rate(gsec, aligned["dates"][0], aligned["dates"][-1])
            if gsec_window is not None:
                overlap = filter_rows(aligned, date.fromisoformat(gsec_window["first"]),
                                      date.fromisoformat(gsec_window["last"]))
                if len(overlap["dates"]) >= 3:
                    entry["overlap_gsec"] = evaluate(
                        overlap, Decimal(gsec_window["per_session"]),
                        "gsec:" + gsec_window["annual"])
                    entry["overlap_gsec"]["rf_detail"] = gsec_window

            entry["segments"] = {}
            for segment_name, (start, end) in segments.items():
                sliced = filter_rows(aligned, start, end)
                if len(sliced["dates"]) < 3:
                    continue
                entry["segments"][segment_name] = evaluate(
                    sliced, ZERO, "zero")
                gsec_segment = gsec_rate(gsec, start, end)
                if gsec_segment is not None:
                    overlap = filter_rows(sliced,
                                          max(start, date.fromisoformat(gsec_segment["first"])),
                                          min(end, date.fromisoformat(gsec_segment["last"])))
                    if len(overlap["dates"]) >= 3:
                        entry["segments"][segment_name]["gsec"] = evaluate(
                            overlap, Decimal(gsec_segment["per_session"]),
                            "gsec:" + gsec_segment["annual"])
                        entry["segments"][segment_name]["gsec"]["rf_detail"] = gsec_segment

            output["evaluations"][label][name] = entry

            if label == output["primary_config"]:
                monthly = monthly_rows(aligned)
                if len(monthly["dates"]) >= 3:
                    output["monthly"][name] = evaluate(
                        monthly, ZERO, "zero", periods_per_year=12)
                    gsec_monthly = gsec_rate(gsec, monthly["dates"][0], monthly["dates"][-1])
                    if gsec_monthly is not None:
                        annual = Decimal(gsec_monthly["annual"])
                        monthly_rf = (ONE + annual) ** (ONE / Decimal(12)) - ONE
                        # The rate is only observed from the G-Sec series' first
                        # date. Applying it to earlier monthly returns would
                        # silently extend a 2015-2025 proxy back to 2013.
                        overlap = filter_rows(
                            monthly,
                            date.fromisoformat(gsec_monthly["first"]),
                            date.fromisoformat(gsec_monthly["last"]))
                        if len(overlap["dates"]) >= 3:
                            output["monthly"][name]["gsec"] = evaluate(
                                overlap, monthly_rf, "gsec:" + gsec_monthly["annual"],
                                periods_per_year=12)
                            output["monthly"][name]["gsec"]["rf_detail"] = gsec_monthly
                output["calendar_years"][name] = annual_rows(aligned)

    OUT.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
