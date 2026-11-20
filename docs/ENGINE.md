# Backtest engines: which one is real

This repository contains **two backtest engines** and **two identity systems** in
`src/nse_pipeline/backtest/`. They are not interchangeable and they are not one
abstraction split in two. This document states which module is canonical for
each job, which tests cover it, and why both still exist.

Written against commit state at export (see `.export-manifest.json`). Every
claim below was verified by grepping `src/`, `tests/`, `research/` and `scripts/`
for the module paths and symbol names, not by assuming what the audit said.

---

## 1. The short version

| Job | Canonical module | Status of the other |
|---|---|---|
| Running a backtest that produces published numbers | **`backtest/momentum.py`** | `backtest/engine.py` is **LEGACY** |
| Resolving security identity for a backtest | **`backtest/security_identity.py`** | `backtest/identity.py` is **ACTIVE, different layer** |

Neither pair is a duplicate. Do not silently substitute one for the other.

---

## 2. Engines

### `backtest/momentum.py` — CANONICAL

Exports `EngineConfig`, `BarsFrame`, `MomentumRotationEngine`, and the published
cost/delisting vocabularies (`COST_COMPONENTS`, `DELISTING_POLICIES`,
`ENTRY_TIMINGS`).

It is canonical because it is what actually produces every number in
`research/`:

| Consumer | Imports |
|---|---|
| `research/run_sweep.py` | `EngineConfig`, `BarsFrame`, `MomentumRotationEngine`, `COST_COMPONENTS` |
| `research/run_alpha_backtest.py` | `EngineConfig`, `BarsFrame`, `MomentumRotationEngine`, `COST_COMPONENTS` |
| `src/nse_pipeline/backtest/momentum_grid.py` | `EngineConfig`, `BarsFrame`, `MomentumRotationEngine` |
| `src/nse_pipeline/cli.py` | `EngineConfig`, `BarsFrame`, `MomentumRotationEngine` |
| `tests/test_backtest_momentum.py` | direct unit tests |
| `tests/test_momentum_empty_universe.py` | empty-universe edge case |
| `tests/test_leakage_causality.py` | causality / look-ahead invariants |
| `tests/test_backtest_exits.py` | exit policy via `momentum_grid` |

Its design rules are enforced in code (see the module docstring): no
look-ahead, **no invented prices**, no invented corporate actions,
deterministic tie-breaking, integer shares, and purity (no I/O, no clock).

### `backtest/engine.py` — LEGACY

Exports `RotationBacktester` plus a family of loaders and identity-continuity
helpers. It is **not** imported by `momentum.py`, and `momentum.py` is not
imported by it: the two engines are independent.

It is retained for three concrete reasons:

1. **Shared utilities in live use.** `history.py`, `sensitivity.py`,
   `total_return.py`, `successor.py`, `series_fallback.py` and `momentum_grid.py`
   import loaders and helpers from it — specifically `load_adjusted_bars`,
   `load_bars_from_duckdb`, `apply_identity_continuity`,
   `build_key_stabilization`, `apply_key_stabilization`, `KeyStabilization`,
   `CostConfig`, `CircuitConfig`, `UniverseConfig`, `executable_reason`,
   `month_end_dates`, `annual_stats`, `momentum_signal` and
   `_root_for_symbol`.
2. **Legacy tests.** `test_backtester.py` and `test_backtest_causality.py`
   assert directly on `RotationBacktester`,
   `apply_identity_continuity`, `apply_key_stabilization` and
   `build_key_stabilization`. `test_history.py`, `test_total_return.py`,
   `test_successor.py` and `test_loader_causality.py` cover its loaders.
3. **Audit traceability.** `docs/QUANT_REPOSITORY_AUDIT.md` cites specific line
   numbers in this file. Deleting it would orphan that evidence.

The repository's own contract test independently classifies it as legacy:
`tests/test_stage1_component_contracts.py` lists
`"nse_pipeline.backtest.engine"` in a `FORBIDDEN` tuple and asserts the Stage-1
harness never imports it.

### Why both exist

`engine.py` was the first engine. It established the shape of the pipeline
(shared cash, signal at close T → fill at open T+1, integer shares, explicit
per-trade cost records) but encodes accounting choices that cannot be
reconciled with a defensible result:

- **Stale marks forever.** A holding with no bar on a session is marked at its
  last observed close indefinitely. Terminal wealth is therefore inflated and
  drawdown understated for any holding that stops trading. On the repository's
  own published `adjusted_2015_2026` run, ~89% of the reported final positions
  value was a stale mark (audit item B16a).
- **Raw prices by default.** `run_demo(adjusted=False)` ranks momentum on raw,
  split-distorted closes.
- **Slippage is charged to cash, not applied to the fill price**, while the
  inline comment says the opposite.
- **Overweight targets are never trimmed**, so "equal weight" is not equal
  weight.

