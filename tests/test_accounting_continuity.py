"""Batch 5 continuity: dated documentary boundaries, no similarity joins, fail-closed conflicts."""
from datetime import date
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden/continuity_cases.json"
RECORDS = json.loads(GOLDEN.read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in RECORDS["cases"]}


def event(payload):
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    return EventTime(date.fromisoformat(payload["session"]), Phase(payload["phase"]),
                     payload.get("sequence", 0))


def evidence(payload):
    from nse_pipeline.backtest.continuity import DocumentaryEvidence
    return DocumentaryEvidence(
        publisher=payload["publisher"], document_id=payload["document_id"],
        published_at=date.fromisoformat(payload["published_at"]),
        available_at=event(payload["available_at"]),
        effective_from=date.fromisoformat(payload["effective_from"]),
        effective_to=(None if payload["effective_to"] is None
                      else date.fromisoformat(payload["effective_to"])),
        citation=payload["citation"], content_sha256=payload["content_sha256"])


def boundary(payload):
    from nse_pipeline.backtest.continuity import ContinuityBoundary
    return ContinuityBoundary(
        boundary_id=payload["boundary_id"], source_key=payload["source_key"],
        successor_key=payload["successor_key"], event_kind=payload["event_kind"],
        effective_on=date.fromisoformat(payload["effective_on"]),
        evidence=evidence(payload["evidence"]))


@pytest.mark.parametrize("case_id", tuple(CASES))
def test_golden_continuity_cases_match_hand_derived_outcomes(case_id):
    from nse_pipeline.backtest.continuity import admit_boundaries
    case = CASES[case_id]
    ledger = admit_boundaries(tuple(boundary(row) for row in case["boundaries"]),
                              decision_at=event(case["decision_at"]))
    expected = case["expected"]
    assert len(ledger.records) == expected["records"], case_id
    assert [issue.reason for issue in ledger.issues] == expected["issues"], case_id
    for query in case["queries"]:
        resolution = ledger.resolve(query["source_key"], query["successor_key"],
                                    date.fromisoformat(query["on"]))
        target = query["expected"]
        assert resolution.admitted is target["continuity"], (case_id, resolution)
        assert resolution.reason == target["reason"], (case_id, resolution)
        if target["continuity"]:
            assert resolution.record.event_kind == target["event_kind"]
            assert resolution.record.source_key == query["source_key"]
            assert resolution.record.successor_key == query["successor_key"]


def test_evidence_requires_publisher_document_digest_and_consistent_dates():
    from nse_pipeline.backtest.continuity import DocumentaryEvidence
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    good = dict(
        publisher="Synthetic Exchange", document_id="SYN/1",
        published_at=date(2024, 5, 2), available_at=EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION),
        effective_from=date(2024, 5, 1), effective_to=None,
        citation="https://example.invalid/synthetic/1", content_sha256="a" * 64)
    with pytest.raises(ValueError):
        DocumentaryEvidence(**{**good, "content_sha256": "not-a-digest"})
    with pytest.raises(ValueError):
        DocumentaryEvidence(**{**good, "published_at": date(2024, 6, 4)})
    with pytest.raises(ValueError):
        DocumentaryEvidence(**{**good, "effective_to": date(2024, 4, 30)})
    with pytest.raises(ValueError):
        DocumentaryEvidence(**{**good, "citation": ""})
    with pytest.raises(ValueError):
        DocumentaryEvidence(**{**good, "document_id": ""})


def test_boundary_requires_two_separate_identities_and_a_proven_interval():
    from nse_pipeline.backtest.continuity import ContinuityBoundary
    source = "AAA|INE000000001|2019-01-01"
    successor = "BBB|INE000000002|2024-05-01"
    proof = evidence(CASES["admitted_merger_boundary"]["boundaries"][0]["evidence"])
    with pytest.raises(ValueError):
        ContinuityBoundary("self", source, source, "merger", date(2024, 5, 1), proof)
    with pytest.raises(ValueError):
        ContinuityBoundary("unknown-kind", source, successor, "similar_name", date(2024, 5, 1), proof)
    with pytest.raises(ValueError):
        # effective_on outside the documentary interval
        ContinuityBoundary("outside", source, successor, "merger", date(2023, 12, 31), proof)


