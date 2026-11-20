"""
Dated identity edges derived from the official NSE corporate-actions API.

The official symbol-change CSV (``nse-symbolchange.csv``) is incomplete: it
has no row for a number of real renames, so a holding in the old symbol can
never be priced or sold. This module closes that gap from a second official
source that is already in the repository: NSE's own corporate-actions API
(``data/processed/older_actions/nse-api-actions.jsonl``).

Every rule here is fail-closed and no price is ever consulted:

(A) ``same_isin``
    The same official company name (exact equality of the API ``comp`` field
    after whitespace/case normalisation) appears under two different
    symbols, and the same non-blank ISIN appears under both. That is a
    rename of one security, not two securities of one company.

(B) ``split_bridged``
    Same official company name, different ISINs, and an official API row
    for the new symbol documents a face-value split (subject matching
    ``Split``/``Fv Split`` with a parsable from/to face value) whose ex-date
    falls between the old symbol's last API row and the new symbol's first
    API row. The split explains the ISIN change, so the identity link is
    admissible. The split ex-date, the from/to face value and the API
    source row are recorded as the citation.

(C) everything else
    A company-name match with no ISIN linkage and no split bridge is NOT an
    edge. It is recorded in ``unresolved_candidates`` with an explicit
    reason. A company-name match whose two symbols are active at the same
    time in the API stream is ambiguous (two simultaneously traded symbols
    of one company are usually different instruments) and is also NOT an
    edge.

(D) one candidate handover per company
    A company observed under three or more symbols is a chain of renames
    (s0 -> s1 -> s2). Only the outermost handover - the symbol that
    disappeared (s0, observed first) to the symbol that appeared (s2,
    observed last) - can bind the two ends of that chain, so exactly one
    candidate pair per company is evaluated. The inner links of the chain
    are recorded in ``rename_chain_inner_links`` with an explicit reason and
    are never evaluated as a rename: a middle symbol of a chain is not a
    rename of either end of it, and evaluating it as one would state an
    identity the official rows do not state.

Company names are compared by exact equality of the normalised official
``comp`` string only. No fuzzy, token, edit-distance, legal-word-stripping
or similarity join is ever performed, and no price field is ever read.
Symbol reuse (a symbol string that reappears later under a different
company name) is detected and flagged, and no link is ever made across a
reuse.

The output is an *additional* identity layer: it is written to its own file
(``data/processed/identity/api_identity_edges.jsonl``) and never overwrites
the symbol-change layer. Every edge carries a flag saying it was derived
from the corporate-actions API and not from ``symbolchange.csv``.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

SOURCE_LABEL = ("NSE corporate-actions API (official, "
                "data/processed/older_actions/nse-api-actions.jsonl)")
API_RELATIVE = Path("processed/older_actions/nse-api-actions.jsonl")
SYMBOLCHANGE_RELATIVE = Path("raw/older_actions/nse-symbolchange.csv")

EVIDENCE_SAME_ISIN = "same_isin"
EVIDENCE_SPLIT_BRIDGED = "split_bridged"

# Reason codes for a company-name match that did NOT become an edge.
REASON_OVERLAP = "overlapping_api_windows_ambiguous_instruments"
REASON_NO_LINKAGE = "different_isins_no_split_bridge"
REASON_SPLIT_NO_FACE_VALUE = "split_subject_without_parsable_face_value"
REASON_SPLIT_OUTSIDE_WINDOW = "split_outside_handover_window_not_a_bridge"
REASON_REUSE = "symbol_reuse_blocks_linking"
# An inner link of a rename chain (a company observed under three or more
# symbols) is recorded, never evaluated as a rename.
REASON_CHAIN_INNER_LINK = "inner_link_of_rename_chain_not_evaluated"

# The five symbols the host measured as frozen on the sensitivity run.
DEFAULT_FROZEN_SYMBOLS = ("CENTEXT", "NICCOCORP", "REIAGRO", "STERLITE", "TVSELEC")

# "Split" (covers "Fv Split" and "Face Value Split") and a from/to face
# value in the same official subject string.
SPLIT_SUBJECT_RE = re.compile(r"\bsplit\b", re.IGNORECASE)
FACE_VALUE_RE = re.compile(
    r"(?:from\s+)?rs\.?\s*([0-9]+(?:\.[0-9]+)?)\s*/?\s*-?\s*(?:to\s+)?"
    r"(?:rs\.?|re\.?)\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small IO helpers (kept local so the module has no import-time dependencies)
# ---------------------------------------------------------------------------

def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if text in ("", "-"):
        return None
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized source date: {value!r}")


# ---------------------------------------------------------------------------
# Official company-name normalisation
# ---------------------------------------------------------------------------

def normalise_company(name: str | None) -> str:
    """Exact-match key of the official ``comp`` string.

    Only whitespace runs are collapsed and case is folded. Nothing else is
    removed: legal words, punctuation and word order are part of the
    official string, so two companies whose names differ only by, say,
    ``( india )`` versus ``(india)`` are *not* the same company here. That
    is deliberate - a looser key would be a similarity join, which never
    creates an edge.
    """
    return " ".join(str(name or "").split()).casefold()


def split_face_value(subject: str | None) -> tuple[float, float] | None:
    """From/to face value of an official split subject, else ``None``.

    Only a subject that matches ``Split``/``Fv Split`` and carries a
    parsable from/to face value yields a value; anything else is ``None``
    and therefore never admissible as a split bridge.
    """
    text = str(subject or "")
    if not SPLIT_SUBJECT_RE.search(text):
        return None
    match = FACE_VALUE_RE.search(text)
    if not match:
        return None
    try:
        return float(match.group(1)), float(match.group(2))
    except ValueError:  # pragma: no cover - regex guarantees numbers
        return None


# ---------------------------------------------------------------------------
# Official API rows
# ---------------------------------------------------------------------------

def parse_api_rows(path: Path) -> list[dict]:
    """Read the official corporate-actions API rows.

    Only identity/citation fields are kept. No price-like field (``close``,
    ``price``, ``prev_close``, ...) is read, so no price can influence an
    edge even by accident. A malformed row raises instead of being dropped
    silently.
    """
    rows: list[dict] = []
    text = path.read_text(encoding="utf-8-sig")
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path.name} line {number}: invalid JSON") from error
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name} line {number}: expected a JSON object")
        symbol = str(raw.get("symbol") or "").strip()
        if not symbol:
            raise ValueError(f"{path.name} line {number}: blank symbol")
        try:
            ex = parse_date(raw.get("ex_date")) or parse_date(raw.get("exDate"))
        except ValueError as error:
            raise ValueError(f"{path.name} line {number}: {error}") from error
        rows.append({
            "symbol": symbol,
            "company": str(raw.get("comp") or "").strip(),
            "company_key": normalise_company(raw.get("comp")),
            "subject": str(raw.get("subject") or "").strip(),
            "ex_date": ex.isoformat() if ex else None,
            "isin": str(raw.get("isin") or "").strip(),
            "face_value": str(raw.get("faceVal") or "").strip(),
            "series": str(raw.get("series") or "").strip(),
            "source_file": raw.get("source_file"),
            "source_row": raw.get("source_row"),
            "source_url": raw.get("source_url"),
        })
    return rows


def _segment(rows: list[dict]) -> dict:
    """Observed date range and identity facts for one (symbol, company).

    ``rows_detail`` keeps the official rows themselves (ordered by ex-date)
    so that a split row can be found and cited. It is never serialised into
    the report; only the derived facts are.
    """
    dated = [row for row in rows if row["ex_date"]]
    dated.sort(key=lambda row: (row["ex_date"], str(row.get("source_file") or ""),
                                 int(row.get("source_row") or 0)))
    dates = [row["ex_date"] for row in dated]
    return {
        "company": rows[0]["company"],
        "company_key": rows[0]["company_key"],
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "rows": len(rows),
        "isins": sorted({row["isin"] for row in rows if row["isin"]}),
        "face_values": sorted({row["face_value"] for row in rows if row["face_value"]}),
        "source_files": sorted({row["source_file"] for row in rows if row["source_file"]}),
        "rows_detail": dated,
    }


def _row_citation(row: dict) -> dict:
    """Citation for one official API row."""
    return {
        "source": "nse_corporate_actions_api",
        "source_file": row.get("source_file"),
        "source_row": row.get("source_row"),
        "source_url": row.get("source_url"),
        "ex_date": row.get("ex_date"),
        "isin": row.get("isin"),
        "face_value": row.get("face_value"),
        "subject": row.get("subject"),
        "symbol": row.get("symbol"),
    }


def _overlaps(start: str | None, end: str | None,
               other_start: str | None, other_end: str | None) -> bool:
    """Closed-interval overlap. Missing bounds never overlap."""
    if not start or not end or not other_start or not other_end:
        return False
    return start <= other_end and other_start <= end


# ---------------------------------------------------------------------------
# Symbol reuse
# ---------------------------------------------------------------------------

def detect_symbol_reuse(rows: list[dict]) -> list[dict]:
    """Symbols that appear under more than one official company name.

    A reused symbol string denotes different securities at different times,
    so no identity link may be made across the reuse.
    """
    by_symbol: dict[str, dict[str, list[dict]]] = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for row in rows:
        by_symbol[row["symbol"]][row["company_key"]].append(row)
    reuse = []
    for symbol in sorted(by_symbol):
        companies = by_symbol[symbol]
        if len(companies) < 2:
            continue
        segments = []
        for company_key in sorted(companies):
            segment = _segment(companies[company_key])
            segments.append({
                "company": segment["company"],
                "company_key": company_key,
                "first_date": segment["first_date"],
                "last_date": segment["last_date"],
                "rows": segment["rows"],
            })
        reuse.append({
            "symbol": symbol,
            "companies": [s["company"] for s in segments],
            "company_keys": [s["company_key"] for s in segments],
            "segments": segments,
            "reason": ("Symbol string appears under more than one official company "
                       "name; the string is reused and must not be linked across."),
        })
    return reuse


# ---------------------------------------------------------------------------
# Edge construction
# ---------------------------------------------------------------------------

def build_api_identity(rows: list[dict]) -> dict:
    """Derive dated identity edges from the official corporate-actions API.

    Returns a dict with ``edges``, ``unresolved_candidates``,
    ``symbol_reuse`` and a ``summary``. A company-name match alone never
    creates an edge; the only two admissible evidence classes are
    ``same_isin`` and ``split_bridged``.
    """
    by_symbol_company: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_symbol_company[(row["symbol"], row["company_key"])].append(row)

    segments_by_symbol: dict[str, list[dict]] = collections.defaultdict(list)
    segments_by_company: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for (symbol, company_key), group in by_symbol_company.items():
        segment = _segment(group)
        segment["symbol"] = symbol
        segments_by_symbol[symbol].append(segment)
        segments_by_company[company_key][symbol] = segment

    reuse = detect_symbol_reuse(rows)

    edges: list[dict] = []
    unresolved: list[dict] = []
    chain_pairs: list[dict] = []

    for company_key in sorted(segments_by_company):
        company_segments = segments_by_company[company_key]
        if len(company_segments) < 2:
            continue
        # One candidate handover per company: the outermost one. A company
        # observed under three symbols is a chain of renames (s0 -> s1 -> s2);
        # only the handover from the symbol that disappeared (s0) to the
        # symbol that appeared (s2) can bind the two ends of that chain. The
        # inner links of the chain are recorded, not evaluated: a middle
        # symbol of a chain is not a rename of either end, and evaluating it
        # as one would invent an identity the official rows do not state.
        ordered = sorted(
            company_segments,
            key=lambda symbol: (company_segments[symbol]["first_date"] or "9999-12-31",
                                symbol))
        old_symbol = ordered[0]
        new_symbol = ordered[-1]
        old_seg = company_segments[old_symbol]
        new_seg = company_segments[new_symbol]
        if not old_seg["first_date"] or not new_seg["first_date"]:
            continue
        # Order the pair by observed first appearance.
        if (old_seg["first_date"], old_symbol) > (new_seg["first_date"], new_symbol):
            old_symbol, new_symbol = new_symbol, old_symbol
            old_seg, new_seg = new_seg, old_seg
        candidate = _evaluate_pair(company_key, old_symbol, new_symbol,
                                   old_seg, new_seg, segments_by_symbol)
        if candidate["kind"] == "edge":
            edges.append(candidate["edge"])
        else:
            unresolved.append(candidate["candidate"])
        for left, right in zip(ordered, ordered[1:]):
            if (left, right) == (old_symbol, new_symbol):
                continue
            left_seg = company_segments[left]
            right_seg = company_segments[right]
            chain_pairs.append({
                "company_name": left_seg["company"],
                "company_name_normalised": company_key,
                "old_symbol": left,
                "new_symbol": right,
                "old_symbol_api_range": {
                    "first_date": left_seg["first_date"],
                    "last_date": left_seg["last_date"],
                    "rows": left_seg["rows"],
                    "isins": left_seg["isins"],
                },
                "new_symbol_api_range": {
                    "first_date": right_seg["first_date"],
                    "last_date": right_seg["last_date"],
                    "rows": right_seg["rows"],
                    "isins": right_seg["isins"],
                },
                "reason_code": REASON_CHAIN_INNER_LINK,
                "reason": ("Inner link of a rename chain: the company is observed "
                           "under more than two symbols, so this pair is neither "
                           "the first nor the last handover of the chain and is "
                           "not evaluated as a rename."),
            })

    edges.sort(key=lambda e: (e["effective_date"], e["old_symbol"], e["new_symbol"]))
    unresolved.sort(key=lambda c: (c["company_name_normalised"], c["old_symbol"],
                                   c["new_symbol"]))
    chain_pairs.sort(key=lambda c: (c["company_name_normalised"], c["old_symbol"],
                                    c["new_symbol"]))

    summary = _summary(rows, edges, unresolved, reuse, segments_by_company,
                        chain_pairs)
    return {
        "edges": edges,
        "unresolved_candidates": unresolved,
        "symbol_reuse": reuse,
        "rename_chain_inner_links": chain_pairs,
        "summary": summary,
        # Not serialised: the raw per-(symbol, company) segments, used by the
        # report builder to derive the external evidence a frozen symbol needs.
        "segments_by_company": segments_by_company,
    }


def _evaluate_pair(company_key: str, old_symbol: str, new_symbol: str,
                    old_seg: dict, new_seg: dict,
                    segments_by_symbol: dict[str, list[dict]]) -> dict:
    """Apply rules (A), (B) and (C) to one same-company symbol pair."""
    window_start = old_seg["last_date"]
    window_end = new_seg["first_date"]

    base = {
        "old_symbol": old_symbol,
        "new_symbol": new_symbol,
        "company_name": old_seg["company"],
        "company_name_normalised": company_key,
        "old_symbol_api_range": {
            "first_date": old_seg["first_date"], "last_date": old_seg["last_date"],
            "rows": old_seg["rows"], "isins": old_seg["isins"],
            "face_values": old_seg["face_values"], "source_files": old_seg["source_files"],
        },
        "new_symbol_api_range": {
            "first_date": new_seg["first_date"], "last_date": new_seg["last_date"],
            "rows": new_seg["rows"], "isins": new_seg["isins"],
            "face_values": new_seg["face_values"], "source_files": new_seg["source_files"],
        },
        "handover": {
            "old_symbol_last_api_date": window_start,
            "new_symbol_first_api_date": window_end,
            "window_start": window_start,
            "window_end": window_end,
            "gap_days": ((date.fromisoformat(window_end) - date.fromisoformat(window_start)).days
                         if window_start and window_end else None),
        },
        "derived_from": "nse_corporate_actions_api",
        "from_symbolchange_csv": False,
    }

    # Symbol reuse blocks a link: if either side is used by another company
    # at any point of the handover window, the symbol string is ambiguous.
    reuse_block = []
    for symbol in (old_symbol, new_symbol):
        for segment in segments_by_symbol.get(symbol, []):
            if segment["company_key"] == company_key:
                continue
            if _overlaps(segment["first_date"], segment["last_date"],
                          window_start, window_end):
                reuse_block.append({
                    "symbol": symbol,
                    "other_company": segment["company"],
                    "other_company_key": segment["company_key"],
                    "other_first_date": segment["first_date"],
                    "other_last_date": segment["last_date"],
                })
    if reuse_block:
        return {"kind": "candidate", "candidate": {
            **base,
            "reason_code": REASON_REUSE,
            "reason": ("Symbol reuse: the symbol string is also used by another "
                       "official company inside the handover window, so no link is "
                       "made across the reuse."),
            "reuse_evidence": reuse_block,
            "symbol_reuse_flagged": sorted({item["symbol"] for item in reuse_block}),
        }}

    # Overlapping activity windows are ambiguous: two symbols of one company
    # traded at the same time are usually different instruments.
    if _overlaps(old_seg["first_date"], old_seg["last_date"],
                 new_seg["first_date"], new_seg["last_date"]):
        return {"kind": "candidate", "candidate": {
            **base,
            "reason_code": REASON_OVERLAP,
            "reason": ("Both symbols are active in the official API stream at the "
                       "same time. Two simultaneously traded symbols of one company "
                       "are usually different instruments (subsidiary, share class), "
                       "not a rename."),
        }}

    shared_isins = sorted(set(old_seg["isins"]) & set(new_seg["isins"]))
    if shared_isins:
        edge = _make_edge(base, EVIDENCE_SAME_ISIN, shared_isins, old_seg, new_seg)
        return {"kind": "edge", "edge": edge}

    window_splits = _split_rows(new_seg, window_start, window_end)
    bridge = [row for row in window_splits if row["face_value_parsable"]]
    if bridge:
        edge = _make_split_edge(base, bridge[0], old_seg, new_seg)
        return {"kind": "edge", "edge": edge}
    if window_splits:
        return {"kind": "candidate", "candidate": {
            **base,
            "reason_code": REASON_SPLIT_NO_FACE_VALUE,
            "reason": ("A split-subject row exists for the new symbol inside the "
                       "handover window but no parsable from/to face value could be "
                       "read from the official subject string, so it cannot bridge "
                       "the ISIN change."),
            "split_subject_rows": window_splits,
        }}
    outside = _split_rows(new_seg)
    if outside:
        return {"kind": "candidate", "candidate": {
            **base,
            "reason_code": REASON_SPLIT_OUTSIDE_WINDOW,
            "reason": ("The new symbol has an official split row, but its ex-date "
                       "falls outside the handover window, so it does not bridge the "
                       "ISIN change."),
            "split_subject_rows": outside,
        }}
    return {"kind": "candidate", "candidate": {
        **base,
        "reason_code": REASON_NO_LINKAGE,
        "reason": ("Same official company name under two symbols, but the ISINs "
                   "differ and no official split row for the new symbol bridges the "
                   "handover window. A company-name match alone never creates an edge."),
    }}


def _split_rows(new_seg: dict, window_start: str | None = None,
                window_end: str | None = None) -> list[dict]:
    """Official rows of the new symbol whose subject documents a face-value split.

    When a window is given, only split rows whose ex-date falls inside it are
    returned. ``face_value_parsable`` says whether a from/to face value could
    be read from the official subject string; an unparsable split subject is
    never admissible as a bridge.
    """
    found = []
    for row in new_seg.get("rows_detail") or []:
        subject = row.get("subject") or ""
        if not SPLIT_SUBJECT_RE.search(subject):
            continue
        if window_start and window_end:
            if not (window_start <= row["ex_date"] <= window_end):
                continue
        face = split_face_value(subject)
        found.append({
            **row,
            "face_value_from": face[0] if face else None,
            "face_value_to": face[1] if face else None,
            "face_value_parsable": face is not None,
        })
    return found


def _make_edge(base: dict, evidence_class: str, shared_isins: list[str],
               old_seg: dict, new_seg: dict) -> dict:
    """Edge for rule (A): the same ISIN under both symbols."""
    old_last = old_seg["rows_detail"][-1] if old_seg.get("rows_detail") else None
    new_first = new_seg["rows_detail"][0] if new_seg.get("rows_detail") else None
    effective_date = base["handover"]["new_symbol_first_api_date"]
    citation = _row_citation(new_first) if new_first else {}
    citation["isin"] = shared_isins[0]
    citation["shared_isins"] = shared_isins
    citation["supporting_rows"] = [c for c in (
        _row_citation(old_last) if old_last else None,
        _row_citation(new_first) if new_first else None) if c]
    edge = {
        **base,
        "edge_id": f"API|{base['old_symbol']}->{base['new_symbol']}|{effective_date}",
        "effective_date": effective_date,
        "effective_date_basis": "new_symbol_first_api_date",
        "evidence_class": evidence_class,
        "isin_old": old_seg["isins"][0] if old_seg["isins"] else None,
        "isin_new": new_seg["isins"][0] if new_seg["isins"] else None,
        "shared_isins": shared_isins,
        "citation": citation,
        "verification": "official_nse_corporate_actions_api",
        "note": ("Symbol rename derived from the official NSE corporate-actions API: "
                 "the same official company name is used under both symbols and the "
                 "same ISIN appears under both. Derived from the corporate-actions "
                 "API, NOT from symbolchange.csv."),
    }
    return edge


def _make_split_edge(base: dict, split_row: dict, old_seg: dict, new_seg: dict) -> dict:
    """Edge for rule (B): a split bridges the ISIN change."""
    old_last = old_seg["rows_detail"][-1] if old_seg.get("rows_detail") else None
    new_first = new_seg["rows_detail"][0] if new_seg.get("rows_detail") else None
    effective_date = split_row["ex_date"]
    citation = _row_citation(split_row)
    citation["face_value_from"] = split_row["face_value_from"]
    citation["face_value_to"] = split_row["face_value_to"]
    citation["supporting_rows"] = [c for c in (
        _row_citation(old_last) if old_last else None,
        _row_citation(new_first) if new_first else None) if c]
    old_face_values = set(old_seg["face_values"])
    citation["old_symbol_face_values"] = sorted(old_face_values)
    citation["face_value_consistent_with_old_symbol"] = (
        split_row["face_value_from"] in old_face_values if old_face_values else None)
    edge = {
        **base,
        "edge_id": f"API|{base['old_symbol']}->{base['new_symbol']}|{effective_date}",
        "effective_date": effective_date,
        "effective_date_basis": "new_symbol_first_api_date_split_row",
        "evidence_class": EVIDENCE_SPLIT_BRIDGED,
        "isin_old": old_seg["isins"][0] if old_seg["isins"] else None,
        "isin_new": new_seg["isins"][0] if new_seg["isins"] else None,
        "shared_isins": [],
        "split_bridge": {
            "ex_date": split_row["ex_date"],
            "face_value_from": split_row["face_value_from"],
            "face_value_to": split_row["face_value_to"],
            "subject": split_row["subject"],
            "source_file": split_row.get("source_file"),
            "source_row": split_row.get("source_row"),
            "source_url": split_row.get("source_url"),
        },
        "citation": citation,
        "verification": "official_nse_corporate_actions_api",
        "note": ("Symbol rename derived from the official NSE corporate-actions API: "
                 "the same official company name is used under both symbols, the "
                 "ISINs differ, and an official split row for the new symbol bridges "
                 "the handover window, so the ISIN change is explained. Derived from "
                 "the corporate-actions API, NOT from symbolchange.csv."),
    }
    return edge


def _summary(rows: list[dict], edges: list[dict], unresolved: list[dict],
             reuse: list[dict], segments_by_company: dict,
             chain_pairs: list[dict] | None = None) -> dict:
    chain_pairs = chain_pairs or []
    multi_symbol = {key: value for key, value in segments_by_company.items()
                    if len(value) > 1}
    same_isin_pairs = 0
    diff_isin_pairs = 0
    overlapping_pairs = 0
    for symbols in multi_symbol.values():
        ordered = sorted(symbols)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1:]:
                left_seg, right_seg = symbols[left], symbols[right]
                if set(left_seg["isins"]) & set(right_seg["isins"]):
                    same_isin_pairs += 1
                else:
                    diff_isin_pairs += 1
                if _overlaps(left_seg["first_date"], left_seg["last_date"],
                             right_seg["first_date"], right_seg["last_date"]):
                    overlapping_pairs += 1
    companies_overlapping = sum(
        1 for symbols in multi_symbol.values()
        if any(
            _overlaps(symbols[a]["first_date"], symbols[a]["last_date"],
                       symbols[b]["first_date"], symbols[b]["last_date"])
            for i, a in enumerate(sorted(symbols))
            for b in sorted(symbols)[i + 1:]))
    overlapping_candidates = sum(1 for c in unresolved
                                 if c["reason_code"] == REASON_OVERLAP)
    return {
        "api_rows": len(rows),
        "api_symbols": len({row["symbol"] for row in rows}),
        "api_companies": len({row["company_key"] for row in rows}),
        "api_first_date": min((row["ex_date"] for row in rows if row["ex_date"]), default=None),
        "api_last_date": max((row["ex_date"] for row in rows if row["ex_date"]), default=None),
        "companies_with_more_than_one_symbol": len(multi_symbol),
        # One candidate handover (the outermost one) is evaluated per company
        # observed under more than one symbol, so the number of evaluated
        # pairs is the number of such companies.
        "symbol_pairs_evaluated": len(edges) + len(unresolved),
        "symbol_pairs_possible_including_chain_links": sum(
            len(v) * (len(v) - 1) // 2 for v in multi_symbol.values()),
        "rename_chain_inner_links_recorded": len(chain_pairs),
        "symbol_pairs_with_shared_isin": same_isin_pairs,
        "symbol_pairs_with_differing_isins": diff_isin_pairs,
        "symbol_pairs_with_overlapping_api_windows": overlapping_pairs,
        "companies_with_overlapping_api_windows": companies_overlapping,
        "overlapping_window_pairs_flagged_ambiguous": overlapping_candidates,
        "edges_total": len(edges),
        "edges_by_evidence_class": dict(sorted(collections.Counter(
            edge["evidence_class"] for edge in edges).items())),
        "edges_earliest": min((e["effective_date"] for e in edges), default=None),
        "edges_latest": max((e["effective_date"] for e in edges), default=None),
        "unresolved_candidates_total": len(unresolved),
        "unresolved_candidates_by_reason": dict(sorted(collections.Counter(
            c["reason_code"] for c in unresolved).items())),
        "symbol_reuse_total": len(reuse),
        "symbol_reuse_symbols": sorted(entry["symbol"] for entry in reuse),
        "derived_from": "nse_corporate_actions_api",
        "from_symbolchange_csv": False,
    }


# ---------------------------------------------------------------------------
# Symbol-change cross-check (read-only; never creates anything)
# ---------------------------------------------------------------------------

def symbolchange_pairs(path: Path) -> set[tuple[str, str]]:
    """Ordered symbol pairs published in the official symbol-change file."""
    if not path.exists():
        return set()
    content = path.read_bytes()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    pairs = set()
    for row in csv.DictReader(text.splitlines()):
        old = (row.get("OLD SYMBOL") or "").strip()
        new = (row.get("NEW SYMBOL") or "").strip()
        if old and new and old != new:
            pairs.add((old, new))
    return pairs


# ---------------------------------------------------------------------------
# Bars (read-only) handover diagnostics
# ---------------------------------------------------------------------------

def bars_ranges(db_path: Path, symbols: list[str]) -> dict:
    """Read-only bars date ranges per symbol and series.

    Never writes to the database. Returns the observed first/last date per
    symbol per series and the per-symbol overall range, which is what the
    handover consistency check compares against the API-observed window.
    """
    try:
        import duckdb
    except ImportError as error:  # pragma: no cover - declared dependency
        raise RuntimeError("duckdb is required to read bars ranges") from error
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    con = open_catalog(database=db_path)
    try:
        marks = ",".join("?" for _ in symbols)
        rows = con.execute(
            f"SELECT symbol, series, min(date), max(date), count(*) FROM bars "
            f"WHERE symbol IN ({marks}) GROUP BY 1, 2 ORDER BY 1, 2", symbols).fetchall()
    finally:
        con.close()
    out: dict[str, dict] = {}
    for symbol, series, first, last, count in rows:
        entry = out.setdefault(symbol, {"series": {}, "first_date": None, "last_date": None})
        entry["series"][series] = {
            "first_date": first.isoformat() if first else None,
            "last_date": last.isoformat() if last else None,
            "rows": int(count),
        }
        if first and (entry["first_date"] is None or first.isoformat() < entry["first_date"]):
            entry["first_date"] = first.isoformat()
        if last and (entry["last_date"] is None or last.isoformat() > entry["last_date"]):
            entry["last_date"] = last.isoformat()
    for symbol in symbols:
        out.setdefault(symbol, {"series": {}, "first_date": None, "last_date": None})
    return out


def handover_consistency(edge: dict, bars: dict) -> dict:
    """Compare an edge's API window with the bars handover of its two symbols."""
    old = bars.get(edge["old_symbol"]) or {}
    new = bars.get(edge["new_symbol"]) or {}
    window_start = edge["handover"]["window_start"]
    window_end = edge["handover"]["window_end"]
    old_last = old.get("last_date")
    new_first = new.get("first_date")
    ordering_ok = bool(old_last and new_first and old_last <= new_first)
    inside = bool(window_start and window_end and old_last and new_first
                  and window_start <= old_last and new_first <= window_end)
    overlap = bool(window_start and window_end and old_last and new_first
                   and old_last >= window_start and new_first <= window_end)
    return {
        "bars_old_symbol": edge["old_symbol"],
        "bars_new_symbol": edge["new_symbol"],
        "bars_old_last_date": old_last,
        "bars_new_first_date": new_first,
        "bars_old_series": old.get("series", {}),
        "bars_new_series": new.get("series", {}),
        "bars_handover_ordering_ok": ordering_ok,
        "bars_handover_inside_api_window": inside,
        "bars_handover_overlaps_api_window": overlap,
        "consistent_with_bars_handover": bool(inside and ordering_ok),
        "days_old_last_after_window_start": (
            (date.fromisoformat(old_last) - date.fromisoformat(window_start)).days
            if old_last and window_start else None),
        "days_new_first_before_window_end": (
            (date.fromisoformat(window_end) - date.fromisoformat(new_first)).days
            if new_first and window_end else None),
        "note": ("Bars are read-only observations used only to check that the "
                 "API-observed handover window is compatible with the traded "
                 "handover. No bar price is used and no bar row creates an edge."),
    }


