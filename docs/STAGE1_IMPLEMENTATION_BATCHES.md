# Stage-1 Implementation Batches

**Status:** Batches 0–6 are implemented with revision-bound evidence; Batch 6 adds a test-only end-to-end harness as regression coverage and does NOT change any contract status; the gate stays CLOSED. The guardrails below (no legacy migration, no strategy/performance output) remain binding. This document itself never modifies production code, tests, goldens, datasets, manifests, inventory, verification artifacts or Git history.
**Baseline:** `f5e943000764fade242b0482d67db2808991c076` (`f5e9430`).
**Consumes:** `docs/STAGE1_REMEDIATION_MASTER_PLAN.md`, `docs/STAGE1_DECISION_REGISTER.md`, `docs/STAGE1_INDEPENDENT_TECHNICAL_AUDIT.md`, `docs/STAGE1_ACCOUNTING_VERIFICATION.md`, `docs/ARCHITECTURE.md`. **Contract authority:** `docs/STAGE1_P0_CONTRACT_MATRIX.md` defines the 29 canonical Stage-1 contract IDs and current statuses; `docs/STAGE1_REGRESSION_TEST_PLAN.md` defines the 71 canonical T-ID tests, their exact node names, inputs and expected values. This plan consumes those IDs and never creates an alternative vocabulary.
**Test-table convention:** the “Tests to add first” tables in §§5–10 are batch-local test intents, including batch tooling tests that have no T-ID (Batch 0) and finer-grained safety cases. The canonical planned regression test for any intent is the corresponding `T<batch>.<n>` entry in `docs/STAGE1_REGRESSION_TEST_PLAN.md`; where wording differs, the regression test plan governs.
## 1. Scope guardrail (applies to every batch)

1. **No legacy runner migration.** `src/nse_pipeline/backtest/{engine,history,sensitivity,series_fallback,successor,total_return,freeze}.py`, `src/get_data.py`, the DuckDB catalog and all legacy execution paths remain untouched.
2. **No strategy execution and no performance output.** No historical portfolio run, no return series, no benchmark, no risk statistic, no claim of alpha. Test fixtures are accounting and contract fixtures only.
3. **No dataset writes.** Nothing may create, delete, rename or rewrite a file under `data/`. The sole permitted future exception is `git add data/README.md` in Batch 4, which changes tracking, not bytes.
4. **Evidence before authorization.** Passing pytest is necessary, never sufficient. A mandatory contract only changes status with independent, revision-bound proof.
5. **Goldens are immutable oracles.** Existing `tests/golden/*.json` are never edited. New cases are additive, hand-derived and hashed before use.
6. **No silent resolution.** Where the decision register marks a decision `OPEN` or `BLOCKED`, or the owner has not approved it, this plan says `DECISION REQUIRED`. The unresolved questions `U-01`–`U-13` block their associated steps; `U-08` is RESOLVED by `D-03` and is retained only for traceability.

### 1.1 Status vocabulary used by every batch

| Status | Meaning | Authorizes the gate? |
|---|---|---|
| `COMPONENT_IMPLEMENTED` | Code exists; no independent verification. | No |
| `COMPONENT_VERIFIED` | Named component tests pass at the tested revision. | No |
| `CONTRACT_PARTIAL` | Some clauses proven; at least one missing. | No |
| `CONTRACT_PASS` | Every clause proven independently at the tested revision. | Only if every mandatory row is PASS |
| `CONTRACT_NOT_IMPLEMENTED` | No implementation, or a stub only. | No |
| `CONTRACT_FAIL` | Implementation contradicts its contract. | No |
| `CONTRACT_BLOCKED` | External evidence required but absent. | No |

At plan time `docs/STAGE1_P0_CONTRACT_MATRIX.md` recorded **6 `CONTRACT_PASS`, 6 `CONTRACT_PARTIAL`, 7 `CONTRACT_FAIL`, 2 `CONTRACT_BLOCKED` and 8 `CONTRACT_NOT_IMPLEMENTED`** across 29 rows; that plan-time vector is preserved as history. At the Batch 5 boundary the matrix records **6 PASS, 11 PARTIAL, 6 FAIL, 3 BLOCKED and 3 NOT_IMPLEMENTED**. This plan promotes none. A `CONTRACT_FAIL` row is not described as "not implemented"; a `COMPONENT_VERIFIED` suite is never treated as clause proof. Every row touched by a batch changes status only through the Batch 4 evidence model and a revision-bound proof.

### 1.2 Decision gates

| Batch | Decisions that must be owner-approved before code |
|---|---|
| 0 | None to freeze evidence; approvals for later batches are recorded here. |
| 1 | `D-01`–`D-05`, `D-25`; `U-01`, `U-02`. `U-08` is RESOLVED by `D-03` and is not a prerequisite. |
| 2 | `D-06`, `D-07`, `D-08`, `D-09`, `D-11` (validation part), `D-12`, `D-26`; `U-03`, `U-13` |
| 3 | `D-13`, `D-14`, `D-15`, `D-16`; `U-06`, `U-07` |
| 4 | `D-17`, `D-18`, `D-19`, `D-20`, `D-21`, `D-22` |
| 5 | `D-10`, `D-27`–`D-32`, plus approved Batch 1–4 decisions; `U-02`, `U-10`, `U-11`, `U-12`; `U-04`, `U-05` remain blocked and keep their contracts `CONTRACT_BLOCKED`; `U-09` only if an adjusted-price basis is in scope |
| 6 | All prior decisions; no new decision is permitted to open the gate. |

Unapproved decisions make the affected step `DECISION REQUIRED`; the batch may close only the unaffected sub-items and must remain `CONTRACT_PARTIAL`. `U-08` is retained only for traceability and is RESOLVED by `D-03`. `U-10`/`U-11` (partial payments and withholding) are out of Batch 1 scope; the `U-12`-dependent disclosure of a confirmed-but-unsettled claim is `DECISION REQUIRED`, and Batch 1 remains `CONTRACT_PARTIAL` until `U-12` is answered.
## 2. Dependency graph

```text
Batch 0  Baseline + evidence freeze
   |
   +--> Batch 1  Accounting correctness (F1, F3, F4, F5, replay)
   |       |
   |       +--> Batch 2  Causality/research semantics (F2, F6-F11, F15)
   |               |
   |               +--> Batch 3  Interface/spec coherence (F12-F14, F21)
   |                       |
   |                       +--> Batch 4  Verification hardening (F16-F20, F22-F23)
   |                               |
   |                               +--> Batch 5  Outstanding P0 domain contracts
   |                                       |
   |                                       +--> Batch 6  End-to-end causal verification
```

**Graph rules.** Batch 1 may not merge before Batch 0 closes. Batch 2 may not close before Batch 1 freezes money/rounding semantics. Batch 3 may not close before the Batch 1 fee contract and Batch 2 signal/ADV field decisions are frozen. Batch 4 may be developed from Batch 0, but its row reconciliation closes after Batch 3. Batch 5 may be prototyped after Batch 2 in isolated worktrees, but merges only after Batch 4. Batch 6 merges last and may not patch production code to make its tests pass.

**Strict authorization path:** `0 → 1 → 2 → 3 → 4 → 5 → 6`.
**Technical critical path:** `0 → 1 (F5 claim lifecycle) → 5 (continuity/lifecycle) → 6`; Batches 2–4 are breadth and governance work that can expose blockers but do not shorten the domain chain.
## 3. Critical path and parallelizable work

- **Lane A (Batch 0)** starts and merges first. **Lane B (Batch 1)** follows A and the D-01..D-05/D-25 approvals. **Lane C (Batch 2)** follows B's `money()`/cap freeze. **Lane D (Batch 3)** may author mapper tests after B and merges after C. **Lane E (Batch 4)** starts after A; row alignment follows D. **Lane F (Batch 5)** prototypes after C in worktrees and merges after E. **Lane G (Batch 6)** authors after F interfaces stabilize and merges last.
- Within a batch, independent modules may proceed concurrently. **No batch may waive an earlier batch's stop condition.** Parallel prototypes must not write to `data/`, must not edit existing goldens, and must not be represented as verification evidence until merged and re-run at the batch commit.
## 4. Batch 0 — Baseline and evidence freeze

