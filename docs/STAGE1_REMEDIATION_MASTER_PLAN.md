# Stage-1 Remediation Master Plan

**Status:** planning-only. No production code, tests, golden fixtures, datasets, manifests, inventories, verification reports or Git history have been modified. No implementation has started.
**Baseline commit:** `f5e943000764fade242b0482d67db2808991c076`
**Planning date:** 2026-11-13
**Supersedes:** none. This plan consumes `docs/STAGE1_INDEPENDENT_TECHNICAL_AUDIT.md` and `docs/STAGE1_ACCOUNTING_VERIFICATION.md`.
**Governing principle:** correctness before completeness; evidence before authorization; causality before performance.

---

## 1. Executive summary

The Stage-1 accounting foundation is materially sound but not yet a trustworthy production baseline. The independent audit confirmed the eight golden accounting cases, the causal ordering and as-of controls, the accounting-module isolation, the dataset inventory, and the closed overall P0 gate. It also confirmed:

- two correctness defects inside implemented components (`F1` sub-paise cap admission, `F2` false price-basis provenance);
- one diagnostic defect (`F3` coverage reason) and one precision ambiguity (`F4` NAV quantization);
- one permanently unreconcilable economic state (`F5` `hold_receivable`);
- material research-semantics limitations (`F6`–`F14`) that are partly disclosed but not frozen as contracts;
- governance-critical verification weaknesses (`F16`–`F18`, `F20`) that bind evidence to a baseline rather than to a tested revision;
- documentation/report divergence (`F17`, `F19`, `F21`, `F23`) and one dataset reproducibility gap (`F22`).

This plan does four things:

1. **Separates** confirmed defects from specification ambiguities, deliberate limitations, verification gaps and unimplemented P0 contracts. No ambiguity is silently resolved.
2. **Defines** a revision-bound verification system and an explicit status vocabulary that cannot promote a component to a contract PASS.
3. **Orders** remediation into seven batches (0–6), with tests written before implementation, independent golden oracles, property/metamorphic/prefix invariants and adversarial authorization tests.
4. **Enumerates** the full mandatory P0 contract set, including the 14 deferred rows and the eight domain areas required by the brief, and specifies the independent proof needed to change each status.

The overall Stage-1 P0 gate remains **CLOSED**. Migration, historical strategy execution and performance publication remain unauthorized. This plan does not reopen the gate; it defines what must be true before any row can move.

---

## 2. Baseline and audit validation

### 2.1 Repository state

| Item | Value |
|---|---|
| Git HEAD | `f5e943000764fade242b0482d67db2808991c076` |
| HEAD subject | `docs: record post-baseline Stage 1 components and remaining gaps` |
| Branch state | detached/linear; no new commits during planning |
| Tracked changes at start | none |
| Untracked at start | `data/README.md`, `src/nse_pipeline/analytics/` |
| Audit files | moved to `docs/STAGE1_INDEPENDENT_TECHNICAL_AUDIT.md` and `docs/QUANT_REPOSITORY_AUDIT.md` as requested |
| Environment | Windows 11 `10.0.26300`, Python `3.14.6`, pytest `9.2.0`, uv `0.11.29` |

`git diff HEAD -- src tests scripts verification docs/ARCHITECTURE.md docs/STAGE1_ACCOUNTING_VERIFICATION.md` is empty. The implementation, tests, goldens, scripts and verification artifacts are **byte-identical to the audited revision**. All F1–F23 findings remain reproducible.

### 2.2 Evidence provenance

| Artifact | Source revision | Covers HEAD? |
|---|---|---|
| `verification/accounting-focused.xml` | dirty tree on `76adf01`, committed in `228f6cf` | No — 90 baseline cases only |
| `verification/accounting-regression.xml` | same | No — 940 baseline cases only |
| `verification/accounting-posttests.json` | `76adf01` working tree | No — dataset only |
| `verification/foundation-postcommit.json` | `76adf01` | No — dataset only |
| `verification/accounting-component-results.json` | `foundation_commit=76adf01`, generated at `head_at_verification=76adf01` | No |
| HEAD rerun performed during audit | `f5e9430` | Yes, but not committed as an artifact |

