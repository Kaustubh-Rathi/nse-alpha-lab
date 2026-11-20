"""Acquire NSE financial-results filing index; never fabricate fundamentals.

Only stdlib dependencies. This module collects the *filing index* — the
metadata NSE exposes for each financial-results filing (symbol, period,
publication timestamps, XBRL / HTML links) — and records exact coverage.
It does NOT extract revenue / profit / EPS / debt values from XBRL or HTML,
because those values require taxonomy-version mapping (1+ taxonomies per
sector per era, standalone vs consolidated, audited variants) that is not
low-overhead and would risk fabricated or mis-mapped numbers.

Point-in-time note: `toDate` is the period end. `filingDate` /
`broadCastDate` / `exchdisstime` are the exchange publication timestamps
as returned by NSE and are the only safe "known from" dates for a
backtest. Restatements appear as new rows with the same (symbol,
fromDate, toDate) but later publication timestamps; this importer keeps
every row so the later filing never silently overwrites the earlier one
in the raw layer.

Licensing note: NSE Terms of Use prohibit systematic/automated collection
and redistribution without express written consent. This collector performs
at most one request per period-year (same pattern as older_actions.py),
stops on 401/403/429, and is for personal research use only.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import urllib.request
from datetime import datetime
from pathlib import Path
from nse_pipeline.paths import data_root

API = "https://www.nseindia.com/api/corporates-financial-results"
PERIODS = ("Quarterly", "Annual")
START_YEAR = 2007
END_YEAR = 2026


def api_url(period: str, year: int) -> str:
    if period not in PERIODS:
        raise ValueError(f"Unknown period: {period!r}")
    if not START_YEAR <= year <= END_YEAR + 1:
        raise ValueError(f"Year out of supported range: {year}")
    return f"{API}?index=equities&period={period}&from_date=01-01-{year}&to_date=31-12-{year}"


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


def validate_api(data: object, period: str) -> list[dict]:
    """Reject error payloads, HTML, wrong periods and malformed rows."""
    if not isinstance(data, list):
        raise ValueError("Expected an NSE list response")
    for row in data:
        if not isinstance(row, dict):
            raise ValueError("Non-object NSE financial-results row")
        if not {"symbol", "toDate", "filingDate"}.issubset(row.keys()):
            raise ValueError("Missing required NSE financial-results fields")
        if row.get("period") is not None and row["period"] != period:
            raise ValueError(f"Out-of-window NSE row: period={row.get('period')!r}")
    return data


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def acquire(raw: Path, periods: tuple[str, ...] = PERIODS,
            start_year: int = START_YEAR, end_year: int = END_YEAR) -> None:
    """At most one request per uncached period-year. Stop on any error."""
    raw.mkdir(parents=True, exist_ok=True)
    manifest_path = raw / "acquisition.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    jobs = [(f"nse-financial-results-{period.lower()}-{year}.json",
             api_url(period, year), period)
            for period in periods for year in range(start_year, end_year + 1)]
    for name, url, period in jobs:
        path = raw / name
        cached = path.exists()
        record = {"url": url, "cached_at_run": cached}
        try:
            if cached:
                content = path.read_bytes()
                expected = manifest.get(name, {}).get("sha256")
                if expected and hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError(f"Cached source hash changed: {name}")
            else:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "NSEHistoricalResearch/1.0 (personal historical-data research)",
                    "Accept": "application/json",
                })
                with urllib.request.urlopen(req, timeout=60) as response:
                    content = response.read()
                    record.update(http_status=response.status,
                                  retrieved_at=datetime.now().astimezone().isoformat())
            validate_api(json.loads(content), period)
            if not cached:
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

    No financial values are derived. Fields NSE does not supply stay absent.
    """
    out = {**row}
    out["source_file"] = source_file
    out["source_row"] = source_row
    out["source_url"] = source_url
    out["point_in_time_date"] = str(parse_date(row.get("filingDate")) or "") or None
    out["period_end"] = str(parse_date(row.get("toDate")) or "") or None
    xbrl = (row.get("xbrl") or "").strip()
    out["xbrl_is_placeholder"] = xbrl in ("", "-") or xbrl.endswith("/xbrl/-")
    out["values_extracted"] = False
    return out


