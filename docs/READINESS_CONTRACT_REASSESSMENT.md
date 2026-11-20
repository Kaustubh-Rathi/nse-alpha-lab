# Read-only P0 Contract Reassessment

**Observed:** 2026-11-14 17:46 UTC  
**Audited HEAD:** `bf7b5a593d17dbf6dc257d983766e5d54739795e`  
**Mode:** additive findings only. No canonical status, matrix, source, test, golden, or dataset was edited by this audit. The only files produced for this request are this report and `verification/stage1/readiness-reopen/contract-reassessment.json`. No commit was made.

## Scope and limits

This reassesses acceptance clauses for P0-FEES, P0-COVERAGE, P0-CAPACITY, P0-SIGNALS, P0-ADV, and P0-GATE against the public tests and implementation visible at the observed time. The authoritative matrix and decision register remain unchanged and still record their earlier statuses. This report is not a status promotion, authorization, strategy run, or performance result.

The worktree was **dirty** at observation: `src/nse_pipeline/backtest/signals.py` and `tests/test_accounting_signals.py` were modified by concurrent work, with additional unrelated uncommitted/untracked files. The signal files were inspected but deliberately excluded from this audit's test command while another agent was editing them. Therefore the audit is explicitly **not final-revision-bound**, and its pass results must not be used as authorization. An independently reported host run of 146 tests is separate evidence and is not represented as this agent's run. The repository subsequently advanced to HEAD `9f079ed332638fefc4d5646cadbdc9f732f0a802` while this additive artifact was being completed; the test result remains associated with the earlier observed HEAD and is not evidence for that later revision. A later dirty-worktree fingerprint/source-hash snapshot is recorded separately in the JSON.

## Focused test run actually performed

Command:

```text
python -m pytest tests/test_accounting_fees.py tests/test_accounting_core.py tests/test_accounting_valuation.py tests/test_accounting_adv.py tests/test_accounting_report.py tests/test_stage1_evidence.py -q
```

Result: **133 passed in 9.82s**. No signal test was run. These are current dirty-worktree component results, not contract-status promotion and not an artifact-backed gate run. Test nodes/source hashes and worktree fingerprints are in the machine artifact.

## Findings by contract

### P0-FEES — old FAIL rationale is stale; do not promote status here

The matrix's F1 claim that a `99.996` hard cap admits `100.00` is contradicted by the current source and focused tests. `fees.py::principal_cap` floors to paise and `within_notional_cap` applies the resulting principal-only cap symmetrically in buy and sell paths. `tests/test_accounting_fees.py::test_cap_never_rounds_up_at_paise_boundary`, `::test_cap_boundary_table_buy_and_sell`, the hand-derived cap-boundary golden test, and the monotonicity property passed in this run; `tests/test_accounting_core.py::test_subpaise_cap_normalization_uses_floor_and_agrees_with_independent_replay` and `::test_replay_rejects_old_half_up_cap_path` also passed. Fee component rounding, integer affordability, fee-inclusive cash, slippage, and the independent execution fixture were exercised by the same run.

**Assessment:** acceptance clauses FEES-01 through FEES-07 are `PROVEN_BY_EXISTING_EVIDENCE` for the observed dirty snapshot. The recorded FAIL reason is stale. This does not reconcile the canonical `CONTRACT_FAIL` status; status reconciliation requires review and fresh revision-bound evidence.

### P0-COVERAGE — old reason failure is stale; zero-gross reachability caveat

The current `valuation.py` has the closed six-state reference-reason vocabulary and separate four-state NAV reason. `tests/test_accounting_valuation.py::test_reference_coverage_state_table`, `::test_fully_priced_book_is_complete_not_mixed`, `::test_unknown_asset_blocks_nav_but_not_coverage`, `::test_reference_coverage_and_nav_reason_vocabularies_are_closed`, and `::test_missing_reference_preserves_a_nonzero_partial_reference_subtotal` passed in this run. The fully-priced-book fall-through defect described as F3 is no longer observed. Exact `nav` and presentation-only `nav_reported` are also covered.

**Zero-gross caveat:** with current public inputs, a held position has positive shares, and accepted close/reference marks have positive prices. A usable priced or reference notional is therefore positive; missing references make aggregate gross `None`, while an empty book is classified earlier as `no_positions`. Consequently `zero_gross_notional` appears in the vocabulary/branch but is not reachable through the normal public construction path. Do not manufacture a zero-price mark or change the model merely to make this branch reachable. If the domain intends a legitimate zero-valued holding/reference case, its admissibility needs an explicit owner/domain decision and corresponding input model. The zero-gross clause is classified `DECISION_NEEDED`; other coverage clauses are supported by current public evidence.

### P0-CAPACITY — old cap FAIL rationale is stale

The cited sub-paise notional-cap defect shares the corrected floor-to-paise implementation and buy/sell tests described under FEES. Capacity integration is exercised by `tests/test_accounting_fees.py::test_production_integer_fills_match_all_independent_capacity_vectors`, together with the ADV tests for exact, as-of capacity and fail-closed unknown/partial liquidity. Core tests cover the execution path and rank-sequential independent sizing vectors.

**Assessment:** CAP-01 through CAP-05 are `PROVEN_BY_EXISTING_EVIDENCE` in this focused run. The old principal-cap FAIL reason is stale. This is a clause-level reassessment only; no contract status is promoted.

### P0-SIGNALS — broad old FAIL observations mostly addressed; provenance issue remains live