Post-baseline commits with no committed JUnit: `19b0e33`, `c6d1b14`, `3405330`, `23c155d`, `8e4e0e2`, `0c80814`, `f5e9430` (seven). `228f6cf` is effectively covered by the dirty-tree run but is not labeled as such.

### 2.3 Finding validation result

| Finding class | IDs | Current status |
|---|---|---|
| Confirmed correctness defects | F1, F2 | Reproduced at HEAD |
| Confirmed diagnostic defect | F3 | Reproduced at HEAD |
| Contract ambiguity | F4 | Reproduced; docs silent on quantization |
| Defect / unreconcilable economic state | F5 | Reproduced at HEAD |
| Disclosed limitations | F6, F8, F10 | Reproduced; docs disclose |
| Latent/dead configuration | F7, F12–F14 | Reproduced; no current behavior switch |
| Contract conflict | F9 | Reproduced; module docstring contradicts behavior |
| Caller-responsibility limitation | F11, F15, F22 | Reproduced |
| Verification/reporting gaps | F16–F18, F20 | Reproduced |
| Documentation defects | F19, F21, F23 | Reproduced |

No finding has been silently fixed, and no finding has become inapplicable.

### 2.4 Files created after the audited commit

None in tracked source. The only new files are planning documents and the relocated audit files. `data/README.md` and `src/nse_pipeline/analytics/__init__.py` pre-date the audit.

---

## 3. Governing principles

1. **A hard financial limit is an invariant, not a rounded estimate.** If a specification says `max_order_notional = X`, no executed principal notional may exceed `X`.
2. **Provenance must describe behavior, not configuration.** A recorded basis must identify the transformation actually applied.
3. **Unknown is not zero.** Absent evidence must not become a verified zero, a verified price, a verified session or a verified membership.
4. **Causality is structural.** An event may use only evidence whose `available_at` is not later than the event's phase. Late evidence may schedule future settlement but may not rewrite earlier cash, orders, fills, valuations or provenance.
5. **Precision is a policy.** Internal economic arithmetic may carry more precision than paise; recorded cash and fees quantize at defined boundaries. No per-event rounding may introduce cumulative drift.
6. **Specification surface is a contract.** Every validated field is either consumed, explicitly reserved behind a named gate, or removed. It must not create a false impression of implemented behavior.
7. **Component verification is not contract verification.** A passing component test suite cannot promote a mandatory P0 contract to PASS.
8. **Evidence is revision-bound.** A result authorizes only the exact commit, worktree state, source fingerprint, test selection, environment and golden/inventory fingerprints it records.
9. **Goldens are immutable oracles.** Existing golden expectations are preserved; new cases are additive and hand-derived. Expected values never come from the function under test.
10. **No legacy migration inside Stage-1 remediation.** Legacy engine, runner, strategy, data acquisition and performance work remain downstream and unauthorized.

---

## 4. F1–F23 disposition matrix

Classifications: **D** = confirmed defect; **A** = contract ambiguity; **L** = limitation (disclosed or scope-bounded); **V** = verification/reporting issue; **DOC** = documentation defect.
Causal impact is "None" unless later data can influence earlier decisions; all findings were checked for this. "None" means no leak was found, not that the area is irrelevant.

