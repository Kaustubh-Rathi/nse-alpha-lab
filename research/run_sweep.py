"""Expanded universe, holding-period and exit-overlay sweep.

This runner answers the "what about the other universes, longer holds, stops
and profit cut-offs?" questions without pretending the data covers more than it
does.

Universes (all point-in-time, exact-date membership):

* ``nifty500``    NIFTY 500, 171 evidence dates, 2013-05-31..2026-09-30
                  (107 monthly snapshots to 2022-03, then 64 irregular anchors)
* ``msc400``      NIFTY MidSmallcap 400, 57 monthly official sets, 2017-07..2022-03
* ``midcap150``   NIFTY Midcap 150, 57 monthly official sets, 2017-07..2022-03
* ``smallcap250`` NIFTY Smallcap 250, 57 monthly official sets, 2017-07..2022-03
* ``nifty50``     NIFTY 50, 13 annual exact snapshots, 2013-10..2025-10

There is **no NIFTY 750 / NIFTY Total Market membership chain** on disk. The
NIFTY 500 already contains the Midcap 150 and Smallcap 250, so adding those
sets does not produce a 750-name universe. A true 750-name test needs the
Microcap 250 / Total Market constituent history, which this repo does not have.
It is not fabricated here.

Calendars: every evidence-dated rebalance is an exact membership date; the one
superseded month-end diagnostic is the exception. A multi-period cadence takes
the first evidence date at or after each 3-, 6-, 12-, 24- or 36-month boundary
counted from the universe's first date. 2-year and 3-year holds therefore
anchor their first rebalance at the first available membership date instead of
skipping the first block. Nothing is interpolated or forward-filled.

Performance windows: each run ends at the close of its final scheduled holding
period (monthly +1 month, 2-year +24 months, and so on), capped at 2026-09-30.
A universe whose membership chain stops in 2022 cannot rebalance afterwards,
but its last cohort still completes the stated hold. This avoids reporting a
multi-year buy-and-hold tail as if it were the strategy; it is why a monthly
MSC400 row ends in 2022-04 while NIFTY 500 runs to 2026-09.

Exits: stop-loss and take-profit are evaluated on the completed session close
and filled at the next session open. A 5/10/15/20 stop can be combined with a
5/10/15/20 target; all 16 combinations plus the 8 single-leg policies and the
no-overlay case are run. Exit proceeds are held in cash until the next
scheduled rebalance, which matters a lot for 2- and 3-year holds.

Prices: split/bonus-adjusted only. Dividends are not applied and the benchmark
is a price index, so this is a price-vs-price comparison, not total return.

Every row carries the same research-preview caveat as the flagship report: the
internal validation gate is open and these are research diagnostics, not published returns.

Usage::

    python verification/research/alpha-backtest/run_sweep.py --universe nifty500 --shard 0 --shards 2 --tag n500a
    python verification/research/alpha-backtest/run_sweep.py --universe nifty500 --shard 1 --shards 2 --tag n500b
    python verification/research/alpha-backtest/run_sweep.py --universe msc400,midcap150 --tag mid
    python verification/research/alpha-backtest/run_sweep.py --universe smallcap250,nifty50 --tag small
    python verification/research/alpha-backtest/merge_sweep.py

The default grid includes the NIFTY 50 annual exit rows, so these four
workers cover all 968 configurations. ``--family``, ``--match``, ``--cadence``,
``--universe``, ``--tag`` and ``--out`` also support targeted reruns; a tagged
shard is still picked up by ``merge_sweep.py``.
"""
from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from collections import Counter
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

from compute_alpha import align, evaluate  # noqa: E402

from nse_pipeline.backtest.exits import ExitPolicy  # noqa: E402
from nse_pipeline.backtest.momentum import (  # noqa: E402
    COST_COMPONENTS,
    BarsFrame,
    EngineConfig,
    MomentumRotationEngine,
)
from nse_pipeline.backtest.momentum_grid import (  # noqa: E402
    DEFAULT_ADJUSTED,
    DEFAULT_EDGES,
    DEFAULT_EXTRA_SNAPSHOTS,
    DEFAULT_SNAPSHOTS,
    compute_metrics,
    load_adjusted_bars,
    load_membership,
    resolve_membership_to_keys,
)
from nse_pipeline.backtest.monthly_universe import (  # noqa: E402
    coverage as monthly_coverage,
    load_monthly_sets,
)
from nse_pipeline.backtest.security_identity import resolve_identity  # noqa: E402