**Objective.** Establish a revision-bound reproduction record for `f5e9430`, freeze the evidence and golden hashes, and record owner decisions. No source, test, golden or dataset change.

**Prerequisites.** None. Read `docs/STAGE1_DECISION_REGISTER.md`; confirm `git rev-parse HEAD` is `f5e9430`.

**In-scope files.**
- `scripts/freeze_stage1_baseline.py` (new, read-only outside `verification/`)
- `scripts/reproduce_stage1_findings.py` (new, probes F1/F2/F3/F5 only)
- `tests/test_stage1_baseline_freeze.py` (new)
- `verification/stage1/baseline-f5e9430/manifest.json`, `pytest-full.xml`, `inventory.json`, `golden-sha256.json`, `reproduction.json`, `decision-gates.json`
- `verification/stage1/index.json` (new append-only evidence index)
- `docs/STAGE1_DECISION_REGISTER.md` (record approvals only; no recommendation rewrites)

**Out of scope.** Every file under `src/`, `tests/` other than the new test, `tests/golden/`, `data/`, existing `verification/*.xml|json`, and all legacy modules.

**Implementation order (tests first).**
1. Write `tests/test_stage1_baseline_freeze.py` and run it red (script absent).
2. Add `scripts/freeze_stage1_baseline.py`: reads Git state, hashes tracked source and the three goldens, invokes no tests, refuses output under `data/`.
3. Add `scripts/reproduce_stage1_findings.py`: F1 cap matrix, F2 two-callable probe, F3 fully-priced book, F5 `hold_receivable` claim; writes JSON only.
4. Run the full suite and inventory verifier, writing output to the baseline directory.
5. Compare counts, hashes and findings with the audit; write `manifest.json` and `reproduction.json`.
6. Record `U-01`–`U-13` and batch decision approvals in `decision-gates.json`; `U-08` is recorded as `RESOLVED` by `D-03`.

**Tests to add first.**

| Test | Expected failure before tooling |
|---|---|
| `test_freeze_manifest_binds_commit_and_worktree_state` | `ImportError`/`FileNotFoundError` |
| `test_freeze_refuses_output_under_data` | no guard exists |
| `test_freeze_hashes_existing_goldens_without_changing_them` | no hash record exists |
| `test_freeze_records_full_suite_counts_and_skip_names` | no baseline record exists |
| `test_reproduction_records_f1_f2_f3_f5` | no probe script exists |

**Verification commands (PowerShell, repo-relative).**
```powershell
uv run --frozen python -m pytest tests/test_stage1_baseline_freeze.py -q --no-header -p no:cacheprovider
uv run --frozen python scripts/freeze_stage1_baseline.py --out verification/stage1/baseline-f5e9430
uv run --frozen --extra pdf python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/stage1/baseline-f5e9430/pytest-full.xml
uv run --frozen python scripts/verify_stage1_inventory.py --out verification/stage1/baseline-f5e9430/inventory.json
uv run --frozen python scripts/reproduce_stage1_findings.py --out verification/stage1/baseline-f5e9430/reproduction.json
git diff --check
uv lock --check
```
**Expected artifacts and schema.** `manifest.json`: `schema_version`, `tested_commit`, `head_subject`, `worktree_state`, `dirty_paths`, `source_sha256`, `golden_sha256`, `inventory_sha256`, `environment`, `test_counts`, `skip_ids`, `decisions`; `reproduction.json`: `findings[]` each with `id`, `status`, `command`, `observed`, `expected_contract`, `artifact_sha256`; `index.json`: append-only list of `{path, sha256, tested_commit, generated_at, status}`.

**Done criteria.**
- `tested_commit == f5e943000764fade242b0482d67db2808991c076`; no tracked file under `src/`, `tests/`, `scripts/`, `verification/` or `data/` is modified. The sole permitted tracked delta is the planning-pass relocation of `QUANT_REPOSITORY_AUDIT.md`: `git hash-object docs/QUANT_REPOSITORY_AUDIT.md` must equal `git rev-parse HEAD:QUANT_REPOSITORY_AUDIT.md`, and that pair is recorded in `manifest.json`. Every untracked path (planning docs, `data/README.md`, `src/nse_pipeline/analytics/`) is enumerated and hashed in `manifest.json`, and none is imported or modified by Batch 0 tooling.
- Full-suite counts and skip names reproduce the audit; any difference is explained in `manifest.json`.
- F1/F2/F3/F5 reproduce with machine-readable evidence; no finding is marked fixed.
- Existing golden files hash to their committed values; inventory `checked_files=8153`, zero errors, zero unexpected changes, one acknowledged exception.
- `decision-gates.json` records every approval as `APPROVED`/`PENDING`; no later batch starts on a pending decision.
- Gate remains `CLOSED`; no contract row changes.

**Stop conditions / rollback.** Stop if HEAD differs, if tracked source is dirty, if an existing golden hash differs, if inventory shows an unexpected change, or if the audit counts cannot be reproduced. Rollback: delete the new `verification/stage1/` directory, scripts and test; no tracked file or dataset needs restoration.
## 5. Batch 1 — Accounting correctness

**Objective.** Fix F1 with a paise-floored principal cap, F5 with a causal dividend confirmation/void lifecycle, F3 with the six-state reference taxonomy and separate `nav_reason`, and F4 with exact `nav` plus presentation-only `nav_reported`; harden the independent replay so it cannot share a production rounding defect.

**Contract IDs.** `P0-FEES` and `P0-CAPACITY` (cap clause only) for F1; `P0-DIVIDEND` for F5; `P0-COVERAGE` for F3; `P0-NAV-GAP` and `P0-LIQUIDATION` for F4's precision wording; `P0-CONSERVATION` for the independent replay. No other contract is touched.

**Prerequisites.** Batch 0 closed. Approved `D-01`, `D-02`, `D-03`, `D-04`, `D-05`, `D-25` and `U-01`, `U-02`; `U-08` is RESOLVED by `D-03` and is not a prerequisite. If any listed decision is pending, the affected item is `DECISION REQUIRED` and Batch 1 stays open.

**In-scope files.**
- `src/nse_pipeline/backtest/fees.py` (`principal_cap` and `within_notional_cap` helpers, both buy and sell, record `applied_principal_cap`)
- `src/nse_pipeline/backtest/accounting_types.py` (claim lifecycle fields and `DIVIDEND_PAYMENT_CONFIRMED`/`DIVIDEND_VOIDED` event kinds)
- `src/nse_pipeline/backtest/valuation.py` (six-state reference reason, separate `nav_reason`, `unresolved_settlement_claims`, `nav_reported`)
- `src/nse_pipeline/backtest/dividends.py` (payment confirmation/void admissibility and causal due determination)
- `src/nse_pipeline/backtest/core.py` (`confirm_dividend_payment`, `void_dividend`, settlement interaction)
- `src/nse_pipeline/backtest/invariants.py` (independent `principal_cap`/`within_notional_cap` and confirmation/void replay)
- `tests/test_accounting_fees.py`, `test_accounting_core.py`, `test_accounting_dividends.py`, `test_accounting_valuation.py`
- `tests/test_accounting_replay.py` (new direct replay suite)
- `tests/golden/accounting_edge_cases.json` (new, additive)
- `docs/ARCHITECTURE.md` (precision paragraph only)
- `verification/stage1/batch1/*`

**Out of scope.** `signals.py`, `adv.py`, `universe.py`, `boundary.py`, `evidence.py`, `specs/__init__.py`, the report scripts, all existing goldens, datasets; partial-payment/withholding semantics (`U-10`), voiding opening-balance claims (`U-11`) and retroactive settlement of `due=None`.

