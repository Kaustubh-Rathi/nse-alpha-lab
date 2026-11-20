"""Total-return backtest mode: split-only execution, dividend-accrual marking.

Scheme-aware callers must pass ``successor_entries`` to ``run``. Dividend /
scheme holding composition is not implemented: the explicit capability guard
stops affected holdings before accrual, execution or marking. Runs that omit
the ledger are labelled scheme-unchecked in ``quality``.

Why a separate module (``engine.py`` is not modified)
----------------------------------------------------
``RotationBacktester`` runs on a single price system.  There are two
naive ways to bolt dividends onto it, and both are wrong for cash
accounting:

* **Execution at ``adj_close_total``** -- spends cash at a price that
  embeds *future* dividend factors (``adj_close_total(d) = adj_close(d) *
  prod_{ex>d} f_ex``).  The dividend cash is not in the account at the
  ex-date (it is paid later, here gross with no pay-date lag), so a fill
  at the total-return price is lookahead on cash and understates the
  cash actually spent.
* **Marking the same share count at ``adj_close_total``** -- the
  retrospective total level is the split level times a per-holding
  constant ``prod_{ex>entry} f_ex < 1``.  Entry equity instantly drops
  by that constant (cash is not scaled, positions are), final equity is
  depressed by dividends *after* the window, and a sale realises the
  dividend as a jump at sell time rather than at the ex-date.  Daily
  *returns* of ``adj_close_total`` are correct; its *level* is not a
  cash-backed mark.

Design used here
----------------
* **Execution / costs / circuit / eligibility:** split-only adjusted
  prices (``adj_open`` / ``adj_close`` / ``adj_previous_close``), exactly
  as the ``--adjusted`` engine run.  Fills, notionals and the cash
  ledger therefore never see a dividend factor.
* **Returns / signal:** ``adj_close_total`` momentum (63-session return
  on the total series), so a stock is not ranked as a -5% loser on its
  ex-date drop.
* **Marking:** gross ex-date reinvestment implemented as *share
  accrual* -- a holding of ``S`` shares becomes ``S / f_ex`` shares on
  the ex-date (``f_ex`` from ``dividends.py``), valued at the split
  close.  This is algebraically the total-return return applied to a
  cash-backed holding: value is continuous across the ex-date, sells
  realise the accrued shares at the split price, cash stays the split
  ledger, and there is no entry dip and no post-window depression
  (events are filtered to ``ex_date <= window end``; ``cum`` at the end
  is 1 by construction).

Two comparisons are reported per window:

* ``split_only`` -- ``RotationBacktester`` on adjusted bars (reproduces
  ``backtester-validation.json``).
* ``total_return`` -- :class:`TotalReturnBacktester` above (total
  signal + accrual marking, split execution).
* ``total_return_marking_only`` -- an accrual overlay on the *split*
  run's own trades (identical trades/cash; dividends accrued while
  held and realised at sell).  The gap to ``split_only`` is the pure
  dividend contribution; the gap to ``total_return`` is the
  signal/sizing effect of ranking on total returns.
* ``naive_total_marking`` (diagnostic only) -- same split trades marked
  at ``adj_close_total`` levels, to quantify the level distortion
  described above.  Not a candidate equity curve.

Outputs (owned by this module only) are written under
``data/processed/backtest_total/<window>/`` and summarised in
``data/reports/backtester-total-return.{json,md}``.
``market.duckdb`` is never opened here; bars come from the adjusted
parquet via ``engine.load_adjusted_bars`` and dividend factors are
reused from ``dividend_events.jsonl`` (already computed read-only by
``dividends.py`` -- no rule is re-derived here).
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from nse_pipeline.paths import data_root

import numpy as np
import pandas as pd

from nse_pipeline.actions.dividends import cumulative_dividend_factors
from nse_pipeline.backtest.engine import (
    CircuitConfig,
    CostConfig,
    RotationBacktester,
    UniverseConfig,
    annual_stats,
    executable_reason,
    load_adjusted_bars,
    load_identity_edges,
    month_end_dates,
    momentum_signal,
)

ADJUSTED_PARQUET_DEFAULT = str(data_root() / "processed/adjusted/adjusted_bars.parquet")
IDENTITY_EDGES_DEFAULT = str(data_root() / "processed/identity/identity_edges.jsonl")
DIVIDEND_EVENTS_DEFAULT = str(data_root() / "processed/actions_dividends/dividend_events.jsonl")


# ---------------------------------------------------------------------------
# Event / bar preparation
# ---------------------------------------------------------------------------

def load_dividend_events(path: str | Path = DIVIDEND_EVENTS_DEFAULT,
                         start: str | date | None = None,
                         end: str | date | None = None,
                         series: str = "EQ") -> pd.DataFrame:
    """Included dividend factors from ``dividend_events.jsonl``.

    Keeps only ``included_in_factor`` rows with a finite factor in
    ``(0, 1)``, optionally restricted to one ``series`` and to
    ``start < ex_date <= end`` (exclusive start: a dividend on the first
    bar has no in-window return to adjust; inclusive end: the cumulative
    factor at the window end is 1).  Pure file read, no DB.
    """
    rows: list[dict] = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if not rec.get("included_in_factor"):
                continue
            if series and str(rec.get("series")) != series:
                continue
            try:
                factor = float(rec["dividend_factor"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (np.isfinite(factor) and 0.0 < factor < 1.0):
                continue
            ex = date.fromisoformat(str(rec["ex_date"])[:10])
            rows.append({
                "symbol": str(rec["symbol"]),
                "series": str(rec.get("series", series)),
                "ex_date": ex,
                "dividend_factor": factor,
                "dividend_per_share": float(rec.get("dividend_per_share", np.nan)),
            })
    events = pd.DataFrame(rows)
    if events.empty:
        return events
    if start is not None:
        start_d = pd.to_datetime(start).date()
        events = events[events["ex_date"] > start_d]
    if end is not None:
        end_d = pd.to_datetime(end).date()
        events = events[events["ex_date"] <= end_d]
    return events.sort_values(["symbol", "series", "ex_date"], kind="stable").reset_index(drop=True)


def attach_total_close(bars: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Add ``total_close`` (``adj_close_total``) to adjusted bars.

    ``bars`` must carry ``date/symbol/series/adj_close`` (as returned by
    :func:`engine.load_adjusted_bars`, which maps ``adj_*`` onto the
    engine columns but keeps the ``adj_*`` columns).  Uses
    ``dividends.cumulative_dividend_factors`` per (symbol, series) line
    with the same strictly-before-ex-date semantics as
    ``dividends.apply_total_return`` -- implemented groupwise here
    because ``apply_total_return`` builds a full-length mask per line
    and is quadratic on the full bar universe.  Split columns, ``open``
    and ``previous_close`` are untouched, so execution stays split-only.
    """
    frame = bars.copy()
    frame["dividend_factor_cum"] = 1.0
    frame["n_future_dividends"] = 0
    if events is not None and len(events):
        ev = events.copy()
        ev["ex_date"] = pd.to_datetime(ev["ex_date"]).dt.normalize()
        by_line: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
        for (symbol, series), group in ev.groupby(["symbol", "series"], sort=False):
            by_line[(symbol, series)] = (
                group["ex_date"].to_numpy(dtype="datetime64[ns]"),
                group["dividend_factor"].to_numpy(dtype=np.float64),
            )
        dates_all = pd.to_datetime(frame["date"]).dt.normalize()
        cum = np.ones(len(frame), dtype=np.float64)
        n_future = np.zeros(len(frame), dtype=np.int64)
        line_groups = frame.groupby([frame["symbol"].astype(str), frame["series"].astype(str)], sort=False).indices
        for line, idx in line_groups.items():
            if line not in by_line:
                continue
            ex, fac = by_line[line]
            order = np.argsort(ex)
            ordered_ex = ex[order]
            dates = dates_all.iloc[idx].to_numpy(dtype="datetime64[ns]")
            cum[idx] = cumulative_dividend_factors(dates, ordered_ex, fac[order])
            pos = np.searchsorted(ordered_ex, dates, side="right")
            n_future[idx] = (len(ex) - pos).astype(np.int64)
        frame["dividend_factor_cum"] = cum
        frame["n_future_dividends"] = n_future
    frame["adj_close_total"] = frame["adj_close"].astype(np.float64) * frame["dividend_factor_cum"].to_numpy(dtype=np.float64)
    frame["total_close"] = frame["adj_close_total"].astype(np.float64)
    # apply_total_return normalises dates to Timestamps; the engine and
    # load_adjusted_bars use date objects -- keep one convention.
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _key_to_line(bars: pd.DataFrame) -> dict[str, tuple[str, str]]:
    """First ``(symbol, series)`` per ``key`` (events match on that pair)."""
    if not len(bars):
        return {}
    seen = bars[["key", "symbol", "series"]].drop_duplicates(subset=["key"])
    return {str(r.key): (str(r.symbol), str(r.series)) for r in seen.itertuples(index=False)}