def test_similar_names_and_isins_never_join_without_a_documentary_boundary():
    from nse_pipeline.backtest.continuity import admit_boundaries
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    ledger = admit_boundaries((), decision_at=EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION))
    similar_a = "AAA|INE000000001|2019-01-01"
    similar_b = "AAAB|INE000000001|2019-01-01"
    resolution = ledger.resolve(similar_a, similar_b, date(2024, 6, 4))
    assert not resolution.admitted
    assert resolution.reason == "no_admitted_continuity"
    assert ledger.records == ()


def test_conflicting_or_cyclic_chains_report_and_do_not_guess():
    from nse_pipeline.backtest.continuity import admit_boundaries
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    source = "AAA|INE000000001|2019-01-01"
    successor = "BBB|INE000000002|2024-05-01"
    proof = evidence(CASES["admitted_merger_boundary"]["boundaries"][0]["evidence"])
    from nse_pipeline.backtest.continuity import ContinuityBoundary
    cycle = (
        ContinuityBoundary("ab", source, successor, "merger", date(2024, 5, 1), proof),
        ContinuityBoundary("ba", successor, source, "merger", date(2024, 5, 1), proof),
    )
    ledger = admit_boundaries(cycle, decision_at=EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION))
    assert ledger.records == ()
    assert any(issue.reason == "continuity_cycle" for issue in ledger.issues)
    resolution = ledger.resolve(source, successor, date(2024, 6, 4))
    assert not resolution.admitted and resolution.reason == "conflicting_evidence"
    with pytest.raises(ValueError):
        ledger.require(source, successor, date(2024, 6, 4))
    with pytest.raises(ValueError):
        admit_boundaries(cycle + (cycle[0],),
                         decision_at=EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION))


def test_prefix_invariance_when_future_evidence_is_appended():
    from dataclasses import replace

    from nse_pipeline.backtest.continuity import admit_boundaries
    from nse_pipeline.backtest.accounting_types import EventTime, Phase

    case = CASES["conflicting_successors_are_not_guessed"]
    early = (boundary(case["boundaries"][0]),)
    future = boundary(case["boundaries"][1])
    future = replace(future, evidence=replace(
        future.evidence,
        available_at=EventTime(date(2024, 6, 5), Phase.PRE_OPEN_EVIDENCE)))
    decision = EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION)
    before = admit_boundaries(early, decision_at=decision)
    before_answer = before.resolve(case["queries"][0]["source_key"],
                                   case["queries"][0]["successor_key"], date(2024, 6, 4))
    after = admit_boundaries(early + (future,), decision_at=decision)
    after_answer = after.resolve(case["queries"][0]["source_key"],
                                 case["queries"][0]["successor_key"], date(2024, 6, 4))
    assert before == after
    assert before.records == after.records
    assert before.issues == after.issues
    assert before_answer == after_answer
    assert before_answer.admitted is True
    assert after_answer.admitted is True


def test_scheme_of_arrangement_may_have_multiple_successors():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.continuity import ContinuityBoundary, admit_boundaries

    proof = evidence(CASES["admitted_merger_boundary"]["boundaries"][0]["evidence"])
    source = "AAA|INE000000001|2019-01-01"
    first = "BBB|INE000000002|2024-05-01"
    second = "CCC|INE000000003|2024-05-01"
    scheme = (
        ContinuityBoundary("scheme-a", source, first, "scheme_of_arrangement",
                           date(2024, 5, 1), proof),
        ContinuityBoundary("scheme-b", source, second, "scheme_of_arrangement",
                           date(2024, 5, 1), proof),
    )
    ledger = admit_boundaries(
        scheme, decision_at=EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION))
    assert ledger.issues == ()
    assert tuple(record.boundary_id for record in ledger.records) == ("scheme-a", "scheme-b")


def test_a_plain_merger_still_cannot_claim_multiple_successors():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.continuity import ContinuityBoundary, admit_boundaries

    proof = evidence(CASES["admitted_merger_boundary"]["boundaries"][0]["evidence"])
    source = "AAA|INE000000001|2019-01-01"
    first = "BBB|INE000000002|2024-05-01"
    second = "CCC|INE000000003|2024-05-01"
    merger = (
        ContinuityBoundary("merger-a", source, first, "merger", date(2024, 5, 1), proof),
        ContinuityBoundary("merger-b", source, second, "merger", date(2024, 5, 1), proof),
    )
    ledger = admit_boundaries(
        merger, decision_at=EventTime(date(2024, 6, 3), Phase.CLOSE_VALUATION))
    assert ledger.records == ()
    assert [issue.reason for issue in ledger.issues] == ["ambiguous_successor"]