**Implementation order (tests first).**
1. Author the hand-derived edge golden and failing cap tests: `principal_cap` floors the declared cap to paise at use, `applied_principal_cap` is recorded, both buy and sell obey it, fees do not expand it, and the replay mutation detects the old half-up path.
2. Author the failing B1–B8 eight-book reference/NAV table covering the six reference states and the separate four-value `nav_reason`.
3. Add the failing exact-`nav`/`nav_reported` test; `nav` is authoritative and `nav_reported` is presentation-only.
4. Add failing dividend confirmation/void tests: causal due date, no cash/NAV movement on confirmation, terminal void requiring verified evidence, a nonempty reason and a write-off, opening-balance `due=None` non-confirmability, duplicate/conflicting evidence rejection, and `unresolved_settlement_claims`.
5. Implement `principal_cap` and `within_notional_cap` per `D-01` (principal only; never change `money()` globally); use both on the buy and sell paths, record `applied_principal_cap`, and mirror the cap independently in `invariants.py`.
6. Implement the six-state reference reason and separate `nav_reason` with explicit precedence per `D-25`.
7. Implement `nav` as the exact authoritative Decimal and `nav_reported = money(nav)` at the presentation boundary only per `D-03`.
8. Implement `confirm_dividend_payment`/`DIVIDEND_PAYMENT_CONFIRMED` and `void_dividend`/`DIVIDEND_VOIDED` per `D-04`/`D-05`; derive due causally, expose `unresolved_settlement_claims`, and update replay.
9. Run the hand-derived edge golden and the old eight cases unchanged; update the architecture precision paragraph.

**Tests to add first.**

| Test | Expected failure at baseline |
|---|---|
| `test_principal_cap_floors_99_996_to_99_99_and_records_it` | cap is half-up; `applied_principal_cap` absent |
| `test_cap_99_996_buy_never_admits_100_00` | 10 shares at 10.00 = 100.00 admitted |
| `test_cap_99_996_sell_never_admits_100_00` | same over-cap sale admitted |
| `test_cap_does_not_bound_fees` | pins `D-02`; cash budget must bind separately |
| `test_replay_rejects_fill_above_floored_cap` | replay uses half-up and passes |
| `test_eight_book_reference_and_nav_state_table` | fall-through expression conflates states |
| `test_reference_coverage_reason_is_a_closed_six_state_vocabulary` | two states collapse into one label |
| `test_nav_reason_is_separate_from_reference_coverage_reason` | `nav_reason` absent |
| `test_exact_nav_is_authoritative_and_nav_reported_is_presentation_only` | `nav_reported` absent |
| `test_hold_receivable_can_be_confirmed_with_later_verified_evidence` | `confirm_dividend_payment` absent |
| `test_confirm_dividend_payment_rejects_unavailable_conflicting_duplicate_or_post_void_evidence` | operation absent |
| `test_void_dividend_is_terminal_and_requires_verified_evidence_reason_and_writeoff` | operation absent |
| `test_opening_balance_due_none_claim_is_not_confirmable` | no confirmability guard |
| `test_confirmed_payment_does_not_move_cash_or_change_nav` | no confirmation lifecycle |
| `test_valuation_discloses_unresolved_settlement_claims_at_known_face_value` | fields absent |
| `test_reconcile_reports_every_named_check_on_a_golden_case` | no direct replay file |

**Eight-book reference/NAV matrix (B1–B8).** The table is authored before implementation and each row is asserted as a whole; `nav` is `None` whenever any position is unresolved or any financial asset is unknown.

| Book | Fixture | `reference_coverage_reason` | `nav_reason` |
|---|---|---|---|
| B1 | Every holding has a current-session, non-blocked close | `complete_current_date` | `nav_available` |
| B2 | No holdings; settled cash only | `no_positions` | `nav_available` |
| B3 | No current marks; every holding has an admissible dated reference | `dated_reference_only` | `unpriced_positions` |
| B4 | Some holdings have current marks; the rest have dated references | `mixed_current_and_dated_reference` | `unpriced_positions` |
| B5 | At least one unresolved holding has no admissible reference; aggregate reference and gross are `None` | `missing_reference` | `unpriced_positions` |
| B6 | Holdings exist and `gross == 0`; no priced or referenced notional | `zero_gross_notional` | `unpriced_positions` |
| B7 | Every holding is priced, but an unrelated financial asset/claim has an unknown amount | `complete_current_date` | `unknown_assets` |
| B8 | At least one holding is unpriced and an unrelated financial asset/claim has an unknown amount | `dated_reference_only` | `unpriced_positions_and_unknown_assets` |

**Verification commands.**
```powershell
uv run --frozen python -m pytest tests/test_accounting_fees.py tests/test_accounting_core.py tests/test_accounting_dividends.py tests/test_accounting_valuation.py tests/test_accounting_replay.py -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch1/focused.xml
uv run --frozen --extra pdf python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch1/regression.xml
uv run --frozen python scripts/verify_stage1_inventory.py --out verification/stage1/batch1/inventory.json
uv run --frozen python scripts/report_stage1_accounting.py --focused verification/stage1/batch1/focused.xml --regression verification/stage1/batch1/regression.xml --inventory verification/stage1/batch1/inventory.json --out verification/stage1/batch1/component-results.json
```
**Expected artifacts.** `verification/stage1/batch1/{summary.json,focused.xml,regression.xml,inventory.json,component-results.json,golden-diff.json}`. `summary.json` records the tested commit, each fixed finding, the failing-before/passing-after test IDs, and the hash of the new golden.

**Done criteria.**
- New cap tests fail at the Batch 1 parent commit and pass at Batch 1 head for both buy and sell; the replay rejects the old half-up rule.
- `principal_cap` floors the declared cap to paise at use (`99.996` becomes `99.99`, never `100.00`; `99.994`/`99.99` permit `99.99`) and `applied_principal_cap` is recorded; `within_notional_cap` bounds principal only; a `100.00` cap is inclusive; `0` admits nothing; `None` is unbounded; fees never expand the cap.
- The B1–B8 matrix returns exactly one of the six reference states (`no_positions`, `missing_reference`, `zero_gross_notional`, `complete_current_date`, `dated_reference_only`, `mixed_current_and_dated_reference`) and a separate `nav_reason` (`nav_available`, `unpriced_positions`, `unknown_assets`, `unpriced_positions_and_unknown_assets`); `missing_reference` preserves `known_reference_subtotal`; `unresolved_settlement_claims` never changes the reference reason; NAV arithmetic is unchanged.
- `nav` is the exact authoritative Decimal; `nav_reported = money(nav)` exists only at the presentation boundary and is never an input to a later event; no per-event rounding drift occurs.
- An unscheduled dividend can be confirmed with later verified evidence, acquire a causal due date and settle; NAV includes the known face value; `unresolved_settlement_claims` lists the claim until confirmation. Whether a confirmed-but-unsettled claim remains in that disclosure is `DECISION REQUIRED` under `U-12` and is not a Batch 1 pass criterion until answered. `void_dividend` is terminal and requires verified evidence, a nonempty reason and a write-off; an opening-balance claim with `due=None` is non-confirmable; confirmation moves no cash and changes no NAV; no retroactive cash appears.
- Existing eight accounting cases and all three existing golden files are byte-identical and pass; replay cannot import `core`, `fees` or `valuation`.
- Report remains `overall_p0_gate=CLOSED`; no contract row is promoted.

**Stop conditions / rollback.** Stop if `D-04`/`D-05` choose a different claim lifecycle, if a fix requires changing `money()` globally, if an existing golden must change, if partial-payment/withholding semantics (`U-10`) or voiding opening-balance claims (`U-11`) are required, or if a confirmed claim would settle with `due=None`. Rollback: revert the batch commit; new golden and evidence files are additive and can be removed; no data rollback is expected because no batch command writes under `data/`.
## 6. Batch 2 — Causality and research semantics

**Objective.** Make provenance truthful with a mandatory `basis_id` (F2), freeze window-scoped momentum observations and dispatch the three single-value momentum fields (F6, F7), freeze ADV semantics (F9), make universe limitations explicit (F8), add month-omission diagnostics and calendar-authority validation (F10, F11), and strengthen `Bar` coherence (F15). Batch 2 adds calendar validation/provenance only; the verified published-exchange adapter belongs to Batch 5.

**Contract IDs.** `P0-SIGNALS` (F2, F6, F7), `P0-ADV` and `P0-CAPACITY` (F9 coverage/capacity clause), `P0-UNIVERSE` (F8 label/reasons), `P0-BOUNDARY` (F10/F11), `P0-EVIDENCE` (F6/F15 counts and bar quality). No row is promoted by component tests alone.

**Prerequisites.** Batch 1 closed. Approved `D-06`, `D-07`, `D-08`, `D-09`, `D-11` (validation only), `D-12`, `D-26`; `U-03`, `U-13`. `U-04` remains `BLOCKED` and is a Batch 5 prerequisite; only `published_exchange` calendars may fire, but Batch 2 does not supply the authoritative adapter.