# ---------------------------------------------------------------------------
# Freeze impact (read-only) and external-evidence needs
# ---------------------------------------------------------------------------

def unpriced_position_days(marks_path: Path) -> dict:
    """Unpriceable position-days per symbol from the sensitivity marks.

    Read-only. ``unpriced_symbols`` is a ``;``-separated list of the symbols
    that could not be priced in that session; each (session, symbol)
    incidence is one unpriceable position-day. Nothing here creates or
    resolves an identity: it only measures how many position-days a symbol
    contributes to the freeze.
    """
    if not marks_path.exists():
        return {"source": None, "exists": False, "total": 0, "symbols": {},
                "distinct_symbols": 0, "sessions": 0}
    counts: collections.Counter = collections.Counter()
    sessions = 0
    total = 0
    with marks_path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            sessions += 1
            total += int(row.get("unpriced_positions") or 0)
            field = (row.get("unpriced_symbols") or "").strip()
            if not field:
                continue
            counts.update({s.strip() for s in field.split(";") if s.strip()})
    return {
        "source": str(marks_path),
        "exists": True,
        "total": total,
        "symbols": dict(sorted(counts.items())),
        "distinct_symbols": len(counts),
        "sessions": sessions,
        "note": ("One unpriceable position-day per (session, symbol) incidence in "
                 "the `unpriced_symbols` column of the sensitivity marks. The total "
                 "equals the sum of the `unpriced_positions` column."),
    }