| ID | Class | Current status | Affected modules | Root cause (evidence) | Financial impact | Causal impact | Dependencies | Tests needed | Action |
|---|---|---|---|---|---|---|---|---|---|
| F1 | D | Reproduced | `fees.py:65,85`; `accounting_types.py:27-29`; `invariants.py:62` | Cap quantized half-up before comparison; `money()` rounds up sub-paise caps | Limited (≤ <0.005/order) but hard-limit violation | None | D-01, D-02 | `principal_cap` boundary matrix, buy/sell symmetry, replay consistency | **Fix** Batch 1 |
| F2 | D | Reproduced | `signals.py:33,112`; `specs/__init__.py:81,90` | Records `spec.price_basis`, not the callable actually used | None directly; reproducibility/audit falsehood | None | D-08 | `BasisProvenance`, mandatory `basis_id`, digest, mutation, process-stability tests | **Fix** Batch 2 |
| F3 | D | Reproduced | `valuation.py:99` | Exclusive `if/else` chain defaults every non-missing, non-zero book to `mixed_date_reference_only` | None (diagnostic) | None | D-25 | Six-state reference table plus four `nav_reason` cases | **Fix** Batch 1 |
| F4 | A | Reproduced | `valuation.py:85,100-102`; `docs/ARCHITECTURE.md:151-153` | No explicit NAV/report quantization policy; fees are paise, NAV is not | Limited (reporting inconsistency) | None | D-03 | Exact-`nav`/`nav_reported`, drift and aggregation tests | **Clarify + freeze** Batch 1 |
| F5 | D/L | Reproduced | `dividends.py:44-45`; `core.py:246-247` | `hold_receivable` returns `due=None`; no evidence-upgrade operation exists | Potentially material (permanent receivable) | None if confirmation is causal | D-04, D-05 | Confirmation/void lifecycle, `unresolved_settlement_claims`, conflict, termination, NAV tests | **Fix** Batch 1 |
| F6 | L | Reproduced | `signals.py:66`; `evidence.py:120-121` | `min_observations` counts whole history, not the lookback window | None (ranking semantics) | None | D-06 | Window-vs-history discriminating tests | **Make window-scoped** Batch 2 |
| F7 | L | Reproduced | `specs/__init__.py:82-85,91-93`; `signals.py` | Three validated fields have no production reader and one legal value | None today (latent) | None | D-07, D-16 | Mutation tests proving second values cannot be inert | **Wire single-value dispatch** Batch 2 |
| F8 | L | Reproduced | `universe.py:69-90`; `evidence.py:117-118` | Screen uses as-of bars/liquidity; no membership/suspension/termination state | Potentially material (survivorship) | None | D-10 | PIT membership, suspension, delisting tests | **Label/reasons** Batch 2; **membership port** Batch 5 |
| F9 | L/D | Reproduced | `adv.py:52-56,66-67,87-88` | Missing bar skipped; never-seen key returns `0.00`; `traded_sessions` ignored | Potentially material (capacity) | None | D-09 | Absence truth table, coverage, capacity `None` tests | **Fix semantics** Batch 2 |
| F10 | L | Reproduced | `boundary.py:139-140,147-154` | Only adjacent observed months compared; no omission record | Potentially material (missed rebalance) | None (omission, not leak) | D-12 | Zero-session-month and late-catch-up tests | **Add diagnostics** Batch 2 |
| F11 | L | Reproduced | `boundary.py:25-57` | No weekday/holiday validation; caller-supplied dates trusted | Potentially material (wrong session) | None | D-11 | Calendar authority; only `published_exchange` may fire | **Add validation; adapter in Batch 5** Batch 2/5 |
| F12 | L/DOC | Reproduced | `specs/__init__.py:168-194`; `sizing.py:11` | `PortfolioSpec` has no production consumer; long default rejected by `size_orders` | Potentially material (wrong/missing sizing) | None | D-13, D-14, D-15 | Long-name default, alias, resolver and wrapper tests | **Fix interface** Batch 3 |
| F13 | L | Reproduced | `specs/__init__.py:218-222` | `validate_policy_combination` imported only by tests | Potentially material (P3 without screen flag) | None | D-15 | Production-path validation tests | **Wire** Batch 3 |
| F14 | L | Reproduced | `specs/__init__.py`; signals/execution/valuation | Many validated fields unread or read only in validation | None/latent | None | D-16 | Four-state disposition registry: 10/12/0/5 | **Classify and drive disconnected to zero** Batch 3 |
| F15 | L | Reproduced | `observations.py:31-45` | No OHLC coherence check; `valid_ohlcv=True` with all prices `None` | None (consumers fail closed) | None | D-26 | Bar coherence/negative-path tests | **Strengthen validation** Batch 2 |
| F16 | V | Reproduced | `verification/*`; report script | Artifacts generated on baseline dirty tree; seven commits uncovered | None | None | D-19, D-21 | Revision-binding and freshness tests | **Regenerate + bind** Batch 4 |
| F17 | V | Reproduced | `scripts/report_stage1_accounting.py:15-46,84-86`; doc matrix | Deferred rows hardcoded; doc and JSON diverge at HEAD | None | None | D-17, D-15 | Report/doc parity tests | **Align** Batch 4 |
| F18 | V | Reproduced | `scripts/report_stage1_accounting.py:64-69,102-110`; `verify_stage1_inventory.py:47-49,65` | No HEAD equality; exception-count coupling; CLI/report disagree on clean inventory | None | None | D-17, D-18, D-19, D-20 | Adversarial authorization tests | **Fix** Batch 4 (start in 0) |
| F19 | DOC | Reproduced | `docs/STAGE1_ACCOUNTING_VERIFICATION.md:132,139` | Baseline "no skips" and HEAD "5 skips" presented without commit qualifier | None | None | D-20 | Doc consistency check | **Fix** Batch 4 |
| F20 | V | Reproduced | `scripts/report_stage1_accounting.py:24` | `sizing_budget` maps to `core`/`fees`, never `test_accounting_sizing` | None | None | D-17 | Report mapping test | **Fix** Batch 4 |
| F21 | DOC | Reproduced | `tests/test_accounting_sizing.py:1`; `tests/golden/execution_cases.json` | Docstring claims P1/P2/P3 goldens; only P2/P3 exist | None | None | D-14 | P1 golden + doc check | **Fix** Batch 3 |
| F22 | V/L | Reproduced | `data/README.md`; inventory | Inventoried file untracked; clean clone fails inventory | None until clone | None | D-22 | Clean-clone inventory test | **Track or declare prerequisite** Batch 4 |
| F23 | DOC | Reproduced | `docs/STAGE1_ACCOUNTING_VERIFICATION.md:165-168` | Stale "kernel does not infer signals/ADV" text contradicts post-baseline modules | None | None | D-17 | Doc consistency check | **Fix** Batch 4 |

