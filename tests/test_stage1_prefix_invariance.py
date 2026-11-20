"""Batch 6 prefix invariance: future appends must not change past economics or provenance.

A run truncated at a past session is a prefix of the full run. Appending future
bars must not change past cash, claims, positions, NAV, ranks, orders or fills.
This module compares a ``stop_at`` prefix run against the full run and asserts
the past artifacts are byte-identical. It never weakens a failing prefix test.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests import stage1_harness as harness

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "golden" / "e2e_cases.json").read_text(encoding="utf-8"))


def test_appending_future_bars_cannot_change_past_nav_ranks_orders_or_provenance():
    prefix = harness.run_plan(GOLDEN, stop_at="2024-06-07")
    full = harness.run_plan(GOLDEN)

    # Past valuation snapshot is unchanged by later sessions.
    assert prefix.valuations["2024-06-07"] == full.valuations["2024-06-07"]

    # Past signal ranks and universe membership are append-invariant.
    assert prefix.ranks == full.ranks
    assert prefix.membership == full.membership
    assert prefix.sizing == full.sizing

    # Past book ledger is a prefix of the full ledger: future appends only grow it,
    # so past cash, claims, positions, orders and fills are identical by replay.
    assert prefix.book.events == full.book.events[:len(prefix.book.events)]
    assert prefix.book.initial == full.book.initial
    assert len(full.book.observed_sessions) > len(prefix.book.observed_sessions)


def test_appending_future_sessions_does_not_change_the_reconciled_prefix():
    prefix = harness.run_plan(GOLDEN, stop_at="2024-06-07")
    full = harness.run_plan(GOLDEN)

    assert prefix.reconciliation_ok is True
    assert full.reconciliation_ok is True
    # The prefix run's ledger is a genuine prefix of the full run's ledger, so the
    # past reconciliation inputs are identical byte-for-byte.
    assert prefix.book.events == full.book.events[:len(prefix.book.events)]
    assert prefix.book.initial == full.book.initial
    assert full.book.snapshot.as_of > prefix.book.snapshot.as_of


def test_economic_prefix_ignores_run_metadata_but_not_provenance():
    # The same past NAV is computed from the same state + marks, independent of
    # how many later sessions exist. Provenance (the exact valuation fields) is
    # retained and equal, not elided.
    prefix = harness.run_plan(GOLDEN, stop_at="2024-06-07")
    full = harness.run_plan(GOLDEN)
    before = prefix.valuations["2024-06-07"]
    after = full.valuations["2024-06-07"]
    assert before.nav == after.nav == harness.money(GOLDEN["expected"]["nav"]["2024-06-07"])
    assert before.cash == after.cash
    assert before.marked_value_priced == after.marked_value_priced
    assert before.unresolved_positions == after.unresolved_positions == ()
    assert before.nav_reason == after.nav_reason == "nav_available"