**In-scope files.**
- `src/nse_pipeline/backtest/signals.py` (`BasisProvenance` with mandatory `basis_id`, window/history counts, `MomentumSpec` schema 2, single-value dispatch for `anchor_rule`/`observation_rule`/`tie_break`)
- `src/nse_pipeline/backtest/adv.py` (absence truth table, coverage, capacity fail-closed)
- `src/nse_pipeline/backtest/universe.py` (`RESEARCH_ELIGIBILITY_ONLY`, distinct reasons)
- `src/nse_pipeline/backtest/boundary.py` (`MonthOmission`, calendar authority/provenance validation; only `published_exchange` may fire)
- `src/nse_pipeline/backtest/observations.py` (`bar_quality`, OHLC coherence)
- `src/nse_pipeline/backtest/evidence.py` (window/history counts, censoring)
- `src/nse_pipeline/specs/__init__.py` (only additive fields required by D-06/D-08/D-09/D-26, including `MomentumSpec` schema 2 and basis identity)
- `tests/test_accounting_signals.py`, `test_accounting_adv.py`, `test_accounting_universe.py`, `test_accounting_boundary.py`, `test_accounting_observations.py`, `test_accounting_evidence.py`
- `tests/test_accounting_absence_contract.py` (new truth-table suite)
- `tests/golden/research_semantics_cases.json`, `tests/golden/absence_cases.json` (new)
- `verification/stage1/batch2/*`

**Out of scope.** `fees.py`, `core.py`, `valuation.py`, `dividends.py`, `invariants.py`, `sizing.py`, `PortfolioSpec`, the report scripts, existing goldens, any authoritative index-membership or exchange-calendar data source, and the verified calendar adapter (Batch 5).

**Implementation order (tests first).**
1. Author the two hand-derived golden files, then add provenance tests for built-in bases, two different callables, explicit `basis_id`, closure/lambda/stateful callables without `basis_id`, unavailable digest, and mutated spec label.
2. Add window-scoped momentum tests (`None` = full window), diagnostic-only history counts, schema-2 rejection of v1 payloads, and dispatch tests for `anchor_rule`/`observation_rule`/`tie_break`.
3. Add the full ADV absence truth table and never-seen-key capacity test.
4. Add universe label/reason tests; do not claim PIT membership.
5. Add zero-session-month omission/late-catch-up and calendar-authority tests: only `published_exchange` may fire; caller-asserted/inferred are diagnostic and cannot be registered; weekends/holidays only as listed special sessions.
6. Add `Bar` quality and OHLC negative tests.
7. Implement in the same order; keep every as-of query append-invariant.

**Tests to add first.**

| Test | Expected failure at baseline |
|---|---|
| `test_two_different_callables_have_different_basis_provenance` | both record `"close"` |
| `test_basis_id_is_mandatory_for_closures_lambdas_and_stateful_callables` | no `basis_id` requirement |
| `test_callable_provenance_uses_custom_basis_id_never_the_spec_label` | mutated `"bogus_basis"` recorded |
| `test_unavailable_digest_is_explicit_not_fabricated` | no provenance record |
| `test_min_observations_is_window_scoped_and_none_requires_full_window` | whole-history count admits a 15-history / 3-window key |
| `test_history_valid_observations_is_diagnostic_only` | no separate diagnostic count |
| `test_momentum_schema_1_payload_is_rejected_with_migration_error` | no schema version |
| `test_anchor_observation_and_tie_break_fields_select_their_dispatch` | fields are inert |
| `test_never_seen_key_adv_is_unknown_not_zero` | returns `0.00` |
| `test_missing_bar_and_explicit_zero_bar_are_distinguished` | both contribute zero silently |
| `test_capacity_is_none_below_minimum_observed_coverage` | capacity ignores coverage |
| `test_universe_is_labelled_research_eligibility_only` | no label |
| `test_suspended_gapped_and_absent_keys_have_distinct_reasons` | all `no_bar_on_session` |
| `test_zero_session_month_emits_omission_record` | no record |
| `test_omission_does_not_synthesise_a_signal` | current silent behavior |
| `test_only_published_exchange_calendar_may_fire_a_signal` | caller-asserted calendar fires |
| `test_caller_asserted_or_inferred_calendar_cannot_be_registered` | no authority field |
| `test_published_exchange_calendar_rejects_weekend_unless_special_session` | Saturday accepted |
| `test_bar_quality_rejects_incoherent_ohlc` | all-None `valid_ohlcv=True` accepted |
**Verification commands.**
```powershell
uv run --frozen python -m pytest tests/test_accounting_signals.py tests/test_accounting_adv.py tests/test_accounting_universe.py tests/test_accounting_boundary.py tests/test_accounting_observations.py tests/test_accounting_evidence.py tests/test_accounting_absence_contract.py -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch2/focused.xml
uv run --frozen --extra pdf python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch2/regression.xml
uv run --frozen python scripts/verify_stage1_inventory.py --out verification/stage1/batch2/inventory.json
git diff --name-only -- tests/golden
```
**Expected artifacts.** `verification/stage1/batch2/{summary.json,focused.xml,regression.xml,inventory.json,golden-diff.json}` plus the two new golden files. `summary.json` records each changed semantic with the decision ID and before/after expected values.

**Done criteria.**
- `BasisProvenance` is emitted with `kind`, `basis_id`, `name`, optional `version`, `source_digest` and `parameters`; `basis_id` is mandatory for closures, lambdas, partials and stateful callables; built-ins keep `price_basis`; callables emit `custom:<basis_id>`, never the built-in spec label; unavailable digests are `None`, not invented; two behaviourally different callables never share provenance.
- `min_observations` is window-scoped over `[anchor, as_of]`; `None` requires every session in the full window; `valid_observations` is the window count and `history_valid_observations` is diagnostic only and never admits a short-window key; `MomentumSpec` schema 2 rejects v1 payloads with an explicit migration error; `anchor_rule`, `observation_rule` and `tie_break` are consulted as single-value dispatch, and a second value is a schema error.
- The ADV truth table matches `D-09` row for row; a never-seen key returns unknown; capacity returns `None` when coverage is insufficient.
- Universe output is explicitly `RESEARCH_ELIGIBILITY_ONLY`; no membership is inferred from absence; `P0-UNIVERSE` remains `CONTRACT_BLOCKED` because authoritative membership evidence is absent.
- Every skipped month produces a `MonthOmission`; no signal is synthesized; late evidence retains `late=True`.
- Only a `published_exchange` calendar with `complete_months`, `source_sha256` and `coverage_end` may fire; caller-asserted/inferred calendars are diagnostic and cannot be registered; weekends/holidays are admissible only as listed special sessions; `P0-BOUNDARY` remains `CONTRACT_BLOCKED` until the Batch 5 verified adapter exists.
- Existing golden files are byte-identical; append-invariance tests for every as-of query pass.

**Stop conditions / rollback.** Stop if `D-06`/`D-07`/`D-08`/`D-09`/`D-11`/`D-12`/`D-26` are unapproved, if a test requires an authoritative holiday/membership source, if a caller-asserted or inferred calendar would be allowed to fire, if a momentum single-value field would need a second value, or if a semantic change would require editing an existing golden. Rollback: revert the batch commit; additive golden files can be removed; no dataset rollback needed.
## 7. Batch 3 — Interface and specification coherence

**Objective.** Remove dead/incoherent specification surface: make long `PortfolioSpec` names canonical with short `P1`/`P2`/`P3` aliases only at the sizing boundary (F12), enforce cross-policy validation (F13), classify all 27 fields with the four-state `D-16` registry and drive `DISCONNECTED` to zero (F14), and make P1/P2/P3 goldens discriminating (F21).

**Contract IDs.** `P0-SIZING` and `P0-POLICIES` for F12/F13/F21; `P0-SIGNALS` (disposition proof only) and `P0-EXECUTION` (reserved execution fields) for F14.

**Prerequisites.** Batches 1 and 2 closed. Approved `D-13`, `D-14`, `D-15`, `D-16`; `U-06`, `U-07`. `D-07` was closed in Batch 2; momentum fields are `IMPLEMENTED` here, not reserved.