`momentum.py` was written to fix exactly these: it refuses to invent a price,
makes the delisting treatment an explicit `DELISTING_POLICIES` choice, and
publishes its cost components as data. Because the shared loaders were already
good, they were left in `engine.py` and imported from there rather than copied.

**Rule for new work: add it to `momentum.py`.** Do not add new callers of
`RotationBacktester`. Fixing an accounting defect in `engine.py` is the wrong
place to work — the same defect space is being collapsed into `momentum.py`.

### Known state of the split

This is a documented, *deliberate* coexistence, not a completed merge. The
remaining work is to move the shared loader/identity-continuity helpers out of
`engine.py` into their own module, at which point `RotationBacktester` can be
deposed and `engine.py` retired along with the legacy tests that pin it. That
work is out of scope here because it changes behaviour in five live modules and
would need its own test plan.

---

## 3. Identity systems

There are two modules named like identity systems. They operate at **different
layers** and both are actively used, so neither was deprecated or shimmed.

| Module | Layer | Exports | Canonical for |
|---|---|---|---|
| `backtest/security_identity.py` | Bar-frame re-keying | `resolve_identity`, `DEFAULT_EDGES`, `IdentityResolution`, `load_symbol_edges` | **Backtests** |
| `backtest/identity.py` | Event-sourced ledger | `IdentityResolver`, `Observation`, `IdentityEdge`, `Retirement`, `SecurityKey`, `Resolution`, `Migration`, `IdentityIssue`, `IdentityLedger`, `resolve_history` | **Stage-1 accounting** |

`security_identity.resolve_identity` attaches a stable `ISIN:<isin>` /
`SYMBOL:<symbol>` key to an already-materialised bar DataFrame, following
official symbol-change edges transitively and refusing joins whose bar spans
overlap (symbol reuse). It is imported by `research/run_sweep.py`,
`research/run_alpha_backtest.py` and `backtest/momentum_grid.py`.

`identity.IdentityResolver` is an event-sourced state machine gated on
`EventTime`: an identity change may only take effect once its evidence was
**available**, and every migration and refusal is recorded in an auditable
ledger. It is imported by `backtest/observed_session.py`,
`tests/stage1_harness.py` and the `test_accounting_*` suite.

**Why no shim re-export.** Replacing `identity.py` with a re-export of
`security_identity` would not be a refactor; it would silently drop the
availability gate that the accounting ledger's causal guarantee depends on.
The two are genuinely different contracts. They were therefore documented
rather than collapsed.

---

## 4. Test map

Which tests cover which engine:

| Test file | Covers | Engine |
|---|---|---|
| `tests/test_backtest_momentum.py` | engine unit behaviour, costs, cadence | **momentum** |
| `tests/test_momentum_empty_universe.py` | empty universe edge case | **momentum** |
| `tests/test_leakage_causality.py` | no look-ahead; future-perturbation invariance | **momentum** |
| `tests/test_backtest_exits.py` | exit policies, via `momentum_grid` | **momentum** |
| `tests/test_backtest_cadence.py` | rebalance cadence selection | shared (`cadence.py`) |
| `tests/test_backtest_delisting.py` | delisting register / haircuts | shared (`delisting.py`) |
| `tests/test_backtester.py` | `RotationBacktester` core, costs, skips | **engine (legacy)** |
| `tests/test_backtest_causality.py` | `RotationBacktester` causality + identity continuity | **engine (legacy)** |
| `tests/test_history.py` | `history.py` runner, loaders | **engine (legacy)** |
| `tests/test_total_return.py` | `total_return.py` runner | **engine (legacy)** |
| `tests/test_successor.py` | `successor.py` runner | **engine (legacy)** |
| `tests/test_loader_causality.py` | `load_bars_from_duckdb`, `load_adjusted_bars` | **engine (legacy)** |
| `tests/test_accounting_identity.py` | `IdentityResolver`, `resolve_history`, ledger | **identity** |
| `tests/test_identity_api_edges.py` | API identity edges | **identity** |

Run only the engine-relevant tests:

```
$env:PYTHONPATH='src'
& 'D:\fix_Investment_backtesting\.venv\Scripts\python.exe' -m pytest -q `
    tests/test_backtest_momentum.py tests/test_momentum_empty_universe.py `
    tests/test_leakage_causality.py tests/test_backtest_exits.py `
    tests/test_backtester.py tests/test_backtest_causality.py
```

## 5. Expected failures in the public export

The export intentionally drops the `verification/` data tree, `data/` and the
DuckDB catalog. Tests that read those paths (for example
`test_nse_membership_evidence.py`, `test_verification_isolation.py`,
`test_backtest_delisting.py::TestRegister`) therefore fail or error at
collection/run time **before and after** this documentation change. That is a
property of the export, not a regression. Compare failure sets rather than
absolute counts when validating a change.
