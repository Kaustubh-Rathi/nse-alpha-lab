"""Read-only session/universe audit; absence is never evidence of a holiday.

Run: uv run python src/audit_coverage.py --cutoff 2026-11-07
Optional --fetch-sources downloads small, public NSE calendars/security lists only.
No prices, catalog, existing pipeline manifests, or corporate actions are changed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from nse_pipeline.paths import data_root
from nse_pipeline.build.portable import open_catalog

import duckdb

NSE = "https://www.nseindia.com"
ARCHIVES = "https://nsearchives.nseindia.com"
HOLIDAYS = NSE + "/resources/exchange-communication-holidays"
DELISTED = ARCHIVES + "/content/equities/delisted.csv"
MASTER = ARCHIVES + "/content/equities/EQUITY_L.csv"
EQUITY_SERIES = ("EQ", "BE", "BZ", "SM", "ST")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def fetch_sources(data: Path, start: date, cutoff: date) -> None:
    """Bounded public downloads, cached; no retries or access-control workarounds."""
    jobs = [("calendar/exchange-holidays.html", HOLIDAYS),
            ("security_master/delisted.csv", DELISTED),
            ("security_master/EQUITY_L.csv", MASTER)]
    jobs.extend((f"calendar/trading-holidays-{year}.json",
                 f"{NSE}/api/holiday-master?type=trading&year={year}")
                for year in range(start.year, cutoff.year + 1))
    for name, url in jobs:
        target = data / "raw" / name
        if target.exists():
            continue
        metadata = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat()}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "NSEHistoricalResearch/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read(2_000_001)
                if len(content) > 2_000_000:
                    raise ValueError("Source exceeded the small-download limit")
                metadata.update(http_status=response.status, bytes=len(content),
                                sha256=hashlib.sha256(content).hexdigest())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        except (OSError, ValueError) as error:
            metadata.update(error=type(error).__name__, detail=str(error))
            if isinstance(error, urllib.error.HTTPError):
                metadata["http_status"] = error.code
                error.close()
        write_json(target.with_suffix(target.suffix + ".metadata.json"), metadata)
        if metadata.get("http_status") in (401, 403, 429):
            break  # Respect denial/rate limits rather than trying another route.


def source_reference(path: Path) -> dict:
    metadata_path = path.with_suffix(path.suffix + ".metadata.json")
    result = {"file": str(path)}
    if metadata_path.exists():
        result.update(json.loads(metadata_path.read_text(encoding="utf-8")))
        if path.exists() and result.get("sha256"):
            if hashlib.sha256(path.read_bytes()).hexdigest() != result["sha256"]:
                raise ValueError(f"Cached evidence checksum mismatch: {path}")
    return result


def exchange_date(value: str) -> date:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unrecognized exchange date: {value!r}")


def csv_text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252")


def index_session(content: bytes, expected: date) -> bool:
    """Require matching report dates and a broad equity index with positive volume.

    Report filenames or a nontrading index valuation alone are not session proof.
    """
    rows = list(csv.DictReader(io.StringIO(csv_text(content))))
    if not rows:
        return False
    if {exchange_date(row["Index Date"]) for row in rows} != {expected}:
        raise ValueError(f"Index report dates disagree with filename {expected}")
    for row in rows:
        name = row["Index Name"].strip().upper()
        if name in ("NIFTY 50", "CNX NIFTY", "S&P CNX NIFTY"):
            try:
                return float(row["Volume"].replace(",", "")) > 0 and float(row["Closing Index Value"]) > 0
            except ValueError:
                return False
    return False


def load_index_evidence(data: Path) -> tuple[dict[date, list[dict]], dict]:
    sessions: dict[date, list[dict]] = defaultdict(list)
    inventory = {"reports_checked": 0, "reports_without_session_proof": [], "rejected_reports": [], "sources": []}

    def add(name: str, content: bytes, reference: dict) -> None:
        match = re.fullmatch(r"ind_close_all_(\d{8})\.csv", Path(name).name, re.I)
        if not match:
            return
        day = datetime.strptime(match[1], "%d%m%Y").date()
        inventory["reports_checked"] += 1
        try:
            valid_session = index_session(content, day)
        except (ValueError, KeyError, UnicodeDecodeError) as error:
            inventory["rejected_reports"].append({"source": name, "reason": str(error)})
            return
        if valid_session:
            sessions[day].append({"kind": "independent_index_session", "source": name,
                                  "official_url": f"{ARCHIVES}/content/indices/{Path(name).name}",
                                  **reference})
        else:
            inventory["reports_without_session_proof"].append(name)

    archive_path = data / "raw" / "nseIndices.zip"
    if archive_path.exists():
        # A complete ZIP only; never wait for, repair, or redownload a host download.
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                if member.filename.lower().endswith(".csv"):
                    add(member.filename, archive.read(member),
                        {"delivery": "exchange report in third-party mirror", "archive": str(archive_path)})
        manifest = data / "manifests" / "downloads.json"
        record = json.loads(manifest.read_text()).get("indices", {}) if manifest.exists() else {}
        inventory["sources"].append({"file": str(archive_path), "download_manifest": record})
    for path in sorted((data / "raw" / "calendar").glob("ind_close_all_*.csv")):
        reference = source_reference(path)
        add(path.name, path.read_bytes(), {"delivery": "direct official archive", **reference})
        inventory["sources"].append(reference)
    inventory.update(first_session=min(sessions, default=None), last_session=max(sessions, default=None),
                     distinct_session_dates=len(sessions))
    return sessions, inventory


EXTERNAL_CALENDAR_FILENAME = "external-official-holidays.json"
EXTERNAL_CALENDAR_SCHEMA_VERSION = 1
# Bases that must never, alone, become closure evidence.  An HTTP 404 / absent
# archive member and a weekday pattern are evidence of absence only.
FORBIDDEN_CLOSURE_BASES = frozenset({"archive_absent", "http_404", "weekday_pattern", "missing_report"})


def _is_official_url(url: object) -> bool:
    """True only for NSE hosts (or an archive capture of an NSE page)."""
    if not isinstance(url, str) or not url.strip():
        return False
    host = (urllib.parse.urlparse(url.strip()).hostname or "").lower()
    if host == "nseindia.com" or host.endswith(".nseindia.com"):
        return True
    # Archived captures of official NSE pages, as used by the archived loader.
    return host == "web.archive.org" and "nseindia.com" in url.lower()


def validate_external_calendar_payload(payload: object, source_name: str = EXTERNAL_CALENDAR_FILENAME
                                       ) -> tuple[dict[date, list[dict]], dict[date, list[dict]], dict]:
    """Validate an external-official-holiday manifest; exact evidence only.

    Schema (schema_version 1)::

        {"schema_version": 1,
         "sources": [{"year": 2000, "official_url": "https://www.nseindia.com/...",
                      "publisher": "NSE", "capture_timestamp": "...",
                      "complete_for_year": true}],
         "entries": [{"date": "2000-01-26", "description": "Republic Day",
                      "session_type": "closed",  # or "special_session"
                      "official_url": "https://www.nseindia.com/...",
                      "source_year": 2000}]}

    Every entry needs an exact ISO date, a non-empty description, and an
    official NSE URL.  ``session_type`` must be ``closed``/``exchange_closed``
    (closure evidence) or ``special_session`` (possible-special-session
    evidence, never a full closure); a boolean ``special`` field is accepted
    as in the archived loader.  Non-official hosts, malformed/mismatched
    dates, unknown session types, conflicting duplicates, and entries whose
    stated basis is a 404/weekday pattern are rejected with ``ValueError``.
    A payload with no entries validates to empty evidence -- it resolves
    nothing, which is the correct result while official lists are absent.
    """
    if not isinstance(payload, dict):
        raise ValueError(f"External calendar manifest {source_name} must be a JSON object")
    if payload.get("schema_version") != EXTERNAL_CALENDAR_SCHEMA_VERSION:
        raise ValueError(
            f"External calendar manifest {source_name} schema_version must be "
            f"{EXTERNAL_CALENDAR_SCHEMA_VERSION}, got {payload.get('schema_version')!r}")
    sources = payload.get("sources", [])
    entries = payload.get("entries", [])
    if not isinstance(sources, list) or not isinstance(entries, list):
        raise ValueError(f"External calendar manifest {source_name} sources/entries must be lists")
    source_years: list[int] = []
    for source in sources:
        if not isinstance(source, dict) or not _is_official_url(source.get("official_url")):
            raise ValueError(f"External calendar source lacks an official NSE URL: {source!r}")
        try:
            source_years.append(int(source["year"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"External calendar source lacks an integer year: {source!r}") from error
    closures: dict[date, list[dict]] = defaultdict(list)
    exceptions: dict[date, list[dict]] = defaultdict(list)
    seen: set[tuple[date, str, str]] = set()
    kinds_by_date: dict[date, str] = {}
    years: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"External calendar entry must be an object: {entry!r}")
        basis = str(entry.get("basis", "")).strip().lower()
        if basis in FORBIDDEN_CLOSURE_BASES:
            raise ValueError(f"External calendar entry basis {basis!r} is never closure evidence: {entry!r}")
        if entry.get("http_status") in (404, "404") and not _is_official_url(entry.get("official_url")):
            raise ValueError(f"External calendar entry based only on HTTP 404 is not closure evidence: {entry!r}")
        try:
            day = date.fromisoformat(str(entry["date"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"External calendar entry lacks a valid ISO date: {entry!r}") from error
        description = str(entry.get("description", "")).strip()
        if not description:
            raise ValueError(f"External calendar entry lacks a description: {entry!r}")
        url = entry.get("official_url")
        if not _is_official_url(url):
            raise ValueError(f"External calendar entry lacks an official NSE URL: {entry!r}")
        source_year = entry.get("source_year")
        if source_year is not None and int(source_year) != day.year:
            raise ValueError(f"External calendar entry year mismatch: {entry!r}")
        session_type = str(entry.get("session_type", "")).strip().lower()
        if not session_type and "special" in entry:
            session_type = "special_session" if entry["special"] else "closed"
        if entry.get("closure") is True:
            session_type = "closed"
        special = session_type in ("special_session", "special", "possible_special_session")
        if not special and session_type not in ("closed", "exchange_closed", "closure"):
            raise ValueError(f"External calendar entry has unknown session_type: {entry!r}")
        category = "special_session" if special else "closed"
        key = (day, category, description)
        if key in seen:
            continue
        seen.add(key)
        previous = kinds_by_date.setdefault(day, category)
        if previous != category:
            raise ValueError(f"External calendar entry conflicts (closed vs special) for {day}: {entry!r}")
        years.add(day.year)
        evidence = {"kind": "external_possible_special_session" if special else "external_official_cm_holiday",
                    "description": description, "source": source_name, "official_url": url,
                    "source_year": day.year}
        (exceptions if special else closures)[day].append(evidence)
    inventory = {"external_calendar_present": True, "external_calendar_schema_version": EXTERNAL_CALENDAR_SCHEMA_VERSION,
                 "external_calendar_years": sorted(years | set(source_years)),
                 "external_calendar_entry_count": len(seen), "external_calendar_source_count": len(sources),
                 "external_calendar_sources": sources}
    return closures, exceptions, inventory


def load_external_calendar(data: Path) -> tuple[dict[date, list[dict]], dict[date, list[dict]], dict]:
    """Load the external official-holiday manifest if present, else empty.

    The manifest is expected at ``data/raw/calendar/external-official-holidays.json``
    and is the only route by which new official annual lists (e.g. for years
    with no captured API/archived calendar) can resolve unresolved dates.
    Its absence resolves nothing and is reported, never treated as a calendar.
    """
    path = data / "raw" / "calendar" / EXTERNAL_CALENDAR_FILENAME
    if not path.exists():
        return {}, {}, {"external_calendar_present": False,
                        "external_calendar_schema_version": EXTERNAL_CALENDAR_SCHEMA_VERSION,
                        "external_calendar_years": [], "external_calendar_entry_count": 0,
                        "external_calendar_source_count": 0, "external_calendar_file": str(path),
                        "external_calendar_note": "No external official-holiday manifest; "
                        "dates in years without a captured calendar remain unresolved."}
    payload = json.loads(path.read_text(encoding="utf-8"))
    closures, exceptions, inventory = validate_external_calendar_payload(payload, str(path))
    inventory["external_calendar_file"] = str(path)
    inventory["external_calendar_source_reference"] = source_reference(path)
    return closures, exceptions, inventory


def calendar_coverage_breakdown(start: date, cutoff: date, calendar_inventory: dict,
                                session_audit: dict) -> dict:
    """Derive calendar-completeness counts from the audit itself.

    No year, count, or unresolved total is hard-coded: covered years are the
    union of the non-empty holiday-API years, archived-page years, and
    validated external-manifest years, intersected with the requested span.
    """
    requested = list(range(start.year, cutoff.year + 1))
    covered = sorted(set(calendar_inventory.get("years_with_cm_calendar", []))
                     | set(calendar_inventory.get("archived_years_with_cm_calendar", []))
                     | set(calendar_inventory.get("external_calendar_years", [])))
    covered_in_span = [year for year in covered if start.year <= year <= cutoff.year]
    missing_years = [year for year in requested if year not in covered_in_span]
    unresolved = [item for item in session_audit.get("missing_dates", [])
                  if item["classification"] == "unresolved"]
    by_year = dict(sorted(Counter(item["date"].year for item in unresolved).items()))
    return {
        "requested_years": requested,
        "years_with_any_cm_calendar": covered_in_span,
        "years_without_any_cm_calendar": missing_years,
        "unresolved_by_year": by_year,
        "unresolved_in_years_without_any_calendar": sum(
            1 for item in unresolved if item["date"].year in missing_years),
        "unresolved_in_years_with_calendar": sum(
            1 for item in unresolved if item["date"].year in covered_in_span),
        "unresolved_weekend_dates": [str(item["date"]) for item in unresolved if item.get("weekend")],
        "unassessed_weekends_by_year": dict(sorted(
            ((int(year), int(count)) for year, count in
             session_audit.get("unassessed_weekends_by_year", {}).items()))),
        "external_calendar_present": bool(calendar_inventory.get("external_calendar_present")),
        "external_calendar_entry_count": calendar_inventory.get("external_calendar_entry_count", 0),
    }


def calendar_remediation_report(report: dict) -> dict:
    """Derived remediation ledger for the calendar blocker (2000-2026).

    Every count is computed from ``report``; nothing is copied from a stale
    audit.  Dates are listed exactly; no holiday is inferred from a 404 or a
    weekday pattern.  The embedded manifest schema is the contract a host
    must satisfy to supply official lists for the missing years.
    """
    sessions, inventory = report["sessions"], report["calendar_inventory"]
    snapshot = report["snapshot"]
    start = date.fromisoformat(str(snapshot["requested_start"]))
    cutoff = date.fromisoformat(str(snapshot["requested_cutoff"]))
    breakdown = calendar_coverage_breakdown(start, cutoff, inventory, sessions)
    unresolved = [item for item in sessions["missing_dates"] if item["classification"] == "unresolved"]
    missing_years = breakdown["years_without_any_cm_calendar"]
    remediation = {
        "requested_span": [str(start), str(cutoff)],
        "snapshot_span": [str(snapshot["first_observed"]), str(snapshot["last_observed"])],
        "missing_date_counts": sessions["missing_date_counts"],
        "within_observed_span_counts": sessions.get("within_observed_span_counts", {}),
        "unresolved_count": len(unresolved),
        "unresolved_dates": [str(item["date"]) for item in unresolved],
        "unresolved_by_year": breakdown["unresolved_by_year"],
        "unresolved_in_years_without_any_calendar": breakdown["unresolved_in_years_without_any_calendar"],
        "unresolved_in_years_with_calendar": breakdown["unresolved_in_years_with_calendar"],
        "unresolved_in_years_with_calendar_dates": [
            str(item["date"]) for item in unresolved if item["date"].year not in missing_years],
        "years_without_any_cm_calendar": missing_years,
        "years_with_any_cm_calendar": breakdown["years_with_any_cm_calendar"],
        "unassessed_absent_weekends": sessions["unobserved_weekends_without_independent_or_calendar_evidence"],
        "unassessed_weekends_by_year": sessions.get("unassessed_weekends_by_year", {}),
        "observed_weekend_sessions": sessions.get("observed_weekend_session_count"),
        "observed_weekend_scope_counts": sessions.get("observed_weekend_scope_counts", {}),
        "observed_weekend_dates": [str(item["date"]) for item in sessions.get("observed_weekend_sessions", [])],
        "external_evidence_manifest": {
            "expected_path": f"data/raw/calendar/{EXTERNAL_CALENDAR_FILENAME}",
            "schema_version": EXTERNAL_CALENDAR_SCHEMA_VERSION,
            "present": bool(inventory.get("external_calendar_present")),
            "entry_count": inventory.get("external_calendar_entry_count", 0),
            "years": inventory.get("external_calendar_years", []),
            "schema": {
                "schema_version": EXTERNAL_CALENDAR_SCHEMA_VERSION,
                "sources": [{"year": "int", "official_url": "https://www.nseindia.com/...",
                             "publisher": "NSE", "capture_timestamp": "str",
                             "complete_for_year": "bool (inventory only; per-date exact evidence resolves a date)"}],
                "entries": [{"date": "YYYY-MM-DD (exact)", "description": "str, non-empty",
                             "session_type": "'closed' | 'special_session'",
                             "official_url": "official NSE URL (nseindia.com host or archive capture of one)",
                             "source_year": "int, must equal date year"}],
                "rejected": ["non-official hosts", "malformed or year-mismatched dates",
                             "unknown session_type", "closed/special conflicts for one date",
                             "basis archive_absent/http_404/weekday_pattern/missing_report",
                             "404-only entries"],
            },
        },
        "next_actions": [
            f"Obtain official NSE annual equity-holiday lists for {missing_years or 'no missing years'} "
            "(captured official pages/announcements, not third-party summaries) and record them in the "
            "external manifest above; rerun the audit to resolve exact dates.",
            "For unresolved dates in years with a captured calendar "
            f"({breakdown['unresolved_in_years_with_calendar']} dates), obtain a fuller/re-captured official "
            "list for that year: absence from a captured list is not proof of a session.",
            "Assess the unassessed absent weekends only against complete annual calendars plus official "
            "special-session announcements for every year; observed weekend sessions prove special sessions "
            "occur, so weekends must not be assumed closed.",
            "Do not classify any date from HTTP 404/archive absence, missing reports, or weekday patterns.",
            f"Cutoff {cutoff} itself is a candidate date with only archive-absence evidence; resolve it only "
            "with official session/closure evidence at or after the cutoff.",
        ],
        "resolved_by_this_audit": {
            "note": "No unresolved date was reclassified without exact official evidence in the "
                    "currently encoded/self-contained sources; counts above are recomputed, not stale.",
            "confirmed_exchange_closure": sessions["missing_date_counts"].get("confirmed_exchange_closure", 0),
            "confirmed_source_gap": sessions["missing_date_counts"].get("confirmed_source_gap", 0),
        },
        "official_source_search": {
            "status": "unresolved_no_official_source_obtained" if missing_years else "complete",
            "note": (
                "Official NSE annual equity-holiday evidence was sought for every year in "
                "years_without_any_cm_calendar via the existing conventions only: "
                "(1) the cached official holiday-master API responses "
                "(data/raw/calendar/trading-holidays-<year>.json, empty {} for these years), "
                "(2) the archived official NSE equity-holiday page loader "
                "(data/raw/calendar/nse-archived-eq-holidays.json, which covers only its "
                "archived_years_with_cm_calendar), and "
                "(3) the validated external-official-holidays manifest. "
                "Live retrieval of the official NSE pages / Wayback captures was attempted in "
                "this environment and returned no obtainable official content (transport/proxy "
                "failure), so no complete official annual list for these years was obtained. "
                "No external-official-holidays.json input was created or edited with guessed, "
                "third-party, or 404/weekday-pattern dates; the manifest remains absent "
                "(present=false, entry_count=0 above) and every date listed in "
                "unresolved_dates for these years remains unresolved until a host supplies a "
                "complete official list satisfying the manifest schema and the audit is rerun."
            ),
            "routes_checked": [
                "NSE holiday-master API (cached trading-holidays-<year>.json; empty for missing years)",
                "Archived official NSE equity-holiday page (nse-archived-eq-holidays.json; archived years only)",
                "External official-holiday manifest (data/raw/calendar/external-official-holidays.json; absent)",
                "Live official NSE / Wayback capture retrieval (attempted; no obtainable official content in this environment)",
            ],
            "archived_years_with_cm_calendar": inventory.get("archived_years_with_cm_calendar", []),
            "years_with_any_cm_calendar": breakdown["years_with_any_cm_calendar"],
            "per_year": [
                {
                    "year": year,
                    "unresolved_count": breakdown["unresolved_by_year"].get(year, breakdown["unresolved_by_year"].get(str(year), 0)),
                    "unresolved_dates": [
                        str(item["date"]) for item in unresolved if item["date"].year == year],
                    "official_annual_list_obtained": False,
                    "entries_added": 0,
                    "status": "unresolved",
                }
                for year in missing_years
            ],
        },
    }
    return remediation


def calendar_remediation_markdown(remediation: dict) -> str:
    manifest = remediation["external_evidence_manifest"]
    return "\n".join([
        "# Calendar remediation (derived from coverage-audit.json)", "",
        f"Requested span: {remediation['requested_span'][0]} through {remediation['requested_span'][1]}; "
        f"snapshot: {remediation['snapshot_span'][0]}–{remediation['snapshot_span'][1]}.", "",
        f"- Missing-date counts (recomputed): {remediation['missing_date_counts']}.",
        f"- Unresolved: {remediation['unresolved_count']} dates; by year: {remediation['unresolved_by_year']}.",
        f"- Unresolved in years with no captured calendar: {remediation['unresolved_in_years_without_any_calendar']} "
        f"(years without any CM calendar: {remediation['years_without_any_cm_calendar']}).",
        f"- Unresolved in years with a captured calendar: {remediation['unresolved_in_years_with_calendar']} "
        f"({', '.join(remediation['unresolved_in_years_with_calendar_dates']) or 'none'}); "
        "absence from a captured list is not proof of a session.",
        f"- Unassessed absent weekends: {remediation['unassessed_absent_weekends']}; by year: "
        f"{remediation['unassessed_weekends_by_year']}.",
        f"- Observed weekend sessions retained: {remediation['observed_weekend_sessions']} "
        f"(scope: {remediation['observed_weekend_scope_counts']}); these prove special sessions occur, "
        "so absent weekends must not be assumed closed.", "",
        "## External official-holiday manifest", "",
        f"- Expected path: `{manifest['expected_path']}`; present: {manifest['present']}; "
        f"entries loaded: {manifest['entry_count']}; years: {manifest['years']}.",
        f"- Schema version: {manifest['schema_version']}. The validator accepts only exact date + "
        "description + official NSE URL entries (`closed` or `special_session`); it rejects non-official "
        "hosts, year mismatches, conflicts, and 404/weekday-pattern bases.", "",
        "## Official source search (missing years)", "",
        f"- Status: {remediation.get('official_source_search', {}).get('status', 'unknown')}.",
        f"- {remediation.get('official_source_search', {}).get('note', '')}", "",
        *[f"- {item['year']}: {item['unresolved_count']} unresolved "
          f"({', '.join(item['unresolved_dates']) or 'none'}); official annual list obtained: "
          f"{item['official_annual_list_obtained']}; entries added: {item['entries_added']}."
          for item in remediation.get('official_source_search', {}).get('per_year', [])],
        "## Next actions", "",
        *[f"- {item}" for item in remediation["next_actions"]], "",
    ])


def load_calendar(data: Path) -> tuple[dict[date, list[dict]], dict[date, list[dict]], dict]:
    closures: dict[date, list[dict]] = defaultdict(list)
    exceptions: dict[date, list[dict]] = defaultdict(list)
    inventory = {"years_with_cm_calendar": [], "empty_cm_years": [],
                 "archived_years_with_cm_calendar": [], "archived_year_row_counts": {}, "sources": []}
    for path in sorted((data / "raw" / "calendar").glob("trading-holidays-*.json")):
        if path.name.endswith(".metadata.json"):
            continue
        year = int(path.stem.rsplit("-", 1)[1])
        rows = json.loads(path.read_text(encoding="utf-8")).get("CM", [])
        inventory["sources"].append(source_reference(path))
        inventory["years_with_cm_calendar" if rows else "empty_cm_years"].append(year)
        for row in rows:
            day = exchange_date(row["tradingDate"])
            if day.year != year:
                raise ValueError(f"Calendar year mismatch in {path}")
            description = row["description"]
            # The holiday list includes Laxmi Pujan despite Muhurat trading.
            special = bool(re.search(r"LAXMI|LAKSHMI|MUHURAT|TRADING|UNION BUDGET|\*", description, re.I))
            target = exceptions if special else closures
            target[day].append({"kind": "possible_special_session" if special else "official_cm_holiday",
                                "description": description, "source": str(path),
                                "official_url": f"{NSE}/api/holiday-master?type=trading&year={year}"})
    archive_path = data / "raw" / "calendar" / "nse-archived-eq-holidays.json"
    if archive_path.exists():
        # Official NSE equity-holiday pages captured before the holiday API's
        # 2011 coverage starts.  These are closure/special-session evidence,
        # never an inference from a missing price report.
        inventory["sources"].append(source_reference(archive_path))
        archived = json.loads(archive_path.read_text(encoding="utf-8"))
        for year_key, bundle in sorted(archived.get("years", {}).items()):
            year = int(year_key)
            inventory["archived_years_with_cm_calendar"].append(year)
            inventory["archived_year_row_counts"][year] = len(bundle.get("rows", []))
            for row in bundle.get("rows", []):
                day = date.fromisoformat(row["date"])
                if day.year != year:
                    raise ValueError(f"Archived calendar year mismatch in {archive_path}: {day}")
                description = row["description"]
                special = bool(row.get("special")) or bool(
                    re.search(r"LAXMI|LAKSHMI|MUHURAT|TRADING|UNION BUDGET|\*", description, re.I))
                target = exceptions if special else closures
                evidence = {"kind": "archived_possible_special_session" if special else "archived_official_cm_holiday",
                            "description": description, "source": str(archive_path),
                            "official_url": bundle["url"],
                            "capture_timestamp": bundle["capture_timestamp"],
                            "source_date": row.get("source_date")}
                if row.get("date_note"):
                    evidence["date_note"] = row["date_note"]
                target[day].append(evidence)
    # External official annual lists (validated, exact evidence only).  Absence
    # of this manifest adds no evidence and resolves no date.
    ext_closures, ext_special, ext_inventory = load_external_calendar(data)
    inventory.update(ext_inventory)
    for day, evidence_list in ext_closures.items():
        closures[day].extend(evidence_list)
    for day, evidence_list in ext_special.items():
        exceptions[day].extend(evidence_list)
    if ext_inventory.get("external_calendar_present"):
        inventory["sources"].append(ext_inventory.get("external_calendar_source_reference", {}))
    path = data / "raw" / "calendar" / "exchange-holidays.html"
    if path.exists():
        inventory["sources"].append(source_reference(path))
        text = re.sub(r"<[^>]+>", " ", path.read_text(encoding="utf-8"))
        match = re.search(r"Muhurat Trading will be conducted on\s+\w+,\s+(\w+\s+\d{1,2},\s+\d{4})", text)
        if match:
            day = datetime.strptime(re.sub(r"\s+", " ", match[1]), "%B %d, %Y").date()
            exceptions[day].append({"kind": "official_special_session_announcement", "source": str(path),
                                    "official_url": HOLIDAYS, "description": "Muhurat trading announced"})
            inventory["announced_muhurat_session"] = day
    return closures, exceptions, inventory


def load_gap_backfill(data: Path) -> tuple[dict[date, list[dict]], dict[date, list[dict]], dict]:
    """Read the host's official-archive probe evidence without re-probing.

    A downloaded official bhavcopy is positive session evidence.  An HTTP 404
    or absent archive member is evidence of absence only; it never becomes a
    holiday or closure by itself.
    """
    sessions: dict[date, list[dict]] = defaultdict(list)
    absent: dict[date, list[dict]] = defaultdict(list)
    path = data / "reports" / "official-gap-backfill.json"
    inventory = {"file": str(path), "present": path.exists(), "downloaded_sessions": [],
                 "absent_sessions": [], "unexpected_results": [], "notes": []}
    if not path.exists():
        inventory["notes"].append("No official-gap-backfill.json; archive absence is not separately audited.")
        return sessions, absent, inventory
    inventory["source"] = source_reference(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    inventory.update(candidate_count=payload.get("candidate_count"),
                     summary=payload.get("summary"), blocked=payload.get("blocked"),
                     evidence_note=payload.get("note"))
    for result in payload.get("results", []):
        day = date.fromisoformat(result["date"])
        status = result.get("status")
        if status == "downloaded":
            sessions[day].append({"kind": "official_bhavcopy_session", "source": str(path),
                                  "official_url": result.get("url"), "http_status": result.get("http_status", 200),
                                  "bytes": result.get("bytes"), "sha256": result.get("sha256"),
                                  "publisher_digest_verified": result.get("publisher_digest_verified")})
            inventory["downloaded_sessions"].append(str(day))
        elif status == "absent":
            absent[day].append({"kind": "official_archive_absent", "source": str(path),
                                "official_url": result.get("url"), "http_status": result.get("http_status"),
                                "note": "HTTP 404/archive absence is evidence of absence, never proof of exchange closure."})
            inventory["absent_sessions"].append(str(day))
        else:
            inventory["unexpected_results"].append({"date": str(day), "status": status})
    summary = payload.get("summary") or {}
    if summary.get("downloaded") is not None and summary.get("downloaded") != len(sessions):
        inventory["notes"].append("Downloaded-date count disagrees with the backfill summary.")
    if summary.get("absent") is not None and summary.get("absent") != len(absent):
        inventory["notes"].append("Absent-date count disagrees with the backfill summary.")
    return sessions, absent, inventory


def read_snapshot(database: Path, start: date | None, cutoff: date) -> tuple[dict, dict, list[dict]]:
    # Close before any network work or report writing, so the host can rebuild.
    with open_catalog(database=database) as db:
        first = db.execute("SELECT min(date) FROM bars WHERE date <= ?", [cutoff]).fetchone()[0]
        start = start or first
        if start is None or start > cutoff:
            raise ValueError("No database observations in the requested range")
        parameters = [start, cutoff]
        rows = db.execute("""SELECT date, count(*), count(DISTINCT symbol),
            count(*) FILTER (WHERE valid_ohlcv AND volume > 0),
            string_agg(DISTINCT symbol, ', ' ORDER BY symbol) FILTER (WHERE dayofweek(date) IN (0,6)),
            count(DISTINCT symbol) FILTER (WHERE series IN ('EQ','BE','BZ','SM','ST')),
            count(*) FILTER (WHERE series IN ('EQ','BE','BZ','SM','ST')),
            string_agg(DISTINCT series, ', ' ORDER BY series)
            FROM bars WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date""", parameters).fetchall()
        symbols = db.execute("""SELECT symbol, min(date), max(date), count(*),
            count(DISTINCT isin), count(*) FILTER (WHERE isin IS NULL),
            string_agg(DISTINCT isin, ', ' ORDER BY isin)
            FROM bars WHERE date BETWEEN ? AND ? AND series IN ('EQ','BE','BZ','SM','ST')
            GROUP BY symbol ORDER BY symbol""", parameters).fetchall()
        counts = db.execute("""SELECT count(*), count(DISTINCT symbol),
            count(*) FILTER (WHERE series IN ('EQ','BE','BZ','SM','ST')),
            count(DISTINCT symbol) FILTER (WHERE series IN ('EQ','BE','BZ','SM','ST') AND valid_ohlcv AND volume > 0)
            FROM bars WHERE date BETWEEN ? AND ?""", parameters).fetchone()
    if not rows:
        raise ValueError("No database observations in the requested range")
    summary = dict(zip(("rows", "historical_symbols_all_series", "equity_candidate_rows", "usable_equity_candidate_symbols"), counts))
    summary.update(database=str(database), requested_start=start, requested_cutoff=cutoff,
                   first_observed=rows[0][0], last_observed=rows[-1][0], sessions=len(rows))
    sessions = {row[0]: {"rows": row[1], "symbols": row[2], "positive_volume_valid_rows": row[3],
                         **({"observed_symbols": row[4]} if row[1] <= 20 and row[4] else {}),
                         "equity_candidate_symbols": row[5], "equity_candidate_rows": row[6],
                         "series": row[7]} for row in rows}
    history = [dict(zip(("symbol", "first_observed", "last_observed", "rows", "distinct_isins",
                         "rows_without_isin", "observed_isins"), row)) for row in symbols]
    return summary, sessions, history


def classify_dates(observed: dict, start: date, cutoff: date, index: dict,
                   closures: dict, special: dict,
                   official_sessions: dict | None = None,
                   archive_absence: dict | None = None) -> dict:
    official_sessions = official_sessions or {}
    archive_absence = archive_absence or {}
    candidates = set()
    day = start
    unassessed_weekends = 0
    unassessed_by_year: Counter = Counter()
    while day <= cutoff:
        if day not in observed:
            if (day.weekday() < 5 or day in index or day in special
                    or day in official_sessions or day == cutoff):
                candidates.add(day)
            elif day not in closures:
                unassessed_weekends += 1
                unassessed_by_year[day.year] += 1
        day += timedelta(days=1)
    missing = []
    for day in sorted(candidates):
        evidence = index.get(day, []) + special.get(day, []) + official_sessions.get(day, [])
        if day in official_sessions or day in index:
            status = "confirmed_source_gap"
        elif day in closures and day not in special:
            status = "confirmed_exchange_closure"
            # A 404 may accompany a closure, but it is never the reason for it.
            evidence = closures[day] + archive_absence.get(day, [])
        else:
            status = "unresolved"
            evidence += closures.get(day, []) + archive_absence.get(day, [])
        missing.append({"date": day, "classification": status, "weekend": day.weekday() >= 5,
                        "evidence": evidence,
                        "evidence_kinds": sorted({item.get("kind") for item in evidence})})

    def scope_assessment(item: dict) -> str:
        equity_symbols = item.get("equity_candidate_symbols")
        if equity_symbols is None:
            return "unknown_scope"
        if equity_symbols == 0:
            return "no_equity_series"
        if equity_symbols < 100:
            return "thin_equity_session_review"
        return "broad_equity_session"

    weekends = [{"date": day, **observed[day],
                 "independent_session_evidence": index.get(day, []),
                 "official_session_evidence": official_sessions.get(day, []),
                 "official_calendar_evidence": special.get(day, []),
                 "scope_assessment": scope_assessment(observed[day]),
                 "scope_note": "Observed report rows do not prove a general common-stock session; thin equity counts are review flags."}
                for day in sorted(observed) if day.weekday() >= 5]
    conflicts = [{"date": day, "holiday_evidence": closures[day], "observation": observed[day]}
                 for day in sorted(set(observed) & set(closures))]
    return {"missing_date_counts": dict(Counter(item["classification"] for item in missing)),
            "missing_dates": missing,
            "source_gap_dates": [item["date"] for item in missing
                                 if item["classification"] == "confirmed_source_gap"],
            "confirmed_closure_dates": [item["date"] for item in missing
                                        if item["classification"] == "confirmed_exchange_closure"],
            "unresolved_dates": [item["date"] for item in missing
                                 if item["classification"] == "unresolved"],
            "observed_weekend_sessions": weekends,
            "observed_weekend_session_count": len(weekends),
            "observed_weekend_scope_counts": dict(Counter(item["scope_assessment"] for item in weekends)),
            "observed_weekend_sessions_with_index_evidence": sum(
                1 for item in weekends if item["independent_session_evidence"]),
            "observed_weekend_sessions_with_official_bhavcopy_evidence": sum(
                1 for item in weekends if item["official_session_evidence"]),
            "observed_sessions_on_holiday_calendar": conflicts,
            "unobserved_weekends_without_independent_or_calendar_evidence": unassessed_weekends,
            "unassessed_weekends_by_year": dict(sorted(unassessed_by_year.items())),
            "weekend_scope_note": "Unassessed weekends are NOT confirmed closures; unknown historical special sessions may remain.",
            "archive_absence_evidence_count": sum(
                1 for item in missing if "official_archive_absent" in item["evidence_kinds"]),
            "official_session_evidence_count": sum(
                1 for item in missing if "official_bhavcopy_session" in item["evidence_kinds"]),
            "special_calendar_dates": [{"date": day, "observed": day in observed, "evidence": evidence}
                                       for day, evidence in sorted(special.items()) if start <= day <= cutoff]}


def audit_security_history(data: Path, history: list[dict], cutoff: date,
                           start: date | None = None) -> dict:
    by_symbol = {row["symbol"]: row for row in history}
    if start is None:
        observed_starts = [row.get("first_observed") for row in history if row.get("first_observed")]
        start = min(observed_starts, default=None)
    path = data / "raw" / "security_master" / "delisted.csv"
    result = {"candidate_symbols_observed": len(history), "official_delisted_matches": [],
              "official_delisted_symbols_not_observed": [], "sources": [],
              "identity_warning": "Symbol-only joins are examples, not identity continuity or proof of missing data; relisting/reuse is possible."}
    coverage = {"official_list_present": path.exists(), "notes": []}
    if path.exists():
        result["sources"].append(source_reference(path))
        raw = list(csv.DictReader(io.StringIO(csv_text(path.read_bytes()))))
        dated = [(row, exchange_date(row["Delisted Date"])) for row in raw if row.get("Delisted Date", "").strip()]
        last_delisting = max((day for _, day in dated), default=None)
        result.update(official_delisting_list_rows=len(raw),
                      official_list_first_delisting=min((day for _, day in dated), default=None),
                      official_list_last_delisting=last_delisting)
        coverage.update(
            official_list_rows=len(raw), dated_rows=len(dated),
            first_delisting=min((day for _, day in dated), default=None),
            last_delisting=last_delisting,
            last_delisting_age_days=(cutoff - last_delisting).days if last_delisting else None,
            type_counts=dict(Counter(row["Type of Delisting"].strip() for row in raw)),
            rows_before_requested_span=sum(1 for _, day in dated if start and day < start),
            rows_within_requested_span=sum(1 for _, day in dated if start and start <= day <= cutoff),
            rows_after_cutoff=sum(1 for _, day in dated if day > cutoff))
        for row, day in dated:
            if day > cutoff:
                continue
            symbol = row["Symbol"].strip().upper()
            evidence = {"symbol": symbol, "company": row["Company"].strip(), "delisting_date": day,
                        "delisting_type": row["Type of Delisting"].strip(), "source": DELISTED}
            if symbol in by_symbol:
                result["official_delisted_matches"].append({**evidence, **by_symbol[symbol]})
            else:
                result["official_delisted_symbols_not_observed"].append(evidence)

        near = before = after = unknown = 0
        for match in result["official_delisted_matches"]:
            last_observed = match.get("last_observed")
            delisting_date = match["delisting_date"]
            if last_observed is None:
                unknown += 1
            elif last_observed > delisting_date + timedelta(days=30):
                after += 1
            elif last_observed < delisting_date - timedelta(days=30):
                before += 1
            else:
                near += 1
        official_symbols = {row["Symbol"].strip().upper() for row, _ in dated}
        ending = [row for row in history
                  if row.get("last_observed") and row["last_observed"] < cutoff - timedelta(days=30)]
        ending_not_in_official = [row for row in ending if row["symbol"] not in official_symbols]
        ending_examples = sorted(ending_not_in_official, key=lambda row: row["last_observed"])[:20]
        coverage.update(
            matched_symbols=len(result["official_delisted_matches"]),
            unmatched_symbols=len(result["official_delisted_symbols_not_observed"]),
            matched_last_observed_within_30_days=near,
            matched_last_observed_before_delisting_30d=before,
            matched_last_observed_after_delisting_30d=after,
            matched_last_observed_unknown=unknown,
            db_symbols_ending_before_cutoff_30d=len(ending),
            db_ending_symbols_not_in_official_list=len(ending_not_in_official),
            db_ending_symbols_not_in_official_list_examples=[
                {"symbol": row["symbol"], "last_observed": row["last_observed"]}
                for row in ending_examples],
            official_list_span=f"{min((day for _, day in dated), default=None)}..{last_delisting}",
            coverage_limit=("Official list is bounded to 2002-2020 and has no 2021-2026 events; "
                            "it is not a complete historical delisting/suspension record."))
        coverage["notes"].extend([
            "A delisted symbol matched by symbol only; identity continuity, relisting and symbol reuse are not proven.",
            "Unmatched official names are review items, not proven omissions.",
            "DB symbols whose observations end before cutoff are review candidates, not proven delistings; "
            "ends can reflect symbol changes, mergers, series changes or source gaps.",
        ])
    else:
        coverage["notes"].append("No official delisted CSV was available; delisting coverage is not assessed.")
    result["delisting_coverage"] = coverage
    master = data / "raw" / "security_master" / "EQUITY_L.csv"
    if master.exists():
        result["sources"].append(source_reference(master))
        rows = list(csv.DictReader(io.StringIO(csv_text(master.read_bytes()))))
        result["current_master_rows"] = len(rows)
        result["current_master_usage"] = "Provenance only; current master is NOT an as-of-cutoff universe and never filters history."
    examples = ("DHFL", "DEWANHOUS", "KFA", "SATYAMCOMP", "LAKSHVILAS", "JETAIRWAYS", "RELCAPITAL", "SINTEX", "HEXAWARE")
    result["historical_name_observations"] = [by_symbol[symbol] for symbol in examples if symbol in by_symbol]
    result["matched_delisted_symbol_count"] = len(result["official_delisted_matches"])
    result["unmatched_delisted_symbol_count"] = len(result["official_delisted_symbols_not_observed"])
    return result


def markdown_report(report: dict) -> str:
    snapshot, sessions, securities = report["snapshot"], report["sessions"], report["security_history"]
    counts = sessions["missing_date_counts"]
    gaps = [str(item["date"]) for item in sessions["missing_dates"] if item["classification"] == "confirmed_source_gap"]
    unresolved = Counter(str(item["date"])[:4] for item in sessions["missing_dates"] if item["classification"] == "unresolved")
    weekend_counts = sessions.get("observed_weekend_scope_counts", {})
    thin_weekends = [str(item["date"]) for item in sessions["observed_weekend_sessions"]
                     if item["scope_assessment"] in ("thin_equity_session_review", "no_equity_series")]
    delisting = securities.get("delisting_coverage", {})
    backfill = report.get("official_gap_backfill", {})
    breakdown = calendar_coverage_breakdown(
        date.fromisoformat(str(snapshot["requested_start"])),
        date.fromisoformat(str(snapshot["requested_cutoff"])),
        report["calendar_inventory"], sessions)
    return "\n".join([
        "# NSE coverage audit", "",
        f"Requested: {snapshot['requested_start']} through **{snapshot['requested_cutoff']}**, inclusive.",
        f"Observed snapshot: **{snapshot['first_observed']}–{snapshot['last_observed']}**, {snapshot['sessions']:,} sessions, {snapshot['rows']:,} rows.", "",
        f"- Confirmed closures among candidate missing dates: {counts.get('confirmed_exchange_closure', 0)}.",
        f"- Confirmed source gaps: {', '.join(gaps) or 'none established'}.",
        f"- Unresolved candidate dates: {counts.get('unresolved', 0)}; years/counts: {dict(sorted(unresolved.items()))}.",
        f"- Observed weekend sessions retained: {sessions.get('observed_weekend_session_count', len(sessions['observed_weekend_sessions']))}; "
        f"scope counts: {weekend_counts}; unassessed absent weekends: {sessions['unobserved_weekends_without_independent_or_calendar_evidence']} "
        f"(by year: {sessions.get('unassessed_weekends_by_year', {})}).",
        f"- Years without any captured CM calendar (derived): {breakdown['years_without_any_cm_calendar']}; "
        f"unresolved by year (derived): {breakdown['unresolved_by_year']}; "
        f"external official-holiday manifest present: {breakdown['external_calendar_present']} "
        f"({breakdown['external_calendar_entry_count']} validated entries).",
        f"- Official archive probes: {len(backfill.get('absent_sessions', []))} absent dates; a 404 is evidence of absence, never proof of closure.",
        f"- Rejected index reports with filename/internal-date disagreement: {len(report['index_inventory']['rejected_reports'])}; never silently reinterpreted.",
        "- All exact unresolved dates, evidence references and holiday/session conflicts are in `coverage-audit.json`.", "",
        "## Weekend special sessions", "",
        f"- Observed weekend sessions with independent index evidence: {sessions.get('observed_weekend_sessions_with_index_evidence', 0)}.",
        f"- Observed weekend sessions with downloaded official bhavcopy evidence: {sessions.get('observed_weekend_sessions_with_official_bhavcopy_evidence', 0)}.",
        f"- Thin or non-equity weekend sessions retained for review: {', '.join(thin_weekends) or 'none'}.",
        f"- Official bhavcopy downloads are positive session evidence, not holidays; a downloaded report missing from the bars snapshot would be a confirmed source gap. "
        f"Missing dates currently confirmed as source gaps from such evidence: {counts.get('confirmed_source_gap', 0)} "
        f"({', '.join(gaps) or 'none'}).", "",
        "## Delisting coverage limits", "",
        f"- Official delisted CSV: {delisting.get('official_list_rows', securities.get('official_delisting_list_rows', 0))} rows, "
        f"span {delisting.get('official_list_span', 'unknown')}, latest event {delisting.get('last_delisting', securities.get('official_list_last_delisting'))} "
        f"({delisting.get('last_delisting_age_days', 'unknown')} days before cutoff).",
        f"- Type counts: {delisting.get('type_counts', {})}.",
        f"- Symbol matches: {delisting.get('matched_symbols', securities.get('matched_delisted_symbol_count', 0))}; "
        f"unmatched official names: {delisting.get('unmatched_symbols', securities.get('unmatched_delisted_symbol_count', 0))}.",
        f"- DB symbols whose observations end >30 days before cutoff: {delisting.get('db_symbols_ending_before_cutoff_30d', 0)}; "
        f"not in the official delisted list: {delisting.get('db_ending_symbols_not_in_official_list', 0)} review candidates.",
        "- The list is stale after 2020-11-09 and symbol-only joins do not prove identity, relisting or true omission.", "",
        "## Primary evidence", "",
        f"- [NSE CM holiday API]({NSE}/api/holiday-master?type=trading&year=2026); returned CM years: {report['calendar_inventory']['years_with_cm_calendar']}.",
        f"- Archived official NSE equity-holiday pages for years: {report['calendar_inventory'].get('archived_years_with_cm_calendar', [])}.",
        f"- [NSE market calendar]({HOLIDAYS}); Muhurat announcement: {report['calendar_inventory'].get('announced_muhurat_session', 'not captured')}.",
        f"- [Official gap backfill evidence]({backfill.get('file', 'data/reports/official-gap-backfill.json')}): "
        f"downloaded {len(backfill.get('downloaded_sessions', []))}, absent {len(backfill.get('absent_sessions', []))}, blocked={backfill.get('blocked')}.",
        *[f"- [NSE independent session report, {item['date']}]({item['evidence'][0]['official_url']})."
          for item in sessions['missing_dates'] if item['classification'] == 'confirmed_source_gap'],
        "- Index archive delivery is a third-party mirror; direct official confirmations are separately identified.",
        f"- [NSE delisting list]({DELISTED}); [NSE current equity master]({MASTER}) is retained only as evidence, never used to filter historical stocks.", "",
        "## Limits", "",
        *[f"- {item}" for item in report["limitations"]], "",
        "Rerun after a host rebuild: `uv run python src/audit_coverage.py --cutoff 2026-11-07`.", "",
    ])


def audit_coverage(data: Path, cutoff: date, start: date | None = None) -> dict:
    if start is not None and start > cutoff:
        raise ValueError("Start must not exceed cutoff")
    snapshot, observed, history = read_snapshot(data / "market.duckdb", start, cutoff)
    start = snapshot["requested_start"]
    indices, index_inventory = load_index_evidence(data)
    closures, special, calendar_inventory = load_calendar(data)
    official_sessions, archive_absence, backfill_inventory = load_gap_backfill(data)
    session_audit = classify_dates(observed, start, cutoff, indices, closures, special,
                                   official_sessions=official_sessions,
                                   archive_absence=archive_absence)
    span_missing = [item for item in session_audit["missing_dates"]
                    if item["date"] <= snapshot["last_observed"] and not item["weekend"]]
    session_audit["missing_weekdays_within_observed_span"] = len(span_missing)
    session_audit["within_observed_span_counts"] = dict(Counter(item["classification"] for item in span_missing))
    breakdown = calendar_coverage_breakdown(start, cutoff, calendar_inventory, session_audit)
    calendar_inventory.update(breakdown)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "snapshot": snapshot,
        "sessions": session_audit, "calendar_inventory": calendar_inventory,
        "index_inventory": index_inventory, "official_gap_backfill": backfill_inventory,
        "gap_backfill_inventory": backfill_inventory,
        "security_history": audit_security_history(data, history, cutoff, start),
        "complete_survivorship_free_coverage_established": False,
        "limitations": [
            "Weekdays, missing files, HTTP 404s and absent index reports never establish exchange closure.",
            "Official archive absence (HTTP 404) is evidence of absence, never proof of closure.",
            f"Years without any captured CM calendar (derived from API + archived + external "
            f"inventories over {start.year}-{cutoff.year}): {breakdown['years_without_any_cm_calendar']}; "
            f"{breakdown['unresolved_in_years_without_any_calendar']} unresolved dates fall in those years and "
            f"{breakdown['unresolved_in_years_with_calendar']} in years with a captured calendar (absence from "
            "a captured list is not proof of a session). Captured archived years: "
            f"{calendar_inventory.get('archived_years_with_cm_calendar', [])} "
            f"(rows per year: {calendar_inventory.get('archived_year_row_counts', {})}).",
            "Holiday lists may contain Muhurat/special-session days; Laxmi Pujan is not treated as a full closure.",
            "A special-session announcement establishes an audit candidate, not proof the scheduled session actually occurred.",
            "Index dates require internal-date agreement and positive broad-index volume; mirror coverage is not a complete exchange calendar.",
            "Empty historical holiday API responses do not mean there were no holidays; unassessed weekends may hide special sessions.",
            "Trading observations are not actually listed/as-of membership, historical Nifty constituents or proof of equity asset class.",
            "Weekend observations can be instrument-restricted, not general common-stock sessions; thin weekend reports are exposed for review.",
            "Official delisting CSV is a bounded 2002-2020 list, stale after 2020-11-09, not all historical exits/suspensions; unmatched names are review items, not proven omissions.",
            "DB symbols whose observations end before cutoff are review candidates, not proven delistings; ends can reflect symbol changes, mergers, series changes or source gaps.",
            "Historical names are retained but identity continuity, symbol reuse, mergers, delisting cash proceeds and executable exit prices are not reconstructed.",
            "Evidence retrieval timestamps may be later than cutoff; this is a retrospective completeness audit, not a point-in-time announcement database.",
        ],
    }
    write_json(data / "reports" / "coverage-audit.json", report)
    (data / "reports" / "coverage-audit.md").write_text(markdown_report(report), encoding="utf-8")
    remediation = calendar_remediation_report(report)
    write_json(data / "reports" / "calendar-remediation.json", remediation)
    (data / "reports" / "calendar-remediation.md").write_text(calendar_remediation_markdown(remediation), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=data_root())
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--cutoff", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--fetch-sources", action="store_true")
    arguments = parser.parse_args()
    if arguments.fetch_sources:
        fetch_sources(arguments.data, arguments.start or date(2000, 1, 3), arguments.cutoff)
    report = audit_coverage(arguments.data, arguments.cutoff, arguments.start)
    print(json.dumps({"snapshot": report["snapshot"], "missing_date_counts": report["sessions"]["missing_date_counts"],
                      "within_observed_span_counts": report["sessions"]["within_observed_span_counts"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