**In-scope files.**
- `src/nse_pipeline/specs/__init__.py` (long-name canonical vocabulary, `resolve_sizing_policy`, short-alias table at the sizing boundary)
- `src/nse_pipeline/specs/validation.py` (new `validate_run_specs`)
- `src/nse_pipeline/specs/consumption.py` (new machine-readable four-state disposition registry over all 27 fields)
- `src/nse_pipeline/backtest/sizing.py` (accept short aliases only at the boundary and normalize once; P3 screen input; `size_portfolio_orders` as the only `PortfolioSpec` entry point)
- `tests/test_specs.py`, `tests/test_accounting_sizing.py`
- `tests/test_spec_consumption.py` (new)
- `tests/golden/policy_cases.json` (new; existing `execution_cases.json` untouched)
- `docs/ARCHITECTURE.md` (spec surface only)
- `verification/stage1/batch3/*`

**Out of scope.** Accounting arithmetic, valuation, dividends, signals/ADV semantics beyond disposition reporting, the report scripts, existing goldens, `PortfolioSpec` integration into a production runner (none exists and none is authorized), and any change that reserves an already-implemented momentum field.

**Implementation order (tests first).**
1. Add failing resolver tests: long names are canonical, the default long name resolves, short `P1`/`P2`/`P3` are accepted only at the `size_orders` boundary, unknown values fail closed, and `size_orders` does not accept a `PortfolioSpec` directly.
2. Add failing cross-policy tests: P3 without `sizing_uses_screen_value=True` must fail before sizing.
3. Add failing P1/P2/P3 goldens with one unresolved rank; assert pairwise different fills.
4. Add failing P3 screen-value and screen-price test that cannot be satisfied by ordinary cash sizing.
5. Add the four-state disposition registry test over all 27 `Specification` fields, including the exact 10/12/0/5 counts and the bounded `DISCONNECTED` allowlist.
6. Implement `resolve_sizing_policy`/`ALIASES`, then `size_portfolio_orders` and the validation entry point, then P3 screen input, then goldens, then the registry; correct the `test_accounting_sizing.py` docstring to name only the goldens that exist.
7. Drive every `DISCONNECTED` field to an implemented reader or an approved reserved/obsolete disposition; the configuration row cannot pass while `disconnected > 0`.

**Tests to add first.**

| Test | Expected failure at baseline |
|---|---|
| `test_long_portfolio_policy_is_canonical_and_default_resolves` | default long name raises `ValueError` |
| `test_short_policy_aliases_are_accepted_only_at_sizing_boundary` | no resolver |
| `test_unknown_policy_name_is_rejected_not_coerced` | no canonicalizer |
| `test_size_orders_does_not_accept_portfolio_spec_directly` | no boundary rule |
| `test_p3_without_screen_flag_fails_before_sizing` | validator test-only |
| `test_p1_freezes_round_when_one_rank_is_unresolved` | no P1 golden |
| `test_p2_excludes_the_same_unresolved_rank` | P1/P2 not compared |
| `test_p3_screen_value_changes_allocation_from_p2` | P3 test non-discriminating |
| `test_p3_screen_value_cannot_create_cash` | screen input absent |
| `test_every_spec_field_has_one_of_four_disposition_states` | no registry |
| `test_disposition_counts_are_10_implemented_12_reserved_0_obsolete_5_disconnected` | no counts |
| `test_momentum_fields_are_implemented_not_reserved` | no registry |
| `test_reserved_field_has_exactly_one_legal_value` | no registry |
| `test_implemented_field_has_a_production_reader_and_effect_test` | no registry |
| `test_disconnected_field_allowlist_is_bounded_and_blocks_configuration_pass` | no allowlist |
| `test_disconnected_count_reaches_zero_before_configuration_pass` | no zero gate |
**Verification commands.**
```powershell
uv run --frozen python -m pytest tests/test_specs.py tests/test_accounting_sizing.py tests/test_spec_consumption.py -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch3/focused.xml
uv run --frozen --extra pdf python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch3/regression.xml
uv run --frozen python scripts/verify_stage1_inventory.py --out verification/stage1/batch3/inventory.json
git diff --name-only -- tests/golden
```
**Expected artifacts.** `verification/stage1/batch3/{summary.json,focused.xml,regression.xml,inventory.json,config-consumption.json}` and `tests/golden/policy_cases.json` with hand-worked `working` fields. `config-consumption.json` records all 27 fields, the four-state counts, the bounded `DISCONNECTED` allowlist and the baseline 10/12/0/5 snapshot.

**Done criteria.**
- Long `PortfolioSpec` names are canonical in specs, goldens and `SizingResult.policy`; short `P1`/`P2`/`P3` are accepted only at the `size_orders` boundary and normalized once; the long default resolves; unknown values fail closed; no public serialized short code exists.
- `size_portfolio_orders` is the only `PortfolioSpec` entry point; `size_orders` rejects a `PortfolioSpec`; `validate_run_specs` runs before sizing; P3 without `sizing_uses_screen_value=True` fails before any order.
- P1, P2 and P3 produce pairwise different `filled` tuples on the same unresolved-rank fixture; P3 uses an explicit caller screen value and screen price, is cash-capped, and cannot create cash.
- The registry covers all 27 fields, each in exactly one of `IMPLEMENTED`, `RESERVED`, `OBSOLETE`, `DISCONNECTED`; the baseline is **10 IMPLEMENTED / 12 RESERVED / 0 OBSOLETE / 5 DISCONNECTED**; momentum fields are `IMPLEMENTED`; each reserved field has one legal value and a no-runtime-effect test; each implemented field has a production reader and an effect test; each disconnected field has a bounded allowlist entry with owner, issue and expiry.
- `disconnected == 0` is required before the configuration row can pass; Batch 3 records the post-wiring counts and does not promote that row while any disconnected field remains.
- The sizing docstring names only existing goldens; the old `execution_cases.json` is byte-identical.
- No contract row is promoted by component tests; `P0-IDENTITY`, `P0-SIGNALS`, `P0-UNIVERSE`, `P0-ADV`, `P0-BOUNDARY` and `P0-EVIDENCE` retain the status recorded by the Batch 4 evidence model until clause-level proof exists.

**Stop conditions / rollback.** Stop if `U-06` makes short codes canonical or public, if `U-07` rejects an explicit P3 screen value or screen price, if a long name cannot round-trip, if any existing spec JSON no longer round-trips, or if a `DISCONNECTED` field is reclassified merely to lower the count. Rollback: revert the batch commit; the new policy golden and disposition registry are additive; no accounting or dataset change.
## 8. Batch 4 — Verification hardening

**Objective.** Make evidence revision-bound (F16, F18), align the machine report with the contract status model (F17, F20), define the skip policy (F19), close the inventory reproducibility gap (F22), and correct the stale limitation statement (F23). The report's 28-row `MANDATORY` set must gain the derived `P0-E2E` row, making 29, before any authorization attempt.

**Contract IDs.** `P0-VERIFY` (F16, F18, F19, F22), `P0-GATE` (F17, F20, 29-row mandate), and the `P0-E2E` row insertion. Batch 4 does not prove `P0-E2E`; Batch 6 adds only test-only end-to-end regression coverage and does not advance its contract status.

**Prerequisites.** Batch 0 closed. Approved `D-17`–`D-22`. Row reconciliation requires Batches 1–3 closed; the shared evidence machinery may be built immediately after Batch 0.

**In-scope files.**
- `src/nse_pipeline/stage1_evidence.py` (new: revision binding, inventory state machine, status model)
- `scripts/report_stage1_accounting.py` (derive rows from status model; correct mappings)
- `scripts/verify_stage1_inventory.py` (shared state machine; CLI/report agreement)
- `scripts/check_stage1_evidence.py` (new read-only validator)
- `tests/test_accounting_report.py`, `tests/test_inventory_verification.py`, `tests/test_verification_isolation.py`
- `tests/test_stage1_evidence.py` (new)
- `docs/STAGE1_ACCOUNTING_VERIFICATION.md` (dated addendum correcting F19/F23 and aligning the matrix)
- `data/README.md` (tracking only, bytes unchanged, if `D-22` option A)
- `verification/stage1/batch4/*`, `verification/stage1/index.json`
- `verification/foundation-postcommit.json`, `accounting-posttests.json`, `accounting-focused.xml`, `accounting-regression.xml`, `accounting-component-results.json` must remain untouched and be marked `SUPERSEDED` in the index.

