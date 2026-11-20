"""Batch 6 component-contract smoke tests and harness import isolation.

The test-only harness composes the public component boundaries into one causal
flow. This module proves the harness stays inside those boundaries (no legacy
engine/runner/strategy/data-acquisition import) and that every component it
composes is exercised at least once by the end-to-end run.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from tests import stage1_harness as harness

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "golden" / "e2e_cases.json").read_text(encoding="utf-8"))

FORBIDDEN = (
    "nse_pipeline.backtest.engine",
    "nse_pipeline.backtest.total_return",
    "nse_pipeline.backtest.history",
    "nse_pipeline.backtest.freeze",
    "nse_pipeline.backtest.sensitivity",
    "nse_pipeline.backtest.series_fallback",
    "nse_pipeline.backtest.successor",
    "pandas",
    "duckdb",
    "nse_pipeline.download",
    "nse_pipeline.build",
)

HARNESS_FILES = (
    "tests/stage1_harness.py",
    "tests/test_stage1_component_contracts.py",
    "tests/test_stage1_frozen_order_perturbation.py",
    "tests/test_stage1_prefix_invariance.py",
    "tests/test_stage1_e2e_reconciliation.py",
)


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module


def test_harness_imports_no_legacy_engine_runner_or_strategy():
    for relative in HARNESS_FILES:
        for module in _imports(ROOT / relative):
            assert not module.startswith(FORBIDDEN), (relative, module)
    # The harness itself never references a performance/return statistic.
    source = (ROOT / "tests" / "stage1_harness.py").read_text(encoding="utf-8")
    for banned in ("ReturnSeries", "strategy_returns", "cagr", "sharpe", "benchmark"):
        assert banned not in source, banned


def test_golden_is_labelled_synthetic_hand_derived_and_self_consistent():
    assert GOLDEN["schema_version"] == 1
    assert GOLDEN["fixture"] == "synthetic_fixture"
    assert "hand_derived" in GOLDEN["status"]
    assert "not_production_P0_pass" in GOLDEN["status"]
    assert GOLDEN["derivation"].strip()
    expected = GOLDEN["expected"]
    assert len(expected["rank_order"]) == 2
    assert expected["rank_order"] == ["AAA|INE000000001|2024-06-03", "BBB|INE000000002|2024-06-03"]


def test_component_contracts_compose_in_the_end_to_end_run():
    result = harness.run_plan(GOLDEN)

    # identity produced the two stable keys referenced by every later stage
    assert result.membership is not None
    assert result.ranks is not None
    assert result.sizing is not None

    # signals: momentum ranks in the expected order with exact fractions
    assert result.ranks.order == tuple(GOLDEN["expected"]["rank_order"])
    for ranked in result.ranks.ranked:
        assert str(ranked.momentum) == GOLDEN["expected"]["momentum"][ranked.key]

    # universe: both synthetic symbols eligible
    assert set(result.membership.eligible) == set(GOLDEN["expected"]["rank_order"])

    # sizing: P2 allocated the two ranks to the hand-derived principal notionals
    # (each rank's notional is floor(desired / price) * price, so it need not equal
    # the desired notional exactly).
    assert result.sizing.filled == tuple(harness.money(v) for v in GOLDEN["expected"]["filled_notionals"])
    assert result.sizing.policy == "P2_size_excluding_unresolved"

    # valuation: two dated NAV snapshots exist and are exact
    assert set(result.valuations) == {"2024-06-07", "2024-06-10"}

    # accounting: reconciliation passed independently
    assert result.reconciliation_ok is True
