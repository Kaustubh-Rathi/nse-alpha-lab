"""
Dated symbol/ISIN identity lineage from the official NSE symbol-change CSV,
enriched (never created) by read-only bars symbol/ISIN observations.

An identity edge exists ONLY where the official symbol-change file has a
row with old_symbol != new_symbol. No name-similarity, ISIN-only, or
price-move joins create edges. ISINs are assigned only from EQ/BE bars
intervals bracketing the effective date within a 365-day window; bars
carry no ISIN before 2011, so pre-2011 edges honestly keep null ISINs
(their first ISIN-bearing observations arrive years later, outside the
window). Symbols can be reused, so every join is date-bounded and
reuse/branching is flagged.

A second official source exists in the repository: NSE's own
corporate-actions API, which publishes renames the symbol-change file
does not. That layer is built by :mod:`nse_pipeline.identity.api_edges`
into its own file and is never merged into the official edges. This module
can publish the UNION of the two layers as an additional file
(``identity_edges_combined.jsonl``), where every edge is tagged with its
provenance (``edge_source``). The official ``identity_edges.jsonl`` is
never modified by that step and stays byte-identical.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
from datetime import date, datetime
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

SOURCE_FILENAME = "nse-symbolchange.csv"
SOURCE_LABEL = "NSE symbolchange.csv (official, data/raw/older_actions/nse-symbolchange.csv)"
EQBE_SERIES = ("EQ", "BE")
# An ISIN interval is assigned only if it ends/starts within this many
# days of the effective date. Longer brackets (e.g. a first ISIN-bearing
# observation in 2011 for a pre-2011 rename) are unresolved, not assigned:
# the security could have changed ISIN, or the symbol string been reused.
MAX_ISIN_BRACKET_GAP_DAYS = 365

# ---------------------------------------------------------------------------
# Merged identity layer: official symbol-change rows + corporate-actions API
# ---------------------------------------------------------------------------
#
# The provenance tags and the file names live with the combined-edge layer
# below (``EDGE_SOURCE_SYMBOLCHANGE`` / ``EDGE_SOURCE_CORPORATE_ACTIONS_API``,
# ``OFFICIAL_EDGES_FILENAME`` / ``API_EDGES_FILENAME``). What follows are the
# refusal codes of the fail-closed gate that decides whether an API-derived
# edge may remap a key at all.

#: Why an API-derived edge may not remap a key. Every code is fail-closed:
#: the edge is reported, never silently applied.
REFUSAL_UNDATED = "api_edge_not_dated"
REFUSAL_REUSE = "api_edge_crosses_symbol_reuse"
REFUSAL_DUPLICATE = "api_edge_duplicates_official_edge"
REFUSAL_BRANCH = "api_edge_would_create_branch_ambiguity"
REFUSAL_MERGE = "api_edge_would_create_merge_ambiguity"
REFUSAL_BARS_ORDER = "api_edge_bars_handover_not_ordered"
REFUSAL_EXCHANGE_RATIO = "successor_continuity_proved_but_exchange_ratio_undocumented"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_date(value: str | None) -> date | None:
    if value is None or value.strip() in ("", "-"):
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized source date: {value!r}")


def csv_rows(path: Path) -> list[dict]:
    """Decode legacy NSE CSVs (utf-8-sig, falling back to cp1252)."""
    content = path.read_bytes()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    return list(csv.DictReader(text.splitlines()))


def parse_symbolchange(path: Path) -> list[dict]:
    """Parse the official symbol-change file. Raises on bad dates/fields.

    Returns one dict per row (including old==new rows, which build_edges
    then excludes from edges). No row is dropped silently here.
    """
    rows = csv_rows(path)
    required = {"NAME OF THE COMPANY", "OLD SYMBOL", "NEW SYMBOL", "EFFECTIVE DATE"}
    parsed = []
    for i, row in enumerate(rows):
        if not required <= row.keys():
            raise ValueError(f"symbolchange file missing columns: {sorted(required - row.keys())}")
        old = (row.get("OLD SYMBOL") or "").strip()
        new = (row.get("NEW SYMBOL") or "").strip()
        if not old or not new:
            raise ValueError(f"symbolchange row {i} has blank old/new symbol")
        effective = parse_date(row.get("EFFECTIVE DATE"))
        if effective is None:
            raise ValueError(f"symbolchange row {i} has no effective date")
        parsed.append({
            "company": (row.get("NAME OF THE COMPANY") or "").strip(),
            "old_symbol": old,
            "new_symbol": new,
            "effective_date": effective.isoformat(),
            "source_row": i,
        })
    return parsed


def parse_delisted(path: Path) -> list[dict]:
    rows = []
    for row in csv_rows(path):
        symbol = (row.get("Symbol") or "").strip()
        if not symbol:
            continue
        when = parse_date(row.get("Delisted Date"))
        rows.append({
            "symbol": symbol,
            "company": (row.get("Company") or "").strip(),
            "delisted_date": when.isoformat() if when else None,
            "type": (row.get("Type of Delisting") or "").strip(),
        })
    return rows


def bars_observations(db: Path) -> dict:
    """Read-only DuckDB bars observations. Never writes to the database.

    ISIN intervals are scoped to EQ/BE series only: the same symbol string
    carries many debt/derivative ISINs under other series, which must not
    pollute equity identity. Symbol presence is reported for all series
    and for EQ/BE separately.
    """
    try:
        import duckdb
    except ImportError as error:  # pragma: no cover - declared dependency
        raise RuntimeError("duckdb is required to read bars observations") from error
    if not db.exists():
        raise FileNotFoundError(db)
    con = open_catalog(database=db)
    try:
        columns = {row[0] for row in con.execute("DESCRIBE bars").fetchall()}
        required = {"date", "symbol", "series", "isin"}
        if not required <= columns:
            raise ValueError(f"bars view lacks required columns: {sorted(required - columns)}")
        total, earliest, latest = con.execute(
            "SELECT count(*), min(date), max(date) FROM bars").fetchone()
        symbols: dict[str, dict] = {}
        for symbol, first, last, sessions, series_count in con.execute("""
            SELECT symbol, min(date), max(date), count(*), count(distinct series)
            FROM bars WHERE symbol IS NOT NULL GROUP BY symbol
        """).fetchall():
            symbols[symbol] = {
                "symbol": symbol,
                "first_observed": first.isoformat(),
                "last_observed": last.isoformat(),
                "sessions": sessions,
                "series_count": series_count,
                "eqbe_first_observed": None,
                "eqbe_last_observed": None,
                "eqbe_sessions": 0,
            }
        for symbol, first, last, sessions in con.execute("""
            SELECT symbol, min(date), max(date), count(*)
            FROM bars WHERE symbol IS NOT NULL AND series IN ('EQ','BE')
            GROUP BY symbol
        """).fetchall():
            symbols[symbol].update({
                "eqbe_first_observed": first.isoformat(),
                "eqbe_last_observed": last.isoformat(),
                "eqbe_sessions": sessions,
            })
        # (symbol, isin) EQ/BE intervals: one interval per pair (min/max dates).
        intervals: list[dict] = []
        for symbol, isin, first, last, sessions in con.execute("""
            SELECT symbol, isin, min(date), max(date), count(*)
            FROM bars WHERE symbol IS NOT NULL AND isin IS NOT NULL
              AND series IN ('EQ','BE')
            GROUP BY symbol, isin ORDER BY symbol, isin
        """).fetchall():
            intervals.append({
                "symbol": symbol, "isin": isin,
                "first_observed": first.isoformat(),
                "last_observed": last.isoformat(),
                "sessions": sessions,
            })
        # Last observed EQ/BE traded price per symbol (observation only).
        # Never an exit price: see terminal_proceeds_audit. Columns are
        # optional in older/test archives, so select only what exists.
        price_by_symbol: dict[str, dict] = {}
        if "close" in columns:
            valid_filter = " AND valid_ohlcv IS NOT FALSE" if "valid_ohlcv" in columns else ""
            source_expr = "source" if "source" in columns else "NULL"
            for symbol, pdate, close, series, isin, source in con.execute(f"""
                SELECT symbol, date, close, series, isin, {source_expr}
                FROM bars
                WHERE symbol IS NOT NULL AND series IN ('EQ','BE')
                  AND close IS NOT NULL{valid_filter}
                QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1
                ORDER BY symbol
            """).fetchall():
                price_by_symbol[symbol] = {
                    "symbol": symbol,
                    "price_date": pdate.isoformat(),
                    "close": close,
                    "series": series,
                    "isin": isin,
                    "source": source,
                    "verification": "eqbe_bars_last_observed_close_observation_only",
                    "warning": ("Last observed traded close in bars. It is a verified "
                                "observation, never a delisting exit price or proceeds."),
                }
                if symbol in symbols:
                    symbols[symbol].update({
                        "last_eqbe_price_date": pdate.isoformat(),
                        "last_eqbe_close": close,
                        "last_eqbe_series": series,
                        "last_eqbe_isin": isin,
                        "last_eqbe_price_source": source,
                    })
    finally:
        con.close()
    by_symbol: dict[str, list[dict]] = collections.defaultdict(list)
    for interval in intervals:
        by_symbol[interval["symbol"]].append(interval)
    isin_symbols: dict[str, set[str]] = collections.defaultdict(set)
    for interval in intervals:
        isin_symbols[interval["isin"]].add(interval["symbol"])
    return {
        "source": str(db),
        "read_only": True,
        "bars_rows": total,
        "bars_earliest": earliest.isoformat() if earliest else None,
        "bars_latest": latest.isoformat() if latest else None,
        "symbols": symbols,
        "isin_intervals": intervals,
        "isin_intervals_by_symbol": dict(by_symbol),
        "isin_to_symbols": {isin: sorted(syms) for isin, syms in isin_symbols.items()},
        "last_eqbe_price_by_symbol": price_by_symbol,
    }


def _pick_old(intervals: list[dict], effective: date) -> tuple[str | None, dict | None, bool]:
    """Latest-ending EQ/BE interval ending on/before effective date.

    Returns (isin, interval, ambiguous). Null when no such interval exists
    (e.g. pre-2011, where bars carry no ISIN) or when the latest ending
    date is tied across distinct ISINs.
    """
    candidates = [iv for iv in intervals
                  if datetime.strptime(iv["last_observed"], "%Y-%m-%d").date() <= effective]
    if not candidates:
        return None, None, False
    best_last = max(iv["last_observed"] for iv in candidates)
    winners = {iv["isin"] for iv in candidates if iv["last_observed"] == best_last}
    if len(winners) != 1:
        return None, None, True
    isin = next(iter(winners))
    interval = next(iv for iv in candidates if iv["isin"] == isin and iv["last_observed"] == best_last)
    return isin, interval, False


def _pick_new(intervals: list[dict], effective: date) -> tuple[str | None, dict | None, bool]:
    """Earliest-starting EQ/BE interval starting on/after effective date."""
    candidates = [iv for iv in intervals
                  if datetime.strptime(iv["first_observed"], "%Y-%m-%d").date() >= effective]
    if not candidates:
        return None, None, False
    best_first = min(iv["first_observed"] for iv in candidates)
    winners = {iv["isin"] for iv in candidates if iv["first_observed"] == best_first}
    if len(winners) != 1:
        return None, None, True
    isin = next(iter(winners))
    interval = next(iv for iv in candidates if iv["isin"] == isin and iv["first_observed"] == best_first)
    return isin, interval, False


def assign_isin(old_intervals: list[dict], new_intervals: list[dict],
                effective: date) -> dict:
    """Date-bounded ISIN assignment for one edge. Nulls are honest answers.

    Candidates outside the 365-day bracket window are rejected (null),
    with the candidate gap preserved so the rejection is auditable.
    """
    cand_old, old_iv, old_amb = _pick_old(old_intervals, effective)
    cand_new, new_iv, new_amb = _pick_new(new_intervals, effective)
    old_gap = (effective - datetime.strptime(old_iv["last_observed"], "%Y-%m-%d").date()).days if old_iv else None
    new_gap = (datetime.strptime(new_iv["first_observed"], "%Y-%m-%d").date() - effective).days if new_iv else None
    isin_old = cand_old if old_gap is not None and old_gap <= MAX_ISIN_BRACKET_GAP_DAYS else None
    isin_new = cand_new if new_gap is not None and new_gap <= MAX_ISIN_BRACKET_GAP_DAYS else None
    old_active = any(iv["first_observed"] <= effective.isoformat() <= iv["last_observed"]
                     for iv in old_intervals)
    new_active_early = any(iv["first_observed"] < effective.isoformat() for iv in new_intervals)
    if isin_old is not None and isin_new is not None:
        change: bool | None = isin_old != isin_new
    else:
        change = None
    return {
        "isin_old": isin_old,
        "isin_new": isin_new,
        "isin_change": change,
        "isin_old_ambiguous": old_amb,
        "isin_new_ambiguous": new_amb,
        "isin_old_gap_days": old_gap,
        "isin_new_gap_days": new_gap,
        "isin_old_within_bracket_window": isin_old is not None,
        "isin_new_within_bracket_window": isin_new is not None,
        "old_symbol_active_on_effective_eqbe": old_active,
        "new_symbol_observed_before_effective_eqbe": new_active_early,
    }


def build_edges(change_rows: list[dict], observations: dict | None) -> tuple[list[dict], list[dict]]:
    """Official edges (old != new only) + excluded old==new rows.

    No other input creates an edge: company-name similarity between rows
    is deliberately never consulted.
    """
    symbols = observations["symbols"] if observations else {}
    by_symbol = observations["isin_intervals_by_symbol"] if observations else {}
    rename_rows = [r for r in change_rows if r["old_symbol"] != r["new_symbol"]]
    excluded = [r for r in change_rows if r["old_symbol"] == r["new_symbol"]]

    successors: dict[str, set[str]] = collections.defaultdict(set)
    predecessors: dict[str, set[str]] = collections.defaultdict(set)
    for row in rename_rows:
        successors[row["old_symbol"]].add(row["new_symbol"])
        predecessors[row["new_symbol"]].add(row["old_symbol"])

    edges = []
    for row in rename_rows:
        effective = datetime.strptime(row["effective_date"], "%Y-%m-%d").date()
        isin_info = assign_isin(by_symbol.get(row["old_symbol"], []),
                                by_symbol.get(row["new_symbol"], []), effective)
        old_obs = symbols.get(row["old_symbol"])
        new_obs = symbols.get(row["new_symbol"])
        old_before = bool(old_obs and old_obs["first_observed"] <= row["effective_date"])
        old_only_after = bool(old_obs and old_obs["first_observed"] > row["effective_date"])
        new_from = bool(new_obs and new_obs["last_observed"] >= row["effective_date"])
        new_only_before = bool(new_obs and new_obs["last_observed"] < row["effective_date"])
        edges.append({
            "edge_id": f"{row['old_symbol']}->{row['new_symbol']}@{row['effective_date']}#{row['source_row']}",
            "old_symbol": row["old_symbol"],
            "new_symbol": row["new_symbol"],
            "company": row["company"],
            "effective_date": row["effective_date"],
            "isin_old": isin_info["isin_old"],
            "isin_new": isin_info["isin_new"],
            "isin_change": isin_info["isin_change"],
            "isin_old_ambiguous": isin_info["isin_old_ambiguous"],
            "isin_new_ambiguous": isin_info["isin_new_ambiguous"],
            "isin_old_gap_days": isin_info["isin_old_gap_days"],
            "isin_new_gap_days": isin_info["isin_new_gap_days"],
            "isin_old_within_bracket_window": isin_info["isin_old_within_bracket_window"],
            "isin_new_within_bracket_window": isin_info["isin_new_within_bracket_window"],
            "source": SOURCE_LABEL,
            "source_row": row["source_row"],
            "verification": "official_symbolchange_row",
            "bars_old": old_obs,
            "bars_new": new_obs,
            "bars_old_observed_on_or_before_effective": old_before,
            "bars_new_observed_on_or_after_effective": new_from,
            "bars_order_violation": old_only_after or new_only_before,
            "reuse_flags": {
                "old_symbol_has_multiple_successors": len(successors[row["old_symbol"]]) > 1,
                "new_symbol_has_multiple_predecessors": len(predecessors[row["new_symbol"]]) > 1,
                "old_symbol_is_also_a_new_symbol": row["old_symbol"] in predecessors,
                "new_symbol_is_also_an_old_symbol": row["new_symbol"] in successors,
                "old_symbol_active_on_effective_eqbe": isin_info["old_symbol_active_on_effective_eqbe"],
                "new_symbol_observed_before_effective_eqbe": isin_info["new_symbol_observed_before_effective_eqbe"],
            },
            "note": ("ISIN from EQ/BE bars intervals bracketing the effective date only; "
                     "null when absent or ambiguous. No name-similarity join."),
        })
    edges.sort(key=lambda e: (e["effective_date"], e["old_symbol"], e["new_symbol"], e["source_row"]))
    return edges, excluded


def sequential_steps(intervals: list[dict], key: str) -> list[dict] | None:
    """Dated steps if intervals are strictly sequential, else None.

    Strictly sequential means sorted by first_observed, each step starts
    after the previous ends, and no two intervals share the same start.
    Overlapping/interleaved intervals are ambiguous (reuse/correction) and
    return None so the caller keeps them unresolved. This never creates a
    symbol edge: it only orders already-observed EQ/BE intervals.
    """
    ordered = sorted(intervals, key=lambda iv: (iv["first_observed"], iv[key], iv["last_observed"]))
    values = [iv[key] for iv in ordered]
    if len(set(values)) != len(values) or len(ordered) < 2:
        return None
    steps = []
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt["first_observed"] <= prev["last_observed"]:
            return None
        gap = (datetime.strptime(nxt["first_observed"], "%Y-%m-%d").date()
               - datetime.strptime(prev["last_observed"], "%Y-%m-%d").date()).days
        steps.append({
            "from": prev[key], "to": nxt[key],
            "from_first_observed": prev["first_observed"],
            "from_last_observed": prev["last_observed"],
            "to_first_observed": nxt["first_observed"],
            "to_last_observed": nxt["last_observed"],
            "gap_days": gap,
        })
    return steps


def build_resolved_observations(edges: list[dict], observations: dict | None) -> list[dict]:
    """Conservative observation-only resolutions (never symbol edges).

    Two unambiguous patterns are resolved as dated observations, not edges:
    (1) one symbol with strictly sequential EQ/BE ISIN intervals -> an ISIN
    transition under that symbol; (2) one ISIN with strictly sequential
    EQ/BE symbol intervals and no official edge for a pair -> a dated symbol
    sequence lead. Overlapping patterns stay unresolved. Company-name
    similarity and latest-ISIN joins are never used.
    """
    if observations is None:
        return []
    edge_pairs = {(e["old_symbol"], e["new_symbol"]) for e in edges}
    edge_symbols = {e["old_symbol"] for e in edges} | {e["new_symbol"] for e in edges}
    resolved: list[dict] = []
    for symbol, intervals in sorted(observations["isin_intervals_by_symbol"].items()):
        if len({iv["isin"] for iv in intervals}) <= 1 or symbol in edge_symbols:
            continue
        steps = sequential_steps(intervals, "isin")
        if steps is None:
            continue
        resolved.append({
            "kind": "isin_transition_same_symbol",
            "symbol": symbol,
            "isins": [iv["isin"] for iv in sorted(intervals, key=lambda x: x["first_observed"])],
            "steps": steps,
            "intervals": sorted(intervals, key=lambda x: x["first_observed"]),
            "verification": "eqbe_bars_intervals_sequential_observation_only",
            "promoted_to_symbol_edge": False,
            "warning": ("ISIN changed under one symbol (ISIN change/correction/series "
                        "migration possible). This is not a symbol rename and creates no edge."),
        })
    for isin, syms in sorted(observations["isin_to_symbols"].items()):
        if len(syms) <= 1:
            continue
        unlinked = [pair for pair in [(a, b) for a in syms for b in syms if a != b]
                    if pair not in edge_pairs and (pair[1], pair[0]) not in edge_pairs]
        if not unlinked:
            continue
        intervals = [iv for iv in observations.get("isin_intervals", []) if iv["isin"] == isin]
        steps = sequential_steps(intervals, "symbol")
        if steps is None:
            continue
        resolved.append({
            "kind": "isin_symbol_sequence",
            "isin": isin,
            "symbols": [iv["symbol"] for iv in sorted(intervals, key=lambda x: x["first_observed"])],
            "steps": steps,
            "unlinked_pairs": sorted({f"{a}->{b}" for a, b in unlinked}),
            "verification": "eqbe_bars_intervals_sequential_observation_only",
            "promoted_to_symbol_edge": False,
            "warning": ("One ISIN observed under sequential symbols with no official "
                        "rename row for the pair. Dated lead only; symbol reuse is "
                        "possible, so no edge is created."),
        })
    return resolved


def build_dated_isin_mapping(observations: dict | None) -> list[dict]:
    """Conservative dated (symbol, ISIN) mapping from EQ/BE intervals only.

    Each row is valid only inside its observed interval
    [first_observed, last_observed]. It is a dated observation map, not a
    security master: it does not prove share class, issuer, or continuity
    across a gap, and must never be joined by symbol string alone or by
    company-name similarity.
    """
    if observations is None:
        return []
    rows = []
    for interval in sorted(observations.get("isin_intervals", []),
                           key=lambda iv: (iv["symbol"], iv["first_observed"], iv["isin"])):
        rows.append({
            "mapping_id": (f"{interval['symbol']}|{interval['isin']}|"
                           f"{interval['first_observed']}|{interval['last_observed']}"),
            "symbol": interval["symbol"],
            "isin": interval["isin"],
            "valid_from": interval["first_observed"],
            "valid_to": interval["last_observed"],
            "sessions": interval["sessions"],
            "verification": "eqbe_bars_interval_observation_only",
            "warning": ("Dated observation only. Validity does not extend before "
                        "first_observed or after last_observed; gaps, reuse, and "
                        "share-class changes are not resolved by this map."),
        })
    return rows


def build_isin_missing_audits(edges: list[dict], observations: dict | None) -> list[dict]:
    """Observation-only audits for edges whose bracketed ISIN is missing.

    An audit is emitted ONLY when all of the following hold, so the
    intervals themselves prove a strictly sequential pattern across the
    official effective date (rather than a name/price guess):

    * each side has exactly one distinct EQ/BE ISIN interval;
    * the old interval ends strictly before the new interval starts;
    * the official effective date lies in the gap between them
      (old last_observed <= effective_date <= new first_observed);
    * neither side's nearest-interval pick was an ambiguous tie.

    The audit NEVER assigns the ISIN to the edge: when a side fell outside
    the 365-day bracket window the candidate stays an unassigned
    observation, with gaps preserved. Different candidate ISINs across a
    sequential gap are recorded as a possible ISIN/share-class change, not
    as proof of one. No symbol-name, company-name, or price evidence is
    consulted, and no edge is created or altered.
    """
    if observations is None:
        return []
    by_symbol = observations.get("isin_intervals_by_symbol", {})
    audits = []
    for edge in edges:
        if edge["isin_old"] is not None and edge["isin_new"] is not None:
            continue
        if edge["isin_old_ambiguous"] or edge["isin_new_ambiguous"]:
            continue
        old_ivs = by_symbol.get(edge["old_symbol"], [])
        new_ivs = by_symbol.get(edge["new_symbol"], [])
        if len({iv["isin"] for iv in old_ivs}) != 1 or len({iv["isin"] for iv in new_ivs}) != 1:
            continue
        old_iv, new_iv = old_ivs[0], new_ivs[0]
        effective = edge["effective_date"]
        if not (old_iv["last_observed"] < new_iv["first_observed"]):
            continue
        if not (old_iv["last_observed"] <= effective <= new_iv["first_observed"]):
            continue
        same_isin = old_iv["isin"] == new_iv["isin"]
        audits.append({
            "audit_id": edge["edge_id"],
            "kind": ("sequential_same_isin_observation_only" if same_isin
                     else "sequential_different_isin_observation_only"),
            "old_symbol": edge["old_symbol"],
            "new_symbol": edge["new_symbol"],
            "company": edge["company"],
            "effective_date": effective,
            "candidate_isin_old_observation_only": old_iv["isin"],
            "candidate_isin_new_observation_only": new_iv["isin"],
            "candidate_isin_same": same_isin,
            "old_interval": old_iv,
            "new_interval": new_iv,
            "old_gap_days_to_effective": (
                datetime.strptime(effective, "%Y-%m-%d").date()
                - datetime.strptime(old_iv["last_observed"], "%Y-%m-%d").date()).days,
            "new_gap_days_from_effective": (
                datetime.strptime(new_iv["first_observed"], "%Y-%m-%d").date()
                - datetime.strptime(effective, "%Y-%m-%d").date()).days,
            "assigned_to_edge": False,
            "verification": "eqbe_bars_intervals_strictly_sequential_across_effective_date",
            "source": (f"{SOURCE_LABEL}; EQ/BE bars intervals "
                       f"({observations.get('source', 'bars')}, read-only)"),
            "source_row": edge["source_row"],
            "warning": ("Observation-only audit. The candidate ISINs are NOT assigned to "
                        "the edge and do not prove the share class or security on the "
                        "effective date; an official dated ISIN/share-class record is "
                        "still required. No symbol- or name-based merge was performed."),
            "external_evidence_gap": ("Dated official ISIN and share-class record for the "
                                      "symbol on the effective date is not in the saved "
                                      "sources; the sequential bars intervals alone do not "
                                      "close this gap."),
        })
    audits.sort(key=lambda a: (a["effective_date"], a["old_symbol"], a["new_symbol"]))
    return audits


def isin_missing_reasons(edge: dict, observations: dict) -> list[dict]:
    """Per-side reason a bracketed ISIN is null, plus the external gap."""
    by_symbol = observations["isin_intervals_by_symbol"]
    reasons = []
    for side, symbol, isin, ambiguous, gap in (
            ("old", edge["old_symbol"], edge["isin_old"], edge["isin_old_ambiguous"], edge["isin_old_gap_days"]),
            ("new", edge["new_symbol"], edge["isin_new"], edge["isin_new_ambiguous"], edge["isin_new_gap_days"])):
        if isin is not None:
            continue
        intervals = by_symbol.get(symbol, [])
        effective = edge["effective_date"]
        covering = [iv for iv in intervals if iv["first_observed"] <= effective <= iv["last_observed"]]
        if ambiguous:
            code = "ambiguous_tied_intervals"
        elif not intervals:
            code = "no_eqbe_isin_interval_for_symbol"
        elif covering and side == "new":
            code = "new_symbol_interval_covers_effective_reuse_risk"
        elif gap is not None and gap > MAX_ISIN_BRACKET_GAP_DAYS:
            code = "nearest_interval_outside_365d_bracket_window"
        else:
            code = "no_bracketing_eqbe_interval"
        reasons.append({"side": side, "symbol": symbol, "code": code,
                        "gap_days": gap, "ambiguous": ambiguous,
                        "covering_intervals": covering})
    return reasons


def terminal_proceeds_audit(delisted: list[dict], observations: dict | None,
                            edges: list[dict], verified_consideration: dict | None = None) -> dict:
    """Read-only terminal-proceeds audit: observations only, no exit prices.

    For every official delisted row, record what bars actually observed
    (presence, EQ/BE sessions, ISIN intervals, timing vs the delisting date,
    and the last observed EQ/BE traded close as a *verified observation*)
    and any official successor edge touching the symbol. The last observed
    close is reported separately as `last_observed_price_observation`: it is
    verified as a bars observation (date, series, ISIN, source), but it is
    never an exit price, and no cash/stock consideration is invented from
    saved sources. Exit price and proceeds stay null unless the caller
    supplies explicit `verified_consideration` evidence (a dated scheme/
    delisting document with cash per share or successor terms and a
    citation) for that symbol; none is present in the saved sources, so
    by default every row remains unresolved.
    """
    symbols = observations["symbols"] if observations else {}
    by_symbol = observations["isin_intervals_by_symbol"] if observations else {}
    price_by_symbol = (observations.get("last_eqbe_price_by_symbol", {})
                       if observations else {})
    consideration = verified_consideration or {}
    rows = []
    for row in sorted(delisted, key=lambda r: (r["delisted_date"] or "9999", r["symbol"])):
        obs = symbols.get(row["symbol"])
        price_obs = price_by_symbol.get(row["symbol"])
        successors = [e["edge_id"] for e in edges if e["old_symbol"] == row["symbol"]]
        predecessors = [e["edge_id"] for e in edges if e["new_symbol"] == row["symbol"]]
        timing, delta = "no_observation", None
        if obs and row["delisted_date"]:
            delta = (date.fromisoformat(row["delisted_date"])
                     - date.fromisoformat(obs["last_observed"])).days
            timing = ("after_30_days" if delta < -30 else "before_30_days" if delta > 30
                      else "within_30_days_of_delisting")
        verified = consideration.get(row["symbol"])
        if verified is not None:
            exit_price = verified.get("cash_per_share")
            proceeds = verified.get("proceeds_per_share", exit_price)
            status = "resolved_verified_consideration"
            gap = None
        else:
            exit_price = proceeds = None
            status = "unresolved_no_verified_exit_price"
            gap = ("Verified delisting/scheme consideration "
                   "(cash per share or successor terms with ex-date and citation) "
                   "is not in the saved official sources; last observed bars are "
                   "observations only, never an exit price.")
        rows.append({
            "symbol": row["symbol"], "company": row["company"],
            "delisted_date": row["delisted_date"], "delisted_type": row["type"],
            "bars_observed": obs is not None,
            "bars_first_observed": obs["first_observed"] if obs else None,
            "bars_last_observed": obs["last_observed"] if obs else None,
            "eqbe_sessions": obs["eqbe_sessions"] if obs else 0,
            "sessions_all_series": obs["sessions"] if obs else 0,
            "eqbe_isin_intervals": by_symbol.get(row["symbol"], []),
            "last_observed_price_observation": price_obs,
            "last_observed_timing_vs_delisting": timing,
            "days_delisting_minus_last_observed": delta,
            "official_successor_edges": successors,
            "official_predecessor_edges": predecessors,
            "exit_price": exit_price,
            "proceeds_per_share": proceeds,
            "verified_consideration": verified,
            "proceeds_status": status,
            "external_evidence_gap": gap,
        })
    summary = {
        "delisted_rows": len(rows),
        "observed_in_bars": sum(r["bars_observed"] for r in rows),
        "not_observed_in_bars": sum(not r["bars_observed"] for r in rows),
        "with_verified_observed_price": sum(r["last_observed_price_observation"] is not None for r in rows),
        "without_verified_observed_price": sum(r["last_observed_price_observation"] is None for r in rows),
        "by_timing": dict(sorted(collections.Counter(r["last_observed_timing_vs_delisting"] for r in rows).items())),
        "by_delisted_type": dict(sorted(collections.Counter(r["delisted_type"] for r in rows).items())),
        "with_official_successor_edge": sum(bool(r["official_successor_edges"]) for r in rows),
        "with_official_predecessor_edge": sum(bool(r["official_predecessor_edges"]) for r in rows),
        "exit_prices_invented": 0,
        "proceeds_resolved": sum(r["proceeds_per_share"] is not None for r in rows),
        "proceeds_unresolved": sum(r["proceeds_per_share"] is None for r in rows),
        "method": ("Read-only bars symbol/ISIN/price observations joined to official "
                   "delisted rows by symbol only. The last observed EQ/BE close is a "
                   "verified bars observation (with date/series/ISIN/source), never an "
                   "exit price; no discount, liquidity haircut, or successor value is "
                   "assumed. Exit prices require explicit verified consideration."),
        "limitations": [
            "Symbol-only join to delisted rows; reuse/relisting is possible and timing is a review flag, not proof of a tradable exit.",
            "Observations end can reflect symbol changes, mergers, suspensions, or source gaps, not only delisting.",
            "The official delisted list is bounded and stale; post-list exits are not covered here.",
            "Last observed close is a traded-price observation on its own date, not delisting consideration; using it as proceeds would be guessing.",
        ],
    }
    return {"rows": rows, "summary": summary}


def lineage_components(edges: list[dict]) -> list[list[str]]:
    """Connected components over edge symbols (union-find, date-agnostic)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for edge in edges:
        ra, rb = find(edge["old_symbol"]), find(edge["new_symbol"])
        if ra != rb:
            parent[ra] = rb
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for symbol in parent:
        groups[find(symbol)].append(symbol)
    return sorted((sorted(members) for members in groups.values() if len(members) > 1),
                  key=lambda members: members[0])