def eq_stop_and_trade_counts(db_path: Path, on: str) -> dict:
    """EQ securities that stopped trading on ``on`` and the EQ trade count.

    Read-only. A security whose last observed EQ bar is ``on`` stopped
    trading on that session. The number of EQ symbols that still traded on
    that same session says whether the date is an exchange/archival boundary
    (everything stops at once) or a genuine end of trading for a handful of
    securities (a few stop while the market keeps running). No price is read.
    """
    try:
        import duckdb
    except ImportError as error:  # pragma: no cover - declared dependency
        raise RuntimeError("duckdb is required to read the EQ session activity") from error
    con = open_catalog(database=db_path)
    try:
        traded = con.execute(
            "SELECT count(DISTINCT symbol) FROM bars WHERE date = ? AND series = 'EQ'",
            [on]).fetchone()[0]
        stopped = con.execute("""
            WITH last_eq AS (
                SELECT symbol, max(date) AS last_date
                FROM bars WHERE series = 'EQ' GROUP BY symbol)
            SELECT symbol FROM last_eq WHERE last_date = ? ORDER BY symbol
        """, [on]).fetchall()
    finally:
        con.close()
    return {
        "date": on,
        "eq_symbols_traded_that_session": int(traded),
        "eq_securities_that_stopped": len(stopped),
        "stopped_symbols": [row[0] for row in stopped],
        "note": ("Read-only observation. A security is counted as stopped when its "
                 "last observed EQ bar is this session. No price is read and no "
                 "identity is created or resolved here."),
    }


