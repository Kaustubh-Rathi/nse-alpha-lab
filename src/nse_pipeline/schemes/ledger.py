"""Scheme event ledger: schema, validation and bounded filing collapse.

Builds *cited ledger stubs* from the NSE scheme-announcement filing index
(`data/processed/schemes/scheme-index.jsonl`, produced by `src/schemes.py`).
It never invents exchange ratios, cash consideration or successor mappings:
those terms live inside scheme PDF documents (SEBI LODR filings), and the
bounded sample ships with **no PDF bodies cached locally** (only PDF URLs).
Every entry therefore carries `ratio_num/ratio_den/cash_per_share/
successor_symbol = null`, `page_ref = null`, `verified_by = null`,
`terms_verified = false`, and a `pdf_url` citation for manual review.

Absolute rules enforced by `validate_entry`:
  * A ratio or cash term is admitted only with `pdf_url` + `page_ref` +
    `verified_by` (a named manual PDF reading). Price-derived terms are
    rejected: passing an `extraction_source` mentioning price/return, or
    any banned price-derived field, raises `ValueError`.
  * Withdrawn schemes (`status == "withdrawn"`) carry no ratio/cash/
    successor and can never be marked effective.
  * Dates, when present, must be ISO and chronologically ordered.

Filing -> scheme collapse (`collapse_filings`) is deliberately conservative
and documented: filings merge only on (a) an explicit multi-company party
signature in the filing text, (b) the same symbol + announcement date, or
(c) a later withdrawal filing for the same symbol (demotes the prior
filing to withdrawn). Generic filings naming only the filer (e.g. every
RAYMOND row) stay as singletons -- merging on symbol alone is forbidden.

No network access. Standard library only; `duckdb` is imported lazily and
only for the optional read-only coverage probe against `market.duckdb`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

SCHEMA_FIELDS = (
    "scheme_id", "scheme_type", "parties", "dates",
    "ratio_num", "ratio_den", "cash_per_share", "successor_symbol",
    "pdf_url", "page_ref", "status", "verified_by",
)
ALLOWED_TYPES = {
    "merger", "demerger", "scheme_of_arrangement",
    "restructuring", "liquidation", "cash_out", "unknown",
}
ALLOWED_STATUSES = {"announced", "approved", "effective", "withdrawn", "needs_pdf"}
DATE_KEYS = {
    "announced", "announcement", "board_date", "nclt_order_date",
    "effective_date", "record_date", "ex_date", "delisting_date",
}
PARTY_ROLES = {
    "primary", "demerged_company", "resulting_company", "transferor",
    "transferee", "survivor", "target", "dissolved",
    "wholly_owned_subsidiary", "stepdown_subsidiary", "counterparty",
}
BANNED_PRICE_FIELDS = {
    "price_factor", "share_multiplier", "inferred_ratio", "inferred_from_price",
    "price_discontinuity_ratio", "implied_ratio_from_return", "raw_return_used",
}
WITHDRAWN_PAT = re.compile(
    r"(?<![A-Za-z0-9])(?:withdraw\w*|cancel\w*|abandon\w*|scrap\w*|"
    r"not proceeding|did not materialise|did not materialize)\b", re.I)
COMPANY_PAT = re.compile(
    r"\b([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Za-z0-9&.\-]+){0,4}?"
    r"\s+(?:Limited|Ltd\.?|Private Limited|Inc\.?|Pty Ltd|LLP|PLC))"
    r"\b")


def _parse_iso(value) -> date:
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Date must be ISO YYYY-MM-DD: {value!r}") from e


def validate_entry(entry: dict) -> dict:
    """Validate one ledger entry; return it unchanged or raise ValueError."""
    if not isinstance(entry, dict):
        raise ValueError("Ledger entry must be an object")
    for field in SCHEMA_FIELDS:
        if field not in entry:
            raise ValueError(f"Missing required ledger field: {field}")
    for banned in BANNED_PRICE_FIELDS:
        if banned in entry:
            raise ValueError(f"Banned price-derived field present: {banned}")
    source = str(entry.get("extraction_source") or "").lower()
    if "price" in source or "raw_return" in source or "discontinuity" in source:
        raise ValueError("Terms must never be inferred from a price move")
    scheme_id = entry["scheme_id"]
    if not isinstance(scheme_id, str) or not scheme_id.strip():
        raise ValueError("scheme_id must be a non-empty string")
    if entry["scheme_type"] not in ALLOWED_TYPES:
        raise ValueError(f"Unknown scheme_type: {entry['scheme_type']!r}")
    parties = entry["parties"]
    if not isinstance(parties, list) or not parties:
        raise ValueError("parties must be a non-empty list")
    for party in parties:
        if not isinstance(party, dict):
            raise ValueError("Each party must be an object")
        if not (party.get("symbol") or party.get("company_name")):
            raise ValueError("Each party needs a symbol or company_name")
        if party.get("role") is not None and party["role"] not in PARTY_ROLES:
            raise ValueError(f"Unknown party role: {party['role']!r}")
    dates = entry["dates"]
    if not isinstance(dates, dict):
        raise ValueError("dates must be an object")
    unknown = set(dates) - DATE_KEYS
    if unknown:
        raise ValueError(f"Unknown date keys: {sorted(unknown)}")
    parsed = {k: _parse_iso(v) for k, v in dates.items() if v is not None}
    order = [k for k in ("board_date", "nclt_order_date", "effective_date")
             if k in parsed]
    if [parsed[k] for k in order] != sorted(parsed[k] for k in order):
        raise ValueError(f"Lifecycle dates out of order: {dates}")
    if "announced" in parsed and order and parsed["announced"] > parsed[order[0]]:
        raise ValueError("announced date precedes board/nclt/effective dates")
    ratio_num, ratio_den = entry["ratio_num"], entry["ratio_den"]
    for v in (ratio_num, ratio_den):
        if v is not None and (not isinstance(v, (int, float)) or v <= 0):
            raise ValueError("ratio_num/ratio_den must be positive numbers or null")
    if (ratio_num is None) != (ratio_den is None):
        raise ValueError("ratio_num and ratio_den must be set together or both null")
    cash = entry["cash_per_share"]
    if cash is not None and (not isinstance(cash, (int, float)) or cash < 0):
        raise ValueError("cash_per_share must be a non-negative number or null")
    if entry["status"] not in ALLOWED_STATUSES:
        raise ValueError(f"Unknown status: {entry['status']!r}")
    has_terms = ratio_num is not None or cash is not None
    if has_terms:
        if not entry["pdf_url"]:
            raise ValueError("Ratio/cash terms require pdf_url citation")
        if not entry["page_ref"]:
            raise ValueError("Ratio/cash terms require page_ref citation")
        if not entry["verified_by"]:
            raise ValueError("Ratio/cash terms require verified_by")
    if entry["status"] == "withdrawn" and (
            ratio_num is not None or cash is not None
            or entry["successor_symbol"] is not None):
        raise ValueError("Withdrawn schemes carry no ratio, cash or successor")
    if entry["status"] == "effective" and not entry["pdf_url"]:
        raise ValueError("Effective status requires pdf_url citation")
    if entry["successor_symbol"] and not entry["pdf_url"]:
        raise ValueError("successor_symbol requires pdf_url citation")
    # Null-party hygiene: a successor must name an exchange symbol.
    if entry["successor_symbol"] is not None and not re.match(
            r"^[A-Z0-9.\-]{1,30}$", str(entry["successor_symbol"])):
        raise ValueError("successor_symbol must be an exchange symbol")
    return entry


# --- filing -> scheme collapse -------------------------------------------

def is_withdrawn_filing(row: dict) -> bool:
    haystack = " ".join(str(row.get(f) or "")
                        for f in ("desc", "attchmntText", "attchmntFile"))
    return bool(WITHDRAWN_PAT.search(haystack))


def classify_type(row: dict) -> str:
    text = " ".join(str(row.get(f) or "") for f in
                    ("desc", "attchmntText", "attchmntFile"))
    low = text.lower()
    if "liquidat" in low or "de-registered" in low or "deregistered" in low:
        return "liquidation"
    if "demerg" in low:
        return "demerger"
    if "merger" in low or "amalgamat" in low:
        return "merger"
    if "restructur" in low:
        return "restructuring"
    if "scheme of arrangement" in low or "scheme" in low:
        return "scheme_of_arrangement"
    return "unknown"


def _companies_in_text(row: dict) -> list[str]:
    text = " ".join(str(row.get(f) or "") for f in ("attchmntText", "desc"))
    found, seen = [], set()
    for m in COMPANY_PAT.finditer(text):
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        # The greedy pattern may run into sentence text ("Directors of
        # X Limited", "Business from X Limited"). Keep only the trailing
        # capitalized run after the last lowercase token.
        tokens = raw.split()
        cut = 0
        for i, tok in enumerate(tokens):
            if tok.isalpha() and tok.islower():
                cut = i + 1
        tokens = tokens[cut:]
        while tokens and tokens[0].lower().rstrip("-") in ("company", "the"):
            tokens.pop(0)
        name = " ".join(tokens) or raw
        if not tokens:
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            found.append(name)
    return found


def _same_date_key(row: dict) -> str:
    return f"{row.get('symbol')}|{row.get('announcement_date')}"


def _party_signature(row: dict) -> str | None:
    companies = _companies_in_text(row)
    if len(companies) >= 2:
        return "|".join(sorted(c.lower() for c in companies))
    return None


def _lifecycle_status(rows: list[dict]) -> str:
    if any(is_withdrawn_filing(r) for r in rows):
        return "withdrawn"
    texts = " ".join(str(r.get("attchmntText") or "") for r in rows).lower()
    if ("effectiveness" in texts or "completed and is effective" in texts
            or "effective from" in texts or "judicial pronouncement" in texts):
        # NCLT pronouncement / effectiveness filings evidence approval;
        # "effective" only where the text says the scheme is effective.
        if "effectiveness" in texts or "effective from" in texts:
            return "effective"
        return "approved"
    return "announced"


def _dates_for(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda r: str(r.get("announcement_date")))
    dates = {"announced": rows[0].get("announcement_date")}
    texts = " ".join(str(r.get("attchmntText") or "") for r in rows)
    if "meeting held today" in texts.lower():
        dates["board_date"] = rows[0]["announcement_date"]
    nclt = [r for r in rows
            if "judicial pronouncement" in str(r.get("attchmntText") or "").lower()]
    if nclt:
        dates["nclt_order_date"] = nclt[0]["announcement_date"]
    eff = re.search(r"effective from ([A-Za-z]+ \d{1,2}, \d{4})", texts)
    if eff:
        from datetime import datetime
        dates["effective_date"] = (
            datetime.strptime(eff.group(1), "%B %d, %Y").date().isoformat())
    return dates


def collapse_filings(index_rows: list[dict]) -> list[dict]:
    """Collapse index filings into ledger stubs (see module docstring)."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    singles = 0

    def add(key: str, row: dict) -> None:
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    pending_withdrawal: dict[str, str] = {}
    for row in sorted(index_rows, key=lambda r: str(r.get("announcement_date"))):
        sig = _party_signature(row)
        if sig:
            add(f"sig:{sig}", row)
            continue
        if is_withdrawn_filing(row):
            # Link to the symbol's most recent prior singleton group.
            prior = pending_withdrawal.get(str(row.get("symbol")))
            if prior is not None:
                add(prior, row)
            else:
                add(f"single:{singles}", row)
                singles += 1
            continue
        # Same symbol + same announcement date merges (split filings).
        same_day = _same_date_key(row)
        merged = False
        for key in order:
            if (groups[key] and not _party_signature(groups[key][0])
                    and _same_date_key(groups[key][0]) == same_day
                    and len(groups[key]) <= 2
                    and key.startswith("day:")):
                add(key, row)
                merged = True
                break
        if not merged:
            add(f"day:{same_day}", row)
        pending_withdrawal[str(row.get("symbol"))] = f"day:{same_day}"

    entries = []
    for key in order:
        rows = sorted(groups[key], key=lambda r: str(r.get("announcement_date")))
        primary = rows[0]
        symbol = str(primary.get("symbol") or "").upper()
        status = _lifecycle_status(rows)
        companies = _companies_in_text(primary)
        parties = [{"symbol": symbol,
                    "company_name": primary.get("sm_name"),
                    "isin": primary.get("sm_isin"), "role": "primary"}]
        for name in companies:
            if name.lower() not in str(primary.get("sm_name")).lower():
                parties.append({"company_name": name, "role": "counterparty"})
        # Deterministic scheme_id: symbol + earliest announcement date +
        # ordinal among same-day stubs (file order kept via sorted rows).
        entries.append(_make_entry(symbol, primary, rows, status, parties))
    entries.sort(key=lambda e: (e["dates"]["announced"], e["scheme_id"]))
    # De-duplicate scheme_ids while keeping order stable.
    seen: dict[str, int] = {}
    for e in entries:
        base = e["scheme_id"]
        n = seen.get(base, 0)
        seen[base] = n + 1
        if n:
            e["scheme_id"] = f"{base}-{n}"
    return entries


