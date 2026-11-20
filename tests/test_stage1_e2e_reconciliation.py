"""Batch 6 harness reconciliation: one hand-derived golden joins cash, claims,
positions, orders, entitlements and an independent replay. This remains a test-only
harness; production execution and prefix contracts are covered separately.
"""
from __future__ import annotations

import json
from pathlib import Path

from nse_pipeline import stage1_evidence as evidence
from tests import stage1_harness as harness

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "golden" / "e2e_cases.json").read_text(encoding="utf-8"))


def test_e2e_golden_reconciles_cash_claims_positions_orders_entitlements():
    result = harness.run_plan(GOLDEN)
    expected = GOLDEN["expected"]

    assert result.reconciliation_ok is True

    # Cash.
    assert result.book.snapshot.cash == harness.money(expected["final_cash"])

    # Positions (shares and delivered shares after settlement).
    positions = {p.key: p for p in result.book.snapshot.positions}
    assert set(positions) == {row["key"] for row in expected["positions"]}
    for row in expected["positions"]:
        assert positions[row["key"]].shares == row["shares"]
        assert positions[row["key"]].delivered_shares == row["delivered_shares"]

    # Claims: all buy payables settled, no open claims remain.
    assert result.book.snapshot.claims == ()

    # Orders: both reserved orders consumed by their fills.
    assert result.book.snapshot.orders == ()

    # Entitlements: none were minted in this dividend-free fixture.
    assert result.book.snapshot.entitlements == ()

    # Sizing and NAV match the hand-derived oracle.
    assert result.sizing.filled == tuple(harness.money(v) for v in expected["filled_notionals"])
    assert result.sizing.remaining_cash == harness.money(expected["remaining_cash_after_sizing"])
    for session, nav in expected["nav"].items():
        assert result.valuations[session].nav == harness.money(nav)


def test_reconciliation_checks_cover_cash_claims_positions_orders_and_entitlements():
    result = harness.run_plan(GOLDEN)
    names = set(result.reconciliation_checks)
    assert {
        "event_integrity", "cash_conservation", "claim_conservation",
        "share_delivery_and_identity", "reservation_conservation",
        "receivable_rollforward", "payable_rollforward", "entitlement_conservation",
        "snapshot_clock", "settlement_phase_completion", "funded_cash",
        "position_cap", "claim_lifecycle",
    } <= names


def test_no_performance_statistic_is_generated_or_published():
    # The report's own flag must remain false and the gate must remain CLOSED.
    source = (ROOT / "tests" / "stage1_harness.py").read_text(encoding="utf-8")
    for token in ("ReturnSeries", "performance", "strategy_returns", "cagr", "sharpe", "benchmark"):
        assert token not in source, token
    counts = evidence.contract_counts(evidence.CONTRACTS)
    assert sum(counts.values()) == 29
    assert counts.get("CONTRACT_PASS", 0) < 29


def test_final_gate_enumerates_29_rows_and_separates_production_e2e_from_harness():
    ids = [contract.contract_id for contract in evidence.CONTRACTS]
    assert len(ids) == len(set(ids)) == 29
    assert "P0-E2E" in ids
    rows = {row.contract_id: row for row in evidence.CONTRACTS}
    assert rows["P0-EXECUTION"].contract_status == "CONTRACT_PASS"
    assert rows["P0-PREFIX"].contract_status == "CONTRACT_PASS"
    e2e = rows["P0-E2E"]
    assert e2e.contract_status == "CONTRACT_PARTIAL"
    assert e2e.component_status == "COMPONENT_IMPLEMENTED"
    assert e2e.component_modules == (
        "test_stage1_production_runner_contracts", "test_stage1_production_e2e",
        "test_accounting_observed_session_phase30", "test_accounting_target_composition")

    # Batch 6 remains an independently useful harness oracle, not the production E2E claim.
    assert harness.run_plan(GOLDEN).reconciliation_ok is True