def delisted_list_coverage(path: Path | None) -> dict | None:
    """Coverage of the official delisted list (rows and date span)."""
    if not path or not path.exists():
        return None
    content = path.read_bytes()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252")
    rows = list(csv.DictReader(text.splitlines()))
    dated = []
    symbols = set()
    for row in rows:
        symbol = (row.get("Symbol") or "").strip()
        if symbol:
            symbols.add(symbol)
        try:
            when = parse_date(row.get("Delisted Date"))
        except ValueError:
            when = None
        if when:
            dated.append(when)
    return {
        "source": str(path),
        "rows": len(rows),
        "earliest": min(dated).isoformat() if dated else None,
        "latest": max(dated).isoformat() if dated else None,
        "rows_with_a_date": len(dated),
        "symbols": sorted(symbols),
        "note": ("Read-only. The official delisted list is bounded: a symbol that is "
                 "absent from it is not evidence that the security still trades, it "
                 "is evidence that this list does not cover the symbol or the period."),
    }


def freeze_ceiling(freeze: dict, bars_db: Path | None,
                   delisted_path: Path | None, top_n: int = 3,
                   coverage_n: int = 10) -> dict:
    """The measured ceiling of the identity work on the freeze.

    The identity layer can only ever make a holding measurable when the
    holding's symbol is the old side of an emitted edge. This measures how
    much of the freeze that could possibly reach, and it separates the two
    very different causes of a frozen holding:

    * an identity gap - the security was renamed and no official source in
      the repository publishes the rename (this is what the API layer
      closes); and
    * the end of trading - the security simply stopped trading and no
      successor exists at all, which no identity edge can ever repair.

    The second cause is the ceiling: a rename edge can only ever reach the
    part of the freeze that is an identity gap.
    """
    counts = freeze.get("symbols") or {}
    total = int(freeze.get("total") or 0)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    top = ranked[:top_n]
    top_days = sum(days for _, days in top)
    coverage_ranked = ranked[:coverage_n]

    symbols_meta = []
    if bars_db and bars_db.exists():
        bars = bars_ranges(bars_db, [symbol for symbol, _ in coverage_ranked])
        for symbol, days in top:
            entry = bars.get(symbol) or {}
            symbols_meta.append({
                "symbol": symbol,
                "position_days": days,
                "share_of_freeze_pct": (round(100.0 * days / total, 2) if total else None),
                "bars_first_observed": entry.get("first_date"),
                "bars_last_observed": entry.get("last_date"),
            })
    else:
        for symbol, days in top:
            symbols_meta.append({
                "symbol": symbol,
                "position_days": days,
                "share_of_freeze_pct": (round(100.0 * days / total, 2) if total else None),
                "bars_first_observed": None,
                "bars_last_observed": None,
            })

    stops = []
    if bars_db and bars_db.exists() and symbols_meta:
        frozen_ranked = [symbol for symbol, _ in coverage_ranked]
        last_dates = sorted({meta["bars_last_observed"] for meta in symbols_meta
                             if meta["bars_last_observed"]})
        for on in last_dates:
            activity = eq_stop_and_trade_counts(bars_db, on)
            stopped = set(activity["stopped_symbols"])
            top_stopping = [meta["symbol"] for meta in symbols_meta
                            if meta["bars_last_observed"] == on]
            activity["top_frozen_symbols_stopping_that_day"] = top_stopping
            activity["frozen_symbols_stopping_that_day"] = [
                symbol for symbol in frozen_ranked if symbol in stopped]
            activity["is_archive_boundary"] = bool(
                activity["eq_symbols_traded_that_session"] == 0)
            activity["note"] = (
                "A handful of securities stopping on a session on which the rest of "
                "the market keeps trading is a genuine end of trading, not an "
                "archive boundary: the archive is not the reason those securities "
                "stopped.")
            stops.append(activity)

    coverage = delisted_list_coverage(delisted_path)
    if coverage:
        listed = set(coverage["symbols"])
        membership = {}
        for symbol, _ in coverage_ranked:
            membership[symbol] = symbol in listed
        coverage = {**coverage, "symbols": None,
                    "checked_symbols": [symbol for symbol, _ in coverage_ranked],
                    "checked_symbols_in_official_delisted_list": membership}

    return {
        "total_unpriced_position_days": total,
        "distinct_unpriced_symbols": freeze.get("distinct_symbols"),
        "top_symbols": symbols_meta,
        "top_symbols_position_days": top_days,
        "top_symbols_share_of_freeze_pct": (round(100.0 * top_days / total, 2)
                                            if total else None),
        "end_of_trading_sessions": stops,
        "official_delisted_list": coverage,
        "note": ("The ceiling is measured, not assumed: the top frozen symbols are "
                 "counted from the sensitivity marks, their last traded session is "
                 "read from the bars, and the official delisted list is checked for "
                 "coverage. No identity edge can make a holding measurable when the "
                 "security has no successor to hand over to."),
    }