def build_unresolved(edges: list[dict], excluded: list[dict],
                     observations: dict | None, delisted: list[dict],
                     isin_audits: list[dict] | None = None) -> list[dict]:
    """Everything not promoted to an edge, with an explicit reason."""
    audit_by_edge = {a["audit_id"]: a for a in (isin_audits or [])}
    unresolved: list[dict] = []
    edge_pairs = {(e["old_symbol"], e["new_symbol"]) for e in edges}
    edge_symbols = {e["old_symbol"] for e in edges} | {e["new_symbol"] for e in edges}

    for row in excluded:
        unresolved.append({
            "kind": "old_equals_new_symbolchange_row",
            "old_symbol": row["old_symbol"], "new_symbol": row["new_symbol"],
            "company": row["company"], "effective_date": row["effective_date"],
            "reason": ("Official row with old == new symbol; not a rename edge. "
                       "May mark another security change; not interpreted here."),
            "source": SOURCE_LABEL, "source_row": row["source_row"],
        })

    for edge in edges:
        base = {"old_symbol": edge["old_symbol"], "new_symbol": edge["new_symbol"],
                "company": edge["company"], "effective_date": edge["effective_date"],
                "source": SOURCE_LABEL, "source_row": edge["source_row"]}
        if observations is not None:
            if edge["bars_old"] is None:
                unresolved.append({**base, "kind": "edge_old_symbol_absent_from_bars",
                                   "reason": "Official edge kept, but old symbol never observed in bars; continuity unverified."})
            if edge["bars_new"] is None:
                unresolved.append({**base, "kind": "edge_new_symbol_absent_from_bars",
                                   "reason": "Official edge kept, but new symbol never observed in bars; continuity unverified."})
            if edge["bars_order_violation"]:
                unresolved.append({**base, "kind": "edge_bars_order_violation",
                                   "reason": ("Bars observations contradict a clean rename order "
                                              "(old only after, or new only before, the effective date); "
                                              "possible symbol reuse or series overlap. Edge kept, flagged.")})
            if edge["isin_old"] is None or edge["isin_new"] is None:
                unresolved.append({**base, "kind": "edge_isin_missing",
                                   "isin_old": edge["isin_old"], "isin_new": edge["isin_new"],
                                   "isin_missing_observation_audit": audit_by_edge.get(edge["edge_id"]),
                                   "isin_missing_reasons": isin_missing_reasons(edge, observations),
                                   "external_evidence_gap": ("Dated official ISIN for the symbol on the effective "
                                                             "date (symbol-change annex/master with effective-date "
                                                             "ISIN) is not in the saved sources; later bars ISINs "
                                                             "are not backfilled."),
                                   "reason": ("ISIN missing on one/both sides (expected pre-2011: bars "
                                              "carry no ISIN then; also if ambiguous or not EQ/BE observed). "
                                              "Not backfilled from later observations.")})
        for flag, value in edge["reuse_flags"].items():
            if value:
                unresolved.append({**base, "kind": "edge_reuse_or_branch_flag",
                                   "flag": flag,
                                   "reason": "Reuse/branching flag: date-bounded joins only; do not chain symbols blindly."})

    if observations is not None:
        # Bars ISIN churn that no official rename row explains. Strictly
        # sequential patterns are resolved as observation-only records by
        # build_resolved_observations; only overlapping/ambiguous patterns
        # remain unresolved here. Neither is promoted to an edge.
        for symbol, intervals in sorted(observations["isin_intervals_by_symbol"].items()):
            isins = {iv["isin"] for iv in intervals}
            if len(isins) > 1 and symbol not in edge_symbols:
                if sequential_steps(intervals, "isin") is not None:
                    continue
                unresolved.append({
                    "kind": "bars_multi_isin_symbol_without_symbolchange_edge",
                    "symbol": symbol, "isins": sorted(isins),
                    "intervals": intervals,
                    "external_evidence_gap": ("Official ISIN-change/series-migration record with effective "
                                              "date is not in the saved sources."),
                    "reason": ("One EQ/BE symbol observed with overlapping/interleaved ISIN intervals "
                               "and no official symbol-change row touches it. Ambiguous (reuse/"
                               "correction possible); not promoted to an edge."),
                })
        for isin, syms in sorted(observations["isin_to_symbols"].items()):
            if len(syms) > 1:
                # A rename edge links the pair in either direction.
                unlinked = [pair for pair in [(a, b) for a in syms for b in syms if a != b]
                            if pair not in edge_pairs and (pair[1], pair[0]) not in edge_pairs]
                if unlinked:
                    intervals = [iv for iv in observations.get("isin_intervals", []) if iv["isin"] == isin]
                    if sequential_steps(intervals, "symbol") is not None:
                        continue
                    unresolved.append({
                        "kind": "bars_isin_maps_to_multiple_symbols_without_edge",
                        "isin": isin, "symbols": syms,
                        "unlinked_pairs": sorted({f"{a}->{b}" for a, b in unlinked}),
                        "external_evidence_gap": ("Official symbol-change row or dated ISIN-symbol master "
                                                  "for the unlinked pair is not in the saved sources."),
                        "reason": ("One ISIN observed under overlapping EQ/BE symbol intervals with no "
                                   "official rename edge for the pair. Ambiguous; not promoted to an edge."),
                    })

    for row in delisted:
        if row["delisted_date"] and row["delisted_date"] < "2011-01-01" \
                and row["symbol"] not in edge_symbols:
            lead = row["symbol"] == "GFL"
            unresolved.append({
                "kind": "pre2011_delisted_without_symbolchange_edge",
                "symbol": row["symbol"], "company": row["company"],
                "delisted_date": row["delisted_date"], "delisted_type": row["type"],
                "reason": ("Pre-2011 delisted symbol with no official symbol-change edge. "
                           + ("GFL/GODREJFOOD same-company lead stays unverified: no dated "
                              "ISIN lineage exists, so no edge is created." if lead
                              else "No symbol successor is published in the saved official sources.")),
            })
    return unresolved


