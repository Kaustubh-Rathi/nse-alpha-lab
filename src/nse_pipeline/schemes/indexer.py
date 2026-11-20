"""Acquire NSE scheme-announcement filing index; never fabricate scheme terms.

Only stdlib dependencies. This module collects the *filing index* — the
metadata NSE exposes for each corporate announcement whose description
indicates a scheme of arrangement, amalgamation/merger, demerger or
restructuring (`desc` text) — and records exact coverage. It stores only
descriptions, announcement dates, PDF attachment URLs, symbol and ISIN.
It does NOT extract exchange ratios, cash consideration, successor mappings
or any price adjustment, because those terms live inside the scheme PDF
documents (SEBI LODR Reg-30/Reg-37 filings), not in any structured field,
and must never be inferred from a price jump.

Point-in-time note: `an_dt` is the exchange announcement timestamp as
returned by NSE and is the only safe "known from" date for a backtest.
Multiple filings per scheme (board outcome, NCLT order, effectiveness
intimation) each appear as their own row; this importer keeps every row
so no filing silently overwrites another in the raw layer.

Licensing note: NSE Terms of Use prohibit systematic/automated collection
and redistribution without express written consent. This collector performs
at most one request per year (or per symbol-year when `--symbols` is given),
stops on 401/403/429, and is for personal research use only.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from nse_pipeline.paths import data_root

API = "https://www.nseindia.com/api/corporate-announcements"
START_YEAR = 2011
END_YEAR = 2026

SCHEME_PATTERN = re.compile(
    r"\b(?:scheme of arrangement|scheme|amalgamation|merger|demerger|"
    r"restructuring|restructure)\b", re.I)
WITHDRAWN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:withdraw\w*|cancel\w*|abandon\w*|scrap\w*|"
    r"not proceeding|did not materialise|did not materialize)\b", re.I)

REQUIRED_FIELDS = {"symbol", "an_dt", "desc"}

YEAR_FILE = re.compile(r"^nse-corporate-announcements-(\d{4})\.json$")
SYMBOL_FILE = re.compile(r"^nse-corporate-announcements-([A-Za-z0-9.\-]+)-(\d{4})\.json$")

SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9.\-]{1,30}$")


def api_url(year: int) -> str:
    if not START_YEAR <= year <= END_YEAR:
        raise ValueError(f"Year out of supported range: {year}")
    return f"{API}?index=equities&from_date=01-01-{year}&to_date=31-12-{year}"


def api_url_symbol(symbol: str, year: int) -> str:
    name = (symbol or "").strip().upper()
    if not SYMBOL_PATTERN.match(name):
        raise ValueError(f"Invalid symbol: {symbol!r}")
    if not START_YEAR <= year <= END_YEAR:
        raise ValueError(f"Year out of supported range: {year}")
    return (f"{API}?index=equities&symbol={urllib.parse.quote(name)}"
            f"&from_date=01-01-{year}&to_date=31-12-{year}")


def parse_date(value: str | None):
    if value is None or str(value).strip() in ("", "-", "null", "None"):
        return None
    text = str(value).strip().split(" ")[0]
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized source date: {value!r}")


def is_scheme_row(row: dict) -> bool:
    return bool(SCHEME_PATTERN.search(str(row.get("desc") or "")))


def is_withdrawn_row(row: dict) -> bool:
    haystack = " ".join(str(row.get(field) or "")
                        for field in ("desc", "attchmntText", "attchmntFile"))
    return bool(WITHDRAWN_PATTERN.search(haystack))


def dedup_key(row: dict) -> tuple[str, str, str, str]:
    """Exact-filing identity: symbol, announcement timestamp, PDF, description."""
    return (str(row.get("symbol") or "").strip(),
            str(row.get("an_dt") or "").strip(),
            str(row.get("attchmntFile") or "").strip(),
            str(row.get("desc") or "").strip())


def validate_api(data: object, year: int) -> list[dict]:
    """Reject error payloads, HTML and malformed rows.

    Rows whose announcement date falls outside the requested year are kept
    (they are real filings) but counted by `build`; only structural problems
    raise here.
    """
    if not isinstance(data, list):
        raise ValueError("Expected an NSE list response")
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("Non-object NSE announcement row")
        if not REQUIRED_FIELDS.issubset(row.keys()):
            raise ValueError("Missing required NSE announcement fields")
        parse_date(row.get("an_dt"))
    return data


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def _fetch(url: str, path: Path, manifest: dict, name: str) -> tuple[bytes, dict]:
    """Return content from cache or one bounded request; never retry."""
    cached = path.exists()
    record = {"url": url, "cached_at_run": cached}
    if cached:
        content = path.read_bytes()
        expected = manifest.get(name, {}).get("sha256")
        if expected and hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"Cached source hash changed: {name}")
        return content, record
    req = urllib.request.Request(url, headers={
        "User-Agent": "NSEHistoricalResearch/1.0 (personal historical-data research)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=300) as response:
        content = response.read()
        record.update(http_status=response.status,
                      retrieved_at=datetime.now().astimezone().isoformat())
    return content, record


def acquire(raw: Path, start_year: int = START_YEAR, end_year: int = END_YEAR,
            symbols: tuple[str, ...] = ()) -> None:
    """At most one request per year (or per symbol-year). Stop on any error."""
    raw.mkdir(parents=True, exist_ok=True)
    manifest_path = raw / "acquisition.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    jobs: list[tuple[str, str, int]] = []
    if symbols:
        for symbol in symbols:
            for year in range(start_year, end_year + 1):
                jobs.append((f"nse-corporate-announcements-{symbol.upper()}-{year}.json",
                             api_url_symbol(symbol, year), year))
    else:
        for year in range(start_year, end_year + 1):
            jobs.append((f"nse-corporate-announcements-{year}.json",
                         api_url(year), year))
    for name, url, year in jobs:
        path = raw / name
        record: dict = {"url": url, "cached_at_run": path.exists()}
        try:
            content, record = _fetch(url, path, manifest, name)
            validate_api(json.loads(content), year)
            if not record["cached_at_run"]:
                path.write_bytes(content)
            record.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
            manifest[name] = {**manifest.get(name, {}), **record}
            manifest[name].pop("error", None)
            write_json(manifest_path, manifest)
            print(f"Ready {name}: {len(content)} bytes", flush=True)
        except Exception as error:
            manifest[name] = {**manifest.get(name, {}), **record, "error": repr(error)}
            write_json(manifest_path, manifest)
            raise  # Respect 401/403/429; never retry with a different identity.


def normalize_row(row: dict, source_file: str, source_row: int, source_url: str) -> dict:
    """Preserve every source field verbatim; only add provenance helpers.

    No ratio, cash, successor or price fields are derived. Withdrawn/cancelled
    schemes are flagged, never silently treated as effective.
    """
    out = {**row}
    out["source_file"] = source_file
    out["source_row"] = source_row
    out["source_url"] = source_url
    out["announcement_date"] = str(parse_date(row.get("an_dt")) or "") or None
    out["scheme_like"] = is_scheme_row(row)
    out["withdrawn"] = is_withdrawn_row(row)
    out["scheme_terms_extracted"] = False
    out["prices_adjusted"] = False
    return out


def _parse_file_name(name: str) -> tuple[int, str | None] | None:
    match = YEAR_FILE.match(name)
    if match:
        return int(match.group(1)), None
    match = SYMBOL_FILE.match(name)
    if match:
        return int(match.group(2)), match.group(1)
    return None


def build(raw: Path, processed: Path, report: Path) -> dict:
    rows: list[dict] = []
    yearly: dict[str, dict] = {}
    symbol_files: dict[str, dict] = {}
    missing: list[str] = []
    seen: set[tuple[str, str, str, str]] = set()
    duplicates_removed = 0
    out_of_window = 0

    for year in range(START_YEAR, END_YEAR + 1):
        path = raw / f"nse-corporate-announcements-{year}.json"
        if not path.exists():
            missing.append(path.name)
            continue
        source_rows = validate_api(json.loads(path.read_bytes()), year)
        scheme_rows = [r for r in source_rows if is_scheme_row(r)]
        out_of_window += sum(
            (parse_date(r.get("an_dt")) is not None
             and parse_date(r.get("an_dt")).year != year)
            for r in source_rows)
        fresh = []
        year_duplicates = 0
        for row in scheme_rows:
            key = dedup_key(row)
            if key in seen:
                duplicates_removed += 1
                year_duplicates += 1
                continue
            seen.add(key)
            fresh.append(row)
        normalized = [normalize_row(r, path.name, i, api_url(year))
                      for i, r in enumerate(fresh)]
        rows.extend(normalized)
        dates = [r["announcement_date"] for r in normalized if r["announcement_date"]]
        yearly[str(year)] = {
            "raw_rows": len(source_rows),
            "scheme_rows": len(normalized),
            "duplicates_removed": year_duplicates,
            "symbols": len({r.get("symbol") for r in scheme_rows if r.get("symbol")}),
            "earliest_announcement": min(dates) if dates else None,
            "latest_announcement": max(dates) if dates else None,
            "withdrawn_rows": sum(r["withdrawn"] for r in normalized),
        }

    for path in sorted(raw.glob("nse-corporate-announcements-*.json")):
        parsed = _parse_file_name(path.name)
        if parsed is None or parsed[1] is None:
            continue
        year, symbol = parsed
        source_rows = validate_api(json.loads(path.read_bytes()), year)
        scheme_rows = [r for r in source_rows if is_scheme_row(r)]
        out_of_window += sum(
            (parse_date(r.get("an_dt")) is not None
             and parse_date(r.get("an_dt")).year != year)
            for r in source_rows)
        fresh = []
        for row in scheme_rows:
            key = dedup_key(row)
            if key in seen:
                duplicates_removed += 1
                continue
            seen.add(key)
            fresh.append(row)
        normalized = [normalize_row(r, path.name, i, api_url_symbol(symbol, year))
                      for i, r in enumerate(fresh)]
        rows.extend(normalized)
        dates = [r["announcement_date"] for r in normalized if r["announcement_date"]]
        symbol_files[path.name] = {
            "symbol": symbol, "year": year,
            "raw_rows": len(source_rows), "scheme_rows": len(normalized),
            "earliest_announcement": min(dates) if dates else None,
            "latest_announcement": max(dates) if dates else None,
            "withdrawn_rows": sum(r["withdrawn"] for r in normalized),
        }

    rows.sort(key=lambda r: (str(r.get("announcement_date")),
                             r.get("symbol") or "",
                             str(r.get("an_dt")),
                             str(r.get("attchmntFile") or "")))
    write_jsonl(processed / "scheme-index.jsonl", rows)
    dates = [r["announcement_date"] for r in rows if r["announcement_date"]]
    coverage = {
        "requested_start_year": START_YEAR,
        "requested_end_year": END_YEAR,
        "years": yearly,
        "symbol_files": symbol_files,
        "missing_files": missing,
        "scheme_rows": len(rows),
        "duplicate_rows_removed": duplicates_removed,
        "rows_outside_requested_year": out_of_window,
        "withdrawn_rows": sum(r["withdrawn"] for r in rows),
        "symbols": len({r.get("symbol") for r in rows if r.get("symbol")}),
        "earliest_announcement": min(dates) if dates else None,
        "latest_announcement": max(dates) if dates else None,
        "complete": not missing and len(rows) > 0,
        "scheme_terms_extracted": False,
        "ratios_inferred": False,
        "prices_adjusted": False,
        "limitations": [
            "Index only: no exchange ratios, cash consideration or successor mappings are stored",
            "Scheme terms live inside filing PDFs; ratio extraction requires manual document review",
            "Withdrawal flag is a description-text heuristic; never treat withdrawn schemes as effective",
            "Filings with generic descriptions (e.g. Outcome of Board Meeting) may be scheme-related but are not captured",
            "NSE API historical completeness is not documented or proven",
            "API uses current identities; join to historical bars on ISIN/symbol is lossy",
            "Announcement timestamps are exchange receipt times, not scheme effectiveness dates",
            "NSE Terms of Use restrict automated collection and redistribution; personal research use only",
        ],
    }
    write_json(report, coverage)
    return coverage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=END_YEAR)
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbols for per-symbol-year fetches")
    args = parser.parse_args()
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    raw = args.data / "raw/schemes"
    if args.fetch:
        acquire(raw, start_year=args.start_year, end_year=args.end_year, symbols=symbols)
    result = build(raw, args.data / "processed/schemes",
                   args.data / "processed/schemes/coverage.json")
    write_json(args.data / "reports/scheme-coverage.json", result)
    print(json.dumps({k: result[k] for k in ("scheme_rows", "symbols",
                                             "earliest_announcement",
                                             "latest_announcement",
                                             "withdrawn_rows", "missing_files")}))


if __name__ == "__main__":
    main()
