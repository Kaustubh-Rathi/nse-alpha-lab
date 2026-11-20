"""Documentary continuity between security identities.

Continuity is never inferred from a similar symbol, ISIN, name or price path.
Only an explicit dated documentary boundary with a publisher, document ID,
citation and content digest can connect a source identity to a successor.
Source and successor keys remain separately addressable at all times.

A boundary is admissible at a decision only when its ``available_at`` is no
later than that decision and its documentary effective interval covers the
queried date. Conflicts and cycles are reported and excluded; they never
silently choose an edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .accounting_types import EventTime, identifier, sha256_digest

CONTINUITY_EVENT_KINDS = (
    "symbol_change", "isin_change", "merger", "demerger", "successor",
    "scheme_of_arrangement",
)
# A composite scheme can combine a merger and a demerger. It may therefore
# legitimately have more than one successor and more than one predecessor,
# unlike a plain merger (one successor) or a plain demerger (one source).
MULTI_SUCCESSOR_KINDS = frozenset({"demerger", "scheme_of_arrangement"})
MULTI_PREDECESSOR_KINDS = frozenset({"merger", "scheme_of_arrangement"})


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _date(value, name):
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")
    return value


@dataclass(frozen=True)
class DocumentaryEvidence:
    """One dated document proving a corporate-action boundary."""

    publisher: str
    document_id: str
    published_at: date
    available_at: EventTime
    effective_from: date
    effective_to: date | None
    citation: str
    content_sha256: str

    def __post_init__(self):
        _text(self.publisher, "publisher")
        _text(self.document_id, "document_id")
        _text(self.citation, "citation")
        _date(self.published_at, "published_at")
        _date(self.effective_from, "effective_from")
        if not isinstance(self.available_at, EventTime):
            raise ValueError("available_at must be a typed EventTime")
        if self.published_at > self.available_at.session:
            raise ValueError("A document cannot become available before it was published")
        if self.effective_to is not None:
            _date(self.effective_to, "effective_to")
            if self.effective_to < self.effective_from:
                raise ValueError("effective_to cannot precede effective_from")
        sha256_digest(self.content_sha256, "content_sha256")

    def covers(self, on: date) -> bool:
        _date(on, "query date")
        return (self.effective_from <= on
                and (self.effective_to is None or on <= self.effective_to))

    def available(self, decision_at: EventTime) -> bool:
        if not isinstance(decision_at, EventTime):
            raise ValueError("decision_at must be a typed EventTime")
        return self.available_at <= decision_at


@dataclass(frozen=True)
class ContinuityBoundary:
    boundary_id: str
    source_key: str
    successor_key: str
    event_kind: str
    effective_on: date
    evidence: DocumentaryEvidence

    def __post_init__(self):
        identifier(self.boundary_id)
        identifier(self.source_key)
        identifier(self.successor_key)
        if self.source_key == self.successor_key:
            raise ValueError("Continuity requires two separately preserved identities")
        if self.event_kind not in CONTINUITY_EVENT_KINDS:
            raise ValueError(f"Unknown continuity event kind: {self.event_kind!r}")
        _date(self.effective_on, "effective_on")
        if not isinstance(self.evidence, DocumentaryEvidence):
            raise ValueError("Continuity requires typed documentary evidence")
        if not self.evidence.covers(self.effective_on):
            raise ValueError("The documentary effective interval does not cover the boundary date")


@dataclass(frozen=True)
class ContinuityRecord:
    boundary_id: str
    source_key: str
    successor_key: str
    event_kind: str
    effective_on: date
    evidence: DocumentaryEvidence


@dataclass(frozen=True)
class ContinuityIssue:
    reason: str
    boundary_ids: tuple[str, ...]
    keys: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "boundary_ids", tuple(self.boundary_ids))
        object.__setattr__(self, "keys", tuple(sorted(set(self.keys))))
        if not self.reason or not self.boundary_ids:
            raise ValueError("A continuity issue needs a reason and affected boundary IDs")


@dataclass(frozen=True)
class ContinuityResolution:
    source_key: str
    successor_key: str
    on: date
    record: ContinuityRecord | None
    reason: str

    @property
    def admitted(self) -> bool:
        return self.record is not None


@dataclass(frozen=True)
class ContinuityLedger:
    decision_at: EventTime
    records: tuple[ContinuityRecord, ...]
    issues: tuple[ContinuityIssue, ...]
    excluded_count: int = 0

    def resolve(self, source_key, successor_key, on) -> ContinuityResolution:
        identifier(source_key)
        identifier(successor_key)
        _date(on, "query date")
        conflicting = any(
            (source_key in issue.keys or successor_key in issue.keys)
            for issue in self.issues)
        matches = tuple(record for record in self.records
                        if record.source_key == source_key and record.successor_key == successor_key)
        if len(matches) > 1:
            return ContinuityResolution(source_key, successor_key, on, None, "conflicting_evidence")
        if not matches:
            # A document that was not available at decision_at must leave no
            # trace at all; otherwise its existence changes a frozen answer.
            reason = "conflicting_evidence" if conflicting else "no_admitted_continuity"
            return ContinuityResolution(source_key, successor_key, on, None, reason)
        record = matches[0]
        if on < record.effective_on:
            return ContinuityResolution(source_key, successor_key, on, None, "effective_in_future")
        if not record.evidence.covers(on):
            return ContinuityResolution(source_key, successor_key, on, None,
                                        "outside_effective_interval")
        return ContinuityResolution(source_key, successor_key, on, record, "admitted")

    def require(self, source_key, successor_key, on) -> ContinuityRecord:
        resolution = self.resolve(source_key, successor_key, on)
        if resolution.record is None:
            raise ValueError(resolution.reason)
        return resolution.record


def _cycles(boundaries, graph):
    """Boundary IDs that participate in a directed cycle."""
    involved = []
    for boundary in boundaries:
        if _reachable(graph, boundary.successor_key, boundary.source_key):
            involved.append(boundary.boundary_id)
    return tuple(involved)


def _reachable(graph, start, target):
    stack, seen = [start], set()
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(graph.get(node, ()))
    return False


def admit_boundaries(boundaries, *, decision_at: EventTime) -> ContinuityLedger:
    """Admit only boundaries available at ``decision_at``; report conflicts, never guess."""
    if not isinstance(decision_at, EventTime):
        raise ValueError("decision_at must be a typed EventTime")
    boundaries = tuple(boundaries)
    if not all(isinstance(boundary, ContinuityBoundary) for boundary in boundaries):
        raise ValueError("Continuity evidence must use typed boundaries")
    ids = [boundary.boundary_id for boundary in boundaries]
    if len(set(ids)) != len(ids):
        raise ValueError("Continuity boundary IDs must be unique")

    available = tuple(boundary for boundary in boundaries
                      if boundary.evidence.available_at <= decision_at)
    issues = []
    excluded = set()

    # One source cannot silently become two different successors. A demerger
    # is the explicit exception: one source legitimately yields multiple
    # successors, each with its own documentary boundary.
    by_source = {}
    for boundary in available:
        by_source.setdefault(boundary.source_key, []).append(boundary)
    for source, group in by_source.items():
        successors = {boundary.successor_key for boundary in group}
        if (len(successors) > 1
                and any(boundary.event_kind not in MULTI_SUCCESSOR_KINDS
                        for boundary in group)):
            ids_here = tuple(sorted(boundary.boundary_id for boundary in group))
            issues.append(ContinuityIssue("ambiguous_successor", ids_here, (source,)))
            excluded.update(ids_here)

    # Conversely, a non-merger event cannot silently claim multiple sources.
    by_successor = {}
    for boundary in available:
        by_successor.setdefault(boundary.successor_key, []).append(boundary)
    for successor, group in by_successor.items():
        sources = {boundary.source_key for boundary in group}
        if (len(sources) > 1
                and any(boundary.event_kind not in MULTI_PREDECESSOR_KINDS
                        for boundary in group)):
            ids_here = tuple(sorted(boundary.boundary_id for boundary in group))
            issues.append(ContinuityIssue("ambiguous_predecessor", ids_here, (successor,)))
            excluded.update(ids_here)

    # A duplicated directed edge is not two independent proofs.
    pairs = {}
    for boundary in available:
        pairs.setdefault((boundary.source_key, boundary.successor_key), []).append(boundary)
    for (source, successor), group in pairs.items():
        if len(group) > 1:
            ids_here = tuple(sorted(boundary.boundary_id for boundary in group))
            issues.append(ContinuityIssue("duplicate_boundary", ids_here, (source, successor)))
            excluded.update(ids_here)

    surviving = tuple(boundary for boundary in available if boundary.boundary_id not in excluded)
    graph = {}
    for boundary in surviving:
        graph.setdefault(boundary.source_key, set()).add(boundary.successor_key)
    cycle_ids = _cycles(surviving, graph)
    if cycle_ids:
        cycle = set(cycle_ids)
        keys = tuple(sorted({key for boundary in surviving if boundary.boundary_id in cycle
                             for key in (boundary.source_key, boundary.successor_key)}))
        issues.append(ContinuityIssue("continuity_cycle", tuple(sorted(cycle)), keys))
        excluded.update(cycle)

    records = tuple(
        ContinuityRecord(boundary.boundary_id, boundary.source_key, boundary.successor_key,
                         boundary.event_kind, boundary.effective_on, boundary.evidence)
        for boundary in sorted(surviving, key=lambda row: (row.effective_on, row.boundary_id))
        if boundary.boundary_id not in excluded)
    return ContinuityLedger(decision_at, records, tuple(issues), len(excluded))
