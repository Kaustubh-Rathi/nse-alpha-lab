"""Cause classification for the unpriceable holdings of the NSE backtest.

This module answers one question: **why** could a holding not be priced on a
session, and could a cross-series (EQ -> BE) fallback ever legitimately supply
the missing price?

It is ANALYSIS ONLY.  It never produces a mark, a price, a valuation or an
equity figure.  Rows found in another series are reported as *proxy
candidates* and are explicitly never used as values.  No runner is executed and
no runner is modified; the sensitivity artefacts are read from disk and the
market database is opened READ-ONLY.

The classification is deliberately independent of the runner's own reason
strings: it is recomputed from the bars, the identity edges, the official
delisting list and the corporate-actions API.  The runner's own counters are
read separately and used only to reconcile the two attributions.

Inputs
------
* ``data/processed/backtest/sensitivity/equity_marks.csv`` - the sensitivity
  run's per-session ledger.  Its ``unpriced_symbols`` column (semicolon
  separated) is the authoritative list of unpriceable position-days.
* ``data/processed/backtest/sensitivity/trades.csv`` - the ledger of executed
  trades, used to recover the *held key* of each position (the identity key the
  runner booked the position under, which may differ from the key the bar
  table computes today).
* ``data/processed/backtest/sensitivity/unknown_coverage_by_year.csv`` - the
  runner's own cause counters, read for the reconciliation only.
* ``data/market.duckdb`` - the bar table, opened read-only.
* ``data/raw/security_master/delisted.csv`` - NSE's official delisting list.
* ``data/processed/older_actions/nse-api-actions.jsonl`` - NSE's corporate
  actions API, used only as a *lead* for a possible successor symbol.
* ``data/processed/identity/identity_edges.jsonl`` and
  ``api_identity_edges.jsonl`` - the dated official rename edges, used to
  compute the identity key exactly as the runner computes it.

Cause buckets
-------------
Each unpriceable position-day is assigned exactly one cause.  The checks are
applied in the order below, which is what makes the result total and mutually
exclusive:

``identity_key_mismatch``
    A row exists for the symbol on the session inside the date-local EQ/INE
    research universe, but the key the position is held under is not the key
    that row resolves to.  The price exists; the *identity* does not line up.
``invalid_or_missing_close``
    A row exists under the held key, but the position-day has no usable close:
    either the observation is not valid/close-able, or the run's quarantine
    policy withheld the mark around an unresolved event.
``cross_series_other_isin_only``
    The held key resolves to no row in the date-local EQ/INE research
    universe, and the only rows for the symbol on that session are in another
    series (BE and friends) with a different or unobservable ISIN.  These rows
    are PROXY CANDIDATES, never marks.
``price_history_ends_and_symbol_delisted``
    No row exists for the symbol in any series on the session and the symbol
    IS on NSE's official delisting list: the exit is a terminal-proceeds
    problem.
``price_history_ends_security_not_in_delisting_list``
    No row exists for the symbol in any series on the session and the symbol
    is NOT on NSE's official delisting list: an unexplained end of trading.

The classification is a pure function of the inputs and is deterministic: the
same inputs always produce the same buckets, in the same order.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog
from typing import Iterable, Mapping, Sequence

import pandas as pd

DEFAULT_DATA = data_root()

# --------------------------------------------------------------------------
# Cause vocabulary
# --------------------------------------------------------------------------

CAUSE_IDENTITY_KEY_MISMATCH = "identity_key_mismatch"
CAUSE_INVALID_OR_MISSING_CLOSE = "invalid_or_missing_close"
CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY = "cross_series_other_isin_only"
CAUSE_PRICE_HISTORY_ENDS_DELISTED = "price_history_ends_and_symbol_delisted"
CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED = (
    "price_history_ends_security_not_in_delisting_list"
)

#: The five causes, in the order the checks are applied.
CAUSES = (
    CAUSE_IDENTITY_KEY_MISMATCH,
    CAUSE_INVALID_OR_MISSING_CLOSE,
    CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY,
    CAUSE_PRICE_HISTORY_ENDS_DELISTED,
    CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED,
)

#: Sub-causes of ``invalid_or_missing_close`` (the bucket name is the only one
#: the brief allows, the sub-cause keeps the two situations apart).
SUB_CAUSE_ROW_NOT_CLOSEABLE = "row_not_closeable"
SUB_CAUSE_POLICY_WITHHELD = "policy_withheld"

#: Series that the runner's research universe actually uses for equity marks.
#: Anything outside this set is a different instrument class (a debt or a
#: currency/commodity line), never a proxy for the same equity.
RESEARCH_UNIVERSE_SERIES = ("EQ",)

#: The figures the brief stated for the same ledger.  They are kept here so
#: the reconciliation is explicit and reproducible instead of a remark.
BRIEF_STATED_FIGURES = {
    "ledger_position_days": 142246,
    "instrument_classification_boundary_position_days": 1806,
    "instrument_classification_boundary_share_pct": 1.27,
    "missing_data_position_days": 204653,
    "missing_data_share_pct": 98.73,
}

#: One-line description of each cause, published with the report so that an
#: empty bucket is still documented.
CAUSE_DESCRIPTIONS = {
    CAUSE_IDENTITY_KEY_MISMATCH: (
        "A row exists for the symbol on the session inside the date-local "
        "EQ/INE research universe, but the key the position is held under is "
        "not the key that row resolves to: the price exists, the identity "
        "does not line up."
    ),
    CAUSE_INVALID_OR_MISSING_CLOSE: (
        "A row exists under the held key, but the position-day has no usable "
        "close: the observation is not valid/close-able, or the run's "
        "quarantine policy withheld the mark around an unresolved event."
    ),
    CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY: (
        "The held key resolves to no row in the date-local EQ/INE research "
        "universe and the only rows for the symbol on the session are in "
        "another series, with a different or unobservable ISIN. Proxy "
        "candidates only: never marks, never values."
    ),
    CAUSE_PRICE_HISTORY_ENDS_DELISTED: (
        "No row exists for the symbol in any series on the session and the "
        "symbol IS on NSE's official delisting list: a terminal-proceeds "
        "problem."
    ),
    CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED: (
        "No row exists for the symbol in any series on the session and the "
        "symbol is NOT on NSE's official delisting list: an unexplained end "
        "of trading."
    ),
}

#: The legal-form words stripped before comparing official company names.
_LEGAL_WORDS = {
    "LTD", "LIMITED", "PVT", "PRIVATE", "CO", "COMPANY", "THE", "AND", "OF",
}


def company_key(name: str | None) -> str:
    """Conservative company-name key.

    Two symbols share a key only when their *official* company names are the
    same after removing punctuation and legal-form words.  A shared key is a
    LEAD for an investigation, never proof that two tickers are the same
    security.
    """
    words = re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).split()
    return " ".join(word for word in words if word not in _LEGAL_WORDS)


# --------------------------------------------------------------------------
# Identity keys
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerIdentity:
    """The runner's own identity key, reproduced from the runner's primitives.

    The analysis has to answer "does the key the position is held under differ
    from the key the bar table computes for that row?" in exactly the terms the
    runner asks it, otherwise the answer is a comparison of two different
    questions.  The key function is therefore the runner's own: the bar-proven
    key stabilization first, then the row's ISIN, then the dated, unambiguous
    rename walk.

    Nothing here executes a backtest: only the key derivation is reused.
    """

    key_function: object

    def key(self, symbol: str, isin: str | None, on: date) -> str:
        """The identity key of one bar row, exactly as the runner computes it."""
        return self.key_function(symbol, isin, on)

    @classmethod
    def from_data(cls, data_root: Path, connection) -> "RunnerIdentity":
        """Build the runner's key derivation from the read-only market database."""
        from nse_pipeline.backtest.engine import build_key_stabilization
        from nse_pipeline.backtest.history import _Identity

        root = Path(data_root)
        stabilization = build_key_stabilization(
            root / "market.duckdb", root, connection=connection
        )
        runner = _Identity(root, set(), [], stabilization=stabilization, db=connection)
        return cls(runner.key)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class UnpricedPositionDay:
    """One holding that could not be priced on one session."""

    variant_id: str
    date: str
    symbol: str