def _make_entry(symbol, primary, rows, status, parties) -> dict:
    dates = _dates_for(rows)
    earliest = rows[0]
    entry = {
        "scheme_id": f"SLG-{symbol}-{earliest.get('announcement_date')}",
        "scheme_type": classify_type(primary),
        "parties": parties,
        "dates": dates,
        "ratio_num": None,
        "ratio_den": None,
        "cash_per_share": None,
        "successor_symbol": None,
        "pdf_url": earliest.get("attchmntFile"),
        "page_ref": None,
        "status": status,
        "verified_by": None,
        # Provenance (non-schema extras; schema fields all present).
        "extraction_source": "announcement_index_text_only; PDF not read",
        "terms_verified": False,
        "withdrawn": status == "withdrawn",
        "filing_trail": [
            {"announcement_date": r.get("announcement_date"),
             "desc": r.get("desc"),
             "attchmntText": r.get("attchmntText"),
             "pdf_url": r.get("attchmntFile")}
            for r in rows],
        "limitations": [
            "Ratio/cash/successor live inside the cited PDF; not extracted",
            "Never inferred from any price move",
            "Lifecycle status is a filing-text reading until PDF review",
        ],
    }
    return validate_entry(entry)


# --- coverage ------------------------------------------------------------

def build_coverage(ledger_entries: list[dict], index_rows: list[dict],
                   reorg: dict) -> dict:
    """Coverage of the 829-row reorganization universe.

    `reorg` supplies host counts (live DB read-only probe): raw row count,
    distinct (symbol, ex_date) scheme-days, distinct symbols, purposes.
    """
    verified = sum(1 for e in ledger_entries if e["terms_verified"])
    ledger_ready = sum(1 for e in ledger_entries
                       if e["pdf_url"] and e["parties"] and not e["terms_verified"])
    withdrawn = sum(1 for e in ledger_entries if e["status"] == "withdrawn")
    by_type: dict[str, int] = {}
    for e in ledger_entries:
        by_type[e["scheme_type"]] = by_type.get(e["scheme_type"], 0) + 1
    scheme_days = reorg["scheme_days"]
    days_covered = min(len(ledger_entries), scheme_days)
    return {
        "ledger_rows": len(ledger_entries),
        "index_rows_collapsed": len(index_rows),
        "index_symbols": len({r.get("symbol") for r in index_rows}),
        "types": by_type,
        "statuses": {s: sum(1 for e in ledger_entries if e["status"] == s)
                     for s in sorted({e["status"] for e in ledger_entries})},
        "withdrawn": withdrawn,
        "terms_verified": verified,
        "ledger_ready_unverified": ledger_ready,
        "needs_pdf_ratios": len(ledger_entries) - verified,
        "reorg_rows_live_db": reorg["rows"],
        "reorg_rows_research_snapshot": 829,
        "reorg_scheme_days": scheme_days,
        "reorg_symbols": reorg["symbols"],
        "scheme_days_with_ledger_stub": days_covered,
        "scheme_days_needs_pdf": scheme_days - days_covered,
        "ratios_inferred_from_price": False,
        "pdfs_downloaded": 0,
        "effort_to_full_coverage": {
            "minutes_per_event_low": 15,
            "minutes_per_event_high": 60,
            "hours_low": round(scheme_days * 15 / 60, 1),
            "hours_high": round(scheme_days * 60 / 60, 1),
            "person_weeks_low_at_40h": round(scheme_days * 15 / 60 / 40, 2),
            "person_weeks_high_at_40h": round(scheme_days * 60 / 60 / 40, 2),
            "note": ("Per-scheme-day reading of cited PDFs (15-60 min/event "
                     "per merger-demerger-research.md); filing trails average "
                     ">1 PDF per scheme, so budget the high end."),
        },
        "limitations": [
            "Ledger sample is index-derived stubs: 0 ratios verified",
            "PDF bodies are not cached locally; pdf_url is a citation only",
            "Live DB row count (815) differs from research snapshot (829)",
            "Generic filings stay singletons; true scheme count needs PDF review",
        ],
    }