The visible working-tree implementation contains window-scoped minimum observations, exact-session anchors, exact rational momentum, deterministic key tie-breaks, structured callable provenance, and explicit dispatch. The modified signal tests also contain cases for global-dependent callable provenance. However, both signal source and tests were concurrently modified; **no signal tests were run**, and the earlier matrix/test evidence cannot verify the current dirty revision.

At the observed time, the global-basis provenance collision remains a **live, actively-being-worked issue**: callable source/name alone can be insufficient when same-source functions vary through global state. The current dirty edits appear aimed at this basis, but this audit does not claim that the change is complete or correct. In particular, any caller-supplied identity/parameters need to represent runtime dependencies truthfully, and a test must prove behaviorally different global configurations cannot acquire indistinguishable provenance. Record this as a point-in-time assessment, not a claim that the concurrent fix is done.

**Assessment:** SIG-01..04, SIG-06, and SIG-07 are `NEEDS_CURRENT_RUN`; SIG-05 is `REAL_GAP` pending review of the concurrent implementation and a successful targeted signal suite. Exact momentum mathematics does not require real-market documents, but a focused test pass alone does not promote contract status.

### P0-ADV — old missingness FAIL rationale is stale; separate traded_sessions gate is redundant

The current ADV implementation distinguishes never-seen/absent bars (`None` or partial estimate), invalid/unknown values (fail closed), and explicit valid zero-volume bars (known zero). Focused tests passing here include `test_never_seen_key_has_unknown_adv`, `test_explicit_zero_volume_is_known_zero`, `test_missing_bar_and_explicit_zero_bar_are_distinguished`, `test_partial_adv_coverage_is_not_a_full_window`, `test_capacity_is_none_below_minimum_observed_coverage`, and append invariance. No positive capacity was found from a public valid input that violated the approved known-observation/coverage policy. With a value-bearing estimate, `traded_sessions` equals the known observed-value count; explicit zero bars count as known observations.

The decision-register wording that capacity must separately require a `traded_sessions` minimum is stale/redundant under these semantics: coverage is the sufficiency gate, and a second positive-trading-frequency threshold is not present in the approved rule. Do **not** invent a positive-volume or trading-frequency threshold. If the owner wants “at least one positive-volume session” or a minimum number of positive-volume sessions, that is a new economic/policy decision, not an implementation fix inferred by this reassessment. Listed/no-trade opt-in may produce zero capacity, never positive capacity from absent bars.

**Assessment:** ADV-01..06 are `PROVEN_BY_EXISTING_EVIDENCE`; ADV-07's separate `traded_sessions` gate wording is `STALE_FAILURE_REASON` as a redundant gate, while the enforced coverage/known-observation policy is proven. A positive-activity rule, if desired, is `DECISION_NEEDED` and is not recommended absent owner approval.

### P0-GATE — closed authorization behavior is evidenced; clause-attestation gap remains

`stage1_evidence.py` declares a 29-contract set including P0-E2E and keeps contract statuses distinct from component results. `report_stage1_accounting.py` derives report rows from that declared manifest and closes authorization on non-PASS declared statuses, revision-binding failures, inventory failures, regression failures, or mandatory skips. The focused report/evidence tests passed, including `test_report_emits_schema_v2_with_29_rows_and_a_closed_gate`, `test_report_component_verified_never_authorizes_a_failing_contract`, `test_e2e_is_mandatory_so_28_passing_rows_still_cannot_authorize`, `test_mandatory_skip_blocks_authorization_but_optional_pdf_skip_is_disclosed`, and revision-binding cases.

This proves the inspected **authorization mechanics**, not that each declared status was freshly derived from clause-level attestations. The 29 `ContractSpec` statuses and blocking explanations are still hand-authored declarations in `stage1_evidence.py`; a component pass does not attest each acceptance clause. The matrix itself likewise remains the canonical owner-facing status record. Thus the old specific GATE claims that 29-row derivation and HEAD-binding mechanics are absent are stale, but the deeper declared-status-vs-clause-attestation gap remains a `REAL_GAP`. This is why the report does not claim P0-GATE fully proven or authorize a status reconciliation.

## Verdict legend and clause-level map

The JSON artifact contains one entry for every in-scope acceptance clause, its public test nodes, source references, and one verdict:

- `PROVEN_BY_EXISTING_EVIDENCE` — supported by public tests/source, including the focused run where stated.
- `NEEDS_CURRENT_RUN` — relevant proof exists or implementation is visible, but this dirty snapshot was not tested by this audit.
- `REAL_GAP` — a material unresolved implementation/evidence issue remains.
- `STALE_FAILURE_REASON` — the cited FAIL basis no longer matches observed implementation/evidence; this does not promote status.
- `DECISION_NEEDED` — an owner/domain decision is needed; no new threshold or economic policy is inferred.

## Status and next action

The matrix and machine-declared contract statuses remain untouched. Earlier plans and their findings remain historical records; this additive reassessment does not rewrite or invalidate them. Mathematical contract proof does not require primary real-world market documents. Conversely, this focused test pass is not automatically a contract-status promotion. After host review, the owner may decide whether to reconcile the stale FAIL rationales for FEES/COVERAGE/CAPACITY/ADV, and whether to request a targeted SIGNALS rerun or close any decision-needed questions. P0-GATE clause-attestation and the live signal provenance concern remain unresolved at the observed time. No migration, strategy/performance run, or publication is authorized.