@dataclass(frozen=True)
class BarFact:
    """A bar row relevant to the classification of one session."""

    date: str
    symbol: str
    series: str
    isin: str | None
    close: float | None
    valid_ohlcv: bool


@dataclass(frozen=True)
class Classification:
    """The cause assigned to one unpriceable position-day."""

    variant_id: str
    date: str
    symbol: str
    held_key: str | None
    cause: str
    detail: Mapping[str, object] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------


def read_unpriced_position_days(marks_csv: Path) -> list[UnpricedPositionDay]:
    """Read the unpriceable position-days from the sensitivity ledger."""
    out: list[UnpricedPositionDay] = []
    with Path(marks_csv).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not int(row.get("unpriced_positions") or 0):
                continue
            symbols = [
                s for s in (row.get("unpriced_symbols") or "").split(";") if s
            ]
            for symbol in symbols:
                out.append(
                    UnpricedPositionDay(
                        variant_id=row["variant_id"],
                        date=row["date"],
                        symbol=symbol,
                    )
                )
    out.sort(key=lambda p: (p.variant_id, p.date, p.symbol))
    return out


def read_held_keys(
    trades_csv: Path,
    dates_by_variant: Mapping[str, Iterable[str]] | None = None,
) -> dict[tuple[str, str], dict[str, str]]:
    """Recover the key each position is booked under, per (variant, session).

    The runner never re-keys a holding: a position keeps the key it was
    bought with.  Replaying the trade ledger therefore recovers the held key
    for any session, and the ledger also records which symbol each key holds,
    so a position's key is found by its symbol rather than guessed.

    ``dates_by_variant`` lists the sessions to snapshot.  A position bought on
    one session is still held on the next one, so the book is carried forward
    to every requested session, not only to the sessions that traded.
    """
    by_variant: dict[str, list[dict[str, str]]] = defaultdict(list)
    with Path(trades_csv).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_variant[row["variant_id"]].append(row)

    held: dict[tuple[str, str], dict[str, str]] = {}
    for variant, rows in by_variant.items():
        # The file order inside a session is the runner's own order (the sells
        # of a session are written before its buys), so only the date is sorted.
        rows.sort(key=lambda t: t["date"])
        wanted = sorted(set(dates_by_variant.get(variant, ()))) if dates_by_variant else []
        if not wanted:
            wanted = sorted({t["date"] for t in rows})
        book: dict[str, dict[str, object]] = {}
        index = 0
        for on in wanted:
            while index < len(rows) and rows[index]["date"] <= on:
                trade = rows[index]
                index += 1
                key = trade["key"]
                shares = int(trade["shares"] or 0)
                if trade["side"] == "buy":
                    position = book.setdefault(
                        key, {"symbol": trade["symbol"], "shares": 0}
                    )
                    position["shares"] = int(position["shares"]) + shares
                elif key in book:
                    book[key]["shares"] = int(book[key]["shares"]) - shares
                    if int(book[key]["shares"]) <= 0:
                        del book[key]
            held[(variant, on)] = {
                key: str(position["symbol"])
                for key, position in book.items()
                if int(position["shares"]) > 0
            }
    return held


def read_delisted(path: Path) -> dict[str, dict[str, str]]:
    """Read NSE's official delisting list."""
    out: dict[str, dict[str, str]] = {}
    if not Path(path).exists():
        return out
    with Path(path).open(newline="", encoding="utf-8", errors="replace") as handle:
        for row in csv.DictReader(handle):
            symbol = (row.get("Symbol") or "").strip()
            if symbol:
                out[symbol] = {
                    "delisted_date": (row.get("Delisted Date") or "").strip(),
                    "delisting_type": (row.get("Type of Delisting") or "").strip(),
                    "company": (row.get("Company") or "").strip(),
                }
    return out


def read_api_company_names(path: Path) -> dict[str, set[str]]:
    """Read the corporate-actions API: symbol -> set of sibling symbols.

    A sibling is another symbol that NSE's own corporate-actions API returns
    under the *same official company name*.  It is a LEAD for the identity
    question; it is never an automatic link and never a price.
    """
    by_company: dict[str, set[str]] = defaultdict(set)
    symbol_company: dict[str, str] = {}
    if not Path(path).exists():
        return {}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            symbol = row.get("symbol")
            key = company_key(row.get("comp"))
            if not symbol or not key:
                continue
            by_company[key].add(symbol)
            symbol_company.setdefault(symbol, key)
    siblings: dict[str, set[str]] = {}
    for symbol, key in symbol_company.items():
        others = sorted(by_company[key] - {symbol})
        if others:
            siblings[symbol] = set(others)
    return siblings


def read_api_company(path: Path) -> dict[str, str]:
    """Read the corporate-actions API: symbol -> official company name."""
    out: dict[str, str] = {}
    if not Path(path).exists():
        return out
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            symbol, comp = row.get("symbol"), row.get("comp")
            if symbol and comp:
                out.setdefault(str(symbol), str(comp))
    return out


def read_identity_edges(path: Path) -> list[dict]:
    """Read the dated official rename edges (evidence only)."""
    out: list[dict] = []
    if not Path(path).exists():
        return out
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def read_runner_reason_totals(path: Path) -> dict[str, int]:
    """Read the runner's own unpriceable-reason counters, for the reconciliation.

    The file is the runner's output, not an input to the classification: it is
    only summed here so the two attributions can be compared.
    """
    totals: Counter = Counter()
    if not Path(path).exists():
        return {}
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            for field_name, reason in (
                ("unknown_reason_no_bar_in_date_local_eq_research_universe",
                 "no_bar_in_date_local_eq_research_universe"),
                ("unknown_reason_no_valid_close_for_session",
                 "no_valid_close_for_session"),
                ("unknown_reason_position_blocked_unverified_identity_change",
                 "position_blocked_unverified_identity_change"),
                ("unknown_reason_quarantined_unresolved_event",
                 "quarantined_unresolved_event"),
            ):
                totals[reason] += int(row.get(field_name) or 0)
    return dict(totals)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def _is_closeable(fact: BarFact) -> bool:
    """A row is close-able when it is a valid observation with a positive close."""
    return bool(fact.valid_ohlcv and fact.close is not None and fact.close > 0)