WARMUP_START = date(2012, 6, 1)
WINDOW_START = date(2013, 5, 31)
WINDOW_END = date(2026, 9, 30)
SESSIONS_PER_YEAR = 252
NIFTY50_DIR = ROOT / "data" / "processed" / "index_membership"
BENCHMARK = HERE / "benchmark-series.json"

BASE = dict(
    lookback_sessions=63,
    entry_timing="next_open",
    delisting_policy="last_close_writeoff",
    min_days_observed=130,
)

HOLDING_MONTHS = {
    "monthly": 1,
    "quarterly": 3,
    "semiannual": 6,
    "annual": 12,
    "2y": 24,
    "3y": 36,
}


def add_months(day: date, months: int) -> date:
    """Calendar-month addition with month-end clamping."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def performance_horizon(last_signal: date, cadence: str,
                        data_end: date = WINDOW_END) -> date:
    """End of the final holding period, capped at the available data.

    A universe whose membership chain stops in 2022 cannot keep rebalancing
    afterwards, but the last cohort it bought still has a stated holding period.
    Ending at the last signal date would truncate that cohort; ending at the
    last bar date (the first sweep implementation) silently turned a monthly
    strategy into a multi-year buy-and-hold. This returns the end of the final
    scheduled hold instead, so a 2-year row runs for two more years and a
    monthly row runs for one more month, never past ``data_end``.
    """
    return min(add_months(last_signal, HOLDING_MONTHS[cadence]), data_end)

EXIT_PCTS = ("0.05", "0.10", "0.15", "0.20")

UNIVERSES = (
    ("nifty500", "NIFTY 500", ("monthly", "quarterly", "semiannual", "annual", "2y", "3y")),
    ("msc400", "NIFTY MidSmallcap 400 (MSC400)",
     ("monthly", "quarterly", "semiannual", "annual", "2y", "3y")),
    ("midcap150", "NIFTY Midcap 150",
     ("monthly", "quarterly", "semiannual", "annual", "2y", "3y")),
    ("smallcap250", "NIFTY Smallcap 250",
     ("monthly", "quarterly", "semiannual", "annual", "2y", "3y")),
    ("nifty50", "NIFTY 50 (annual exact snapshots)", ("annual",)),
)

OVERLAP = {
    "nifty500": "broad market; contains Midcap 150 and Smallcap 250",
    "msc400": "mid/small segment; overlaps NIFTY 500",
    "midcap150": "subset of NIFTY 500 (rank 101-250)",
    "smallcap250": "subset of NIFTY 500 (rank 251-500)",
    "nifty50": "large-cap only; annual membership snapshots",
}


# --------------------------------------------------------------------------
# calendars and configs
# --------------------------------------------------------------------------

def select_cadence_dates(dates, cadence: str) -> list[date]:
    """Select rebalance dates at fixed holding-period boundaries.

    Monthly keeps every evidence date. Quarterly, semiannual, annual, 2-year
    and 3-year holdings divide the timeline into 3-, 6-, 12-, 24- and 36-month
    blocks counted from the universe's first evidence date, then take the first
    evidence date in each block. That anchors the first rebalance at the first
    available membership date, so a 2-year test does not silently skip its first
    two years. Nothing is interpolated: a block with no evidence date produces
    no rebalance.
    """
    ordered = sorted(set(dates))
    if not ordered:
        return []
    if cadence == "monthly":
        return ordered
    months = {
        "quarterly": 3,
        "semiannual": 6,
        "annual": 12,
        "2y": 24,
        "3y": 36,
    }.get(cadence)
    if months is None:
        raise ValueError(f"unknown cadence {cadence!r}")
    first = ordered[0]
    start = first.year * 12 + (first.month - 1)
    chosen: dict[int, date] = {}
    for day in ordered:
        block = ((day.year * 12 + (day.month - 1)) - start) // months
        chosen.setdefault(block, day)
    return [chosen[block] for block in sorted(chosen)]


def exit_specs() -> list[tuple[str, str | None, str | None]]:
    specs: list[tuple[str, str | None, str | None]] = [("none", None, None)]
    for value in EXIT_PCTS:
        tag = value.replace("0.", "")
        specs.append((f"sl{tag}", value, None))
    for value in EXIT_PCTS:
        tag = value.replace("0.", "")
        specs.append((f"tp{tag}", None, value))
    for stop in EXIT_PCTS:
        for target in EXIT_PCTS:
            specs.append((f"sl{stop.replace('0.', '')}_tp{target.replace('0.', '')}",
                          stop, target))
    return specs


def make_id(universe: str, direction: str, top_n: int, cadence: str,
            overlay: str, *, costs_zero: bool = False,
            calendar_mode: str = "evidence") -> str:
    parts = [universe, direction, f"N{top_n}", "L63", cadence, overlay]
    if costs_zero:
        parts.append("zero_cost")
    if calendar_mode != "evidence":
        parts.append(calendar_mode)
    return "_".join(parts)


def build_configs() -> list[dict]:
    configs: list[dict] = []
    seen: set[tuple] = set()

    def add(*, family: str, universe: str, direction: str, top_n: int,
            cadence: str, overlay: str, stop: str | None, take: str | None,
            costs_zero: bool = False, calendar_mode: str = "evidence") -> None:
        key = (universe, direction, top_n, cadence, stop, take,
               costs_zero, calendar_mode)
        if key in seen:
            return
        seen.add(key)
        configs.append({
            "id": make_id(universe, direction, top_n, cadence, overlay,
                          costs_zero=costs_zero, calendar_mode=calendar_mode),
            "family": family,
            "universe": universe,
            "direction": direction,
            "top_n": top_n,
            "cadence": cadence,
            "overlay": overlay,
            "stop": stop,
            "take": take,
            "costs_zero": costs_zero,
            "calendar_mode": calendar_mode,
        })

    # 1. Core: every universe, both directions, 10/20/25 names, every cadence.
    for universe, _label, cadences in UNIVERSES:
        for direction in ("top", "bottom"):
            for top_n in (10, 20, 25):
                for cadence in cadences:
                    add(family="universe_core", universe=universe,
                        direction=direction, top_n=top_n, cadence=cadence,
                        overlay="none", stop=None, take=None)

    # 2. Exit grid: all 25 overlays, monthly, every monthly universe.
    for universe in ("nifty500", "msc400", "midcap150", "smallcap250"):
        for direction in ("top", "bottom"):
            for top_n in (10, 20, 25):
                for overlay, stop, take in exit_specs():
                    add(family="exit_grid_monthly", universe=universe,
                        direction=direction, top_n=top_n, cadence="monthly",
                        overlay=overlay, stop=stop, take=take)

    # 3. Bottom 20/25 on a 2- or 3-year hold, every overlay.
    for top_n in (20, 25):
        for cadence in ("2y", "3y"):
            for overlay, stop, take in exit_specs():
                add(family="bottom_hold_exit", universe="nifty500",
                    direction="bottom", top_n=top_n, cadence=cadence,
                    overlay=overlay, stop=stop, take=take)

    # 4. Every exit overlay on the NIFTY 50 annual universe. The core rows above
    #    cover no overlay; these add the same stop/target grid to the large-cap
    #    comparison universe. Exits fill at the next open and proceeds sit in
    #    cash until the next annual rebalance, so these are intentionally
    #    flagged as cash-heavy diagnostics.
    for direction in ("top", "bottom"):
        for top_n in (10, 20, 25):
            for overlay, stop, take in exit_specs():
                if overlay == "none":
                    continue
                add(family="nifty50_exit_annual", universe="nifty50",
                    direction=direction, top_n=top_n, cadence="annual",
                    overlay=overlay, stop=stop, take=take)

    # 5. Two attribution/diagnostic rows from the flagship report.
    add(family="counterfactual_zero_cost", universe="nifty500", direction="top",
        top_n=10, cadence="monthly", overlay="none", stop=None, take=None,
        costs_zero=True)
    add(family="superseded_diagnostic", universe="nifty500", direction="top",
        top_n=10, cadence="monthly", overlay="none", stop=None, take=None,
        calendar_mode="month_end_exact")

    return configs


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def load_nifty50_snapshots() -> dict[date, set[str]]:
    membership: dict[date, set[str]] = {}
    for path in sorted(NIFTY50_DIR.glob("NIFTY50_*.csv")):
        try:
            as_of = date.fromisoformat(path.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        if not (WINDOW_START <= as_of <= WINDOW_END):
            continue
        frame = pd.read_csv(path, usecols=["symbol"])
        symbols = {str(value).strip().upper() for value in frame["symbol"].dropna()}
        symbols.discard("")
        if symbols:
            membership[as_of] = symbols
    return membership


def prepare_context() -> dict:
    print("loading bars ...", flush=True)
    bars = load_adjusted_bars(DEFAULT_ADJUSTED, WARMUP_START, WINDOW_END, None)
    if bars.empty:
        raise SystemExit("no bars")
    identity = resolve_identity(bars, DEFAULT_EDGES)
    bars = identity.frame
    frame = BarsFrame(bars)
    print(f"bars: {len(bars):,} rows, {len(frame.sessions):,} sessions", flush=True)

    universes: dict[str, dict] = {}

    print("resolving NIFTY 500 membership ...", flush=True)
    raw = load_membership([DEFAULT_SNAPSHOTS, DEFAULT_EXTRA_SNAPSHOTS])
    dates = sorted(d for d in raw if WINDOW_START <= d <= WINDOW_END)
    resolved = resolve_membership_to_keys({d: raw[d] for d in dates}, bars)
    unresolved = resolved.pop("_unresolved_symbol_dates", 0)
    dates = sorted(resolved)
    universes["nifty500"] = {
        "label": "NIFTY 500 exact-date membership",
        "membership": resolved,
        "dates": dates,
        "coverage": {
            "snapshots": len(dates),
            "first": dates[0].isoformat(),
            "last": dates[-1].isoformat(),
            "members_min": min(len(resolved[d]) for d in dates),
            "members_max": max(len(resolved[d]) for d in dates),
            "unresolved_symbol_dates": unresolved,
        },
    }

    for alias in ("msc400", "midcap150", "smallcap250"):
        print(f"resolving {alias} membership ...", flush=True)
        raw = load_monthly_sets(alias)
        resolved = resolve_membership_to_keys(raw, bars)
        unresolved = resolved.pop("_unresolved_symbol_dates", 0)
        dates = sorted(resolved)
        summary = monthly_coverage(alias)
        universes[alias] = {
            "label": summary["index"],
            "membership": resolved,
            "dates": dates,
            "coverage": {
                **summary,
                "unresolved_symbol_dates": unresolved,
            },
        }

    print("resolving NIFTY 50 annual snapshots ...", flush=True)
    raw = load_nifty50_snapshots()
    resolved = resolve_membership_to_keys(raw, bars)
    unresolved = resolved.pop("_unresolved_symbol_dates", 0)
    dates = sorted(resolved)
    universes["nifty50"] = {
        "label": "NIFTY 50 annual exact snapshots",
        "membership": resolved,
        "dates": dates,
        "coverage": {
            "snapshots": len(dates),
            "first": dates[0].isoformat(),
            "last": dates[-1].isoformat(),
            "members_min": min(len(resolved[d]) for d in dates),
            "members_max": max(len(resolved[d]) for d in dates),
            "unresolved_symbol_dates": unresolved,
        },
    }

    payload = json.loads(BENCHMARK.read_text(encoding="utf-8"))
    benchmarks = {
        key: {date.fromisoformat(day): Decimal(str(close))
              for day, close in series}
        for key, series in payload["series"].items()
    }
    return {"frame": frame, "universes": universes, "benchmarks": benchmarks}


# --------------------------------------------------------------------------
# run and serialise
# --------------------------------------------------------------------------

def trim_to_deployment(equity: list[dict], trades: list[dict]) -> list[dict]:
    if not trades:
        return equity
    first_fill = min(trade["date"] for trade in trades)
    index = next((i for i, row in enumerate(equity) if row["date"] == first_fill), None)
    if index is None:
        return [row for row in equity if row["date"] >= first_fill]
    return equity[max(index - 1, 0):]


def cost_audit(config: EngineConfig, trades: list[dict]) -> dict:
    components = {name: Decimal(0) for name in COST_COMPONENTS}
    buy_notional = sell_notional = Decimal(0)
    buy_charges = sell_charges = Decimal(0)
    for trade in trades:
        notional = Decimal(trade["notional"])
        parts = config.cost_components(notional, trade["side"])
        stored = Decimal(trade["cost"])
        if sum(parts.values(), Decimal(0)) != stored:
            raise AssertionError(
                f"cost audit mismatch on {trade['date']} {trade['side']} "
                f"{trade['key']}: stored {stored}, components {parts}")
        for name, value in parts.items():
            components[name] += value
        if trade["side"] == "buy":
            buy_notional += notional
            buy_charges += stored
        else:
            sell_notional += notional
            sell_charges += stored
    turnover = buy_notional + sell_notional
    total = buy_charges + sell_charges
    return {
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "turnover": turnover,
        "buy_charges": buy_charges,
        "sell_charges": sell_charges,
        "total_charges": total,
        "charges_bps_of_turnover": (total / turnover * Decimal(10_000)
                                    if turnover else None),
        "charges_pct_of_initial_cash": total / config.initial_cash,
        "components": components,
    }


def equity_from_rows(rows: list[dict]) -> dict[date, Decimal]:
    result: dict[date, Decimal] = {}
    for row in rows:
        if row["equity"] is None:
            continue
        day = row["date"] if isinstance(row["date"], date) else date.fromisoformat(
            str(row["date"]))
        result[day] = Decimal(row["equity"])
    return result


def relative(equity: dict[date, Decimal], benchmark: dict[date, Decimal]) -> dict:
    if len(equity) < 3:
        return {"error": "fewer than 3 known-equity sessions"}
    aligned = align(equity, benchmark)
    return evaluate(aligned, Decimal(0), "zero")


def as_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_pct(value):
    if value is None:
        return None
    try:
        return float(Decimal(str(value)) * 100)
    except (TypeError, ValueError):
        return None


def run_one(spec: dict, context: dict) -> dict:
    universe = context["universes"][spec["universe"]]
    membership = universe["membership"]

    if spec["calendar_mode"] == "month_end_exact":
        dates: list[date] = []
        rebalance_dates = None
        cadence = "monthly"
    else:
        dates = select_cadence_dates(universe["dates"], spec["cadence"])
        rebalance_dates = tuple(dates)
        cadence = spec["cadence"]

    policy = ExitPolicy(
        stop_loss_pct=Decimal(spec["stop"]) if spec["stop"] else None,
        take_profit_pct=Decimal(spec["take"]) if spec["take"] else None,
    )
    config = EngineConfig(
        cadence=cadence,
        rebalance_dates=rebalance_dates,
        top_n=spec["top_n"],
        direction=spec["direction"],
        exit_policy=policy,
        **BASE,
    )
    if spec["costs_zero"]:
        config = replace(
            config,
            brokerage_bps=Decimal("0"),
            stt_bps_buy=Decimal("0"),
            stt_bps_sell=Decimal("0"),
            stamp_bps=Decimal("0"),
            exchange_bps=Decimal("0"),
            gst_on_brokerage_pct=Decimal("0"),
            slippage_bps=Decimal("0"),
        )

    outcome = MomentumRotationEngine(config).run(context["frame"],
                                                universe=membership)

    def row_date(row: dict) -> date:
        value = row.get("date")
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    cadence_dates = [date.fromisoformat(day) for day in outcome["cadence"]["dates"]
                     if day >= WINDOW_START.isoformat()]
    last_signal = max(cadence_dates) if cadence_dates else WINDOW_END
    horizon = performance_horizon(last_signal, spec["cadence"])

    equity_rows = [row for row in outcome["equity"] if row_date(row) <= horizon]
    trades = [trade for trade in outcome["trades"] if row_date(trade) <= horizon]
    skips = [row for row in outcome["skips"] if row_date(row) <= horizon]
    exits = [row for row in outcome["exits"] if row_date(row) <= horizon]
    equity = trim_to_deployment(equity_rows, trades) if trades else equity_rows
    metrics = compute_metrics(equity)
    charges = cost_audit(config, trades)

    hold_gaps = [row for row in skips
                 if row["reason"] == "no_proven_universe_hold_unchanged"]
    writeoffs = [row for row in exits
                 if row["reason"].startswith("delisting_")]
    buys = sum(1 for trade in trades if trade["side"] == "buy")
    sells = sum(1 for trade in trades if trade["side"] == "sell")
    signal_dates = {trade["signal_date"] for trade in trades
                    if trade.get("signal_date") is not None}
    # The evidence rows already start at WINDOW_START. The month-end diagnostic
    # is generated from the loaded warm-up sessions, so count only the dates
    # that fall inside the reported performance window.
    scheduled_dates = cadence_dates

    row = {
        "config_id": spec["id"],
        "family": spec["family"],
        "universe": spec["universe"],
        "universe_label": universe["label"],
        "direction": spec["direction"],
        "top_n": spec["top_n"],
        "lookback_sessions": config.lookback_sessions,
        "cadence": spec["cadence"],
        "holding_period_months": HOLDING_MONTHS[spec["cadence"]],
        "calendar_mode": spec["calendar_mode"],
        "stop_loss_pct": spec["stop"],
        "take_profit_pct": spec["take"],
        "exit_overlay": spec["overlay"],
        "costs": "zero" if spec["costs_zero"] else "all_charges",
        "scheduled_rebalances": len(scheduled_dates),
        "signal_dates_with_trades": len(signal_dates),
        "unproven_universe_holds": len(hold_gaps),
        "last_signal_date": last_signal.isoformat(),
        "scheduled_horizon_end": horizon.isoformat(),
        "first_fill": (min(row_date(trade) for trade in trades).isoformat()
                       if trades else None),
        "last_fill": (max(row_date(trade) for trade in trades).isoformat()
                      if trades else None),
        "publishable": bool(metrics.get("publishable")),
        "refusal": metrics.get("refusal"),
        "trades": len(trades),
        "buys": buys,
        "sells": sells,
        "skips": len(skips),
        "skip_reasons": dict(Counter(f"{row['side']}:{row['reason']}"
                                     for row in skips)),
        "delisting_writeoffs": len(writeoffs),
        "buy_notional_inr": as_float(charges["buy_notional"]),
        "sell_notional_inr": as_float(charges["sell_notional"]),
        "turnover_inr": as_float(charges["turnover"]),
        "buy_charges_inr": as_float(charges["buy_charges"]),
        "sell_charges_inr": as_float(charges["sell_charges"]),
        "total_charges_inr": as_float(charges["total_charges"]),
        "charges_bps_of_turnover": as_float(charges["charges_bps_of_turnover"]),
        "charges_pct_of_initial_cash": as_float(charges["charges_pct_of_initial_cash"]),
    }
    for name, value in charges["components"].items():
        row[f"{name}_inr"] = as_float(value)

    if metrics.get("publishable"):
        row.update({
            "performance_start": metrics["start"],
            "performance_end": metrics["end"],
            "sessions": metrics["sessions_known_equity"],
            "window_years": metrics["sessions_known_equity"] / SESSIONS_PER_YEAR,
            "initial_equity_inr": as_float(metrics["initial_equity"]),
            "final_equity_inr": as_float(metrics["final_equity"]),
            "total_return_pct": as_pct(metrics["total_return"]),
            "cagr_pct": as_pct(metrics["cagr"]),
            "annualised_volatility_pct": as_pct(metrics["annualised_volatility"]),
            "sharpe_rf0": as_float(metrics["sharpe_zero_rf"]),
            "max_drawdown_pct": as_pct(metrics["max_drawdown"]),
            "drawdown_episodes": metrics["drawdown_durations"]["episodes"],
            "sessions_underwater": metrics["drawdown_durations"]["sessions_underwater"],
            "pct_sessions_underwater": as_pct(
                metrics["drawdown_durations"]["pct_sessions_underwater"]),
        })
        worst = metrics["drawdown_durations"].get("worst") or {}
        longest = metrics["drawdown_durations"].get("longest") or {}
        row.update({
            "max_dd_peak_date": worst.get("peak_date"),
            "max_dd_trough_date": worst.get("trough_date"),
            "max_dd_recovery_date": worst.get("recovery_date"),
            "max_dd_peak_to_trough_days": worst.get("peak_to_trough_days"),
            "max_dd_peak_to_trough_sessions": worst.get("peak_to_trough_sessions"),
            "max_dd_trough_to_recovery_days": worst.get("trough_to_recovery_days"),
            "max_dd_trough_to_recovery_sessions": worst.get("trough_to_recovery_sessions"),
            "max_dd_total_days": worst.get("total_days"),
            "max_dd_total_sessions": worst.get("total_sessions"),
            "max_dd_recovered": worst.get("recovered"),
            "longest_dd_peak_date": longest.get("peak_date"),
            "longest_dd_trough_date": longest.get("trough_date"),
            "longest_dd_recovery_date": longest.get("recovery_date"),
            "longest_dd_total_days": longest.get("total_days"),
            "longest_dd_total_sessions": longest.get("total_sessions"),
        })

        equity_map = equity_from_rows(equity)
        n50 = relative(equity_map, context["benchmarks"]["NIFTY50_YAHOO"])
        n500 = relative(equity_map, context["benchmarks"]["NIFTY500_SPLICED_NSE"])
        row.update({
            "alpha_error": n50.get("error"),
            "benchmark": "NIFTY 50 price index (Yahoo ^NSEI)",
            "benchmark_window_start": n50.get("first_return_date"),
            "benchmark_window_end": n50.get("last_return_date"),
            "benchmark_sessions": n50.get("n_returns"),
            "benchmark_cagr_pct": as_pct(
                (n50.get("benchmark") or {}).get("annualised_return")),
            "strategy_cagr_aligned_pct": as_pct(
                (n50.get("strategy") or {}).get("annualised_return")),
            "excess_cagr_pct": as_pct(
                (n50.get("relative") or {}).get("tracking_difference")),
            "beta": as_float((n50.get("relative") or {}).get("beta")),
            "alpha_linear_pct": as_pct(
                (n50.get("relative") or {}).get("jensen_alpha_linear")),
            "alpha_geometric_pct": as_pct(
                (n50.get("relative") or {}).get("jensen_alpha_geometric")),
            "tracking_error_pct": as_pct(
                (n50.get("relative") or {}).get("tracking_error")),
            "information_ratio": as_float(
                (n50.get("relative") or {}).get("information_ratio")),
            "up_capture_pct": as_pct(
                (n50.get("relative") or {}).get("up_capture")),
            "down_capture_pct": as_pct(
                (n50.get("relative") or {}).get("down_capture")),
            "r_squared": as_float((n50.get("relative") or {}).get("r_squared")),
            "t_ols": as_float(
                (n50.get("alpha_significance") or {}).get("t_ols")),
            "t_newey_west_5": as_float(
                (n50.get("alpha_significance") or {}).get("t_newey_west_5")),
            "nifty500_alpha_error": n500.get("error"),
            "nifty500_benchmark": "NIFTY 500 spliced NSE",
            "nifty500_window_start": n500.get("first_return_date"),
            "nifty500_window_end": n500.get("last_return_date"),
            "nifty500_sessions": n500.get("n_returns"),
            "nifty500_benchmark_cagr_pct": as_pct(
                (n500.get("benchmark") or {}).get("annualised_return")),
            "nifty500_strategy_cagr_aligned_pct": as_pct(
                (n500.get("strategy") or {}).get("annualised_return")),
            "nifty500_excess_cagr_pct": as_pct(
                (n500.get("relative") or {}).get("tracking_difference")),
            "nifty500_beta": as_float(
                (n500.get("relative") or {}).get("beta")),
            "nifty500_alpha_linear_pct": as_pct(
                (n500.get("relative") or {}).get("jensen_alpha_linear")),
            "nifty500_alpha_geometric_pct": as_pct(
                (n500.get("relative") or {}).get("jensen_alpha_geometric")),
            "nifty500_tracking_error_pct": as_pct(
                (n500.get("relative") or {}).get("tracking_error")),
            "nifty500_information_ratio": as_float(
                (n500.get("relative") or {}).get("information_ratio")),
            "nifty500_up_capture_pct": as_pct(
                (n500.get("relative") or {}).get("up_capture")),
            "nifty500_down_capture_pct": as_pct(
                (n500.get("relative") or {}).get("down_capture")),
            "nifty500_r_squared": as_float(
                (n500.get("relative") or {}).get("r_squared")),
            "nifty500_t_ols": as_float(
                (n500.get("alpha_significance") or {}).get("t_ols")),
            "nifty500_t_newey_west_5": as_float(
                (n500.get("alpha_significance") or {}).get("t_newey_west_5")),
        })
    else:
        row.update({
            "performance_start": None,
            "performance_end": None,
            "sessions": metrics.get("sessions_total"),
            "window_years": None,
            "initial_equity_inr": None,
            "final_equity_inr": None,
            "total_return_pct": None,
            "cagr_pct": None,
            "annualised_volatility_pct": None,
            "sharpe_rf0": None,
            "max_drawdown_pct": None,
        })

    scheduled = row["scheduled_rebalances"]
    row["flag_few_rebalances"] = scheduled < 6
    row["flag_short_window"] = (row["window_years"] is not None
                                and row["window_years"] < 6)
    row["flag_universe_overlap"] = OVERLAP[spec["universe"]]
    row["flag_superseded"] = spec["family"] == "superseded_diagnostic"
    row["price_basis"] = "split/bonus adjusted; dividends excluded"
    row["benchmark_basis"] = "NIFTY 50 price index (Yahoo ^NSEI)"
    row["dividend_treatment"] = ("excluded on both sides; no verified "
                                 "total-return benchmark")
    row["gate_status"] = "research_preview"

    notes = []
    if row["flag_few_rebalances"]:
        notes.append(f"only {scheduled} rebalances")
    if row["flag_short_window"]:
        notes.append(f"window {row['window_years']:.2f} years")
    if spec["calendar_mode"] == "month_end_exact":
        notes.append("month-end calendar held the book when no snapshot existed; superseded")
    if spec["universe"] == "nifty50":
        notes.append("annual membership snapshots only")
    if spec["costs_zero"]:
        notes.append("zero-cost counterfactual, not investable")
    row["notes"] = "; ".join(notes)
    return row


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--match", default=None,
                        help="comma-separated substrings; keep configs whose id "
                             "contains any of them (use for targeted reruns)")
    parser.add_argument("--family", default=None,
                        help="keep only this config family (for supplementary runs)")
    parser.add_argument("--cadence", default=None,
                        help="comma-separated cadences to keep (for reruns)")
    parser.add_argument("--universe", default=None,
                        help="comma-separated universes to keep (for sharding "
                             "by universe)")
    parser.add_argument("--tag", default=None,
                        help="suffix for the shard output file, e.g. nifty50")
    parser.add_argument("--out", default=None,
                        help="explicit output JSONL path (for a correction shard)")
    args = parser.parse_args(argv)
    if args.shards < 1 or not (0 <= args.shard < args.shards):
        raise SystemExit("--shard must satisfy 0 <= shard < shards")

    configs = build_configs()
    if args.match:
        needles = [part.strip() for part in args.match.split(",") if part.strip()]
        configs = [spec for spec in configs
                   if any(needle in spec["id"] for needle in needles)]
        if not configs:
            raise SystemExit("--match selected no configurations")
    if args.family:
        configs = [spec for spec in configs if spec["family"] == args.family]
        if not configs:
            raise SystemExit(f"--family {args.family!r} selected no configurations")
    if args.cadence:
        allowed = {part.strip() for part in args.cadence.split(",") if part.strip()}
        configs = [spec for spec in configs if spec["cadence"] in allowed]
        if not configs:
            raise SystemExit(f"--cadence {args.cadence!r} selected no configurations")
    if args.universe:
        allowed = {part.strip() for part in args.universe.split(",") if part.strip()}
        configs = [spec for spec in configs if spec["universe"] in allowed]
        if not configs:
            raise SystemExit(f"--universe {args.universe!r} selected no configurations")
    if args.limit is not None:
        configs = configs[:args.limit]
    targeted = bool(args.match or args.family or args.cadence
                    or args.universe or args.limit is not None)
    if targeted and not (args.out or args.tag):
        raise SystemExit("targeted reruns require --tag or --out so a main "
                         "shard is not overwritten")
    mine = [spec for index, spec in enumerate(configs)
            if index % args.shards == args.shard]
    suffix = f"-{args.tag}" if args.tag else ""
    out = (Path(args.out) if args.out
           else HERE / f"sweep-shard-{args.shard}{suffix}.jsonl")
    out.write_text("", encoding="utf-8")
    print(f"shard {args.shard}/{args.shards}: {len(mine)} of {len(configs)} configs",
          flush=True)

    context = prepare_context()
    started = time.perf_counter()
    for index, spec in enumerate(mine, start=1):
        row = run_one(spec, context)
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, default=str) + "\n")
        elapsed = time.perf_counter() - started
        rate = elapsed / index
        remaining = rate * (len(mine) - index)
        print(f"[{args.shard}:{index}/{len(mine)}] {spec['id']} "
              f"ret={row.get('total_return_pct')} "
              f"sharpe={row.get('sharpe_rf0')} "
              f"alpha={row.get('alpha_linear_pct')} "
              f"elapsed={elapsed/60:.1f}m eta={remaining/60:.1f}m", flush=True)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
