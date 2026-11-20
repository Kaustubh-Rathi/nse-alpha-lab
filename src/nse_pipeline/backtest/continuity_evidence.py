"""Typed binding of the Batch 8 documentary continuity ledgers.

The module loads reviewed evidence lanes, verifies every retained document
against its recorded byte size and SHA-256, cross-checks each boundary against
its catalogued primary document, and constructs the frozen dataclasses from
:mod:`nse_pipeline.backtest.continuity`.

Two real lanes are supported:

* ``nse_indices`` -- NSE Indices Limited press releases (the parent lane);
* ``company_filings`` -- NSE-filed company announcements (the history lane).

It is an integrity-bound evidence loader, not source authentication and not
owner approval. A lane may remain ``INCOMPLETE``; loading it never promotes a
contract, never fills an uncovered interval and never invents an identity edge.
A lane that claims a contract pass, a missing document, a hash mismatch, a
catalog/digest disagreement or an uncovered effective date is refused.

Both real lanes declare *screened* corpora. They prove dated edges, not the
absence of other edges between them, so ``ContinuityCoverage.complete`` is
false and :meth:`ContinuityEvidenceSet.require_coverage` fails closed even for
a range inside the declared proven interval.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType

from .accounting_types import EventTime, Phase
from .continuity import (
    ContinuityBoundary,
    ContinuityLedger,
    DocumentaryEvidence,
    admit_boundaries,
)

DEFAULT_ROOT = (Path(__file__).resolve().parents[3]
                / "verification" / "stage1" / "batch8" / "continuity")
HISTORY_ROOT = DEFAULT_ROOT / "history"
CONTRACT = "P0-CONTINUITY"
NON_PROMOTING_STATUSES = frozenset({"INCOMPLETE", "READY_FOR_OWNER_REVIEW"})
COMMITTED_RETENTION = "committed_lane"
EXTERNAL_RETENTION = "external_acquisition_cache"


class ContinuityEvidenceError(ValueError):
    """The supplied evidence lane is incomplete, inconsistent or unverifiable."""


class ContinuityCoverageUnknown(ContinuityEvidenceError):
    """The requested interval is not fully covered by complete owner-reviewable evidence."""


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContinuityEvidenceError(f"{name} must be a nonempty string")
    return value


def _date(value, name: str) -> date:
    if not isinstance(value, str):
        raise ContinuityEvidenceError(f"{name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ContinuityEvidenceError(f"{name} is not an ISO date: {value!r}") from exc


def _timestamp_date(value, name: str) -> date:
    if not isinstance(value, str):
        raise ContinuityEvidenceError(f"{name} must be an ISO timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContinuityEvidenceError(f"{name} is not an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ContinuityEvidenceError(f"{name} must declare a timezone: {value!r}")
    return parsed.date()


def _published(value, name: str, parser: str) -> date:
    if parser == "date":
        return _date(value, name)
    if parser == "timestamp":
        return _timestamp_date(value, name)
    raise ContinuityEvidenceError(f"unknown publication-date parser: {parser!r}")


def _digest(value, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ContinuityEvidenceError(f"{name} must be a 64-character SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ContinuityEvidenceError(f"{name} must be hexadecimal") from exc
    return value


def _object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ContinuityEvidenceError(f"{name} must be an object")
    return value


def _array(value, name: str) -> list:
    if not isinstance(value, list) or not value:
        raise ContinuityEvidenceError(f"{name} must be a nonempty array")
    return value


def _json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContinuityEvidenceError(f"evidence file is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ContinuityEvidenceError(f"evidence file is unreadable: {path}") from exc
    return _object(payload, str(path))


def _inside(root: Path, relative: str, name: str) -> Path:
    _text(relative, name)
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContinuityEvidenceError(f"{name} escapes the evidence lane: {relative!r}") from exc
    if not candidate.is_file():
        raise ContinuityEvidenceError(f"{name} is missing: {relative}")
    return candidate


def _verify_file(path: Path, *, digest: str, size: int, name: str) -> str:
    raw = path.read_bytes()
    actual = sha256(raw).hexdigest()
    if len(raw) != size:
        raise ContinuityEvidenceError(
            f"{name} byte size changed: {len(raw)} != recorded {size}")
    if actual != digest:
        raise ContinuityEvidenceError(
            f"{name} SHA-256 changed: {actual} != recorded {digest}")
    return actual


@dataclass(frozen=True)
class LaneProfile:
    """The schema and authority rules that one evidence lane is allowed to use."""

    name: str
    ledger_kind: str
    catalog_kind: str
    publisher_rule: str
    published_at_parser: str
    evidence_digest_field: str
    citation_source: str
    default_document_retention: str = COMMITTED_RETENTION
    require_hash_verified_flag: bool = True

    def __post_init__(self):
        if self.publisher_rule not in {"nse_indices", "company_filing"}:
            raise ContinuityEvidenceError(
                f"unknown publisher rule: {self.publisher_rule!r}")
        if self.published_at_parser not in {"date", "timestamp"}:
            raise ContinuityEvidenceError(
                f"unknown publication-date parser: {self.published_at_parser!r}")
        if self.evidence_digest_field not in {"content_sha256", "sha256"}:
            raise ContinuityEvidenceError(
                f"unknown evidence digest field: {self.evidence_digest_field!r}")
        if self.citation_source not in {"evidence", "boundary"}:
            raise ContinuityEvidenceError(
                f"unknown citation source: {self.citation_source!r}")
        if self.default_document_retention not in {
                COMMITTED_RETENTION, EXTERNAL_RETENTION}:
            raise ContinuityEvidenceError(
                f"unknown default retention: {self.default_document_retention!r}")


NSE_INDICES_PROFILE = LaneProfile(
    name="nse_indices",
    ledger_kind="nse_indices_continuity_boundary_ledger",
    catalog_kind="nse_indices_continuity_document_catalog",
    publisher_rule="nse_indices",
    published_at_parser="date",
    evidence_digest_field="content_sha256",
    citation_source="evidence",
)

COMPANY_FILINGS_PROFILE = LaneProfile(
    name="company_filings",
    ledger_kind="nse_company_announcement_continuity_boundary_ledger",
    catalog_kind="nse_company_announcement_document_manifest",
    publisher_rule="company_filing",
    published_at_parser="timestamp",
    evidence_digest_field="sha256",
    citation_source="boundary",
)

PROFILES = {
    profile.name: profile
    for profile in (NSE_INDICES_PROFILE, COMPANY_FILINGS_PROFILE)
}
PROFILES_BY_LEDGER_KIND = {
    profile.ledger_kind: profile for profile in PROFILES.values()
}


@dataclass(frozen=True)
class ContinuityCoverage:
    """The lane's declared proven interval, completeness and unproven intervals."""

    requested_interval: tuple[date, date]
    proven_interval: tuple[date, date]
    unproven_intervals: tuple[tuple[date, date], ...]
    event_kind_gaps: Mapping[str, str]
    complete: bool = False
    completeness_reason: str = ""

    def __post_init__(self):
        first, last = self.requested_interval
        proven_first, proven_last = self.proven_interval
        if first > last:
            raise ContinuityEvidenceError("requested continuity interval is reversed")
        if not (first <= proven_first <= proven_last <= last):
            raise ContinuityEvidenceError("proven interval lies outside the requested interval")
        for gap_first, gap_last in self.unproven_intervals:
            if gap_first > gap_last:
                raise ContinuityEvidenceError("an unproven interval is reversed")
        for gap_first, gap_last in self.unproven_intervals:
            if gap_first <= proven_last and proven_first <= gap_last:
                raise ContinuityEvidenceError(
                    "an unproven interval overlaps the declared proven interval")
        if type(self.complete) is not bool:
            raise ContinuityEvidenceError("coverage completeness must be a boolean")
        if not isinstance(self.completeness_reason, str):
            raise ContinuityEvidenceError("coverage completeness reason must be text")

    def covers(self, first: date, last: date) -> bool:
        """Interval arithmetic only; completeness is enforced by ``require_coverage``."""
        if type(first) is not date or type(last) is not date or first > last:
            return False
        proven_first, proven_last = self.proven_interval
        if not (proven_first <= first and last <= proven_last):
            return False
        return not any(gap_first <= last and first <= gap_last
                       for gap_first, gap_last in self.unproven_intervals)