**Out of scope.** Any economic rule, any accounting module, any existing golden, any dataset content, any rewrite of historical verification artifacts.

**Implementation order (tests first).**
1. Add failing revision-binding tests: mismatched HEAD, stale JUnit, dirty fingerprint, missing golden hash.
2. Add failing inventory state-machine tests for all eight `D-18` states and CLI/report parity.
3. Add failing status-model tests: component rows cannot authorize; deferred rows stay non-PASS; the 29-row mandatory set includes `P0-E2E`; real test modules are named.
4. Add failing skip-policy tests: mandatory skip, optional PDF skip, separate counts.
5. Add the clean-clone/prerequisite test for `data/README.md`.
6. Implement `stage1_evidence.py`, then the inventory CLI, then the report, then docs, then the tracking/prerequisite step.
7. Regenerate JUnit and inventory at the batch commit; generate the report; run the evidence checker.

**Tests to add first.**

| Test | Expected failure at baseline |
|---|---|
| `test_report_rejects_junit_from_another_commit` | report labels baseline while running at HEAD |
| `test_report_requires_dirty_fingerprint_for_dirty_tree` | no dirty fingerprint |
| `test_report_requires_current_golden_and_inventory_hashes` | hashes not compared |
| `test_clean_inventory_is_integrity_ok` | `only_accepted_exception=false` fails |
| `test_authorized_exception_absent_is_recorded_and_ok` | state absent |
| `test_cli_and_report_share_one_integrity_state` | disagree on clean tree |
| `test_component_row_never_authorizes_integration` | deferred/component rows not modelled |
| `test_mandatory_report_has_29_rows_including_e2e` | report unions 28 rows and omits `P0-E2E` |
| `test_report_maps_sizing_budget_to_sizing_tests` | maps to `core`/`fees` |
| `test_report_names_real_test_module_for_each_component` | hardcoded rows |
| `test_mandatory_skip_blocks_authorization` | no skip classification |
| `test_optional_pdf_skip_is_disclosed_not_counted_as_pass` | no mandatory/optional split |
| `test_every_inventoried_file_is_tracked_or_declared_prerequisite` | `data/README.md` untracked |
| `test_existing_verification_artifacts_are_not_overwritten` | no append-only check |
**Verification commands.**
```powershell
uv run --frozen python -m pytest tests/test_accounting_report.py tests/test_inventory_verification.py tests/test_verification_isolation.py tests/test_stage1_evidence.py -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch4/focused.xml
uv run --frozen --extra pdf python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch4/regression.xml
uv run --frozen python scripts/verify_stage1_inventory.py --out verification/stage1/batch4/inventory.json
uv run --frozen python scripts/report_stage1_accounting.py --focused verification/stage1/batch4/focused.xml --regression verification/stage1/batch4/regression.xml --inventory verification/stage1/batch4/inventory.json --out verification/stage1/batch4/component-results.json
uv run --frozen python scripts/check_stage1_evidence.py --report verification/stage1/batch4/component-results.json
git status --short
```
**Expected artifacts and schema (D-19 v2).** `verification/stage1/batch4/component-results.json`: `schema_version=2`, `tested_commit`, `worktree_state`, `dirty_fingerprint`, `source_fingerprint`, `test_selection`, `test_results` (`tests/pass/fail/skip/xml_sha256`), `mandatory_families`, `mandatory_contracts` (29), `golden_sha256`, `inventory_sha256`, `environment`, `authorization`; `inventory.json`: `state`, `integrity_ok`, `acknowledged_changes`, `unexpected_changes`, `errors`, `checked_files`, `inventory_sha256`; `index.json`: append-only, marks prior artifacts `SUPERSEDED`, never rewrites them.

**Done criteria.**
- Authorization is impossible when `tested_commit != HEAD` unless an explicit evidence-only descendant rule is approved and recorded; a dirty tree is labelled with a reproducible fingerprint.
- All eight inventory states are tested; a clean inventory and an authorized-exception-absent tree both pass; unauthorized changes fail both CLI and report through one function.
- The report derives component rows from declared test modules; a `COMPONENT_VERIFIED` suite never creates `CONTRACT_PASS`; `P0-SIGNALS`/`P0-ADV` are not reported PASS solely because their component suites pass; `P0-UNIVERSE`/`P0-BOUNDARY` remain `CONTRACT_BLOCKED` without external evidence; `sizing_budget` reads `test_accounting_sizing`.
- `MANDATORY` contains 29 rows including `P0-E2E`; a missing or non-PASS E2E row blocks authorization even if the other 28 pass.
- Mandatory and optional skips are reported separately; a mandatory skip blocks authorization; the PDF environment is recorded.
- The doc addendum and JSON agree row by row on every status; existing claims retain commit qualifiers.
- `data/README.md` is tracked or named in a prerequisite manifest; its inventory hash is unchanged.
- Existing verification artifacts are byte-identical and marked superseded; gate remains `CLOSED`.

**Stop conditions / rollback.** Stop if a historical artifact would need rewriting, if the owner rejects tracking `data/README.md` and also rejects a prerequisite manifest, or if JUnit cannot be generated from a clean tested tree. Rollback: revert the batch commit and delete the new evidence directory; prior artifacts were never touched.
## 9. Batch 5 — Outstanding P0 domain contracts

**Objective.** Implement and independently prove the six outstanding domain areas: documentary continuity, scoped adjustment, lifecycle, evidence completeness, scenario isolation, and friction-aware liquidation. `P0-UNIVERSE` and `P0-BOUNDARY` may consume the new evidence ports; they must stay `CONTRACT_BLOCKED` if authoritative membership or calendar evidence is absent.

**Contract IDs.** `P0-CONTINUITY`, `P0-IDENTITY` (contract-level fixture as a lifecycle/continuity dependency), `P0-SCOPED-ADJ`, `P0-LIFECYCLE`, `P0-EVIDENCE`, `P0-SCENARIO`, `P0-FULL-LIQUIDATION`. `P0-UNIVERSE` and `P0-BOUNDARY` may consume new evidence ports but stay `CONTRACT_BLOCKED` without authoritative membership/calendar evidence. `P0-LIQUIDATION` remains a separate `CONTRACT_PASS` zero-friction contract and is not reopened.

**Prerequisites.** Batches 1–4 closed. Approved `D-27`..`D-32`, owner approvals `U-02`, `U-10` (as modified), `U-11` (as modified), `U-12`, and all prior decisions; `U-09` is required only if an adjusted-price basis is in scope. `U-04`/`U-05` remain `BLOCKED`, so `P0-BOUNDARY`/`P0-UNIVERSE` stay `CONTRACT_BLOCKED` and the affected steps are not guessed. No rule is inferred from a test fixture: the continuity, adjustment, lifecycle, scenario and liquidation fixtures are labelled synthetic and cannot promote a contract to `CONTRACT_PASS`.

**In-scope files.**
- `src/nse_pipeline/backtest/identity.py` (only if the contract-level identity fixture exposes a defect)
- `src/nse_pipeline/backtest/continuity.py` (new)
- `src/nse_pipeline/backtest/scoped_adjustment.py` (new)
- `src/nse_pipeline/backtest/lifecycle.py` (new)
- `src/nse_pipeline/backtest/evidence.py` (extend to the full evidence contract)
- `src/nse_pipeline/backtest/scenarios.py` (new)
- `src/nse_pipeline/backtest/liquidation.py` (new)
- `src/nse_pipeline/backtest/universe.py`, `boundary.py` (extend only if the evidence ports are approved); `calendar.py` (new only if `D-11` adapter is approved and sourced)
- `scripts/report_stage1_accounting.py` (additive contract rows using the Batch 4 status model)
- `src/nse_pipeline/stage1_evidence.py` (Batch 5 contract statuses and component modules)
- `tests/test_accounting_continuity.py`, `test_accounting_scoped_adjustment.py`, `test_accounting_lifecycle.py`, `test_accounting_evidence_contract.py`, `test_accounting_scenarios.py`, `test_accounting_liquidation.py`, `test_accounting_batch5_integration.py`, `test_accounting_dividend_decisions.py` (all new)
- `tests/test_accounting_evidence.py`, `test_accounting_universe.py`, `test_accounting_boundary.py`, `test_accounting_dividends.py`, `test_accounting_replay.py`, `test_stage1_evidence.py` (extend)
- `tests/golden/continuity_cases.json`, `scoped_adjustment_cases.json`, `lifecycle_cases.json`, `scenario_cases.json`, `liquidation_cases.json`, `batch5_integration_cases.json`, `report_matrix_cases_v2.json` (new, hand-derived; Batch 4 goldens stay byte-identical)
- `docs/ARCHITECTURE.md` (new modules and status language)
- `docs/STAGE1_P0_CONTRACT_MATRIX.md` (status and proof references for Batch 5 rows only)
- `verification/stage1/batch5/*`

