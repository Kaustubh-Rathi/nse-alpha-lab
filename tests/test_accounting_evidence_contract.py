"""Batch 5 evidence contract: labelled synthetic fixtures and a shared information horizon.

The focused contract checks that every new domain record carries explicit dated
provenance and that future evidence is invisible to an already-decided query.
"""
from datetime import date
import ast
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"
FILES = {
    "continuity": "continuity_cases.json",
    "scoped_adjustment": "scoped_adjustment_cases.json",
    "lifecycle": "lifecycle_cases.json",
    "scenarios": "scenario_cases.json",
    "liquidation": "liquidation_cases.json",
}
SOURCE_FILES = (
    "continuity.py", "scoped_adjustment.py", "lifecycle.py", "scenarios.py", "liquidation.py")


def load(name):
    return json.loads((GOLDEN / FILES[name]).read_text(encoding="utf-8"))


@pytest.mark.parametrize("domain", tuple(FILES))
def test_new_goldens_are_labelled_synthetic_hand_derived_fixtures(domain):
    record = load(domain)
    assert record["schema_version"] == 1
    assert record["fixture"] == "synthetic_fixture"
    assert "hand_derived" in record["status"]
    assert record["derivation"].strip()
    cases = record["cases"]
    assert cases
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    for case in cases:
        assert case["working"].strip()
        has_expected = ("expected" in case
                        or bool(case.get("queries"))
                        or "expected_error" in case)
        assert has_expected


@pytest.mark.parametrize("domain", tuple(FILES))
def test_every_case_has_a_hand_derived_expected_or_error(domain):
    for case in load(domain)["cases"]:
        assert "expected_error" not in case or "expected" not in case, case["id"]
        assert bool(case.get("queries")) or "expected" in case or "expected_error" in case, case["id"]


def test_continuity_evidence_carries_publisher_document_digest_and_horizon():
    count = 0
    for case in load("continuity")["cases"]:
        for row in case["boundaries"]:
            proof = row["evidence"]
            assert proof["publisher"].strip()
            assert proof["document_id"].strip()
            assert proof["published_at"]
            assert proof["available_at"]["session"]
            assert proof["effective_from"]
            assert "effective_to" in proof
            assert proof["citation"].startswith("https://")
            assert len(proof["content_sha256"]) == 64
            int(proof["content_sha256"], 16)
            assert date.fromisoformat(proof["published_at"]) <= date.fromisoformat(
                proof["available_at"]["session"])
            count += 1
    assert count > 0


def test_lifecycle_adjustment_and_liquidation_evidence_cites_availability():
    lifecycle_count = 0
    for case in load("lifecycle")["cases"]:
        for row in case["records"]:
            assert row["citation"].startswith("https://")
            assert row["available_at"]["session"]
            lifecycle_count += 1
    assert lifecycle_count > 0

    factor_count = 0
    for case in load("scoped_adjustment")["cases"]:
        for row in case["factors"]:
            assert row["citation"]
            assert row["available_at"]["session"]
            factor_count += 1
    assert factor_count > 0

    schedule = load("liquidation")["schedule"]
    assert schedule["citation"] and schedule["available_at"]["session"]
    capacity_count = 0
    for case in load("liquidation")["cases"]:
        for row in case["capacities"]:
            assert row["citation"] and row["available_at"]["session"]
            capacity_count += 1
    assert capacity_count > 0


def test_shared_horizon_rule_excludes_future_evidence_in_every_new_domain():
    from nse_pipeline.backtest.accounting_types import EventTime, Phase
    from nse_pipeline.backtest.continuity import (
        ContinuityBoundary, DocumentaryEvidence, admit_boundaries)
    from nse_pipeline.backtest.lifecycle import (
        LifecycleRecord, LifecycleState, resolve_lifecycle)
    from nse_pipeline.backtest.scoped_adjustment import (
        AdjustmentFactor, RawPriceSeries, adjusted_series)

    at = EventTime(date(2024, 6, 4), Phase.CLOSE_VALUATION)
    future = EventTime(date(2024, 6, 5), Phase.PRE_OPEN_EVIDENCE)
    digest = "a" * 64
    proof = DocumentaryEvidence("Synthetic Exchange", "SYN/FUTURE", date(2024, 6, 5), future,
                                date(2024, 1, 1), None, "https://example.invalid/future", digest)
    boundary = ContinuityBoundary("b", "AAA", "BBB", "merger", date(2024, 5, 1), proof)
    assert admit_boundaries((boundary,), decision_at=at).records == ()

    lifecycle = LifecycleRecord("AAA", LifecycleState.SUSPENDED, date(2024, 6, 1), future,
                                "https://example.invalid/future", None, None, False)
    assert resolve_lifecycle((lifecycle,), "AAA", date(2024, 6, 4),
                             decision_at=at).state == LifecycleState.UNKNOWN

    factor = AdjustmentFactor("AAA", "split", date(2024, 6, 5), future, "1/2",
                              date(2024, 6, 1), date(2024, 6, 4), 1,
                              "https://example.invalid/future", None)
    prices = RawPriceSeries("AAA", ((date(2024, 6, 3), 100), (date(2024, 6, 4), 102)))
    assert adjusted_series(prices, (factor,), mode="point_in_time",
                           decision_at=at).value_on(date(2024, 6, 3)) == 100


def test_shared_sha256_validator_is_public_and_rejects_invalid_digests():
    from nse_pipeline.backtest.accounting_types import sha256_digest

    digest = "a" * 64
    assert sha256_digest(digest) == digest
    assert sha256_digest(digest, "content_sha256") == digest
    for invalid in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, "", None, 64):
        with pytest.raises(ValueError):
            sha256_digest(invalid, "content_sha256")


def test_new_domain_modules_do_not_import_private_cross_module_helpers():
    root = Path(__file__).resolve().parents[1] / "src/nse_pipeline/backtest"
    for name in SOURCE_FILES:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert all(not alias.name.startswith("_") for alias in node.names), (
                    name, node.module)
    scoped_source = (root / "scoped_adjustment.py").read_text(encoding="utf-8")
    assert "from .continuity import" not in scoped_source
    assert "_DIGEST" not in scoped_source


def test_new_modules_do_not_import_network_pandas_or_production_data_readers():
    root = Path(__file__).resolve().parents[1] / "src/nse_pipeline/backtest"
    forbidden = {"pandas", "requests", "urllib", "httpx", "duckdb", "psycopg2"}
    for name in SOURCE_FILES:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & forbidden), (name, imported & forbidden)