@dataclass(frozen=True)
class ContinuityEvidenceSet:
    """A verified, non-promoting documentary ledger plus its declared coverage."""

    root: Path
    status: str
    coverage: ContinuityCoverage
    boundaries: tuple[ContinuityBoundary, ...]
    document_digests: Mapping[str, str]
    profile_name: str = "nse_indices"

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "boundaries", tuple(self.boundaries))
        object.__setattr__(self, "document_digests",
                           MappingProxyType(dict(self.document_digests)))
        if self.status not in NON_PROMOTING_STATUSES:
            raise ContinuityEvidenceError(
                f"continuity lane status {self.status!r} is not a non-promoting review state")
        if not self.boundaries:
            raise ContinuityEvidenceError("continuity evidence contains no boundaries")
        if self.profile_name not in PROFILES:
            raise ContinuityEvidenceError(f"unknown lane profile: {self.profile_name!r}")

    def ledger_at(self, decision_at: EventTime) -> ContinuityLedger:
        """Admit only evidence available at ``decision_at``; future documents vanish."""
        return admit_boundaries(self.boundaries, decision_at=decision_at)

    def require_coverage(self, first: date, last: date) -> ContinuityCoverage:
        """Fail closed unless the whole range is a *complete* proven interval."""
        first, last = _ordered_bounds(first, last)
        if not self.coverage.complete:
            reason = self.coverage.completeness_reason or "the corpus is a screened subset"
            raise ContinuityCoverageUnknown(
                f"continuity coverage is not complete for {first.isoformat()}..{last.isoformat()}: "
                f"{reason}")
        if not self.coverage.covers(first, last):
            raise ContinuityCoverageUnknown(
                f"continuity coverage is not proven for {first.isoformat()}..{last.isoformat()}")
        return self.coverage

    def boundaries_effective_between(self, first: date, last: date) -> tuple[ContinuityBoundary, ...]:
        """Evidence rows whose effective date is inside the range; availability is not implied."""
        first, last = _ordered_bounds(first, last)
        rows = (row for row in self.boundaries if first <= row.effective_on <= last)
        return tuple(sorted(rows, key=lambda row: (row.effective_on, row.boundary_id)))