**Disposition summary:** 5 fix-first (F1, F2, F3, F5 + F4 clarify), 6 research-semantics clarifications (F6–F11), 3 interface/config (F12–F14), 1 validation (F15), 6 verification/doc (F16–F21), 2 dataset/doc (F22–F23). No finding is left without an owner batch.

---

## 5. Root-cause findings

| ID | Root cause | Findings | Consequence |
|---|---|---|---|
| R1 | Hard limits and provenance are recorded values, not enforced or observed invariants | F1, F2 | Caps can be exceeded; provenance can name code that did not run |
| R2 | Diagnostic taxonomies are implemented as fall-through strings | F3 | A valid state is mislabeled |
| R3 | Precision policy is implicit and component-local | F4 | Internal and reported values use different conventions |
| R4 | The claim model has no evidence-upgrade transition | F5 | `due=None` is terminal, not pending |
| R5 | Sample sufficiency and window completeness share one counter | F6, F9 | Whole-history counts and zero-filled gaps look authoritative |
| R6 | Specification surface runs ahead of implementation | F7, F12–F14 | Validated knobs do nothing; defaults are unusable |
| R7 | Universe/lifecycle/calendar evidence is not modeled as first-class dated evidence | F8, F10, F11 | Screens substitute for membership; months and sessions can vanish |
| R8 | Verification binds to a foundation label, not a tested revision | F16–F18 | Evidence cannot authorize the code it supposedly tested |
| R9 | Machine reports are hand-maintained rather than derived from contract state | F17, F20 | JSON and prose disagree |
| R10 | Dataset inventory assumes an untracked file exists | F22 | A clean clone is not verifiable |

---

## 6. Architectural decisions

These are recommendations; the alternatives and trade-offs are in `STAGE1_DECISION_REGISTER.md`.

