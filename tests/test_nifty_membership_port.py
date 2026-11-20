from datetime import date
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest

from nse_pipeline.backtest.accounting_types import EventTime, Phase
from nse_pipeline.backtest.evidence import ObservationLedger
from nse_pipeline.backtest.observations import Bar, SessionStream
from nse_pipeline.backtest.universe import build_membership
from nse_pipeline.nifty.snapshots import Member, Snapshot
from nse_pipeline.nifty.membership_port import (
    AvailabilityEvidence, CoverageUnknown, SnapshotMembershipEvidence,
)
from nse_pipeline.specs import UniverseSpec

D1 = date(2024, 1, 2)
D2 = date(2024, 1, 3)
SOURCE = b"hand-literal reviewed-input fixture; not a historical source"


def snapshot(day=D1, symbols=("AAA",), source=SOURCE):
    return Snapshot("NIFTY50", day, tuple(Member(symbol) for symbol in symbols), {
        "index_code": "NIFTY50", "as_of": day.isoformat(),
        "membership_kind": "dated_official_snapshot",
        "as_of_basis": "document_explicit_date", "as_of_evidence": "fixture date",
        "expected_security_count": len(symbols),
        "source_url": "https://www.niftyindices.com/reviewed-fixture.pdf",
        "source_sha256": sha256(source).hexdigest(),
        "retrieved_at_utc": "2024-01-04T12:00:00+00:00",
    })


def availability(snap, available=D1):
    return AvailabilityEvidence(
        snap.index_code, snap.as_of,
        EventTime(available, Phase.PRE_OPEN_EVIDENCE),
        "reviewer availability attestation", "fixture publication basis",
        snap.metadata["source_sha256"],
    )


def adapter(*snapshots, decision=D2, key_map=None, availability_records=None,
            sources=None, index_code=None):
    return SnapshotMembershipEvidence(
        snapshots, key_map or {member.symbol: member.symbol for item in snapshots
                               for member in item.members},
        sources or {(item.index_code, item.as_of): SOURCE for item in snapshots},
        availability_records or [availability(item) for item in snapshots],
        decision_at=EventTime(decision, Phase.PRE_OPEN_EVIDENCE), index_code=index_code,
    )


def ledger(*keys):
    stream = SessionStream()
    observations = ObservationLedger(stream)
    stream.observe(D1)
    observations.ingest(D1, [Bar(key, key, "EQ", D1, 100, 100, 100, 100,
                                 10000, True, "INE000000001", 1000, 10)
                             for key in keys])
    return observations


def test_exact_date_port_composes_with_public_official_membership_builder():
    snap = snapshot()
    evidence = adapter(snap)
    result = build_membership(ledger("AAA"), D1, UniverseSpec(min_listed_sessions=1),
                              membership_evidence=evidence)
    assert result.kind == "official_membership"
    assert result.eligible == ("AAA",)
    assert evidence.is_member("AAA", D1) is True


def test_cutoff_and_exact_date_gaps_fail_unknown_instead_of_carrying():
    snap = snapshot()
    future = adapter(snap, decision=date(2024, 1, 1))
    with pytest.raises(CoverageUnknown):
        future.is_member("AAA", D1)
    evidence = adapter(snap)
    with pytest.raises(CoverageUnknown, match="exact-date"):
        evidence.is_member("AAA", D2)
    future_snap = snapshot(D2)
    future_only = adapter(future_snap, decision=D1,
                          availability_records=[availability(future_snap, D2)])
    with pytest.raises(CoverageUnknown):
        future_only.is_member("AAA", D1)


def test_complete_snapshot_can_say_false_only_for_exact_mapped_key():
    snap = snapshot(symbols=("AAA",))
    evidence = adapter(snap, key_map={"AAA": "AAA", "BBB": "BBB"})
    assert evidence.is_member("BBB", D1) is False
    with pytest.raises(ValueError, match="exact key"):
        evidence.is_member("NOT_MAPPED", D1)


def test_constructor_freezes_mapping_snapshot_metadata_and_source_bytes():
    snap = snapshot()
    mapping = {"AAA": "AAA"}
    metadata = snap.metadata
    evidence = adapter(snap, key_map=mapping)
    mapping["AAA"] = "OTHER"
    metadata["source_sha256"] = "0" * 64
    assert evidence.is_member("AAA", D1) is True


def test_source_bytes_are_digest_bound_and_zip_member_digest_is_checked():
    snap = snapshot()
    with pytest.raises(ValueError, match="source digest"):
        adapter(snap, sources={(snap.index_code, snap.as_of): b"changed"})

    archive_bytes = BytesIO()
    with ZipFile(archive_bytes, "w") as archive:
        archive.writestr("report.csv", b"inner source")
    zip_source = archive_bytes.getvalue()
    zip_meta = dict(snap.metadata, source_sha256=sha256(zip_source).hexdigest(),
                    source_member="report.csv", source_member_sha256="0" * 64)
    zip_snap = Snapshot(snap.index_code, snap.as_of, snap.members, zip_meta)
    with pytest.raises(ValueError, match="source member digest"):
        adapter(zip_snap, sources={(zip_snap.index_code, zip_snap.as_of): zip_source})


def test_availability_must_be_separately_attested_and_not_predate_snapshot():
    snap = snapshot()
    too_early = availability(snap, date(2024, 1, 1))
    with pytest.raises(ValueError, match="predates snapshot"):
        adapter(snap, availability_records=[too_early])


def test_symbol_mapping_is_exact_bijective_and_indices_are_explicit():
    snap = snapshot(symbols=("AAA", "BBB"))
    with pytest.raises(ValueError, match="collision"):
        adapter(snap, key_map={"KEY1": "AAA", "KEY2": "AAA"})
    second = snapshot(D2)
    records = [availability(snap), availability(second, D2)]
    with pytest.raises(ValueError, match="index_code"):
        adapter(snap, second, index_code=None, availability_records=records)