def build(raw: Path, processed: Path, report: Path) -> dict:
    rows: list[dict] = []
    yearly: dict[str, dict] = {}
    missing: list[str] = []
    for period in PERIODS:
        for year in range(START_YEAR, END_YEAR + 1):
            name = f"nse-financial-results-{period.lower()}-{year}.json"
            path = raw / name
            if not path.exists():
                missing.append(name)
                continue
            source_rows = validate_api(json.loads(path.read_bytes()), period)
            normalized = [normalize_row(r, name, i, api_url(period, year))
                          for i, r in enumerate(source_rows)]
            rows.extend(normalized)
            dates = [r["period_end"] for r in normalized if r["period_end"]]
            yearly[name] = {
                "raw_rows": len(source_rows),
                "symbols": len({r.get("symbol") for r in source_rows if r.get("symbol")}),
                "earliest_period_end": min(dates) if dates else None,
                "latest_period_end": max(dates) if dates else None,
                "with_xbrl_file": sum(not r["xbrl_is_placeholder"] for r in normalized),
                "with_html_detail": sum(bool(r.get("resultDetailedDataLink")) for r in normalized),
                "consolidated": dict(collections.Counter(str(r.get("consolidated")) for r in normalized)),
                "audited": dict(collections.Counter(str(r.get("audited")) for r in normalized)),
            }
    rows.sort(key=lambda r: (r.get("period") or "", str(r.get("period_end")), r.get("symbol") or "",
                             str(r.get("filingDate"))))
    write_jsonl(processed / "filing-index.jsonl", rows)
    dates = [r["period_end"] for r in rows if r["period_end"]]
    pubs = [r["point_in_time_date"] for r in rows if r["point_in_time_date"]]
    coverage = {
        "requested_start_year": START_YEAR,
        "requested_end_year": END_YEAR,
        "periods": list(PERIODS),
        "years": yearly,
        "missing_files": missing,
        "filing_rows": len(rows),
        "symbols": len({r.get("symbol") for r in rows if r.get("symbol")}),
        "earliest_period_end": min(dates) if dates else None,
        "latest_period_end": max(dates) if dates else None,
        "earliest_publication": min(pubs) if pubs else None,
        "latest_publication": max(pubs) if pubs else None,
        "complete": not missing and len(rows) > 0,
        "values_extracted": False,
        "prices_adjusted": False,
        "limitations": [
            "Index only: no revenue/profit/EPS/equity/debt/shares values are stored",
            "XBRL value extraction requires per-era taxonomy mapping; not done here",
            "NSE API returns current filing identities; join to 2000-2026 bars on ISIN/symbol is lossy",
            "Publication timestamps are exchange receipt times, not audited finality dates",
            "Restatements appear as extra rows; consumers must filter point_in_time_date <= as_of",
            "Pre-2007 fundamentals are absent from this source; no official API bridge was found",
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
    parser.add_argument("--periods", default=",".join(PERIODS))
    args = parser.parse_args()
    periods = tuple(p.strip() for p in args.periods.split(",") if p.strip())
    raw = args.data / "raw/fundamentals"
    if args.fetch:
        acquire(raw, periods=periods, start_year=args.start_year, end_year=args.end_year)
    result = build(raw, args.data / "processed/fundamentals",
                   args.data / "processed/fundamentals/coverage.json")
    write_json(args.data / "reports/fundamentals-coverage.json", result)
    print(json.dumps({k: result[k] for k in ("filing_rows", "symbols",
                                             "earliest_period_end", "latest_period_end",
                                             "missing_files")}))


if __name__ == "__main__":
    main()
