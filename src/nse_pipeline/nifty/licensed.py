"""Strict, offline import of licensed effective-date constituent intervals.

This is separate from public dated snapshots and from the research universe.
Coverage/licensing metadata is an external attestation, not authentication of a
vendor or a licence grant. No intervals, identifiers or historical members are
inferred. All date ranges, including coverage ranges, are half-open.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path


INDEX_CODES = frozenset({"NIFTY50", "NIFTY500"})
REQUIRED_COLUMNS = ("index", "symbol", "isin", "effective_from", "effective_to", "source")
OPTIONAL_COLUMNS = ("known_at", "security_type", "tradable")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9&.\-]*\Z")
ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")
SECURITY_TYPES = frozenset({"equity", "dvr", "dummy", "other"})


def _date(value: str | date, field: str) -> date:
    if type(value) is date:
        return value
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise ValueError(f"{field} must be strict ISO YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO date") from exc


def _timestamp(value: str | datetime, field: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
            raise ValueError(f"{field} must be an ISO timestamp with timezone")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be a valid ISO timestamp") from exc
    if result.utcoffset() is None:
        raise ValueError(f"{field} needs a timezone")
    return result.astimezone(timezone.utc)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be nonempty text without outer whitespace")
    return value


def _index(value: str) -> str:
    if not isinstance(value, str) or value not in INDEX_CODES:
        raise ValueError("index must be NIFTY50 or NIFTY500; map historic names explicitly")
    return value


@dataclass(frozen=True)
class MembershipInterval:
    index: str
    symbol: str
    isin: str
    effective_from: date
    effective_to: date | None
    source: str
    known_at: datetime | None = None
    security_type: str = "equity"
    tradable: bool = True


@dataclass(frozen=True)
class CoverageAttestation:
    index: str
    start: date
    end: date
    complete: bool
    reference: str


def _missing(start: date, end: date,
             spans: list[tuple[date, date]]) -> list[tuple[date, date]]:
    cursor = start
    missing = []
    for left, right in sorted(spans):
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            missing.append((cursor, left))
        cursor = max(cursor, min(right, end))
    if cursor < end:
        missing.append((cursor, end))
    return missing


@dataclass(frozen=True)
class LicensedHistory:
    members: tuple[MembershipInterval, ...]
    coverage: tuple[CoverageAttestation, ...]
    licence_reference: str
    source_sha256: str
    has_known_at: bool

    def coverage_report(self, start: str | date, end: str | date,
                        indices: tuple[str, ...] = ("NIFTY50", "NIFTY500")) -> dict:
        """Check effective-date coverage only, never historical knowability."""
        first, last = _date(start, "start"), _date(end, "end")
        if first >= last:
            raise ValueError("coverage start must be before end (exclusive)")
        if not indices or len(set(indices)) != len(indices):
            raise ValueError("indices must be nonempty and distinct")
        reports = {}
        for index in indices:
            _index(index)
            spans = [(c.start, c.end) for c in self.coverage
                     if c.index == index and c.complete]
            gaps = _missing(first, last, spans)
            reports[index] = {
                "complete": not gaps,
                "missing": [{"start": a.isoformat(), "end": b.isoformat()}
                            for a, b in gaps],
            }
        return {
            "start": first.isoformat(), "end": last.isoformat(),
            "interval_semantics": "[start,end)",
            "full_history": all(r["complete"] for r in reports.values()),
            "basis": "external licensed effective-coverage attestation; not authenticity proof",
            "point_in_time_completeness_proven": False,
            "indices": reports,
        }

    def require_full_history(self, start: str | date, end: str | date,
                             indices: tuple[str, ...] = ("NIFTY50", "NIFTY500")) -> dict:
        report = self.coverage_report(start, end, indices)
        if not report["full_history"]:
            raise ValueError("full-history claim rejected: missing licensed coverage: "
                             + json.dumps(report["indices"], sort_keys=True))
        return report

    def membership_asof(self, index: str, asof: str | date, *,
                        known_at: str | datetime | None = None,
                        tradable_only: bool = False,
                        require_complete: bool = True) -> tuple[MembershipInterval, ...]:
        """Return rows with from <= asof < to and, if requested, known_at <= cutoff.

        Without timestamps, a knowledge-cutoff query fails rather than treating
        an effective date or file-import time as an announcement timestamp.
        An empty filtered result does not prove an empty historical index.
        """
        _index(index)
        day = _date(asof, "asof")
        if require_complete and not any(c.index == index and c.complete
                                        and c.start <= day < c.end for c in self.coverage):
            raise ValueError(f"no complete licensed coverage for {index} on {day}")
        cutoff = _timestamp(known_at, "known_at") if known_at is not None else None
        if cutoff is not None and not self.has_known_at:
            raise ValueError("known_at query requires timestamps for every imported row")
        return tuple(row for row in self.members
                     if row.index == index and row.effective_from <= day
                     and (row.effective_to is None or day < row.effective_to)
                     and (cutoff is None or row.known_at <= cutoff)
                     and (not tradable_only or row.tradable))


def _coverage(metadata: dict) -> tuple[CoverageAttestation, ...]:
    raw = metadata.get("coverage")
    if not isinstance(raw, list):
        raise ValueError("metadata.coverage must be a list (empty means unproven coverage)")
    result = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {
                "index", "start", "end", "complete", "reference"}:
            raise ValueError("coverage fields must be index,start,end,complete,reference")
        index = _index(entry["index"])
        start, end = _date(entry["start"], "coverage.start"), _date(entry["end"], "coverage.end")
        if start >= end:
            raise ValueError("coverage start must be before end (exclusive)")
        if type(entry["complete"]) is not bool:
            raise ValueError("coverage.complete must be a boolean")
        reference = _text(entry["reference"], "coverage.reference")
        result.append(CoverageAttestation(index, start, end, entry["complete"], reference))
    for index in INDEX_CODES:
        spans = sorted((c.start, c.end) for c in result if c.index == index)
        if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise ValueError("overlapping or duplicate coverage attestations")
    return tuple(result)


def _member(raw: dict, has_known_at: bool) -> MembershipInterval:
    index = _index(raw["index"])
    symbol = _text(raw["symbol"], "symbol")
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("invalid symbol (contemporaneous exchange symbol required)")
    start = _date(raw["effective_from"], "effective_from")
    end = _date(raw["effective_to"], "effective_to") if raw["effective_to"] else None
    if end is not None and start >= end:
        raise ValueError("effective_from must be before effective_to (exclusive)")
    source = _text(raw["source"], "source")
    kind = raw.get("security_type", "equity")
    if kind not in SECURITY_TYPES:
        raise ValueError("security_type must be equity, dvr, dummy or other")
    tradable = raw.get("tradable", "true")
    if tradable not in ("true", "false"):
        raise ValueError("tradable must be literal true or false")
    if symbol.startswith("DUMMY") and kind != "dummy":
        raise ValueError("DUMMY symbol requires explicit dummy classification")
    if kind == "dummy" and tradable != "false":
        raise ValueError("dummy rows must be explicitly untradable")
    isin = raw["isin"]
    if not ISIN_RE.fullmatch(isin) and not (kind == "dummy" and isin == ""):
        raise ValueError("invalid or missing ISIN; only untradable dummy rows may omit it")
    known = _timestamp(raw["known_at"], "known_at") if has_known_at else None
    return MembershipInterval(index, symbol, isin, start, end, source,
                              known, kind, tradable == "true")


def _validate_intervals(members: tuple[MembershipInterval, ...]) -> None:
    seen = set()
    groups = defaultdict(list)
    for row in members:
        key = (row.index, row.symbol, row.isin, row.effective_from, row.effective_to)
        if key in seen:
            raise ValueError("duplicate membership interval (including different provenance)")
        seen.add(key)
        groups[(row.index, "symbol", row.symbol)].append(row)
        if row.isin:
            groups[(row.index, "isin", row.isin)].append(row)
    for key, rows in groups.items():
        ordered = sorted(rows, key=lambda r: r.effective_from)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.effective_to is None or previous.effective_to > current.effective_from:
                raise ValueError(f"overlapping membership intervals for {key}; "
                                 "resolve corrections externally, do not guess a version")


def import_licensed_csv(source: Path, metadata: dict) -> LicensedHistory:
    """Validate bytes against an external metadata manifest, then import atomically.

    Required manifest: licence_reference, source_sha256, membership_kind set to
    licensed_effective_intervals, interval_semantics set to [effective_from,effective_to),
    and coverage (possibly []). A checksum binds the attestation to exact bytes.
    No network, persistence, purchase, or catalogue promotion occurs here.
    """
    if not isinstance(metadata, dict):
        raise ValueError("external metadata must be an object")
    licence = _text(metadata.get("licence_reference"), "licence_reference")
    if metadata.get("membership_kind") != "licensed_effective_intervals":
        raise ValueError("membership_kind must be licensed_effective_intervals, not snapshots")
    if metadata.get("interval_semantics") != "[effective_from,effective_to)":
        raise ValueError("explicit half-open interval_semantics required")
    content = Path(source).read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if metadata.get("source_sha256") != digest:
        raise ValueError("source_sha256 mismatch or missing digest")
    coverage = _coverage(metadata)
    try:
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        fields = reader.fieldnames
        if fields is None or len(fields) != len(set(fields)):
            raise ValueError("missing or duplicate CSV header")
        if not set(REQUIRED_COLUMNS) <= set(fields) or not set(fields) <= set(
                REQUIRED_COLUMNS + OPTIONAL_COLUMNS):
            raise ValueError("CSV schema requires index,symbol,isin,effective_from,effective_to,source; "
                             "optional known_at,security_type,tradable only")
        has_known = "known_at" in fields
        imported = []
        for raw in reader:
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError(f"CSV line {reader.line_num}: column count mismatch")
            try:
                imported.append(_member(raw, has_known))
            except ValueError as exc:
                raise ValueError(f"CSV line {reader.line_num}: {exc}") from exc
    except (csv.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid UTF-8 CSV: {exc}") from exc
    if not imported:
        raise ValueError("licensed CSV has no constituent rows")
    members = tuple(sorted(imported, key=lambda r: (r.index, r.effective_from, r.symbol, r.isin)))
    _validate_intervals(members)
    for claim in coverage:
        if claim.complete:
            spans = [(r.effective_from, r.effective_to or claim.end) for r in members
                     if r.index == claim.index]
            if _missing(claim.start, claim.end, spans):
                raise ValueError(f"complete coverage attestation for {claim.index} "
                                 "has dates with no constituent rows")
    return LicensedHistory(members, coverage, licence, digest, has_known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("metadata", type=Path, help="external JSON attestation; no licence text needed")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default="2027-01-01", help="exclusive end; not an assertion of available data")
    parser.add_argument("--require-full-history", action="store_true")
    parser.add_argument("--index", choices=sorted(INDEX_CODES))
    parser.add_argument("--asof")
    parser.add_argument("--known-at")
    parser.add_argument("--tradable-only", action="store_true")
    args = parser.parse_args(argv)
    if bool(args.index) != bool(args.asof) or (args.known_at and not args.asof):
        parser.error("--index and --asof must be supplied together; --known-at requires them")
    try:
        history = import_licensed_csv(args.csv, json.loads(args.metadata.read_text(encoding="utf-8")))
        report = history.coverage_report(args.start, args.end)
        if args.require_full_history:
            history.require_full_history(args.start, args.end)
        output = {"rows": len(history.members), "licence_reference": history.licence_reference,
                  "source_sha256": history.source_sha256, "coverage": report}
        if args.asof:
            output["members"] = [asdict(row) for row in history.membership_asof(
                args.index, args.asof, known_at=args.known_at, tradable_only=args.tradable_only)]
        print(json.dumps(output, indent=2, default=str))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"licensed import rejected: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