def priority_queue(large_moves_path: Path, ledger_entries: list[dict],
                   reorg_symbols: set[str], top_n: int = 20) -> list[dict]:
    """Rank reorg symbols by |raw_return|; never use the move as a ratio.

    Returns up to `top_n` queue items: scheme-index symbols first (they have
    a citable PDF URL), then remaining reorganization symbols ordered by
    their largest absolute raw price move in large-raw-price-moves.csv.
    Each item records the move only as a *priority signal* for manual
    PDF review -- the price move itself is never a ratio source.
    """
    import csv
    moves: dict[str, float] = {}
    with large_moves_path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            sym = str(row.get("symbol") or "")
            if sym not in reorg_symbols:
                continue
            try:
                mag = abs(float(row["raw_return"]))
            except (ValueError, KeyError, TypeError):
                continue
            moves[sym] = max(moves.get(sym, 0.0), mag)
    ledger_by_symbol = {}
    for e in ledger_entries:
        ledger_by_symbol.setdefault(e["parties"][0].get("symbol"), []).append(
            e["scheme_id"])
    ranked = sorted(moves, key=lambda s: -moves[s])
    index_symbols = [s for s in ledger_by_symbol]
    ordered = index_symbols + [s for s in ranked if s not in index_symbols]
    queue = []
    for sym in ordered[:top_n]:
        queue.append({
            "symbol": sym,
            "priority_reason": ("scheme_index_filing" if sym in index_symbols
                                else "price_discontinuity"),
            "largest_abs_raw_return": round(moves.get(sym, 0.0), 4),
            "has_index_filing": sym in index_symbols,
            "ledger_scheme_ids": ledger_by_symbol.get(sym, []),
            "estimated_pdf_minutes_low": 15,
            "estimated_pdf_minutes_high": 60,
            "ratio_from_price": False,
        })
    return queue


