"""Alternative public sources for Nifty membership — classification and bounded import.

This module is deliberately separate from ``index_membership``. Nothing here
may be labelled ``dated_official_snapshot``: that label is reserved for
official NSE Indices documents whose own text states the membership date.
Alternative sources are kept in their own trust tiers so they can never be
silently mixed into the official snapshot series:

- ``official_archive``: a Wayback capture of an official NSE URL. The file
  itself is undated, so the only honest ``as_of`` is the *capture* date and
  the membership kind is ``wayback_capture_observed`` — an observation that
  the list looked this way at crawl time, not a document-stated close.
- ``secondary_editable``: Wikipedia. Editable by anyone; usable as a change
  *ledger* that must be reconciled to official records, never as a snapshot.
- ``third_party``: GitHub mirrors / community CSVs. Single-date, unlicensed
  or GPL-only code licences; data rights do not follow from code licences.
- ``exchange_current_only``: BSE/NSE pages that publish only today's list
  (or index-level series). No historical constituent depth.
- ``rejected``: paid / login sources and anything requiring an access bypass.

All lookups are exact-date-only, same spirit as ``index_membership``:
gaps are unknown, never filled.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

CUTOFF = date(2026, 11, 7)
INDEX_CODES = {"NIFTY50", "NIFTY500"}
SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&.\-]*\Z")
ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")
CAPTURE_TS = re.compile(r"\A\d{14}\Z")

TRUST_ORDER = {
    "official_archive": 1,
    "secondary_editable": 2,
    "third_party": 3,
    "exchange_current_only": 4,
    "rejected": 5,
}

ALT_KINDS = {"wayback_capture_observed", "third_party_single_date"}


@dataclass(frozen=True)
class AltMember:
    symbol: str
    company_name: str = ""
    isin: str = ""
    series: str = ""


@dataclass(frozen=True)
class AltSnapshot:
    index_code: str
    as_of: date
    members: tuple[AltMember, ...]
    metadata: dict


@dataclass(frozen=True)
class ChangeRow:
    excluded: str
    included: str
    effective_date: date
    reason: str = ""


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def classify_source(url: str) -> dict:
    """Best-effort trust classification for a source URL (never a licence check)."""
    host = (urlparse(url).hostname or "").lower()
    if host in {"web.archive.org"}:
        return {"trust": "official_archive", "note": "Wayback capture; original must be an official NSE URL."}
    if host in {"en.wikipedia.org", "wikipedia.org"}:
        return {"trust": "secondary_editable", "note": "Community-editable; reconcile to official records."}
    if host in {"github.com", "raw.githubusercontent.com", "api.github.com"}:
        return {"trust": "third_party", "note": "Code licence (if any) does not grant data rights."}
    if host in {"bseindia.com", "www.bseindia.com"}:
        return {"trust": "exchange_current_only", "note": "BSE publishes BSE indices, not Nifty history."}
    if host in {"nseindia.com", "www.nseindia.com", "nseindices.com", "www.niftyindices.com",
                "archives.nseindia.com", "nsearchives.nseindia.com"}:
        return {"trust": "official_current_only",
                "note": "Official but undated/current-only; not a dated snapshot."}
    if host in {"connect2nse.com", "www.connect2nse.com", "bloomberg.com", "factset.com", "spglobal.com"}:
        return {"trust": "rejected", "note": "Paid / login source; not scraped."}
    return {"trust": "third_party", "note": "Unknown host; treat as untrusted."}


def _validate_common(snapshot: AltSnapshot, cutoff: date = CUTOFF) -> None:
    meta = snapshot.metadata
    if snapshot.index_code not in INDEX_CODES:
        raise ValueError("unsupported index_code")
    if snapshot.as_of > cutoff:
        raise ValueError("snapshot is after cutoff")
    if meta.get("membership_kind") == "dated_official_snapshot":
        raise ValueError("alt snapshots must not claim dated_official_snapshot")
    if meta.get("membership_kind") not in ALT_KINDS:
        raise ValueError("unsupported alt membership_kind")
    if meta.get("as_of") != snapshot.as_of.isoformat():
        raise ValueError("as_of differs from metadata")
    if meta.get("index_code") != snapshot.index_code:
        raise ValueError("index_code differs from metadata")
    if not re.fullmatch(r"[0-9a-f]{64}", meta.get("source_sha256", "")):
        raise ValueError("source_sha256 is required")
    count = meta.get("expected_security_count")
    if type(count) is not int or count <= 0 or count != len(snapshot.members):
        raise ValueError("security count differs from explicitly declared count")
    try:
        retrieved = datetime.fromisoformat(meta["retrieved_at_utc"].replace("Z", "+00:00"))
        if retrieved.utcoffset() is None:
            raise ValueError("retrieved_at_utc needs a timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid retrieved_at_utc") from exc
    if retrieved.date() < snapshot.as_of:
        raise ValueError("retrieval precedes as_of")
    symbols, isins = set(), set()
    for member in snapshot.members:
        if not SYMBOL.fullmatch(member.symbol):
            raise ValueError(f"invalid symbol: {member.symbol!r}")
        if member.symbol in symbols:
            raise ValueError("duplicate symbol")
        symbols.add(member.symbol)
        if member.isin:
            if not ISIN.fullmatch(member.isin):
                raise ValueError("invalid ISIN")
            if member.isin in isins:
                raise ValueError("duplicate ISIN")
            isins.add(member.isin)


OFFICIAL_ARCHIVE_HOSTS = {"archives.nseindia.com", "www.niftyindices.com", "niftyindices.com"}


def import_wayback_csv(content: bytes, index_code: str, provenance: dict,
                       cutoff: date = CUTOFF) -> AltSnapshot:
    """Import an undated official constituent CSV via its Wayback capture date.

    ``as_of`` is the capture date, explicitly NOT a document-stated date.
    The manifest must carry ``capture_timestamp`` (14 digits), the
    ``original_url`` (official host) and the Wayback replay URL.
    """
    if provenance.get("sha256") != sha256(content):
        raise ValueError("source digest mismatch")
    capture = provenance.get("capture_timestamp", "")
    if not CAPTURE_TS.fullmatch(capture):
        raise ValueError("capture_timestamp (14 digits) is required")
    original = urlparse(provenance.get("original_url", ""))
    if original.scheme != "https" or (original.hostname or "") not in OFFICIAL_ARCHIVE_HOSTS:
        raise ValueError("original_url must be an official HTTPS NSE URL")
    as_of = datetime.strptime(capture, "%Y%m%d%H%M%S").date()
    text = content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    expected_header = {"Company Name", "Industry", "Symbol", "Series", "ISIN Code"}
    if set(rows and rows[0].keys() or []) != expected_header:
        raise ValueError("unexpected constituent CSV header")
    if not rows:
        raise ValueError("empty constituent CSV")
    members = []
    for row in rows:
        if any(row.get(key) is None for key in expected_header):
            raise ValueError("malformed constituent row")
        members.append(AltMember(row["Symbol"].strip(), row["Company Name"].strip(),
                                 row["ISIN Code"].strip(), row["Series"].strip()))
    for member in members:
        if not member.symbol or not member.company_name:
            raise ValueError("incomplete constituent row")
        if member.series and member.series not in ("EQ", "BE"):
            raise ValueError(f"unsupported series in constituent list: {member.series}")
    meta = {
        "index_code": index_code, "as_of": as_of.isoformat(),
        "membership_kind": "wayback_capture_observed",
        "as_of_basis": "wayback_capture_timestamp_not_document_date",
        "as_of_evidence": f"Wayback capture {capture} of {provenance['original_url']}; "
                          "file itself carries no date.",
        "expected_security_count": len(members),
        "source_url": provenance.get("replay_url") or provenance["original_url"],
        "original_url": provenance["original_url"],
        "capture_timestamp": capture,
        "source_sha256": sha256(content),
        "retrieved_at_utc": provenance["retrieved_at_utc"],
        "trust": "official_archive",
        "license_note": "NSE Indices data; Wayback capture does not grant redistribution rights.",
        "extraction": "Full CSV import; no symbols added/removed.",
    }
    snapshot = AltSnapshot(index_code, as_of, tuple(members), meta)
    _validate_common(snapshot, cutoff)
    return snapshot


def import_third_party_tickers(content: bytes, provenance: dict) -> list[dict]:
    """Parse a community ticker CSV (``TICKERS,Company`` with ``.NS``/``.BO``).

    Returns ledger rows preserving the raw ticker; the filename date is
    provenance only and is never promoted to a snapshot date here.
    """
    if provenance.get("sha256") != sha256(content):
        raise ValueError("source digest mismatch")
    text = content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or set(rows[0].keys()) < {"TICKERS", "Company"}:
        raise ValueError("unexpected ticker CSV header")
    out = []
    seen = set()
    for row in rows:
        raw = (row.get("TICKERS") or "").strip()
        company = (row.get("Company") or "").strip()
        if not raw or not company:
            raise ValueError("incomplete ticker row")
        bare = raw.split(".")[0].upper()
        if not SYMBOL.fullmatch(bare):
            raise ValueError(f"invalid ticker: {raw!r}")
        if bare in seen:
            raise ValueError("duplicate bare symbol")
        seen.add(bare)
        out.append({"raw_ticker": raw, "symbol": bare, "company": company})
    if not out:
        raise ValueError("empty ticker CSV")
    return out


def import_wikipedia_changes(rows: list[dict], provenance: dict) -> tuple[ChangeRow, ...]:
    """Import a Wikipedia index-changes table as an event *ledger* (not a snapshot).

    The NIFTY 50 table covers changes since 2005 only. Rows claiming an
    earlier effective date are rejected as out of table scope; a missing
    baseline is always rejected by :func:`ledger_to_snapshot`.
    """
    if not provenance.get("page_url") or not provenance.get("retrieved_at_utc"):
        raise ValueError("wikipedia provenance requires page_url and retrieved_at_utc")
    out = []
    for row in rows:
        try:
            when = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("change row needs an ISO effective date") from exc
        if when < date(2005, 1, 1):
            raise ValueError("change predates the table's 2005 scope")
        excluded, included = (row.get("excluded") or "").strip(), (row.get("included") or "").strip()
        if not excluded or not included or "\n" in excluded or "\n" in included:
            raise ValueError("change row needs excluded/included company names")
        if "[[ " in excluded or "]]" in excluded or "[[ " in included or "]]" in included:
            raise ValueError("change row must be plain company names, not wikilinks")
        out.append(ChangeRow(excluded, included, when, (row.get("reason") or "").strip()))
    if not out:
        raise ValueError("empty change ledger")
    if [r.effective_date for r in out] != sorted(r.effective_date for r in out):
        raise ValueError("change ledger must be in effective-date order")
    return tuple(out)


def ledger_to_snapshot(ledger: tuple[ChangeRow, ...], index_code: str,
                       baseline: AltSnapshot) -> AltSnapshot:
    """Reconstruct a later list from a baseline — only if a baseline is given.

    A change ledger without a verified baseline can never be turned into a
    snapshot.
    """
    if baseline is None:
        raise ValueError("no verified baseline: ledger alone cannot make a snapshot")
    members = {m.symbol: m for m in baseline.members}
    as_of = ledger[-1].effective_date if ledger else baseline.as_of
    for change in ledger:
        members.pop(change.excluded, None)
        if change.included in members:
            raise ValueError(f"duplicate included symbol: {change.included}")
        members[change.included] = AltMember(change.included)
    meta = dict(baseline.metadata,
                index_code=index_code, as_of=as_of.isoformat(),
                membership_kind="third_party_single_date",
                as_of_basis="ledger_reconstruction_from_baseline",
                as_of_evidence="Wikipedia change ledger applied to supplied baseline; "
                               "not an official dated snapshot.",
                expected_security_count=len(members),
                trust="secondary_editable")
    snapshot = AltSnapshot(index_code, as_of, tuple(members.values()), meta)
    _validate_common(snapshot)
    return snapshot


def import_wikipedia_current(rows: list[dict], provenance: dict) -> AltSnapshot:  # noqa: ARG001
    """A 'current constituents' table has no list as-of date: always reject."""
    raise ValueError(
        "wikipedia current-constituents table has no list as-of date; "
        "'date added' is a per-stock entry date, not a snapshot date")


def write_alt_snapshot(snapshot: AltSnapshot, directory: Path,
                       cutoff: date = CUTOFF) -> tuple[Path, Path]:
    _validate_common(snapshot, cutoff)
    stem = f"{snapshot.index_code}_{snapshot.as_of.isoformat()}"
    csv_path, manifest_path = directory / (stem + ".csv"), directory / (stem + ".json")
    if csv_path.exists() or manifest_path.exists():
        raise FileExistsError(stem)
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("symbol", "company_name", "isin", "series"))
    writer.writerows((m.symbol, m.company_name, m.isin, m.series) for m in snapshot.members)
    content = output.getvalue().encode("utf-8")
    metadata = dict(snapshot.metadata, snapshot_sha256=sha256(content))
    directory.mkdir(parents=True, exist_ok=True)
    with csv_path.open("xb") as target:
        target.write(content)
    with manifest_path.open("x", encoding="utf-8") as target:
        json.dump(metadata, target, indent=2)
        target.write("\n")
    return csv_path, manifest_path


def load_alt_snapshot(csv_path: Path, manifest_path: Path, source_path: Path,
                      cutoff: date = CUTOFF) -> AltSnapshot:
    meta = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = source_path.read_bytes()
    if sha256(source) != meta.get("source_sha256"):
        raise ValueError("source digest mismatch")
    content = csv_path.read_bytes()
    if sha256(content) != meta.get("snapshot_sha256"):
        raise ValueError("snapshot digest mismatch")
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    if reader.fieldnames != ["symbol", "company_name", "isin", "series"]:
        raise ValueError("unexpected snapshot columns")
    members = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("malformed snapshot row")
        members.append(AltMember(**row))
    snapshot = AltSnapshot(meta["index_code"], date.fromisoformat(meta["as_of"]),
                           tuple(members), meta)
    _validate_common(snapshot, cutoff)
    return snapshot


def members_on_alt(snapshots: list[AltSnapshot], index_code: str, day: date) -> tuple[AltMember, ...]:
    """Exact-date lookup only (capture dates); never fill gaps."""
    matches = [s for s in snapshots if s.index_code == index_code and s.as_of == day]
    if not matches:
        raise LookupError(f"unknown alt membership for {index_code} on {day}")
    if len(matches) != 1:
        raise ValueError("ambiguous duplicate alt snapshots")
    _validate_common(matches[0], cutoff=day)
    return matches[0].members


def diff_symbols(before: AltSnapshot, after: AltSnapshot) -> dict:
    a, b = {m.symbol for m in before.members}, {m.symbol for m in after.members}
    return {"added": sorted(b - a), "removed": sorted(a - b),
            "unchanged": sorted(a & b)}
