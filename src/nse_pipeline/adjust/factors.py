"""Verified split/bonus adjustment engine (never infer a factor from prices).

Reads ``market.duckdb`` read-only (``bars`` + ``corporate_actions``) and applies
ONLY actions that are unambiguously parsed in the host data:

    action_type IN ('split', 'bonus')
    AND parse_status == 'parsed_not_applied'
    AND share_multiplier IS NOT NULL (finite, > 1)

This is exactly the conservative rule in ``build_data.parse_action``: single-type
splits (FROM x TO y) and bonuses (a:b) that produced a ``share_multiplier``.
Anything else -- ``needs_review``, ``other``, dividends, rights,
reorganizations, compound types (``split+bonus``, ``bonus+dividend``, ...),
missing/invalid multipliers, '/'-joined bonus+split purposes
(``BONUS 1:1/FV SPL ...``), and same-(symbol, series, ex_date) duplicate
groups -- is recorded in the audit table as excluded and never applied.
A price jump is diagnostic only and is never used as a factor source.

Semantics
---------
* ``share_multiplier`` M means the share count multiplies by M at the ex-date
  (e.g. split Rs10 -> Rs2 has M=5, bonus 1:1 has M=2).
* Adjustment factor for a bar on date ``d`` is the cumulative multiplier of all
  applied actions with ``ex_date > d`` (strictly before the ex-date).  Ex-date
  bars themselves are NOT adjusted for that action (``date >= ex_date`` keeps
  the post-action price level).
* Adjusted price = raw price / adjustment_factor, computed in float64 with NO
  rounding (full binary precision preserved).  ``price_multiplier = 1/factor``.
* Multiple actions per (symbol, series) compound multiplicatively (suffix
  products), handled by interval factors in the DuckDB export and by an
  equivalent searchsorted computation in the pure ``apply_adjustment`` helper.
* Only OHLC are adjusted.  Volume, traded value and trade counts are carried
  through unchanged -- there is no dividend adjustment (no total-return
  adjustment) and no volume scaling in this engine.
* ``adj_previous_close`` divides the raw ``previous_close`` by the factor of
  the *previous* bar in the same (symbol, series) line (lagged factor), which
  is the level of the day the previous close refers to.  First bar per line
  has no lag and ``adj_previous_close`` is NULL.
* Raw columns (``open``/``high``/``low``/``close``/``previous_close`` ...) are
  preserved verbatim in the output; adjusted values live in ``adj_*`` columns.
   Legacy actions match (symbol, series). Normalized pre-2011 actions additionally
   require dated official lineage and an exact historical ex-date observation;
   their factors are bounded below by the observed identity epoch.

Outputs (owned by this module only)
-----------------------------------
* ``data/processed/adjusted/adjusted_bars.parquet`` -- all bars, raw + adjusted.
* ``data/processed/adjusted/adjusted_bars_sample.jsonl`` -- small sample.
* ``data/processed/adjusted/action_audit.parquet`` -- one row per
  ``corporate_actions`` row with decision/reason.
* ``data/processed/adjusted/audit_manifest.json`` -- bounded audit manifest
  (counts already computed via DuckDB, streaming SHA-256 of outputs; the
  full adjusted-bars parquet is never loaded into pandas).
* ``data/reports/adjustment-coverage.json`` and
  ``data/reports/adjustment-validation.md``.

``market.duckdb`` is opened with ``read_only=True`` and never written.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

import numpy as np
import pandas as pd

ELIGIBLE_ACTION_TYPES = ("split", "bonus")
VERIFIED_PARSE_STATUS = "parsed_not_applied"

# Audit decisions / reasons.
DECISION_APPLIED = "applied"
DECISION_EXCLUDED = "excluded"
REASON_VERIFIED = "verified_split_bonus"
REASON_SCOPE = "scope_not_split_bonus"
REASON_NEEDS_REVIEW = "needs_review_or_unparsed"
REASON_MISSING_MULT = "missing_share_multiplier"
REASON_INVALID_MULT = "invalid_share_multiplier"
REASON_COMPOUND = "compound_same_day_ambiguous"
REASON_SLASH_COMPOUND = "compound_purpose_ambiguous"
REASON_NO_PRE_EX = "no_pre_ex_bars"

OHLC = ("open", "high", "low", "close")

INTERVAL_SENTINEL_START = date(1900, 1, 1)
INTERVAL_SENTINEL_END = date(9999, 12, 31)


def _to_date(value) -> date | None:
    """Normalize datetime/date/string to ``date``; None stays None."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return pd.to_datetime(text).date()
    return pd.to_datetime(value).date()


def _valid_multiplier(value) -> bool:
    """Finite multiplier strictly greater than 1 (no-op/inverted rejected)."""
    if value is None:
        return False
    try:
        current = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(current) and current > 1.0


def _norm_key(symbol, series, when) -> tuple:
    return (
        None if pd.isna(symbol) else str(symbol),
        None if pd.isna(series) else str(series),
        _to_date(when),
    )


def _has_slash_compound(purpose) -> bool:
    """True when a '/'-joined purpose packs a bonus ratio and a split signal.

    The host parser only catches the first component of such rows (e.g.
    "BONUS 1:1/FV SPL 10 TO 2" is typed bonus with multiplier 2, silently
    dropping the 5x face-value split), so the stored multiplier is known to
    be incomplete.  A single-component slash purpose such as
    "AGM/FV SPLT FRM 10 TO 1" (meeting prefix + one split) is not compound
    and stays eligible.
    """
    if purpose is None:
        return False
    text = str(purpose).upper()
    if "/" not in text:
        return False
    import re

    def bonus_ratio(part: str) -> bool:
        return bool(re.search(r"\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?", part))

    def split_signal(part: str) -> bool:
        return (
            "SPLIT" in part
            or "SPLT" in part
            or bool(re.search(r"\bSPL\b", part))
            or bool(re.search(r"\bFVS\b", part))
            or bool(re.search(r"\b(?:FROM|FRM)\b.{0,30}\bTO\b", part))
        )

    parts = text.split("/")
    return any(bonus_ratio(part) for part in parts) and any(split_signal(part) for part in parts)