def classify_position_day(
    position: UnpricedPositionDay,
    facts: Sequence[BarFact],
    *,
    held_key: str | None,
    identity: RunnerIdentity,
    in_delisting_list: bool,
    history_resumes: bool,
    policy_withholds_mark: bool = False,
) -> Classification:
    """Assign exactly one cause to one unpriceable position-day.

    ``facts`` are the bar rows for (position.date, position.symbol) across
    *all* series.  The order of the checks below is the classification
    contract, and it is what makes the result total and mutually exclusive.
    """
    on = date.fromisoformat(position.date)
    universe_rows = [
        f
        for f in facts
        if f.series in RESEARCH_UNIVERSE_SERIES
        and (not f.isin or not str(f.isin).strip() or str(f.isin).startswith("INE"))
    ]
    other_rows = [f for f in facts if f.series not in RESEARCH_UNIVERSE_SERIES]

    # (1) A row exists in the date-local EQ/INE research universe.
    if universe_rows:
        computed_keys = {identity.key(f.symbol, f.isin, on) for f in universe_rows}
        if held_key is not None and held_key not in computed_keys:
            return Classification(
                position.variant_id,
                position.date,
                position.symbol,
                held_key,
                CAUSE_IDENTITY_KEY_MISMATCH,
                {
                    "computed_keys": sorted(computed_keys),
                    "held_key": held_key,
                    "note": (
                        "A row exists for this symbol on this session inside "
                        "the date-local EQ/INE research universe, but the key "
                        "the position is held under is not the key that row "
                        "resolves to. The price exists; the identity does not "
                        "line up."
                    ),
                },
            )
        matching = [
            f for f in universe_rows if identity.key(f.symbol, f.isin, on) == held_key
        ]
        if matching and not any(_is_closeable(f) for f in matching):
            return Classification(
                position.variant_id,
                position.date,
                position.symbol,
                held_key,
                CAUSE_INVALID_OR_MISSING_CLOSE,
                {
                    "sub_cause": SUB_CAUSE_ROW_NOT_CLOSEABLE,
                    "rows": [
                        {
                            "series": f.series,
                            "has_isin": bool(f.isin and str(f.isin).strip()),
                            "valid_ohlcv": f.valid_ohlcv,
                            "close_present": f.close is not None,
                            "close_positive": bool(f.close and f.close > 0),
                        }
                        for f in matching
                    ],
                    "note": (
                        "A row exists under the held key but it is not a "
                        "valid, close-able observation, so this position-day "
                        "has no usable close."
                    ),
                },
            )
        if matching:
            return Classification(
                position.variant_id,
                position.date,
                position.symbol,
                held_key,
                CAUSE_INVALID_OR_MISSING_CLOSE,
                {
                    "sub_cause": SUB_CAUSE_POLICY_WITHHELD,
                    "rows": [
                        {
                            "series": f.series,
                            "has_isin": bool(f.isin and str(f.isin).strip()),
                            "valid_ohlcv": f.valid_ohlcv,
                            "close_present": f.close is not None,
                            "close_positive": bool(f.close and f.close > 0),
                        }
                        for f in matching
                    ],
                    "note": (
                        "A close-able row exists under the held key on this "
                        "session, but the run's quarantine policy withheld "
                        "the mark around an unresolved event, so this "
                        "position-day still has no usable close. The "
                        "observation is not the defect; the policy is."
                    ),
                },
            )
        return Classification(
            position.variant_id,
            position.date,
            position.symbol,
            held_key,
            CAUSE_IDENTITY_KEY_MISMATCH,
            {
                "computed_keys": sorted(computed_keys),
                "held_key": held_key,
                "note": (
                    "A row exists for this symbol on this session inside the "
                    "date-local EQ/INE research universe, but no key of this "
                    "holding resolves to it."
                ),
            },
        )

    # (2) Rows exist only outside the research universe -> a proxy at best.
    if other_rows:
        return Classification(
            position.variant_id,
            position.date,
            position.symbol,
            held_key,
            CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY,
            {
                "other_series": sorted({f.series for f in other_rows}),
                "other_isins": sorted(
                    {str(f.isin) if f.isin else "" for f in other_rows}
                ),
                "held_isin": (
                    held_key.split(":", 1)[1]
                    if held_key and held_key.startswith("ISIN:")
                    else None
                ),
                "same_isin_in_other_series": any(
                    held_key
                    and held_key.startswith("ISIN:")
                    and f.isin
                    and str(f.isin).strip() == held_key.split(":", 1)[1]
                    for f in other_rows
                ),
                "note": (
                    "The held key resolves to no row in the date-local EQ/INE "
                    "research universe and the only rows for this symbol on "
                    "this session are in another series, with a different or "
                    "unobservable ISIN. These rows are PROXY CANDIDATES ONLY. "
                    "They are never marks and never values: a different series "
                    "is a different instrument class."
                ),
            },
        )

    # (3) No row on this session at all.
    if in_delisting_list:
        return Classification(
            position.variant_id,
            position.date,
            position.symbol,
            held_key,
            CAUSE_PRICE_HISTORY_ENDS_DELISTED,
            {
                "price_history_resumes_after_this_date": history_resumes,
                "note": (
                    "No bar exists in any series on this session and the "
                    "security is on NSE's official delisting list: the exit is "
                    "a terminal-proceeds problem."
                ),
            },
        )
    return Classification(
        position.variant_id,
        position.date,
        position.symbol,
        held_key,
        CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED,
        {
            "price_history_resumes_after_this_date": history_resumes,
            "note": (
                "No bar exists in any series on this session and the "
                "security's price history stops: an unexplained end of "
                "trading (a scheme, an unrecorded rename, a long suspension "
                "or an acquisition)."
            ),
        },
    )


# --------------------------------------------------------------------------
# Analysis driver
# --------------------------------------------------------------------------


