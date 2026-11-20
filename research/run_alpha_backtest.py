"""Run the evidence-dated momentum alpha backtest on the available data.

Why this driver exists
----------------------
``momentum_grid`` rebalances on calendar month-ends and only accepts a universe
on an exact snapshot date. The batch8 chain is monthly and mostly month-end, so
that works through 2022-03. The batch9 chain is **65 irregular point anchors**:
after 2022-03 most month-ends have no snapshot. The pre-fix engine liquidated
the book on those dates; the current engine holds it unchanged. Neither is a
rebalance. This driver therefore uses the evidence dates themselves as the
rebalance calendar, through the engine's explicit ``rebalance_dates`` override.

Calendar
--------
* 2013-05-31 .. 2022-03-31: every batch8 snapshot date (monthly).
* 2022-04-07 .. 2026-09-30: every batch9 anchor date (irregular, gaps up to
  several months; documented in the output).
Every rebalance therefore has an exact point-in-time membership set. No date is
interpolated, forward-filled or invented.

Warm-up
-------
Bars are loaded from 2012-06-01 (about 11 months before the first membership
date) so the 63-session momentum lookback is satisfied at the first rebalance.
Performance is measured from the session before the first executed trade, so
the pre-deployment warm-up is excluded and the initial equity is the full cash
balance.

Outputs (all under verification/research/alpha-backtest/):
  strategy-results.json   per-config metrics, equity curves, itemised charges,
                          skip diagnostics
  run.log                 console transcript

Nothing under data/ is written. Research diagnostic only: the validation gate is open.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from nse_pipeline.backtest.delisting import load_delisting_register
from nse_pipeline.backtest.exits import ExitPolicy
from nse_pipeline.backtest.momentum import (
    COST_COMPONENTS,
    BarsFrame,
    EngineConfig,
    MomentumRotationEngine,
)
from nse_pipeline.backtest.momentum_grid import (
    DEFAULT_ADJUSTED,
    DEFAULT_DELISTED,
    DEFAULT_EDGES,
    DEFAULT_EXTRA_SNAPSHOTS,
    DEFAULT_SNAPSHOTS,
    compute_metrics,
    load_adjusted_bars,
    load_membership,
    resolve_membership_to_keys,
)
from nse_pipeline.backtest.security_identity import resolve_identity

HERE = Path(__file__).resolve().parent
OUT = HERE / "strategy-results.json"

WARMUP_START = date(2012, 6, 1)
WINDOW_START = date(2013, 5, 31)
WINDOW_END = date(2026, 9, 30)
SPLICE = date(2022, 4, 1)

BASE = dict(lookback_sessions=63, entry_timing="next_open",
            delisting_policy="last_close_writeoff", min_days_observed=130)


def pct(value) -> str:
    return f"{Decimal(value) * 100:.2f}%"


def summarise_calendar(calendar: list[date]) -> dict:
    gaps = [(b - a).days for a, b in zip(calendar, calendar[1:])]
    monthly = [d for d in calendar if d < SPLICE]
    anchors = [d for d in calendar if d >= SPLICE]
    return {
        "dates": [d.isoformat() for d in calendar],
        "count": len(calendar),
        "first": calendar[0].isoformat(),
        "last": calendar[-1].isoformat(),
        "composition": {
            "batch8_monthly_exact_snapshots": len(monthly),
            "batch9_post_2022_anchors": len(anchors),
        },
        "gap_days": {
            "min": min(gaps) if gaps else None,
            "median": statistics.median(gaps) if gaps else None,
            "max": max(gaps) if gaps else None,
            "gaps_over_45_days": sum(1 for gap in gaps if gap > 45),
            "largest": sorted(gaps, reverse=True)[:10],
        },
    }


def trim_to_deployment(equity: list[dict], trades: list[dict]) -> list[dict]:
    """Start one session before the first fill so initial equity is 100% cash."""
    if not trades:
        return equity
    first_fill = min(trade["date"] for trade in trades)
    index = next((i for i, row in enumerate(equity) if row["date"] == first_fill), None)
    if index is None:
        return [row for row in equity if row["date"] >= first_fill]
    return equity[max(index - 1, 0):]


def cost_audit(config: EngineConfig, trades: list[dict]) -> dict:
    """Itemise every charge actually paid on the recorded trades.

    The engine stores the cash cost per trade. This recomputes the components
    from the same notional and side, asserts they reconcile to the stored cost,
    and aggregates them. A mismatch would mean the run's cost model and the
    audit disagree, so it is a hard error rather than a warning.
    """
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
        "currency": "INR",
        "buy_notional": str(buy_notional),
        "sell_notional": str(sell_notional),
        "turnover": str(turnover),
        "buy_charges": str(buy_charges),
        "sell_charges": str(sell_charges),
        "total_charges": str(total),
        "components": {name: str(value) for name, value in components.items()},
        "charges_bps_of_turnover": (str(total / turnover * Decimal(10_000))
                                    if turnover else None),
        "charges_pct_of_initial_cash": str(total / config.initial_cash),
    }


def run_config(label: str, config: EngineConfig, bars: BarsFrame,
               universe: dict, calendar_note: str) -> dict:
    outcome = MomentumRotationEngine(config).run(bars, universe=universe)
    equity = trim_to_deployment(outcome["equity"], outcome["trades"])
    metrics = compute_metrics(equity)
    charges = cost_audit(config, outcome["trades"])

    skip_counts = Counter((row["side"], row["reason"]) for row in outcome["skips"])
    hold_gaps = [row["date"].isoformat() for row in outcome["skips"]
                 if row["reason"] == "no_proven_universe_hold_unchanged"]
    writeoffs = [row for row in outcome["exits"]
                 if row["reason"] == "delisting_last_close_writeoff"]
    buys = [trade for trade in outcome["trades"] if trade["side"] == "buy"]
    sells = [trade for trade in outcome["trades"] if trade["side"] == "sell"]

    print(f"[{label}] {metrics.get('publishable')} "
          f"total={pct(metrics['total_return']) if metrics.get('publishable') else 'n/a'} "
          f"trades={len(outcome['trades'])} skips={len(outcome['skips'])} "
          f"charges=INR {Decimal(charges['total_charges']):,.0f}", flush=True)

    return {
        "label": label,
        "engine_label": outcome["label"],
        "calendar_note": calendar_note,
        "config": outcome["config"],
        "direction": config.direction,
        "metrics": metrics,
        "charges": charges,
        "cadence": outcome["cadence"],
        "n_trades": len(outcome["trades"]),
        "n_buys": len(buys),
        "n_sells": len(sells),
        "n_exits": len(outcome["exits"]),
        "n_skips": len(outcome["skips"]),
        "skip_reasons": {f"{side}:{reason}": count
                         for (side, reason), count in sorted(skip_counts.items())},
        "unproven_universe_holds": hold_gaps,
        "delisting_writeoffs": [
            {"date": row["date"].isoformat(), "key": row["key"],
             "fill_price": row["fill_price"]} for row in writeoffs],
        "first_trade": (min(trade["date"] for trade in outcome["trades"]).isoformat()
                        if outcome["trades"] else None),
        "last_trade": (max(trade["date"] for trade in outcome["trades"]).isoformat()
                       if outcome["trades"] else None),
        "final_positions": outcome["final_positions"],
        "final_cash": outcome["final_cash"],
        "equity": [{"date": row["date"].isoformat(),
                    "equity": row["equity"],
                    "cash": row["cash"],
                    "positions_value": row["positions_value"],
                    "n_positions": row["n_positions"]} for row in equity],
    }


def main() -> int:
    print(f"loading membership {WINDOW_START} .. {WINDOW_END}", flush=True)
    membership = load_membership([DEFAULT_SNAPSHOTS, DEFAULT_EXTRA_SNAPSHOTS])
    calendar = sorted(d for d in membership if WINDOW_START <= d <= WINDOW_END)
    if not calendar:
        raise SystemExit("no membership dates in window")

    print(f"loading bars {WARMUP_START} .. {WINDOW_END}", flush=True)
    bars = load_adjusted_bars(DEFAULT_ADJUSTED, WARMUP_START, WINDOW_END, None)
    identity = resolve_identity(bars, DEFAULT_EDGES)
    bars = identity.frame
    frame = BarsFrame(bars)
    session_set = set(frame.sessions)

    missing_from_sessions = [d.isoformat() for d in calendar if d not in session_set]
    calendar = [d for d in calendar if d in session_set]
    if not calendar:
        raise SystemExit("no membership date is an observed session")

    print(f"resolving {len(calendar)} snapshot dates to security keys", flush=True)
    resolved = resolve_membership_to_keys(
        {d: membership[d] for d in calendar}, bars)
    unresolved = resolved.pop("_unresolved_symbol_dates", 0)
    empty_dates = [d.isoformat() for d in calendar if not resolved.get(d)]
    calendar = [d for d in calendar if resolved.get(d)]

    sizes = [len(resolved[d]) for d in calendar]
    universe_meta = {
        "label": "NIFTY 500 exact-date membership, evidence-dated rebalance calendar",
        "snapshot_dates_total": len(membership),
        "rebalance_dates": len(calendar),
        "members_per_date": {"min": min(sizes), "median": statistics.median(sizes),
                             "max": max(sizes)},
        "unresolved_symbol_snapshot_pairs": unresolved,
        "dates_with_empty_universe": empty_dates,
        "dates_not_observed_sessions": missing_from_sessions,
    }

    register = load_delisting_register(DEFAULT_DELISTED)

    calendar_tuple = tuple(calendar)
    configs = [
        ("evidence-N10-L63-next_open-no_stop", EngineConfig(
            cadence="monthly", rebalance_dates=calendar_tuple, top_n=10, **BASE),
         resolved, "explicit evidence dates (monthly to 2022-03, point anchors after)"),
        ("evidence-N10-L63-next_open-sl0.10", EngineConfig(
            cadence="monthly", rebalance_dates=calendar_tuple, top_n=10,
            exit_policy=ExitPolicy(stop_loss_pct=Decimal("0.10")), **BASE),
         resolved, "explicit evidence dates; 10% stop evaluated on prior close"),
        ("evidence-N20-L63-next_open-no_stop", EngineConfig(
            cadence="monthly", rebalance_dates=calendar_tuple, top_n=20, **BASE),
         resolved, "explicit evidence dates; breadth sensitivity"),
        ("evidence-N20-L63-next_open-no_stop-bottom", EngineConfig(
            cadence="monthly", rebalance_dates=calendar_tuple, top_n=20,
            direction="bottom", **BASE),
         resolved, "explicit evidence dates; bottom-20 reversal counterfactual"),
        ("evidence-N10-L63-next_open-no_stop-gross_of_costs", EngineConfig(
            cadence="monthly", rebalance_dates=calendar_tuple, top_n=10,
            brokerage_bps=Decimal("0"), stt_bps_buy=Decimal("0"),
            stt_bps_sell=Decimal("0"), stamp_bps=Decimal("0"),
            exchange_bps=Decimal("0"), gst_on_brokerage_pct=Decimal("0"),
            slippage_bps=Decimal("0"), **BASE),
         resolved, "primary with every charge set to zero (counterfactual)"),
        ("as-committed-month-end-exact-match", EngineConfig(
            cadence="monthly", top_n=10, **BASE),
         resolved, "month-end cadence, exact-date universe only (superseded diagnostic)"),
    ]

    results = []
    for label, config, universe, note in configs:
        results.append(run_config(label, config, frame, universe, note))

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "research_diagnostic_p0_closed",
        "window": {
            "bar_start": WARMUP_START.isoformat(),
            "bar_end": WINDOW_END.isoformat(),
            "first_rebalance": calendar[0].isoformat(),
            "last_rebalance": calendar[-1].isoformat(),
            "splice": SPLICE.isoformat(),
            "performance_start_rule": "session before the first executed fill",
        },
        "calendar": summarise_calendar(calendar),
        "universe": universe_meta,
        "identity": identity.stats,
        "price_basis": {
            "source": str(DEFAULT_ADJUSTED),
            "adjustment": "split/bonus only (adj_close/adj_open)",
            "total_return": False,
            "note": "dividends not applied; benchmark comparison uses price indices",
        },
        "costs": results[0]["config"]["costs_bps"],
        "delisting_policy": "last_close_writeoff",
        "delisting_register_symbols": len(register),
        "configs": results,
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
