"""Integrity-bound reviewed-input port for exact-date exchange calendar sessions.

This is an integrity-bound reviewed-input port, not source authentication or
coverage certification. Digests and availability attestations bind the supplied
bytes and a reviewer's claim of when they were available; they do not prove who
published them or that the source was authoritative. Annual holiday lists are
calendar candidates only: a holiday list alone never certifies a trading day,
and absence from a captured list is not evidence of a session. No weekday,
weekend, or holiday-complement inference is performed, and special-session
dates are declared as neither session nor closure, so they resolve nothing.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from io import BytesIO
from types import MappingProxyType
from zipfile import BadZipFile, ZipFile

from nse_pipeline.audit.calendar import FORBIDDEN_CLOSURE_BASES, exchange_date
from nse_pipeline.backtest.accounting_types import EventTime


class CalendarCoverageUnknown(ValueError):
    """Requested session status is not known from an admitted exact-date declaration."""


@dataclass(frozen=True)
class CalendarDocumentDeclaration:
    """Reviewer-admitted session and closure dates for one digest-bound document."""

    calendar_identity: str
    as_of: date
    declared_source_sha256: str
    session_dates: tuple
    holiday_dates: tuple
    source_member: str | None = None
    source_member_sha256: str | None = None

    def __post_init__(self):
        if not isinstance(self.calendar_identity, str) or not self.calendar_identity.strip():
            raise ValueError("calendar identity must be a nonempty string")
        if type(self.as_of) is not date:
            raise ValueError("document as_of must be a date")
        _require_digest(self.declared_source_sha256, "declaration source binding")
        object.__setattr__(self, "session_dates", _freeze_dates(self.session_dates, "session"))
        object.__setattr__(self, "holiday_dates", _freeze_dates(self.holiday_dates, "holiday"))
        if set(self.session_dates) & set(self.holiday_dates):
            raise ValueError("declaration cannot list one date as both session and holiday")
        if (self.source_member is None) != (self.source_member_sha256 is None):
            raise ValueError("ZIP source member name and digest are both required")
        if self.source_member is not None:
            if not isinstance(self.source_member, str) or not self.source_member.strip():
                raise ValueError("ZIP source member name must be nonempty")
            _require_digest(self.source_member_sha256, "source member binding")

    @property
    def identity(self) -> tuple[str, date]:
        return (self.calendar_identity, self.as_of)


@dataclass(frozen=True)
class CalendarAvailabilityEvidence:
    """Separate reviewer attestation of when bound calendar material was available."""

    calendar_identity: str
    as_of: date
    available_at: EventTime
    citation: str
    basis: str
    source_sha256: str

    def __post_init__(self):
        if (not isinstance(self.calendar_identity, str) or not self.calendar_identity.strip()
                or type(self.as_of) is not date
                or not isinstance(self.available_at, EventTime)):
            raise ValueError("availability needs a calendar identity, document date, and EventTime")
        if not isinstance(self.citation, str) or not self.citation.strip():
            raise ValueError("availability citation must be nonempty")
        if not isinstance(self.basis, str) or not self.basis.strip():
            raise ValueError("availability basis must be nonempty")
        if self.basis.strip().lower() in FORBIDDEN_CLOSURE_BASES:
            raise ValueError("availability basis is never closure evidence")
        _require_digest(self.source_sha256, "availability source binding")
        if self.available_at.session < self.as_of:
            raise ValueError("availability predates document date")


class CalendarEvidencePort:
    """Answer only exact-date queries from digest-bound, reviewed calendar inputs.

    Construction requires each document's primary-source bytes, an explicit
    session/holiday declaration, a separate availability attestation for every
    document, and a bound decision EventTime. ``is_session`` returns True or
    False only for dates an admitted (available at or before ``decision_at``)
    declaration lists exactly; every other date raises
    :class:`CalendarCoverageUnknown`, never an inferred False.
    """

    def __init__(self, declarations: Sequence[CalendarDocumentDeclaration],
                 source_bytes: Mapping[tuple[str, date], bytes],
                 availability_records: Sequence[CalendarAvailabilityEvidence], *,
                 decision_at: EventTime):
        if (not isinstance(declarations, Sequence) or isinstance(declarations, (str, bytes))
                or not declarations):
            raise ValueError("at least one CalendarDocumentDeclaration is required")
        if not isinstance(decision_at, EventTime):
            raise ValueError("decision_at must be an explicit EventTime")
        if not isinstance(source_bytes, Mapping):
            raise ValueError("source bytes mapping is required")
        if not isinstance(availability_records, Sequence) or isinstance(availability_records, (str, bytes)):
            raise ValueError("separate availability records are required")

        copied_sources = {}
        for identity, content in source_bytes.items():
            if (not isinstance(identity, tuple) or len(identity) != 2
                    or not isinstance(identity[0], str) or not identity[0]
                    or type(identity[1]) is not date
                    or not isinstance(content, (bytes, bytearray, memoryview))):
                raise ValueError("source bytes must be keyed by (calendar_identity, as_of)")
            copied_sources[identity] = bytes(content)

        evidence_by_identity = {}
        for evidence in availability_records:
            if not isinstance(evidence, CalendarAvailabilityEvidence):
                raise ValueError("availability records must be CalendarAvailabilityEvidence")
            identity = (evidence.calendar_identity, evidence.as_of)
            prior = evidence_by_identity.get(identity)
            if prior is not None and prior != evidence:
                raise ValueError("ambiguous availability evidence")
            evidence_by_identity[identity] = evidence

        accepted = {}
        for declaration in declarations:
            if not isinstance(declaration, CalendarDocumentDeclaration):
                raise ValueError("declarations must be CalendarDocumentDeclaration objects")
            identity = declaration.identity
            previous = accepted.get(identity)
            if previous is not None:
                if previous[0] != declaration:
                    raise ValueError("ambiguous duplicate calendar document identity")
                continue
            source = copied_sources.get(identity)
            if source is None:
                raise ValueError("primary source bytes are required for every declaration")
            digest = sha256(source).hexdigest()
            if digest != declaration.declared_source_sha256:
                raise ValueError("source digest mismatch")
            if declaration.source_member is not None:
                try:
                    with ZipFile(BytesIO(source)) as archive:
                        inner = archive.read(declaration.source_member)
                except (BadZipFile, KeyError, OSError) as exc:
                    raise ValueError("invalid ZIP source member") from exc
                if sha256(inner).hexdigest() != declaration.source_member_sha256:
                    raise ValueError("source member digest mismatch")

            evidence = evidence_by_identity.get(identity)
            if evidence is None:
                raise ValueError("separate availability evidence is required")
            if evidence.source_sha256 != digest:
                raise ValueError("availability source binding mismatch")
            accepted[identity] = (declaration, evidence)

        date_index = {}
        for identity, (declaration, _evidence) in accepted.items():
            for day in declaration.session_dates:
                date_index.setdefault(day, []).append((identity, True))
            for day in declaration.holiday_dates:
                date_index.setdefault(day, []).append((identity, False))

        self._documents = MappingProxyType(accepted)
        self._date_index = MappingProxyType({day: tuple(items)
                                             for day, items in date_index.items()})
        self._decision_at = decision_at

    def is_session(self, session_date: date) -> bool:
        """Return exact-date session status, or raise CalendarCoverageUnknown when not known."""
        if type(session_date) is not date:
            raise ValueError("session_date must be a date, not a datetime or other value")
        if session_date > self._decision_at.session:
            raise CalendarCoverageUnknown("query date is after the bound decision_at")
        covering = self._date_index.get(session_date)
        if covering is None:
            raise CalendarCoverageUnknown(
                f"calendar coverage is unknown: no admitted declaration for {session_date}")
        kinds = set()
        for identity, is_session in covering:
            _declaration, evidence = self._documents[identity]
            if evidence.available_at > self._decision_at:
                raise CalendarCoverageUnknown(
                    f"calendar availability is after decision_at for {session_date}")
            kinds.add(is_session)
        if len(kinds) > 1:
            raise CalendarCoverageUnknown(
                f"conflicting session and holiday declarations for {session_date}")
        return next(iter(kinds))


def _require_digest(value, label: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)):
        raise ValueError(f"{label} must be a SHA-256 digest")


def _freeze_dates(values, label: str) -> tuple:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{label} dates must be a sequence of dates")
    frozen = []
    for item in values:
        if type(item) is not date:
            raise ValueError(f"{label} dates must be date objects, not datetime or other values")
        frozen.append(exchange_date(item.isoformat()))
    return tuple(sorted(frozen))