@dataclass(frozen=True)
class ContinuityEvidenceCollection:
    """Two independently verified lanes merged into one non-promoting ledger view."""

    lanes: tuple[ContinuityEvidenceSet, ...]
    boundaries: tuple[ContinuityBoundary, ...]
    document_digests: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "lanes", tuple(self.lanes))
        object.__setattr__(self, "boundaries", tuple(self.boundaries))
        object.__setattr__(self, "document_digests",
                           MappingProxyType(dict(self.document_digests)))
        if not self.lanes:
            raise ContinuityEvidenceError("a combined ledger needs at least one lane")
        if not self.boundaries:
            raise ContinuityEvidenceError("combined continuity evidence contains no boundaries")

    @property
    def statuses(self) -> tuple[str, ...]:
        return tuple(lane.status for lane in self.lanes)

    def ledger_at(self, decision_at: EventTime) -> ContinuityLedger:
        return admit_boundaries(self.boundaries, decision_at=decision_at)

    def boundaries_effective_between(self, first: date, last: date) -> tuple[ContinuityBoundary, ...]:
        first, last = _ordered_bounds(first, last)
        rows = (row for row in self.boundaries if first <= row.effective_on <= last)
        return tuple(sorted(rows, key=lambda row: (row.effective_on, row.boundary_id)))

    def require_coverage(self, first: date, last: date) -> tuple[ContinuityCoverage, ...]:
        """Require at least one *complete* lane to cover the whole range."""
        first, last = _ordered_bounds(first, last)
        matches = tuple(
            lane.coverage for lane in self.lanes
            if lane.coverage.complete and lane.coverage.covers(first, last))
        if not matches:
            raise ContinuityCoverageUnknown(
                f"no complete continuity lane covers {first.isoformat()}..{last.isoformat()}")
        return matches


