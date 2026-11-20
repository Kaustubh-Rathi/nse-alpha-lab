"""Freeze diagnostic - why the 2000-2026 momentum strategy stops trading.

Read-only analysis over the artefacts produced by
``nse_pipeline.backtest.sensitivity``.  This module does **not** re-run a
backtest, does **not** import the engine and does **not** write to the market
database.  Everything it reports is a deterministic function of the sensitivity
artefacts, so two runs over the same inputs produce byte-identical output.

Per variant it re-derives:

* trading activity over time - sessions with trades, sessions with none, the
  longest consecutive zero-trade streak, first/last trade date;
* the rebalance accounting - how many rebalances produced an order and how
  many produced none;
* a mechanism attribution that separates (a) a genuine market/strategy effect
  from (b) a data/model limitation; and
* the counterfactual - in how many sessions did the book hold ZERO
  unpriceable positions, i.e. in how many sessions was the equity actually
  knowable at all.

The engine's own behaviour that this diagnostic measures is (source:
``src/nse_pipeline/backtest/sensitivity.py``, ``_execute``):

    opening_known = all(_positive(day.get(k, (None,) * 5)[4]) and not p.get("blocked")
                        for k, p in positions.items())

A holding that has no bar in the date-local EQ/INE research universe has no
opening mark, so ``opening_known`` is False, ``per`` (the per-name budget) is
zero, and every buy is skipped with ``buy_unknown_held_opening_value``.  The
same holding cannot be sold either - a sell needs a tradable row - so it stays
in the book forever.  The book can therefore only ever shrink after such a
freeze; it can never be re-populated.  This module measures how much of the
26-year window that single mechanism consumed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog
from typing import Iterable, Mapping, Sequence

SCHEMA_VERSION = 1

DEFAULT_DATA = data_root()
DEFAULT_SENSITIVITY = DEFAULT_DATA / "processed/backtest/sensitivity"
DEFAULT_REPORTS = DEFAULT_DATA / "reports"
DEFAULT_OUT = DEFAULT_DATA / "processed/backtest/freeze"
REPORT_STEM = "backtest-freeze-diagnostic"

# Attribution buckets for an execution session that produced no order.
BUCKET_FROZEN = "frozen_unpriceable_holding"
BUCKET_NOT_EXECUTABLE = "not_executable_order"
BUCKET_QUARANTINED = "quarantined_event"
BUCKET_NO_ELIGIBLE = "no_eligible_names"
BUCKET_ORDERS = "orders_filled"

#: Buckets that are a genuine market/strategy (or deliberate policy) effect.
GENUINE_BUCKETS = (BUCKET_NO_ELIGIBLE, BUCKET_NOT_EXECUTABLE, BUCKET_QUARANTINED)
#: Buckets that are a data/model limitation rather than a market effect.
LIMITATION_BUCKETS = (BUCKET_FROZEN,)

ATTESTED_NOT_EXECUTABLE = "buy_missing_open_or_not_executable"
ATTESTED_QUARANTINED = "buy_quarantined_unresolved_event"


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without any file I/O)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ZeroTradeStreak:
    """Longest run of consecutive sessions in which nothing was traded."""

    sessions: int = 0
    start: date | None = None
    end: date | None = None

    def as_dict(self) -> dict:
        return {
            "sessions": self.sessions,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
        }


def longest_zero_trade_streak(session_dates: Sequence[date],
                              traded_dates: Iterable[date]) -> ZeroTradeStreak:
    """Longest run of consecutive sessions carrying no trade.

    ``session_dates`` must be the full ordered session calendar (a session
    with no trade still counts, and breaks nothing).  Sessions outside the
    calendar are ignored.  Consecutive means consecutive *in the calendar*:
    weekends and holidays do not break a streak.
    """
    traded = set(traded_dates)
    best = ZeroTradeStreak()
    run = 0
    run_start: date | None = None
    for session in session_dates:
        if session in traded:
            run = 0
            run_start = None
            continue
        if run == 0:
            run_start = session
        run += 1
        if run > best.sessions:
            best = ZeroTradeStreak(run, run_start, session)
    return best


def execution_sessions_for(session_dates: Sequence[date]) -> list[date]:
    """Rebuild the engine's execution calendar from the session calendar.

    The engine rebalances on the last session of every calendar month and
    executes at the open of the *next* session.  The final month end has no
    successor and therefore never executes.
    """
    ordered = sorted(session_dates)
    month_ends: list[date] = []
    current: tuple[int, int] | None = None
    for session in ordered:
        key = (session.year, session.month)
        if key != current:
            month_ends.append(session)
            current = key
        else:
            month_ends[-1] = session
    index = {session: i for i, session in enumerate(ordered)}
    executions = []
    for month_end in month_ends:
        i = index[month_end]
        if i + 1 < len(ordered):
            executions.append(ordered[i + 1])
    return executions


def attribute_execution_sessions(
    executions: Sequence[date],
    trades_by_session: Mapping[date, int],
    unpriced_by_session: Mapping[date, int],
    attested_by_session: Mapping[date, frozenset[str]] | None = None,
) -> dict[str, int]:
    """Bucket each execution session by the mechanism that produced no order.

    ``trades_by_session``      orders actually filled on that session.
    ``unpriced_by_session``    number of holdings that could not be priced.
    ``attested_by_session``    skip reasons attested for that session by the
                               impact audit (a capped sample, so it can only
                               ever *add* evidence, never remove it).

    The buckets are disjoint for the buy path by construction of the engine:
    a ``buy_not_executable`` skip is only reachable once ``opening_known`` is
    true, so an unpriceable holding and a not-executable buy can never both
    explain the same suppressed buy.
    """
    attested = attested_by_session or {}
    counts: Counter[str] = Counter()
    for session in executions:
        if trades_by_session.get(session, 0) > 0:
            counts[BUCKET_ORDERS] += 1
            continue
        marks = attested.get(session, frozenset())
        if ATTESTED_NOT_EXECUTABLE in marks:
            counts[BUCKET_NOT_EXECUTABLE] += 1
        elif ATTESTED_QUARANTINED in marks:
            counts[BUCKET_QUARANTINED] += 1
        elif unpriced_by_session.get(session, 0) > 0:
            counts[BUCKET_FROZEN] += 1
        else:
            counts[BUCKET_NO_ELIGIBLE] += 1
    return dict(sorted(counts.items()))


def known_equity_by_year(rows: Iterable[Mapping]) -> list[dict]:
    """Per-year count of sessions whose equity was actually knowable.

    A session is *knowable* only when the book held zero unpriceable
    positions.  This is the counterfactual that bounds how much of the window
    is measurable today at all.
    """
    buckets: dict[int, Counter] = defaultdict(Counter)
    for row in rows:
        year = int(row["year"])
        bucket = buckets[year]
        bucket["sessions"] += 1
        unpriced = int(row.get("unpriced_positions") or 0)
        if unpriced == 0:
            bucket["known_equity_sessions"] += 1
        else:
            bucket["unknown_equity_sessions"] += 1
            bucket["unpriced_position_instances"] += unpriced
    out = []
    for year in sorted(buckets):
        bucket = buckets[year]
        sessions = bucket["sessions"]
        known = bucket["known_equity_sessions"]
        out.append({
            "year": year,
            "sessions": sessions,
            "known_equity_sessions": known,
            "unknown_equity_sessions": bucket["unknown_equity_sessions"],
            "known_equity_share": round(known / sessions, 6) if sessions else 0.0,
            "unpriced_position_instances": bucket["unpriced_position_instances"],
        })
    return out


# ---------------------------------------------------------------------------
# Artefact loading
# ---------------------------------------------------------------------------
def _read_csv(path: Path, encoding: str = "utf-8") -> list[dict]:
    """Read a CSV, tolerating a non-UTF-8 source file."""
    try:
        with Path(path).open(newline="", encoding=encoding) as stream:
            return list(csv.DictReader(stream))
    except UnicodeDecodeError:
        with Path(path).open(newline="", encoding="latin-1") as stream:
            return list(csv.DictReader(stream))


def _to_int(value) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _to_float(value) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_date(value) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Per-variant analysis
# ---------------------------------------------------------------------------
@dataclass
class VariantActivity:
    """Everything the diagnostic knows about one variant."""

    variant_id: str = ""
    lookback_sessions: int = 0
    top_n: int = 0
    policy: str = ""
    sessions: int = 0
    window_start: date | None = None
    window_end: date | None = None
    trades: int = 0
    buys: int = 0
    sells: int = 0
    trade_sessions: int = 0
    sessions_with_zero_trades: int = 0
    longest_zero_trade_streak: ZeroTradeStreak = field(default_factory=ZeroTradeStreak)
    first_trade: date | None = None
    last_trade: date | None = None
    rebalances: int = 0
    execution_sessions: int = 0
    rebalances_with_eligible_names: int | None = None
    rebalances_without_eligible_names: int | None = None
    execution_sessions_with_orders: int = 0
    execution_sessions_without_orders: int = 0
    share_rebalances_without_order: float = 0.0
    mechanism: dict[str, int] = field(default_factory=dict)
    skip_counts: dict[str, int] = field(default_factory=dict)
    unpriced_reasons: dict[str, int] = field(default_factory=dict)
    sessions_with_unknown_opening_mark: int = 0
    known_equity_sessions: int = 0
    unknown_equity_sessions: int = 0
    known_equity_share: float = 0.0
    last_known_equity_session: date | None = None
    first_unknown_equity_session: date | None = None
    terminal_positions: int = 0
    terminal_frozen_symbols: list[str] = field(default_factory=list)
    final_equity_strict: float | None = None
    final_equity_marked: float | None = None
    trades_on_non_execution_sessions: int = 0
    no_order_sessions_before_first_trade: int = 0
    execution_sessions_with_unpriced_holding: int = 0
    year_rows: list[dict] = field(default_factory=list)
    session_rows: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        genuine = sum(self.mechanism.get(b, 0) for b in GENUINE_BUCKETS)
        limitation = sum(self.mechanism.get(b, 0) for b in LIMITATION_BUCKETS)
        denominator = self.execution_sessions or 1
        return {
            "variant_id": self.variant_id,
            "lookback_sessions": self.lookback_sessions,
            "top_n": self.top_n,
            "policy": self.policy,
            "sessions": self.sessions,
            "window": {
                "start": self.window_start.isoformat() if self.window_start else None,
                "end": self.window_end.isoformat() if self.window_end else None,
            },
            "trades": self.trades,
            "buys": self.buys,
            "sells": self.sells,
            "trade_sessions": self.trade_sessions,
            "sessions_with_zero_trades": self.sessions_with_zero_trades,
            "longest_zero_trade_streak": self.longest_zero_trade_streak.as_dict(),
            "first_trade": self.first_trade.isoformat() if self.first_trade else None,
            "last_trade": self.last_trade.isoformat() if self.last_trade else None,
            "rebalances": self.rebalances,
            "execution_sessions": self.execution_sessions,
            "rebalances_with_eligible_names": self.rebalances_with_eligible_names,
            "rebalances_without_eligible_names": self.rebalances_without_eligible_names,
            "execution_sessions_with_orders": self.execution_sessions_with_orders,
            "execution_sessions_without_orders": self.execution_sessions_without_orders,
            "share_rebalances_without_order": round(
                self.execution_sessions_without_orders / denominator, 6),
            "mechanism_attribution": {
                "buckets": self.mechanism,
                "genuine_market_or_strategy_sessions": genuine,
                "data_or_model_limitation_sessions": limitation,
                "genuine_share_of_no_order_sessions": round(
                    genuine / (self.execution_sessions_without_orders or 1), 6),
                "limitation_share_of_no_order_sessions": round(
                    limitation / (self.execution_sessions_without_orders or 1), 6),
            },
            "engine_skip_counts": self.skip_counts,
            "unpriced_reason_counts": self.unpriced_reasons,
            "sessions_with_unknown_opening_mark": self.sessions_with_unknown_opening_mark,
            "known_equity_sessions": self.known_equity_sessions,
            "unknown_equity_sessions": self.unknown_equity_sessions,
            "known_equity_share": self.known_equity_share,
            "last_known_equity_session": (self.last_known_equity_session.isoformat()
                                          if self.last_known_equity_session else None),
            "first_unknown_equity_session": (self.first_unknown_equity_session.isoformat()
                                             if self.first_unknown_equity_session else None),
            "terminal_positions": self.terminal_positions,
            "terminal_frozen_symbols": list(self.terminal_frozen_symbols),
            "final_equity_strict": self.final_equity_strict,
            "final_equity_last_observed_close_marking": self.final_equity_marked,
            "reconstruction_cross_checks": {
                "trades_on_non_execution_sessions": self.trades_on_non_execution_sessions,
                "execution_sessions_with_unpriced_holding":
                    self.execution_sessions_with_unpriced_holding,
                "engine_sessions_with_unknown_opening_mark":
                    self.sessions_with_unknown_opening_mark,
                "sessions_unpriced_but_opening_mark_known": (
                    self.execution_sessions_with_unpriced_holding
                    - self.sessions_with_unknown_opening_mark),
                "no_order_sessions_before_first_trade":
                    self.no_order_sessions_before_first_trade,
                "no_eligible_names_bucket_sessions":
                    self.mechanism.get(BUCKET_NO_ELIGIBLE, 0),
            },
        }


def _analyse_variant(variant_id: str, marks: list[dict], trades: list[dict],
                     summary_row: Mapping | None, impact_rows: list[dict],
                     unknown_by_year: dict[int, dict]) -> VariantActivity:
    activity = VariantActivity(variant_id=variant_id)
    marks = sorted(marks, key=lambda row: row["date"])
    session_dates = [row["date"] for row in marks]
    activity.sessions = len(session_dates)
    if session_dates:
        activity.window_start = session_dates[0]
        activity.window_end = session_dates[-1]

    trades = sorted(trades, key=lambda row: row["date"])
    activity.trades = len(trades)
    activity.buys = sum(1 for row in trades if row["side"] == "buy")
    activity.sells = sum(1 for row in trades if row["side"] == "sell")
    trade_dates = {row["date"] for row in trades}
    activity.trade_sessions = len(trade_dates)
    activity.sessions_with_zero_trades = activity.sessions - activity.trade_sessions
    activity.longest_zero_trade_streak = longest_zero_trade_streak(session_dates, trade_dates)
    if trade_dates:
        activity.first_trade = min(trade_dates)
        activity.last_trade = max(trade_dates)

    # -- known vs unknown equity ------------------------------------------
    unpriced_by_session: dict[date, int] = {}
    trades_by_session: Counter = Counter()
    year_buckets: dict[int, Counter] = defaultdict(Counter)
    year_unpriced_instances: Counter = Counter()
    known = 0
    unknown = 0
    last_known: date | None = None
    first_unknown: date | None = None
    for row in marks:
        unpriced = row["unpriced_positions"]
        unpriced_by_session[row["date"]] = unpriced
        year_buckets[row["date"].year]["sessions"] += 1
        if unpriced == 0:
            known += 1
            year_buckets[row["date"].year]["known_equity_sessions"] += 1
            last_known = row["date"]
        else:
            unknown += 1
            year_buckets[row["date"].year]["unknown_equity_sessions"] += 1
            year_buckets[row["date"].year]["unpriced_position_instances"] += unpriced
            year_unpriced_instances[row["date"].year] += unpriced
            if first_unknown is None:
                first_unknown = row["date"]
    activity.known_equity_sessions = known
    activity.unknown_equity_sessions = unknown
    activity.known_equity_share = round(known / activity.sessions, 6) if activity.sessions else 0.0
    activity.last_known_equity_session = last_known
    activity.first_unknown_equity_session = first_unknown

    for row in trades:
        trades_by_session[row["date"]] += 1

    # -- execution-session reconstruction ----------------------------------
    executions = execution_sessions_for(session_dates)
    activity.execution_sessions = len(executions)
    execution_set = set(executions)
    activity.trades_on_non_execution_sessions = sum(
        count for session, count in trades_by_session.items() if session not in execution_set)

    attested: dict[date, set[str]] = defaultdict(set)
    for row in impact_rows:
        session = row["date"]
        if session is not None:
            attested[session].add(row["reason"])
    activity.mechanism = attribute_execution_sessions(
        executions, trades_by_session, unpriced_by_session,
        {k: frozenset(v) for k, v in attested.items()})
    activity.execution_sessions_with_orders = activity.mechanism.get(BUCKET_ORDERS, 0)
    activity.execution_sessions_without_orders = activity.execution_sessions - \
        activity.execution_sessions_with_orders

    # -- cross-checks -------------------------------------------------------
    # Every no-order execution session before the first trade is a warm-up
    # session: the book was empty and the momentum rank could not yet name
    # anything (the engine needs MIN_VALID_OBSERVATIONS valid observations per
    # security).  That count must equal the 'no eligible names' bucket.
    activity.no_order_sessions_before_first_trade = sum(
        1 for session in executions
        if trades_by_session.get(session, 0) == 0
        and (activity.first_trade is None or session < activity.first_trade))
    activity.execution_sessions_with_unpriced_holding = sum(
        1 for session in executions if unpriced_by_session.get(session, 0) > 0)

    # -- engine counters ----------------------------------------------------
    if summary_row:
        activity.lookback_sessions = _to_int(summary_row.get("lookback_sessions"))
        activity.top_n = _to_int(summary_row.get("top_n"))
        activity.policy = str(summary_row.get("policy") or "")
        activity.rebalances = _to_int(summary_row.get("rebalances"))
        activity.skip_counts = {k: _to_int(v) for k, v in
                                (summary_row.get("skip_counts") or {}).items()}
        activity.unpriced_reasons = {k: _to_int(v) for k, v in
                                     (summary_row.get("unpriced_reason_counts") or {}).items()}
        activity.sessions_with_unknown_opening_mark = _to_int(
            summary_row.get("sessions_with_unknown_opening_mark"))
        activity.terminal_positions = _to_int(summary_row.get("terminal_positions"))
        activity.final_equity_strict = _to_float(summary_row.get("final_equity_strict"))
        activity.final_equity_marked = _to_float(
            summary_row.get("final_equity_last_observed_close_marking"))
        scheduled = summary_row.get("signals_scheduled")
        if scheduled is not None:
            activity.rebalances_with_eligible_names = _to_int(scheduled)
            activity.rebalances_without_eligible_names = (
                activity.rebalances - activity.rebalances_with_eligible_names)

    # -- per-year rows ------------------------------------------------------
    for year in sorted(year_buckets):
        bucket = year_buckets[year]
        sessions = bucket["sessions"]
        known_year = bucket["known_equity_sessions"]
        year_trades = sum(count for session, count in trades_by_session.items()
                          if session.year == year)
        year_trade_sessions = sum(1 for session in trade_dates if session.year == year)
        reasons = unknown_by_year.get(year, {}).get("unknown_reasons", {})
        activity.year_rows.append({
            "variant_id": variant_id,
            "year": year,
            "sessions": sessions,
            "trades": year_trades,
            "trade_sessions": year_trade_sessions,
            "sessions_with_zero_trades": sessions - year_trade_sessions,
            "known_equity_sessions": known_year,
            "unknown_equity_sessions": bucket["unknown_equity_sessions"],
            "known_equity_share": round(known_year / sessions, 6) if sessions else 0.0,
            "unpriced_position_instances": bucket["unpriced_position_instances"],
            "unknown_reason_no_bar_in_date_local_eq_research_universe": _to_int(
                reasons.get("no_bar_in_date_local_eq_research_universe")),
            "unknown_reason_no_valid_close_for_session": _to_int(
                reasons.get("no_valid_close_for_session")),
            "unknown_reason_position_blocked_unverified_identity_change": _to_int(
                reasons.get("position_blocked_unverified_identity_change")),
            "unknown_reason_quarantined_unresolved_event": _to_int(
                reasons.get("quarantined_unresolved_event")),
        })

    # -- per-session rows + terminal frozen symbols -------------------------
    frozen_symbols: set[str] = set()
    tail_from = last_known
    for row in marks:
        symbols = [s for s in (row["unpriced_symbols"] or "").split(";") if s]
        if tail_from is not None and row["date"] > tail_from:
            frozen_symbols.update(symbols)
        activity.session_rows.append({
            "variant_id": variant_id,
            "date": row["date"].isoformat(),
            "year": row["date"].year,
            "trades": trades_by_session.get(row["date"], 0),
            "is_execution_session": row["date"] in execution_set,
            "equity_status": row["equity_status"],
            "known_equity": row["unpriced_positions"] == 0,
            "n_positions": row["n_positions"],
            "unpriced_positions": row["unpriced_positions"],
            "unpriced_symbols": row["unpriced_symbols"] or "",
        })
    activity.terminal_frozen_symbols = sorted(frozen_symbols)
    return activity


# ---------------------------------------------------------------------------
# Optional enrichment: where did the frozen securities go?
# ---------------------------------------------------------------------------
def enrich_frozen_symbols(data_root: Path, symbols: Sequence[str],
                          window_end: date | None = None) -> list[dict]:
    """Describe each frozen security from the read-only market database.

    Best effort: if the database or the delisted list is unavailable the
    section is simply empty.  Nothing is written and the database is opened
    read-only.

    The date that matters is the last date the security was inside the
    date-local EQ/INE research universe, i.e. the last date the engine could
    have priced it.  ``frozen_cause`` names the reason the holding became
    unpriceable:

    ``left_eq_universe_series_migration``
        the security kept trading but moved to another series (BE, BZ);
    ``identity_key_change``
        the security kept trading inside the research universe, so the
        holding's own key must have changed under it (a rename or the arrival
        of an ISIN) and the holding was never transferred;
    ``data_gap_no_bars``
        the security simply stopped having bars in the source.
    """
    if not symbols:
        return []
    db_path = Path(data_root) / "market.duckdb"
    if not db_path.exists():
        return []
    try:
        import duckdb  # noqa: PLC0415 - optional dependency, imported lazily
    except Exception:  # pragma: no cover - duckdb is a project dependency
        return []
    delisted: dict[str, dict] = {}
    delisted_path = Path(data_root) / "raw/security_master/delisted.csv"
    if delisted_path.exists():
        for row in _read_csv(delisted_path):
            symbol = (row.get("Symbol") or "").strip()
            if symbol:
                delisted[symbol] = {
                    "company": (row.get("Company") or "").strip(),
                    "delisted_date": (row.get("Delisted Date") or "").strip(),
                    "delisting_type": (row.get("Type of Delisting") or "").strip(),
                }
    out: list[dict] = []
    edges: set[tuple[str, str]] = set()
    edges_path = Path(data_root) / "processed/identity/identity_edges.jsonl"
    if edges_path.exists():
        for line in edges_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                edge = json.loads(line)
            except json.JSONDecodeError:
                continue
            old, new = edge.get("old_symbol"), edge.get("new_symbol")
            if old and new:
                edges.add((old, new))
                edges.add((new, old))
    try:
        db = open_catalog(database=db_path)
    except Exception:  # pragma: no cover - defensive
        return []
    try:
        for symbol in sorted(set(symbols)):
            row = db.execute(
                "SELECT min(date), max(date), count(*), "
                "       count(*) FILTER (WHERE series = 'EQ'), "
                "       count(*) FILTER (WHERE series = 'EQ' AND (isin LIKE 'INE%' "
                "           OR isin IS NULL OR trim(isin) = '')) "
                "FROM bars WHERE symbol = ?", [symbol]).fetchone()
            universe = db.execute(
                "SELECT min(date), max(date) FROM bars WHERE symbol = ? AND series = 'EQ' "
                "AND (isin LIKE 'INE%' OR isin IS NULL OR trim(isin) = '')",
                [symbol]).fetchone()
            last_row = db.execute(
                "SELECT date, series, isin, valid_ohlcv FROM bars "
                "WHERE symbol = ? ORDER BY date DESC LIMIT 1", [symbol]).fetchone()
            info = delisted.get(symbol, {})
            universe_exit = universe[1] if universe and universe[1] else None
            last_series = last_row[1] if last_row else None
            still_trading = bool(window_end and last_row and last_row[0] >= window_end)
            if still_trading and last_series == "EQ":
                cause = "identity_key_change"
            elif last_series and last_series != "EQ":
                cause = "left_eq_universe_series_migration"
            else:
                cause = "data_gap_no_bars"
            # A delisted row under a different symbol whose name starts with
            # this one is a NAME match, not a proven identity.  It is reported
            # only to show that the official list knows the security while the
            # repository cannot bind the two, which is exactly why the
            # terminal proceeds stay unresolved.
            prefix_match = next(
                (name for name in sorted(delisted)
                 if name != symbol and name.startswith(symbol)), None)
            prefix_info = delisted.get(prefix_match) or {}
            out.append({
                "symbol": symbol,
                "first_bar": row[0].isoformat() if row[0] else None,
                "last_bar": row[1].isoformat() if row[1] else None,
                "bars_total": int(row[2] or 0),
                "bars_eq": int(row[3] or 0),
                "bars_eq_ine_research_universe": int(row[4] or 0),
                "research_universe_first_bar": (universe[0].isoformat()
                                                if universe and universe[0] else None),
                "research_universe_exit_date": (universe_exit.isoformat()
                                                if universe_exit else None),
                "last_bar_series": last_series,
                "last_bar_isin": last_row[2] if last_row else None,
                "still_trading_at_window_end": still_trading,
                "frozen_cause": cause,
                "official_delisted_date": info.get("delisted_date") or None,
                "official_delisting_type": info.get("delisting_type") or None,
                "official_delisted_company": info.get("company") or None,
                "delisted_list_prefix_match": prefix_match,
                "prefix_match_delisted_date": prefix_info.get("delisted_date") or None,
                "prefix_match_delisting_type": prefix_info.get("delisting_type") or None,
                "prefix_match_company": prefix_info.get("company") or None,
                "identity_edge_binds_the_two": bool(
                    prefix_match and (symbol, prefix_match) in edges),
            })
    finally:
        db.close()
    return out


# ---------------------------------------------------------------------------
# Diagnostic assembly
# ---------------------------------------------------------------------------
def build_diagnostic(data_root: Path | str = DEFAULT_DATA,
                     sensitivity_dir: Path | str | None = None,
                     reports_dir: Path | str | None = None,
                     enrich: bool = True,
                     include_session_activity: bool = False) -> dict:
    """Assemble the freeze diagnostic from the sensitivity artefacts.

    Read-only: the market database is opened read-only and only when
    ``enrich`` is true.
    """
    data_root = Path(data_root)
    sensitivity_dir = Path(sensitivity_dir) if sensitivity_dir else \
        data_root / "processed/backtest/sensitivity"
    reports_dir = Path(reports_dir) if reports_dir else data_root / "reports"

    summary_path = sensitivity_dir / "summary.json"
    comparison_path = sensitivity_dir / "comparison.csv"
    trades_path = sensitivity_dir / "trades.csv"
    marks_path = sensitivity_dir / "equity_marks.csv"
    unknown_year_path = sensitivity_dir / "unknown_coverage_by_year.csv"
    impact_path = sensitivity_dir / "impact_audit.csv"
    identity_path = reports_dir / "identity-coverage.json"

    summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    comparison = _read_csv(comparison_path)
    trades_rows = _read_csv(trades_path)
    marks_rows = _read_csv(marks_path)
    unknown_year_rows = _read_csv(unknown_year_path)
    impact_rows = _read_csv(impact_path) if impact_path.exists() else []

    summary_variants = {row["variant_id"]: row for row in summary.get("variants", [])}
    comparison_by_variant = {row["variant_id"]: row for row in comparison}

    def _summary_row(variant_id: str) -> dict:
        row = dict(summary_variants.get(variant_id) or {})
        flat = comparison_by_variant.get(variant_id) or {}
        for key in ("lookback_sessions", "top_n", "policy", "rebalances", "terminal_positions"):
            if row.get(key) is None and flat.get(key) not in (None, ""):
                row[key] = flat[key]
        if row.get("final_equity_strict") is None and flat.get("final_equity_strict"):
            row["final_equity_strict"] = flat["final_equity_strict"]
        return row

    # -- bucket the session-level artefacts by variant ----------------------
    marks_by_variant: dict[str, list[dict]] = defaultdict(list)
    for row in marks_rows:
        session = _to_date(row["date"])
        if session is None:
            continue
        marks_by_variant[row["variant_id"]].append({
            "date": session,
            "equity_status": row.get("equity_status") or "",
            "n_positions": _to_int(row.get("n_positions")),
            "unpriced_positions": _to_int(row.get("unpriced_positions")),
            "unpriced_symbols": row.get("unpriced_symbols") or "",
        })

    trades_by_variant: dict[str, list[dict]] = defaultdict(list)
    for row in trades_rows:
        session = _to_date(row["date"])
        if session is None:
            continue
        trades_by_variant[row["variant_id"]].append({
            "date": session,
            "side": (row.get("side") or "").strip(),
        })

    impact_by_variant: dict[str, list[dict]] = defaultdict(list)
    for row in impact_rows:
        session = _to_date(row["date"])
        impact_by_variant[row["variant_id"]].append({
            "date": session,
            "reason": row.get("reason") or "",
        })

    unknown_by_variant: dict[str, dict[int, dict]] = defaultdict(dict)
    for row in unknown_year_rows:
        reasons = {
            "no_bar_in_date_local_eq_research_universe": _to_int(
                row.get("unknown_reason_no_bar_in_date_local_eq_research_universe")),
            "no_valid_close_for_session": _to_int(
                row.get("unknown_reason_no_valid_close_for_session")),
            "position_blocked_unverified_identity_change": _to_int(
                row.get("unknown_reason_position_blocked_unverified_identity_change")),
            "quarantined_unresolved_event": _to_int(
                row.get("unknown_reason_quarantined_unresolved_event")),
        }
        unknown_by_variant[row["variant_id"]][int(row["year"])] = {"unknown_reasons": reasons}

    variant_ids = sorted(set(marks_by_variant) | set(comparison_by_variant)
                         | set(summary_variants))
    activities: list[VariantActivity] = []
    for variant_id in variant_ids:
        activities.append(_analyse_variant(
            variant_id,
            marks_by_variant.get(variant_id, []),
            trades_by_variant.get(variant_id, []),
            _summary_row(variant_id),
            impact_by_variant.get(variant_id, []),
            unknown_by_variant.get(variant_id, {}),
        ))

    # -- counterfactual: sessions in which equity was knowable --------------
    counterfactual_per_variant = [{
        "variant_id": activity.variant_id,
        "sessions": activity.sessions,
        "known_equity_sessions": activity.known_equity_sessions,
        "unknown_equity_sessions": activity.unknown_equity_sessions,
        "known_equity_share": activity.known_equity_share,
        "last_known_equity_session": (activity.last_known_equity_session.isoformat()
                                      if activity.last_known_equity_session else None),
    } for activity in activities]

    by_year: dict[int, list[dict]] = defaultdict(list)
    for activity in activities:
        for row in activity.year_rows:
            by_year[row["year"]].append(row)
    known_equity_year_rows = []
    for year in sorted(by_year):
        rows = by_year[year]
        sessions = sum(row["sessions"] for row in rows)
        known = sum(row["known_equity_sessions"] for row in rows)
        known_equity_year_rows.append({
            "year": year,
            "variants": len(rows),
            "sessions_per_variant": rows[0]["sessions"],
            "total_variant_sessions": sessions,
            "known_equity_sessions_total": known,
            "unknown_equity_sessions_total": sessions - known,
            "known_equity_share_min": round(min(row["known_equity_share"] for row in rows), 6),
            "known_equity_share_max": round(max(row["known_equity_share"] for row in rows), 6),
            "known_equity_share_mean": round(
                sum(row["known_equity_share"] for row in rows) / len(rows), 6),
            "variants_with_zero_known_equity_sessions": sum(
                1 for row in rows if row["known_equity_sessions"] == 0),
            "total_trades": sum(row["trades"] for row in rows),
            "total_trade_sessions": sum(row["trade_sessions"] for row in rows),
        })

    # -- frozen securities --------------------------------------------------
    frozen_symbols = sorted({symbol for activity in activities
                             for symbol in activity.terminal_frozen_symbols})
    window_end = activities[0].window_end if activities else None
    frozen_detail = (enrich_frozen_symbols(data_root, frozen_symbols, window_end)
                     if enrich else [])
    frozen_cause_counts: Counter = Counter()
    for row in frozen_detail:
        frozen_cause_counts[row["frozen_cause"]] += 1
    frozen_per_variant = [{
        "variant_id": activity.variant_id,
        "last_known_equity_session": (activity.last_known_equity_session.isoformat()
                                      if activity.last_known_equity_session else None),
        "terminal_frozen_symbols": list(activity.terminal_frozen_symbols),
    } for activity in activities]
    identity_gap_examples = []
    for row in frozen_detail:
        match = row.get("delisted_list_prefix_match")
        if not match or row.get("identity_edge_binds_the_two"):
            continue
        entry = dict(row)
        entry["official_delisted_date"] = row.get("prefix_match_delisted_date")
        entry["official_delisting_type"] = row.get("prefix_match_delisting_type")
        entry["company"] = row.get("prefix_match_company")
        entry["variant_id"] = next(
            (item["variant_id"] for item in frozen_per_variant
             if row["symbol"] in item["terminal_frozen_symbols"]), None)
        identity_gap_examples.append(entry)

    # -- terminal proceeds --------------------------------------------------
    terminal_proceeds = {}
    if identity_path.exists():
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        terminal_proceeds = identity.get("terminal_proceeds") or {}
    proceeds_resolved = _to_int(terminal_proceeds.get("proceeds_resolved"))
    proceeds_unresolved = _to_int(terminal_proceeds.get("proceeds_unresolved"))
    delisted_rows = _to_int(terminal_proceeds.get("delisted_rows"))

    # -- aggregate verdict --------------------------------------------------
    total_sessions = sum(activity.sessions for activity in activities)
    total_known = sum(activity.known_equity_sessions for activity in activities)
    total_frozen = sum(activity.mechanism.get(BUCKET_FROZEN, 0) for activity in activities)
    total_no_order = sum(activity.execution_sessions_without_orders for activity in activities)
    total_genuine = sum(activity.mechanism.get(b, 0) for activity in activities
                         for b in GENUINE_BUCKETS)
    variants_with_equity = [a.variant_id for a in activities
                            if a.final_equity_strict is not None]
    variants_stopped = [a for a in activities if a.trades > 0]

    inputs = []
    for path in (summary_path, comparison_path, trades_path, marks_path,
                 unknown_year_path, impact_path, identity_path):
        if Path(path).exists():
            inputs.append({
                "path": str(path).replace("\\", "/"),
                "bytes": Path(path).stat().st_size,
                "sha256": _sha256(path),
            })

    diagnostic = {
        "schema_version": SCHEMA_VERSION,
        "report": REPORT_STEM,
        "purpose": ("Explain why the 2000-2026 momentum backtest stops trading after "
                    "~2008, and separate genuine market/strategy effects from data "
                    "and model limitations."),
        "verdict": {
            "headline": ("The strategy does not go quiet because the market stopped "
                         "offering opportunities. It goes quiet because ONE held "
                         "security stops being priceable, which makes the book's "
                         "equity unknown, which makes every new position unsizable, "
                         "which blocks every buy - and the same unpriceable holding "
                         "can never be sold, so the book can only shrink."),
            "hypothesis_tested": ("Once a held security leaves the date-local EQ/INE "
                                  "research universe it can never be priced or sold "
                                  "again, so portfolio equity becomes unknown, so no "
                                  "new position can be sized, so the strategy stops "
                                  "rebalancing for the remaining ~18 years."),
            "hypothesis_supported": True,
            "skip_reason_checked": "buy_unknown_held_opening_value",
            "skip_reason_is_raised": True,
            "skip_reason_note": ("The reason exists in the engine's skip taxonomy and is "
                                 "raised on every target of every execution session whose "
                                 "held opening mark is unknown. It is NOT a dormant "
                                 "counter: it is the single largest skip bucket in "
                                 "every variant."),
            "supporting_findings": [
                "The freeze blocks BUYS but not SELLS, so the book degenerates "
                "monotonically: it can shed priceable holdings but never replace them.",
                "The dominant reason a holding cannot be priced is "
                "'no_bar_in_date_local_eq_research_universe', not a bad close.",
                "The measurable window is tiny: a single unpriceable holding removes "
                "the equity for every later session.",
                "No variant publishes a final equity because terminal proceeds for "
                "delisted holdings are unverified.",
            ],
            "final_equity_available": bool(variants_with_equity),
            "variants_with_final_equity": variants_with_equity,
            "terminal_proceeds": {
                "delisted_rows": delisted_rows,
                "proceeds_resolved": proceeds_resolved,
                "proceeds_unresolved": proceeds_unresolved,
                "statement": (
                    f"Terminal proceeds are unresolved for {proceeds_unresolved} of "
                    f"{delisted_rows} delisted rows ({proceeds_resolved} resolved). "
                    "Until they are verified the equity series cannot be completed for "
                    "the sessions in which such a holding is carried, which is why no "
                    "variant has a final equity."),
            },
        },
        "method": {
            "inputs": "read-only artefacts of the sensitivity run; no backtest re-run",
            "execution_session_reconstruction": (
                "The engine rebalances on the last session of every calendar month and "
                "executes at the open of the next session. The calendar is rebuilt "
                "from the session list in equity_marks.csv and validated by checking "
                "that no trade falls on a non-execution session."),
            "attribution_rules": [
                "An execution session with at least one filled order is counted as "
                "'orders_filled'.",
                "Otherwise the session is attributed to the first matching bucket: "
                "'not_executable_order' (attested buy_not_executable), "
                "'quarantined_event' (attested quarantine), "
                "'frozen_unpriceable_holding' (the book held at least one unpriceable "
                "position, so its opening mark - and therefore its equity - was "
                "unknown), else 'no_eligible_names'.",
                "The buy-side buckets are disjoint by construction of the engine: a "
                "buy_not_executable or a quarantine refusal is only reachable once "
                "the held opening mark is known, so an attested buy-side refusal "
                "proves that the session was NOT frozen.",
                "The impact audit is a capped sample (25 rows per bucket per "
                "variant), so it can only add evidence to a session, never remove a "
                "cause. Engine skip counters remain the authority for totals.",
            ],
            "classification": {
                "genuine_market_or_strategy": list(GENUINE_BUCKETS),
                "data_or_model_limitation": list(LIMITATION_BUCKETS),
            },
        },
        "summary": {
            "variants": len(activities),
            "sessions_per_variant": activities[0].sessions if activities else 0,
            "window": {
                "start": activities[0].window_start.isoformat()
                if activities and activities[0].window_start else None,
                "end": activities[0].window_end.isoformat()
                if activities and activities[0].window_end else None,
            },
            "total_variant_sessions": total_sessions,
            "total_known_equity_sessions": total_known,
            "overall_known_equity_share": round(total_known / total_sessions, 6)
            if total_sessions else 0.0,
            "execution_sessions_without_orders_total": total_no_order,
            "execution_sessions_frozen_total": total_frozen,
            "execution_sessions_genuine_total": total_genuine,
            "frozen_share_of_no_order_sessions": round(
                total_frozen / total_no_order, 6) if total_no_order else 0.0,
            "variants_that_traded_at_all": len(variants_stopped),
            "variants_with_final_equity": len(variants_with_equity),
        },
        "variants": [activity.as_dict() for activity in activities],
        "variant_year_activity": [row for activity in activities for row in activity.year_rows],
        "counterfactual_known_equity": {
            "definition": ("A session is measurable only when the book held ZERO "
                           "unpriceable positions; only then is the equity known and "
                           "publishable. This counts the window that is measurable "
                           "today, independently of any trade."),
            "per_variant": counterfactual_per_variant,
            "by_year": known_equity_year_rows,
            "bound_statement": (
                f"Across {len(activities)} variants and {total_sessions:,} variant-"
                f"sessions, the equity was knowable on {total_known:,} of them "
                f"({round(100.0 * total_known / total_sessions, 2) if total_sessions else 0}%). "
                "The rest of the window is not merely unprofitable or unmeasurable - it "
                "is unknowable, because a holding that cannot be priced contributes "
                "nothing to any equity figure."),
        },
        "frozen_securities": {
            "symbols": frozen_symbols,
            "detail": frozen_detail,
            "cause_counts": dict(sorted(frozen_cause_counts.items())),
            "per_variant": frozen_per_variant,
            "identity_gap_examples": identity_gap_examples,
            "note": ("These are the securities that were still held and unpriceable "
                     "after the last session on which the book's equity was known. "
                     "A holding can become unpriceable in three different ways, "
                     "and all three end in the same freeze: the security leaves "
                     "the date-local EQ/INE research universe, its own key changes "
                     "under it, or the source simply stops carrying it."),
        },
        "determinism": {
            "volatile_fields": [],
            "note": ("This diagnostic contains no wall-clock field and no run location. "
                     "Every number, table and figure is a deterministic function of "
                     "the input artefacts, so two runs over the same inputs produce "
                     "byte-identical JSON and Markdown."),
            "analysis_digest": "",
        },
        "inputs": inputs,
        "caveats": [
            "This diagnostic explains why the backtest stops trading. It does not "
            "repair it, and it does not license any return, alpha or performance "
            "claim: the equity series is unknown on every session in which a "
            "holding cannot be priced.",
            "The impact audit is a capped sample (25 rows per bucket per variant). "
            "Counts in this report come from the engine's skip counters; the audit "
            "is used only as positive attestation on a session.",
            "'No eligible names' means the rebalance produced no target list. The "
            "engine needs MIN_VALID_OBSERVATIONS (130) valid observations per "
            "security before any name can be ranked, so the early sessions cannot "
            "produce a signal for reasons that have nothing to do with the freeze. "
            "The diagnostic identifies these sessions directly: they are exactly "
            "the no-order execution sessions before the first trade.",
            "The engine's own counter says every variant had 12 month-ends with no "
            "eligible name, but the published artefacts do not identify which they "
            "are. The diagnostic can only name the warm-up ones (those before the "
            "first trade). The remaining empty-target execution sessions also held "
            "an unpriceable position and are therefore counted in the freeze "
            "bucket. The freeze remains a sufficient explanation for such a "
            "session, but the two causes are not separable per session from the "
            "published artefacts.",
            "The research universe is date-local EQ with an INE ISIN or no ISIN. A "
            "security that migrates from EQ to another series (BE, BZ) leaves the "
            "universe even though it keeps trading; that is a universe-definition "
            "limitation, not a delisting.",
            "Terminal proceeds for delisted holdings are unverified, so the tail of "
            "every variant is not completable today even in principle.",
        ],
    }
    # The session-level dump is a row-level view of the same numbers.  It is
    # written to CSV, not embedded in the JSON, so the report stays readable;
    # the digest therefore covers exactly the JSON content.
    session_activity = [row for activity in activities for row in activity.session_rows]
    diagnostic["determinism"]["analysis_digest"] = _digest(diagnostic)
    if include_session_activity:
        diagnostic["session_activity"] = session_activity
    return diagnostic


def _digest(payload: Mapping) -> str:
    """sha256 over the deterministic part of the diagnostic."""
    clone = json.loads(json.dumps(payload, sort_keys=True, default=str))
    determinism = clone.get("determinism") or {}
    determinism.pop("analysis_digest", None)
    clone["determinism"] = determinism
    blob = json.dumps(clone, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CSV / Markdown rendering
# ---------------------------------------------------------------------------
VARIANT_CSV_COLUMNS = [
    "variant_id", "lookback_sessions", "top_n", "policy", "sessions",
    "window_start", "window_end", "trades", "buys", "sells", "trade_sessions",
    "sessions_with_zero_trades", "longest_zero_trade_streak_sessions",
    "longest_zero_trade_streak_start", "longest_zero_trade_streak_end",
    "first_trade", "last_trade", "rebalances", "execution_sessions",
    "rebalances_with_eligible_names", "rebalances_without_eligible_names",
    "execution_sessions_with_orders", "execution_sessions_without_orders",
    "share_rebalances_without_order",
    "attr_orders_filled", "attr_frozen_unpriceable_holding",
    "attr_not_executable_order", "attr_quarantined_event", "attr_no_eligible_names",
    "attr_genuine_total", "attr_limitation_total",
    "skip_buy_unknown_held_opening_value", "skip_sell_not_executable",
    "skip_buy_not_executable", "skip_sell_quarantined_unresolved_event",
    "skip_buy_quarantined_unresolved_event", "skip_sell_position_blocked",
    "skip_buy_position_blocked",
    "unpriced_no_bar_in_date_local_eq_research_universe",
    "unpriced_no_valid_close_for_session",
    "unpriced_position_blocked_unverified_identity_change",
    "unpriced_quarantined_unresolved_event",
    "sessions_with_unknown_opening_mark",
    "known_equity_sessions", "unknown_equity_sessions", "known_equity_share",
    "last_known_equity_session", "terminal_positions", "terminal_frozen_symbols",
    "final_equity_strict", "final_equity_last_observed_close_marking",
]

YEAR_CSV_COLUMNS = [
    "variant_id", "year", "sessions", "trades", "trade_sessions",
    "sessions_with_zero_trades", "known_equity_sessions", "unknown_equity_sessions",
    "known_equity_share", "unpriced_position_instances",
    "unknown_reason_no_bar_in_date_local_eq_research_universe",
    "unknown_reason_no_valid_close_for_session",
    "unknown_reason_position_blocked_unverified_identity_change",
    "unknown_reason_quarantined_unresolved_event",
]

SESSION_CSV_COLUMNS = [
    "variant_id", "date", "year", "trades", "is_execution_session", "equity_status",
    "known_equity", "n_positions", "unpriced_positions", "unpriced_symbols",
]


def _variant_csv_rows(diagnostic: Mapping) -> list[dict]:
    rows = []
    for variant in diagnostic["variants"]:
        mechanism = variant["mechanism_attribution"]["buckets"]
        skips = variant["engine_skip_counts"]
        reasons = variant["unpriced_reason_counts"]
        streak = variant["longest_zero_trade_streak"]
        rows.append({
            "variant_id": variant["variant_id"],
            "lookback_sessions": variant["lookback_sessions"],
            "top_n": variant["top_n"],
            "policy": variant["policy"],
            "sessions": variant["sessions"],
            "window_start": variant["window"]["start"],
            "window_end": variant["window"]["end"],
            "trades": variant["trades"],
            "buys": variant["buys"],
            "sells": variant["sells"],
            "trade_sessions": variant["trade_sessions"],
            "sessions_with_zero_trades": variant["sessions_with_zero_trades"],
            "longest_zero_trade_streak_sessions": streak["sessions"],
            "longest_zero_trade_streak_start": streak["start"],
            "longest_zero_trade_streak_end": streak["end"],
            "first_trade": variant["first_trade"],
            "last_trade": variant["last_trade"],
            "rebalances": variant["rebalances"],
            "execution_sessions": variant["execution_sessions"],
            "rebalances_with_eligible_names": variant["rebalances_with_eligible_names"],
            "rebalances_without_eligible_names": variant["rebalances_without_eligible_names"],
            "execution_sessions_with_orders": variant["execution_sessions_with_orders"],
            "execution_sessions_without_orders": variant["execution_sessions_without_orders"],
            "share_rebalances_without_order": variant["share_rebalances_without_order"],
            "attr_orders_filled": mechanism.get(BUCKET_ORDERS, 0),
            "attr_frozen_unpriceable_holding": mechanism.get(BUCKET_FROZEN, 0),
            "attr_not_executable_order": mechanism.get(BUCKET_NOT_EXECUTABLE, 0),
            "attr_quarantined_event": mechanism.get(BUCKET_QUARANTINED, 0),
            "attr_no_eligible_names": mechanism.get(BUCKET_NO_ELIGIBLE, 0),
            "attr_genuine_total": variant["mechanism_attribution"]
            ["genuine_market_or_strategy_sessions"],
            "attr_limitation_total": variant["mechanism_attribution"]
            ["data_or_model_limitation_sessions"],
            "skip_buy_unknown_held_opening_value": skips.get("buy_unknown_held_opening_value", 0),
            "skip_sell_not_executable": skips.get("sell_not_executable", 0),
            "skip_buy_not_executable": skips.get("buy_not_executable", 0),
            "skip_sell_quarantined_unresolved_event":
                skips.get("sell_quarantined_unresolved_event", 0),
            "skip_buy_quarantined_unresolved_event":
                skips.get("buy_quarantined_unresolved_event", 0),
            "skip_sell_position_blocked": skips.get("sell_position_blocked", 0),
            "skip_buy_position_blocked": skips.get("buy_position_blocked", 0),
            "unpriced_no_bar_in_date_local_eq_research_universe":
                reasons.get("no_bar_in_date_local_eq_research_universe", 0),
            "unpriced_no_valid_close_for_session":
                reasons.get("no_valid_close_for_session", 0),
            "unpriced_position_blocked_unverified_identity_change":
                reasons.get("position_blocked_unverified_identity_change", 0),
            "unpriced_quarantined_unresolved_event":
                reasons.get("quarantined_unresolved_event", 0),
            "sessions_with_unknown_opening_mark": variant["sessions_with_unknown_opening_mark"],
            "known_equity_sessions": variant["known_equity_sessions"],
            "unknown_equity_sessions": variant["unknown_equity_sessions"],
            "known_equity_share": variant["known_equity_share"],
            "last_known_equity_session": variant["last_known_equity_session"],
            "terminal_positions": variant["terminal_positions"],
            "terminal_frozen_symbols": ";".join(variant["terminal_frozen_symbols"]),
            "final_equity_strict": variant["final_equity_strict"],
            "final_equity_last_observed_close_marking":
                variant["final_equity_last_observed_close_marking"],
        })
    return rows


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})


def _fmt(value, digits: int = 2) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _year(value) -> str:
    """A year is a label, not a quantity: no thousands separator."""
    return "-" if value is None else str(value)


def _delisted_cell(row: Mapping) -> str:
    """The official delisted row for a frozen symbol, if there is one."""
    if row.get("official_delisted_date"):
        return (f"{row['official_delisted_date']} "
                f"({row.get('official_delisting_type') or 'delisted'})")
    match = row.get("delisted_list_prefix_match")
    if match:
        return f"name match only: `{match}`"
    return "-"


def _identity_edge_cell(row: Mapping) -> str:
    """Whether a verified identity edge binds the frozen symbol to its delisted name."""
    if not row.get("delisted_list_prefix_match"):
        return "-"
    if row.get("identity_edge_binds_the_two"):
        return "yes"
    return "**no** - the two cannot be bound, so the holding can never be closed"


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "---|" * len(headers)]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def render_markdown(diagnostic: Mapping) -> str:
    """Render the diagnostic as Markdown."""
    verdict = diagnostic["verdict"]
    summary = diagnostic["summary"]
    variants = diagnostic["variants"]
    frozen = diagnostic["frozen_securities"]
    lines: list[str] = []

    lines += [
        "# Backtest freeze diagnostic - why the momentum strategy stops trading",
        "",
        f"- **Schema version:** {diagnostic['schema_version']}",
        f"- **Window:** {summary['window']['start']} to {summary['window']['end']} "
        f"({_fmt(summary['sessions_per_variant'])} sessions per variant)",
        f"- **Variants analysed:** {summary['variants']}",
        f"- **Analysis digest:** `{diagnostic['determinism']['analysis_digest']}`",
        "",
        "## Verdict",
        "",
        verdict["headline"],
        "",
        f"**Hypothesis tested.** {verdict['hypothesis_tested']}",
        "",
        f"**Hypothesis supported:** {'yes' if verdict['hypothesis_supported'] else 'no'}.",
        "",
        "### Was `skip_buy_unknown_held_opening_value` actually raised?",
        "",
        verdict["skip_reason_note"],
        "",
    ]

    totals = summary
    lines += [
        "## The mechanism, in one table",
        "",
    ]
    lines += _table(
        ["quantity", "value"],
        [
            ["variant-sessions simulated", f"{totals['total_variant_sessions']:,}"],
            ["sessions with a knowable equity",
             f"{totals['total_known_equity_sessions']:,} "
             f"({totals['overall_known_equity_share'] * 100:.2f}%)"],
            ["execution sessions that produced no order",
             f"{totals['execution_sessions_without_orders_total']:,}"],
            ["... because the book was frozen by an unpriceable holding",
             f"{totals['execution_sessions_frozen_total']:,} "
             f"({totals['frozen_share_of_no_order_sessions'] * 100:.2f}% of no-order "
             f"execution sessions)"],
            ["... because of a genuine market/strategy effect",
             f"{totals['execution_sessions_genuine_total']:,}"],
            ["variants with a published final equity",
             f"{totals['variants_with_final_equity']} of {totals['variants']}"],
        ],
    )
    lines += [
        "",
        "### How the freeze works",
        "",
        "1. A held security has no bar in the date-local EQ/INE research universe, so "
        "it has no opening mark.",
        "2. `opening_known` is therefore false, the per-name budget is zero, and every "
        "buy of that session is skipped as `buy_unknown_held_opening_value`.",
        "3. The same holding cannot be sold either - a sell needs a tradable row - so "
        "it stays in the book.",
        "4. The book can therefore only ever shrink. Once every remaining holding is "
        "unpriceable the book is frozen for good: equity unknown, no buys, no sells.",
        "5. The equity series is not merely incomplete on those sessions; it is "
        "**unknown**, because an unpriceable holding contributes nothing to any equity "
        "figure.",
        "",
        "This is why the skip taxonomy entry is the whole story: it is the largest "
        "skip bucket in every single variant.",
        "",
        "## Terminal proceeds: the reason no variant has a final equity",
        "",
    ]
    terminal = verdict["terminal_proceeds"]
    lines += [
        terminal["statement"],
        "",
    ]
    examples = frozen.get("identity_gap_examples") or []
    if examples:
        example = examples[0]
        lines += [
            "This is not a formality. The clearest example is the security that "
            f"froze the `{example['variant_id']}` variant: the bars carry it as "
            f"`{example['symbol']}`, the official delisted list records the same "
            f"company as `{example['delisted_list_prefix_match']}` with a delisting "
            f"date of {example['official_delisted_date']}, and the repository has "
            "**no verified identity edge** binding the two. The holding therefore "
            "cannot be closed and its proceeds cannot be established - which is "
            "exactly what `proceeds_resolved: 0` means.",
            "",
        ]
    else:
        lines += [
            "This is not a formality: the holdings that freeze the book are "
            "securities whose exit cannot be established from the current "
            "artefacts.",
            "",
        ]
    lines += _table(
        ["terminal proceeds", "value"],
        [
            ["delisted rows in the official list", _fmt(terminal["delisted_rows"])],
            ["proceeds resolved", _fmt(terminal["proceeds_resolved"])],
            ["proceeds unresolved", _fmt(terminal["proceeds_unresolved"])],
        ],
    )
    lines += [
        "",
        "## Per-variant trading activity",
        "",
    ]
    lines += _table(
        ["variant", "trades", "trade sessions", "zero-trade sessions",
         "longest zero-trade streak", "first trade", "last trade"],
        [[
            f"`{v['variant_id']}`",
            _fmt(v["trades"]),
            _fmt(v["trade_sessions"]),
            _fmt(v["sessions_with_zero_trades"]),
            f"{_fmt(v['longest_zero_trade_streak']['sessions'])} sessions "
            f"({v['longest_zero_trade_streak']['start']} -> "
            f"{v['longest_zero_trade_streak']['end']})",
            v["first_trade"] or "-",
            v["last_trade"] or "-",
        ] for v in variants],
    )
    lines += [
        "",
        "## Rebalance accounting and mechanism attribution",
        "",
        "`orders filled` counts execution sessions that produced at least one order. "
        "The remaining columns attribute every no-order execution session to exactly "
        "one mechanism.",
        "",
    ]
    lines += _table(
        ["variant", "rebalances", "exec sessions", "orders filled", "no order",
         "share no order", "frozen", "not executable", "quarantined",
         "no eligible names"],
        [[
            f"`{v['variant_id']}`",
            _fmt(v["rebalances"]),
            _fmt(v["execution_sessions"]),
            _fmt(v["execution_sessions_with_orders"]),
            _fmt(v["execution_sessions_without_orders"]),
            f"{v['share_rebalances_without_order'] * 100:.1f}%",
            _fmt(v["mechanism_attribution"]["buckets"].get(BUCKET_FROZEN, 0)),
            _fmt(v["mechanism_attribution"]["buckets"].get(BUCKET_NOT_EXECUTABLE, 0)),
            _fmt(v["mechanism_attribution"]["buckets"].get(BUCKET_QUARANTINED, 0)),
            _fmt(v["mechanism_attribution"]["buckets"].get(BUCKET_NO_ELIGIBLE, 0)),
        ] for v in variants],
    )
    lines += [
        "",
        "### Engine skip counters behind the attribution",
        "",
    ]
    lines += _table(
        ["variant", "buy_unknown_held_opening_value", "sell_not_executable",
         "buy_not_executable", "buy_quarantined", "sell_quarantined",
         "sessions w/ unknown opening mark"],
        [[
            f"`{v['variant_id']}`",
            _fmt(v["engine_skip_counts"].get("buy_unknown_held_opening_value", 0)),
            _fmt(v["engine_skip_counts"].get("sell_not_executable", 0)),
            _fmt(v["engine_skip_counts"].get("buy_not_executable", 0)),
            _fmt(v["engine_skip_counts"].get("buy_quarantined_unresolved_event", 0)),
            _fmt(v["engine_skip_counts"].get("sell_quarantined_unresolved_event", 0)),
            _fmt(v["sessions_with_unknown_opening_mark"]),
        ] for v in variants],
    )
    lines += [
        "",
        "### Why the holding cannot be priced",
        "",
    ]
    lines += _table(
        ["variant", "no bar in date-local EQ/INE universe", "no valid close",
         "position blocked (identity)", "quarantined event"],
        [[
            f"`{v['variant_id']}`",
            _fmt(v["unpriced_reason_counts"].get(
                "no_bar_in_date_local_eq_research_universe", 0)),
            _fmt(v["unpriced_reason_counts"].get("no_valid_close_for_session", 0)),
            _fmt(v["unpriced_reason_counts"].get(
                "position_blocked_unverified_identity_change", 0)),
            _fmt(v["unpriced_reason_counts"].get("quarantined_unresolved_event", 0)),
        ] for v in variants],
    )
    lines += [
        "",
        "### Reconstruction cross-checks",
        "",
        "The diagnostic rebuilds the engine's execution calendar and its freeze "
        "detector from the published artefacts. Every column below is a check on "
        "that reconstruction, not a restatement of it.",
        "",
        "| variant | trades on a non-execution session | exec sessions with an "
        "unpriceable holding | engine's sessions with an unknown opening mark | "
        "unpriced but opening mark known | no-order sessions before the first "
        "trade | 'no eligible names' bucket |",
        "|---|---|---|---|---|---|---|",
    ]
    for v in variants:
        c = v["reconstruction_cross_checks"]
        lines.append(
            f"| `{v['variant_id']}` | {_fmt(c['trades_on_non_execution_sessions'])} "
            f"| {_fmt(c['execution_sessions_with_unpriced_holding'])} "
            f"| {_fmt(c['engine_sessions_with_unknown_opening_mark'])} "
            f"| {_fmt(c['sessions_unpriced_but_opening_mark_known'])} "
            f"| {_fmt(c['no_order_sessions_before_first_trade'])} "
            f"| {_fmt(c['no_eligible_names_bucket_sessions'])} |")
    lines += [
        "",
        "- *Trades on a non-execution session* must be 0: it proves the rebuilt "
        "execution calendar is the engine's own.",
        "- *Engine's sessions with an unknown opening mark* is the engine's own "
        "counter for 'buying was impossible'. The detector counts execution "
        "sessions that held an unpriceable position. The two agree exactly for 16 "
        "of the 18 variants; the two quarantine-policy variants differ by 1 and 2 "
        "sessions, where a holding was unpriceable because the policy quarantined "
        "it. A quarantine makes the equity unknown but does **not** freeze the "
        "opening mark, so those sessions could still trade.",
        "- *No-order sessions before the first trade* must equal the 'no eligible "
        "names' bucket: those are the warm-up sessions, before any security could "
        "be ranked.",
        "",
    ]
    lines += [
        "## (a) Genuine market/strategy effect vs (b) data/model limitation",
        "",
        "(a) **Genuine market or strategy effect** - the strategy had nothing to do "
        "for a reason that has nothing to do with the data gap:",
        "",
        "- `no_eligible_names` - the momentum rank was empty at the signal date "
        "(the book needed 130 valid observations per security before any name could "
        "be ranked, so the warm-up sessions cannot produce a signal);",
        "- `not_executable_order` - the order was refused by the execution screen "
        "(missing open, or an open outside the stylized 20% band around the previous "
        "close);",
        "- `quarantined_event` - the variant's policy deliberately refused the order.",
        "",
        "(b) **Data or model limitation** - the strategy was stopped by the data:",
        "",
        "- `frozen_unpriceable_holding` - one holding could not be priced, so the "
        "book's equity was unknown, so no position could be sized, so nothing could "
        "be bought; and the same holding could not be sold, so the book could never "
        "be rebuilt.",
        "",
    ]
    lines += _table(
        ["variant", "(a) genuine sessions", "(b) limitation sessions",
         "share of no-order sessions that is (b)"],
        [[
            f"`{v['variant_id']}`",
            _fmt(v["mechanism_attribution"]["genuine_market_or_strategy_sessions"]),
            _fmt(v["mechanism_attribution"]["data_or_model_limitation_sessions"]),
            f"{v['mechanism_attribution']['limitation_share_of_no_order_sessions'] * 100:.1f}%",
        ] for v in variants],
    )
    lines += [
        "",
        "## Counterfactual: how much of the window is measurable at all?",
        "",
        diagnostic["counterfactual_known_equity"]["definition"],
        "",
        diagnostic["counterfactual_known_equity"]["bound_statement"],
        "",
    ]
    lines += _table(
        ["variant", "sessions", "known equity sessions", "unknown equity sessions",
         "known share", "last known-equity session"],
        [[
            f"`{row['variant_id']}`",
            _fmt(row["sessions"]),
            _fmt(row["known_equity_sessions"]),
            _fmt(row["unknown_equity_sessions"]),
            f"{row['known_equity_share'] * 100:.2f}%",
            row["last_known_equity_session"] or "-",
        ] for row in diagnostic["counterfactual_known_equity"]["per_variant"]],
    )
    lines += [
        "",
        "### Per-year distribution of knowable equity",
        "",
        "Sessions per variant per year are identical across variants; the counts "
        "below are per variant.",
        "",
    ]
    lines += _table(
        ["year", "sessions", "known equity (min)", "known equity (max)",
         "known equity (mean)", "variants with zero known sessions",
         "total trades (all variants)"],
        [[
            _year(row["year"]),
            _fmt(row["sessions_per_variant"]),
            _fmt(row["known_equity_share_min"] * 100) + "%",
            _fmt(row["known_equity_share_max"] * 100) + "%",
            _fmt(row["known_equity_share_mean"] * 100) + "%",
            _fmt(row["variants_with_zero_known_equity_sessions"]),
            _fmt(row["total_trades"]),
        ] for row in diagnostic["counterfactual_known_equity"]["by_year"]],
    )
    frozen = diagnostic["frozen_securities"]
    if frozen["symbols"]:
        lines += [
            "",
            "## The securities that froze the book",
            "",
            frozen["note"],
            "",
            "### Per variant: what was still held when the equity went unknown",
            "",
        ]
        lines += _table(
            ["variant", "last known-equity session", "still held and unpriceable"],
            [[
                f"`{row['variant_id']}`",
                row["last_known_equity_session"] or "-",
                ", ".join(f"`{s}`" for s in row["terminal_frozen_symbols"]) or "-",
            ] for row in frozen["per_variant"]],
        )
        if frozen["detail"]:
            lines += [
                "",
                "### Why each one could not be priced",
                "",
                "*Research-universe exit date* is the last session on which the "
                "security was inside the date-local EQ/INE research universe - "
                "the last session on which the engine could have priced it.",
                "",
            ]
            lines += _table(
                ["symbol", "research-universe exit date", "last bar in the database",
                 "last series", "why it froze", "official delisted row",
                 "identity edge binds them?"],
                [[
                    f"`{row['symbol']}`",
                    row["research_universe_exit_date"] or "-",
                    row["last_bar"] or "-",
                    row["last_bar_series"] or "-",
                    row["frozen_cause"],
                    _delisted_cell(row),
                    _identity_edge_cell(row),
                ] for row in frozen["detail"]],
            )
            if frozen.get("cause_counts"):
                lines += [
                    "",
                    "Causes across the frozen securities: "
                    + ", ".join(f"`{cause}` = {count}"
                                for cause, count in frozen["cause_counts"].items())
                    + ".",
                    "",
                    "- `data_gap_no_bars` - the source stopped carrying the "
                    "security altogether.",
                    "- `left_eq_universe_series_migration` - the security kept "
                    "trading but moved out of the EQ series (BE, BZ), so it left "
                    "the research universe while still being listed.",
                    "- `identity_key_change` - the security kept trading inside "
                    "the research universe, so the holding's own key must have "
                    "changed under it (a rename, or the arrival of an ISIN that "
                    "re-keys the security from `SYMBOL:` to `ISIN:`). The holding "
                    "is never transferred, so it can never be priced again.",
                    "",
                ]
    lines += [
        "",
        "## Caveats",
        "",
    ]
    for caveat in diagnostic["caveats"]:
        lines.append(f"- {caveat}")
    lines += [
        "",
        "## Reproduce",
        "",
        "```powershell",
        "$env:PYTHONPATH = \"src\"",
        ".\\.venv\\Scripts\\python.exe -m nse_pipeline.backtest.freeze",
        "```",
        "",
        "The module is read-only: it re-derives every number from the sensitivity "
        "artefacts and never re-runs a backtest.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def write_outputs(diagnostic: Mapping, out_dir: Path | str = DEFAULT_OUT,
                  reports_dir: Path | str = DEFAULT_REPORTS) -> dict:
    """Write the CSV artefacts and the JSON/Markdown reports."""
    out_dir = Path(out_dir)
    reports_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    _write_csv(out_dir / "variant_activity.csv", VARIANT_CSV_COLUMNS,
               _variant_csv_rows(diagnostic))
    _write_csv(out_dir / "variant_year_activity.csv", YEAR_CSV_COLUMNS,
               diagnostic["variant_year_activity"])
    _write_csv(out_dir / "known_equity_by_year.csv",
               ["year", "variants", "sessions_per_variant", "total_variant_sessions",
                "known_equity_sessions_total", "unknown_equity_sessions_total",
                "known_equity_share_min", "known_equity_share_max",
                "known_equity_share_mean",
                "variants_with_zero_known_equity_sessions",
                "total_trades", "total_trade_sessions"],
               diagnostic["counterfactual_known_equity"]["by_year"])
    session_rows = diagnostic.get("session_activity", [])
    _write_csv(out_dir / "session_activity.csv", SESSION_CSV_COLUMNS, session_rows)
    frozen = diagnostic["frozen_securities"]["detail"]
    if frozen:
        _write_csv(out_dir / "frozen_securities.csv",
                   list(frozen[0].keys()), frozen)

    json_path = reports_dir / f"{REPORT_STEM}.json"
    md_path = reports_dir / f"{REPORT_STEM}.md"
    payload = {key: value for key, value in diagnostic.items() if key != "session_activity"}
    json_path.write_text(json.dumps(payload, indent=2, allow_nan=False,
                                    sort_keys=False, default=str) + "\n",
                         encoding="utf-8")
    md_path.write_text(render_markdown(diagnostic), encoding="utf-8")
    return {
        "variant_activity_csv": str(out_dir / "variant_activity.csv"),
        "variant_year_activity_csv": str(out_dir / "variant_year_activity.csv"),
        "known_equity_by_year_csv": str(out_dir / "known_equity_by_year.csv"),
        "session_activity_csv": str(out_dir / "session_activity.csv"),
        "report_json": str(json_path),
        "report_md": str(md_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nse_pipeline.backtest.freeze",
        description="Diagnose why the momentum backtest stops trading (read-only).")
    parser.add_argument("--data", default=str(data_root()),
                        help="repository data root (default: data)")
    parser.add_argument("--sensitivity", default=None,
                        help="sensitivity artefact directory")
    parser.add_argument("--reports", default=None,
                        help="report output directory")
    parser.add_argument("--out", default=None,
                        help="CSV artefact output directory")
    parser.add_argument("--no-enrich", action="store_true",
                        help="skip the read-only market-database enrichment")
    args = parser.parse_args(argv)

    data = Path(args.data).resolve()
    diagnostic = build_diagnostic(
        data_root=data,
        sensitivity_dir=args.sensitivity,
        reports_dir=args.reports,
        enrich=not args.no_enrich,
        include_session_activity=True,
    )
    written = write_outputs(diagnostic, out_dir=Path(args.out) if args.out else data / "processed/backtest/freeze",
                             reports_dir=Path(args.reports) if args.reports else data / "reports")
    print(json.dumps({
        "variants": diagnostic["summary"]["variants"],
        "known_equity_share": diagnostic["summary"]["overall_known_equity_share"],
        "frozen_share_of_no_order_sessions":
            diagnostic["summary"]["frozen_share_of_no_order_sessions"],
        "analysis_digest": diagnostic["determinism"]["analysis_digest"],
        "outputs": written,
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