def coverage_summary(edges: list[dict], unresolved: list[dict], excluded: list[dict],
                     observations: dict | None, delisted: list[dict],
                     resolved: list[dict] | None = None,
                     terminal: dict | None = None,
                     dated_mapping: list[dict] | None = None,
                     isin_audits: list[dict] | None = None) -> dict:
    resolved = resolved or []
    dated_mapping = dated_mapping or []
    isin_audits = isin_audits or []
    components = lineage_components(edges)
    component_symbols = {s for comp in components for s in comp}
    bars_symbols = set(observations["symbols"]) if observations else set()
    eqbe_symbols = {s for s, obs in (observations["symbols"].items() if observations else [])
                    if obs["eqbe_sessions"] > 0}
    by_year = dict(sorted(collections.Counter(e["effective_date"][:4] for e in edges).items()))
    isin_both = [e for e in edges if e["isin_old"] and e["isin_new"]]
    summary = {
        "source": SOURCE_LABEL,
        "bars_source": observations["source"] if observations else None,
        "bars_read_only": observations["read_only"] if observations else None,
        "edges_total": len(edges),
        "symbolchange_rows_total": len(edges) + len(excluded),
        "excluded_old_equals_new_rows": len(excluded),
        "edges_earliest": min((e["effective_date"] for e in edges), default=None),
        "edges_latest": max((e["effective_date"] for e in edges), default=None),
        "edges_pre2011": sum(e["effective_date"] < "2011-01-01" for e in edges),
        "edges_2000_2010": sum("2000-01-01" <= e["effective_date"] <= "2010-12-31" for e in edges),
        "edges_by_year": by_year,
        "distinct_old_symbols": len({e["old_symbol"] for e in edges}),
        "distinct_new_symbols": len({e["new_symbol"] for e in edges}),
        "chains_or_components": len(components),
        "largest_component_size": max((len(c) for c in components), default=0),
        "isin": {
            "both_present": len(isin_both),
            "both_present_and_changed": sum(e["isin_change"] is True for e in isin_both),
            "both_present_and_same": sum(e["isin_change"] is False for e in isin_both),
            "one_or_both_missing": len(edges) - len(isin_both),
            "both_missing": sum(e["isin_old"] is None and e["isin_new"] is None for e in edges),
            "ambiguous_old": sum(e["isin_old_ambiguous"] for e in edges),
            "ambiguous_new": sum(e["isin_new_ambiguous"] for e in edges),
            "note": ("ISIN only from EQ/BE bars intervals bracketing the effective date. "
                     "Bars carry no ISIN before 2011, so pre-2011 edges are null by construction."),
        },
        "bars_continuity": {
            "bars_distinct_symbols_all_series": len(bars_symbols),
            "bars_distinct_symbols_eqbe": len(eqbe_symbols),
            "edge_old_in_bars": sum(e["bars_old"] is not None for e in edges),
            "edge_new_in_bars": sum(e["bars_new"] is not None for e in edges),
            "edge_both_in_bars": sum(e["bars_old"] is not None and e["bars_new"] is not None for e in edges),
            "edge_neither_in_bars": sum(e["bars_old"] is None and e["bars_new"] is None for e in edges),
            "edge_bars_order_violations": sum(e["bars_order_violation"] for e in edges),
            "bars_symbols_gaining_continuity_all_series": len(bars_symbols & component_symbols),
            "bars_symbols_gaining_continuity_eqbe": len(eqbe_symbols & component_symbols),
            "bars_symbols_untouched_by_any_edge": len(bars_symbols - component_symbols),
            "definition": ("A bars symbol gains continuity if it belongs to a lineage "
                           "component of size >= 2 (has a predecessor or successor edge)."),
        },
        "unresolved_total": len(unresolved),
        "unresolved_by_kind": dict(sorted(collections.Counter(u["kind"] for u in unresolved).items())),
        "isin_missing_by_reason": dict(sorted(collections.Counter(
            r["code"] for u in unresolved if u["kind"] == "edge_isin_missing"
            for r in u.get("isin_missing_reasons", [])).items())),
        "resolved_observations_total": len(resolved),
        "resolved_observations_by_kind": dict(sorted(collections.Counter(r["kind"] for r in resolved).items())),
        "resolved_observations_note": ("Dated observation-only records (sequential EQ/BE ISIN/symbol "
                                        "intervals). None is a symbol edge and none was merged by name "
                                        "similarity or latest ISIN."),
        "dated_isin_mapping_rows": len(dated_mapping),
        "dated_isin_mapping_note": ("Conservative dated (symbol, ISIN) map from EQ/BE bars "
                                    "intervals only; valid only inside each observed "
                                    "interval. Not a security master or share-class proof."),
        "isin_missing_audits_total": len(isin_audits),
        "isin_missing_audits_by_kind": dict(sorted(collections.Counter(a["kind"] for a in isin_audits).items())),
        "isin_missing_audits_note": ("Observation-only audits where single-ISIN EQ/BE "
                                     "intervals are strictly sequential across the official "
                                     "effective date. Candidates are never assigned to the "
                                     "edge; edges stay unresolved and no name merge occurs."),
        "terminal_proceeds": (terminal or {}).get("summary", {
            "delisted_rows": len(delisted), "exit_prices_invented": 0,
            "proceeds_resolved": 0,
            "note": "No bars observations supplied; terminal proceeds not audited."}),
        "delisted_reference_rows": len(delisted),
        "limitations": [
            "Edges are exactly the official symbol-change rows with old != new; absence of a row is never filled by name or ISIN similarity.",
            "symbolchange.csv carries no ISIN, series, or company identifier beyond the name text; ISINs come only from post-2011 EQ/BE bars intervals.",
            "Pre-2011 identity has no ISIN corroboration in bars (ISIN is null before 2011); pre-2011 edges rest on the official row alone.",
            "Symbol reuse: an edge is valid only as a dated old->new step; blind symbol-string chaining across edges or years can join different securities.",
            "Bars presence uses every series; EQ/BE-scoped ISINs do not cover debt/ETF/derivative renames present in the symbol-change file.",
            "GFL<->GODREJFOOD remains unresolved by design (no official row, no dated ISIN lineage).",
            "Sequential ISIN/symbol interval patterns are resolved as observation-only records, never as edges; overlapping patterns stay unresolved.",
            "Terminal proceeds: delisted rows are audited from observations only; exit prices and proceeds stay null until verified consideration is obtained (see terminal_proceeds).",
        ],
    }
    return summary