def classify_actions(actions: pd.DataFrame, pre_ex_counts: dict | None = None) -> pd.DataFrame:
    """Assign applied/excluded decisions to every corporate-action row.

    Pure function (no DB). ``actions`` must have columns symbol, series,
    ex_date, action_type, share_multiplier, parse_status. ``pre_ex_counts``
    maps ``(symbol, series, ex_date)`` to the number of bars strictly before
    the ex-date in that (symbol, series) line; rows absent from the map count
    as 0.  The same-day duplicate rule: among rows that are otherwise
    verified candidates (single split/bonus, parsed_not_applied, valid
    multiplier), if more than one row shares a (symbol, series, ex_date),
    ALL rows in that group are excluded as ambiguous/compound -- their
    multipliers must never be multiplied together or picked arbitrarily.
    """
    if pre_ex_counts is None:
        pre_ex_counts = {}
    frame = actions.copy()
    decisions: list[str] = []
    reasons: list[str] = []
    pre_counts: list[int] = []
    # First pass: per-row verification, collecting candidate group keys.
    candidate_keys: list[tuple | None] = []
    adapter_conflicts: set[tuple] = set()
    for row in frame.itertuples(index=False):
        record = row._asdict() if hasattr(row, "_asdict") else dict(row)
        action_type = str(record.get("action_type") or "").strip()
        parse_status = str(record.get("parse_status") or "").strip()
        mult = record.get("share_multiplier")
        key = _norm_key(record.get("symbol"), record.get("series"), record.get("ex_date"))
        if isinstance(record.get("verified_action_id"), str) and record.get("verified_readiness_reason") in (
            "compound_action_ambiguous", "conflicting_same_day_actions",
        ):
            adapter_conflicts.add(key)
        pre_counts.append(int(pre_ex_counts.get(key, 0)))
        if action_type not in ELIGIBLE_ACTION_TYPES:
            decisions.append(DECISION_EXCLUDED)
            reasons.append(REASON_SCOPE)
            candidate_keys.append(None)
        elif parse_status != VERIFIED_PARSE_STATUS:
            decisions.append(DECISION_EXCLUDED)
            adapter_reason = record.get("verified_readiness_reason")
            reasons.append(adapter_reason if isinstance(adapter_reason, str) else REASON_NEEDS_REVIEW)
            candidate_keys.append(None)
        elif mult is None or (isinstance(mult, float) and math.isnan(mult)):
            decisions.append(DECISION_EXCLUDED)
            reasons.append(REASON_MISSING_MULT)
            candidate_keys.append(None)
        elif not _valid_multiplier(mult):
            decisions.append(DECISION_EXCLUDED)
            reasons.append(REASON_INVALID_MULT)
            candidate_keys.append(None)
        elif _has_slash_compound(record.get("purpose")):
            decisions.append(DECISION_EXCLUDED)
            reasons.append(REASON_SLASH_COMPOUND)
            candidate_keys.append(None)
        else:
            decisions.append("")  # pending compound / pre-ex check
            reasons.append("")
            candidate_keys.append(key)
    from collections import Counter

    group_sizes = Counter(key for key in candidate_keys if key is not None)
    for index, key in enumerate(candidate_keys):
        if key is None:
            continue
        if group_sizes[key] > 1 or key in adapter_conflicts:
            decisions[index] = DECISION_EXCLUDED
            reasons[index] = REASON_COMPOUND
        elif pre_counts[index] <= 0:
            decisions[index] = DECISION_EXCLUDED
            reasons[index] = REASON_NO_PRE_EX
        else:
            decisions[index] = DECISION_APPLIED
            reasons[index] = REASON_VERIFIED
    frame["decision"] = decisions
    frame["reason"] = reasons
    frame["pre_ex_bars"] = pre_counts
    frame["rows_adjusted"] = [
        pre_counts[i] if decisions[i] == DECISION_APPLIED else 0
        for i in range(len(frame))
    ]
    return frame


def _suffix_products(multipliers: np.ndarray) -> np.ndarray:
    """suffix[i] = prod(multipliers[i:]); suffix[n] = 1."""
    n = len(multipliers)
    suffix = np.ones(n + 1, dtype=np.float64)
    for i in range(n - 1, -1, -1):
        suffix[i] = multipliers[i] * suffix[i + 1]
    return suffix


def bar_factors(dates: np.ndarray, ex_dates: np.ndarray, multipliers: np.ndarray):
    """(factor, n_future) per bar date: prod of M where ex_date > date.

    Strict ``>`` implements "adjust strictly before ex_date": a bar dated
    exactly on the ex-date does not include that action's multiplier.
    """
    if len(ex_dates) == 0:
        return np.ones(len(dates), dtype=np.float64), np.zeros(len(dates), dtype=np.int64)
    order = np.argsort(ex_dates)
    ordered_ex = ex_dates[order]
    ordered_mult = multipliers[order]
    suffix = _suffix_products(ordered_mult)
    idx = np.searchsorted(ordered_ex, dates, side="right")
    return suffix[idx], (len(ex_dates) - idx).astype(np.int64)


