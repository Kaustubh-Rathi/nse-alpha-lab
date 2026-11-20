"""Point-in-time research universe (explicitly NOT official Nifty).

Monthly eligibility from a bars snapshot, never from a future symbol list:

- Source is the read-only ``cash_equity_candidates`` view in
  ``data/market.duckdb``. That view already contains no INF/IN9 series
  (its series are EQ/BE/BZ/SM/ST); this builder additionally requires
  series ``EQ`` only. It deliberately does NOT filter symbols by
  prefix: names such as INFY are real EQ symbols, so a ``LIKE 'INF%'``
  symbol filter would remove legitimate equities.
- A symbol is eligible at month-end ``T`` iff, using only bars with
  ``date <= T`` (point-in-time / survivorship-aware):
  * it has an EQ bar on ``T`` with valid OHLCV,
  * ``close >= min_price`` and ``volume >= min_volume`` on ``T``,
  * it has at least ``min_days_observed`` EQ bars with ``date <= T``.
  Symbols never observed up to ``T`` are ineligible; symbols that stop
  trading drop out at the next month-end (no forward fill).
- Nifty snapshots (``data/processed/index_membership/``) are an
  optional exact-date cross-check only. Membership is taken from
  ``members_on`` on the snapshot's exact ``as_of`` date; any other
  date raises LookupError (no forward/backward fill), mirroring
  ``src/index_membership.py``. This universe never claims to be
  Nifty membership.

Run: uv run python src/universe.py
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

UNIVERSE_LABEL = "research-defined price/liquidity (not official Nifty)"
SOURCE_VIEW = "cash_equity_candidates"
SOURCE_DB = str(data_root() / "market.duckdb")
SNAPSHOT_DIR = str(data_root() / "processed/index_membership")


@dataclass(frozen=True)
class EligibilityConfig:
    """Eligibility thresholds; defaults mirror backtester.UniverseConfig."""

    min_price: float = 5.0
    min_volume: float = 5000.0  # shares on month-end T
    min_days_observed: int = 63  # cumulative EQ bars with date <= T
    series: str = "EQ"


def validate_config(cfg: EligibilityConfig) -> EligibilityConfig:
    if not (isinstance(cfg.min_price, (int, float)) and cfg.min_price > 0):
        raise ValueError("min_price must be positive")
    if not (isinstance(cfg.min_volume, (int, float)) and cfg.min_volume > 0):
        raise ValueError("min_volume must be positive")
    if not (isinstance(cfg.min_days_observed, int) and cfg.min_days_observed > 0):
        raise ValueError("min_days_observed must be a positive integer")
    if cfg.series != "EQ":
        raise ValueError("only series EQ is supported by this universe definition")
    return cfg


def is_valid_ohlcv(open_: object, high: object, low: object,
                   close: object, volume: object) -> bool:
    """Pure OHLCV sanity: all present, prices positive, high/low consistent.

    Volume must be present and non-negative; zero-volume days are valid
    OHLCV but will still fail the min_volume liquidity test.
    """
    import math

    for value in (open_, high, low, close, volume):
        if value is None:
            return False
        if isinstance(value, float) and math.isnan(value):
            return False
    o, h, lo, c = float(open_), float(high), float(low), float(close)
    v = float(volume)
    if o <= 0 or h <= 0 or lo <= 0 or c <= 0 or v < 0:
        return False
    return h >= lo and h >= o and h >= c and lo <= o and lo <= c


def evaluate_symbol(bar: dict | None, days_observed: int,
                    cfg: EligibilityConfig = EligibilityConfig()) -> dict:
    """Decide eligibility of one symbol at one month-end ``T``.

    ``bar`` is the month-end bar (or None if the symbol is unobserved
    on ``T``); ``days_observed`` counts EQ bars with date <= T only.
    Returns {"eligible": bool, "reasons": [str], "stats": {...}}.
    """
    cfg = validate_config(cfg)
    reasons: list[str] = []
    stats: dict = {"days_observed": int(days_observed)}
    if bar is None:
        return {"eligible": False, "reasons": ["not_observed_on_T"], "stats": stats}
    stats["close"] = bar.get("close")
    stats["volume"] = bar.get("volume")
    if bar.get("series") != cfg.series:
        reasons.append(f"series_{bar.get('series')!r}_not_{cfg.series}")
    if bar.get("valid_ohlcv") is not True:
        reasons.append("invalid_ohlcv_flag")
    # Even when the flag says valid, re-check the numbers when present.
    if None not in (bar.get("open"), bar.get("high"), bar.get("low"),
                    bar.get("close"), bar.get("volume")):
        if not is_valid_ohlcv(bar["open"], bar["high"], bar["low"],
                              bar["close"], bar["volume"]):
            reasons.append("invalid_ohlcv_numbers")
    close, vol = bar.get("close"), bar.get("volume")
    if close is None:
        reasons.append("missing_close")
    elif float(close) < cfg.min_price:
        reasons.append(f"close_{close}_below_min_price_{cfg.min_price}")
    if vol is None:
        reasons.append("missing_volume")
    elif float(vol) < cfg.min_volume:
        reasons.append(f"volume_{vol}_below_min_volume_{cfg.min_volume}")
    if days_observed < cfg.min_days_observed:
        reasons.append(f"days_observed_{days_observed}_below_min_{cfg.min_days_observed}")
    return {"eligible": not reasons, "reasons": reasons, "stats": stats}


def monthly_universe_from_bars(bars: list[dict], month_ends: list[date],
                               cfg: EligibilityConfig = EligibilityConfig()) -> list[dict]:
    """Build eligible observations from bars (dates as date objects).

    Point-in-time: for each month-end T only bars with date <= T are
    consulted. Each observation records the evidence used.
    """
    cfg = validate_config(cfg)
    by_symbol: dict[str, list[dict]] = {}
    for bar in bars:
        by_symbol.setdefault(bar["symbol"], []).append(bar)
    for rows in by_symbol.values():
        rows.sort(key=lambda r: r["date"])
    observations: list[dict] = []
    for month_end in sorted(month_ends):
        for symbol in sorted(by_symbol):
            hist = [r for r in by_symbol[symbol] if r["date"] <= month_end]
            if not hist:
                continue  # never observed up to T: survivorship exclusion
            today = next((r for r in hist if r["date"] == month_end), None)
            eq_hist = [r for r in hist if r.get("series") == cfg.series]
            result = evaluate_symbol(today, len(eq_hist), cfg)
            if result["eligible"]:
                observations.append({
                    "month_end": month_end.isoformat(),
                    "symbol": symbol,
                    "series": cfg.series,
                    "close": today["close"],
                    "volume": today["volume"],
                    "days_observed": result["stats"]["days_observed"],
                    "first_observed": min(r["date"] for r in hist).isoformat(),
                    "eligible": True,
                    "reasons": [],
                })
    return observations


def coverage_from_observations(observations: list[dict],
                               all_month_ends: list | set | None = None) -> dict:
    """Symbols per month and month-to-month turnover for given observations.

    ``all_month_ends`` (dates or ISO strings) enumerates every calendar
    month-end in scope; months absent here would otherwise be missing
    from the report even when zero symbols were eligible (e.g. the
    first months before the min-days history is satisfiable).
    """
    by_month: dict[str, set[str]] = {}
    for obs in observations:
        month_end = obs["month_end"] if isinstance(obs["month_end"], str) else obs["month_end"].isoformat()
        by_month.setdefault(month_end[:7], set()).add(obs["symbol"])
    month_ends: dict[str, str] = {}
    if all_month_ends is not None:
        for item in all_month_ends:
            me = item if isinstance(item, str) else item.isoformat()
            month_ends[me[:7]] = me
            by_month.setdefault(me[:7], set())
    for obs in observations:
        me = obs["month_end"] if isinstance(obs["month_end"], str) else obs["month_end"].isoformat()
        month_ends[me[:7]] = me
    months = sorted(by_month)
    entries: dict[str, dict] = {}
    prev: set[str] | None = None
    for month in months:
        symbols = by_month[month]
        if prev is None:
            entries[month] = {"month": month, "symbols": len(symbols),
                              "entries": len(symbols), "exits": 0,
                              "retained": 0, "retention_rate_vs_prior": None}
        else:
            retained = len(symbols & prev)
            entries[month] = {
                "month": month, "symbols": len(symbols),
                "entries": len(symbols - prev), "exits": len(prev - symbols),
                "retained": retained,
                "retention_rate_vs_prior": round(retained / len(prev), 4) if prev else None,
            }
        prev = symbols
    monthly = [entries[m] for m in months]
    for row in monthly:
        row["month_end"] = month_ends[row["month"]]
    totals = {
        "months": len(monthly),
        "observation_rows": len(observations),
        "distinct_symbols": len({o["symbol"] for o in observations}),
        "avg_symbols_per_month": round(sum(r["symbols"] for r in monthly) / len(monthly), 2) if monthly else 0,
        "avg_entries_per_month": round(sum(r["entries"] for r in monthly) / len(monthly), 2) if monthly else 0,
        "avg_exits_per_month": round(sum(r["exits"] for r in monthly) / len(monthly), 2) if monthly else 0,
        "avg_retention_rate": round(
            sum(r["retention_rate_vs_prior"] for r in monthly if r["retention_rate_vs_prior"] is not None)
            / sum(1 for r in monthly if r["retention_rate_vs_prior"] is not None), 4)
        if any(r["retention_rate_vs_prior"] is not None for r in monthly) else None,
    }
    return {"monthly": monthly, "totals": totals}


def load_snapshot_symbols(membership_dir: Path) -> tuple[dict[tuple[str, str], set[str]], list[dict]]:
    """Load processed snapshot CSVs as exact-date symbol sets (no inference).

    Reads catalog.json when present, else scans ``NIFTY*_*.csv`` files.
    Returns (mapping, inventory). Only symbols are kept; company/ISIN
    columns are not needed for the overlap check.
    """
    mapping: dict[tuple[str, str], set[str]] = {}
    inventory: list[dict] = []
    catalog_path = membership_dir / "catalog.json"
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        rows = [(entry["index_code"], entry["as_of"], membership_dir / Path(entry["snapshot_csv"]).name)
                for entry in catalog]
    else:
        rows = []
        for csv_path in sorted(membership_dir.glob("NIFTY*.csv")):
            index_code, _, stem = csv_path.stem.partition("_")
            rows.append((index_code, stem, csv_path))
    for index_code, as_of, csv_path in rows:
        with open(csv_path, newline="", encoding="utf-8-sig") as stream:
            symbols = {row["symbol"] for row in csv.DictReader(stream) if row.get("symbol")}
        key = (index_code, as_of)
        if key in mapping:
            raise ValueError(f"ambiguous duplicate snapshot {key}")
        mapping[key] = symbols
        inventory.append({"index_code": index_code, "as_of": as_of,
                          "snapshot_symbols": len(symbols), "csv": csv_path.as_posix()})
    return mapping, sorted(inventory, key=lambda r: (r["index_code"], r["as_of"]))


def members_on_exact(snapshots: dict[tuple[str, str], set[str]], index_code: str,
                     as_of: str) -> set[str]:
    """Exact-date lookup only; gaps are unknown, never filled."""
    try:
        return snapshots[(index_code, as_of)]
    except KeyError:
        raise LookupError(f"unknown membership for {index_code} on {as_of}") from None


def overlap_report(eligible_symbols: set[str] | None, snapshot_symbols: set[str]) -> dict:
    if eligible_symbols is None:
        return {"eligible_on_snapshot_date": None, "overlap": None,
                "overlap_pct_of_snapshot": None, "overlap_pct_of_eligible": None,
                "snapshot_not_eligible": None, "eligible_not_in_snapshot": None,
                "reason": "snapshot_date_not_an_observed_session"}
    overlap = len(eligible_symbols & snapshot_symbols)
    return {
        "eligible_on_snapshot_date": len(eligible_symbols),
        "overlap": overlap,
        "overlap_pct_of_snapshot": round(overlap / len(snapshot_symbols), 4) if snapshot_symbols else None,
        "overlap_pct_of_eligible": round(overlap / len(eligible_symbols), 4) if eligible_symbols else None,
        "snapshot_not_eligible": len(snapshot_symbols - eligible_symbols),
        "eligible_not_in_snapshot": len(eligible_symbols - snapshot_symbols),
        "reason": None,
    }


def _month_ends_and_observations(db_path: Path, cfg: EligibilityConfig):
    """Read-only DuckDB pass: month-end bars + cumulative EQ days observed."""
    import duckdb

    con = open_catalog(database=db_path)
    try:
        rows = con.execute("""
            WITH eq AS (
                SELECT date, symbol, close, volume, valid_ohlcv
                FROM cash_equity_candidates
                WHERE series = 'EQ' AND valid_ohlcv = TRUE
            ),
            monthly AS (
                SELECT strftime(date, '%Y-%m') AS ym, symbol, count(*) AS n
                FROM eq GROUP BY 1, 2
            ),
            cum AS (
                SELECT ym, symbol,
                       sum(n) OVER (PARTITION BY symbol ORDER BY ym
                                    ROWS UNBOUNDED PRECEDING) AS cum_days,
                       min(ym) OVER (PARTITION BY symbol) AS first_ym
                FROM monthly
            ),
            mes AS (
                SELECT strftime(date, '%Y-%m') AS ym, max(date) AS month_end
                FROM eq GROUP BY 1
            )
            SELECT mes.ym, mes.month_end, eq.symbol, eq.close, eq.volume,
                   CAST(cum.cum_days AS BIGINT) AS cum_days
            FROM eq
            JOIN mes ON eq.date = mes.month_end
            JOIN cum ON cum.ym = mes.ym AND cum.symbol = eq.symbol
            ORDER BY mes.month_end, eq.symbol
        """).fetchall()
        session_dates = {str(r[0]) for r in con.execute(
            "SELECT DISTINCT date FROM cash_equity_candidates "
            "WHERE series = 'EQ' AND valid_ohlcv = TRUE").fetchall()}
    finally:
        con.close()
    observations = []
    for _ym, month_end, symbol, close, volume, cum_days in rows:
        result = evaluate_symbol(
            {"series": "EQ", "valid_ohlcv": True, "open": close, "high": close,
             "low": close, "close": close, "volume": volume},
            int(cum_days), cfg)
        if result["eligible"]:
            observations.append({"month_end": str(month_end), "symbol": symbol,
                                 "series": "EQ", "close": float(close), "volume": int(volume),
                                 "days_observed": int(cum_days)})
    month_end_set = {str(row[1]) for row in rows}
    return rows, observations, session_dates, month_end_set


def _eligible_on_dates(db_path: Path, dates: list[str], cfg: EligibilityConfig) -> dict[str, set[str] | None]:
    """Eligible symbol sets on arbitrary session dates (same thresholds)."""
    if not dates:
        return {}
    import duckdb

    placeholders = ", ".join("?" for _ in dates)
    con = open_catalog(database=db_path)
    try:
        monthly = con.execute(f"""
            WITH eq AS (
                SELECT date, symbol, close, volume FROM cash_equity_candidates
                WHERE series = 'EQ' AND valid_ohlcv = TRUE
            ),
            monthly_counts AS (
                SELECT strftime(date, '%Y-%m') AS ym, symbol, count(*) AS n
                FROM eq GROUP BY 1, 2
            ),
            day_counts AS (
                SELECT date, symbol, count(*) AS n FROM eq GROUP BY 1, 2
            )
            SELECT eq.date, eq.symbol, eq.close, eq.volume,
                   (SELECT coalesce(sum(m2.n), 0) FROM monthly_counts m2
                    WHERE m2.symbol = eq.symbol
                      AND m2.ym < strftime(eq.date, '%Y-%m'))
                   + coalesce(
                      (SELECT sum(d.n) FROM day_counts d
                       WHERE d.symbol = eq.symbol
                         AND strftime(d.date, '%Y-%m') = strftime(eq.date, '%Y-%m')
                         AND d.date <= eq.date), 0) AS cum_days
            FROM eq WHERE eq.date IN ({placeholders})
        """, list(dates)).fetchall()
    finally:
        con.close()
    grouped: dict[str, set[str]] = {d: set() for d in dates}
    by_date: dict[str, list] = {d: [] for d in dates}
    for day, symbol, close, volume, cum_days in monthly:
        by_date[str(day)].append((symbol, close, volume, cum_days))
    for day, rows in by_date.items():
        for symbol, close, volume, cum_days in rows:
            result = evaluate_symbol(
                {"series": "EQ", "valid_ohlcv": True,
                 "open": close, "high": close, "low": close,
                 "close": close, "volume": volume},
                int(cum_days), cfg)
            if result["eligible"]:
                grouped[day].add(symbol)
    return grouped


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, default=str) + "\n")


def build_universe(db_path: Path, membership_dir: Path, processed_dir: Path,
                   coverage_path: Path, cfg: EligibilityConfig = EligibilityConfig(),
                   sample_rows: int = 1000) -> dict:
    cfg = validate_config(cfg)
    rows, observations, session_dates, month_end_set = _month_ends_and_observations(db_path, cfg)
    coverage = coverage_from_observations(observations, all_month_ends=sorted(month_end_set))
    monthly = coverage["monthly"]
    zero_months = [r["month"] for r in monthly if r["symbols"] == 0]
    yearly: dict[str, dict] = {}
    for year in sorted({r["month"][:4] for r in monthly}):
        vals = [r["symbols"] for r in monthly if r["month"][:4] == year]
        yearly[year] = {"months": len(vals), "avg_symbols": round(sum(vals) / len(vals), 1),
                        "min_symbols": min(vals), "max_symbols": max(vals)}
    snapshots, inventory = load_snapshot_symbols(membership_dir)
    snap_dates_sorted = sorted({as_of for _, as_of in snapshots}, key=str)
    eligible_by_date = _eligible_on_dates(
        db_path, [d for d in snap_dates_sorted if d in session_dates], cfg)
    nifty: list[dict] = []
    for item in inventory:
        self_symbols = members_on_exact(snapshots, item["index_code"], item["as_of"])
        if item["as_of"] in eligible_by_date:
            eligible: set[str] | None = eligible_by_date[item["as_of"]]
        elif item["as_of"] in session_dates:
            eligible = set()
        else:
            eligible = None
        nifty.append({**item, **overlap_report(eligible, self_symbols),
                      "exact_date_only": True,
                      "is_month_end": item["as_of"] in month_end_set})
    # Outputs: full parquet + jsonl sample.
    processed_dir.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd

        df = pd.DataFrame(observations).sort_values(["month_end", "symbol"])
        df.to_parquet(processed_dir / "universe_observations.parquet", index=False)
    except ImportError:
        df = None  # parquet optional; jsonl sample still written
    sample = sorted(observations, key=lambda o: (o["month_end"], o["symbol"]))[:sample_rows]
    write_jsonl(processed_dir / "universe_observations.sample.jsonl", sample)
    summary = {
        "label": UNIVERSE_LABEL,
        "is_official_nifty": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {"min_price": cfg.min_price, "min_volume": cfg.min_volume,
                   "min_days_observed": cfg.min_days_observed, "series": cfg.series},
        "source": {"db": db_path.as_posix(), "read_only": True, "view": SOURCE_VIEW,
                   "month_end_candidate_rows": len(rows),
                   "note": "cash_equity_candidates already contains no INF/IN9 series; "
                           "EQ-only is enforced here. Symbol-prefix filtering "
                           "(e.g. INFY) is deliberately not applied."},
        "date_range": (min(month_end_set, default=None), max(month_end_set, default=None)),
        "first_eligible_month_end": (min((o["month_end"] for o in observations), default=None)),
        "symbols_per_month": monthly,
        "zero_eligible_months": zero_months,
        "yearly_symbols": yearly,
        "totals": coverage["totals"],
        "nifty_overlap": nifty,
        "survivorship": ("Only symbols observed with date <= T are candidates at T. "
                         "No future symbol list, delisting list, or current master is consulted; "
                         "symbols that stop trading exit at the next month-end."),
        "limitations": [
            "Research-defined price/liquidity screen, not official Nifty membership.",
            "Point-in-time: no fill for missing months; absence means not observed, not assumed membership.",
            "Nifty join is exact-date only (26 dated snapshots); all other dates are unknown.",
            "Raw exchange prices: corporate actions are not applied here.",
            "Cash-equity view is already INF/IN9-series-free; EQ symbols such as INFY are retained.",
        ],
        "outputs": {"parquet": (processed_dir / "universe_observations.parquet").as_posix(),
                    "sample_jsonl": (processed_dir / "universe_observations.sample.jsonl").as_posix()},
    }
    write_json(coverage_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=data_root() / "market.duckdb")
    parser.add_argument("--snapshots", type=Path, default=data_root() / "processed/index_membership")
    parser.add_argument("--processed", type=Path, default=data_root() / "processed/universe")
    parser.add_argument("--coverage", type=Path, default=data_root() / "reports/universe-coverage.json")
    parser.add_argument("--min-price", type=float, default=5.0)
    parser.add_argument("--min-volume", type=float, default=5000.0)
    parser.add_argument("--min-days", type=int, default=63)
    args = parser.parse_args()
    cfg = validate_config(EligibilityConfig(min_price=args.min_price,
                                            min_volume=args.min_volume,
                                            min_days_observed=args.min_days))
    summary = build_universe(args.db, args.snapshots, args.processed,
                             args.coverage, cfg)
    print(json.dumps({"label": summary["label"], "totals": summary["totals"],
                      "date_range": summary["date_range"],
                      "nifty_snapshots": len(summary["nifty_overlap"])}))


if __name__ == "__main__":
    main()