| ID | Decision | Rationale |
|---|---|---|
| AD-01 | Retain the modular monolith; introduce ports/adapters only for external evidence (exchange calendar, index membership, corporate-action terms, data catalog) | No evidence justifies distributed architecture; domain purity is preserved |
| AD-02 | Keep one accounting kernel with immutable event records and append-only state transitions | Existing design already satisfies determinism and replay |
| AD-03 | Decimal for recorded amounts, Fraction for ratios; fees and cash round half-up at recognition; hard caps floor downward at use and are never rounded upward; internal `nav` is exact and only `nav_reported` is presentation-quantized | Prevents F1, F4 and cumulative drift |
| AD-04 | Explicit `UNKNOWN` / `INSUFFICIENT` / `NOT_OBSERVED` states in every evidence-bearing result | Prevents absent→zero conflation |
| AD-05 | All external evidence carries `available_at`, `effective_on`, `citation` and identity scope | Existing causality model; extend to membership/calendar |
| AD-06 | Every validated spec field is consumed, reserved behind a named gate, disconnected with a bounded allowlist, or removed; a four-state field-disposition registry is CI-enforced and requires `disconnected == 0` | Prevents false configuration |
| AD-07 | Verification artifacts are revision-bound and content-addressed | Closes F16–F18 |
| AD-08 | Component status and contract status are separate; only `CONTRACT_PASS` authorizes | Prevents component→contract promotion |
| AD-09 | Inventory integrity is a state machine, not a boolean exception count | Closes F18/F22 |
| AD-10 | Stage-1 remediation never modifies the legacy engine/runner; legacy P0-1..P0-5 remain downstream | Preserves scope and avoids mixing accounting with performance |
| AD-11 | Existing golden fixtures are immutable; new goldens are additive and hand-derived | Protects independent oracle |
| AD-12 | Public interfaces change additively; removals require a deprecation note and a major-version decision | Protects callers |
| AD-13 | Calendar and universe membership are ports; only a `published_exchange` calendar may fire a signal, while caller-asserted/inferred calendars are diagnostic and cannot be registered | Prevents false authority |
| AD-14 | Human-readable verification docs are generated from, or cross-checked against, the machine report | Prevents F17/F19/F23 recurrence |

### 6.1 Proposed status model

| Status | Meaning | Authorizes migration? |
|---|---|---|
| `COMPONENT_IMPLEMENTED` | Code exists; no independent verification | No |
| `COMPONENT_VERIFIED` | Named component tests pass at the tested revision | No |
| `CONTRACT_PARTIAL` | Some contract clauses verified; at least one missing | No |
| `CONTRACT_PASS` | Every clause independently proven at the tested revision | Yes, only if all mandatory rows are PASS |
| `CONTRACT_NOT_IMPLEMENTED` | No implementation, or only a stub | No |
| `CONTRACT_FAIL` | Implementation exists but violates its contract | No |
| `CONTRACT_BLOCKED` | Cannot be verified because required external evidence is absent | No |

**Authorization rule:** `integration_authorized = all mandatory rows == CONTRACT_PASS AND evidence is current AND revision matches AND no mandatory skip AND inventory integrity passes`. A single `COMPONENT_VERIFIED`, `CONTRACT_PARTIAL`, `CONTRACT_NOT_IMPLEMENTED`, `CONTRACT_FAIL` or `CONTRACT_BLOCKED` keeps the gate CLOSED.

---

## 7. Dependency graph

```text
Batch 0  Baseline + evidence freeze
   |
   +--> Batch 1  Accounting correctness (F1, F3, F4, F5, replay)
           |
           +--> Batch 2  Causality/research semantics (F2, F6-F11, F15)
                   |
                   +--> Batch 3  Interface/spec coherence (F12-F14, F21)
                           |
                           +--> Batch 4  Verification hardening (F16-F20, F22-F23)
                                   |
                                   +--> Batch 5  Outstanding P0 domain contracts
                                           |
                                           +--> Batch 6  End-to-end causal verification
```

- **Merge order:** strictly `0 → 1 → 2 → 3 → 4 → 5 → 6`.
- **Technical critical path:** `0 → 1 (F5 claim lifecycle) → 5 (continuity/lifecycle) → 6`; Batches 2–4 are breadth and governance work that can expose blockers but do not shorten the domain chain.
- **Parallel lanes:** Batch 4 development starts in Batch 0 because safety controls must not wait for remediation, but its row reconciliation closes after Batch 3. Batch 5 may be prototyped after Batch 2 in isolated worktrees, but it merges only after Batch 4.
- **Hard dependency:** Batch 5 cannot claim `CONTRACT_PASS` without Batch 1 accounting semantics and Batch 2 evidence semantics.
- **Hard dependency:** Batch 6 cannot start until Batch 5 contracts are at least `CONTRACT_PARTIAL` with independent contract tests.

---

## 8. Batch roadmap summary