def _ordered_bounds(first, last) -> tuple[date, date]:
    if type(first) is not date or type(last) is not date:
        raise ContinuityEvidenceError("coverage bounds must be dates")
    if first > last:
        raise ContinuityEvidenceError("coverage bounds are reversed")
    return first, last


def _profile(profile: str | LaneProfile | None, ledger_payload: dict) -> LaneProfile:
    if isinstance(profile, LaneProfile):
        return profile
    if isinstance(profile, str):
        try:
            return PROFILES[profile]
        except KeyError as exc:
            raise ContinuityEvidenceError(f"unknown lane profile: {profile!r}") from exc
    if profile is not None:
        raise ContinuityEvidenceError("profile must be a LaneProfile, a profile name or None")
    kind = ledger_payload.get("kind")
    try:
        return PROFILES_BY_LEDGER_KIND[kind]
    except KeyError as exc:
        raise ContinuityEvidenceError(
            f"unrecognised continuity ledger kind: {kind!r}") from exc


def _check_publisher(record: dict, document_id: str, profile: LaneProfile) -> str:
    publisher = _text(record.get("publisher"), f"{document_id}.publisher")
    if profile.publisher_rule == "nse_indices":
        if publisher != "NSE Indices Limited":
            raise ContinuityEvidenceError(
                f"{document_id} publisher {publisher!r} is outside the declared authority")
        return publisher
    role = _text(record.get("publisher_role"), f"{document_id}.publisher_role")
    venue = _text(record.get("venue"), f"{document_id}.venue")
    if role != "NSE-listed company filing":
        raise ContinuityEvidenceError(
            f"{document_id} publisher role {role!r} is outside the declared authority")
    if "National Stock Exchange of India" not in venue:
        raise ContinuityEvidenceError(
            f"{document_id} venue {venue!r} is outside the declared authority")
    return publisher


def _load_documents(root: Path, payload: dict, profile: LaneProfile) -> dict[str, dict]:
    if payload.get("kind") != profile.catalog_kind:
        raise ContinuityEvidenceError(
            f"document catalog kind must be {profile.catalog_kind!r}")
    if payload.get("contract") != CONTRACT:
        raise ContinuityEvidenceError(f"document catalog contract must be {CONTRACT!r}")
    if payload.get("contract_pass_claimed") is True:
        raise ContinuityEvidenceError("document catalog must not claim a contract pass")
    records = _array(payload.get("documents"), "documents")
    by_id: dict[str, dict] = {}
    for index, raw in enumerate(records):
        record = _object(raw, f"documents[{index}]")
        document_id = _text(record.get("document_id"), f"documents[{index}].document_id")
        if document_id in by_id:
            raise ContinuityEvidenceError(f"duplicate document id: {document_id!r}")
        _check_publisher(record, document_id, profile)
        published_at = _published(record.get("published_at"),
                                  f"{document_id}.published_at",
                                  profile.published_at_parser)
        digest = _digest(record.get("sha256"), f"{document_id}.sha256")
        size = record.get("bytes")
        if type(size) is not int or size < 0:
            raise ContinuityEvidenceError(f"{document_id}.bytes must be a nonnegative integer")
        retention = record.get("retention") or profile.default_document_retention
        if retention == COMMITTED_RETENTION:
            local_path = _inside(root, _text(record.get("local_path"),
                                             f"{document_id}.local_path"),
                                 f"{document_id}.local_path")
            _verify_file(local_path, digest=digest, size=size, name=document_id)
            if profile.require_hash_verified_flag and record.get("hash_verified_on_disk") is not True:
                raise ContinuityEvidenceError(
                    f"{document_id} does not declare a verified on-disk digest")
            stored_path = local_path
        elif retention == EXTERNAL_RETENTION:
            if record.get("local_path"):
                raise ContinuityEvidenceError(
                    f"{document_id} is external but still declares a lane-local path")
            _text(record.get("cache_path"), f"{document_id}.cache_path")
            stored_path = None
        else:
            raise ContinuityEvidenceError(
                f"{document_id} has an unknown retention policy: {retention!r}")
        record = dict(record)
        record["published_at"] = published_at
        record["retention"] = retention
        record["local_path"] = stored_path
        by_id[document_id] = record
    return by_id


