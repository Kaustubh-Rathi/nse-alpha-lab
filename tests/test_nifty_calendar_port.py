from datetime import date
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.nifty.calendar_port import (
    CalendarAvailabilityEvidence, CalendarCoverageUnknown,
    CalendarDocumentDeclaration, CalendarEvidencePort,
)

D1 = date(2024, 1, 2)
D2 = date(2024, 1, 3)
D3 = date(2024, 1, 4)
UNLISTED = date(2024, 1, 5)
SOURCE = b"hand-literal reviewed-input calendar fixture; not a historical source"


def declaration(session_dates=(D1,), holiday_dates=(), identity="NSE_CM", as_of=D1,
                source=SOURCE, **member_fields):
    return CalendarDocumentDeclaration(
        identity, as_of, sha256(source).hexdigest(),
        tuple(session_dates), tuple(holiday_dates), **member_fields,
    )


def availability(decl, available=None):
    return CalendarAvailabilityEvidence(
        decl.calendar_identity, decl.as_of,
        EventTime(available or decl.as_of, Phase.PRE_OPEN_EVIDENCE),
        "reviewer availability attestation", "fixture publication basis",
        decl.declared_source_sha256,
    )


def port(*decls, decision=D3, sources=None, records=None):
    return CalendarEvidencePort(
        decls,
        sources if sources is not None else {decl.identity: SOURCE for decl in decls},
        records if records is not None else [availability(decl) for decl in decls],
        decision_at=EventTime(decision, Phase.PRE_OPEN_EVIDENCE),
    )


def test_exact_admitted_dates_answer_true_and_false():
    evidence = port(declaration(session_dates=(D1, D3), holiday_dates=(D2,)))
    assert evidence.is_session(D1) is True
    assert evidence.is_session(D3) is True
    assert evidence.is_session(D2) is False


def test_unlisted_date_raises_coverage_unknown_and_never_returns_false():
    evidence = port(declaration(session_dates=(D1,)))
    with pytest.raises(CalendarCoverageUnknown):
        evidence.is_session(UNLISTED)


def test_availability_after_decision_at_raises_unknown_at_query():
    decl = declaration(session_dates=(D1,))
    evidence = port(decl, decision=D2, records=[availability(decl, D3)])
    with pytest.raises(CalendarCoverageUnknown, match="after decision_at"):
        evidence.is_session(D1)


def test_query_after_decision_at_raises_unknown():
    evidence = port(declaration(session_dates=(D1, D2)), decision=D1)
    with pytest.raises(CalendarCoverageUnknown, match="after the bound decision_at"):
        evidence.is_session(D2)


def test_source_bytes_are_digest_bound():
    decl = declaration()
    with pytest.raises(ValueError, match="source digest"):
        port(decl, sources={decl.identity: b"changed"})


def test_availability_must_be_separately_attested_and_not_predate_document():
    decl = declaration()
    with pytest.raises(ValueError, match="availability"):
        port(decl, records=[])
    with pytest.raises(ValueError, match="predates document"):
        CalendarAvailabilityEvidence(
            decl.calendar_identity, D2, EventTime(D1, Phase.PRE_OPEN_EVIDENCE),
            "citation", "basis", decl.declared_source_sha256)


def test_holiday_list_alone_never_certifies_a_trading_day():
    evidence = port(declaration(session_dates=(), holiday_dates=(D2, UNLISTED)))
    assert evidence.is_session(D2) is False
    with pytest.raises(CalendarCoverageUnknown):
        evidence.is_session(D1)
    with pytest.raises(CalendarCoverageUnknown):
        evidence.is_session(D3)


def test_zip_member_digest_is_checked():
    archive_bytes = BytesIO()
    with ZipFile(archive_bytes, "w") as archive:
        archive.writestr("holidays.csv", b"inner calendar source")
    zip_source = archive_bytes.getvalue()
    good = declaration(source=zip_source, source_member="holidays.csv",
                       source_member_sha256=sha256(b"inner calendar source").hexdigest())
    assert port(good, sources={good.identity: zip_source}).is_session(D1) is True
    bad = declaration(source=zip_source, source_member="holidays.csv",
                      source_member_sha256="0" * 64)
    with pytest.raises(ValueError, match="source member digest"):
        port(bad, sources={bad.identity: zip_source})


def test_constructor_freezes_declarations_and_source_bytes():
    session_list = [D1]
    holiday_list = [D2]
    sources = {("NSE_CM", D1): SOURCE}
    decl = CalendarDocumentDeclaration("NSE_CM", D1, sha256(SOURCE).hexdigest(),
                                       session_list, holiday_list)
    evidence = port(decl, sources=sources)
    session_list.append(D3)
    holiday_list.append(UNLISTED)
    sources[("NSE_CM", D3)] = b"extra"
    assert evidence.is_session(D1) is True
    assert evidence.is_session(D2) is False
    with pytest.raises(CalendarCoverageUnknown):
        evidence.is_session(D3)
    with pytest.raises(CalendarCoverageUnknown):
        evidence.is_session(UNLISTED)


def test_ambiguous_duplicate_document_identity_raises():
    first = declaration(session_dates=(D1,))
    conflicting = declaration(session_dates=(D2,))
    with pytest.raises(ValueError, match="ambiguous duplicate"):
        port(first, conflicting)


def test_forbidden_closure_basis_is_rejected():
    decl = declaration()
    with pytest.raises(ValueError, match="never closure evidence"):
        CalendarAvailabilityEvidence(
            decl.calendar_identity, decl.as_of,
            EventTime(D1, Phase.PRE_OPEN_EVIDENCE),
            "citation", "weekday_pattern", decl.declared_source_sha256)