def whitespace_variant_companies(rows: list[dict]) -> dict[str, list[dict]]:
    """Company names that differ from another only in whitespace.

    Report-only. Two official company strings that differ only in whitespace
    are still different strings, so they never group together and never
    create an edge. This lookup exists so the report can point at the rows
    that a future official confirmation of the spelling would have to cover,
    instead of quietly widening the match rule.
    """
    by_key: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_key[row["company_key"]].append(row)
    stripped = {key: re.sub(r"\s+", "", key) for key in by_key}
    variants: dict[str, list[dict]] = collections.defaultdict(list)
    for key in sorted(by_key):
        for other in sorted(by_key):
            if other == key or stripped[other] != stripped[key]:
                continue
            group = by_key[other]
            variants[key].append({
                "company": group[0]["company"],
                "company_key": other,
                "symbols": sorted({row["symbol"] for row in group}),
                "rows": len(group),
                "split_rows": [
                    {"ex_date": row["ex_date"], "symbol": row["symbol"],
                     "subject": row["subject"], "face_value": row["face_value"],
                     "source_file": row.get("source_file"),
                     "source_row": row.get("source_row")}
                    for row in group if SPLIT_SUBJECT_RE.search(row.get("subject") or "")],
            })
    return dict(variants)


def _external_evidence_needed(symbol: str, candidate: dict,
                              segments_by_company: dict,
                              whitespace_variants: dict | None = None) -> list[str]:
    """What official evidence would be needed to promote a candidate to an edge.

    Derived from the official rows only; nothing is asserted as known.
    """
    needed = []
    company_key = candidate["company_name_normalised"]
    old_range = candidate["old_symbol_api_range"]
    new_range = candidate["new_symbol_api_range"]
    needed.append(
        "An official dated symbol-change record for "
        f"{candidate['old_symbol']} -> {candidate['new_symbol']} (the official "
        "symbol-change CSV row, or an NSE/company filing that states the rename and "
        "its effective date).")
    if not (set(old_range["isins"]) & set(new_range["isins"])):
        needed.append(
            "OR an official dated ISIN-change record binding old ISIN "
            f"{old_range['isins'] or 'none observed'} to new ISIN "
            f"{new_range['isins'] or 'none observed'}, which would evidence that the "
            "two symbols are one security.")
        new_seg = segments_by_company.get(company_key, {}).get(candidate["new_symbol"])
        if new_seg:
            splits = _split_rows(new_seg)
            if splits:
                needed.append(
                    "OR an official split row for "
                    f"{candidate['new_symbol']} whose ex-date falls inside the "
                    f"handover window {candidate['handover']['window_start']}.."
                    f"{candidate['handover']['window_end']}; the observed split "
                    f"row(s) {[s['ex_date'] for s in splits]} fall outside it.")
            else:
                needed.append(
                    f"OR an official split row for {candidate['new_symbol']} with a "
                    "parsable from/to face value inside the handover window "
                    f"{candidate['handover']['window_start']}.."
                    f"{candidate['handover']['window_end']}; the API has no split "
                    "subject for that symbol at all.")
            # The row that actually lands in the handover window is what the
            # new symbol is first observed doing. Name it when it is not a
            # split: a capital reduction, a demerger or an AGM cannot explain
            # an ISIN change the way a face-value split does.
            bridging = next((row for row in (new_seg.get("rows_detail") or [])
                             if row.get("ex_date") == new_range["first_date"]), None)
            if bridging is not None and not SPLIT_SUBJECT_RE.search(
                    bridging.get("subject") or ""):
                needed.append(
                    f"The official row that lands in the handover window for "
                    f"{candidate['new_symbol']} is '{bridging.get('subject')}' "
                    f"({bridging.get('source_file')} row {bridging.get('source_row')}, "
                    f"ex {bridging.get('ex_date')}). That is not a face-value split, "
                    "so it cannot bridge the ISIN change.")
    if old_range["face_values"] and new_range["face_values"] \
            and old_range["face_values"] != new_range["face_values"]:
        needed.append(
            "A dated official record explaining the face-value change "
            f"{old_range['face_values']} -> {new_range['face_values']} (the API "
            "rows do not document one as a split).")
    for variant in (whitespace_variants or {}).get(company_key, []):
        dates = [row["ex_date"] for row in variant["split_rows"]]
        needed.append(
            f"The API carries {variant['rows']} row(s) for symbol(s) "
            f"{variant['symbols']} under the whitespace-only company spelling "
            f"'{variant['company']}'. That is a different official string, so it "
            "never groups with this company and creates no edge. An official "
            "confirmation that both spellings denote the same company would be "
            "needed before any of those rows could be used"
            + (f" (their split row(s) are dated {dates}, which is outside the "
               f"handover window {candidate['handover']['window_start']}.."
               f"{candidate['handover']['window_end']}, so a bridge would also need "
               "the split ex-date inside the window)."
               if dates else "."))
    needed.append(
        "Until such a dated official record exists, the "
        f"{symbol} holding stays unpriceable: no price, no fuzzy name match and no "
        "latest-ISIN join may be used to bridge it.")
    return needed


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def _frozen_symbol_rows(frozen: list[str], result: dict,
                        bars: dict,
                        whitespace_variants: dict | None = None) -> list[dict]:
    """Per frozen symbol: edge, candidate or nothing, with its citation."""
    edges = result["edges"]
    candidates = result["unresolved_candidates"]
    segments_by_company = result.get("segments_by_company", {})
    rows = []
    for symbol in frozen:
        related_edges = [e for e in edges
                         if e["old_symbol"] == symbol or e["new_symbol"] == symbol]
        related_candidates = [c for c in candidates
                              if c["old_symbol"] == symbol or c["new_symbol"] == symbol]
        bars_entry = bars.get(symbol) or {}
        row = {
            "symbol": symbol,
            "bars_first_date": bars_entry.get("first_date"),
            "bars_last_date": bars_entry.get("last_date"),
            "bars_series": bars_entry.get("series", {}),
            "edges": related_edges,
            "unresolved_candidates": related_candidates,
            "edge_count": len(related_edges),
            "candidate_count": len(related_candidates),
        }
        if related_edges:
            edge = related_edges[0]
            row["status"] = "edge_emitted"
            row["edge"] = edge
            row["evidence_class"] = edge["evidence_class"]
            row["effective_date"] = edge["effective_date"]
            row["citation"] = edge["citation"]
            row["handover"] = edge["handover"]
            row["bars_consistency"] = handover_consistency(edge, bars)
            row["external_evidence_needed"] = []
            row["resolution_note"] = (
                "The rename is admissible: the official corporate-actions API "
                "carries the handover and the required identity evidence. It is "
                "published in api_identity_edges.jsonl as an additional identity "
                "layer; the symbol-change layer is unchanged.")
        elif related_candidates:
            candidate = related_candidates[0]
            row["status"] = "unresolved_candidate_no_edge"
            row["evidence_class"] = None
            row["effective_date"] = None
            row["citation"] = None
            row["handover"] = candidate["handover"]
            row["reason_code"] = candidate["reason_code"]
            row["reason"] = candidate["reason"]
            row["bars_consistency"] = _candidate_consistency(candidate, bars)
            row["external_evidence_needed"] = _external_evidence_needed(
                symbol, candidate, segments_by_company, whitespace_variants)
            row["resolution_note"] = (
                "Stays unresolved by design: a company-name match without an ISIN "
                "linkage or a bridged split never becomes an edge.")
        else:
            row["status"] = "no_same_company_symbol_in_api"
            row["evidence_class"] = None
            row["effective_date"] = None
            row["citation"] = None
            row["handover"] = None
            row["reason_code"] = "no_same_company_symbol_in_api"
            row["reason"] = ("The official corporate-actions API has no other symbol "
                             "under this exact company name.")
            row["bars_consistency"] = None
            row["external_evidence_needed"] = []
            row["resolution_note"] = (
                "No same-company sibling exists in the corporate-actions API, so "
                "this layer cannot help; other official evidence is needed.")
        rows.append(row)
    return rows