def apply_adjustment(bars: pd.DataFrame, applied_actions: pd.DataFrame) -> pd.DataFrame:
    """Return bars plus adj_* columns using applied split/bonus actions only.

    Pure function on DataFrames (no DB, no rounding).  ``bars`` needs
    date/symbol/series/open/high/low/close/previous_close; float64 is kept
    end-to-end (raw columns untouched, adj_* = raw / cumulative factor).
    Volume/traded_value/trades are never touched.
    """
    frame = bars.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    actions_by_line: dict[tuple, pd.DataFrame] = {}
    if applied_actions is not None and len(applied_actions) > 0:
        for (symbol, series), group in applied_actions.groupby(["symbol", "series"], sort=False):
            actions_by_line[(symbol, series)] = group
    factors = np.ones(len(frame), dtype=np.float64)
    n_future = np.zeros(len(frame), dtype=np.int64)
    if actions_by_line:
        line_key = list(zip(frame["symbol"].tolist(), frame["series"].tolist()))
        for line, group in actions_by_line.items():
            mask = np.array([k == line for k in line_key])
            if not mask.any():
                continue
            dates = frame.loc[mask, "date"].to_numpy(dtype="datetime64[ns]")
            ex = pd.to_datetime(group["ex_date"]).dt.normalize().to_numpy(dtype="datetime64[ns]")
            mult = group["share_multiplier"].to_numpy(dtype=np.float64)
            if "factor_start" in group and group["factor_start"].notna().any():
                factor = np.ones(len(dates), dtype=np.float64)
                count = np.zeros(len(dates), dtype=np.int64)
                for (_, action), end, multiplier in zip(group.iterrows(), ex, mult):
                    start = _to_date(action.get("factor_start")) or INTERVAL_SENTINEL_START
                    active = (dates >= np.datetime64(start, "ns")) & (dates < end)
                    factor[active] *= multiplier
                    count[active] += 1
            else:
                factor, count = bar_factors(dates, ex, mult)
            factors[mask] = factor
            n_future[mask] = count
    frame["adjustment_factor"] = factors
    frame["price_multiplier"] = 1.0 / factors
    frame["n_future_actions"] = n_future
    for column in OHLC:
        frame[f"adj_{column}"] = frame[column].astype(np.float64) / factors
    # Previous close belongs to the previous bar's factor in the same line.
    prior_factor = frame.groupby(["symbol", "series"], sort=False)["adjustment_factor"].shift(1)
    frame["adj_previous_close"] = frame["previous_close"].astype(np.float64) / prior_factor
    frame.loc[frame["previous_close"].isna(), "adj_previous_close"] = np.nan
    frame["is_adjusted"] = frame["adjustment_factor"] != 1.0
    return frame


def build_intervals(applied_actions: pd.DataFrame) -> pd.DataFrame:
    """Per-line factor intervals for the DuckDB ASOF export.

    For actions e1<...<en with multipliers M1..Mn, bars in
    [e_k, e_{k+1}) carry factor prod(M_{k+1..n}); bars before e1 carry the
    total product.  Intervals tile [1900-01-01, 9999-12-31) contiguously so an
    ASOF lookup on ``interval_start <= date`` always resolves. Normalized
    actions can supply ``factor_start``: their multiplier is active only
    within that dated observed identity epoch, never earlier symbol reuse.
    """
    rows = []
    if applied_actions is None or len(applied_actions) == 0:
        return pd.DataFrame({
            "symbol": pd.Series(dtype="string"), "series": pd.Series(dtype="string"),
            "interval_start": pd.Series(dtype="datetime64[us]"),
            "interval_end": pd.Series(dtype="datetime64[us]"),
            "factor": pd.Series(dtype="float64"), "n_future": pd.Series(dtype="int64"),
        })
    for (symbol, series), group in applied_actions.groupby(["symbol", "series"], sort=False):
        ordered = group.sort_values("ex_date", kind="stable")
        ex_dates = [pd.to_datetime(value).date() for value in ordered["ex_date"]]
        mults = ordered["share_multiplier"].to_numpy(dtype=np.float64)
        starts = [_to_date(row.get("factor_start")) or INTERVAL_SENTINEL_START
                  for _, row in ordered.iterrows()]
        boundaries = sorted({INTERVAL_SENTINEL_START, INTERVAL_SENTINEL_END, *ex_dates, *starts})
        for current, end in zip(boundaries, boundaries[1:]):
            active = [mult for start, ex, mult in zip(starts, ex_dates, mults) if start <= current < ex]
            rows.append(
                {
                    "symbol": symbol,
                    "series": series,
                    "interval_start": current,
                    "interval_end": end,
                    "factor": float(math.prod(active)),
                    "n_future": len(active),
                }
            )
    intervals = pd.DataFrame(rows)
    intervals["interval_start"] = pd.to_datetime(intervals["interval_start"]).dt.date
    intervals["interval_end"] = pd.to_datetime(intervals["interval_end"]).dt.date
    return intervals.sort_values(["symbol", "series", "interval_start"], kind="stable").reset_index(drop=True)