| Batch | Objective | Findings/contracts | Exit criterion |
|---|---|---|---|
| 0 | Freeze baseline, reproduce defects, define acceptance | All F1–F23 | Signed reproduction record; no code changes |
| 1 | Accounting correctness | F1, F3, F4, F5 | Corrected invariants + independent replay; gate still CLOSED |
| 2 | Research-data causality | F2, F6–F11, F15 | Frozen semantics + discriminating tests |
| 3 | Interface/spec coherence | F12–F14, F21 | One policy mapping; discriminating goldens; no dead config |
| 4 | Verification hardening | F16–F20, F22–F23 | Revision-bound evidence; adversarial authorization suite |
| 5 | Outstanding P0 contracts | P0-CONTINUITY, P0-IDENTITY, P0-SCOPED-ADJ, P0-LIFECYCLE, P0-EVIDENCE, P0-SCENARIO, P0-FULL-LIQUIDATION; P0-UNIVERSE and P0-BOUNDARY only via approved evidence ports | Each row `CONTRACT_PASS` with independent proof, or explicitly `CONTRACT_BLOCKED`; `P0-EXECUTION`, `P0-PREFIX`, `P0-GATE` and `P0-E2E` close in Batch 6 |
| 6 | End-to-end causal verification | P0-E2E, P0-EXECUTION, P0-PREFIX, P0-CONSERVATION (integration clause), P0-GATE (29-row authorization) | Prefix/metamorphic suite and final revision-bound evidence reconciliation |

Detailed steps are in `STAGE1_IMPLEMENTATION_BATCHES.md`.

---

## 9. Risks and constraints

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Authoritative exchange calendars are unavailable | High | Cannot certify month-end/holiday behavior | Model as `CONTRACT_BLOCKED` with raw-calendar fallback; never infer |
| Point-in-time index membership history is incomplete | High | Survivorship bias | Membership port + explicit `RESEARCH_ELIGIBILITY_ONLY` label; no current-constituent substitution |
| Terminal proceeds/delisting values are unresolved | High | NAV/liquidation incomplete | Mark unknown; block authoritative statistics; legacy P0-5 |
| Changing `money()` globally breaks fees | Medium | Broad regressions | Local cap comparison; keep global rounding policy |
| Float spec fields lose precision | Medium | Cap boundary behavior | Normalize via `Decimal(str(value))`; consider Decimal spec inputs |
| Golden fixtures accidentally altered | Low | Loss of independent oracle | Hash goldens; additive-only change control |
| Verification rewrite becomes a framework project | Medium | Schedule/scope creep | Minimal schema + deterministic JSON; no external service |
| Windows file locking / audit hooks | Medium | Flaky verification | Use temp roots; extend existing guard tests |
| Status vocabulary migration breaks consumers | Medium | Report incompatibility | Emit both legacy and new fields during transition |
| Scope mixing Stage-1 with legacy runner | High | Unauthorized performance work | Hard batch boundary; legacy P0s marked downstream |

### 9.1 Missing specifications and owner decisions (must not be invented)

The planning phase records an explicit design decision for every finding (`D-01`–`D-26`), but the following remain owner decisions:

1. Confirm that `max_order_notional` bounds principal only (D-02); a total-payable cap would be a separate field (U-01).
2. Confirm the pending `hold_receivable` lifecycle and the confirmation/void operations (D-04); partial payments and withholding remain unspecified (U-02, U-10, U-11), and a confirmed-but-unsettled claim must leave `unresolved_settlement_claims` (U-12).
3. Confirm window-scoped momentum and the schema-2 migration (D-06, U-03).
4. The authoritative source and update cadence for historical exchange calendars (U-04); until supplied, P0-BOUNDARY is `CONTRACT_BLOCKED`.
5. The authoritative source for historical index membership and suspension/delisting states, and whether PIT membership is Stage-1 or Stage-2 (U-05); until supplied, P0-UNIVERSE is `CONTRACT_BLOCKED`.
6. Confirm that long `PortfolioSpec` names are the public serialized vocabulary (D-13, U-06).
7. Confirm the P3 screen value and screen price source (D-14, U-07).
8. Confirm the strict ADV default versus an explicit zero-fill opt-in (D-09, U-13).
9. Confirm the Stage-1 scope of an adjusted-price basis (U-09).