def _candidate_consistency(candidate: dict, bars: dict) -> dict:
    """Handover compatibility diagnostic for a non-edge candidate."""
    pseudo = {
        "old_symbol": candidate["old_symbol"],
        "new_symbol": candidate["new_symbol"],
        "handover": candidate["handover"],
    }
    result = handover_consistency(pseudo, bars)
    result["consistent_with_bars_handover"] = None
    result["note"] = ("No edge was emitted, so no consistency verdict is issued. The "
                      "diagnostic only shows whether the observed handover window is "
                      "compatible with the traded handover.")
    return result


def build_report(api_rows: list[dict], bars_db: Path | None,
                 symbolchange: Path | None,
                 frozen: list[str],
                 marks: Path | None = None,
                 delisted: Path | None = None) -> dict:
    """Assemble the full report payload."""
    result = build_api_identity(api_rows)
    bars = bars_ranges(bars_db, sorted({row["symbol"] for row in api_rows})) \
        if bars_db and bars_db.exists() else {}

    change_pairs = symbolchange_pairs(symbolchange) if symbolchange else set()
    for edge in result["edges"]:
        edge["present_in_symbolchange_csv"] = (
            (edge["old_symbol"], edge["new_symbol"]) in change_pairs
            if change_pairs else None)

    for edge in result["edges"]:
        edge["bars_consistency"] = handover_consistency(edge, bars) if bars else None

    frozen_rows = _frozen_symbol_rows(frozen, result, bars,
                                      whitespace_variant_companies(api_rows))
    with_edge = [row for row in frozen_rows if row["status"] == "edge_emitted"]

    freeze = unpriced_position_days(marks) if marks else {
        "source": None, "exists": False, "total": 0, "symbols": {},
        "distinct_symbols": 0, "sessions": 0,
        "note": "No sensitivity marks file supplied; the freeze impact was not measured.",
    }
    frozen_days = {row["symbol"]: int(freeze["symbols"].get(row["symbol"], 0))
                   for row in frozen_rows}
    measurable = sum(days for row, days in frozen_days.items()
                     if any(e["old_symbol"] == row or e["new_symbol"] == row
                            for e in result["edges"]))
    frozen_total = sum(frozen_days.values())
    freeze_impact = {
        "marks_source": freeze.get("source"),
        "total_unpriced_position_days": freeze.get("total"),
        "distinct_unpriced_symbols": freeze.get("distinct_symbols"),
        "frozen_symbols": [
            {
                "symbol": row["symbol"],
                "unpriced_position_days": frozen_days[row["symbol"]],
                "share_of_all_unpriced_pct": (
                    round(100.0 * frozen_days[row["symbol"]] / freeze["total"], 2)
                    if freeze.get("total") else None),
                "identity_status": row["status"],
                "evidence_class": row.get("evidence_class"),
                "effective_date": row.get("effective_date"),
                "reason_code": row.get("reason_code"),
                "days_would_become_measurable": (
                    frozen_days[row["symbol"]] if row["status"] == "edge_emitted" else 0),
                "days_still_unresolved": (
                    0 if row["status"] == "edge_emitted" else frozen_days[row["symbol"]]),
                "external_evidence_needed": row.get("external_evidence_needed", []),
            }
            for row in frozen_rows
        ],
        "frozen_symbols_unpriced_position_days_total": frozen_total,
        "frozen_symbols_share_of_all_unpriced_pct": (
            round(100.0 * frozen_total / freeze["total"], 2) if freeze.get("total") else None),
        "days_made_measurable_by_emitted_edges": measurable,
        "days_still_unresolved": frozen_total - measurable,
        "note": ("A position-day is counted as unpriceable when the session's "
                 "`unpriced_symbols` list contains the symbol. An emitted identity "
                 "edge makes the holding transferable to the successor symbol, so "
                 "the corresponding position-days become measurable; the counts "
                 "here are the ceiling of that effect, not a realised backtest "
                 "result. The edges are published as an additional identity layer "
                 "and are not yet consumed by the backtest engine."),
    }

    summary = dict(result["summary"])
    ceiling = freeze_ceiling(freeze, bars_db, delisted)
    summary.update({
        "source": SOURCE_LABEL,
        "symbolchange_csv_checked": str(symbolchange) if symbolchange else None,
        "symbolchange_csv_rows": len(change_pairs),
        "edges_also_in_symbolchange_csv": sum(
            1 for edge in result["edges"] if edge.get("present_in_symbolchange_csv")),
        "edges_missing_from_symbolchange_csv": sum(
            1 for edge in result["edges"]
            if change_pairs and not edge.get("present_in_symbolchange_csv")),
        "bars_source": str(bars_db) if bars_db else None,
        "bars_read_only": True,
        "frozen_symbols_checked": list(frozen),
        "frozen_symbols_with_edge": len(with_edge),
        "frozen_symbols_with_edge_symbols": [row["symbol"] for row in with_edge],
        "frozen_symbols_without_edge": [
            row["symbol"] for row in frozen_rows if row["status"] != "edge_emitted"],
        "frozen_symbols_by_status": dict(sorted(collections.Counter(
            row["status"] for row in frozen_rows).items())),
        "freeze_total_unpriced_position_days": freeze.get("total"),
        "freeze_frozen_symbols_unpriced_position_days": frozen_total,
        "freeze_days_made_measurable_by_emitted_edges": measurable,
        "freeze_days_still_unresolved": frozen_total - measurable,
        "freeze_top_symbols_share_of_freeze_pct": ceiling.get(
            "top_symbols_share_of_freeze_pct"),
    })

    return {
        "summary": summary,
        "edges": result["edges"],
        "unresolved_candidates": result["unresolved_candidates"],
        "rename_chain_inner_links": result["rename_chain_inner_links"],
        "symbol_reuse": result["symbol_reuse"],
        "frozen_symbols": frozen_rows,
        "freeze_impact": freeze_impact,
        "freeze_ceiling": ceiling,
        "method": {
            "source": SOURCE_LABEL,
            "name_matching": ("Exact equality of the official API `comp` field after "
                              "whitespace-run collapsing and case folding. No fuzzy, "
                              "token, edit-distance, legal-word-stripping or "
                              "similarity join is used anywhere."),
            "price_use": ("None. No price-like field is read from the API rows and no "
                          "bar price is consulted; bars are used only to check the "
                          "handover dates of an already-derived edge."),
            "rule_a_same_isin": ("Same official company name under two symbols AND the "
                                 "same non-blank ISIN under both -> rename edge."),
            "rule_b_split_bridged": ("Same official company name, different ISINs, and "
                                      "an official split row for the new symbol whose "
                                      "ex-date falls between the old symbol's last API "
                                      "row and the new symbol's first API row -> "
                                      "rename edge."),
            "rule_c_unresolved": ("Everything else -> unresolved_candidates with an "
                                  "explicit reason. A company-name match alone never "
                                  "creates an edge."),
            "rule_d_one_handover_per_company": ("Exactly one candidate pair per company "
                                                "observed under more than one symbol: the "
                                                "outermost handover (the symbol observed "
                                                "first -> the symbol observed last). A "
                                                "company seen under three or more symbols "
                                                "is a chain of renames; its inner links are "
                                                "recorded in rename_chain_inner_links and "
                                                "are never evaluated as renames."),
            "overlap_rule": ("Two symbols of one company whose API date ranges overlap "
                             "(both active at the same time) are ambiguous and are "
                             "never an edge."),
            "reuse_rule": ("A symbol string that appears under more than one official "
                           "company name is flagged and no link is made across the "
                           "reuse."),
            "effective_date": ("The observed handover: the first date the new symbol "
                               "appears in the official API stream. The last date the "
                               "old symbol was seen and the resulting window are "
                               "recorded on every edge."),
            "freeze_measurement": ("Unpriceable position-days are read from the "
                                   "sensitivity marks (`unpriced_symbols`, `;` "
                                   "separated). One position-day per (session, symbol) "
                                   "incidence; the total equals the sum of the "
                                   "`unpriced_positions` column."),
        },
        "limitations": [
            "The corporate-actions API is an official but not complete historical database; an absent row is not proof that no event occurred.",
            "The API returns NSE's own identity strings, which are not a point-in-time security master; an API row is an observation, not a verified lineage.",
            "A company-name match that is not backed by a shared ISIN or a bridged split stays unresolved on purpose; it is not an edge.",
            "The split bridge requires the split row to be the new symbol's earliest official API row; a split recorded only after that row does not bridge the window.",
            "Company names are compared as exact strings, so two spellings of one company (for example a different spacing inside brackets) never match and stay unresolved.",
            "No edge here has been through the symbol-change file's ISIN bracketing or bars continuity audits; this layer is additive and does not replace the official file.",
            "The freeze counts are the ceiling of what the emitted edges could make measurable; the backtest engine does not read this layer yet, so no realised effect is claimed.",
        ],
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Identity edges from the official NSE corporate-actions API",
        "",
        "Additional identity layer derived from NSE's own corporate-actions API, for the",
        "renames the official symbol-change CSV does not publish. No price is used and a",
        "company-name match alone never creates an edge.",
        "",
        "## Source",
        "",
        f"- {summary['source']}",
        f"- API rows: {summary['api_rows']} | symbols: {summary['api_symbols']} | "
        f"companies: {summary['api_companies']}",
        f"- API date range: {summary['api_first_date']} .. {summary['api_last_date']}",
        f"- symbol-change CSV checked: {summary.get('symbolchange_csv_checked')} "
        f"({summary.get('symbolchange_csv_rows')} rename rows)",
        "",
        "## Counts",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| companies with more than one symbol | {summary['companies_with_more_than_one_symbol']} |",
        f"| symbol pairs evaluated (one outer handover per company) | {summary['symbol_pairs_evaluated']} |",
        f"| symbol pairs possible including rename-chain inner links | {summary['symbol_pairs_possible_including_chain_links']} |",
        f"| rename-chain inner links recorded, not evaluated | {summary['rename_chain_inner_links_recorded']} |",
        f"| symbol pairs with a shared ISIN | {summary['symbol_pairs_with_shared_isin']} |",
        f"| symbol pairs with differing ISINs | {summary['symbol_pairs_with_differing_isins']} |",
        f"| overlapping-window pairs flagged ambiguous (evaluated) | {summary['overlapping_window_pairs_flagged_ambiguous']} |",
        f"| symbol pairs with overlapping API windows (all combinations) | {summary['symbol_pairs_with_overlapping_api_windows']} |",
        f"| companies with overlapping API windows | {summary['companies_with_overlapping_api_windows']} |",
        f"| edges total | {summary['edges_total']} |",
    ]
    for name, count in summary["edges_by_evidence_class"].items():
        lines.append(f"| edges: {name} | {count} |")
    lines.append(f"| unresolved candidates | {summary['unresolved_candidates_total']} |")
    for name, count in summary["unresolved_candidates_by_reason"].items():
        lines.append(f"| unresolved: {name} | {count} |")
    lines.append(f"| symbol reuse flagged | {summary['symbol_reuse_total']} |")
    lines.append(f"| edges also in symbolchange.csv | {summary.get('edges_also_in_symbolchange_csv')} |")
    lines.append(f"| edges missing from symbolchange.csv | {summary.get('edges_missing_from_symbolchange_csv')} |")
    lines.append("")

    lines += ["## Frozen symbols", "",
              f"Checked: {', '.join(summary['frozen_symbols_checked'])}",
              "",
              "| symbol | status | evidence | effective date | bars consistency | reason |",
              "| --- | --- | --- | --- | --- | --- |"]
    for row in report["frozen_symbols"]:
        consistency = row.get("bars_consistency") or {}
        if row["status"] == "edge_emitted":
            verdict = "consistent" if consistency.get("consistent_with_bars_handover") else "inconsistent"
        elif row["status"] == "unresolved_candidate_no_edge":
            verdict = "no edge (diagnostic only)"
        else:
            verdict = "n/a"
        lines.append(
            f"| {row['symbol']} | {row['status']} | {row.get('evidence_class') or '-'} | "
            f"{row.get('effective_date') or '-'} | {verdict} | "
            f"{row.get('reason_code') or '-'} |")
    lines.append("")

    lines += ["## Edges", "",
              "| old | new | effective | class | company | ISIN | citation |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for edge in report["edges"]:
        citation = edge["citation"]
        cite = (f"{citation.get('source_file')} row {citation.get('source_row')} "
                f"ex {citation.get('ex_date')}")
        lines.append(
            f"| {edge['old_symbol']} | {edge['new_symbol']} | {edge['effective_date']} | "
            f"{edge['evidence_class']} | {edge['company_name']} | "
            f"{citation.get('isin') or '-'} | {cite} |")
    lines.append("")

    freeze = report.get("freeze_impact", {})
    with_edge = [row["symbol"] for row in freeze.get("frozen_symbols", [])
                 if row["days_would_become_measurable"]]
    without_edge = [row["symbol"] for row in freeze.get("frozen_symbols", [])
                    if row["days_still_unresolved"]]
    lines += ["## Impact on the freeze", "",
              f"- Unpriceable position-days in the sensitivity run: "
              f"**{freeze.get('total_unpriced_position_days')}** across "
              f"**{freeze.get('distinct_unpriced_symbols')}** symbols.",
              f"- The five frozen symbols with a same-company sibling in the API "
              f"contribute **{freeze.get('frozen_symbols_unpriced_position_days_total')}** "
              f"position-days "
              f"({freeze.get('frozen_symbols_share_of_all_unpriced_pct')}%).",
              f"- Emitted identity edges make **{freeze.get('days_made_measurable_by_emitted_edges')}** "
              f"position-days measurable; **{freeze.get('days_still_unresolved')}** stay "
              "unresolved.",
              ]
    if with_edge:
        lines.append(
            "- The single admissible edge (" + ", ".join(with_edge) + ") therefore makes "
            f"**{freeze.get('days_made_measurable_by_emitted_edges')}** of the "
            f"**{freeze.get('total_unpriced_position_days')}** unpriceable "
            "position-days potentially measurable.")
    if without_edge:
        lines.append(
            "- The other " + str(len(without_edge)) + " (" + ", ".join(without_edge) +
            ") remain unresolved because their ISIN change has no face-value-split "
            "bridge: the official API row that lands in each handover window is not a "
            "split, so no rename edge can be emitted for them from the saved "
            "sources.")
    lines.append(
        "- This is the identity layer's ceiling, not a realised backtest result. "
        "Resolving WHICH security the holding is does not by itself carry the "
        "holding across the handover: the share count across a split-bridged "
        "handover is a separate question that the backtest runner gates "
        "separately, and no 1:1 share conversion is ever implied by an edge here.")
    lines += ["",
              "| symbol | unpriceable position-days | share of freeze | identity status | effective date | days measurable | days still unresolved |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for row in freeze.get("frozen_symbols", []):
        lines.append(
            f"| {row['symbol']} | {row['unpriced_position_days']} | "
            f"{row.get('share_of_all_unpriced_pct')}% | "
            f"{row['identity_status']} | {row.get('effective_date') or '-'} | "
            f"{row['days_would_become_measurable']} | {row['days_still_unresolved']} |")
    lines.append("")
    lines += ["### External evidence each unresolved symbol needs", ""]
    for row in freeze.get("frozen_symbols", []):
        if not row.get("external_evidence_needed"):
            continue
        lines.append(f"- **{row['symbol']}** ({row.get('reason_code')}, "
                     f"{row['unpriced_position_days']} position-days):")
        for item in row["external_evidence_needed"]:
            lines.append(f"  - {item}")
    lines.append("")

    lines += ["## Unresolved candidates", "",
              "| old | new | company | reason |",
              "| --- | --- | --- | --- |"]
    for candidate in report["unresolved_candidates"]:
        lines.append(
            f"| {candidate['old_symbol']} | {candidate['new_symbol']} | "
            f"{candidate['company_name']} | {candidate['reason_code']} |")
    lines.append("")

    chain = report.get("rename_chain_inner_links") or []
    if chain:
        lines += ["## Rename-chain inner links (recorded, not evaluated)", "",
                  "A company observed under three or more symbols is a chain of "
                  "renames. Only the outer handover is evaluated; the inner links "
                  "below are recorded so nothing is hidden, and they are never "
                  "evaluated as renames.", "",
                  "| old | new | company | reason |",
                  "| --- | --- | --- | --- |"]
        for link in chain:
            lines.append(
                f"| {link['old_symbol']} | {link['new_symbol']} | "
                f"{link['company_name']} | {link['reason_code']} |")
        lines.append("")

    ceiling = report.get("freeze_ceiling") or {}
    if ceiling:
        lines += ["## The measured ceiling of the freeze", "",
                  "An identity edge can only make a holding measurable when the "
                  "holding's symbol is the old side of an edge. This section says how "
                  "much of the freeze that could ever reach, and separates the two "
                  "causes of a frozen holding: an identity gap (a rename no official "
                  "source in the repository publishes) and the end of trading (no "
                  "successor exists at all, which no edge can repair).", "",
                  f"- Unpriceable position-days in the sensitivity run: "
                  f"**{ceiling.get('total_unpriced_position_days')}** across "
                  f"**{ceiling.get('distinct_unpriced_symbols')}** symbols.",
                  f"- The top {len(ceiling.get('top_symbols') or [])} frozen symbols "
                  f"contribute **{ceiling.get('top_symbols_position_days')}** "
                  f"position-days "
                  f"(**{ceiling.get('top_symbols_share_of_freeze_pct')}%** of the "
                  "freeze).",
                  ""]
        lines += ["| symbol | unpriceable position-days | share of freeze | last traded |",
                  "| --- | --- | --- | --- |"]
        for meta in ceiling.get("top_symbols") or []:
            lines.append(
                f"| {meta['symbol']} | {meta['position_days']} | "
                f"{meta['share_of_freeze_pct']}% | "
                f"{meta.get('bars_last_observed') or '-'} |")
        lines.append("")
        for stop in ceiling.get("end_of_trading_sessions") or []:
            stopped = stop["eq_securities_that_stopped"]
            verb = ("1 EQ security has its last observed EQ bar on that session"
                    if stopped == 1 else
                    f"{stopped} EQ securities have their last observed EQ bar on "
                    "that session")
            lines += [
                f"**End of trading on {stop['date']}**: {verb}, while "
                f"**{stop['eq_symbols_traded_that_session']}** EQ securities still "
                "traded on it. A handful of securities stopping on a session on "
                "which the rest of the market keeps trading is a genuine end of "
                "trading, not an archive boundary.",
                "",
            ]
            if stop.get("top_frozen_symbols_stopping_that_day"):
                lines += ["Top frozen symbols stopping that day: "
                          + ", ".join(f"`{s}`" for s in
                                      stop["top_frozen_symbols_stopping_that_day"]),
                          ""]
            if stop.get("frozen_symbols_stopping_that_day"):
                lines += ["Frozen symbols among the securities that stopped that day: "
                          + ", ".join(f"`{s}`" for s in
                                      stop["frozen_symbols_stopping_that_day"]),
                          ""]
        coverage = ceiling.get("official_delisted_list") or {}
        if coverage:
            membership = coverage.get("checked_symbols_in_official_delisted_list") or {}
            absent = [symbol for symbol, present in membership.items() if not present]
            present = [symbol for symbol, present in membership.items() if present]
            lines += [
                f"The official delisted list holds **{coverage.get('rows')}** rows and "
                f"covers **{coverage.get('earliest')} .. {coverage.get('latest')}** "
                "only. It therefore says nothing about a symbol outside that span.",
            ]
            if absent:
                lines.append("")
                lines.append("Frozen symbols **absent** from the official delisted "
                             "list (checked: " + ", ".join(f"`{s}`" for s in
                                                            coverage.get("checked_symbols") or [])
                             + "): " + ", ".join(f"`{s}`" for s in absent) + ".")
            if present:
                lines.append("")
                lines.append("Frozen symbols **present** in the official delisted "
                             "list: " + ", ".join(f"`{s}`" for s in present) + ".")
            lines.append("")

    lines += ["## Method", ""]
    for key, value in report["method"].items():
        lines.append(f"- **{key}**: {value}")
    lines.append("")
    lines += ["## Limitations", ""]
    for item in report["limitations"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build(data: Path, bars_db: Path | None, symbolchange: Path | None,
          frozen: list[str], marks: Path | None = None,
          delisted: Path | None = None) -> dict:
    api_path = data / API_RELATIVE
    if not api_path.exists():
        raise FileNotFoundError(api_path)
    api_rows = parse_api_rows(api_path)
    report = build_report(api_rows, bars_db, symbolchange, frozen, marks, delisted)
    write_jsonl(data / "processed/identity/api_identity_edges.jsonl", report["edges"])
    write_json(data / "reports/identity-api-edges.json", report)
    (data / "reports/identity-api-edges.md").write_text(
        render_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--bars-db", type=Path, default=None)
    parser.add_argument("--no-bars", action="store_true")
    parser.add_argument("--symbolchange", type=Path, default=None)
    parser.add_argument("--delisted", type=Path, default=None,
                        help="Official NSE delisted list CSV (read-only)")
    parser.add_argument("--marks", type=Path, default=None,
                        help="Sensitivity equity marks CSV used for the freeze impact")
    parser.add_argument("--frozen-symbols", default=",".join(DEFAULT_FROZEN_SYMBOLS))
    args = parser.parse_args()
    bars_db = None if args.no_bars else (args.bars_db or args.data / "market.duckdb")
    symbolchange = args.symbolchange or (args.data / SYMBOLCHANGE_RELATIVE)
    delisted = args.delisted or (args.data / "raw/older_actions/nse-delisted.csv")
    marks = args.marks or (args.data / "processed/backtest/sensitivity/equity_marks.csv")
    frozen = [s.strip() for s in args.frozen_symbols.split(",") if s.strip()]
    report = build(args.data, bars_db, symbolchange, frozen, marks, delisted)
    print(json.dumps({
        "edges_total": report["summary"]["edges_total"],
        "edges_by_evidence_class": report["summary"]["edges_by_evidence_class"],
        "unresolved_candidates_total": report["summary"]["unresolved_candidates_total"],
        "unresolved_candidates_by_reason": report["summary"]["unresolved_candidates_by_reason"],
        "overlapping_window_pairs_flagged_ambiguous":
            report["summary"]["overlapping_window_pairs_flagged_ambiguous"],
        "rename_chain_inner_links_recorded":
            report["summary"]["rename_chain_inner_links_recorded"],
        "symbol_reuse_total": report["summary"]["symbol_reuse_total"],
        "frozen_symbols_with_edge": report["summary"]["frozen_symbols_with_edge"],
        "frozen_symbols_with_edge_symbols": report["summary"]["frozen_symbols_with_edge_symbols"],
        "frozen_symbols_without_edge": report["summary"]["frozen_symbols_without_edge"],
        "freeze_total_unpriced_position_days": report["summary"]["freeze_total_unpriced_position_days"],
        "freeze_frozen_symbols_unpriced_position_days":
            report["summary"]["freeze_frozen_symbols_unpriced_position_days"],
        "freeze_days_made_measurable_by_emitted_edges":
            report["summary"]["freeze_days_made_measurable_by_emitted_edges"],
        "freeze_days_still_unresolved": report["summary"]["freeze_days_still_unresolved"],
        "freeze_top_symbols_share_of_freeze_pct":
            report["summary"]["freeze_top_symbols_share_of_freeze_pct"],
    }, indent=2))


if __name__ == "__main__":
    main()