# ---------------------------------------------------------------------------
# Combined identity edge set (official symbol-change rows + corporate-actions API)
# ---------------------------------------------------------------------------
#
# The official symbol-change file and the official corporate-actions API are
# two independent official sources of the same fact - a security changed its
# symbol on a date. Neither layer is rewritten here. The combined set is
# their UNION, in a fixed order, with every edge tagged with the provenance
# of the source that produced it, so a consumer can always tell which layer
# an edge came from:
#
#   edge_source = "official_symbolchange_row"      (from nse-symbolchange.csv)
#   edge_source = "official_corporate_actions_api"  (from the NSE API layer)
#
# The official edges are carried through UNCHANGED except for the added
# provenance tag: no field of an official edge is edited, dropped or
# recomputed. The API edges are appended with their own verification value
# so that a consumer can never mistake an API-derived rename for a published
# symbol-change row.
#
# The combined set is written to an ADDITIONAL file
# (``identity_edges_combined.jsonl``). The existing
# ``identity_edges.jsonl`` is never read-modified-written by this layer and
# stays byte-identical.

EDGE_SOURCE_SYMBOLCHANGE = "official_symbolchange_row"
EDGE_SOURCE_CORPORATE_ACTIONS_API = "official_corporate_actions_api"
API_EDGE_VERIFICATION = "official_corporate_actions_api_rename"
COMBINED_EDGES_FILENAME = "identity_edges_combined.jsonl"
OFFICIAL_EDGES_FILENAME = "identity_edges.jsonl"
API_EDGES_FILENAME = "api_identity_edges.jsonl"


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSON-lines file of objects. A malformed line raises."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path.name} line {number}: invalid JSON") from error
        if not isinstance(row, dict):
            raise ValueError(f"{path.name} line {number}: expected a JSON object")
        rows.append(row)
    return rows