def _availability(value: dict, name: str) -> EventTime:
    available_at = _object(value, name)
    try:
        return EventTime(
            session=_date(available_at.get("session"), f"{name}.session"),
            phase=Phase(available_at.get("phase")),
            sequence=available_at.get("sequence", 0))
    except (ValueError, TypeError) as exc:
        raise ContinuityEvidenceError(
            f"{name} availability time is not a typed EventTime") from exc


def _citation(row: dict, evidence: dict, profile: LaneProfile, boundary_id: str) -> str:
    if profile.citation_source == "evidence":
        return _text(evidence.get("citation"), f"{boundary_id}.citation")
    return _text(row.get("citation"), f"{boundary_id}.citation")


def _load_boundaries(payload: dict, documents: Mapping[str, dict],
                     profile: LaneProfile) -> tuple[ContinuityBoundary, ...]:
    if payload.get("kind") != profile.ledger_kind:
        raise ContinuityEvidenceError(
            f"boundary ledger kind must be {profile.ledger_kind!r}")
    if payload.get("contract") != CONTRACT:
        raise ContinuityEvidenceError(f"boundary ledger contract must be {CONTRACT!r}")
    if payload.get("contract_pass_claimed") is not False:
        raise ContinuityEvidenceError("continuity lane must not claim a contract pass")
    status = _text(payload.get("status"), "status")
    if status not in NON_PROMOTING_STATUSES:
        raise ContinuityEvidenceError(
            f"continuity lane status {status!r} is not a non-promoting review state")

    records = _array(payload.get("boundaries"), "boundaries")
    boundaries: list[ContinuityBoundary] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, date]] = set()
    for index, raw in enumerate(records):
        row = _object(raw, f"boundaries[{index}]")
        boundary_id = _text(row.get("boundary_id"), f"boundaries[{index}].boundary_id")
        if boundary_id in seen_ids:
            raise ContinuityEvidenceError(f"duplicate boundary id: {boundary_id!r}")
        seen_ids.add(boundary_id)
        evidence = _object(row.get("evidence"), f"{boundary_id}.evidence")
        document_id = _text(evidence.get("document_id"), f"{boundary_id}.document_id")
        if document_id not in documents:
            raise ContinuityEvidenceError(
                f"{boundary_id} cites an uncatalogued document: {document_id!r}")
        document = documents[document_id]
        if document["retention"] != COMMITTED_RETENTION:
            raise ContinuityEvidenceError(
                f"{boundary_id} cites a document that is not retained in the lane")
        content_digest = _digest(evidence.get(profile.evidence_digest_field),
                                 f"{boundary_id}.{profile.evidence_digest_field}")
        if content_digest != document["sha256"]:
            raise ContinuityEvidenceError(
                f"{boundary_id} evidence digest disagrees with the document catalog")
        if boundary_id not in (document.get("primary_boundary_ids") or ()):
            raise ContinuityEvidenceError(
                f"{document_id} does not bind {boundary_id} as a primary boundary")
        published_at = _published(evidence.get("published_at"),
                                  f"{boundary_id}.published_at",
                                  profile.published_at_parser)
        if published_at != document["published_at"]:
            raise ContinuityEvidenceError(
                f"{boundary_id} publication date disagrees with the document catalog")
        evidence_publisher = evidence.get("publisher")
        if evidence_publisher is not None and evidence_publisher != document["publisher"]:
            raise ContinuityEvidenceError(
                f"{boundary_id} publisher disagrees with the document catalog")
        evidence_venue = evidence.get("venue")
        if evidence_venue is not None and evidence_venue != document.get("venue"):
            raise ContinuityEvidenceError(
                f"{boundary_id} venue disagrees with the document catalog")
        primary = row.get("primary_citation")
        if primary is not None:
            primary = _object(primary, f"{boundary_id}.primary_citation")
            if primary.get("document_id") != document_id:
                raise ContinuityEvidenceError(
                    f"{boundary_id} primary citation names a different document")
            if profile.published_at_parser == "timestamp" and primary.get("published_at"):
                if _published(primary.get("published_at"),
                              f"{boundary_id}.primary_citation.published_at",
                              profile.published_at_parser) != published_at:
                    raise ContinuityEvidenceError(
                        f"{boundary_id} primary citation publication disagrees")
        decision_time = _availability(evidence.get("available_at"),
                                      f"{boundary_id}.available_at")
        try:
            documentary = DocumentaryEvidence(
                publisher=_text(evidence.get("publisher"), f"{boundary_id}.publisher"),
                document_id=document_id,
                published_at=published_at,
                available_at=decision_time,
                effective_from=_date(evidence.get("effective_from"),
                                     f"{boundary_id}.effective_from"),
                effective_to=(None if evidence.get("effective_to") is None
                              else _date(evidence.get("effective_to"),
                                         f"{boundary_id}.effective_to")),
                citation=_citation(row, evidence, profile, boundary_id),
                content_sha256=content_digest,
            )
            boundary = ContinuityBoundary(
                boundary_id=boundary_id,
                source_key=_text(row.get("source_key"), f"{boundary_id}.source_key"),
                successor_key=_text(row.get("successor_key"), f"{boundary_id}.successor_key"),
                event_kind=_text(row.get("event_kind"), f"{boundary_id}.event_kind"),
                effective_on=_date(row.get("effective_on"), f"{boundary_id}.effective_on"),
                evidence=documentary,
            )
        except ValueError as exc:
            raise ContinuityEvidenceError(
                f"{boundary_id} is not admissible documentary evidence: {exc}") from exc
        edge = (boundary.source_key, boundary.successor_key, boundary.effective_on)
        if edge in seen_edges:
            raise ContinuityEvidenceError(f"duplicate continuity edge: {boundary_id!r}")
        seen_edges.add(edge)
        boundaries.append(boundary)
    return tuple(boundaries)


