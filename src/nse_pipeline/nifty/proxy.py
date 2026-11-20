"""Validated Nifty-PROXY (NOT official Nifty membership).

This module builds a defensible *proxy* when official point-in-time purchase
fails. It never presents carried-forward membership as official history.

Two distinct constructs are produced, both labelled PROXY:

1. **Snapshot-anchored interval carry** (`build_observations`): the latest
   dated snapshot on/before each month-end is carried forward only as
   *labelled interval uncertainty*. Every row records its anchor
   ``as_of``, the next snapshot date, distance-to-anchor, and a
   ``certainty`` flag. The anchor is exact on its own ``as_of`` date only;
   neighbouring months are explicitly uncertain. Dates before the first
   snapshot have no anchor and yield no member rows (unknown, not filled).

2. **Rule-based replay** (`replay_at_dates` + `validate_replay`): a
   point-in-time top-N ranking using only bars with ``date <= T``.
   Free-float market-cap is proxied by trailing-6-month average daily
   turnover (``close * volume``); impact cost is proxied by
   ``1 / sqrt(avg_turnover)`` (higher turnover -> lower proxy cost) plus a
   trading-frequency screen. This approximates the documented methodology
   (free-float rank, impact-cost/liquidity screens, buffers) but cannot
   recover shares outstanding, IWF, order-book depth, or intraday
   observations, so it is evaluated (precision/recall per snapshot) rather
   than trusted.

A third, **hybrid** design (snapshot anchors + press-release changelog)
is specified in ``data/reports/nifty-proxy-research.md`` and encoded as
``apply_changelog``: dated, effective-date-aware add/remove events applied
between anchors. Until a complete, reconciled changelog is acquired and
checkpoint-reconciled against snapshots, the hybrid is a *proposal*, not a
generated history.

Ground truth: the 26 dated official snapshots in
``data/processed/index_membership/`` (Nifty50 2008-10-31..2026-10-30,
Nifty500 2015-10-30..2021-10-29), loaded with the same exact-date
loader as :mod:`universe`.

Outputs (CLI defaults):
``data/processed/nifty_proxy/proxy_observations.parquet`` + sample JSONL,
``data/reports/nifty-proxy-coverage.json``,
``data/reports/nifty-proxy-research.md`` (human note; the CLI writes the
JSON + parquet; the Markdown is maintained as a dated report).

Run: uv run python src/nifty_proxy.py
Tests: uv run python -m unittest tests.test_nifty_proxy -v
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

PROXY_LABEL = "Nifty-proxy (PROXY - NOT official Nifty membership)"
DB_PATH = data_root() / "market.duckdb"
SNAPSHOT_DIR = data_root() / "processed/index_membership"
PROCESSED_DIR = data_root() / "processed/nifty_proxy"
COVERAGE_PATH = data_root() / "reports/nifty-proxy-coverage.json"

# --- Rule-replay methodology-era breaks (verified in index-membership
# research; weighting vs membership noted there, never conflated here). ---
METHODOLOGY_ERAS = [
    {"era": "pre-2009 full-cap weighting", "from": "2000-01-01", "to": "2009-06-25",
     "notes": "S&P CNX Nifty full-cap weighting before free-float transition."},
    {"era": "post-2009 free-float weighting", "from": "2009-06-26", "to": "2016-03-31",
     "notes": "2009-03-24 notice (CNX Nifty free-float, eff. 2009-06-26); "
              "2010-09-09 notice (CNX 500 free-float, eff. 2010-10-11). "
              "Weighting transitions; membership-selection changes not assumed."},
    {"era": "post-2016 F&O/DVR/structure", "from": "2016-04-01", "to": "2026-11-07",
     "notes": "2016-02-22 notices: F&O eligibility for Nifty 50, DVR classes, "
              "revised broad-market/Nifty 500 criteria eff. 2016-04-01."},
]


@dataclass(frozen=True)
class ProxyConfig:
    """Replay thresholds. Defaults proxy contemporary Nifty 50 screens with
    the Rs. 10 crore impact-cost observation scaled down by two orders of
    magnitude to a daily-turnover proxy (see docstring)."""

    target_counts: dict = field(default_factory=lambda: {"NIFTY50": 50, "NIFTY500": 500})
    min_price: float = 5.0
    min_volume_on_T: float = 5000.0
    min_days_observed: int = 63
    frequency_window_sessions: int = 126  # ~6 trading months
    min_trading_frequency: float = 0.90  # fraction of market sessions traded
    min_avg_daily_turnover: float = 10_000_000.0  # Rs. 1 crore/day (proxy screen)
    series: str = "EQ"

    def validate(self) -> "ProxyConfig":
        if set(self.target_counts) != {"NIFTY50", "NIFTY500"}:
            raise ValueError("target_counts must cover NIFTY50 and NIFTY500")
        for k, v in self.target_counts.items():
            if not isinstance(v, int) or v <= 0:
                raise ValueError(f"target count for {k} must be a positive integer")
        if not self.min_price > 0:
            raise ValueError("min_price must be positive")
        if not self.min_volume_on_T > 0:
            raise ValueError("min_volume_on_T must be positive")
        if not (isinstance(self.min_days_observed, int) and self.min_days_observed > 0):
            raise ValueError("min_days_observed must be a positive integer")
        if not (0 < self.min_trading_frequency <= 1.0):
            raise ValueError("min_trading_frequency must be in (0, 1]")
        if not self.min_avg_daily_turnover > 0:
            raise ValueError("min_avg_daily_turnover must be positive")
        if self.series != "EQ":
            raise ValueError("only series EQ is supported")
        return self


# ------------------------------------------------------------------ snapshots

def load_snapshot_symbols(membership_dir: Path):
    """Exact-date symbol sets + inventory. Same semantics as universe.py."""
    mapping: dict[tuple[str, str], set[str]] = {}
    inventory: list[dict] = []
    catalog_path = membership_dir / "catalog.json"
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        rows = [(e["index_code"], e["as_of"], membership_dir / Path(e["snapshot_csv"]).name)
                for e in catalog]
    else:
        rows = []
        for csv_path in sorted(membership_dir.glob("NIFTY*.csv")):
            index_code, _, stem = csv_path.stem.partition("_")
            rows.append((index_code, stem, csv_path))
    for index_code, as_of, csv_path in rows:
        with open(csv_path, newline="", encoding="utf-8-sig") as stream:
            symbols = {r["symbol"] for r in csv.DictReader(stream) if r.get("symbol")}
        key = (index_code, as_of)
        if key in mapping:
            raise ValueError(f"ambiguous duplicate snapshot {key}")
        mapping[key] = symbols
        inventory.append({"index_code": index_code, "as_of": as_of,
                          "snapshot_symbols": len(symbols)})
    return mapping, sorted(inventory, key=lambda r: (r["index_code"], r["as_of"]))


# --------------------------------------------------- 1. interval-carry (proxy)

def certainty_for(month_end: str, anchor: str | None, next_snapshot: str | None) -> str:
    """Label how much the carried set can be trusted at ``month_end``."""
    if anchor is None:
        return "unknown_before_first_snapshot"
    if month_end == anchor:
        return "exact_anchor"
    if next_snapshot is not None:
        return "carried_forward_inside_interval"
    return "carried_forward_beyond_last_snapshot"


def anchor_for(month_end: str, dates: list[str]) -> str | None:
    """Latest snapshot date <= month_end (LOCF anchor), else None."""
    best = None
    for d in dates:
        if d <= month_end:
            best = d
        else:
            break
    return best


def next_snapshot_for(month_end: str, dates: list[str]) -> str | None:
    for d in dates:
        if d > month_end:
            return d
    return None


def _days(delta: str) -> int | None:
    return None  # placeholder replaced below by real parsing


def days_between(a: str, b: str) -> int:
    """|b - a| in days (ISO dates)."""
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    return abs((db - da).days)


def build_observations(month_ends: list[str],
                       snapshots: dict[tuple[str, str], set[str]],
                       index_codes=("NIFTY50", "NIFTY500")) -> list[dict]:
    """Snapshot-anchored PROXY observations, one row per (month, index, symbol).

    Only months with an anchor (``month_end >= first snapshot``) emit rows.
    Before the first snapshot: unknown (no rows), never back-filled.
    """
    by_index: dict[str, list[str]] = {}
    for code in index_codes:
        by_index[code] = sorted(as_of for (c, as_of) in snapshots if c == code)
    observations: list[dict] = []
    for month_end in sorted(month_ends):
        for code in index_codes:
            dates = by_index[code]
            anchor = anchor_for(month_end, dates)
            nxt = next_snapshot_for(month_end, dates)
            if anchor is None:
                continue  # unknown interval: emit nothing, not a guess
            certainty = certainty_for(month_end, anchor, nxt)
            anchor_symbols = snapshots[(code, anchor)]
            gap = days_between(anchor, month_end)
            horizon = days_between(month_end, nxt) if nxt else None
            for symbol in sorted(anchor_symbols):
                observations.append({
                    "month_end": month_end,
                    "index_code": code,
                    "symbol": symbol,
                    "anchor_as_of": anchor,
                    "next_snapshot_as_of": nxt,
                    "certainty": certainty,
                    "days_from_anchor": gap,
                    "days_to_next_snapshot": horizon,
                    "is_proxy": True,
                    "is_official": False,
                    "label": PROXY_LABEL,
                })
    return observations


def coverage_from_observations(observations: list[dict],
                               month_ends: list[str]) -> dict:
    """Per-index monthly counts + uncertainty composition."""
    by_month_index: dict[tuple[str, str], list[dict]] = {}
    for obs in observations:
        by_month_index.setdefault((obs["month_end"], obs["index_code"]), []).append(obs)
    monthly = []
    for month_end in sorted(month_ends):
        for code in ("NIFTY50", "NIFTY500"):
            rows = by_month_index.get((month_end, code), [])
            certainty = rows[0]["certainty"] if rows else "unknown_before_first_snapshot"
            monthly.append({
                "month_end": month_end,
                "index_code": code,
                "proxy_symbols": len({r["symbol"] for r in rows}),
                "certainty": certainty,
                "anchor_as_of": rows[0]["anchor_as_of"] if rows else None,
                "next_snapshot_as_of": rows[0]["next_snapshot_as_of"] if rows else None,
                "is_proxy": True,
                "is_official": False,
            })
    totals = {
        "observation_rows": len(observations),
        "distinct_symbols": len({o["symbol"] for o in observations}),
        "distinct_index_symbol_pairs": len({(o["index_code"], o["symbol"]) for o in observations}),
        "months_total": len(month_ends),
    }
    return {"monthly": monthly, "totals": totals}


# ------------------------------------------------------ 2. rule-based replay

def impact_cost_proxy(avg_turnover: float) -> float:
    """Cost proxy: strictly decreasing in turnover. Not a measured impact.

    Returns ~ ``K / sqrt(turnover)`` with ``K`` chosen so Rs. 1 crore/day
    maps near 50 bps (the current 0.50% headline scale). Documented as a
    *rank-equivalent*: only ordering matters for a top-N replay.
    """
    if avg_turnover <= 0:
        return float("inf")
    return 10000.0 / math.sqrt(avg_turnover)


def free_float_proxy(avg_turnover: float, trading_frequency: float) -> float:
    """Free-float market-cap proxy: turnover scaled by trading consistency.

    True free-float needs shares outstanding and IWF (unavailable in
    OHLCV). Turnover is the best OHLCV-observable correlate of float size
    and liquidity; frequency down-weights sporadically traded names.
    """
    return avg_turnover * trading_frequency


def rank_proxy(candidates: list[dict]) -> list[dict]:
    """Sort candidates by free-float proxy desc, symbol asc (stable)."""
    return sorted(candidates,
                  key=lambda c: (-c["free_float_proxy"], c["symbol"]))


def select_proxy(candidates: list[dict], cfg: ProxyConfig, index_code: str) -> set[str]:
    """Apply screens then take top-N (never relabelled as official)."""
    cfg.validate()
    passing = [c for c in candidates
               if c["close_ok"] and c["volume_ok"] and c["history_ok"]
               and c["frequency_ok"] and c["turnover_ok"]]
    ranked = rank_proxy(passing)
    return {c["symbol"] for c in ranked[: cfg.target_counts[index_code]]}


def precision_recall(predicted: set[str], truth: set[str]) -> dict:
    tp = len(predicted & truth)
    fp = len(predicted - truth)
    fn = len(truth - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4)}


def validate_replay(predicted_by_key: dict[tuple[str, str], set[str]],
                    snapshots: dict[tuple[str, str], set[str]]) -> list[dict]:
    rows = []
    for key in sorted(snapshots):
        truth = snapshots[key]
        pred = predicted_by_key.get(key)
        if pred is None:
            rows.append({"index_code": key[0], "as_of": key[1],
                         "ground_truth": len(truth), "predicted": None,
                         "precision": None, "recall": None, "f1": None,
                         "reason": "no_candidate_panel"})
            continue
        scores = precision_recall(pred, truth)
        rows.append({"index_code": key[0], "as_of": key[1],
                     "ground_truth": len(truth),
                     "predicted": len(pred),
                     "overlap": scores["tp"],
                     "precision": scores["precision"],
                     "recall": scores["recall"], "f1": scores["f1"],
                     "fp": scores["fp"], "fn": scores["fn"],
                     "is_proxy": True, "is_official": False})
    return rows


def replay_at_dates(db_path: Path, eval_dates: list[str], cfg: ProxyConfig) -> dict[tuple[str, str], set[str]]:
    """Point-in-time candidate panels at snapshot dates + top-N selection.

    Uses ONLY bars with ``date <= T``. Trailing window is the last
    ``frequency_window_sessions`` *market* sessions on/before T (EQ
    valid-OHLCV sessions), not calendar days.
    """
    import duckdb

    cfg.validate()
    con = open_catalog(database=db_path)
    try:
        # Market session list (EQ valid bars) once.
        sessions: list[str] = [str(r[0]) for r in con.execute(
            "SELECT DISTINCT date FROM cash_equity_candidates "
            "WHERE series='EQ' AND valid_ohlcv=TRUE ORDER BY 1").fetchall()]
        session_index = {s: i for i, s in enumerate(sessions)}
        out: dict[tuple[str, str], set[str]] = {}
        for T in eval_dates:
            if T not in session_index:
                continue
            idx = session_index[T]
            start = sessions[max(0, idx - cfg.frequency_window_sessions + 1)]
            # Bars on T: close/volume + cumulative EQ days observed <= T.
            on_T = con.execute(
                "SELECT symbol, close, volume FROM cash_equity_candidates "
                "WHERE date=? AND series='EQ' AND valid_ohlcv=TRUE", [T]).fetchall()
            # Cumulative days observed <= T per symbol.
            cum_rows = con.execute(
                "SELECT symbol, COUNT(*) FROM cash_equity_candidates "
                "WHERE date<=? AND series='EQ' AND valid_ohlcv=TRUE "
                "GROUP BY 1", [T]).fetchall()
            cum_days = {str(s): int(n) for s, n in cum_rows}
            # Trailing window: per-symbol avg turnover + sessions traded.
            win = con.execute(
                "SELECT symbol, COUNT(*) AS traded_sessions, "
                "AVG(close*volume) AS avg_turnover "
                "FROM cash_equity_candidates "
                "WHERE date BETWEEN ? AND ? AND series='EQ' AND valid_ohlcv=TRUE "
                "AND close IS NOT NULL AND volume IS NOT NULL "
                "GROUP BY 1", [start, T]).fetchall()
            window_sessions = idx - session_index[start] + 1
            stats = {str(s): (int(n), float(avg) if avg is not None else 0.0)
                     for s, n, avg in win}
            candidates = []
            for symbol, close, volume in on_T:
                symbol = str(symbol)
                traded, avg_turnover = stats.get(symbol, (0, 0.0))
                frequency = traded / window_sessions if window_sessions else 0.0
                candidates.append({
                    "symbol": symbol,
                    "close_ok": float(close) >= cfg.min_price,
                    "volume_ok": int(volume) >= cfg.min_volume_on_T,
                    "history_ok": cum_days.get(symbol, 0) >= cfg.min_days_observed,
                    "frequency_ok": frequency >= cfg.min_trading_frequency,
                    "turnover_ok": avg_turnover >= cfg.min_avg_daily_turnover,
                    "avg_turnover": avg_turnover,
                    "trading_frequency": frequency,
                    "impact_cost_proxy": impact_cost_proxy(avg_turnover),
                    "free_float_proxy": free_float_proxy(avg_turnover, frequency),
                })
            for code in ("NIFTY50", "NIFTY500"):
                out[(code, T)] = select_proxy(candidates, cfg, code)
        return out
    finally:
        con.close()


# ------------------------------------------------------ 3. changelog hybrid

def apply_changelog(anchor_symbols: set[str], events: list[dict],
                    as_of: str) -> set[str]:
    """Apply effective-date-aware add/remove events to an anchor set.

    Each event must carry ``effective`` (ISO date), ``action``
    (``add``/``remove``) and ``symbol``. Only events with
    ``effective <= as_of`` are applied (point-in-time). Malformed events
    fail closed. Result stays a PROXY until checkpoint-reconciled.
    """
    members = set(anchor_symbols)
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("changelog event must be a dict")
        symbol = event.get("symbol")
        effective = event.get("effective")
        action = event.get("action")
        if not symbol or not effective or action not in ("add", "remove"):
            raise ValueError(f"malformed changelog event: {event!r}")
        try:
            date.fromisoformat(str(effective))
        except ValueError:
            raise ValueError(f"invalid effective date: {effective!r}") from None
        if str(effective) > as_of:
            continue
        if action == "add":
            members.add(str(symbol))
        else:
            members.discard(str(symbol))
    return members


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


def _month_ends_from_db(db_path: Path) -> list[str]:
    import duckdb

    con = open_catalog(database=db_path)
    try:
        rows = con.execute(
            "SELECT strftime(date,'%Y-%m') ym, max(date) FROM cash_equity_candidates "
            "WHERE series='EQ' AND valid_ohlcv=TRUE GROUP BY 1 ORDER BY 1").fetchall()
    finally:
        con.close()
    return [str(r[1]) for r in rows]


def build_proxy(db_path: Path = DB_PATH, membership_dir: Path = SNAPSHOT_DIR,
                processed_dir: Path = PROCESSED_DIR,
                coverage_path: Path = COVERAGE_PATH,
                cfg: ProxyConfig = ProxyConfig()) -> dict:
    cfg.validate()
    snapshots, inventory = load_snapshot_symbols(membership_dir)
    month_ends = _month_ends_from_db(db_path)
    observations = build_observations(month_ends, snapshots)
    coverage = coverage_from_observations(observations, month_ends)

    # Rule-replay validation at the 26 exact snapshot dates.
    eval_dates = sorted({as_of for _, as_of in snapshots})
    predicted = replay_at_dates(db_path, eval_dates, cfg)
    validation = validate_replay(predicted, snapshots)
    by_index: dict[str, list[dict]] = {"NIFTY50": [], "NIFTY500": []}
    for row in validation:
        if row["precision"] is not None:
            by_index[row["index_code"]].append(row)
    aggregates = {}
    for code, rows in by_index.items():
        if rows:
            aggregates[code] = {
                "snapshots_evaluated": len(rows),
                "mean_precision": round(sum(r["precision"] for r in rows) / len(rows), 4),
                "mean_recall": round(sum(r["recall"] for r in rows) / len(rows), 4),
                "mean_f1": round(sum(r["f1"] for r in rows) / len(rows), 4),
                "min_precision": min(r["precision"] for r in rows),
                "max_precision": max(r["precision"] for r in rows),
                "min_recall": min(r["recall"] for r in rows),
                "max_recall": max(r["recall"] for r in rows),
            }
    # Write parquet (+ sample JSONL for inspection).
    processed_dir.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd

        df = pd.DataFrame(observations).sort_values(["month_end", "index_code", "symbol"])
        df.to_parquet(processed_dir / "proxy_observations.parquet", index=False)
    except ImportError:
        df = None
    sample = sorted(observations,
                    key=lambda o: (o["month_end"], o["index_code"], o["symbol"]))[:1000]
    with (processed_dir / "proxy_observations.sample.jsonl").open("w", encoding="utf-8") as stream:
        for row in sample:
            stream.write(json.dumps(row, default=str) + "\n")
    summary = {
        "label": PROXY_LABEL,
        "is_official_nifty": False,
        "is_proxy": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {"target_counts": cfg.target_counts, "min_price": cfg.min_price,
                   "min_volume_on_T": cfg.min_volume_on_T,
                   "min_days_observed": cfg.min_days_observed,
                   "frequency_window_sessions": cfg.frequency_window_sessions,
                   "min_trading_frequency": cfg.min_trading_frequency,
                   "min_avg_daily_turnover": cfg.min_avg_daily_turnover,
                   "series": cfg.series},
        "source": {"db": db_path.as_posix(), "read_only": True,
                   "view": "cash_equity_candidates",
                   "snapshots": inventory,
                   "month_ends": [min(month_ends, default=None), max(month_ends, default=None)],
                   "month_end_count": len(month_ends)},
        "methodology_eras": METHODOLOGY_ERAS,
        "interval_carry": {"counts": coverage["totals"],
                           "certainty_counts": {},
                           "policy": "LOCF anchor only; before first snapshot unknown "
                                     "(no rows). Carried rows are interval uncertainty, "
                                     "never exact membership."},
        "monthly": coverage["monthly"],
        "rule_replay_validation": validation,
        "rule_replay_aggregates": aggregates,
        "limitations": [
            "Proxy only: interval carry assumes membership persistence between "
            "annual October snapshots; real reconstitutions occur intra-year.",
            "Rule replay: true free-float (shares/IWF) and impact cost "
            "(order-book depth, Rs.10cr observations) are unobservable in "
            "daily OHLCV; turnover/frequency proxies are rank-equivalent only.",
            "Methodology eras (2009 weighting, 2016 F&O/DVR/structure) are "
            "documented but not retro-applied as membership edits here.",
            "Hybrid changelog requires a complete, reconciled event ledger; "
            "sample press releases alone do not constitute one (see research.md).",
            "Raw (unadjusted) prices; no corporate-action adjustment in replay.",
        ],
        "outputs": {"parquet": (processed_dir / "proxy_observations.parquet").as_posix(),
                    "sample_jsonl": (processed_dir / "proxy_observations.sample.jsonl").as_posix()},
    }
    cert: dict[str, int] = {}
    for monthly_row in coverage["monthly"]:
        cert[monthly_row["certainty"]] = cert.get(monthly_row["certainty"], 0) + 1
    summary["interval_carry"]["certainty_counts"] = cert
    write_json(coverage_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--snapshots", type=Path, default=SNAPSHOT_DIR)
    parser.add_argument("--processed", type=Path, default=PROCESSED_DIR)
    parser.add_argument("--coverage", type=Path, default=COVERAGE_PATH)
    args = parser.parse_args()
    summary = build_proxy(args.db, args.snapshots, args.processed, args.coverage)
    print(json.dumps({"label": summary["label"],
                      "observation_rows": summary["interval_carry"]["counts"]["observation_rows"],
                      "snapshots_evaluated": len(summary["rule_replay_validation"]),
                      "aggregates": summary["rule_replay_aggregates"]}))


if __name__ == "__main__":
    main()