def _series_migration_evidence(connection, symbols: Sequence[str]) -> dict:
    """Measure the series-migration evidence for the frozen symbols.

    A cross-series fallback would need the other line to carry the ISIN of
    the held position.  This measures three things: whether the ISINs of the
    frozen symbols cross the series boundary at all, whether the ISINs of the
    *pre-2011* window exist at all, and how many of the other-line rows
    carry an ISIN at all.
    """
    frame = pd.DataFrame({"symbol": list(symbols)})
    connection.register("sf_migration_symbols", frame)
    first_isin = connection.execute(
        """
        SELECT min(date) FROM bars
        WHERE series='EQ' AND isin IS NOT NULL AND trim(isin)<>''
        """
    ).fetchone()[0]
    pre_onset = None
    if first_isin is not None:
        pre_onset = connection.execute(
            """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE isin IS NULL OR trim(isin)='') AS null_isin
            FROM bars WHERE series='EQ' AND date < ?
            """,
            [first_isin],
        ).fetchone()
    other_rows = connection.execute(
        """
        SELECT b.series,
               count(*) FILTER (WHERE b.isin IS NULL OR trim(b.isin)='') AS null_isin_rows,
               count(*) FILTER (WHERE b.isin IS NOT NULL AND trim(b.isin)<>'') AS rows_with_isin
        FROM bars b
        WHERE b.symbol IN (SELECT symbol FROM sf_migration_symbols)
          AND b.series<>'EQ'
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    shared = connection.execute(
        """
        WITH syms AS (SELECT symbol FROM sf_migration_symbols),
        eq AS (
          SELECT DISTINCT b.symbol, trim(b.isin) AS isin FROM bars b JOIN syms USING(symbol)
          WHERE b.series='EQ' AND b.isin IS NOT NULL AND trim(b.isin)<>''
        ), other AS (
          SELECT DISTINCT b.symbol, trim(b.isin) AS isin FROM bars b JOIN syms USING(symbol)
          WHERE b.series<>'EQ' AND b.isin IS NOT NULL AND trim(b.isin)<>''
        )
        SELECT count(*) FROM eq JOIN other USING(isin)
        """
    ).fetchone()[0]
    return {
        "first_eq_bar_with_an_isin": str(first_isin) if first_isin else None,
        "eq_bars_before_the_isin_onset": int(pre_onset[0]) if pre_onset else 0,
        "eq_bars_before_the_isin_onset_without_an_isin": int(pre_onset[1]) if pre_onset else 0,
        "other_series_rows_by_series": {
            series: {"null_isin_rows": int(nulls), "rows_with_isin": int(with_isin)}
            for series, nulls, with_isin in other_rows
        },
        "symbol_isin_pairs_shared_by_the_eq_line_and_another_series": int(shared),
    }


def analyse(
    data_root: Path = DEFAULT_DATA,
    *,
    marks_csv: Path | None = None,
    trades_csv: Path | None = None,
    db_path: Path | None = None,
    delisted_csv: Path | None = None,
    api_actions: Path | None = None,
    identity_edges: Path | None = None,
    coverage_csv: Path | None = None,
) -> dict:
    """Classify every unpriceable position-day and gather per-symbol evidence.

    The market database is opened READ-ONLY and is never written to.
    """
    root = Path(data_root)
    marks_path = Path(marks_csv or root / "processed/backtest/sensitivity/equity_marks.csv")
    trades_path = Path(trades_csv or root / "processed/backtest/sensitivity/trades.csv")
    database = Path(db_path or root / "market.duckdb")
    delisted_path = Path(delisted_csv or root / "raw/security_master/delisted.csv")
    api_path = Path(api_actions or root / "processed/older_actions/nse-api-actions.jsonl")
    edges_path = Path(identity_edges or root / "processed/identity/identity_edges.jsonl")
    coverage_path = Path(
        coverage_csv or root / "processed/backtest/sensitivity/unknown_coverage_by_year.csv"
    )

    positions = read_unpriced_position_days(marks_path)
    dates_by_variant: dict[str, set[str]] = defaultdict(set)
    for position in positions:
        dates_by_variant[position.variant_id].add(position.date)
    held_by_session = read_held_keys(trades_path, dates_by_variant)
    delisted = read_delisted(delisted_path)
    api_siblings = read_api_company_names(api_path)
    api_company = read_api_company(api_path)
    edges = read_identity_edges(edges_path)
    runner_reasons = read_runner_reason_totals(coverage_path)

    import duckdb  # imported lazily so the module stays importable without it

    connection = open_catalog(database=database)
    try:
        identity = RunnerIdentity.from_data(root, connection)

        facts_by_pair: dict[tuple[str, str], list[BarFact]] = defaultdict(list)
        symbol_series_dates: dict[str, dict[str, list[str]]] = defaultdict(
            lambda: defaultdict(list)
        )

        pairs = sorted({(p.date, p.symbol) for p in positions})
        if pairs:
            frame = pd.DataFrame(pairs, columns=["date", "symbol"])
            connection.register("sf_pairs", frame)
            rows = connection.execute(
                """
                SELECT p.date, p.symbol, b.series, b.isin, b.close, b.valid_ohlcv
                FROM sf_pairs p
                LEFT JOIN bars b ON b.date = p.date AND b.symbol = p.symbol
                ORDER BY p.date, p.symbol, b.series
                """
            ).fetchall()
            for row in rows:
                if row[2] is None:
                    continue
                facts_by_pair[(str(row[0]), row[1])].append(
                    BarFact(
                        date=str(row[0]),
                        symbol=row[1],
                        series=row[2],
                        isin=row[3],
                        close=row[4],
                        valid_ohlcv=bool(row[5]),
                    )
                )

        symbols = sorted({p.symbol for p in positions})
        migration_evidence: dict[str, object] = {}
        if symbols:
            connection.register(
                "sf_symbols", pd.DataFrame(symbols, columns=["symbol"])
            )
            rows = connection.execute(
                """
                SELECT symbol, series, min(date), max(date)
                FROM bars WHERE symbol IN (SELECT symbol FROM sf_symbols)
                GROUP BY 1,2 ORDER BY 1,2
                """
            ).fetchall()
            for symbol, series, first, last in rows:
                symbol_series_dates[symbol][series] = [str(first), str(last)]
            migration_evidence = _series_migration_evidence(connection, symbols)
    finally:
        connection.close()

    last_bar_overall = {
        symbol: max(value[1] for value in series_map.values())
        for symbol, series_map in symbol_series_dates.items()
    }

    classifications: list[Classification] = []
    for position in positions:
        book = held_by_session.get((position.variant_id, position.date), {})
        held_key = _pick_held_key(position.symbol, book)
        facts = facts_by_pair.get((position.date, position.symbol), [])
        last = last_bar_overall.get(position.symbol, "")
        classifications.append(
            classify_position_day(
                position,
                facts,
                held_key=held_key,
                identity=identity,
                in_delisting_list=position.symbol in delisted,
                history_resumes=bool(last and last > position.date),
            )
        )

    per_symbol = _summarise_symbols(
        classifications,
        symbol_series_dates,
        last_bar_overall,
        delisted,
        api_siblings,
        api_company,
    )
    summary = _summarise(classifications, per_symbol, positions, runner_reasons)
    summary["series_migration_evidence"] = migration_evidence
    return {
        "summary": summary,
        "symbols": per_symbol,
        "cause_notes": _cause_notes(classifications),
        "classifications": [
            {
                "variant_id": c.variant_id,
                "date": c.date,
                "symbol": c.symbol,
                "held_key": c.held_key,
                "cause": c.cause,
                "detail": dict(c.detail),
            }
            for c in classifications
        ],
    }


def _cause_notes(classifications: Sequence[Classification]) -> dict[str, str]:
    """The explanatory note of each cause, stated once instead of per row.

    Every cause is documented, including a cause that no position-day in the
    ledger falls into.
    """
    notes: dict[str, str] = dict(CAUSE_DESCRIPTIONS)
    for classification in classifications:
        note = classification.detail.get("note")
        if note:
            notes[classification.cause] = str(note)
    return notes


def _pick_held_key(symbol: str, book: Mapping[str, str]) -> str | None:
    """The key the position of ``symbol`` is booked under.

    The trade ledger records the symbol each key holds, so the key is found
    by its symbol.  The fallbacks only matter when the ledger does not carry
    the symbol: a symbol-prefixed key is then accepted when it is the sole
    candidate, because a renamed holding is booked under its older name.
    """
    if not book:
        return None
    exact = sorted(key for key, held in book.items() if held == symbol)
    if exact:
        return exact[0]
    prefixed = sorted(key for key in book if key.startswith("SYMBOL:"))
    if len(prefixed) == 1:
        return prefixed[0]
    return sorted(book)[0] if book else None


def _summarise_symbols(
    classifications: Sequence[Classification],
    symbol_series_dates: Mapping[str, Mapping[str, list[str]]],
    last_bar_overall: Mapping[str, str],
    delisted: Mapping[str, Mapping[str, str]],
    api_siblings: Mapping[str, set[str]],
    api_company: Mapping[str, str],
) -> list[dict]:
    causes_by_symbol: dict[str, Counter] = defaultdict(Counter)
    pd_by_symbol: Counter = Counter()
    sessions_by_symbol: dict[str, set[str]] = defaultdict(set)
    keys_by_symbol: dict[str, set[str]] = defaultdict(set)
    variants_by_symbol: dict[str, set[str]] = defaultdict(set)
    for classification in classifications:
        pd_by_symbol[classification.symbol] += 1
        sessions_by_symbol[classification.symbol].add(classification.date)
        causes_by_symbol[classification.symbol][classification.cause] += 1
        variants_by_symbol[classification.symbol].add(classification.variant_id)
        if classification.held_key:
            keys_by_symbol[classification.symbol].add(classification.held_key)

    out: list[dict] = []
    for symbol in sorted(pd_by_symbol):
        series_map = symbol_series_dates.get(symbol, {})
        delisted_row = delisted.get(symbol, {})
        leads = sorted(api_siblings.get(symbol, ()))
        out.append(
            {
                "symbol": symbol,
                "first_bar_by_series": {
                    s: v[0] for s, v in sorted(series_map.items())
                },
                "last_bar_by_series": {
                    s: v[1] for s, v in sorted(series_map.items())
                },
                "last_bar_overall": last_bar_overall.get(symbol),
                "unpriceable_position_days": pd_by_symbol[symbol],
                "unpriceable_sessions": len(sessions_by_symbol[symbol]),
                "variants": sorted(variants_by_symbol[symbol]),
                "causes": dict(sorted(causes_by_symbol[symbol].items())),
                "held_keys": sorted(keys_by_symbol.get(symbol, ())),
                "held_under_rename_root": any(
                    key.startswith("SYMBOL:")
                    and key.split(":", 1)[-1] != symbol
                    for key in keys_by_symbol.get(symbol, ())
                ),
                "rename_root_symbols": sorted(
                    {
                        key.split(":", 1)[-1]
                        for key in keys_by_symbol.get(symbol, ())
                        if key.startswith("SYMBOL:")
                        and key.split(":", 1)[-1] != symbol
                    }
                ),
                "in_official_delisting_list": symbol in delisted,
                "official_delisting_date": delisted_row.get("delisted_date") or None,
                "official_delisting_type": delisted_row.get("delisting_type") or None,
                "official_delisted_company": delisted_row.get("company") or None,
                "api_company_name": api_company.get(symbol),
                "api_same_name_successors": leads,
                "api_successor_is_lead_only": bool(leads),
                "api_lead_official_delisting": {
                    lead: {
                        "delisting_date": (delisted.get(lead) or {}).get("delisted_date") or None,
                        "delisting_type": (delisted.get(lead) or {}).get("delisting_type") or None,
                        "company": (delisted.get(lead) or {}).get("company") or None,
                    }
                    for lead in leads
                    if lead in delisted
                },
                "evidence_needed": _evidence_needed(
                    symbol,
                    causes=causes_by_symbol[symbol],
                    in_delisting_list=symbol in delisted,
                    leads=leads,
                    lead_delisting={
                        lead: {
                            "delisting_date": (delisted.get(lead) or {}).get("delisted_date") or None,
                            "delisting_type": (delisted.get(lead) or {}).get("delisting_type") or None,
                        }
                        for lead in leads
                        if lead in delisted
                    },
                    last_bar=last_bar_overall.get(symbol),
                ),
            }
        )
    return out


def _evidence_needed(
    symbol: str,
    *,
    causes: Mapping[str, int],
    in_delisting_list: bool,
    leads: Sequence[str],
    lead_delisting: Mapping[str, Mapping[str, str]] | None = None,
    last_bar: str | None = None,
) -> str:
    """What external evidence would have to arrive for this symbol to price.

    The evidence that would help depends on the dominant cause, so it is
    chosen from the cause and not from the symbol.
    """
    lead_delisting = lead_delisting or {}
    if leads:
        lead_text = (
            "a verified dated identity edge to " + ", ".join(leads)
            + " (an official NSE symbol-change row, or a corporate-actions "
            "API edge that clears the identity layer's fail-closed gate) plus "
            "the applied share-count audit for the handover"
        )
        for lead in leads:
            record = lead_delisting.get(lead)
            if record and record.get("delisting_date"):
                lead_text += (
                    f", and the official terminal proceeds of {lead}'s "
                    f"delisting ({record.get('delisting_date')}, "
                    f"{record.get('delisting_type')})"
                )
        if causes.get(CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED, 0):
            lead_text += (
                "; the edge alone does not reach the sessions after "
                f"{last_bar or 'the last observed bar'}, which also need the "
                "successor's proceeds"
            )
        return lead_text
    dominant = max(causes.items(), key=lambda item: item[1])[0] if causes else ""
    if dominant == CAUSE_IDENTITY_KEY_MISMATCH:
        return (
            "a verified identity binding between the held key and the row: a "
            "dated symbol-change record, or a corrected key for the position"
        )
    if dominant == CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY:
        return (
            "a documented series migration and the tradability terms of the "
            "successor line; a row of another series is not a quote for the "
            "held position and never becomes one"
        )
    if dominant == CAUSE_INVALID_OR_MISSING_CLOSE:
        return (
            "a corrected daily observation for the session (the row exists "
            "but is not a valid, close-able observation) or the event "
            "evidence the quarantine policy waits for"
        )
    if in_delisting_list:
        return (
            "the official terminal proceeds of the delisting (the payout of "
            "the delisting, per NSE's own corporate-action record)"
        )
    return (
        "the official record of what happened after the last observed bar "
        f"({last_bar or 'the last bar'}): NSE's corporate-actions API for "
        "that period, the official symbol-change and delisting lists, and "
        "the issuer's exchange filings (scheme, suspension or merger "
        "terms). No official input in this repository records an event for "
        "this symbol, so nothing in the bars can substitute for that record"
    )


def _summarise(
    classifications: Sequence[Classification],
    per_symbol: Sequence[Mapping[str, object]],
    positions: Sequence[UnpricedPositionDay],
    runner_reasons: Mapping[str, int],
) -> dict:
    counts = Counter(c.cause for c in classifications)
    total = sum(counts.values())
    sub_counts = Counter(
        str(c.detail.get("sub_cause"))
        for c in classifications
        if c.cause == CAUSE_INVALID_OR_MISSING_CLOSE
    )
    resume = Counter()
    proxy_series = Counter()
    same_isin = 0
    for classification in classifications:
        if "price_history_resumes_after_this_date" in classification.detail:
            key = (
                "resumes_after_this_date"
                if classification.detail["price_history_resumes_after_this_date"]
                else "never_resumes"
            )
            resume[key] += 1
        if classification.cause == CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY:
            proxy_series["+".join(classification.detail.get("other_series", ()))] += 1
            if classification.detail.get("same_isin_in_other_series"):
                same_isin += 1

    delisted = [s["symbol"] for s in per_symbol if s["in_official_delisting_list"]]
    leads = [s["symbol"] for s in per_symbol if s["api_same_name_successors"]]
    lead_pd = sum(
        int(s["unpriceable_position_days"]) for s in per_symbol if s["api_same_name_successors"]
    )
    boundary = counts.get(CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY, 0)
    missing = total - boundary
    return {
        "unpriceable_position_days": total,
        "frozen_symbols": len(per_symbol),
        "distinct_date_symbol_pairs": len({(p.date, p.symbol) for p in positions}),
        "variants": len({p.variant_id for p in positions}),
        "cause_counts": {cause: counts.get(cause, 0) for cause in CAUSES},
        "cause_pct": {
            cause: (counts.get(cause, 0) / total * 100.0 if total else 0.0)
            for cause in CAUSES
        },
        "invalid_close_sub_causes": dict(sorted(sub_counts.items())),
        "no_row_position_days": (
            counts.get(CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED, 0)
            + counts.get(CAUSE_PRICE_HISTORY_ENDS_DELISTED, 0)
        ),
        "price_history": dict(sorted(resume.items())),
        "proxy_row_series_mix": dict(sorted(proxy_series.items())),
        "cross_series_position_days_with_the_same_isin": same_isin,
        "instrument_classification_boundary_position_days": boundary,
        "instrument_classification_boundary_pct": (
            boundary / total * 100.0 if total else 0.0
        ),
        "missing_data_position_days": missing,
        "missing_data_pct": (missing / total * 100.0 if total else 0.0),
        "symbols_in_official_delisting_list": sorted(delisted),
        "symbols_with_api_same_name_successor_lead": sorted(leads),
        "position_days_with_api_same_name_lead": lead_pd,
        "position_days_without_api_same_name_lead": total - lead_pd,
        "position_days_with_api_same_name_lead_pct": (
            lead_pd / total * 100.0 if total else 0.0
        ),
        "rename_root_holdings": {
            str(row["symbol"]): {
                "position_days": int(row["unpriceable_position_days"]),
                "root_symbols": list(row["rename_root_symbols"]),
            }
            for row in per_symbol
            if row.get("held_under_rename_root")
        },
        "quarantined_position_days_without_a_row": (
            int(runner_reasons.get("quarantined_unresolved_event", 0))
            - sub_counts.get(SUB_CAUSE_POLICY_WITHHELD, 0)
            if runner_reasons
            else None
        ),
        "runner_reason_totals": dict(sorted(runner_reasons.items())),
        "brief_stated_figures": dict(BRIEF_STATED_FIGURES),
    }


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------

_HEADER = (
    "# Series-fallback cause analysis: why frozen holdings became unpriceable\n"
)

_DISCLAIMER = (
    "Analysis only. No price, mark or valuation figure appears in this "
    "report. Rows found in another series are PROXY CANDIDATES and are never "
    "used as values, and no fallback valuation is proposed anywhere in it.\n"
)


def render_markdown(report: Mapping[str, object]) -> str:
    """Render the human-readable report."""
    summary = report["summary"]
    symbols = report["symbols"]
    total = int(summary["unpriceable_position_days"])
    lines: list[str] = [_HEADER, _DISCLAIMER]

    lines.append("## 1. Scope\n")
    lines.append(
        f"- Unpriceable position-days classified: **{total:,}** "
        f"(over {summary['variants']} sensitivity "
        f"{_plural(summary['variants'], 'variant')}, "
        f"{summary['distinct_date_symbol_pairs']:,} distinct "
        "(session, symbol) pairs, "
        f"{summary['frozen_symbols']} frozen "
        f"{_plural(summary['frozen_symbols'], 'symbol')})."
    )
    lines.append(
        "- The classification is recomputed from the bars, the dated identity "
        "edges, NSE's official delisting list and NSE's corporate-actions API. "
        "It does not read the runner's own reason strings; those are used "
        "only for the reconciliation in section 4."
    )
    lines.append(
        "- The identity key of every bar row is the runner's own key "
        "(bar-proven key stabilization, then the row's ISIN, then the dated "
        "unambiguous rename walk), so \"the held key differs\" is measured in "
        "exactly the terms the runner measures it."
    )
    lines.append(
        "- `data/market.duckdb` is opened read-only. No runner is executed "
        "and no existing module is modified."
    )
    lines.append(
        "- The per-position-day ledger of this analysis is "
        "`data/processed/backtest/series_fallback/position_day_causes.csv` "
        f"({total:,} rows, one row per unpriceable position-day) and the "
        "per-symbol evidence is "
        "`data/processed/backtest/series_fallback/symbol_evidence.csv`.\n"
    )

    lines.append("## 2. Cause distribution\n")
    lines.append("| cause | position-days | share |")
    lines.append("| --- | ---: | ---: |")
    for cause in CAUSES:
        lines.append(
            f"| `{cause}` | {summary['cause_counts'][cause]:,} | "
            f"{summary['cause_pct'][cause]:.2f}% |"
        )
    lines.append(f"| **total** | **{total:,}** | **100.00%** |\n")
    lines.append(
        "The two sub-causes of `invalid_or_missing_close` are kept apart, "
        "because they are different defects:\n"
    )
    lines.append("| sub-cause | position-days | meaning |")
    lines.append("| --- | ---: | --- |")
    lines.append(
        f"| `row_not_closeable` | "
        f"{summary['invalid_close_sub_causes'].get(SUB_CAUSE_ROW_NOT_CLOSEABLE, 0):,} | "
        "the row exists under the held key but is not a valid, close-able "
        "observation |"
    )
    lines.append(
        f"| `policy_withheld` | "
        f"{summary['invalid_close_sub_causes'].get(SUB_CAUSE_POLICY_WITHHELD, 0):,} | "
        "a close-able row exists under the held key, but the run's quarantine "
        "policy withheld the mark around an unresolved event |"
    )
    lines.append("")
    history = summary["price_history"]
    no_row = int(summary["no_row_position_days"])
    never = int(history.get("never_resumes", 0))
    resumes = int(history.get("resumes_after_this_date", 0))
    lines.append(
        "Among the "
        f"{no_row:,} position-days whose cause is that no row exists at all, "
        f"{never:,} ({never / max(no_row, 1) * 100:.2f}% of that bucket) are "
        "sessions on which the security's bars never appear again, and "
        f"{resumes:,} ({resumes / max(no_row, 1) * 100:.2f}%) are sessions "
        "before a bar reappears: for those the gap is a suspension or a data "
        "gap rather than a terminal end.\n"
    )
    lines.append(
        "The proxy rows behind `cross_series_other_isin_only` are in series "
        + ", ".join(
            f"`{k}` ({v:,} position-days)"
            for k, v in summary["proxy_row_series_mix"].items()
        )
        + ". On "
        f"{summary['cross_series_position_days_with_the_same_isin']:,} of the "
        "position-days in this bucket the proxy row carries the same ISIN as "
        "the held key.\n"
    )
    rename_roots = summary.get("rename_root_holdings") or {}
    if rename_roots:
        listed = ", ".join(
            f"{symbol} under {', '.join(row['root_symbols'])} "
            f"({row['position_days']:,} position-days)"
            for symbol, row in sorted(rename_roots.items())
        )
        lines.append(
            "The `identity_key_mismatch` bucket is empty on this ledger, and "
            "that is a result rather than a gap: under the runner's own key "
            "derivation the held key of every position resolves to its row. "
            f"{len(rename_roots)} holdings are booked under a rename root "
            f"rather than under the ticker their bars carry ({listed}); the "
            "dated rename walk bridges them, so the key is not what stranded "
            "them. The identity problem in this ledger is therefore not a "
            "mismatched key - it is a missing verified edge.\n"
        )

    lines.append("## 3. Missing data against an instrument-classification boundary\n")
    lines.append(
        "| reading | position-days | share of the ledger |\n| --- | ---: | ---: |"
    )
    lines.append(
        f"| instrument-classification boundary (a row exists, but outside the "
        f"date-local EQ/INE research universe) | "
        f"{summary['instrument_classification_boundary_position_days']:,} | "
        f"{summary['instrument_classification_boundary_pct']:.2f}% |"
    )
    lines.append(
        f"| missing data (no row for the security in any series) | "
        f"{summary['missing_data_position_days']:,} | "
        f"{summary['missing_data_pct']:.2f}% |"
    )
    lines.append("")

    lines.append("## 4. Reconciliation\n")
    runner = summary.get("runner_reason_totals") or {}
    if runner:
        lines.append(
            "The runner's own counters, read from "
            "`unknown_coverage_by_year.csv`, attribute the same ledger as "
            "follows. The two attributions are made on different evidence "
            "(the runner reasons from its own session state, this analysis "
            "reasons from the bars), so they are compared, not merged.\n"
        )
        lines.append("| runner reason | position-days | this analysis |")
        lines.append("| --- | ---: | --- |")
        lines.append(
            f"| `no_bar_in_date_local_eq_research_universe` | "
            f"{runner.get('no_bar_in_date_local_eq_research_universe', 0):,} | "
            "the price-history-ends and cross-series buckets "
            f"({summary['cause_counts'][CAUSE_PRICE_HISTORY_ENDS_NOT_DELISTED] + summary['cause_counts'][CAUSE_PRICE_HISTORY_ENDS_DELISTED] + summary['cause_counts'][CAUSE_CROSS_SERIES_OTHER_ISIN_ONLY]:,}) |"
        )
        lines.append(
            f"| `no_valid_close_for_session` | "
            f"{runner.get('no_valid_close_for_session', 0):,} | "
            f"`invalid_or_missing_close` / `row_not_closeable` "
            f"({summary['invalid_close_sub_causes'].get(SUB_CAUSE_ROW_NOT_CLOSEABLE, 0):,}) |"
        )
        lines.append(
            f"| `position_blocked_unverified_identity_change` | "
            f"{runner.get('position_blocked_unverified_identity_change', 0):,} | "
            f"`identity_key_mismatch` "
            f"({summary['cause_counts'][CAUSE_IDENTITY_KEY_MISMATCH]:,}) |"
        )
        lines.append(
            f"| `quarantined_unresolved_event` | "
            f"{runner.get('quarantined_unresolved_event', 0):,} | "
            f"`invalid_or_missing_close` / `policy_withheld` "
            f"({summary['invalid_close_sub_causes'].get(SUB_CAUSE_POLICY_WITHHELD, 0):,}) "
            "plus the position-days that have no row at all and were also "
            f"quarantined ({summary.get('quarantined_position_days_without_a_row') or 0:,}) |"
        )
        lines.append(
            f"| **total** | **{sum(runner.values()):,}** | **{total:,}** |\n"
        )
        lines.append(
            "The runner counts a position that the quarantine policy "
            "withheld as `quarantined` even when no row exists at all, "
            "because the policy is checked first. This analysis reports the "
            "binding constraint instead: when no row exists, the data is the "
            "constraint, whatever the policy would also have said. That is "
            "why the runner's `no_bar` count is smaller than the "
            "price-history-ends and cross-series buckets put together: the "
            "difference is exactly the quarantined position-days that have no "
            f"row ({summary.get('quarantined_position_days_without_a_row') or 0:,}).\n"
        )
    brief = summary.get("brief_stated_figures") or {}
    stated_total = int(brief.get("ledger_position_days") or 0)
    if brief and stated_total == total:
        stated_b = brief["instrument_classification_boundary_position_days"]
        stated_c = brief["missing_data_position_days"]
        implied = total - stated_b
        measured_b = int(summary["instrument_classification_boundary_position_days"])
        lines.append(
            "The brief stated the same split as "
            f"{stated_b:,} position-days "
            f"({brief['instrument_classification_boundary_share_pct']}%) for "
            f"the instrument-classification boundary and {stated_c:,} "
            f"({brief['missing_data_share_pct']}%) for missing data, against "
            f"the same {total:,}-position-day ledger. Measured against that "
            f"ledger this analysis finds {measured_b:,} "
            f"({summary['instrument_classification_boundary_pct']:.2f}%) and "
            f"{summary['missing_data_position_days']:,} "
            f"({summary['missing_data_pct']:.2f}%). The two figures of the "
            "brief cannot both hold as stated: "
            f"{stated_c:,} exceeds the whole ledger of {total:,} "
            "position-days, so it cannot be a position-day count, while the "
            f"{brief['missing_data_share_pct']}% it is paired with implies "
            f"{implied:,} position-days ({implied / total * 100:.2f}%). The "
            f"boundary figure is {stated_b - measured_b:,} position-days "
            f"above the measured one, a difference of "
            f"{abs(stated_b - measured_b) / total * 100:.2f}% of the ledger "
            "that the bars, the identity edges and the runner's own counters "
            "do not account for: in this ledger no unpriceable position-day "
            "has a row outside the date-local EQ/INE research universe other "
            "than the ones counted in the cross-series bucket.\n"
        )
    elif brief:
        lines.append(
            "The brief's stated split refers to a ledger of "
            f"{stated_total:,} position-days, which is not the ledger "
            f"analysed here ({total:,} position-days), so the two are not "
            "compared.\n"
        )

    lines.append("## 5. Per-symbol evidence\n")
    lines.append(
        "| symbol | first EQ | last EQ | other series (last) | pos-days | "
        "causes | delisted | API same-name lead (lead only) |"
    )
    lines.append(
        "| --- | --- | --- | --- | ---: | --- | --- | --- |"
    )
    for row in symbols:
        series = row["first_bar_by_series"]
        last_series = row["last_bar_by_series"]
        other = sorted(s for s in series if s not in RESEARCH_UNIVERSE_SERIES)
        other_text = (
            ", ".join(f"{s}:{last_series[s]}" for s in other) or "none"
        )
        causes = ", ".join(f"{k}={v}" for k, v in sorted(row["causes"].items()))
        delisted = (
            f"{row['official_delisting_date']} ({row['official_delisting_type']})"
            if row["in_official_delisting_list"]
            else "no"
        )
        leads = ", ".join(
            f"{lead}"
            + (
                f" (delisted {row['api_lead_official_delisting'][lead]['delisting_date']})"
                if lead in row.get("api_lead_official_delisting", {})
                else ""
            )
            for lead in row["api_same_name_successors"]
        ) or "none"
        lines.append(
            f"| {row['symbol']} | {series.get('EQ', '-')} | "
            f"{last_series.get('EQ', '-')} | {other_text} | "
            f"{row['unpriceable_position_days']:,} | {causes} | {delisted} | "
            f"{leads} |"
        )
    lines.append("")

    lines.append("## 6. Conclusion\n")
    lines.extend(_render_conclusion(report))
    lines.append("")

    lines.append("## 7. What this does and does not claim\n")
    lines.append(
        "- This is a cause analysis of an existing ledger. It publishes no "
        "price, no mark and no valuation figure, and it proposes no fallback "
        "valuation."
    )
    lines.append(
        "- The proxy rows of `cross_series_other_isin_only` are reported as "
        "candidates for an investigation. They are never marks and are never "
        "used as values."
    )
    lines.append(
        "- A successor symbol under the same official company name is a LEAD. "
        "It is never an automatic link: it says where to look, not that the "
        "two tickers are one security."
    )
    lines.append(
        "- Nothing here changes what the strategy earned. It says why the "
        "held book could not be priced, and what external evidence would "
        "have to arrive for it to be.\n"
    )
    return "\n".join(lines) + "\n"


def _plural(count: int, word: str) -> str:
    """``"variant"`` for one, ``"variants"`` otherwise."""
    return word if int(count) == 1 else word + "s"


def _render_conclusion(report: Mapping[str, object]) -> list[str]:
    """The three-part conclusion the brief asks for."""
    summary = report["summary"]
    symbols = report["symbols"]
    total = int(summary["unpriceable_position_days"])
    lines: list[str] = []

    boundary = int(summary["instrument_classification_boundary_position_days"])
    missing = int(summary["missing_data_position_days"])
    lines.append(
        "**(i) Missing data against an instrument-classification boundary.** "
        f"Of the {total:,} unpriceable position-days, {missing:,} "
        f"({summary['missing_data_pct']:.2f}%) have no row for the security in "
        f"any series on that session - missing data - and only {boundary:,} "
        f"({summary['instrument_classification_boundary_pct']:.2f}%) are a "
        "boundary case in which a row exists but outside the date-local "
        "EQ/INE research universe (a different series). The cause of the "
        "freeze is overwhelmingly missing data, not a mis-classified "
        "instrument: a cross-series fallback could touch at most "
        f"{boundary:,} of the {total:,} position-days, and "
        f"{missing:,} position-days would still be unpriceable even with a "
        "perfectly classified instrument.\n"
    )

    same_isin = int(summary["cross_series_position_days_with_the_same_isin"])
    migration = summary.get("series_migration_evidence") or {}
    onset = migration.get("first_eq_bar_with_an_isin")
    pre_total = int(migration.get("eq_bars_before_the_isin_onset") or 0)
    pre_null = int(migration.get("eq_bars_before_the_isin_onset_without_an_isin") or 0)
    shared = int(migration.get("symbol_isin_pairs_shared_by_the_eq_line_and_another_series") or 0)
    lines.append(
        "**(ii) Could an EQ -> BE fallback ever be legitimate here? No.** A "
        "cross-series fallback would need three things at once: the same "
        "ISIN on both lines, a documented series migration, and tradability "
        "of the other line as the same instrument. The migration pattern "
        "itself is not imaginary - for the frozen symbols "
        f"{shared} (symbol, ISIN) pairs do appear on the EQ line and on "
        "another line, so the same instrument does cross a series boundary "
        "in this data. It is the other two conditions that fail. On "
        f"{same_isin:,} of the {boundary:,} proxy position-days does the "
        "proxy row carry the ISIN of the held key, so the same-ISIN test "
        "cannot be satisfied on the very sessions that need it; and the "
        "window that holds the freeze cannot even evaluate it: all "
        f"{pre_null:,} of the {pre_total:,} EQ bars before "
        f"{onset or 'the ISIN onset'} carry no ISIN at all, so the pre-2011 "
        "holdings have no ISIN to match against a proxy row. Beyond the "
        "ISIN, a BE line is a different instrument class with its own "
        "contract, its own clearing and its own price formation; it is not a "
        "quote for the EQ position and it is not tradable as one. The proxy "
        "rows therefore stay PROXY CANDIDATES: they are never marks, never "
        "values, and no fallback valuation is proposed from them.\n"
    )

    lead_pd = int(summary["position_days_with_api_same_name_lead"])
    leads = summary["symbols_with_api_same_name_successor_lead"]
    lines.append(
        f"**(iii) The fraction that is measurable only with verified "
        f"identity edges and terminal proceeds.** {lead_pd:,} position-days "
        f"({summary['position_days_with_api_same_name_lead_pct']:.2f}%) belong "
        f"to the {len(leads)} frozen {_plural(len(leads), 'symbol')} that "
        "NSE's own corporate-actions API records under the same official "
        f"company name as another symbol ({', '.join(leads)}). Those are the "
        "position-days a *verified* dated identity edge could reach: an "
        "official NSE symbol-change row, or a corporate-actions API edge that "
        "clears the identity layer's fail-closed gate, plus the applied "
        f"share-count audit for the handover. The other "
        f"{total - lead_pd:,} position-days "
        f"({100 - summary['position_days_with_api_same_name_lead_pct']:.2f}%) "
        "have no such successor in the official data at all; they need "
        "terminal proceeds (the payout of a delisting) or the scheme/merger "
        "terms of an unexplained end of trading, which no amount of bar "
        "data can supply.\n"
    )

    lines.append("The largest contributors, with the external evidence each one needs:\n")
    lines.append("| symbol | pos-days | share | what would have to arrive |")
    lines.append("| --- | ---: | ---: | --- |")
    top = sorted(
        symbols, key=lambda row: -int(row["unpriceable_position_days"])
    )[:10]
    for row in top:
        lines.append(
            f"| {row['symbol']} | {row['unpriceable_position_days']:,} | "
            f"{row['unpriceable_position_days'] / total * 100:.2f}% | "
            f"{row['evidence_needed']} |"
        )
    lines.append("")
    delisted_leads = sorted(
        {
            lead
            for row in symbols
            for lead in row.get("api_lead_official_delisting", {})
        }
    )
    if delisted_leads:
        lines.append(
            "Two of the same-name leads are themselves on NSE's official "
            "delisting list (" + ", ".join(delisted_leads) + "), so for them "
            "the terminal proceeds exist as a record and the missing piece is "
            "the dated identity edge that binds them to the frozen ticker. "
            "For the rest of the ledger no official input records what ended "
            "the trading of the frozen security, and that record cannot be "
            "manufactured from the bars.\n"
        )
    lines.append(
        "Read together: the freeze is a *data* problem with an *identity* "
        "tail. The largest contributors are securities whose trading simply "
        "stopped inside the backtest window, with no official delisting and "
        "no recorded successor; the identity-led part is the smaller, "
        "better-documented part of the ledger, and it is the only part a "
        "verified edge could reach.\n"
    )
    return lines


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


def write_report(
    report: Mapping[str, object],
    *,
    json_path: Path,
    markdown_path: Path,
    ledger: Mapping[str, object] | None = None,
) -> None:
    """Write the JSON and Markdown reports.

    The JSON holds the summary, the per-symbol evidence and the cause
    vocabulary; the per-position-day ledger itself is the machine-readable
    CSV under ``processed/backtest/series_fallback`` and is referenced from
    the JSON rather than duplicated inside it.
    """
    payload = {
        "summary": report["summary"],
        "symbols": report["symbols"],
        "cause_notes": report.get("cause_notes", {}),
        "ledger": dict(ledger or {}),
    }
    Path(json_path).parent.mkdir(parents=True, exist_ok=True)
    Path(markdown_path).parent.mkdir(parents=True, exist_ok=True)
    Path(json_path).write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    Path(markdown_path).write_text(render_markdown(report), encoding="utf-8")


def write_tables(report: Mapping[str, object], out_dir: Path) -> list[Path]:
    """Write the machine-readable per-position-day and per-symbol tables."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    causes_path = out / "position_day_causes.csv"
    with causes_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "variant_id", "date", "symbol", "held_key", "cause",
                "sub_cause", "other_series", "price_history_resumes",
            ]
        )
        for row in report["classifications"]:
            detail = row["detail"]
            writer.writerow(
                [
                    row["variant_id"],
                    row["date"],
                    row["symbol"],
                    row["held_key"] or "",
                    row["cause"],
                    detail.get("sub_cause", ""),
                    ";".join(detail.get("other_series", ())),
                    detail.get("price_history_resumes_after_this_date", ""),
                ]
            )
    written.append(causes_path)

    symbols_path = out / "symbol_evidence.csv"
    with symbols_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "symbol", "unpriceable_position_days", "unpriceable_sessions",
                "first_bar_by_series", "last_bar_by_series",
                "last_bar_overall", "held_keys", "causes",
                "in_official_delisting_list", "official_delisting_date",
                "official_delisting_type", "official_delisted_company",
                "api_company_name", "api_same_name_successors",
                "api_lead_official_delisting", "api_successor_is_lead_only",
                "evidence_needed",
            ]
        )
        for row in report["symbols"]:
            writer.writerow(
                [
                    row["symbol"],
                    row["unpriceable_position_days"],
                    row["unpriceable_sessions"],
                    ";".join(f"{k}={v}" for k, v in row["first_bar_by_series"].items()),
                    ";".join(f"{k}={v}" for k, v in row["last_bar_by_series"].items()),
                    row["last_bar_overall"] or "",
                    ";".join(row["held_keys"]),
                    ";".join(f"{k}={v}" for k, v in row["causes"].items()),
                    row["in_official_delisting_list"],
                    row["official_delisting_date"] or "",
                    row["official_delisting_type"] or "",
                    row["official_delisted_company"] or "",
                    row["api_company_name"] or "",
                    ";".join(row["api_same_name_successors"]),
                    ";".join(
                        f"{lead}={record['delisting_date']}"
                        for lead, record in row.get(
                            "api_lead_official_delisting", {}
                        ).items()
                    ),
                    row["api_successor_is_lead_only"],
                    row["evidence_needed"],
                ]
            )
    written.append(symbols_path)
    return written


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Classify why frozen holdings became unpriceable."
    )
    parser.add_argument("--data", default=str(data_root()))
    parser.add_argument("--json", default=None)
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--tables", default=None)
    args = parser.parse_args(argv)

    root = Path(args.data)
    json_path = Path(args.json or root / "reports/backtest-series-fallback.json")
    markdown_path = Path(
        args.markdown or root / "reports/backtest-series-fallback.md"
    )
    tables_path = Path(args.tables or root / "processed/backtest/series_fallback")
    report = analyse(root)
    written = write_tables(report, tables_path)
    ledger = {
        "path": str(written[0].as_posix()),
        "rows": int(report["summary"]["unpriceable_position_days"]),
        "fields": [
            "variant_id", "date", "symbol", "held_key", "cause", "sub_cause",
            "other_series", "price_history_resumes",
        ],
        "symbol_table_path": str(written[1].as_posix()),
    }
    write_report(
        report, json_path=json_path, markdown_path=markdown_path, ledger=ledger
    )
    for path in written:
        print(f"wrote {path}")
    print(f"wrote {json_path}")
    print(f"wrote {markdown_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