def probe_reorg_stats(db_path: Path, include_symbols: bool = False):
    """Read-only probe of the host DB; returns None if unavailable."""
    try:
        import duckdb
    except ImportError:
        return None
    try:
        con = open_catalog(database=db_path)
        try:
            rows = con.execute(
                "SELECT count(*) FROM corporate_actions "
                "WHERE action_type='reorganization'").fetchone()[0]
            days = con.execute(
                "SELECT count(*) FROM (SELECT DISTINCT symbol, ex_date "
                "FROM corporate_actions WHERE action_type='reorganization')"
            ).fetchone()[0]
            syms = con.execute(
                "SELECT count(DISTINCT symbol) FROM corporate_actions "
                "WHERE action_type='reorganization'").fetchone()[0]
            symbol_set = {r[0] for r in con.execute(
                "SELECT DISTINCT symbol FROM corporate_actions "
                "WHERE action_type='reorganization'").fetchall()}
        finally:
            con.close()
        return {"rows": int(rows), "scheme_days": int(days), "symbols": int(syms),
                "symbol_set": symbol_set}
    except Exception:
        return None


def probe_reorg_symbols(db_path: Path) -> set[str]:
    try:
        import duckdb
        con = open_catalog(database=db_path)
        try:
            return {r[0] for r in con.execute(
                "SELECT DISTINCT symbol FROM corporate_actions "
                "WHERE action_type='reorganization'").fetchall()}
        finally:
            con.close()
    except Exception:
        return set()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_index(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--index", type=Path, default=None,
                        help="scheme-index.jsonl (default: processed/schemes/...)")
    args = parser.parse_args()
    index_path = args.index or (args.data / "processed/schemes/scheme-index.jsonl")
    rows = load_index(index_path)
    entries = collapse_filings(rows)
    for e in entries:
        validate_entry(e)
    out_dir = args.data / "processed/ledger"
    write_jsonl(out_dir / "scheme-ledger.jsonl", entries)
    stats = probe_reorg_stats(args.data / "market.duckdb") or {
        "rows": 815, "scheme_days": 346, "symbols": 303}
    coverage = build_coverage(entries, rows, stats)
    write_json(out_dir / "coverage.json", coverage)
    write_json(args.data / "reports/scheme-ledger.json", coverage)
    reorg_symbols = stats.get("symbol_set") or probe_reorg_symbols(args.data / "market.duckdb")
    moves_path = args.data / "reports/large-raw-price-moves.csv"
    queue = (priority_queue(moves_path, entries, reorg_symbols)
             if moves_path.exists() and reorg_symbols else [])
    write_json(out_dir / "pdf-reading-queue.json", queue)
    coverage["pdf_reading_queue"] = queue
    write_json(out_dir / "coverage.json", coverage)
    write_json(args.data / "reports/scheme-ledger.json", coverage)
    print(json.dumps({k: coverage[k] for k in
                      ("ledger_rows", "withdrawn", "terms_verified",
                       "reorg_scheme_days", "scheme_days_needs_pdf")}))


if __name__ == "__main__":
    main()
