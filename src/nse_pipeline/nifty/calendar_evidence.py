"""Load dated NSE/Nifty Indices calendar evidence into the published adapter.

The evidence file is a reconciliation artifact, not authority by itself. This
loader makes every classification traceable to a dated, digest-bound source and
refuses to turn a retrospective artifact copy into contemporaneous session
knowledge. A month-specific ``CalendarSource`` is admissible at a month-end only
when an explicit owner-acceptance record, bound to the evidence file digest,
accepts the index calculation dates as contemporaneous session evidence.

Without that record the loader still exposes the retrospective diagnostic
calendar, but ``monthly_calendars`` refuses to emit a production calendar.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path

from nse_pipeline.backtest.accounting_types import EventTime, Phase, sha256_digest
from nse_pipeline.backtest.boundary import KnownCalendar
from nse_pipeline.nifty.published_calendar import (
    HOLIDAY_LIST, SESSION_SERIES, SPECIAL_SESSION_NOTICE,
    CalendarSource, PublishedExchangeCalendar,
)

EVIDENCE_KIND = "nse_capital_market_session_evidence"
EVIDENCE_STATUS = "READY_FOR_OWNER_REVIEW"
ACCEPTANCE_KIND = "nse_calendar_owner_acceptance"
CONTEMPORANEOUS_SESSION_SOURCE = "nifty_indices_daily_calculation_dates"


class CalendarEvidenceError(ValueError):
    """Calendar evidence is missing, inconsistent or not bound to its artifacts."""


class OwnerAcceptanceRequired(CalendarEvidenceError):
    """Monthly calendars need a digest-bound owner acceptance record."""


class OwnerAcceptanceInvalid(CalendarEvidenceError):
    """The owner acceptance record is forged, stale or incomplete."""


def _date(value, name: str) -> date:
    if type(value) is not date:
        raise CalendarEvidenceError(f"{name} must be a date")
    return value


def _parse_date(value, name: str) -> date:
    if not isinstance(value, str):
        raise CalendarEvidenceError(f"{name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CalendarEvidenceError(f"{name} is not an ISO date: {value!r}") from None


def _parse_datetime(value, name: str) -> datetime:
    if not isinstance(value, str):
        raise CalendarEvidenceError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CalendarEvidenceError(f"{name} is not an ISO timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        raise CalendarEvidenceError(f"{name} must include a timezone")
    return parsed


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalendarEvidenceError(f"{name} must be a nonempty string")
    return value


def _sequence(value, name: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise CalendarEvidenceError(f"{name} must be a collection")
    return tuple(value)


def _mapping(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise CalendarEvidenceError(f"{name} must be an object")
    return value


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


@dataclass(frozen=True)
class CitationEvidence:
    """One dated document backing a closure or special-session classification."""

    document_id: str
    publication_date: date
    artifact: str
    artifact_sha256: str
    citation_strength: str | None = None
    title: str | None = None

    def __post_init__(self):
        _text(self.document_id, "document_id")
        _date(self.publication_date, "publication_date")
        _text(self.artifact, "artifact")
        sha256_digest(self.artifact_sha256, "artifact_sha256")


@dataclass(frozen=True)
class ClosureEvidence:
    on: date
    kind: str
    citation: CitationEvidence

    def __post_init__(self):
        _date(self.on, "closure date")
        _text(self.kind, "closure kind")


@dataclass(frozen=True)
class SpecialSessionEvidence:
    on: date
    session_type: str
    citation: CitationEvidence

    def __post_init__(self):
        _date(self.on, "special-session date")
        _text(self.session_type, "session_type")


@dataclass(frozen=True)
class IndexSeriesEvidence:
    index_name: str
    artifact: str
    artifact_sha256: str

    def __post_init__(self):
        _text(self.index_name, "index_name")
        _text(self.artifact, "artifact")
        sha256_digest(self.artifact_sha256, "artifact_sha256")


@dataclass(frozen=True)
class CalendarYearEvidence:
    year: int
    sessions: tuple[date, ...]
    closures: tuple[ClosureEvidence, ...]
    special_sessions: tuple[SpecialSessionEvidence, ...]
    complete_months: tuple[tuple[int, int], ...]
    index_series: tuple[IndexSeriesEvidence, ...]

    def __post_init__(self):
        if self.year < 1:
            raise CalendarEvidenceError("year must be positive")
        if not self.index_series:
            raise CalendarEvidenceError(f"{self.year} has no index-series artifact")


@dataclass(frozen=True)
class ContradictionEvidence:
    on: date
    kind: str
    resolved: bool
    resolution: str | None
    resolving_document: str | None


@dataclass(frozen=True)
class PublishedCalendarEvidence:
    """Parsed, hash-bound calendar evidence with an explicit availability basis."""

    path: Path
    file_sha256: str
    generated_at: datetime
    contract_coverage_end: date
    coverage_end: date
    years: tuple[CalendarYearEvidence, ...]
    contradictions: tuple[ContradictionEvidence, ...]
    artifacts: tuple[tuple[str, str], ...]
    retrospective_at: EventTime
    retrospective_basis: str

    def year(self, value: int) -> CalendarYearEvidence:
        for row in self.years:
            if row.year == value:
                return row
        raise CalendarEvidenceError(f"no evidence for year {value}")

    @property
    def complete_months(self) -> tuple[tuple[int, int], ...]:
        return tuple(month for row in self.years for month in row.complete_months)

    def verify_artifacts(self, root: str | Path | None = None) -> tuple[str, ...]:
        """Verify every referenced artifact against its recorded SHA-256."""
        base = Path(root) if root is not None else self.path.parent
        checked = []
        for relative, expected in self.artifacts:
            path = base / relative
            if not path.is_file():
                raise CalendarEvidenceError(f"missing calendar artifact: {path}")
            actual = sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise CalendarEvidenceError(
                    f"calendar artifact hash mismatch for {relative}: {actual} != {expected}")
            checked.append(relative)
        return tuple(checked)


@dataclass(frozen=True)
class CalendarOwnerAcceptance:
    """Owner sign-off needed before retrospective index dates can fire monthly."""

    accepted_by: str
    accepted_at: date
    evidence_sha256: str
    session_source: str
    accepts_msd_notices: bool
    accepts_revised_muhurat_citation: bool
    coverage_end: date

    def __post_init__(self):
        _text(self.accepted_by, "accepted_by")
        _date(self.accepted_at, "accepted_at")
        sha256_digest(self.evidence_sha256, "evidence_sha256")
        _text(self.session_source, "session_source")
        _date(self.coverage_end, "coverage_end")


def load_published_calendar_evidence(
        path: str | Path, *, provenance_path: str | Path | None = None
) -> PublishedCalendarEvidence:
    """Parse and validate a dated calendar evidence manifest.

    When ``provenance_path`` is supplied, the retrospective availability is the
    latest recorded index-series retrieval, and every index artifact must match
    its logged digest. Otherwise the manifest's own ``generated_at`` is the
    conservative availability floor.
    """
    evidence_path = Path(path)
    try:
        raw = evidence_path.read_bytes()
    except OSError as exc:
        raise CalendarEvidenceError(f"cannot read calendar evidence: {exc}") from None
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise CalendarEvidenceError(f"calendar evidence is not JSON: {exc}") from None
    payload = _mapping(payload, "calendar evidence")

    if payload.get("schema_version") != 1:
        raise CalendarEvidenceError("unsupported calendar evidence schema version")
    if payload.get("kind") != EVIDENCE_KIND:
        raise CalendarEvidenceError(f"unexpected evidence kind: {payload.get('kind')!r}")
    if payload.get("contract") != "P0-BOUNDARY":
        raise CalendarEvidenceError("calendar evidence is not bound to P0-BOUNDARY")
    if payload.get("status") != EVIDENCE_STATUS:
        raise CalendarEvidenceError(
            f"calendar evidence status must be {EVIDENCE_STATUS!r}, "
            f"not {payload.get('status')!r}")

    generated_at = _parse_datetime(payload.get("generated_at"), "generated_at")
    coverage = _mapping(payload.get("coverage"), "coverage")
    contract_end = _parse_date(coverage.get("contract_coverage_end"), "contract_coverage_end")
    evidence_end = _parse_date(coverage.get("coverage_end"), "coverage_end")
    if contract_end > evidence_end:
        raise CalendarEvidenceError("contract coverage cannot exceed evidence coverage")

    artifacts: dict[str, str] = {}

    def remember(relative, digest, name):
        _text(relative, f"{name} artifact")
        sha256_digest(digest, f"{name} artifact_sha256")
        previous = artifacts.get(relative)
        if previous is not None and previous != digest:
            raise CalendarEvidenceError(
                f"artifact {relative} is bound to two different digests")
        artifacts[relative] = digest

    years = []
    raw_years = _mapping(payload.get("years"), "years")
    if not raw_years:
        raise CalendarEvidenceError("calendar evidence has no years")
    for key, raw_year in sorted(raw_years.items()):
        try:
            year_value = int(key)
        except (TypeError, ValueError):
            raise CalendarEvidenceError(f"invalid year key: {key!r}") from None
        year = _mapping(raw_year, f"year {year_value}")

        if year.get("index_date_sets_identical") is not True:
            raise CalendarEvidenceError(
                f"{year_value}: the NIFTY 500 and NIFTY 50 date sets disagree")

        raw_sessions = _sequence(year.get("session_dates"), f"{year_value} session_dates")
        sessions = tuple(_parse_date(value, f"{year_value} session") for value in raw_sessions)
        if tuple(sorted(set(sessions))) != sessions:
            raise CalendarEvidenceError(f"{year_value}: sessions must be sorted and unique")
        if year.get("session_count") != len(sessions):
            raise CalendarEvidenceError(f"{year_value}: session_count does not match the list")
        if year.get("first_covered_date") is not None:
            if _parse_date(year["first_covered_date"], "first_covered_date") != sessions[0]:
                raise CalendarEvidenceError(f"{year_value}: first_covered_date is inconsistent")
        if year.get("last_covered_date") is not None:
            if _parse_date(year["last_covered_date"], "last_covered_date") != sessions[-1]:
                raise CalendarEvidenceError(f"{year_value}: last_covered_date is inconsistent")

        closures = []
        for row in _sequence(year.get("closure_dates"), f"{year_value} closure_dates"):
            row = _mapping(row, "closure")
            citation = _mapping(row.get("citation"), "closure citation")
            closures.append(ClosureEvidence(
                on=_parse_date(row.get("date"), "closure date"),
                kind=_text(row.get("kind"), "closure kind"),
                citation=CitationEvidence(
                    document_id=_text(citation.get("document_id"), "closure document_id"),
                    publication_date=_parse_date(
                        citation.get("publication_date"), "closure publication_date"),
                    artifact=_text(citation.get("artifact"), "closure artifact"),
                    artifact_sha256=_text(
                        citation.get("artifact_sha256"), "closure artifact_sha256"),
                    citation_strength=citation.get("citation_strength"),
                    title=row.get("description") if isinstance(row.get("description"), str) else None,
                )))
        if len({row.on for row in closures}) != len(closures):
            raise CalendarEvidenceError(f"{year_value}: closures must be unique")
        closures.sort(key=lambda row: row.on)

        specials = []
        for row in _sequence(year.get("special_sessions"), f"{year_value} special_sessions"):
            row = _mapping(row, "special session")
            specials.append(SpecialSessionEvidence(
                on=_parse_date(row.get("date"), "special-session date"),
                session_type=_text(row.get("session_type"), "special-session type"),
                citation=CitationEvidence(
                    document_id=_text(row.get("document_id"), "special document_id"),
                    publication_date=_parse_date(
                        row.get("publication_date"), "special publication_date"),
                    artifact=_text(row.get("artifact"), "special artifact"),
                    artifact_sha256=_text(
                        row.get("artifact_sha256"), "special artifact_sha256"),
                    citation_strength=row.get("citation_strength"),
                    title=row.get("title") if isinstance(row.get("title"), str) else None,
                )))
        if len({row.on for row in specials}) != len(specials):
            raise CalendarEvidenceError(f"{year_value}: special sessions must be unique")
        specials.sort(key=lambda row: row.on)

        raw_months = _mapping(year.get("months"), f"{year_value} months")
        complete = []
        for month in range(1, 13):
            key = _month_key(year_value, month)
            row = _mapping(raw_months.get(key), f"{year_value} month {key}")
            if row.get("complete") is not True:
                continue
            if row.get("unclassified_weekdays"):
                raise CalendarEvidenceError(f"{key}: a complete month has unclassified weekdays")
            if any(not item.get("resolved") for item in _sequence(
                    row.get("contradictions", ()), f"{key} contradictions")):
                raise CalendarEvidenceError(f"{key}: a complete month has unresolved contradictions")
            if not any(on.month == month for on in sessions):
                raise CalendarEvidenceError(f"{key}: a complete month has no evidenced session")
            complete.append((year_value, month))
        complete_tuple = tuple(complete)
        if year.get("year_complete") is True and len(complete_tuple) != 12:
            raise CalendarEvidenceError(f"{year_value}: year_complete but not all months are complete")

        index_series = []
        for row in _sequence(year.get("index_series"), f"{year_value} index_series"):
            row = _mapping(row, "index series")
            artifact = _mapping(row.get("artifact"), "index artifact")
            index_series.append(IndexSeriesEvidence(
                index_name=_text(row.get("index"), "index name"),
                artifact=_text(artifact.get("path"), "index artifact path"),
                artifact_sha256=_text(artifact.get("sha256"), "index artifact sha256"),
            ))
        names = [row.index_name for row in index_series]
        if sorted(names) != ["NIFTY 50", "NIFTY 500"]:
            raise CalendarEvidenceError(
                f"{year_value}: expected NIFTY 500 and NIFTY 50 index artifacts")
        for row in index_series:
            remember(row.artifact, row.artifact_sha256, f"{year_value} {row.index_name}")
        for row in closures:
            remember(row.citation.artifact, row.citation.artifact_sha256, row.citation.document_id)
        for row in specials:
            remember(row.citation.artifact, row.citation.artifact_sha256, row.citation.document_id)

        years.append(CalendarYearEvidence(
            year=year_value,
            sessions=sessions,
            closures=tuple(closures),
            special_sessions=tuple(specials),
            complete_months=complete_tuple,
            index_series=tuple(index_series),
        ))

    contradictions = []
    for row in _sequence(payload.get("contradictions", ()), "contradictions"):
        row = _mapping(row, "contradiction")
        contradictions.append(ContradictionEvidence(
            on=_parse_date(row.get("date"), "contradiction date"),
            kind=_text(row.get("type"), "contradiction type"),
            resolved=row.get("resolved") is True,
            resolution=row.get("resolution") if isinstance(row.get("resolution"), str) else None,
            resolving_document=(row.get("resolving_document")
                                if isinstance(row.get("resolving_document"), str) else None),
        ))
    complete_ids = {month for year in years for month in year.complete_months}
    for item in contradictions:
        if (item.on.year, item.on.month) in complete_ids and not item.resolved:
            raise CalendarEvidenceError(
                f"unresolved contradiction in a complete month: {item.on.isoformat()}")

    retrospective_at, retrospective_basis = _retrospective_point(
        generated_at, years, provenance_path)

    return PublishedCalendarEvidence(
        path=evidence_path,
        file_sha256=sha256(raw).hexdigest(),
        generated_at=generated_at,
        contract_coverage_end=contract_end,
        coverage_end=evidence_end,
        years=tuple(years),
        contradictions=tuple(contradictions),
        artifacts=tuple(sorted(artifacts.items())),
        retrospective_at=retrospective_at,
        retrospective_basis=retrospective_basis,
    )


def _retrospective_point(generated_at, years, provenance_path):
    if provenance_path is None:
        return (EventTime(generated_at.date(), Phase.CLOSE_AUDIT),
                "self_declared_generated_at")
    path = Path(provenance_path)
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise CalendarEvidenceError(f"cannot read calendar provenance: {exc}") from None
    payload = _mapping(payload, "calendar provenance")
    index_series = _mapping(payload.get("index_series"), "provenance index_series")
    rows = {}
    for row in _sequence(index_series.get("rows"), "provenance rows"):
        row = _mapping(row, "provenance row")
        relative = _text(row.get("path"), "provenance path")
        retrieved = _parse_datetime(row.get("retrieved_at"), "retrieved_at")
        digest = _text(row.get("sha256"), "provenance sha256")
        sha256_digest(digest, "provenance sha256")
        if relative in rows:
            raise CalendarEvidenceError(f"duplicate provenance row for {relative}")
        rows[relative] = (retrieved, digest)
    if not rows:
        raise CalendarEvidenceError("calendar provenance has no index-series rows")
    latest = max(item[0] for item in rows.values())
    if latest > generated_at:
        raise CalendarEvidenceError(
            "calendar provenance records a retrieval after the manifest was generated")
    for year in years:
        for artifact in year.index_series:
            if artifact.artifact not in rows:
                raise CalendarEvidenceError(
                    f"index artifact has no provenance row: {artifact.artifact}")
            if rows[artifact.artifact][1] != artifact.artifact_sha256:
                raise CalendarEvidenceError(
                    f"index artifact provenance digest disagrees: {artifact.artifact}")
    return EventTime(latest.date(), Phase.CLOSE_AUDIT), "provenance_retrieval"


def load_owner_acceptance(
        path: str | Path, evidence: PublishedCalendarEvidence
) -> CalendarOwnerAcceptance:
    """Load an owner acceptance record and bind it to this exact evidence file."""
    try:
        payload = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise OwnerAcceptanceInvalid(f"cannot read owner acceptance: {exc}") from None
    try:
        payload = _mapping(payload, "owner acceptance")
        if payload.get("kind") != ACCEPTANCE_KIND:
            raise OwnerAcceptanceInvalid("owner acceptance has the wrong kind")
        acceptance = CalendarOwnerAcceptance(
            accepted_by=_text(payload.get("accepted_by"), "accepted_by"),
            accepted_at=_parse_date(payload.get("accepted_at"), "accepted_at"),
            evidence_sha256=_text(payload.get("evidence_sha256"), "evidence_sha256"),
            session_source=_text(payload.get("session_source"), "session_source"),
            accepts_msd_notices=payload.get("accepts_msd_notices") is True,
            accepts_revised_muhurat_citation=(
                payload.get("accepts_revised_muhurat_citation") is True),
            coverage_end=_parse_date(payload.get("coverage_end"), "coverage_end"),
        )
    except (CalendarEvidenceError, ValueError) as exc:
        raise OwnerAcceptanceInvalid(str(exc)) from None
    _validate_acceptance(acceptance, evidence)
    return acceptance


def _validate_acceptance(acceptance, evidence):
    if not isinstance(acceptance, CalendarOwnerAcceptance):
        raise OwnerAcceptanceInvalid("owner acceptance must be a typed record")
    if acceptance.evidence_sha256 != evidence.file_sha256:
        raise OwnerAcceptanceInvalid(
            "owner acceptance is not bound to this evidence file digest")
    if acceptance.accepted_at < evidence.retrospective_at.session:
        raise OwnerAcceptanceInvalid(
            "owner acceptance predates the evidence it claims to accept")
    if acceptance.session_source != CONTEMPORANEOUS_SESSION_SOURCE:
        raise OwnerAcceptanceInvalid(
            f"session_source must be {CONTEMPORANEOUS_SESSION_SOURCE!r}")
    if acceptance.coverage_end not in (evidence.contract_coverage_end, evidence.coverage_end):
        raise OwnerAcceptanceInvalid(
            "accepted coverage end is neither the contract boundary nor the evidence boundary")

    documents = {
        row.citation.document_id
        for year in evidence.years
        for row in tuple(year.closures) + tuple(year.special_sessions)
    }
    if any(document.startswith("NSE/MSD/") for document in documents):
        if not acceptance.accepts_msd_notices:
            raise OwnerAcceptanceInvalid(
                "evidence contains MSD notices but the owner did not accept them")
    if "NSE/CMTR/42483" in documents and not acceptance.accepts_revised_muhurat_citation:
        raise OwnerAcceptanceInvalid(
            "evidence cites the revised 2019 Muhurat circular without acceptance")


def build_published_calendar(
        evidence: PublishedCalendarEvidence,
        *,
        through: date | None = None,
        acceptance: CalendarOwnerAcceptance | None = None,
        root: str | Path | None = None,
) -> PublishedExchangeCalendar:
    """Build the exact calendar, verifying every cited artifact first."""
    if not isinstance(evidence, PublishedCalendarEvidence):
        raise CalendarEvidenceError("build requires loaded calendar evidence")
    evidence.verify_artifacts(root)
    if acceptance is not None:
        _validate_acceptance(acceptance, evidence)
        limit = acceptance.coverage_end
    else:
        limit = evidence.contract_coverage_end
    if through is None:
        through = limit
    through = _date(through, "through")
    if through > limit:
        raise CalendarEvidenceError("requested coverage exceeds the accepted evidence boundary")
    if through > evidence.coverage_end:
        raise CalendarEvidenceError("requested coverage exceeds the evidence boundary")

    complete = tuple(
        month for month in evidence.complete_months
        if _month_end(*month) <= through)
    if not complete:
        raise CalendarEvidenceError("no complete month ends within the requested coverage")
    if through != _month_end(*max(complete)):
        raise CalendarEvidenceError("requested coverage must end at a complete month boundary")

    sessions = tuple(sorted(
        on for year in evidence.years for on in year.sessions if on <= through))
    closures = tuple(sorted(
        (row for year in evidence.years for row in year.closures if row.on <= through),
        key=lambda row: row.on))
    specials = tuple(sorted(
        (row for year in evidence.years for row in year.special_sessions if row.on <= through),
        key=lambda row: row.on))
    if not sessions:
        raise CalendarEvidenceError("calendar evidence has no sessions in coverage")
    if not set(row.on for row in specials) <= set(sessions):
        raise CalendarEvidenceError("a special session is not an evidenced session")
    if set(row.on for row in closures) & set(row.on for row in specials):
        raise CalendarEvidenceError("a date is both a closure and a special session")

    sources: list[CalendarSource] = []
    for year in evidence.years:
        primary = next((row for row in year.index_series if row.index_name == "NIFTY 500"), None)
        if primary is None:
            raise CalendarEvidenceError(f"{year.year}: no NIFTY 500 session source")
        for month in range(1, 13):
            month_sessions = [on for on in year.sessions
                              if on.month == month and on <= through]
            if not month_sessions:
                continue
            if acceptance is not None:
                last = month_sessions[-1]
                published_at = last
                available_at = EventTime(last, Phase.CLOSE_ORDERS)
            else:
                published_at = evidence.retrospective_at.session
                available_at = evidence.retrospective_at
            sources.append(CalendarSource(
                citation=f"{primary.index_name.replace(' ', '')}/index/"
                         f"{year.year:04d}-{month:02d}",
                sha256=primary.artifact_sha256,
                kind=SESSION_SERIES,
                published_at=published_at,
                available_at=available_at,
                effective_from=date(year.year, month, 1),
                effective_to=_month_end(year.year, month),
            ))

    annual: dict[str, CalendarSource] = {}
    supplemental: dict[str, CalendarSource] = {}
    for row in closures:
        document = row.citation.document_id
        if row.kind.startswith("annual"):
            if document in annual:
                if annual[document].effective_from.year != row.on.year:
                    raise CalendarEvidenceError(
                        f"annual closure document {document} spans multiple years")
                continue
            annual[document] = CalendarSource(
                citation=document,
                sha256=row.citation.artifact_sha256,
                kind=HOLIDAY_LIST,
                published_at=row.citation.publication_date,
                available_at=EventTime(row.citation.publication_date, Phase.CLOSE_AUDIT),
                effective_from=date(row.on.year, 1, 1),
                effective_to=date(row.on.year, 12, 31),
            )
        else:
            if document in supplemental:
                if supplemental[document].effective_from != row.on:
                    raise CalendarEvidenceError(
                        f"supplemental closure document {document} covers multiple dates")
                continue
            supplemental[document] = CalendarSource(
                citation=document,
                sha256=row.citation.artifact_sha256,
                kind=HOLIDAY_LIST,
                published_at=row.citation.publication_date,
                available_at=EventTime(row.citation.publication_date, Phase.CLOSE_AUDIT),
                effective_from=row.on,
                effective_to=row.on,
            )

    special_sources: dict[str, CalendarSource] = {}
    special_pairs = []
    for row in specials:
        document = row.citation.document_id
        if document in special_sources:
            if special_sources[document].effective_from != row.on:
                raise CalendarEvidenceError(
                    f"special-session document {document} covers multiple dates")
            continue
        special_sources[document] = CalendarSource(
            citation=document,
            sha256=row.citation.artifact_sha256,
            kind=SPECIAL_SESSION_NOTICE,
            published_at=row.citation.publication_date,
            available_at=EventTime(row.citation.publication_date, Phase.CLOSE_AUDIT),
            effective_from=row.on,
            effective_to=row.on,
        )
        special_pairs.append((row.on, document))

    sources.extend(annual.values())
    sources.extend(supplemental.values())
    sources.extend(special_sources.values())

    first_month = min(complete)
    coverage_start = min(sessions[0], date(first_month[0], first_month[1], 1))
    return PublishedExchangeCalendar(
        citation=f"B8/{evidence.path.stem}/{through.isoformat()}",
        sources=tuple(sources),
        sessions=sessions,
        closures=tuple(row.on for row in closures),
        special_sessions=tuple(row.on for row in specials),
        special_session_sources=tuple(special_pairs),
        complete_months=complete,
        coverage_start=coverage_start,
        coverage_end=through,
    )


def monthly_calendars(
        evidence: PublishedCalendarEvidence,
        acceptance: CalendarOwnerAcceptance | None,
        *,
        through: date | None = None,
        root: str | Path | None = None,
) -> tuple[KnownCalendar, ...]:
    """Production monthly calendars; refuse without a bound owner acceptance."""
    if acceptance is None:
        raise OwnerAcceptanceRequired(
            "monthly published_exchange calendars require an owner acceptance "
            "bound to the evidence digest")
    return build_published_calendar(
        evidence, through=through, acceptance=acceptance, root=root
    ).to_monthly_calendars()


def retrospective_calendar(
        evidence: PublishedCalendarEvidence,
        *,
        through: date | None = None,
        root: str | Path | None = None,
) -> KnownCalendar:
    """The diagnostic calendar, admissible only after the evidence was retrieved."""
    return build_published_calendar(
        evidence, through=through, root=root
    ).to_retrospective_calendar()
