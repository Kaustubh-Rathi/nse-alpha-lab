"""Run the momentum grid over the membership-backed NIFTY 500 window.

This runner is deliberately conservative about what it claims:

* It uses **point-in-time membership** from the retained snapshot chain, so a
  symbol is only eligible on a date where an official snapshot proves it was a
  member. Dates between snapshots are not interpolated.
* It uses the **split/bonus-adjusted** series. It does not apply dividends; the
  adjusted series is not a total-return series and the output says so.
* It refuses to publish performance statistics when the equity curve contains
  any session with an unpriceable holding, because a return computed over an
  unknown equity is not a return.

Usage::

    python -m nse_pipeline.backtest.momentum_grid --start 2013-04-18 --end 2026-09-30
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import pandas as pd

from .cadence import select_rebalance_dates
from .delisting import load_delisting_register
from .exits import ExitPolicy, parse_exit_policy
from .msc400 import load_snapshots as load_msc400_snapshots
from .msc400 import membership_by_date as msc400_membership
from .monthly_universe import coverage as monthly_coverage
from .monthly_universe import load_monthly_sets
from .momentum import BarsFrame, EngineConfig, MomentumRotationEngine
from .rolling import (
    drawdown_durations,
    drawdown_episodes,
    rolling_returns,
    summarise_rolling,
    worst_drawdown_episode,
)
from .security_identity import DEFAULT_EDGES, resolve_identity

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SNAPSHOTS = ROOT / "verification" / "stage1" / "batch8" / "membership" / "snapshots"
DEFAULT_EXTRA_SNAPSHOTS = ROOT / "verification" / "stage1" / "batch9" / "universe" / "snapshots"
DEFAULT_ADJUSTED = ROOT / "data" / "processed" / "adjusted" / "adjusted_bars.parquet"
DEFAULT_OUT = ROOT / "data" / "processed" / "backtest" / "momentum_grid"
DEFAULT_DELISTED = ROOT / "data" / "raw" / "security_master" / "delisted.csv"

SESSIONS_PER_YEAR = 252


# --------------------------------------------------------------------------
# universe
# --------------------------------------------------------------------------

def load_membership(snapshot_dirs) -> dict[date, set[str]]:
    """Map each snapshot's exact date to its symbol set.

    Only files named ``NIFTY500_<ISO date>.csv`` are read. A snapshot date is
    exact: it proves membership on that date and nothing else.
    """
    membership: dict[date, set[str]] = {}
    for directory in snapshot_dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("NIFTY500_*.csv")):
            stem = path.stem
            try:
                as_of = date.fromisoformat(stem.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            frame = pd.read_csv(path, usecols=["symbol"])
            symbols = {str(value).strip().upper()
                       for value in frame["symbol"].dropna()}
            symbols.discard("")
            if symbols:
                membership[as_of] = symbols
    return membership


def membership_as_of(membership: dict[date, set[str]], session: date) -> set[str] | None:
    """The member set proven for ``session``, or ``None`` when unproven.

    No carry-forward: a date without its own snapshot has no proven membership.
    """
    return membership.get(session)


# --------------------------------------------------------------------------
# bars
# --------------------------------------------------------------------------

def load_adjusted_bars(path: Path, start: date, end: date,
                       symbols: set[str] | None = None) -> pd.DataFrame:
    """Split/bonus-adjusted EQ bars for the window.

    ``symbols`` restricts the fetch. Pass ``None`` to load every EQ symbol: a
    membership snapshot names a security by the symbol it carried on the
    snapshot date, but the security's bars may continue under a *different*
    symbol after a rename. Fetching only the snapshot symbols would truncate
    every renamed security at its rename date and make it look delisted.

    Identity is resolved afterwards by :func:`resolve_identity`, which assigns
    the stable ``key``.
    """
    import duckdb

    con = duckdb.connect()
    try:
        if symbols is None:
            frame = con.execute(
                """
                SELECT date, isin, symbol, series, adj_open AS open, adj_high AS high,
                       adj_low AS low, adj_close AS close, volume, valid_ohlcv
                FROM read_parquet(?)
                WHERE date BETWEEN ? AND ?
                  AND series = 'EQ'
                  AND valid_ohlcv
                  AND volume > 0
                  AND adj_close IS NOT NULL AND adj_close > 0
                  AND adj_open IS NOT NULL AND adj_open > 0
                ORDER BY date, symbol
                """,
                [str(path), start, end],
            ).fetchdf()
        else:
            if not symbols:
                raise ValueError("no symbols requested")
            frame = con.execute(
                """
                SELECT date, isin, symbol, series, adj_open AS open, adj_high AS high,
                       adj_low AS low, adj_close AS close, volume, valid_ohlcv
                FROM read_parquet(?)
                WHERE date BETWEEN ? AND ?
                  AND series = 'EQ'
                  AND valid_ohlcv
                  AND volume > 0
                  AND adj_close IS NOT NULL AND adj_close > 0
                  AND adj_open IS NOT NULL AND adj_open > 0
                  AND upper(symbol) IN (SELECT * FROM UNNEST(?))
                ORDER BY date, symbol
                """,
                [str(path), start, end, sorted(symbols)],
            ).fetchdf()
    finally:
        con.close()
    if frame.empty:
        return frame
    frame["symbol"] = frame["symbol"].str.upper()
    return frame


def resolve_membership_to_keys(membership: dict[date, set[str]],
                               resolved_frame: pd.DataFrame) -> dict[date, set[str]]:
    """Translate each snapshot's symbol set into stable security keys.

    A symbol is mapped to the key it carried on the snapshot date. A snapshot
    can fall on a non-trading day, so the nearest preceding observation of that
    symbol is used. When the snapshot's ticker has no observation yet, its
    canonical key is used if that security already had bars under a predecessor
    ticker (a rename), so a tradable security is kept. Only a security whose
    first bar postdates the snapshot is dropped and counted.
    """
    if resolved_frame.empty:
        return {}
    working = resolved_frame[["date", "symbol", "key"]].copy()
    working["date"] = pd.to_datetime(working["date"]).dt.date
    by_symbol = {symbol: group.sort_values("date")
                 for symbol, group in working.groupby("symbol")}
    # First bar seen for each canonical key, across every ticker that resolves to
    # it. A security renamed mid-history trades under a predecessor symbol, so the
    # snapshot's symbol can have no bar before ``as_of`` while the key itself does.
    first_bar_by_key = working.groupby("key")["date"].min().to_dict()

    resolved: dict[date, set[str]] = {}
    unresolved = 0
    for as_of, symbols in membership.items():
        keys: set[str] = set()
        for symbol in symbols:
            group = by_symbol.get(symbol)
            if group is None or group.empty:
                unresolved += 1
                continue
            dates = group["date"].tolist()
            chosen = None
            for index, bar_date in enumerate(dates):
                if bar_date <= as_of:
                    chosen = index
                else:
                    break
            if chosen is not None:
                keys.add(str(group["key"].iloc[chosen]))
                continue
            # This ticker has no bar on or before the snapshot. Fall back to the
            # canonical key: if that security already had history under a
            # predecessor ticker it was tradable and belongs in the universe.
            canonical = None
            for candidate in group["key"].astype(str).unique():
                first_bar = first_bar_by_key.get(candidate)
                if first_bar is not None and first_bar <= as_of:
                    canonical = candidate
                    break
            if canonical is None:
                # The security's first bar postdates the snapshot: mapping it to
                # iloc[0] would leak a future security into the universe.
                unresolved += 1
                continue
            keys.add(canonical)
        if keys:
            resolved[as_of] = keys
    resolved["_unresolved_symbol_dates"] = unresolved  # type: ignore[assignment]
    return resolved


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def _decimal_curve(equity_rows) -> tuple:
    """(date, Decimal equity) pairs, only where equity is known."""
    curve = []
    for row in equity_rows:
        if row["equity"] is None:
            continue
        value = row["date"]
        session = value if isinstance(value, date) else date.fromisoformat(str(value))
        curve.append((session, Decimal(row["equity"])))
    return tuple(curve)


def compute_metrics(equity_rows) -> dict:
    """Performance statistics, or an explicit refusal when the curve is unknown."""
    total_sessions = len(equity_rows)
    unknown = [row for row in equity_rows if row["equity"] is None]
    curve = _decimal_curve(equity_rows)

    result = {
        "sessions_total": total_sessions,
        "sessions_unknown_equity": len(unknown),
        "sessions_known_equity": len(curve),
        "publishable": False,
        "refusal": None,
    }

    if unknown:
        result["refusal"] = (
            f"{len(unknown)} of {total_sessions} sessions have an unpriceable "
            "holding, so the equity curve is unknown on those sessions. "
            "No return, drawdown or Sharpe ratio is computed over an unknown curve.")
        return result

    if len(curve) < 2:
        result["refusal"] = "fewer than 2 known-equity sessions"
        return result

    with localcontext() as ctx:
        ctx.prec = 34
        first_date, first_equity = curve[0]
        last_date, last_equity = curve[-1]
        growth = last_equity / first_equity
        years = Decimal(len(curve)) / Decimal(SESSIONS_PER_YEAR)

        returns = []
        for index in range(1, len(curve)):
            previous = curve[index - 1][1]
            current = curve[index][1]
            returns.append(current / previous - Decimal(1))

        mean_return = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = (sum(((value - mean_return) ** 2 for value in returns), Decimal(0))
                    / Decimal(len(returns) - 1))
        stdev = variance ** Decimal("0.5") if variance > 0 else Decimal(0)

        peak = curve[0][1]
        worst = Decimal(0)
        for _, equity in curve:
            if equity > peak:
                peak = equity
            depth = equity / peak - Decimal(1)
            if depth < worst:
                worst = depth

        episodes = drawdown_episodes(curve)
        worst_episode = worst_drawdown_episode(curve)
        durations = drawdown_durations(curve)

        def serialise(value):
            if isinstance(value, dict):
                return {key: serialise(item) for key, item in value.items()}
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, Decimal):
                return str(value)
            return value

        result.update({
            "publishable": True,
            "start": first_date.isoformat(),
            "end": last_date.isoformat(),
            "initial_equity": str(first_equity),
            "final_equity": str(last_equity),
            "total_return": str(growth - Decimal(1)),
            "cagr": str(growth ** (Decimal(1) / years) - Decimal(1)) if years > 0 else None,
            "annualised_volatility": (str(stdev * Decimal(SESSIONS_PER_YEAR) ** Decimal("0.5"))
                                      if stdev > 0 else None),
            "sharpe_zero_rf": (str(mean_return / stdev * Decimal(SESSIONS_PER_YEAR) ** Decimal("0.5"))
                               if stdev > 0 else None),
            "max_drawdown": str(worst),
            "drawdown_episodes": len(episodes),
            "worst_drawdown": None if worst_episode is None else {
                "peak_date": worst_episode.peak_date.isoformat(),
                "trough_date": worst_episode.trough_date.isoformat(),
                "recovery_date": (worst_episode.recovery_date.isoformat()
                                  if worst_episode.recovery_date else None),
                "depth": str(worst_episode.depth),
                "recovered": worst_episode.recovered,
            },
            "drawdown_durations": serialise(durations),
            "rolling": {
                f"{years_}y": summarise_rolling(rolling_returns(curve, years_))
                for years_ in (1, 2, 5, 10)
            },
        })
    return result


# --------------------------------------------------------------------------
# grid
# --------------------------------------------------------------------------

def build_grid(top_ns, cadences, entry_timings, exit_policies,
               delisting_policies=("strict",),
               delisting_register: dict | None = None) -> list[EngineConfig]:
    configs = []
    for cadence in cadences:
        for top_n in top_ns:
            for entry_timing in entry_timings:
                for exit_policy in exit_policies:
                    for delisting_policy in delisting_policies:
                        configs.append(EngineConfig(
                            cadence=cadence, top_n=top_n, lookback_sessions=63,
                            entry_timing=entry_timing, exit_policy=exit_policy,
                            delisting_policy=delisting_policy,
                            delisting_register=delisting_register,
                            min_days_observed=130))
    return configs


def run_grid(bars_frame: BarsFrame, membership: dict[date, set[str]],
             configs, *, verbose: bool = True) -> list[dict]:
    """Run every config. The universe is restricted to proven membership dates."""
    universe = {session: members for session, members in membership.items()}
    results = []
    for index, config in enumerate(configs, start=1):
        engine = MomentumRotationEngine(config)
        outcome = engine.run(bars_frame, universe=universe)
        metrics = compute_metrics(outcome["equity"])
        results.append({
            "label": outcome["label"],
            "config": outcome["config"],
            "cadence": outcome["cadence"],
            "metrics": metrics,
            "n_trades": len(outcome["trades"]),
            "n_exits": len(outcome["exits"]),
            "n_skips": len(outcome["skips"]),
            "final_positions": outcome["final_positions"],
            "final_cash": outcome["final_cash"],
        })
        if verbose:
            status = "OK" if metrics["publishable"] else "REFUSED"
            print(f"[{index}/{len(configs)}] {outcome['label']}: {status}",
                  flush=True)
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2013-04-18")
    parser.add_argument("--end", default="2026-09-30")
    parser.add_argument("--adjusted", type=Path, default=DEFAULT_ADJUSTED)
    parser.add_argument("--edges", type=Path, default=DEFAULT_EDGES)
    parser.add_argument("--snapshots", type=Path, action="append", default=None)
    parser.add_argument("--universe-source",
                        choices=("nifty500", "msc400", "monthly"),
                        default="nifty500",
                        help=("nifty500: the retained NIFTY 500 snapshot chain. "
                              "msc400: NSE's NIFTY MidSmallcap 400 annual tables "
                              "from the raw index ZIPs. "
                              "monthly: the monthly official constituent sets "
                              "from the universe-construction lane (recommended; "
                              "57 monthly MSC400 sets, 107 monthly NIFTY 500)."))
    parser.add_argument("--monthly-index", default="msc400",
                        help=("with --universe-source monthly: msc400, nifty500, "
                              "midcap150 or smallcap250"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--top-n", type=int, nargs="+", default=[10, 20, 30])
    parser.add_argument("--cadence", nargs="+", default=["monthly", "quarterly", "annual"])
    parser.add_argument("--entry-timing", nargs="+", default=["next_open", "same_close"])
    parser.add_argument("--delisting-policy", nargs="+", default=["strict"])
    parser.add_argument("--delisted-register", type=Path, default=DEFAULT_DELISTED)
    parser.add_argument("--stop-loss", nargs="+", type=str, default=["none", "0.10"])
    parser.add_argument("--take-profit", nargs="+", type=str, default=["none", "0.20"])
    parser.add_argument(
        "--exit-policy", nargs="+", default=None,
        help="explicit overlay list (none, tp0.20, sl0.05, sl0.10+tp0.20); "
             "overrides --stop-loss/--take-profit")
    parser.add_argument("--max-configs", type=int, default=None)
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    snapshot_dirs = args.snapshots or [DEFAULT_SNAPSHOTS, DEFAULT_EXTRA_SNAPSHOTS]
    if args.universe_source == "monthly":
        membership = load_monthly_sets(args.monthly_index)
        if not membership:
            print(json.dumps({"error": "no monthly sets for index",
                              "index": args.monthly_index}), file=sys.stderr)
            return 2
        summary = monthly_coverage(args.monthly_index)
        universe_label = (
            f"{summary['index']} monthly official constituent sets "
            f"({summary['months']} months, {summary['first']}..{summary['last']}, "
            f"{summary['size_min']}-{summary['size_max']} symbols)")
    elif args.universe_source == "msc400":
        msc_snapshots = load_msc400_snapshots()
        if not msc_snapshots:
            print(json.dumps({"error": "no NIFTY MidSmallcap 400 tables found"}),
                  file=sys.stderr)
            return 2
        membership = msc400_membership(msc_snapshots)
        universe_label = ("NIFTY MidSmallcap 400 (official dated constituent "
                          "tables; Indian analogue of Raju (2023) MSC400)")
    else:
        membership = load_membership(snapshot_dirs)
        universe_label = "NIFTY 500 point-in-time membership (exact snapshot dates only)"
    in_window = {d: m for d, m in membership.items() if start <= d <= end}
    if not in_window:
        print(json.dumps({"error": "no membership snapshots in window",
                          "window": [args.start, args.end]}), file=sys.stderr)
        return 2

    all_symbols = set().union(*in_window.values())
    # Load every EQ symbol, not just the snapshot symbols: a renamed security's
    # bars continue under its new symbol, which no snapshot names.
    bars = load_adjusted_bars(args.adjusted, start, end, None)
    if bars.empty:
        print(json.dumps({"error": "no bars for the requested universe/window"}),
              file=sys.stderr)
        return 2

    # One stable key per economic security: symbol changes and within-symbol
    # ISIN changes are joined, so a rename is not mistaken for a delisting.
    identity = resolve_identity(bars, args.edges)
    bars = identity.frame

    resolved = resolve_membership_to_keys(in_window, bars)
    unresolved = resolved.pop("_unresolved_symbol_dates", 0)
    if not resolved:
        print(json.dumps({"error": "no membership date resolved to a security key"}),
              file=sys.stderr)
        return 2
    in_window = resolved
    all_keys = set().union(*in_window.values())

    bars_frame = BarsFrame(bars)

    delisting_register = load_delisting_register(args.delisted_register)
    if "reason_haircut" in args.delisting_policy and not delisting_register:
        print(json.dumps({"error": "reason_haircut needs the delisted register",
                          "path": str(args.delisted_register)}), file=sys.stderr)
        return 2

    if args.exit_policy:
        exit_policies = [parse_exit_policy(spec) for spec in args.exit_policy]
    else:
        exit_policies = []
        for stop in args.stop_loss:
            for target in args.take_profit:
                exit_policies.append(ExitPolicy(
                    stop_loss_pct=(None if stop.lower() == "none" else Decimal(stop)),
                    take_profit_pct=(None if target.lower() == "none" else Decimal(target))))
    # Deduplicate identical policies.
    seen = set()
    unique_policies = []
    for policy in exit_policies:
        key = (policy.stop_loss_pct, policy.take_profit_pct, policy.mode)
        if key in seen:
            continue
        seen.add(key)
        unique_policies.append(policy)

    configs = build_grid(args.top_n, args.cadence, args.entry_timing, unique_policies,
                         args.delisting_policy, delisting_register)
    if args.max_configs:
        configs = configs[: args.max_configs]

    print(f"universe: {len(all_symbols)} symbols -> {len(all_keys)} ISIN keys across "
          f"{len(in_window)} proven membership dates; {len(bars_frame.sessions)} sessions; "
          f"{len(configs)} configs; {unresolved} unresolved symbol-snapshot pairs",
          flush=True)

    results = run_grid(bars_frame, in_window, configs)

    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "window": {"start": args.start, "end": args.end},
        "universe": {
            "label": universe_label,
            "source": args.universe_source,
            "key": "stable security key (symbol edges + within-symbol ISIN changes)",
            "symbols": len(all_symbols),
            "security_keys": len(all_keys),
            "unresolved_symbol_snapshot_pairs": unresolved,
            "membership_dates": len(in_window),
            "first_membership_date": min(in_window).isoformat(),
            "last_membership_date": max(in_window).isoformat(),
            "carry_forward": False,
        },
        "identity": identity.stats,
        "price_basis": {
            "source": str(args.adjusted),
            "adjustment": "split/bonus only (adj_close/adj_open)",
            "total_return": False,
            "note": ("Dividends are NOT applied. This is not a total-return series. "
                     "Results understate total return by the dividend yield."),
        },
        "delisting_policy_note": (
            "'strict' leaves a delisted holding unpriceable so equity becomes "
            "unknown and no return is computed. 'last_close_writeoff' exits it "
            "at its last observed close, which is an assumption about terminal "
            "proceeds, not a fact. 'reason_haircut' applies a Shumway (1997) "
            "reason-conditioned haircut: liquidation -100%, compulsory -30%, "
            "voluntary 0% (reverse book building at a SEBI-floored exit price)."),
        "delisting_register": {
            "path": str(args.delisted_register),
            "symbols": len(delisting_register),
            "by_reason": {
                reason: sum(1 for record in delisting_register.values()
                            if record.get("reason") == reason)
                for reason in ("compulsory", "liquidation", "voluntary", None)
            },
        },
        "sessions": len(bars_frame.sessions),
        "results": results,
    }
    (args.out / "grid-results.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8")

    publishable = [row for row in results if row["metrics"]["publishable"]]
    summary = {
        "configs": len(results),
        "publishable": len(publishable),
        "refused": len(results) - len(publishable),
        "out": str(args.out),
    }
    print(json.dumps(summary, indent=2))
    return 0 if publishable else 1


if __name__ == "__main__":
    raise SystemExit(main())