def _load_coverage(payload: dict) -> ContinuityCoverage:
    scope = _object(payload.get("scope"), "scope")
    requested = _object(scope.get("requested_interval"), "scope.requested_interval")
    requested_first = _date(requested.get("from"), "scope.requested_interval.from")
    requested_last = _date(requested.get("to"), "scope.requested_interval.to")
    coverage = _object(payload.get("coverage"), "coverage")
    complete = coverage.get("complete")
    if complete is None:
        complete = False
    if type(complete) is not bool:
        raise ContinuityEvidenceError("coverage.complete must be a boolean")
    completeness_reason = coverage.get("completeness_reason") or ""
    if not isinstance(completeness_reason, str):
        raise ContinuityEvidenceError("coverage.completeness_reason must be text")
    proven_first = _date(coverage.get("proven_effective_from"),
                         "coverage.proven_effective_from")
    proven_last = _date(coverage.get("proven_effective_to"),
                        "coverage.proven_effective_to")
    gaps = []
    for index, raw in enumerate(coverage.get("unproven_intervals") or ()):
        gap = _object(raw, f"coverage.unproven_intervals[{index}]")
        gaps.append((_date(gap.get("from"), f"gap[{index}].from"),
                     _date(gap.get("to"), f"gap[{index}].to")))
    kinds = {}
    for name, reason in (coverage.get("event_kind_gaps") or {}).items():
        kinds[_text(name, "event_kind_gaps key")] = _text(reason, f"event_kind_gaps[{name}]")
    return ContinuityCoverage(
        requested_interval=(requested_first, requested_last),
        proven_interval=(proven_first, proven_last),
        unproven_intervals=tuple(gaps),
        event_kind_gaps=MappingProxyType(kinds),
        complete=complete,
        completeness_reason=completeness_reason,
    )