def _sha256_file(path: Path) -> str:
    """Bounded streaming hash (1 MiB chunks); never loads the whole file."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_entry(path: Path) -> dict:
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _read_large_moves(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["date", "symbol", "series", "close", "prior_close", "raw_return"])
    moves = pd.read_csv(path)
    moves["date"] = pd.to_datetime(moves["date"]).dt.normalize()
    return moves


def run_adjustment(data_root: Path, out_root: Path | None = None, report_root: Path | None = None) -> dict:
    """Run the full engine read-only against ``data_root/market.duckdb``."""
    import duckdb
    from nse_pipeline.actions.verified import load_verified_actions, write_outputs

    data_root = Path(data_root)
    out_root = Path(out_root) if out_root is not None else data_root / "processed" / "adjusted"
    report_root = Path(report_root) if report_root is not None else data_root / "reports"
    out_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)
    db_path = data_root / "market.duckdb"
    if not db_path.exists():
        raise FileNotFoundError(f"Missing {db_path}; build the database first")
    adjusted_parquet = out_root / "adjusted_bars.parquet"
    audit_parquet = out_root / "action_audit.parquet"
    sample_path = out_root / "adjusted_bars_sample.jsonl"
    con = open_catalog(database=db_path)
    try:
        actions = con.execute(
            "SELECT symbol, series, ex_date, record_date, purpose, action_type, "
            "share_multiplier, dividend_per_share, parse_status, first_observed, "
            "last_observed, source FROM corporate_actions "
            "ORDER BY ex_date, symbol, series, purpose"
        ).fetchdf()
        raw_actions_rows = len(actions)
        normalized, normalized_stats = load_verified_actions(data_root, con)
        if normalized:
            actions = pd.concat([actions, pd.DataFrame([row.adjustment_row() for row in normalized])],
                                ignore_index=True)
        # Normalize date columns to a single dtype after the adapter concat:
        # DB rows carry Timestamps while verified rows carry datetime.date,
        # and a mixed object column crashes groupby/sort on real data.
        actions["ex_date"] = pd.to_datetime(actions["ex_date"], errors="coerce")
        if "record_date" in actions:
            actions["record_date"] = pd.to_datetime(actions["record_date"], errors="coerce")
        if "factor_start" in actions:
            actions["factor_start"] = pd.to_datetime(actions["factor_start"], errors="coerce")
        bars_info = con.execute(
            "SELECT count(*) AS rows, count(DISTINCT date) AS sessions, "
            "min(date) AS first_date, max(date) AS last_date FROM bars"
        ).fetchone()
        # Exact pre-ex bar counts per distinct candidate key (inequality join).
        if "factor_start" not in actions:
            actions["factor_start"] = None
        candidate_keys = actions.loc[
            actions["action_type"].isin(ELIGIBLE_ACTION_TYPES)
            & (actions["parse_status"] == VERIFIED_PARSE_STATUS)
            & actions["share_multiplier"].notna(),
            ["symbol", "series", "ex_date", "factor_start"],
        ].copy()
        candidate_keys["factor_start"] = candidate_keys["factor_start"].map(
            lambda value: _to_date(value) or INTERVAL_SENTINEL_START)
        candidate_keys = candidate_keys.groupby(["symbol", "series", "ex_date"], as_index=False).agg(
            factor_start=("factor_start", "min"))
        con.register("candidate_keys", candidate_keys)
        pre_df = con.execute(
            "SELECT k.symbol, k.series, k.ex_date, count(b.date) AS pre_ex_bars "
            "FROM candidate_keys k LEFT JOIN bars b "
            "ON b.symbol = k.symbol AND b.series = k.series AND b.date < k.ex_date "
            "AND b.date >= k.factor_start "
            "GROUP BY k.symbol, k.series, k.ex_date"
        ).fetchdf()
        # Keep original tagged inventory distinct from the verified adapter.
        pre2011 = con.execute(
            "SELECT symbol, series, ex_date, purpose FROM pre2011_actions"
        ).fetchdf()
        pre2011_tags = con.execute("SELECT action_tags FROM pre2011_actions").fetchall()
        con.unregister("candidate_keys")
        pre_ex_counts = {
            _norm_key(row["symbol"], row["series"], row["ex_date"]): int(row["pre_ex_bars"])
            for _, row in pre_df.iterrows()
        }
        audit = classify_actions(actions, pre_ex_counts)
        applied = audit[audit["decision"] == DECISION_APPLIED].copy()
        intervals = build_intervals(applied)
        intervals["factor"] = intervals["factor"].astype(float)
        con.register("intervals", intervals)
        try:
            # Full export: ASOF join on factor intervals + lagged factor for
            # previous_close. No rounding anywhere (DOUBLE division only).
            con.execute(
                f"""COPY (
                WITH joined AS (
                    SELECT b.date, b.symbol, b.series, b.isin, b.open, b.high, b.low,
                        b.close, b.previous_close, b.volume, b.traded_value, b.trades,
                        b.valid_ohlcv, b.source,
                        COALESCE(iv.factor, 1.0) AS adjustment_factor,
                        COALESCE(iv.n_future, 0) AS n_future_actions
                    FROM bars b
                    ASOF LEFT JOIN intervals iv
                      ON b.symbol = iv.symbol AND b.series = iv.series
                     AND b.date >= iv.interval_start
                ),
                lagged AS (
                    SELECT *, lag(adjustment_factor) OVER (
                        PARTITION BY symbol, series ORDER BY date) AS prior_factor
                    FROM joined
                )
                SELECT date, symbol, series, isin,
                    open, high, low, close, previous_close,
                    volume, traded_value, trades, valid_ohlcv, source,
                    open / adjustment_factor AS adj_open,
                    high / adjustment_factor AS adj_high,
                    low / adjustment_factor AS adj_low,
                    close / adjustment_factor AS adj_close,
                    previous_close / prior_factor AS adj_previous_close,
                    adjustment_factor,
                    1.0 / adjustment_factor AS price_multiplier,
                    n_future_actions,
                    (adjustment_factor <> 1.0) AS is_adjusted
                FROM lagged ORDER BY symbol, series, date
                ) TO '{str(adjusted_parquet.resolve()).replace(chr(39), chr(39) * 2)}' """
                f"(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        finally:
            con.unregister("intervals")
        # Audit table: one row per corporate_actions row, deterministic order.
        audit_out = audit.sort_values(["ex_date", "symbol", "series", "purpose"], kind="stable")
        audit_out.to_parquet(audit_parquet, index=False, compression="zstd")
        # Adjusted-lines summary from the written parquet (source of truth).
        adjusted_summary = con.execute(
            f"""SELECT count(*) AS rows,
                    count(*) FILTER (WHERE adjustment_factor <> 1.0) AS rows_adjusted,
                    count(DISTINCT symbol) FILTER (WHERE adjustment_factor <> 1.0) AS symbols_adjusted,
                    count(DISTINCT symbol || ':' || COALESCE(series, '')) FILTER (
                        WHERE adjustment_factor <> 1.0) AS lines_adjusted
                FROM read_parquet('{str(adjusted_parquet.resolve()).replace(chr(39), chr(39) * 2)}')"""
        ).fetchone()
        # Before/after discontinuities on the adjusted parquet.
        disc = con.execute(
            f"""WITH r AS (
                    SELECT symbol, series, close, adj_close,
                        lag(close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_close,
                        lag(adj_close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_adj
                    FROM read_parquet('{str(adjusted_parquet.resolve()).replace(chr(39), chr(39) * 2)}')
                ), m AS (
                    SELECT close / NULLIF(prior_close, 0) - 1 AS raw_move,
                        adj_close / NULLIF(prior_adj, 0) - 1 AS adj_move,
                        (close / NULLIF(prior_close, 0) - 1) IS NOT NULL AS has_raw
                    FROM r WHERE prior_close IS NOT NULL AND prior_close <> 0
                )
                SELECT count(*) FILTER (WHERE has_raw) AS pairs,
                    max(abs(raw_move)) AS max_abs_raw, max(abs(adj_move)) AS max_abs_adj,
                    count(*) FILTER (WHERE abs(raw_move) > 0.5) AS n_over_half_raw,
                    count(*) FILTER (WHERE abs(adj_move) > 0.5) AS n_over_half_adj
                FROM m"""
        ).fetchone()
        # Same before/after restricted to lines that carry at least one applied
        # action, plus the single largest overall move for context (it is not
        # a split: its factor is 1, so before == after by design).
        disc_lines = disc_max_example = None
        if len(applied):
            applied_lines = applied[["symbol", "series"]].drop_duplicates()
            con.register("applied_lines", applied_lines)
            disc_lines = con.execute(
                f"""WITH r AS (
                    SELECT symbol, series, close, adj_close,
                        lag(close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_close,
                        lag(adj_close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_adj
                    FROM read_parquet('{str(adjusted_parquet.resolve()).replace(chr(39), chr(39) * 2)}')
                )
                SELECT count(*) AS pairs, max(abs(close / NULLIF(prior_close, 0) - 1)),
                    max(abs(adj_close / NULLIF(prior_adj, 0) - 1)),
                    count(*) FILTER (WHERE abs(close / NULLIF(prior_close, 0) - 1) > 0.5),
                    count(*) FILTER (WHERE abs(adj_close / NULLIF(prior_adj, 0) - 1) > 0.5)
                FROM r JOIN applied_lines l USING (symbol, series)
                WHERE prior_close IS NOT NULL AND prior_close <> 0"""
            ).fetchone()
            con.unregister("applied_lines")
        disc_max_example = con.execute(
            f"""WITH r AS (
                SELECT symbol, series, date, close, adj_close, adjustment_factor,
                    lag(close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_close
                FROM read_parquet('{str(adjusted_parquet.resolve()).replace(chr(39), chr(39) * 2)}')
            )
            SELECT symbol, series, CAST(date AS VARCHAR), close, prior_close,
                close / NULLIF(prior_close, 0) - 1, adjustment_factor
            FROM r WHERE prior_close IS NOT NULL AND prior_close <> 0
            ORDER BY abs(close / NULLIF(prior_close, 0) - 1) DESC LIMIT 1"""
        ).fetchone()
        # Large-moves overlap: file rows whose (symbol, series, date) equals an
        # applied action's ex-date, with adj returns from the same lag logic.
        large_moves = _read_large_moves(report_root / "large-raw-price-moves.csv")
        matched = pd.DataFrame()
        matched_summary = {
            "large_moves_rows": int(len(large_moves)),
            "matched_applied_rows": 0,
            "matched_max_abs_raw": None,
            "matched_max_abs_adj": None,
            "matched_median_abs_raw": None,
            "matched_median_abs_adj": None,
            "matched_mean_abs_raw": None,
            "matched_mean_abs_adj": None,
            "matched_still_over_half_adj": 0,
        }
        if len(large_moves) and len(applied):
            applied_keys = pd.DataFrame(
                {
                    "symbol": applied["symbol"].astype(str),
                    "series": applied["series"].astype(str),
                    "date": pd.to_datetime(applied["ex_date"]).dt.normalize(),
                }
            ).drop_duplicates()
            keys_df = large_moves.assign(
                symbol=large_moves["symbol"].astype(str),
                series=large_moves["series"].astype(str),
            )[["symbol", "series", "date"]].merge(
                applied_keys, on=["symbol", "series", "date"], how="inner"
            )
            matched = keys_df
            matched_summary["matched_applied_rows"] = int(len(keys_df))
            if len(keys_df):
                con.register("matched_keys", keys_df.assign(date=keys_df["date"].dt.date))
                row = con.execute(
                    f"""WITH r AS (
                        SELECT symbol, series, CAST(date AS DATE) AS date, close, adj_close,
                            lag(close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_close,
                            lag(adj_close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_adj
                        FROM read_parquet('{str(adjusted_parquet.resolve()).replace(chr(39), chr(39) * 2)}')
                    )
                    SELECT max(abs(close / NULLIF(prior_close, 0) - 1)),
                        max(abs(adj_close / NULLIF(prior_adj, 0) - 1)),
                        count(*) FILTER (WHERE abs(adj_close / NULLIF(prior_adj, 0) - 1) > 0.5)
                    FROM r JOIN matched_keys k
                      ON k.symbol = r.symbol AND k.series = r.series AND k.date = r.date
                    WHERE prior_close IS NOT NULL AND prior_close <> 0"""
                ).fetchone()
                matched_moves = con.execute(
                    f"""WITH r AS (
                        SELECT symbol, series, CAST(date AS DATE) AS date, close, adj_close,
                            lag(close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_close,
                            lag(adj_close) OVER (PARTITION BY symbol, series ORDER BY date) AS prior_adj
                        FROM read_parquet('{str(adjusted_parquet.resolve()).replace(chr(39), chr(39) * 2)}')
                    )
                    SELECT abs(close / NULLIF(prior_close, 0) - 1),
                        abs(adj_close / NULLIF(prior_adj, 0) - 1)
                    FROM r JOIN matched_keys k
                      ON k.symbol = r.symbol AND k.series = r.series AND k.date = r.date
                    WHERE prior_close IS NOT NULL AND prior_close <> 0"""
                ).fetchall()
                con.unregister("matched_keys")
                matched_summary["matched_max_abs_raw"] = None if row[0] is None else float(row[0])
                matched_summary["matched_max_abs_adj"] = None if row[1] is None else float(row[1])
                matched_summary["matched_still_over_half_adj"] = int(row[2] or 0)
                if matched_moves:
                    raw_abs = np.array([item[0] for item in matched_moves], dtype=float)
                    adj_abs = np.array([item[1] for item in matched_moves], dtype=float)
                    matched_summary["matched_median_abs_raw"] = float(np.median(raw_abs))
                    matched_summary["matched_median_abs_adj"] = float(np.median(adj_abs))
                    matched_summary["matched_mean_abs_raw"] = float(np.mean(raw_abs))
                    matched_summary["matched_mean_abs_adj"] = float(np.mean(adj_abs))
        # Illustrative sample: rows whose factor != 1 (bounded).
        sample = con.execute(
            f"""SELECT date, symbol, series, open, adj_open, close, adj_close,
                        adjustment_factor, price_multiplier, n_future_actions
                FROM read_parquet('{str(adjusted_parquet.resolve()).replace(chr(39), chr(39) * 2)}')
                WHERE adjustment_factor <> 1.0 ORDER BY symbol, series, date LIMIT 5000"""
        ).fetchdf()
        sample.to_json(sample_path, orient="records", date_format="iso", lines=True)
    finally:
        con.close()
    from collections import Counter

    reason_counts = dict(Counter(audit["reason"]))
    decision_counts = dict(Counter(audit["decision"]))
    applied_series = dict(Counter(applied["series"].astype(str))) if len(applied) else {}
    pre2011_tag_counts: Counter = Counter()
    for (tags,) in pre2011_tags:
        for tag in (tags or []):
            pre2011_tag_counts[str(tag)] += 1
    pre2011_split_bonus_rows = int(
        sum(1 for (tags,) in pre2011_tags if tags and ("split" in tags or "bonus" in tags))
    )
    applied_verified_ids = set(applied["verified_action_id"].dropna()) if "verified_action_id" in applied else set()
    verified_coverage = write_outputs(data_root, normalized, normalized_stats, applied_verified_ids)
    pre2011_applied = int((pd.to_datetime(applied["ex_date"]) < "2011-01-01").sum())
    coverage = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_database": "data/market.duckdb (read-only)",
        "bars_rows": int(bars_info[0]),
        "bars_sessions": int(bars_info[1]),
        "bars_range": [str(bars_info[2]), str(bars_info[3])],
        "rule": {
            "action_type_in": list(ELIGIBLE_ACTION_TYPES),
            "parse_status": VERIFIED_PARSE_STATUS,
            "share_multiplier": "not null, finite, > 1; never inferred from prices",
            "ex_date_semantics": "adjust strictly before ex_date (date < ex_date)",
            "factor": "product of multipliers with ex_date > bar date; adj = raw / factor (float64, no rounding)",
            "volume_adjusted": False,
            "dividends_price_adjusted": False,
            "exclusions": "needs_review, non-split/bonus types, missing/invalid multipliers, "
            "'/'-joined bonus+split purposes, same-(symbol,series,ex_date) duplicate groups, "
            "and verified rows with no bars before the ex-date",
        },
        "corporate_actions_rows": int(len(audit)),
        "raw_corporate_actions_rows": raw_actions_rows,
        "normalized_verified_actions_rows": len(normalized),
        "verified_actions": verified_coverage,
        "decision_counts": decision_counts,
        "reason_counts": reason_counts,
        "applied_actions": int((audit["decision"] == DECISION_APPLIED).sum()),
        "excluded_actions": int((audit["decision"] == DECISION_EXCLUDED).sum()),
        "applied_symbols": int(applied["symbol"].nunique()) if len(applied) else 0,
        "applied_symbol_series": int(len(applied[["symbol", "series"]].drop_duplicates())) if len(applied) else 0,
        "applied_by_series": applied_series,
        "rows_adjusted": int(audit["rows_adjusted"].sum()),
        "rows_adjusted_note": "Per-action sum over applied actions (bars before the earliest "
        "action in a multi-action line count once per later action). Unique bars with "
        "factor != 1 are counted separately in bars_rows_adjusted_factor_ne1.",
        "bars_rows_adjusted_factor_ne1": int(adjusted_summary[1]),
        "symbols_with_adjusted_bars": int(adjusted_summary[2]),
        "symbol_series_with_adjusted_bars": int(adjusted_summary[3]),
        "pre2011": {
            "rows": int(len(pre2011)),
            "tag_counts": dict(pre2011_tag_counts),
            "split_or_bonus_tagged_rows": pre2011_split_bonus_rows,
            "verified_multipliers": sum(row.terms_verified and row.share_multiplier is not None for row in normalized),
            "verified_terms_rows": verified_coverage["verified_terms_rows"],
            "execution_ready": verified_coverage["execution_ready_actions"],
            "applied": pre2011_applied,
            "note": "Original pre2011_actions remains a tags-only inventory. The normalized "
            "adapter separately verifies explicit official terms, NSE exDate and historical "
            "symbol/series. Issuer record/effective dates and price breaks never substitute for ex-dates.",
            "verified_split_bonus_before_2011_in_corporate_actions": int(
                (
                    (audit["action_type"].isin(list(ELIGIBLE_ACTION_TYPES)))
                    & (audit["parse_status"] == VERIFIED_PARSE_STATUS)
                    & audit["share_multiplier"].notna()
                    & (pd.to_datetime(audit["ex_date"]) < "2011-01-01")
                ).sum()
            ),
        },
        "validation": {
            "large_moves_file": "data/reports/large-raw-price-moves.csv",
            **matched_summary,
            "overall_pairs": int(disc[0]),
            "overall_max_abs_raw": None if disc[1] is None else float(disc[1]),
            "overall_max_abs_adj": None if disc[2] is None else float(disc[2]),
            "overall_n_abs_gt_50pct_raw": int(disc[3]),
            "overall_n_abs_gt_50pct_adj": int(disc[4]),
            "overall_max_example": (
                {"symbol": disc_max_example[0], "series": disc_max_example[1],
                 "date": str(disc_max_example[2]), "close": float(disc_max_example[3]),
                 "prior_close": float(disc_max_example[4]),
                 "raw_return": float(disc_max_example[5]),
                 "adjustment_factor": float(disc_max_example[6])}
                if disc_max_example is not None else None
            ),
            "adjusted_lines_pairs": int(disc_lines[0]) if disc_lines else 0,
            "adjusted_lines_max_abs_raw": None if not disc_lines or disc_lines[1] is None else float(disc_lines[1]),
            "adjusted_lines_max_abs_adj": None if not disc_lines or disc_lines[2] is None else float(disc_lines[2]),
            "adjusted_lines_n_abs_gt_50pct_raw": int(disc_lines[3]) if disc_lines else 0,
            "adjusted_lines_n_abs_gt_50pct_adj": int(disc_lines[4]) if disc_lines else 0,
            "note": "Before/after are consecutive-close moves per (symbol, series) on raw close "
            "vs adj_close from the written parquet. The pooled overall max is a factor-1 "
            "(non-split) jump by construction; the split-relevant tests are the matched "
            "large-move rows and the adjusted-lines >50% counts.",
        },
        "files": {
            "adjusted_bars": "data/processed/adjusted/adjusted_bars.parquet",
            "adjusted_bars_sample": "data/processed/adjusted/adjusted_bars_sample.jsonl",
            "action_audit": "data/processed/adjusted/action_audit.parquet",
            "audit_manifest": "data/processed/adjusted/audit_manifest.json",
            "coverage_json": "data/reports/adjustment-coverage.json",
            "validation_md": "data/reports/adjustment-validation.md",
        },
        "limitations": [
            "Matching is (symbol, series) only: historical symbol/ISIN continuity and symbol "
            "reuse are unresolved, so an action adjusts every bar in the same (symbol, series) "
            "line before its ex-date.",
            "Ambiguous same-day groups (both rows) are excluded rather than combined, so any "
            "true compound split+bonus on one ex-date remains unadjusted.",
            "Dividends, rights, mergers/reorganizations and delisting proceeds are not "
            "price-adjusted; this is a split/bonus-only adjustment, not a total-return series.",
            "Volume, traded value and trades are unadjusted (raw levels), so volume-based "
            "backtest quantities are pre-action levels.",
            "Pre-2011 coverage remains incomplete: only cited explicit terms with exact NSE ex-dates "
            "and dated observed historical identity are executable; see verified-actions.json.",
            "A large raw move without a verified action is left as-is by design -- it may reflect "
            "a genuine crash, a missing action, or a data issue, never an invented factor.",
        ],
    }
    (report_root / "adjustment-coverage.json").write_text(
        json.dumps(coverage, indent=2, default=str) + "\n", encoding="utf-8"
    )
    # Audit manifest: counts already computed in DuckDB above plus streaming
    # hashes of the written outputs. Never loads adjusted_bars into pandas.
    audit_manifest = {
        "generated_at": coverage["generated_at"],
        "source_database": coverage["source_database"],
        "bars_rows": coverage["bars_rows"],
        "bars_rows_adjusted_factor_ne1": coverage["bars_rows_adjusted_factor_ne1"],
        "symbols_with_adjusted_bars": coverage["symbols_with_adjusted_bars"],
        "corporate_actions_rows": coverage["corporate_actions_rows"],
        "raw_corporate_actions_rows": coverage["raw_corporate_actions_rows"],
        "normalized_verified_actions_rows": coverage["normalized_verified_actions_rows"],
        "applied_actions": coverage["applied_actions"],
        "excluded_actions": coverage["excluded_actions"],
        "decision_counts": decision_counts,
        "reason_counts": reason_counts,
        "pre2011_applied": coverage["pre2011"]["applied"],
        "verified_actions": verified_coverage,
        "files": {
            "adjusted_bars": _file_entry(adjusted_parquet),
            "action_audit": _file_entry(audit_parquet),
            "adjusted_bars_sample": _file_entry(sample_path),
        },
        "bounded_path": (
            "bounded: DuckDB ASOF interval join for adjusted_bars, SQL pre-ex counts, "
            "and streaming SHA-256; no full-bars pandas load"
        ),
    }
    (out_root / "audit_manifest.json").write_text(
        json.dumps(audit_manifest, indent=2, default=str) + "\n", encoding="utf-8"
    )
    markdown = f"""# Split/bonus adjustment: validation

Generated {coverage["generated_at"]} from `data/market.duckdb` (read-only).
Rule: `action_type IN (split, bonus) AND parse_status=parsed_not_applied AND
share_multiplier NOT NULL`, factors built backward from each ex-date
(strictly-before semantics), `adj = raw / cumulative_factor` in float64 with no
rounding. Volume and dividends are untouched.

## Counts

- Bars: {coverage["bars_rows"]:,} rows, {coverage["bars_sessions"]:,} sessions, {coverage["bars_range"][0]} to {coverage["bars_range"][1]}.
- Corporate-action rows audited: {coverage["corporate_actions_rows"]:,} (applied {coverage["applied_actions"]:,}, excluded {coverage["excluded_actions"]:,}).

| reason | rows |
|---|---|
"""
    for reason, count in sorted(reason_counts.items()):
        markdown += f"| {reason} | {count:,} |\n"
    markdown += f"""
- Applied actions: {coverage["applied_actions"]:,} across {coverage["applied_symbols"]:,} symbols
  ({coverage["applied_symbol_series"]:,} symbol/series lines; by series {coverage["applied_by_series"]}).
- Bars with factor != 1: {coverage["bars_rows_adjusted_factor_ne1"]:,} rows, {coverage["symbols_with_adjusted_bars"]:,} symbols.
- Pre-2011: {coverage["pre2011"]["applied"]} applied; readiness and terms are reported separately below.

## Pre-2011

pre2011_actions has {coverage["pre2011"]["rows"]:,} rows. Tag counts:
split: {coverage["pre2011"]["tag_counts"].get("split", 0)},
bonus: {coverage["pre2011"]["tag_counts"].get("bonus", 0)},
other tags {coverage["pre2011"]["tag_counts"]}. Rows tagged split/bonus:
{coverage["pre2011"]["split_or_bonus_tagged_rows"]}. Verified numeric multipliers:
{coverage["pre2011"]["verified_multipliers"]}; verified-terms rows:
{coverage["pre2011"]["verified_terms_rows"]}; exact execution-ready events:
{coverage["pre2011"]["execution_ready"]}; actually applied: {coverage["pre2011"]["applied"]}.
The original table stays tags-only. The separate normalized adapter consumes
cited explicit official numeric terms, exact NSE `exDate`, and historical
symbol/series observations with dated official lineage. Record/effective dates
and queue discontinuity dates never become execution dates. Issuer prose alone
remains non-executable. See `verified-actions.json` / `.md` for the complete audit.

## Discontinuity validation (before vs after)

Consecutive-close moves per (symbol, series) on the written adjusted parquet
(all bars, {coverage["validation"]["overall_pairs"]:,} pairs):

- Max |raw return| before: {coverage["validation"]["overall_max_abs_raw"]:.4f}
- Max |adjusted return| after: {coverage["validation"]["overall_max_abs_adj"]:.4f}
- Moves with |return| > 50%: {coverage["validation"]["overall_n_abs_gt_50pct_raw"]:,} before, {coverage["validation"]["overall_n_abs_gt_50pct_adj"]:,} after.

Against `data/reports/large-raw-price-moves.csv` ({coverage["validation"]["large_moves_rows"]:,} rows,
all series EQ, |raw return| > 50%):

- Rows whose (symbol, series, date) equals an applied action ex-date: {coverage["validation"]["matched_applied_rows"]:,}
- Matched max |raw return|: {_fmt(coverage["validation"]["matched_max_abs_raw"])}
- Matched max |adjusted return|: {_fmt(coverage["validation"]["matched_max_abs_adj"])}
- Matched median |raw|: {_fmt(coverage["validation"]["matched_median_abs_raw"])}; median |adjusted|: {_fmt(coverage["validation"]["matched_median_abs_adj"])}
- Matched mean |raw|: {_fmt(coverage["validation"]["matched_mean_abs_raw"])}; mean |adjusted|: {_fmt(coverage["validation"]["matched_mean_abs_adj"])}
- Matched rows still > 50% after adjustment: {coverage["validation"]["matched_still_over_half_adj"]}

Restricted to lines carrying at least one applied action ({coverage["validation"]["adjusted_lines_pairs"]:,} pairs):

- Max |raw|: {_fmt(coverage["validation"]["adjusted_lines_max_abs_raw"])}; max |adjusted|: {_fmt(coverage["validation"]["adjusted_lines_max_abs_adj"])}
- Moves with |return| > 50%: {coverage["validation"]["adjusted_lines_n_abs_gt_50pct_raw"]:,} before, {coverage["validation"]["adjusted_lines_n_abs_gt_50pct_adj"]:,} after.
- Largest overall move (factor-1 context): {coverage["validation"]["overall_max_example"]}

A pooled maximum over all dates is not a split test: it is dominated by non-action jumps
(e.g. the overall max has adjustment_factor 1.0), and on an adjusted line a genuine ex-date
rally is amplified since adj ratio = raw ratio x F_prior/F_current. The split-relevant test
is the matched subset above. Worked examples (EQ): ONMOBILE 2011-05-03 (bonus 2x) raw
257.45 -> 128.30 (-50.2%), adjusted 128.725 -> 128.30 (-0.3%); NELCAST 2011-09-06 (split 5x)
raw 108.00 -> 25.30 (-76.6%), adjusted 21.60 -> 25.30 (+17.1%, a post-split rally on the
split day). GRUH 2012-07-24 keeps its raw break because the same-day conflicting pair is
excluded as ambiguous -- by design, not an error. Conversely HDFCMFGETF 2020-03-26 (100x
split, raw 4004.75 -> 3931.50, adjusted 40.05 -> 3931.50) shows a verified factor that did
not move the listed price, creating an adjusted jump; the factor is never overridden by
price evidence.

For a 2:1 split the raw ex-date return is about -50% and the adjusted return is
about 0%; the matched maxima should therefore collapse for split/bonus-driven
large moves, while genuine crashes and un-actioned moves stay large by design.

## Files

- `data/processed/adjusted/adjusted_bars.parquet` -- all bars: raw columns
  verbatim plus `adj_open/adj_high/adj_low/adj_close/adj_previous_close`,
  `adjustment_factor`, `price_multiplier`, `n_future_actions`, `is_adjusted`.
- `data/processed/adjusted/action_audit.parquet` -- one row per
  `corporate_actions` row with `decision`, `reason`, `pre_ex_bars`, `rows_adjusted`.
- `data/processed/adjusted/audit_manifest.json` -- bounded audit manifest with
  counts and streaming SHA-256 hashes of the written outputs.
- `data/reports/adjustment-coverage.json` -- machine-readable coverage.
- `data/processed/adjusted/adjusted_bars_sample.jsonl` -- 5,000-row
  illustrative sample of adjusted bars.

## Limits

"""
    markdown += "\n".join("- " + item for item in coverage["limitations"]) + "\n"
    (report_root / "adjustment-validation.md").write_text(markdown, encoding="utf-8")
    return coverage


def _fmt(value) -> str:
    return "n/a" if value is None else f"{float(value):.4f}"


# Late-bound placeholder replaced in the markdown template above.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=data_root(),
                        help="Directory holding market.duckdb and processed outputs")
    args = parser.parse_args()
    result = run_adjustment(args.data)
    print(json.dumps({
        "applied_actions": result["applied_actions"],
        "excluded_actions": result["excluded_actions"],
        "symbols_with_adjusted_bars": result["symbols_with_adjusted_bars"],
        "bars_rows_adjusted_factor_ne1": result["bars_rows_adjusted_factor_ne1"],
        "overall_max_abs_raw": result["validation"]["overall_max_abs_raw"],
        "overall_max_abs_adj": result["validation"]["overall_max_abs_adj"],
        "matched_applied_rows": result["validation"]["matched_applied_rows"],
        "matched_max_abs_raw": result["validation"]["matched_max_abs_raw"],
        "matched_max_abs_adj": result["validation"]["matched_max_abs_adj"],
    }, indent=2))


if __name__ == "__main__":
    main()