def load_api_identity_edges(path: Path) -> list[dict]:
    """Read the corporate-actions API identity edges.

    Returns an empty list when the API layer has not been built yet; an
    absent API layer never fabricates edges and never blocks the official
    layer.
    """
    return read_jsonl(path)


def build_combined_edges(symbolchange_edges: list[dict],
                         api_edges: list[dict]) -> list[dict]:
    """Official symbol-change edges plus API-derived edges, each provenance-tagged.

    The result is the union of the two layers in that order. Every official
    edge keeps every one of its own fields (the only addition is the
    ``edge_source`` provenance tag). Every API edge keeps its own fields too
    and carries the API verification value. Nothing is merged, deduplicated
    or rewritten, so the combined set can always be split back into its two
    layers by ``edge_source``.
    """
    combined: list[dict] = []
    for edge in symbolchange_edges:
        combined.append({**edge, "edge_source": EDGE_SOURCE_SYMBOLCHANGE})
    for edge in api_edges:
        combined.append({
            **edge,
            "edge_source": EDGE_SOURCE_CORPORATE_ACTIONS_API,
            "verification": API_EDGE_VERIFICATION,
        })
    return combined


def combined_edge_summary(combined: list[dict], official: list[dict],
                           api: list[dict]) -> dict:
    """Counts of the combined edge set, by provenance."""
    by_source = collections.Counter(edge.get("edge_source") for edge in combined)
    return {
        "combined_edges_total": len(combined),
        "combined_edges_by_source": dict(sorted(by_source.items())),
        "official_symbolchange_edges": len(official),
        "official_corporate_actions_api_edges": len(api),
        "official_edges_file": OFFICIAL_EDGES_FILENAME,
        "combined_edges_file": COMBINED_EDGES_FILENAME,
        "official_edges_file_modified": False,
        "note": ("The combined set is the union of the two official layers, each "
                 "edge tagged with its provenance. The official "
                 f"{OFFICIAL_EDGES_FILENAME} file is not modified by this build."),
    }


