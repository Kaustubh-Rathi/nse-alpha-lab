"""Integrity-bound loader for the NIFTY 500 membership evidence lanes.

The lanes' ``anchors.json`` and ``reconstruction.json`` are reviewed evidence
claims, not owner approval. Loading verifies every retained snapshot and sidecar
against its recorded byte size and SHA-256, cross-checks the reconstruction
digests and symbol sets against those anchors, and constructs the typed interval
port. It never creates a between-snapshot interval, never consults current
constituents, never forward-fills a snapshot and never promotes a status.

The default :func:`load_membership_evidence` remains the Batch 8 lane. Batch 9
(post-2022 point evidence) is available through
:func:`load_batch9_membership_evidence`, and both lanes can be joined at their
byte-identical 2022-03-31 boundary through
:func:`load_merged_membership_evidence`. Merging is only a concatenation of
one-day anchors: gaps between anchors remain ``CoverageUnknown``.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.nifty.membership_intervals import (
    CHANGE_NOTICE_EVIDENCE,
    SNAPSHOT_EVIDENCE,
    IntervalMembershipEvidence,
    MembershipCoverageWindow,
    MembershipInterval,
)
from nse_pipeline.nifty.snapshots import SYMBOL

DEFAULT_ROOT = (Path(__file__).resolve().parents[3]
                / "verification" / "stage1" / "batch8" / "membership")
BATCH9_ROOT = (Path(__file__).resolve().parents[3]
               / "verification" / "stage1" / "batch9" / "universe")
CONTRACT = "P0-UNIVERSE"
REVIEW_STATUS = "READY_FOR_OWNER_REVIEW"
EXPECTED_ANCHOR_COUNT = 108
EXPECTED_WINDOW_COUNT = 108
EXPECTED_INTERVAL_COUNT = 54_072
BATCH9_STATUS = "FINAL_EVIDENCE"
BATCH9_EXPECTED_ANCHOR_COUNT = 65
BATCH9_EXPECTED_WINDOW_COUNT = 65
BATCH9_EXPECTED_INTERVAL_COUNT = 32_670
EVIDENCE_PHASE = Phase.PRE_OPEN_EVIDENCE


@dataclass(frozen=True)
class _LaneSpec:
    """The reviewed counts and evidence classes admitted from one lane."""

    label: str
    root: Path
    status: str
    anchor_count: int
    window_count: int
    interval_count: int
    evidence_kinds: frozenset[str]


def _batch8_spec(root: Path | str | None = None) -> _LaneSpec:
    return _LaneSpec(
        "batch8",
        Path(root) if root is not None else DEFAULT_ROOT,
        REVIEW_STATUS,
        EXPECTED_ANCHOR_COUNT,
        EXPECTED_WINDOW_COUNT,
        EXPECTED_INTERVAL_COUNT,
        frozenset({SNAPSHOT_EVIDENCE}),
    )


def _batch9_spec(root: Path | str | None = None) -> _LaneSpec:
    return _LaneSpec(
        "batch9",
        Path(root) if root is not None else BATCH9_ROOT,
        BATCH9_STATUS,
        BATCH9_EXPECTED_ANCHOR_COUNT,
        BATCH9_EXPECTED_WINDOW_COUNT,
        BATCH9_EXPECTED_INTERVAL_COUNT,
        frozenset({SNAPSHOT_EVIDENCE, CHANGE_NOTICE_EVIDENCE}),
    )


class MembershipEvidenceError(ValueError):
    """The membership evidence lane is absent, inconsistent or unverifiable."""


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MembershipEvidenceError(f"{name} must be a nonempty string")
    return value


def _object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise MembershipEvidenceError(f"{name} must be an object")
    return value


def _array(value, name: str) -> list:
    if not isinstance(value, list) or not value:
        raise MembershipEvidenceError(f"{name} must be a nonempty array")
    return value


def _date(value, name: str) -> date:
    if not isinstance(value, str):
        raise MembershipEvidenceError(f"{name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise MembershipEvidenceError(f"{name} is not an ISO date: {value!r}") from exc


def _digest(value, name: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise MembershipEvidenceError(
            f"{name} must be a lowercase 64-character SHA-256 hex digest")
    return value


def _integer(value, name: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        raise MembershipEvidenceError(f"{name} must be an integer >= {minimum}")
    return value


def _json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MembershipEvidenceError(f"evidence file is missing: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MembershipEvidenceError(f"evidence file is unreadable: {path}") from exc
    return _object(payload, str(path))


def _json_bytes(raw: bytes, name: str) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MembershipEvidenceError(f"{name} is not readable JSON") from exc
    return _object(payload, name)


def _inside(root: Path, relative, name: str) -> Path:
    relative = _text(relative, name)
    candidate = Path(relative)
    if candidate.is_absolute():
        raise MembershipEvidenceError(f"{name} must be relative to the evidence lane")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MembershipEvidenceError(
            f"{name} escapes the evidence lane: {relative!r}") from exc
    if not resolved.is_file():
        raise MembershipEvidenceError(f"{name} is missing: {relative}")
    return resolved


def _verify_file(path: Path, *, size: int, digest: str, name: str) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise MembershipEvidenceError(f"{name} is unreadable: {path}") from exc
    if len(raw) != size:
        raise MembershipEvidenceError(
            f"{name} byte size changed: {len(raw)} != recorded {size}")
    actual = hashlib.sha256(raw).hexdigest()
    if actual != digest:
        raise MembershipEvidenceError(
            f"{name} SHA-256 changed: {actual} != recorded {digest}")
    return raw


def _event_time(value, name: str) -> EventTime:
    record = _object(value, name)
    session_value = record.get("session", record.get("date"))
    session = _date(session_value, f"{name}.session")
    if record.get("date") is not None and _date(record["date"], f"{name}.date") != session:
        raise MembershipEvidenceError(f"{name}.date and {name}.session disagree")
    phase = record.get("phase")
    if type(phase) is not int or phase != EVIDENCE_PHASE:
        raise MembershipEvidenceError(
            f"{name}.phase must be {int(EVIDENCE_PHASE)}")
    sequence = record.get("sequence", 0)
    try:
        return EventTime(session, EVIDENCE_PHASE, sequence)
    except (TypeError, ValueError) as exc:
        raise MembershipEvidenceError(f"{name} is not a typed EventTime") from exc


def _check_review(payload: dict, name: str, spec: _LaneSpec) -> None:
    if payload.get("contract") != CONTRACT:
        raise MembershipEvidenceError(f"{name} contract must be {CONTRACT!r}")
    if payload.get("status") != spec.status:
        raise MembershipEvidenceError(
            f"{name} status {payload.get('status')!r} is not {spec.status!r}")
    if payload.get("contract_pass_claimed") is not False:
        raise MembershipEvidenceError(f"{name} must not claim a contract pass")


def _load_anchors(lane: Path, spec: _LaneSpec) -> dict[date, dict]:
    payload = _json(lane / "anchors.json")
    _check_review(payload, "anchors.json", spec)
    records = _array(payload.get("snapshot_anchors"), "anchors.json.snapshot_anchors")
    if len(records) != spec.anchor_count:
        raise MembershipEvidenceError(
            f"{spec.label} anchors.json must contain exactly "
            f"{spec.anchor_count} snapshot anchors")

    anchors: dict[date, dict] = {}
    for index, raw in enumerate(records):
        name = f"snapshot_anchors[{index}]"
        record = _object(raw, name)
        as_of = _date(record.get("as_of"), f"{name}.as_of")
        if as_of in anchors:
            raise MembershipEvidenceError(f"duplicate anchor date: {as_of.isoformat()}")

        digest = _digest(record.get("snapshot_sha256"), f"{name}.snapshot_sha256")
        csv_alias = record.get("csv_sha256")
        if csv_alias is not None and _digest(csv_alias, f"{name}.csv_sha256") != digest:
            raise MembershipEvidenceError(
                f"{name} snapshot_sha256 and csv_sha256 disagree")
        csv_size = _integer(record.get("csv_bytes"), f"{name}.csv_bytes", positive=True)
        sidecar_digest = _digest(record.get("manifest_sha256"), f"{name}.manifest_sha256")
        sidecar_size = _integer(record.get("manifest_bytes"), f"{name}.manifest_bytes",
                                positive=True)

        csv_path = _inside(lane, record.get("retained_csv_path"),
                           f"{name}.retained_csv_path")
        sidecar_path = _inside(lane, record.get("retained_sidecar_path"),
                               f"{name}.retained_sidecar_path")
        csv_raw = _verify_file(csv_path, size=csv_size, digest=digest,
                               name=f"{name} retained CSV")
        sidecar_raw = _verify_file(sidecar_path, size=sidecar_size, digest=sidecar_digest,
                                   name=f"{name} retained sidecar")
        sidecar = _json_bytes(sidecar_raw, f"{name} retained sidecar")
        declared = _digest(sidecar.get("snapshot_sha256"), f"{name}.sidecar.snapshot_sha256")
        if declared != digest:
            raise MembershipEvidenceError(
                f"{name} sidecar declares a different snapshot digest")
        if sidecar.get("as_of") != as_of.isoformat():
            raise MembershipEvidenceError(f"{name} sidecar as_of disagrees with the anchor")
        if sidecar.get("index_code") != "NIFTY500":
            raise MembershipEvidenceError(f"{name} sidecar is not NIFTY500 evidence")

        reader = csv.DictReader(io.StringIO(csv_raw.decode("utf-8-sig"), newline=""))
        if not reader.fieldnames or "symbol" not in reader.fieldnames:
            raise MembershipEvidenceError(f"{name} retained CSV has no symbol column")
        symbols: list[str] = []
        for row in reader:
            symbol = row.get("symbol")
            if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
                raise MembershipEvidenceError(
                    f"{name} retained CSV contains an invalid symbol: {symbol!r}")
            symbols.append(symbol)
        if len(set(symbols)) != len(symbols):
            raise MembershipEvidenceError(f"{name} retained CSV contains duplicate symbols")
        counted = _integer(record.get("row_count"), f"{name}.row_count", positive=True)
        unique = _integer(record.get("unique_symbol_count"),
                          f"{name}.unique_symbol_count", positive=True)
        if counted != len(symbols):
            raise MembershipEvidenceError(
                f"{name} row count changed: {len(symbols)} != recorded {counted}")
        if unique != len(set(symbols)):
            raise MembershipEvidenceError(
                f"{name} unique-symbol count changed: {len(set(symbols))} != recorded {unique}")
        expected = _integer(sidecar.get("expected_security_count"),
                            f"{name}.sidecar.expected_security_count", positive=True)
        if expected != len(symbols):
            raise MembershipEvidenceError(
                f"{name} sidecar security count disagrees with the retained CSV")

        anchors[as_of] = {
            "digest": digest,
            "symbols": frozenset(symbols),
            "row_count": counted,
        }
    return anchors


def _load_reconstruction(lane: Path, anchors: dict[date, dict], spec: _LaneSpec) -> tuple[
        list[MembershipInterval], list[MembershipCoverageWindow], dict[str, str]]:
    payload = _json(lane / "reconstruction.json")
    _check_review(payload, "reconstruction.json", spec)
    interval_records = _array(payload.get("intervals"), "reconstruction.json.intervals")
    window_records = _array(payload.get("coverage_windows"),
                            "reconstruction.json.coverage_windows")
    if len(interval_records) != spec.interval_count:
        raise MembershipEvidenceError(
            f"{spec.label} reconstruction.json must contain exactly "
            f"{spec.interval_count} intervals")
    if len(window_records) != spec.window_count:
        raise MembershipEvidenceError(
            f"{spec.label} reconstruction.json must contain exactly "
            f"{spec.window_count} windows")

    key_to_symbol: dict[str, str] = {}
    symbols: set[str] = set()
    mapping = _object(payload.get("key_to_symbol"), "reconstruction.json.key_to_symbol")
    for key, symbol in mapping.items():
        key = _text(key, "key_to_symbol key")
        if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
            raise MembershipEvidenceError(
                f"key_to_symbol contains an invalid symbol: {symbol!r}")
        if symbol in symbols:
            raise MembershipEvidenceError("key_to_symbol symbol collision")
        key_to_symbol[key] = symbol
        symbols.add(symbol)
    if not key_to_symbol:
        raise MembershipEvidenceError("reconstruction.json key_to_symbol is empty")

    windows_by_date: dict[date, dict] = {}
    for index, raw in enumerate(window_records):
        name = f"coverage_windows[{index}]"
        record = _object(raw, name)
        first = _date(record.get("effective_from"), f"{name}.effective_from")
        last = _date(record.get("effective_to"), f"{name}.effective_to")
        if first != last:
            raise MembershipEvidenceError(f"{name} is not a one-day window")
        if first not in anchors:
            raise MembershipEvidenceError(f"{name} date is not an anchor: {first.isoformat()}")
        if first in windows_by_date:
            raise MembershipEvidenceError(f"duplicate coverage window date: {first.isoformat()}")
        available = _event_time(record.get("available_at"), f"{name}.available_at")
        if available.session <= first:
            raise MembershipEvidenceError(
                f"{name} availability is not later than the snapshot date")
        digest = _digest(record.get("source_sha256"), f"{name}.source_sha256")
        if digest != anchors[first]["digest"]:
            raise MembershipEvidenceError(
                f"{name} digest disagrees with its anchor")
        windows_by_date[first] = {
            "available_at": available,
            "digest": digest,
            "citation": _text(record.get("citation"), f"{name}.citation"),
        }
    if len(windows_by_date) != spec.window_count:
        raise MembershipEvidenceError("coverage window dates are not unique")
    if set(windows_by_date) != set(anchors):
        raise MembershipEvidenceError("coverage windows do not match the anchor dates")

    intervals: list[MembershipInterval] = []
    seen: set[tuple[str, date]] = set()
    interval_dates: set[date] = set()
    symbols_by_date: dict[date, set[str]] = defaultdict(set)
    for index, raw in enumerate(interval_records):
        name = f"intervals[{index}]"
        record = _object(raw, name)
        first = _date(record.get("effective_from"), f"{name}.effective_from")
        last = _date(record.get("effective_to"), f"{name}.effective_to")
        if first != last:
            raise MembershipEvidenceError(f"{name} is not a one-day interval")
        if first not in anchors:
            raise MembershipEvidenceError(f"{name} date is not an anchor: {first.isoformat()}")
        symbol = _text(record.get("symbol"), f"{name}.symbol")
        if not SYMBOL.fullmatch(symbol):
            raise MembershipEvidenceError(f"{name} contains an invalid symbol")
        if symbol not in symbols:
            raise MembershipEvidenceError(
                f"{name} symbol is absent from key_to_symbol")
        identity = (symbol, first)
        if identity in seen:
            raise MembershipEvidenceError(
                f"duplicate interval for {symbol} on {first.isoformat()}")
        seen.add(identity)
        available = _event_time(record.get("available_at"), f"{name}.available_at")
        if available != windows_by_date[first]["available_at"]:
            raise MembershipEvidenceError(
                f"{name} availability disagrees with its coverage window")
        digest = _digest(record.get("source_sha256"), f"{name}.source_sha256")
        if digest != anchors[first]["digest"]:
            raise MembershipEvidenceError(f"{name} digest disagrees with its anchor")
        evidence_kind = _text(record.get("evidence_kind"), f"{name}.evidence_kind")
        if evidence_kind not in spec.evidence_kinds:
            raise MembershipEvidenceError(
                f"{name} evidence kind {evidence_kind!r} is not admitted by "
                f"the {spec.label} lane")
        intervals.append(MembershipInterval(
            symbol, first, last, available,
            _text(record.get("citation"), f"{name}.citation"),
            digest, evidence_kind))
        interval_dates.add(first)
        symbols_by_date[first].add(symbol)
    if interval_dates != set(anchors):
        raise MembershipEvidenceError("intervals do not cover every anchor date")
    for on, date_symbols in symbols_by_date.items():
        if date_symbols != anchors[on]["symbols"]:
            raise MembershipEvidenceError(
                f"intervals on {on.isoformat()} do not match the retained snapshot symbols")

    windows = [MembershipCoverageWindow(
        on, on, data["available_at"], data["citation"], data["digest"])
        for on, data in sorted(windows_by_date.items())]
    return intervals, windows, key_to_symbol


def _load_parts(spec: _LaneSpec) -> tuple[
        dict[date, dict], list[MembershipInterval],
        list[MembershipCoverageWindow], dict[str, str]]:
    lane = spec.root
    if not lane.is_dir():
        raise MembershipEvidenceError(f"membership evidence lane is missing: {lane}")
    lane = lane.resolve()
    anchors = _load_anchors(lane, spec)
    intervals, windows, key_to_symbol = _load_reconstruction(lane, anchors, spec)
    return anchors, intervals, windows, key_to_symbol


def _port(parts: tuple[dict[date, dict], list[MembershipInterval],
                       list[MembershipCoverageWindow], dict[str, str]],
          *, decision_at: EventTime) -> IntervalMembershipEvidence:
    if not isinstance(decision_at, EventTime):
        raise MembershipEvidenceError("decision_at must be an explicit EventTime")
    _, intervals, windows, key_to_symbol = parts
    first_available = min(window.available_at for window in windows)
    if decision_at < first_available:
        raise MembershipEvidenceError(
            f"decision_at precedes the first membership availability "
            f"{first_available.session.isoformat()}")
    try:
        return IntervalMembershipEvidence(
            intervals, windows, key_to_symbol, decision_at=decision_at)
    except ValueError as exc:
        raise MembershipEvidenceError(
            f"membership evidence is not admissible: {exc}") from exc


def load_membership_evidence(
        root: Path | str | None = None, *,
        decision_at: EventTime) -> IntervalMembershipEvidence:
    """Load and verify the Batch 8 membership lane.

    This is the original default. Batch 8 evidence is published constituent
    snapshots only; the loader refuses derived change-notice intervals in this
    lane. Evidence whose availability is later than ``decision_at`` is
    constructed but remains invisible to the returned port; a decision before
    the first availability is refused because it can see no evidence at all.
    """
    return _port(_load_parts(_batch8_spec(root)), decision_at=decision_at)


def load_batch9_membership_evidence(
        root: Path | str | None = None, *,
        decision_at: EventTime) -> IntervalMembershipEvidence:
    """Load and verify the Batch 9 post-2022 point-evidence lane.

    Batch 9 admits both the copied 2022-03-31 snapshot and derived one-day
    change-notice intervals. Every anchor's retained CSV, sidecar, digest,
    symbol set and availability is checked. Between-anchor dates remain
    ``CoverageUnknown``; this loader never fills them.
    """
    return _port(_load_parts(_batch9_spec(root)), decision_at=decision_at)


def load_merged_membership_evidence(
        batch8_root: Path | str | None = None,
        batch9_root: Path | str | None = None, *,
        decision_at: EventTime) -> IntervalMembershipEvidence:
    """Load both lanes and join them at their shared 2022-03-31 boundary.

    The boundary snapshot must be byte-identical in both lanes (same digest and
    symbol set), and the boundary interval/window must agree on availability.
    Only that one day is de-duplicated. Every other date remains a one-day
    anchor, so gaps and post-chain dates stay ``CoverageUnknown``.
    """
    batch8 = _load_parts(_batch8_spec(batch8_root))
    batch9 = _load_parts(_batch9_spec(batch9_root))
    anchors8, intervals8, windows8, mapping8 = batch8
    anchors9, intervals9, windows9, mapping9 = batch9

    shared = set(anchors8) & set(anchors9)
    boundary8, boundary9 = max(anchors8), min(anchors9)
    if shared != {boundary8} or boundary8 != boundary9:
        raise MembershipEvidenceError(
            "membership lanes must share exactly one boundary anchor")
    if anchors8[boundary8]["digest"] != anchors9[boundary9]["digest"]:
        raise MembershipEvidenceError(
            "membership lane boundary snapshots have different digests")
    if anchors8[boundary8]["symbols"] != anchors9[boundary9]["symbols"]:
        raise MembershipEvidenceError(
            "membership lane boundary snapshots have different symbol sets")

    windows_by_date: dict[date, MembershipCoverageWindow] = {
        window.effective_from: window for window in windows8}
    for window in windows9:
        existing = windows_by_date.get(window.effective_from)
        if existing is not None:
            if (existing.effective_to != window.effective_to
                    or existing.available_at != window.available_at
                    or existing.source_sha256 != window.source_sha256):
                raise MembershipEvidenceError(
                    f"membership lane windows disagree on {window.effective_from}")
            continue
        windows_by_date[window.effective_from] = window

    intervals_by_identity: dict[
        tuple[str, date, date], MembershipInterval] = {}
    for interval in intervals8:
        intervals_by_identity[
            (interval.symbol, interval.effective_from, interval.effective_to)] = interval
    for interval in intervals9:
        identity = (interval.symbol, interval.effective_from, interval.effective_to)
        existing = intervals_by_identity.get(identity)
        if existing is not None:
            if (existing.available_at != interval.available_at
                    or existing.source_sha256 != interval.source_sha256
                    or existing.evidence_kind != interval.evidence_kind):
                raise MembershipEvidenceError(
                    f"membership lane intervals disagree on {identity}")
            continue
        intervals_by_identity[identity] = interval

    key_to_symbol = dict(mapping8)
    for key, symbol in mapping9.items():
        current = key_to_symbol.get(key)
        if current is not None and current != symbol:
            raise MembershipEvidenceError(
                f"membership lane key mapping disagrees on {key!r}")
        key_to_symbol[key] = symbol

    merged = (
        {**anchors8, **anchors9},
        list(intervals_by_identity.values()),
        list(windows_by_date.values()),
        key_to_symbol,
    )
    return _port(merged, decision_at=decision_at)