Resolved during planning and retained for traceability: NAV exposure is exact `nav` plus reporting-only `nav_reported` (D-03, U-08 resolved); F3 uses six reference states plus a separate `nav_reason` (D-25); F1 uses a downward-floored cap (D-01); F5 uses `confirm_dividend_payment` / `void_dividend` (D-04).

Each open item is recorded in `STAGE1_DECISION_REGISTER.md` and blocks the associated implementation until decided.

---

## 10. Completion criteria

### 10.1 Per-batch criteria

A batch is complete only when:

- every in-scope finding has a disposition (fixed, clarified-and-frozen, deferred with named gate, or removed);
- tests were written first and failed for the intended reason before implementation;
- independent goldens are hand-derived and hashed;
- the batch's verification commands pass at the tested revision;
- evidence artifacts are regenerated and revision-bound;
- no dataset/manifest/inventory byte changed;
- the overall gate remains CLOSED unless every mandatory row is `CONTRACT_PASS`.

### 10.2 Global Stage-1 gate criteria

`CONTRACT_PASS` for every mandatory row in `STAGE1_P0_CONTRACT_MATRIX.md`, including:

- cash, fees, actions, dividend, coverage, NAV gaps;
- identity/observations, continuity, scoped adjustment, universe/membership, signals, ADV, boundary/calendar;
- lifecycle, evidence completeness, sizing/policies, execution causality, prefix invariance;
- scenario isolation, friction-aware liquidation, end-to-end integration;
- verification and gate controls.

Plus:

- HEAD-bound JUnit for every mandatory test family, with zero mandatory skips;
- independent replay and independent economic oracle for every accounting contract;
- inventory integrity in `clean` or `authorized_exception` state, with consistent CLI/report behavior;
- a signed verification report naming commit, dirty state, fingerprints, environment and test selection;
- an independent review confirming no legacy runner migration or performance publication occurred.

Passing the full pytest suite is **not** sufficient for any contract PASS.

---

## 11. Authorization and non-goals

**Authorized by this plan:** read-only analysis, design, test design, documentation, and the creation of planning artifacts.

**Not authorized:** production-code edits, test edits, golden changes, dataset/manifest/inventory changes, verification-report overwrites, commits, legacy runner migration, historical strategy execution, performance metric publication, or claims that any P0 contract has passed.

---

## 12. Internal consistency review

| Check | Result |
|---|---|
| Every F1–F23 has a disposition row | Yes |
| Every F1–F23 maps to a batch | Yes |
| Every mandatory P0 contract appears in the contract matrix | Yes (see `STAGE1_P0_CONTRACT_MATRIX.md`) |
| Every proposed code change maps to a finding or contract | Yes |
| Every batch has measurable acceptance criteria | Yes |
| Batch dependencies form a valid order | Yes (section 7) |
| No contract is declared PASS from component tests | Yes — enforced by AD-08 |
| No historical source evidence assumed | Yes — `CONTRACT_BLOCKED` where evidence is absent |
| No ambiguity silently resolved | Yes — section 9.1 and decision register |
| Datasets and goldens untouched | Yes |
| Overall P0 gate remains CLOSED | Yes |
| No implementation started | Yes |

---

## 13. Artifact index

| Artifact | Purpose |
|---|---|
| `docs/STAGE1_REMEDIATION_MASTER_PLAN.md` | This document: baseline, dispositions, architecture, batches, completion |
| `docs/STAGE1_P0_CONTRACT_MATRIX.md` | Every mandatory contract, status, proof, gate |
| `docs/STAGE1_REGRESSION_TEST_PLAN.md` | Tests-first specification, oracles, invariants, adversarial cases |
| `docs/STAGE1_IMPLEMENTATION_BATCHES.md` | Executable batch order, files, commands, stop conditions |
| `docs/STAGE1_DECISION_REGISTER.md` | Unresolved decisions, alternatives, recommendations, consequences |
| `docs/STAGE1_INDEPENDENT_TECHNICAL_AUDIT.md` | Independent audit (relocated) |
| `docs/STAGE1_ACCOUNTING_VERIFICATION.md` | Existing verification record under audit |
| `docs/QUANT_REPOSITORY_AUDIT.md` | Legacy repository audit (relocated) |