def write_combined_identity_edges(processed: Path,
                                  api_edges_path: Path | None = None) -> dict:
    """Write the combined identity edge file next to the official one.

    The official ``identity_edges.jsonl`` is only READ here; it is never
    rewritten, so it stays byte-identical. Returns the combined summary.
    """
    official_path = processed / OFFICIAL_EDGES_FILENAME
    official_edges = read_jsonl(official_path)
    api_path = api_edges_path or (processed / API_EDGES_FILENAME)
    api_edges = load_api_identity_edges(api_path)
    combined = build_combined_edges(official_edges, api_edges)
    write_jsonl(processed / COMBINED_EDGES_FILENAME, combined)
    summary = combined_edge_summary(combined, official_edges, api_edges)
    summary["api_edges_source"] = str(api_path)
    return summary


def iso_date(value) -> date | None:
    """Tolerant ISO date parser: a date, or ``None``. Never raises."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("-", "None", "null"):
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def bars_handover(edge: dict) -> dict:
    """The observed trading handover recorded on an API-derived edge.

    The corporate-actions feed dates the successor's first *action*, not the
    day the successor started trading, so the feed date is an upper bound on
    the handover. The read-only bars observation on the edge is the trading
    handover itself: the predecessor's last observed bar and the successor's
    first observed bar. It is a price-series observation only - it never
    creates an edge and it never supplies a share exchange ratio.
    """
    consistency = edge.get("bars_consistency") or {}
    old_last = iso_date(consistency.get("bars_old_last_date"))
    new_first = iso_date(consistency.get("bars_new_first_date"))
    observed = old_last is not None and new_first is not None
    ordered = bool(observed and old_last < new_first
                   and consistency.get("bars_handover_ordering_ok", True))
    return {
        "observed": observed,
        "ordered": ordered,
        "old_last_date": old_last.isoformat() if old_last else None,
        "new_first_date": new_first.isoformat() if new_first else None,
        "basis": "read_only_bars_trading_handover_observation",
        "note": ("Bars are read-only observations of the traded handover. They "
                 "are not an identity record and they never carry a share "
                 "exchange ratio."),
    }


def merge_identity_edges(official_edges: list[dict], api_edges: list[dict]) -> dict:
    """The two identity layers, plus a strict consumer-side admissibility view.

    The published combined edge set is written by
    :func:`write_combined_identity_edges` and is the plain union of the two
    official layers, every edge tagged with its provenance. This function
    returns that same union and adds, for each API edge, the stricter
    fail-closed gate a *consumer* must clear before it may use that edge to
    remap a holding:

    * it must be a dated step with exactly one old and one new symbol;
    * it must not cross a symbol reuse (one ticker, two companies);
    * it must not duplicate an official edge;
    * it must not create a branch (a second successor of the old symbol) or a
      merge (a second predecessor of the new symbol) in the merged graph;
    * the read-only bars handover must be ordered, so the two price series
      never overlap and the joined series has no duplicate key;
    * and the share count must be provably continuous across the handover.
      The same ISIN under both symbols proves the same *instrument*, so a
      1:1 carry-over is admissible. Two different ISINs prove the same
      *company* only: unless an official split row documents the ratio, the
      exchange ratio at the handover is not documented anywhere in the saved
      sources, so the edge may never imply a 1:1 share conversion and the
      holding stays unresolved.

    A refused edge is reported with its reason; it is never applied and never
    silently dropped. This view never rewrites the official layer's own
    fields.
    """
    official: list[dict] = []
    official_succ: dict[str, set[str]] = collections.defaultdict(set)
    official_pred: dict[str, set[str]] = collections.defaultdict(set)
    official_pairs: set[tuple[str, str]] = set()
    combined = build_combined_edges(official_edges, api_edges)
    for record in combined:
        if record.get("edge_source") != EDGE_SOURCE_SYMBOLCHANGE:
            continue
        record["remap_admissible"] = True
        record["refusal_codes"] = []
        record["remap_effective_date"] = record.get("effective_date")
        official.append(record)
        old, new = record.get("old_symbol"), record.get("new_symbol")
        if old and new:
            official_succ[old].add(new)
            official_pred[new].add(old)
            official_pairs.add((old, new))

    # Company keys per symbol in the API layer: a ticker used by two official
    # companies is a reused symbol and may not be linked across.
    company_by_symbol: dict[str, set[str]] = collections.defaultdict(set)
    for edge in api_edges:
        key = edge.get("company_name_normalised") or edge.get("company_name")
        for symbol in (edge.get("old_symbol"), edge.get("new_symbol")):
            if symbol and key:
                company_by_symbol[symbol].add(str(key).strip().casefold())

    edges = list(official)
    remap_edges = list(official)
    refused: list[dict] = []
    merged_succ = {symbol: set(targets) for symbol, targets in official_succ.items()}
    merged_pred = {symbol: set(sources) for symbol, sources in official_pred.items()}

    for record in combined:
        if record.get("edge_source") != EDGE_SOURCE_CORPORATE_ACTIONS_API:
            continue
        old, new = record.get("old_symbol"), record.get("new_symbol")
        effective = iso_date(record.get("effective_date"))
        codes: list[str] = []
        reasons: list[str] = []

        if not old or not new or effective is None:
            codes.append(REFUSAL_UNDATED)
            reasons.append("The edge is not a dated old->new step, so it can "
                           "never bound a predecessor/successor walk.")

        reused = sorted(symbol for symbol in (old, new)
                        if symbol and len(company_by_symbol.get(symbol, set())) > 1)
        if reused:
            codes.append(REFUSAL_REUSE)
            reasons.append(f"Symbol reuse: {', '.join(reused)} is used by more "
                           "than one official company in the API layer, so no "
                           "link may be made across the reuse.")
        if edge.get("symbol_reuse_flagged"):
            codes.append(REFUSAL_REUSE)
            reasons.append("The edge itself is flagged for symbol reuse.")

        if old and new and (old, new) in official_pairs:
            codes.append(REFUSAL_DUPLICATE)
            reasons.append("The official symbol-change layer already publishes "
                           "this step; the API edge adds no dated evidence and "
                           "is not applied twice.")

        if old and new:
            other_successors = sorted(merged_succ.get(old, set()) - {new})
            if other_successors:
                codes.append(REFUSAL_BRANCH)
                reasons.append(f"Branch ambiguity: {old} already has the "
                               f"successor(s) {', '.join(other_successors)}, so "
                               "the step is not a single unambiguous successor.")
            other_predecessors = sorted(merged_pred.get(new, set()) - {old})
            if other_predecessors:
                codes.append(REFUSAL_MERGE)
                reasons.append(f"Merge ambiguity: {new} already has the "
                               f"predecessor(s) {', '.join(other_predecessors)}, "
                               "so the step is not a single unambiguous "
                               "predecessor.")

        handover = bars_handover(record)
        if handover["observed"] and not handover["ordered"]:
            codes.append(REFUSAL_BARS_ORDER)
            reasons.append("The read-only bars handover is not ordered (the "
                           "predecessor and the successor overlap in time), so "
                           "their price series cannot be joined without a "
                           "duplicate key.")
        # The handover is the moment the successor started trading. The
        # corporate-actions feed dates the successor's first *action*, which
        # is only an upper bound on it, so the observed trading handover is
        # the remap boundary whenever it is known.
        boundary = (handover["new_first_date"] if handover["observed"]
                    and handover["ordered"] else
                    (effective.isoformat() if effective else None))
        record["remap_effective_date"] = boundary
        record["handover_observation"] = handover

        isin_old, isin_new = record.get("isin_old"), record.get("isin_new")
        if isin_old and isin_new and str(isin_old) != str(isin_new):
            # The two sides carry different ISINs: the successor continuity is
            # proved at *company* level, but the instrument changed and the
            # share count across the handover may have changed with it. The
            # same ISIN under both symbols proves the same instrument and a
            # 1:1 carry-over is then admissible. Two different ISINs prove the
            # same company only, and the share exchange ratio at the handover
            # is not documented in the saved sources, so no 1:1 carry-over may
            # be made and the holding stays unresolved. A face-value split row
            # dated on the successor's side of the handover is a separate,
            # later event that the applied action audit already carries; it
            # says nothing about the handover's own ratio.
            codes.append(REFUSAL_EXCHANGE_RATIO)
            reasons.append("The successor continuity is proved (same official "
                           "company name, split-bridged ISIN) but the share "
                           "exchange ratio at the handover is not documented in "
                           "the saved sources. The edge proves which company "
                           "the security is, not how many successor shares a "
                           "holder owns, so no 1:1 carry-over may be made and "
                           "the holding stays unresolved.")
        record["remap_admissible"] = not codes
        record["refusal_codes"] = codes
        record["refusal_reasons"] = reasons
        edges.append(record)
        if codes:
            refused.append({
                "edge_id": record.get("edge_id"),
                "edge_source": EDGE_SOURCE_CORPORATE_ACTIONS_API,
                "old_symbol": old,
                "new_symbol": new,
                "effective_date": record.get("effective_date"),
                "remap_effective_date": boundary,
                "evidence_class": record.get("evidence_class"),
                "refusal_codes": codes,
                "reason": " ".join(reasons),
                "citation": record.get("citation"),
                "note": ("Refused: the edge is reported, never applied. The "
                         "holding that crosses this handover stays unresolved "
                         "and its equity stays unknown."),
            })
            continue
        remap_edges.append(record)
        if old and new:
            merged_succ.setdefault(old, set()).add(new)
            merged_pred.setdefault(new, set()).add(old)

    summary = {
        "official_edges": len(official),
        "api_edges": len(api_edges),
        "edges_total": len(edges),
        "edges_by_source": {
            EDGE_SOURCE_SYMBOLCHANGE: len(official),
            EDGE_SOURCE_CORPORATE_ACTIONS_API: len(api_edges),
        },
        "remap_admissible": len(remap_edges),
        "refused_total": len(refused),
        "refused_by_code": dict(sorted(collections.Counter(
            code for entry in refused for code in entry["refusal_codes"]).items())),
        "note": ("The official symbol-change layer is unchanged; the API layer "
                 "is additive and every API edge must clear the same "
                 "fail-closed gate before it may remap a key."),
    }
    return {"edges": edges, "remap_edges": remap_edges, "refused": refused,
            "summary": summary}


def load_merged_identity_edges(data: Path | str,
                                api_edges: Path | str | None = None) -> dict:
    """Load and merge both identity layers from a data directory.

    ``data/processed/identity/identity_edges.jsonl`` holds the official
    symbol-change rows and ``data/processed/identity/api_identity_edges.jsonl``
    the corporate-actions API edges. Missing files are simply absent layers;
    a malformed file raises.
    """
    data = Path(data)
    official_path = data / "processed/identity" / OFFICIAL_EDGES_FILENAME
    api_path = Path(api_edges) if api_edges is not None else (
        data / "processed/identity" / API_EDGES_FILENAME)
    return merge_identity_edges(read_jsonl(official_path),
                                load_api_identity_edges(api_path))


def build(symbolchange_path: Path, delisted_path: Path | None,
          bars_db: Path | None, processed: Path, coverage_path: Path) -> dict:
    change_rows = parse_symbolchange(symbolchange_path)
    delisted = parse_delisted(delisted_path) if delisted_path and delisted_path.exists() else []
    observations = bars_observations(bars_db) if bars_db and bars_db.exists() else None
    edges, excluded = build_edges(change_rows, observations)
    isin_audits = build_isin_missing_audits(edges, observations)
    unresolved = build_unresolved(edges, excluded, observations, delisted,
                                  isin_audits=isin_audits)
    resolved = build_resolved_observations(edges, observations)
    dated_mapping = build_dated_isin_mapping(observations)
    terminal = terminal_proceeds_audit(delisted, observations, edges)
    summary = coverage_summary(edges, unresolved, excluded, observations, delisted,
                               resolved=resolved, terminal=terminal,
                               dated_mapping=dated_mapping, isin_audits=isin_audits)
    write_jsonl(processed / "identity_edges.jsonl", edges)
    write_jsonl(processed / "unresolved.jsonl", unresolved)
    write_jsonl(processed / "resolved_observations.jsonl", resolved)
    write_jsonl(processed / "dated_isin_mapping.jsonl", dated_mapping)
    write_jsonl(processed / "isin_missing_audits.jsonl", isin_audits)
    write_json(processed / "terminal_proceeds_audit.json", terminal)
    if observations is not None:
        write_jsonl(processed / "bars_symbol_observations.jsonl",
                    [observations["symbols"][s] for s in sorted(observations["symbols"])])
        write_jsonl(processed / "bars_isin_intervals_eqbe.jsonl", observations["isin_intervals"])
    # The combined edge set is an ADDITIONAL output. The official
    # identity_edges.jsonl written above is only read by this step and is
    # never rewritten, so it stays byte-identical.
    write_combined_identity_edges(processed)
    write_json(coverage_path, summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--symbolchange", type=Path, default=None)
    parser.add_argument("--bars-db", type=Path, default=None)
    parser.add_argument("--no-bars", action="store_true")
    parser.add_argument("--combined-only", action="store_true",
                        help="only (re)build the combined identity edge file; "
                             "the official identity_edges.jsonl is left untouched")
    args = parser.parse_args()
    if args.combined_only:
        combined = write_combined_identity_edges(args.data / "processed/identity")
        print(json.dumps(combined, indent=2))
        return
    raw = args.data / "raw/older_actions"
    symbolchange = args.symbolchange or raw / SOURCE_FILENAME
    bars_db = None if args.no_bars else (args.bars_db or args.data / "market.duckdb")
    result = build(symbolchange, raw / "nse-delisted.csv",
                   bars_db if bars_db and bars_db.exists() else None,
                   args.data / "processed/identity",
                   args.data / "reports/identity-coverage.json")
    print(json.dumps({"edges_total": result["edges_total"],
                      "unresolved_total": result["unresolved_total"],
                      "resolved_observations_total": result["resolved_observations_total"],
                      "dated_isin_mapping_rows": result["dated_isin_mapping_rows"],
                      "isin_missing_audits_total": result["isin_missing_audits_total"],
                      "isin_missing_audits_by_kind": result["isin_missing_audits_by_kind"],
                      "terminal_proceeds": result["terminal_proceeds"],
                      "edges_by_year": result["edges_by_year"]}, indent=2))


if __name__ == "__main__":
    main()