def load_continuity_evidence(
        root: Path | str | None = None, *,
        profile: str | LaneProfile | None = None) -> ContinuityEvidenceSet:
    """Load and verify one Batch 8 continuity lane without promoting its status."""
    lane = Path(root) if root is not None else DEFAULT_ROOT
    if not lane.is_dir():
        raise ContinuityEvidenceError(f"continuity evidence lane is missing: {lane}")
    lane = lane.resolve()
    boundary_payload = _json(lane / "boundaries.json")
    selected = _profile(profile, boundary_payload)
    document_payload = _json(lane / "documents.json")
    documents = _load_documents(lane, document_payload, selected)
    boundaries = _load_boundaries(boundary_payload, documents, selected)
    coverage = _load_coverage(boundary_payload)
    for boundary in boundaries:
        if not coverage.covers(boundary.effective_on, boundary.effective_on):
            raise ContinuityEvidenceError(
                f"{boundary.boundary_id} lies outside the declared proven interval")
    return ContinuityEvidenceSet(
        root=lane,
        status=_text(boundary_payload.get("status"), "status"),
        coverage=coverage,
        boundaries=boundaries,
        document_digests={document_id: document["sha256"]
                          for document_id, document in documents.items()},
        profile_name=selected.name,
    )


def load_continuity_evidence_many(
        roots: Sequence[Path | str | None] | None = None, *,
        profiles: Sequence[str | LaneProfile | None] | None = None,
) -> ContinuityEvidenceCollection:
    """Load and merge the parent and history lanes, refusing cross-lane conflicts."""
    selected_roots = tuple(roots) if roots is not None else (DEFAULT_ROOT, HISTORY_ROOT)
    if profiles is not None and len(profiles) != len(selected_roots):
        raise ContinuityEvidenceError("profiles must align one-for-one with roots")
    selected_profiles = (tuple(profiles) if profiles is not None
                         else (None,) * len(selected_roots))
    lanes = tuple(
        load_continuity_evidence(root, profile=profile)
        for root, profile in zip(selected_roots, selected_profiles))
    boundaries: list[ContinuityBoundary] = []
    seen_ids: set[str] = set()
    seen_edges: set[tuple[str, str, date]] = set()
    digests: dict[str, str] = {}
    for lane in lanes:
        for boundary in lane.boundaries:
            if boundary.boundary_id in seen_ids:
                raise ContinuityEvidenceError(
                    f"duplicate boundary id across lanes: {boundary.boundary_id!r}")
            seen_ids.add(boundary.boundary_id)
            edge = (boundary.source_key, boundary.successor_key, boundary.effective_on)
            if edge in seen_edges:
                raise ContinuityEvidenceError(
                    f"duplicate continuity edge across lanes: {boundary.boundary_id!r}")
            seen_edges.add(edge)
            boundaries.append(boundary)
        for document_id, digest in lane.document_digests.items():
            previous = digests.get(document_id)
            if previous is not None and previous != digest:
                raise ContinuityEvidenceError(
                    f"document {document_id!r} has conflicting digests across lanes")
            digests[document_id] = digest
    return ContinuityEvidenceCollection(
        lanes=lanes,
        boundaries=tuple(sorted(boundaries,
                                key=lambda row: (row.effective_on, row.boundary_id))),
        document_digests=digests,
    )