**Out of scope.** Legacy modules, concrete historical datasets, current-index constituents, inferred holidays, inferred delistings, real terminal proceeds, strategy returns, any existing golden edit.

**Implementation order (tests first).**
1. Identity/continuity: contract-level identity fixture plus dated evidence chains, proven scope, unresolved conflicts; no similarity inference.
2. Scoped adjustment: horizon-admissible factors over proven actions; raw cash/units never rewritten.
3. Lifecycle: dated suspension/series/termination states; absence is not termination; NAV/universe blockers explicit.
4. Evidence completeness: session counts, censoring, ticker-reuse isolation, lower bounds.
5. Scenario isolation: separate portfolio/state/statistics namespace; reference portfolio immutable.
6. Friction-aware liquidation: supplied orders, per-order costs/slippage, as-of capacity, integer shares; zero-friction subtotal remains separate.
7. Add report rows and statuses; run the Batch 4 evidence checker; write contract proofs. Each area's hand-derived golden is authored and hashed before that area's implementation; no expected value is derived from the code under test. The cross-domain integration golden binds one successor key across identity, continuity, adjustment, lifecycle, scenario and liquidation.

**Tests to add first.**

| Test | Expected failure at baseline |
|---|---|
| `test_continuity_requires_dated_documentary_evidence_for_every_boundary` | module absent |
| `test_identity_fixture_spans_edges_retirements_actions_and_lifecycle` | no contract-level fixture |
| `test_continuity_does_not_infer_from_symbol_or_isin_similarity` | module absent |
| `test_scoped_adjustment_applies_only_within_proven_scope` | module absent |
| `test_scoped_adjustment_never_rewrites_recorded_raw_cash_or_units` | module absent |
| `test_suspension_is_not_delisting_and_absence_is_not_termination` | module absent |
| `test_series_change_is_dated_and_only_causal` | module absent |
| `test_termination_requires_verified_terminal_evidence` | module absent |
| `test_evidence_counts_are_session_based_and_censored_lower_bounds` | partial only |
| `test_ticker_reuse_is_isolated_by_stable_key` | partial only |
| `test_scenario_portfolio_cannot_mutate_reference_positions` | module absent |
| `test_scenario_values_cannot_produce_statistics` | module absent |
| `test_liquidation_applies_per_order_fees_slippage_and_capacity` | module absent |
| `test_liquidation_never_values_unpriced_assets` | module absent |
**Verification commands.**
```powershell
uv run --frozen python -m pytest tests/test_accounting_continuity.py tests/test_accounting_scoped_adjustment.py tests/test_accounting_lifecycle.py tests/test_accounting_evidence_contract.py tests/test_accounting_scenarios.py tests/test_accounting_liquidation.py tests/test_accounting_batch5_integration.py tests/test_accounting_dividend_decisions.py tests/test_accounting_dividends.py tests/test_accounting_replay.py -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch5/focused.xml
uv run --frozen --extra pdf python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch5/regression.xml
uv run --frozen python scripts/verify_stage1_inventory.py --out verification/stage1/batch5/inventory.json
uv run --frozen python scripts/report_stage1_accounting.py --focused verification/stage1/batch5/focused.xml --regression verification/stage1/batch5/regression.xml --inventory verification/stage1/batch5/inventory.json --out verification/stage1/batch5/contract-results.json
uv run --frozen python scripts/check_stage1_evidence.py --report verification/stage1/batch5/contract-results.json
```
**Expected artifacts.** `verification/stage1/batch5/{focused.xml,regression.xml,inventory.json,contract-results.json,proofs.json,summary.json,checker.txt,golden-diff.json}`. `proofs.json` maps each contract ID to every clause, its independent test/golden, the tested commit and the resulting status. `golden-diff.json` records that Batch 4 goldens are byte-identical and lists only the additive Batch 5 goldens.

**Done criteria.**
- Each of the six areas has an independent, hand-derived golden and a cross-module test, not just unit coverage.
- The `P0-IDENTITY` fixture composes observations, dated edges, retirements, actions and lifecycle without an ISIN-only join; its status changes only with clause-level proof.
- Continuity, adjustment and termination never infer from symbol, ISIN similarity, absence or current constituents.
- Scenario state is isolated; reference positions/NAV are unchanged by scenario operations; scenario values cannot reach statistics.
- Friction-aware liquidation is integer, capacity-bounded, fee-inclusive and never invents a mark; the zero-friction subtotal is separately labelled.
- Each contract row is `CONTRACT_PASS` only if every clause is proven; otherwise `CONTRACT_BLOCKED` or `CONTRACT_PARTIAL` with a named missing source. `P0-UNIVERSE`/`P0-BOUNDARY` remain blocked without verified evidence.
- Report, contract matrix and proofs agree; no mandatory row is promoted from unit tests alone.
- Gate remains `CLOSED` unless every mandatory row is `CONTRACT_PASS`; existing goldens and datasets are unchanged.

**Stop conditions / rollback.** Stop a sub-item if authoritative continuity, membership, calendar or terminal-proceeds evidence is absent; record `CONTRACT_BLOCKED` and do not synthesize. Stop the batch if scenario isolation would change reference-portfolio accounting, if liquidation requires intraday depth not supplied, or if any golden/dataset edit is proposed. Rollback: revert the batch commit; new modules/goldens/evidence are additive; no data rollback needed.
## 10. Batch 6 — End-to-end causal integration verification

**Objective.** Prove the components compose as one causal system: public component contracts, frozen-order perturbation, economic/provenance prefix invariance, and final revision-bound evidence reconciliation. The final gate must enumerate all 29 mandatory contracts, including the `P0-E2E` row added in Batch 4.

**Contract IDs.** `P0-E2E`, `P0-EXECUTION`, `P0-PREFIX`, `P0-CONSERVATION` (integration clause), `P0-GATE` (29-row authorization).

**Prerequisites.** Batch 5 closed; every contract whose clauses are not owned by this batch (`P0-EXECUTION`, `P0-PREFIX`, `P0-CONSERVATION` integration clause, `P0-GATE`, `P0-E2E`) is `CONTRACT_PASS` or `CONTRACT_BLOCKED` with named external evidence; the excluded rows are proven here. `D-32` records the prefix comparison scope (freeze past economic and decision artifacts; revised history is a separately versioned run); the test set may not widen that scope without a new owner-approved decision.

**In-scope files.**
- `tests/stage1_harness.py` (new test-only scenario builder; no production import path)
- `tests/test_stage1_component_contracts.py` (new)
- `tests/test_stage1_frozen_order_perturbation.py` (new)
- `tests/test_stage1_prefix_invariance.py` (new)
- `tests/test_stage1_e2e_reconciliation.py` (new)
- `tests/golden/e2e_cases.json` (new, hand-derived)
- `verification/stage1/batch6/*`, `verification/stage1/final/*`
- `docs/STAGE1_P0_CONTRACT_MATRIX.md` (`P0-E2E`, `P0-EXECUTION`, `P0-PREFIX`, `P0-GATE` rows only)
- `scripts/check_stage1_evidence.py` (only if final reconciliation exposes a validator defect)

**Out of scope.** Production strategy code, legacy runners, performance metrics, new economic rules, existing goldens, datasets. **A production change discovered here is not made in Batch 6; it reopens the owning batch.**