def events_by_key(bars: pd.DataFrame, events: pd.DataFrame) -> dict[str, list[tuple[date, float]]]:
    """Map each ``key`` to its sorted ``(ex_date, factor)`` dividend list."""
    if events is None or not len(events):
        return {}
    key_line = _key_to_line(bars)
    by_line: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for r in events.itertuples(index=False):
        by_line.setdefault((str(r.symbol), str(r.series)), []).append((r.ex_date, float(r.dividend_factor)))
    out: dict[str, list[tuple[date, float]]] = {}
    for key, line in key_line.items():
        if line in by_line:
            out[key] = sorted(by_line[line])
    return out


# ---------------------------------------------------------------------------
# Total-return (accrual) backtester
# ---------------------------------------------------------------------------

@dataclass
class TotalReturnConfig:
    use_total_signal: bool = True  # rank momentum on total_close vs split close


class TotalReturnBacktester(RotationBacktester):
    """Rotation engine with split execution and dividend share accrual.

    Identical control flow to :class:`RotationBacktester`; the only
    differences are (a) momentum ranks on ``total_close`` when
    ``use_total_signal`` (falls back to ``close``), (b) on each
    ex-date held shares grow by ``1/f_ex`` before that session's
    executions, and (c) marking values accrued shares at split closes.
    With no dividend events it is exactly the split engine (tested).
    """

    def __init__(self, costs=None, universe=None, circuit=None,
                 total_config: TotalReturnConfig | None = None):
        super().__init__(costs, universe, circuit)
        self.total_config = total_config or TotalReturnConfig()

    def build_eligible(self, bars: pd.DataFrame, signal_date: date) -> pd.DataFrame:
        # min_price on contemporaneous raw close when available, never total level.
        return super().build_eligible(bars, signal_date)

    def rank_momentum(self, bars: pd.DataFrame, signal_date: date,
                      lookback_sessions: int = 63, _cache: dict | None = None,
                      price_col: str | None = None) -> pd.DataFrame:
        col = price_col or ("total_close" if (self.total_config.use_total_signal
                                              and "total_close" in bars.columns) else "close")
        day_map = _cache["day_map"] if _cache else None
        sessions = _cache["sessions"] if _cache else sorted(bars["date"].unique())
        idx = sessions.index(signal_date) if signal_date in sessions else None
        if idx is None or idx < lookback_sessions:
            return pd.DataFrame()
        anchor = sessions[idx - lookback_sessions]
        if day_map is not None:
            cur = day_map[signal_date].copy()
            old = day_map[anchor][["key", col]].rename(columns={col: "anchor_close"})
        else:
            cur = bars[bars["date"] == signal_date].copy()
            old = bars[bars["date"] == anchor][["key", col]].rename(columns={col: "anchor_close"})
        eligible = self.build_eligible(cur, signal_date)
        if _cache is not None and "history_counts" in _cache:
            counts = _cache["history_counts"].loc[eligible.index]
            eligible = eligible[counts >= self.universe.min_days_observed]
        else:
            counts = bars[bars["date"] <= signal_date].groupby("key")["date"].nunique()
            eligible = eligible[eligible["key"].map(counts) >= self.universe.min_days_observed]
        m = eligible.merge(old, on="key", how="inner", validate="one_to_one")
        m = m[(m["anchor_close"] > 0) & (m[col] > 0) & (m["valid_ohlcv"] == True)]  # noqa: E712
        m["mom_return"] = m[col] / m["anchor_close"] - 1
        return m.sort_values(["mom_return", "key"], ascending=[False, True], kind="stable")

    def run(self, bars: pd.DataFrame, rebalance_dates: list[date],
            signal_fn, initial_cash: float = 1_000_000.0,
            max_positions: int = 20, debug: bool = False,
            dividend_events: pd.DataFrame | None = None, *,
            successor_entries: list[dict] | None = None) -> dict:
        """Accrue dividends; optionally enforce the scheme capability guard.

        Passing ``successor_entries`` enables fail-closed held-exposure checks.
        Dividend-adjusted shares cannot yet compose with raw scheme ratios,
        pending children or settlement cash. Such exposure raises before any
        accrual, sale or EOD mark; universe exclusion does not waive the check.
        Omitting the ledger preserves legacy runs, labelled scheme-unchecked.
        """
        from nse_pipeline.backtest.successor import assess_exposure, SchemeSafetyError

        scheme_entries = list(successor_entries or [])
        bars = attach_total_close(bars, dividend_events) if "total_close" not in bars.columns else bars.copy()
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        if bars.duplicated(["date", "key"]).any():
            raise ValueError("Duplicate security/date bars: resolve identity or series ambiguity before execution")
        day_map: dict[date, pd.DataFrame] = {d: g for d, g in bars.groupby("date")}
        sessions = sorted(day_map.keys())
        closes: dict[date, dict[str, float]] = {
            d: dict(zip(g["key"].tolist(), g["close"].tolist())) for d, g in day_map.items()
        }
        counts = bars.sort_values(["key", "date"]).groupby("key").cumcount() + 1
        cache = {"day_map": day_map, "sessions": sessions, "history_counts": counts}

        # ex-date accrual buckets: first session on/after each ex-date.
        by_key = events_by_key(bars, dividend_events)
        accrual_on: dict[date, list[tuple[str, float, date]]] = {}
        for key, evs in by_key.items():
            for ex, factor in evs:
                pos = bisect.bisect_left(sessions, ex)
                if pos < len(sessions):
                    accrual_on.setdefault(sessions[pos], []).append((key, factor, ex))

        cash = float(initial_cash)
        positions: dict[str, dict] = {}
        trades: list[dict] = []
        skips: list[dict] = []
        dividends_ledger: list[dict] = []
        last_price: dict[str, float] = {}

        def check_scheme_exposure(d):
            if successor_entries is None:
                return
            assessment = assess_exposure(scheme_entries, positions, d,
                                         price_basis="split_adjusted", return_mode="total_return")
            if not assessment["safe"]:
                assessment.update({"on": d, "cash": cash,
                                   "holdings": [dict(p, key=k) for k, p in positions.items()]})
                raise SchemeSafetyError(assessment)

        def split_value(d: date) -> float:
            return sum(p["split_shares"] * closes.get(d, {}).get(k, last_price.get(k, 0.0))
                       for k, p in positions.items())

        def accrued_value(d: date) -> float:
            return sum(p["shares"] * closes.get(d, {}).get(k, last_price.get(k, 0.0))
                       for k, p in positions.items())

        sess_idx = {s: i for i, s in enumerate(sessions)}
        exec_map: dict[date, date | None] = {}
        for sd in rebalance_dates:
            i = sess_idx.get(sd)
            if i is not None:
                exec_map[sd] = sessions[i + 1] if i + 1 < len(sessions) else None
        pending: dict[date, list[tuple[str, date]]] = {}
        for sd in rebalance_dates:
            ed = exec_map.get(sd)
            if ed is None:
                continue
            try:
                targets = list(signal_fn(self, bars, sd, cache))[:max_positions]
            except TypeError:
                targets = list(signal_fn(self, bars, sd))[:max_positions]
            pending.setdefault(ed, []).extend([(k, sd) for k in targets])

        equity_rows: list[dict] = []
        for d in sessions:
            check_scheme_exposure(d)
            lookup = ({row.key: row._asdict() for row in day_map[d].itertuples(index=False)}
                      if d in pending or d in accrual_on else {})

            def opening_value(k):
                row = lookup.get(k)
                opn = row.get("open") if row else None
                return float(opn) if opn is not None and pd.notna(opn) and float(opn) > 0 else last_price.get(k, 0.0)

            # 1) ex-date accrual on pre-open holdings (holders of the
            #    previous close are entitled; buyers today are not).
            for key, factor, ex in accrual_on.get(d, []):
                pos = positions.get(key)
                if pos is None or pos["shares"] <= 0:
                    continue
                before = pos["shares"]
                pos["shares"] = before / factor
                px = opening_value(key)
                dividends_ledger.append({
                    "date": d, "ex_date": ex, "key": key,
                    "symbol": pos.get("symbol", key), "dividend_factor": factor,
                    "shares_before": before, "shares_after": pos["shares"],
                    "implied_dividend_value": (pos["shares"] - before) * px,
                })
            # 2) executions at split open (engine logic, accrued shares).
            if d in pending:
                items = list(dict.fromkeys(pending[d]))
                targets = [k for k, _ in items]
                sig_of = {k: sd for k, sd in items}
                for k in list(positions.keys()):
                    if k in targets:
                        continue
                    bar = lookup.get(k)
                    if bar is None:
                        skips.append({"date": d, "key": k, "side": "sell",
                                      "reason": "no_bar_delisted_or_suspended"})
                        positions[k]["flag"] = "delisted_or_suspended"
                        continue
                    ok, reason = executable_reason(bar, self.circuit)
                    if not ok:
                        skips.append({"date": d, "key": k, "side": "sell", "reason": reason})
                        positions[k]["flag"] = f"suspended_{reason}"
                        continue
                    fill = float(bar["open"])
                    shares = positions[k]["shares"]
                    notional = shares * fill
                    cost = self.costs.cost_for(notional, "sell")
                    cash += notional - cost
                    trades.append({"date": d, "signal_date": None, "key": k,
                                   "symbol": bar.get("symbol", k), "side": "sell",
                                   "fill_price": fill, "shares": shares,
                                   "notional": notional, "cost": cost})
                    del positions[k]
                eq = cash + sum(p["shares"] * opening_value(k) for k, p in positions.items())
                n = len(targets)
                if n:
                    per = eq / n
                    for k in targets:
                        held = positions[k]["shares"] * opening_value(k) \
                            if k in positions else 0.0
                        need = max(per - held, 0.0)
                        if need <= 0:
                            continue
                        bar = lookup.get(k)
                        if bar is None:
                            skips.append({"date": d, "key": k, "side": "buy", "reason": "no_bar_at_execution"})
                            continue
                        ok, reason = executable_reason(bar, self.circuit)
                        if not ok:
                            skips.append({"date": d, "key": k, "side": "buy", "reason": reason})
                            continue
                        fill = float(bar["open"])
                        shares = int(min(need, cash) // fill)
                        while shares > 0:
                            notional = shares * fill
                            cost = self.costs.cost_for(notional, "buy")
                            if notional + cost <= cash:
                                break
                            shares -= 1
                        if shares <= 0:
                            skips.append({"date": d, "key": k, "side": "buy", "reason": "insufficient_cash"})
                            continue
                        notional = shares * fill
                        cost = self.costs.cost_for(notional, "buy")
                        cash -= notional + cost
                        trades.append({"date": d, "signal_date": sig_of.get(k), "key": k,
                                       "symbol": bar.get("symbol", k), "side": "buy",
                                       "fill_price": fill, "shares": shares,
                                       "notional": notional, "cost": cost})
                        if k in positions:
                            old = positions[k]
                            tot_split = old["split_shares"] + shares
                            old["avg_entry"] = (old["split_shares"] * old["avg_entry"]
                                                + shares * fill) / tot_split
                            old["split_shares"] = tot_split
                            old["shares"] += shares
                            old["last_seen"] = d
                            old["flag"] = "active"
                        else:
                            positions[k] = {"shares": float(shares), "split_shares": float(shares),
                                            "avg_entry": fill, "last_price": fill, "last_seen": d,
                                            "flag": "active", "symbol": bar.get("symbol", k)}
            # 3) EOD marking at split closes on accrued shares.
            check_scheme_exposure(d)
            for k, px in closes.get(d, {}).items():
                last_price[k] = px
            day_closes = closes.get(d, {})
            for k, p in positions.items():
                if k not in day_closes:
                    p["flag"] = "delisted_or_suspended"
                else:
                    if p["flag"].startswith("delisted"):
                        p["flag"] = "active"
                    p["last_seen"] = d
            acc_v = accrued_value(d)
            split_v = split_value(d)
            equity_rows.append({"date": d, "cash": cash,
                                "positions_value": acc_v,
                                "positions_value_split_basis": split_v,
                                "dividend_value_unrealized": acc_v - split_v,
                                "equity": cash + acc_v,
                                "equity_split_basis": cash + split_v,
                                "n_positions": len(positions)})

        equity_df = pd.DataFrame(equity_rows)
        trades_df = pd.DataFrame(trades)
        skips_df = pd.DataFrame(skips)
        ledger = [{"key": k, "shares": p["shares"], "split_basis_shares": p["split_shares"],
                   "avg_entry": p["avg_entry"], "last_price": last_price.get(k, p["avg_entry"]),
                   "last_seen": p.get("last_seen"), "flag": p.get("flag", "active")}
                  for k, p in positions.items()]
        stats = annual_stats(equity_df.set_index("date")["equity"]) if not equity_df.empty \
            else {"total_return": 0, "cagr": 0, "ann_vol": 0, "max_drawdown": 0, "n_sessions": 0}
        stats_split = annual_stats(equity_df.set_index("date")["equity_split_basis"]) \
            if not equity_df.empty else dict(stats)
        for s in (stats, stats_split):
            s["n_trades"] = int(len(trades_df))
            s["n_skips"] = int(len(skips_df))
            s["final_cash"] = float(cash)
            s["final_equity"] = float(equity_df["equity"].iloc[-1]) if not equity_df.empty else cash
        stats_split["final_equity"] = float(equity_df["equity_split_basis"].iloc[-1]) \
            if not equity_df.empty else cash
        return {"equity": equity_df, "trades": trades_df, "skips": skips_df,
                "positions": ledger, "stats": stats, "stats_split_basis": stats_split,
                "dividends": pd.DataFrame(dividends_ledger),
                "quality": {"scheme_guard_enabled": successor_entries is not None,
                            "dividend_scheme_composition_supported": False},
                "config": {"costs": self.costs.__dict__, "universe": self.universe.__dict__,
                           "circuit": self.circuit.__dict__,
                           "use_total_signal": self.total_config.use_total_signal}}


def total_momentum_signal(top_n: int = 20, lookback_sessions: int = 63):
    def fn(engine, bars, signal_date, cache=None):
        ranked = engine.rank_momentum(bars, signal_date, lookback_sessions, cache,
                                      price_col="total_close")
        if ranked.empty:
            return []
        return ranked["key"].tolist()[:top_n]
    return fn


# ---------------------------------------------------------------------------
# Accrual overlay on an existing (split) run's trades
# ---------------------------------------------------------------------------

def reconstruct_equities(trades: pd.DataFrame, equity_split: pd.DataFrame,
                         bars: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Re-mark a split run's trades with dividend accrual (marking-only).

    Trades and cash are taken as given; held shares accrue by ``1/f_ex``
    on ex-dates and the accrued increment is realised at the sell fill.
    Also returns ``equity_naive`` (same split shares at ``total_close``
    levels) as a distortion diagnostic.  ``bars`` should already carry
    ``total_close`` (see :func:`attach_total_close`).
    """
    equity_split = equity_split.copy()
    equity_split["date"] = pd.to_datetime(equity_split["date"]).dt.date
    sessions = equity_split["date"].tolist()
    sess_pos = {d: i for i, d in enumerate(sessions)}
    n = len(sessions)
    cash_arr = equity_split["cash"].to_numpy(dtype=np.float64)

    if trades is None or not len(trades):
        out = equity_split[["date", "cash"]].copy()
        out["positions_value_split"] = equity_split["positions_value"].to_numpy(dtype=np.float64)
        out["positions_value_total"] = out["positions_value_split"]
        out["positions_value_naive"] = out["positions_value_split"]
        out["realized_dividend_cash"] = 0.0
        out["equity_total"] = equity_split["equity"].to_numpy(dtype=np.float64)
        out["equity_naive"] = out["equity_total"]
        return out

    trades = trades.copy()
    trades["date"] = pd.to_datetime(trades["date"]).dt.date
    if "total_close" not in bars.columns:
        bars = attach_total_close(bars, events)
    key_line = _key_to_line(bars)
    by_key = events_by_key(bars, events)

    # Per-key chronological simulation -> state records (date, split, accrued, realized).
    states: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
    for key, tgroup in trades.groupby("key", sort=False):
        combined: list[tuple[date, int, dict]] = []
        for ex, factor in by_key.get(str(key), []):
            combined.append((ex, 0, {"kind": "ex", "factor": factor}))
        for r in tgroup.itertuples(index=False):
            combined.append((r.date, 1, {"kind": str(r.side), "shares": float(r.shares),
                                         "fill": float(r.fill_price)}))
        combined.sort(key=lambda x: (x[0], x[1]))
        rec_dates, rec_split, rec_accrued, rec_real = [], [], [], []
        split_sh, accrued_sh, realized = 0.0, 0.0, 0.0
        for d, _, payload in combined:
            if payload["kind"] == "ex":
                if accrued_sh > 0:
                    accrued_sh /= payload["factor"]
            elif payload["kind"] == "buy":
                split_sh += payload["shares"]
                accrued_sh += payload["shares"]
            elif payload["kind"] == "sell":
                realized += max(accrued_sh - split_sh, 0.0) * payload["fill"]
                split_sh, accrued_sh = 0.0, 0.0
            rec_dates.append(d)
            rec_split.append(split_sh)
            rec_accrued.append(accrued_sh)
            rec_real.append(realized)
        states[str(key)] = (np.array(rec_dates, dtype="datetime64[D]"),
                            np.array(rec_split), np.array(rec_accrued), np.array(rec_real))

    # Split/total last prices for traded keys only.
    traded = set(states)
    sub = bars[bars["key"].astype(str).isin(traded)]
    sub = sub.copy()
    sub["date"] = pd.to_datetime(sub["date"]).dt.date
    split_by_date: dict[date, dict[str, float]] = {}
    total_by_date: dict[date, dict[str, float]] = {}
    for d, g in sub.groupby("date"):
        split_by_date[d] = dict(zip(g["key"].astype(str).tolist(), g["close"].astype(float).tolist()))
        total_by_date[d] = dict(zip(g["key"].astype(str).tolist(), g["total_close"].astype(float).tolist()))

    split_val = np.zeros(n)
    accrued_val = np.zeros(n)
    naive_val = np.zeros(n)
    realized_arr = np.zeros(n)
    last_split: dict[str, float] = {}
    last_total: dict[str, float] = {}
    # Per-key state index arrays (state at each session).
    per_key_state: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    sess_dates64 = np.array(sessions, dtype="datetime64[D]")
    for key, (rd, rs, ra, rr) in states.items():
        idx = np.searchsorted(rd, sess_dates64, side="right") - 1
        s_arr = np.where(idx >= 0, rs[np.clip(idx, 0, len(rs) - 1)] if len(rs) else 0.0, 0.0)
        a_arr = np.where(idx >= 0, ra[np.clip(idx, 0, len(ra) - 1)] if len(ra) else 0.0, 0.0)
        r_arr = np.where(idx >= 0, rr[np.clip(idx, 0, len(rr) - 1)] if len(rr) else 0.0, 0.0)
        per_key_state[key] = (s_arr, a_arr, r_arr)

    for i, d in enumerate(sessions):
        for k, px in split_by_date.get(d, {}).items():
            last_split[k] = px
        for k, px in total_by_date.get(d, {}).items():
            last_total[k] = px
        sv = av = nv = rv = 0.0
        for key, (s_arr, a_arr, r_arr) in per_key_state.items():
            s_sh, a_sh = float(s_arr[i]), float(a_arr[i])
            if s_sh == 0.0 and a_sh == 0.0:
                rv += float(r_arr[i])
                continue
            sv += s_sh * last_split.get(key, 0.0)
            av += a_sh * last_split.get(key, 0.0)
            nv += s_sh * last_total.get(key, 0.0)
            rv += float(r_arr[i])
        split_val[i], accrued_val[i], naive_val[i], realized_arr[i] = sv, av, nv, rv

    out = pd.DataFrame({
        "date": sessions,
        "cash": cash_arr,
        "positions_value_split": split_val,
        "positions_value_total": accrued_val,
        "positions_value_naive": naive_val,
        "realized_dividend_cash": realized_arr,
    })
    out["equity_split"] = out["cash"] + out["positions_value_split"]
    out["equity_total"] = out["cash"] + out["positions_value_total"] + out["realized_dividend_cash"]
    out["equity_naive"] = out["cash"] + out["positions_value_naive"] + out["realized_dividend_cash"]
    return out


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_series(split_eq: pd.Series, total_eq: pd.Series) -> dict:
    """Dividend-contribution metrics between two equity series."""
    s = annual_stats(split_eq)
    t = annual_stats(total_eq)
    final_s = float(split_eq.dropna().iloc[-1]) if len(split_eq.dropna()) else 0.0
    final_t = float(total_eq.dropna().iloc[-1]) if len(total_eq.dropna()) else 0.0
    gain_s = final_s - float(split_eq.dropna().iloc[0]) if len(split_eq.dropna()) else 0.0
    return {
        "split_only": s,
        "total_return": t,
        "dividend_contribution": {
            "final_equity_diff": final_t - final_s,
            "total_return_pp_diff": t["total_return"] - s["total_return"],
            "cagr_pp_diff": t["cagr"] - s["cagr"],
            "ann_vol_pp_diff": t["ann_vol"] - s["ann_vol"],
            "max_drawdown_pp_diff": t["max_drawdown"] - s["max_drawdown"],
            "final_equity_ratio": (final_t / final_s) if final_s else None,
            "share_of_split_gain": ((final_t - final_s) / gain_s) if gain_s else None,
        },
    }


def _window_label(start: str, end: str) -> str:
    return f"{start[:4]}_{end[:4]}"


def run_window(start: str, end: str, out_root: str | Path = data_root() / "processed/backtest_total",
               initial_cash: float = 10_000_000.0, top_n: int = 20,
               adjusted_parquet: str = ADJUSTED_PARQUET_DEFAULT,
               identity_path: str = IDENTITY_EDGES_DEFAULT,
               events_path: str = DIVIDEND_EVENTS_DEFAULT) -> dict:
    out = Path(out_root) / _window_label(start, end)
    out.mkdir(parents=True, exist_ok=True)
    edges = load_identity_edges(identity_path) if Path(identity_path).exists() else None
    bars_split = load_adjusted_bars(adjusted_parquet, start, end, ("EQ",), edges)
    events = load_dividend_events(events_path, start, end)
    bars_total = attach_total_close(bars_split, events)

    sessions = sorted(pd.to_datetime(bars_split["date"]).dt.date.unique().tolist())
    rebalances = [d for d in month_end_dates(sessions) if sessions.index(d) >= 63]

    split_engine = RotationBacktester()
    split_res = split_engine.run(bars_split, rebalances, momentum_signal(top_n=top_n), initial_cash)

    total_engine = TotalReturnBacktester()
    total_res = total_engine.run(bars_total, rebalances, total_momentum_signal(top_n=top_n),
                                 initial_cash, dividend_events=events)

    overlay = reconstruct_equities(split_res["trades"], split_res["equity"], bars_total, events)
    overlay_stats = annual_stats(overlay.set_index("date")["equity_total"])
    overlay_stats["final_equity"] = float(overlay["equity_total"].iloc[-1])
    overlay_stats["n_trades"] = int(len(split_res["trades"]))
    naive_stats = annual_stats(overlay.set_index("date")["equity_naive"])
    naive_stats["final_equity"] = float(overlay["equity_naive"].iloc[-1])
    naive_stats["n_trades"] = int(len(split_res["trades"]))

    curves = pd.DataFrame({
        "date": overlay["date"],
        "cash_split": overlay["cash"],
        "equity_split_only": split_res["equity"]["equity"].to_numpy(dtype=np.float64),
        "equity_total_marking_only": overlay["equity_total"].to_numpy(dtype=np.float64),
        "equity_total_return_full": total_res["equity"]["equity"].to_numpy(dtype=np.float64),
        "equity_naive_total_marking": overlay["equity_naive"].to_numpy(dtype=np.float64),
        "realized_dividend_cash": overlay["realized_dividend_cash"].to_numpy(dtype=np.float64),
        "dividend_value_unrealized_full": total_res["equity"]["dividend_value_unrealized"].to_numpy(dtype=np.float64),
    })
    curves.to_csv(out / "equity_curves.csv", index=False)
    split_res["trades"].to_csv(out / "trades_split_only.csv", index=False)
    total_res["trades"].to_csv(out / "trades_total_return.csv", index=False)
    if not total_res["dividends"].empty:
        total_res["dividends"].to_csv(out / "dividend_reinvestments.csv", index=False)

    comparison = {
        "window": {"start": start, "end": end, "sessions": len(sessions),
                   "rebalances": [d.isoformat() for d in rebalances],
                   "bars_rows": int(len(bars_split)),
                   "dividend_events_in_window": int(len(events)),
                   "dividend_symbols_in_window": int(events["symbol"].nunique()) if len(events) else 0,
                   "initial_cash": initial_cash, "top_n": top_n},
        "split_only": split_res["stats"],
        "total_return_full": total_res["stats"],
        "total_return_marking_only": overlay_stats,
        "naive_total_marking_diagnostic": naive_stats,
        "dividend_contribution_full_vs_split": compare_series(
            split_res["equity"].set_index("date")["equity"],
            total_res["equity"].set_index("date")["equity"])["dividend_contribution"],
        "dividend_contribution_marking_only_vs_split": compare_series(
            split_res["equity"].set_index("date")["equity"],
            overlay.set_index("date")["equity_total"])["dividend_contribution"],
        "dividend_reinvestments_full": int(len(total_res["dividends"])),
        "dividend_reinvested_value_full": float(total_res["dividends"]["implied_dividend_value"].sum())
        if not total_res["dividends"].empty else 0.0,
        "outputs": {"equity_curves": str(out / "equity_curves.csv"),
                    "trades_split_only": str(out / "trades_split_only.csv"),
                    "trades_total_return": str(out / "trades_total_return.csv")},
    }
    (out / "comparison.json").write_text(json.dumps(comparison, indent=2, default=str) + "\n", encoding="utf-8")
    return comparison


def run_all(windows: list[tuple[str, str]] | None = None,
            out_root: str | Path = data_root() / "processed/backtest_total",
            report_root: str | Path = data_root() / "reports") -> dict:
    windows = windows or [("2021-01-01", "2022-12-31"), ("2015-01-01", "2026-11-06")]
    results = [run_window(s, e, out_root) for s, e in windows]
    report_root = Path(report_root)
    report_root.mkdir(parents=True, exist_ok=True)
    summary = {
        "module": "src/nse_pipeline/backtest/total_return.py",
        "method": {
            "execution": "split-only adj_open/adj_close (adj_*) for fills, costs, circuit, min_price",
            "signal": "63-session momentum on adj_close_total (split+dividend) in total_return_full",
            "marking": "gross ex-date reinvestment as share accrual S -> S/f_ex valued at split close; "
                       "cash ledger is split-only (no dividend cash is spent before it exists)",
            "events": "dividend_events.jsonl included_in_factor, EQ, start<ex_date<=end; "
                      "factors reused from dividends.py, not re-derived",
            "naive_diagnostic": "same split trades marked at adj_close_total levels (level distortion only)",
        },
        "windows": results,
        "limitations": [
            "Gross only: no TDS/slab tax, no pay-date lag (accrual at ex-date, spendable only via share value), no reinvestment slippage/fees.",
            "Dividend coverage inherits dividends.py: multi-amount triage rows are parsed separately by specials.py (non-overlapping only, NSE 2% rule); true no-amount bare dividends stay excluded with disclosed sensitivity, so total return understates true TR for affected lines (see data/reports/dividends-coverage.json for the current bare registry).",
            "Accrual is applied to holdings at the first session on/after the ex-date; a holding sold into a suspended session keeps accrued shares until a tradable bar reappears (same exit rule as engine.py).",
            "Total momentum uses retrospective adj_close_total within the window; its returns are point-in-time correct (future factors cancel in ratios), but the level must not be used for execution -- this module never does.",
            "min_price=5 applies on the split scale in both modes (unlike a naive total-close engine).",
            "Research-defined price/liquidity universe, not official Nifty; circuit flat 20%; delisted holdings marked at last split price.",
        ],
        "files_owned": [
            "src/nse_pipeline/backtest/total_return.py",
            "tests/test_total_return.py",
            "data/processed/backtest_total/",
            "data/reports/backtester-total-return.json",
            "data/reports/backtester-total-return.md",
        ],
    }
    (report_root / "backtester-total-return.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    (report_root / "backtester-total-return.md").write_text(
        _render_markdown(summary), encoding="utf-8")
    return summary


def _pct(x) -> str:
    return f"{x * 100:+.2f}%" if x is not None and np.isfinite(x) else "n/a"


def _render_markdown(summary: dict) -> str:
    lines = [
        "# Total-return backtest (split execution, dividend accrual)",
        "",
        "Split-only adjusted prices drive execution, costs, circuit and the cash "
        "ledger. `adj_close_total` drives momentum returns; marking reinvests "
        "gross dividends at the ex-date as share accrual `S -> S/f_ex` valued at "
        "the split close -- the cash-backed equivalent of total-return marking. "
        "Marking the same shares at `adj_close_total` levels (naive) is shown "
        "only as a distortion diagnostic.",
        "",
    ]
    for w in summary["windows"]:
        win = w["window"]
        lines += [
            f"## {win['start']} to {win['end']}",
            "",
            f"Bars {win['bars_rows']:,}, sessions {win['sessions']}, "
            f"dividend events in window {win['dividend_events_in_window']} "
            f"({win['dividend_symbols_in_window']} symbols). Initial cash "
            f"Rs {win['initial_cash']:,.0f}, top-{win['top_n']} monthly 3M momentum.",
            "",
            "| mode | total return | CAGR | ann vol | max DD | final equity | trades |",
            "|---|---|---|---|---|---|---|",
        ]
        for label, key in [("split-only", "split_only"),
                           ("total-return (full)", "total_return_full"),
                           ("total-return (marking only)", "total_return_marking_only"),
                           ("naive total marking (diagnostic)", "naive_total_marking_diagnostic")]:
            s = w[key]
            lines.append(
                f"| {label} | {_pct(s['total_return'])} | {_pct(s['cagr'])} | "
                f"{_pct(s['ann_vol'])} | {_pct(s['max_drawdown'])} | "
                f"Rs {s['final_equity']:,.0f} | {s['n_trades']} |")
        dc = w["dividend_contribution_full_vs_split"]
        dm = w["dividend_contribution_marking_only_vs_split"]
        lines += [
            "",
            f"Dividend contribution (full vs split): total return {_pct(dc['total_return_pp_diff'])}, "
            f"CAGR {_pct(dc['cagr_pp_diff'])}, vol {_pct(dc['ann_vol_pp_diff'])}, "
            f"maxDD {_pct(dc['max_drawdown_pp_diff'])}, final equity "
            f"Rs {dc['final_equity_diff']:+,.0f} "
            f"({dc['final_equity_ratio']:.4f}x split; "
            f"{(dc['share_of_split_gain'] * 100 if dc['share_of_split_gain'] is not None else float('nan')):.1f}% of split gain).",
            f"Dividend contribution (marking only vs split): total return {_pct(dm['total_return_pp_diff'])}, "
            f"CAGR {_pct(dm['cagr_pp_diff'])}, final equity Rs {dm['final_equity_diff']:+,.0f}.",
            f"Dividend reinvestments in full run: {w['dividend_reinvestments_full']} "
            f"(implied reinvested value Rs {w['dividend_reinvested_value_full']:,.0f}).",
            "",
            f"Equity curves: `{w['outputs']['equity_curves']}`",
            "",
        ]
    lines += ["## Limitations", ""]
    for lim in summary["limitations"]:
        lines.append(f"- {lim}")
    lines += ["", "## Files", ""]
    for f in summary["files_owned"]:
        lines.append(f"- `{f}`")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Total-return backtest comparison")
    p.add_argument("--out", default=str(data_root() / "processed/backtest_total"))
    p.add_argument("--reports", default=str(data_root() / "reports"))
    p.add_argument("--window", action="append", choices=["2021-2022", "2015-2026"],
                   help="restrict to one window (repeatable)")
    a = p.parse_args()
    windows = [("2021-01-01", "2022-12-31"), ("2015-01-01", "2026-11-06")]
    if a.window:
        windows = [w for w in windows if _window_label(*w) in a.window]
    print(json.dumps(run_all(windows, a.out, a.reports), indent=2, default=str))