**Implementation order (tests first).**
1. Author `tests/golden/e2e_cases.json` from hand-worked expectations, then build the test-only harness from public component APIs; prove it imports no legacy module.
2. Add component-contract smoke tests for identity, observations, evidence, signals, ADV, universe, boundary, sizing, accounting and valuation.
3. Add frozen-order perturbation tests: sells before buys, rank/key order, unrelated-rank invariance.
4. Add prefix-invariance tests: append future bars/evidence/calendars; past cash, claims, NAV, orders, ranks and provenance must not change.
5. Add economic and provenance prefix comparisons; run metadata may differ, provenance may not.
6. Add final reconciliation and gate-report tests asserting the gate is `CLOSED` unless every row is `CONTRACT_PASS`; write `final/gate.json` and update the evidence index.

**Tests to add first.**

| Test | Expected failure at baseline |
|---|---|
| `test_harness_imports_no_legacy_engine_runner_or_strategy` | harness absent |
| `test_sells_precede_buys_and_buys_follow_frozen_rank_order` | no cross-component proof |
| `test_perturbing_unrelated_rank_does_not_change_earlier_fills` | no perturbation suite |
| `test_appending_future_bars_cannot_change_past_nav_ranks_orders_or_provenance` | no prefix suite |
| `test_appending_late_evidence_never_rewrites_prior_cash_or_fills` | no prefix suite |
| `test_economic_prefix_ignores_run_metadata_but_not_provenance` | no comparison contract |
| `test_e2e_golden_reconciles_cash_claims_positions_orders_entitlements` | no e2e golden |
| `test_final_report_names_every_blocker_and_never_overclaims_pass` | no final reconciliation |
| `test_mandatory_gate_enumerates_29_rows_including_e2e` | no final gate record |
| `test_no_performance_statistic_is_generated_or_published` | no guard |
**Verification commands.**
```powershell
uv run --frozen python -m pytest tests/test_stage1_component_contracts.py tests/test_stage1_frozen_order_perturbation.py tests/test_stage1_prefix_invariance.py tests/test_stage1_e2e_reconciliation.py -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch6/e2e.xml
uv run --frozen --extra pdf python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/stage1/batch6/regression.xml
uv run --frozen python scripts/verify_stage1_inventory.py --out verification/stage1/batch6/inventory.json
uv run --frozen python scripts/report_stage1_accounting.py --focused verification/stage1/batch6/e2e.xml --regression verification/stage1/batch6/regression.xml --inventory verification/stage1/batch6/inventory.json --out verification/stage1/final/component-results.json
uv run --frozen python scripts/check_stage1_evidence.py --report verification/stage1/final/component-results.json
git diff --name-only -- data
```
**Expected artifacts.** `verification/stage1/batch6/{e2e.xml,regression.xml,inventory.json,perturbation.json,prefix-invariance.json}`, `verification/stage1/final/{component-results.json,gate.json,review.json}`. `gate.json` lists every mandatory contract, status, evidence hashes, tested commit and blockers.

**Done criteria.**
- The harness uses only public component boundaries and imports no legacy engine/runner/strategy/data-acquisition module.
- Frozen-order perturbation has a matrix showing earlier fills unchanged and only the intended later allocation changed.
- Prefix invariance holds for all `tests/golden/*.json` scenarios: future appends cannot change past cash, claims, positions, NAV, ranks, orders or provenance; run metadata may differ.
- The e2e golden reconciles cash, claims, positions, orders, entitlements and replay checks.
- `final/gate.json` enumerates 29 mandatory rows including `P0-E2E` and is `CLOSED` unless every row is `CONTRACT_PASS`; every non-PASS row names its blocker and required proof.
- Inventory is `CLEAN` or `AUTHORIZED_EXCEPTION*`; no `data/` diff; no performance statistic; no legacy import.
- The final review record explicitly confirms no legacy migration and no strategy execution occurred in Batches 0–6.

**Stop conditions / rollback.** Stop immediately if a production change is needed, if a prefix test fails (do not loosen the test), if a golden/dataset change is proposed, or if the gate would open with a `CONTRACT_BLOCKED` row. Rollback: delete the Batch 6 test/evidence additions; no production or data rollback is expected because Batch 6 changes tests and evidence only.

**Status after implementation.** The four new test modules and golden are regression coverage only; `P0-EXECUTION`, `P0-PREFIX` and `P0-E2E` remain `CONTRACT_NOT_IMPLEMENTED` (no synthetic promotion). `P0-VERIFY` advances to `CONTRACT_PASS` because its Batch 4 findings were resolved. The gate stays CLOSED.

## 11. Change-control rule for goldens and datasets

1. **Existing goldens are frozen.** `tests/golden/accounting_cases.json`, `execution_cases.json` and `valuation_cases.json` may not change in any batch. Their SHA256 values are recorded in Batch 0 and re-checked in every batch artifact.
2. **New goldens are additive and hand-derived.** Every new case carries `working` prose, explicit units/date basis, and expected values derived independently of production code. A new golden is committed and hashed before the code that satisfies it.
3. **No self-fulfilling commit.** A commit may not both introduce/alter an expected value and the code that makes it pass. A golden change requires owner approval, a new file/version, a written economic reason, and an independent review.
4. **Mutation evidence.** Every fixed accounting bug must include a mutation test proving the old defective rule fails the new oracle.
5. **Dataset bytes are immutable in Stage 1.** No command writes under `data/`. `data/README.md` may be tracked in Batch 4 only if `D-22` is approved; its bytes and inventory hash must not change. Any dataset refresh, manifest rewrite or inventory update is a separate authorization outside Batches 0–6.
6. **Evidence is append-only and content-addressed.** New reports go under `verification/stage1/`; existing artifacts are never overwritten and are marked `SUPERSEDED` in `index.json`. Every report references inputs by path and SHA256.
7. **Revision binding.** A report authorizes only the tested commit, source fingerprint, test selection, environment, golden hash and inventory state it records. An evidence-only descendant commit is acceptable only if the approved rule records that the diff is confined to `verification/`, and the report says so.
## 12. Proposed first implementation action (Batch 0, exact steps)

1. Confirm the baseline: `git rev-parse HEAD` must be `f5e943000764fade242b0482d67db2808991c076`; `git diff HEAD -- src tests scripts verification data` must be empty; `git status --short` must show no tracked modification under those paths and only the untracked paths enumerated in the baseline manifest. The sole permitted tracked delta is the planning-pass audit relocation (`D QUANT_REPOSITORY_AUDIT.md` / `?? docs/QUANT_REPOSITORY_AUDIT.md`), and the two blobs must match (`git hash-object` equals `git rev-parse HEAD:QUANT_REPOSITORY_AUDIT.md`).
2. Write `tests/test_stage1_baseline_freeze.py` with the five tests above. Run it; it must fail because `scripts/freeze_stage1_baseline.py` does not exist.
3. Add `scripts/freeze_stage1_baseline.py` with an output guard that rejects any path under `data/` and writes nothing outside `verification/stage1/`.
4. Add `scripts/reproduce_stage1_findings.py` and run the F1/F2/F3/F5 probes; confirm the audit's observed values.
5. Run:
   ```powershell
   uv run --frozen python scripts/freeze_stage1_baseline.py --out verification/stage1/baseline-f5e9430
   uv run --frozen --extra pdf python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/stage1/baseline-f5e9430/pytest-full.xml
   uv run --frozen python scripts/verify_stage1_inventory.py --out verification/stage1/baseline-f5e9430/inventory.json
   uv run --frozen python scripts/reproduce_stage1_findings.py --out verification/stage1/baseline-f5e9430/reproduction.json
   ```
6. Write `golden-sha256.json`, `manifest.json` and `index.json`; compare every count/hash with the audit and record any difference.
7. Write `decision-gates.json` with `U-01`–`U-13` and the Batch 1 decision approvals as `PENDING` unless the owner has approved them in `docs/STAGE1_DECISION_REGISTER.md`; `U-08` is recorded as `RESOLVED` by `D-03`.
8. Commit only the Batch 0 scripts, test and evidence. Do not touch `src/`, existing tests, goldens or `data/` content.
9. If any check fails, stop and restore nothing under `data/`; delete only the new Batch 0 files.

**No legacy runner migration, historical strategy execution or performance publication occurs in any batch. The overall P0 gate remains CLOSED until a revision-bound report proves every mandatory row `CONTRACT_PASS`.**
