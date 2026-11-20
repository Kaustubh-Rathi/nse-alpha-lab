# Stage-1 Independent Technical Audit

**Repository:** `D:\fix_Investment_backtesting`
**Audited commit:** `f5e943000764fade242b0482d67db2808991c076` (`docs: record post-baseline Stage 1 components and remaining gaps`, 2026-11-13T22:01:24+05:30)
**Audit date:** 2026-11-13
**Mode:** read-only, adversarial, independent re-derivation. No production code, tests, docs, datasets, manifests, inventories, golden fixtures or existing verification reports were modified.
**Environment:** Windows 11 (`10.0.26300`), Python `3.14.6` (MSC v.1944, 64-bit), pytest `9.2.0`, uv `0.11.29`.
**Worktree at start and end:** `?? data/README.md`, `?? src/nse_pipeline/analytics/` only.

> Scope: the Stage-1 accounting slice — 15 new `backtest/` modules, `specs/`, both verification scripts, the verification layer, accounting tests and golden fixtures. The legacy engine/runner, data acquisition, historical strategy simulation and performance statistics were **not** audited. This report does not publish or endorse any performance result.

---

## A. Executive summary

The verification document `docs/STAGE1_ACCOUNTING_VERIFICATION.md` is, on the whole, unusually accurate and candid. Its central operational claims reproduce independently at HEAD: the baseline test counts, the inventory size and hash, the single accepted manifest exception, the eight golden case IDs, the fee quote, the coverage ratios, the xfail-resolution history, and the closed overall P0 gate all check out. The overall gate is correctly **CLOSED**, because 14 of 28 mandatory contracts remain unimplemented; no migration is authorized.

The audit nevertheless found material issues inside the implemented slice and in the verification layer:

1. **Two confirmed code defects** in implemented components:
   - the sub-paise `max_order_notional` cap is rounded half-up, so a declared cap of `99.996` admits a `100.00` buy or sell (`fees.py:65,85`; `accounting_types.py:27-29`);
   - a custom `basis_price` callable is recorded under the *spec string* (`"close"` by default) rather than under the callable's identity, so the emitted provenance can describe a different price series than the one ranked (`signals.py:112`).
2. **One confirmed diagnostic mislabel**: a fully priced book (`coverage=1`, `completeness=1`) is reported as `reference_coverage_reason='mixed_date_reference_only'` (`valuation.py:99`).
3. **One permanently unreconciled economic state**: `dividend_settlement_mode="hold_receivable"` creates a receivable with `due=None`, and `core.settle` rejects any claim whose `due` is `None`; no API can later attach verified payment evidence (`dividends.py:44-45`; `core.py:246-247`).
4. **A machine-report contradiction**: re-running `scripts/report_stage1_accounting.py` at HEAD emits 14 PASS / 14 NOT IMPLEMENTED; `identity`, `universe`, `signals`, `adv`, `boundary` and `evidence` are `NOT IMPLEMENTED` in JSON while the doc matrix calls them (or their modules) PASS/component. The report also maps `sizing_budget` to `core`/`fees` tests, never to `test_accounting_sizing.py`.
5. **Verification-layer weaknesses**: the report records `head_at_verification` but never compares it to `foundation_commit`; `integrity_ok` requires *exactly one* accepted exception, so a perfectly clean inventory would close the gate; and the inventory CLI exits `0` whenever there are no unexpected changes/errors even if `only_accepted_exception=false`.
6. **A provenance gap already disclosed by the docs**: committed artifacts were generated from a working tree on `76adf01`; the seven commits after `228f6cf` have no regenerated JUnit.

None of these findings invalidates the eight golden accounting cases or the replay, but findings 1–3 mean the affected component rows are **PASS (qualified)**, not unqualified PASS. The gate remains CLOSED and migration remains unauthorized.

---

## B. Claim-by-claim verification

Legend: **VERIFIED** — independently reproduced; **VERIFIED (qualified)** — the claim is true only with the stated caveat; **CONTRADICTED** — the evidence at HEAD says otherwise; **PARTIAL** — some sub-claims hold, others do not.

| # | Doc line | Claim | Verdict | Independent evidence |
|---:|---|---|---|---|
| C1 | 3–5 | Base `76adf01`; overall P0 gate **CLOSED**; no migration/performance work | **VERIFIED** | `verification/accounting-component-results.json:122-124`; re-run of `report_stage1_accounting.py` at HEAD returns `overall_p0_gate: CLOSED`, `integration_authorized: false` |
| C2 | 9–13 | `foundation-postcommit.json` refreshed; `accounting-posttests.json` repeats the complete check | **VERIFIED (qualified)** | Both files exist and both record `verified_commit=76adf010...`; they do **not** cover the seven later commits (see F16) |
| C3 | 14–17 | 8,153 inventoried files; 6,835 raw + 1 database matched; no errors/unexpected changes | **VERIFIED** | `audit-inventory.json` rerun at HEAD: `checked_files=8153`, `raw_files_checked=6835`, `database_files_checked=1`, `errors=[]`, `unexpected_changes=[]` |
| C4 | 16–17 | Inventory SHA256 `a4e3ac408f800e1a7d7623e04e455927ecc4dc6c1c063643ad204b8f49b7edee` | **VERIFIED** | Recomputed with `hashlib.sha256` over `data/manifests/dataset_hashes.json` |
| C5 | 19–23 | Sole mismatch is `dataset_manifest.json` `5547ac45...` → `c304d08e...` | **VERIFIED** | Inventory rerun: exactly one `acknowledged_unrecovered_changes` entry with those hashes |
| C6 | 25–28 | Manifest is untrusted, not restored, not staged; baseline inventory never updated | **VERIFIED** | `git ls-files data/processed/dataset/dataset_manifest.json` empty; `git check-ignore` matches `.gitignore:5`; inventory hash unchanged |
| C7 | 32–44 | Production module responsibilities | **VERIFIED** | Structural read of all 15 accounting modules; responsibilities match the one-line descriptions |
| C8 | 56–60 | All eight accounting cases match every asserted field and all eight replay to PASS | **VERIFIED** | `tests/test_accounting_reconciliation.py`: 13 passed; the eight IDs are `prefunded_purchase`, `unsettled_sale`, `dividend_lag`, `split_up`, `split_down`, `absence_reopening`, `reservation_cancel`, `verified_proceeds` |
| C9 | 64–75 | Golden terminal values (reservation, purchase, sale, dividend, splits, absence, proceeds) | **VERIFIED** | Independent JSON read of `tests/golden/accounting_cases.json`; test assertions compare every field exactly |
| C10 | 74 | Fee quote: fill `100.05`, fees `141.67`, payable `100,191.67` | **VERIFIED** | `tests/golden/execution_cases.json`; production test passes |
| C11 | 75 | Coverage: completeness 1/2; reference coverage 2/3 → 1/6; missing makes ratio unavailable | **VERIFIED** | `tests/test_accounting_valuation.py` golden cases; `valuation.py:96-99` |
| C12 | 82–86 | Inventory, focused, regression, report, lock/whitespace/syntax commands | **VERIFIED** | All re-executed read-only; `uv lock --check` resolved 15 packages; `git diff --check` clean; 166 Python files parsed, 0 errors |
| C13 | 95–96 | 90 focused passed; 940 full-suite + 129 subtests; zero failures/skips (baseline) | **VERIFIED** | `verification/accounting-focused.xml`: 90 testcases, 0 fail/error/skip; `accounting-regression.xml`: 940 testcases + 129 subtests, 0 fail/error/skip; JUnit `tests` attr 1069 = 940 + 129 |
| C14 | 97–99 | Post-test SHA256 zero errors; accepted manifest only mismatch; not a clean all-match claim | **VERIFIED** | `verification/accounting-posttests.json:18-19`: `all_hashes_match=false`, `only_accepted_exception=true` |
| C15 | 103–107 | Three standards/correctness + two spec issues reproduced and fixed; follow-ups fixed caps, scenarios, missed settlements | **VERIFIED** | Four strict xfails at `c6d1b14` all exist at HEAD without markers; fixes in `8e4e0e2` and `0c80814`; zero `xfail` occurrences at HEAD |
| C16 | 116–123 | PASS rows for cash, fees, actions, dividend, coverage, consideration, replay, artifact guards | **VERIFIED (qualified)** | Test-backed; `fees` qualified by F1, `dividend` by F5, `coverage` by F3/F4, `verification` by F18 |
| C17 | 124 | PASS (component) for identity and observations | **VERIFIED (qualified)** | Code and 58 identity + 23 observation tests pass; machine report still says `identity: NOT IMPLEMENTED` (F17) |
| C18 | 125 | Continuity and scoped adjustment NOT IMPLEMENTED | **VERIFIED** | No continuity/scoped-adjustment module exists |
| C19 | 126 | PASS (component) spec-driven universe; exact causal momentum ranks | **VERIFIED (qualified)** | `universe.py`/`signals.py` exist and 32 tests pass; limitations F6–F8; report says `NOT IMPLEMENTED` |
| C20 | 127 | Causal month-end scheduling NOT IMPLEMENTED | **VERIFIED** | `boundary.py` implements only observed-month change + supplied-calendar month-end; no holiday inference (F10/F11) |
| C21 | 128 | Full lifecycle/series/termination and evidence-counting contracts NOT IMPLEMENTED | **PARTIAL** | The broad contract is absent, but `evidence.py` exists and line 129 credits it as PASS(component). The two rows are not clearly reconciled |
| C22 | 129 | PASS (component) P1/P2/P3 sizing, trailing ADV, evidence ledger, boundary; P1 unconfirmed | **VERIFIED (qualified)** | Code and tests exist; P1 has no golden (F21); ADV F9; boundary F10/F11; report maps `sizing_budget` elsewhere (F20) |
| C23 | 132 | No FAIL/SKIP in final focused/full results | **CONTRADICTED at HEAD** | Baseline artifacts have 0 skips, but the same document line 139 records 5 skips at HEAD (optional PDF deps) — see F19 |
| C24 | 133 | Overall mandatory P0 gate CLOSED; no migration authorization | **VERIFIED** | Machine report re-run at HEAD: `CLOSED`, `integration_authorized=false` |
| C25 | 137–139 | Eight post-baseline components; full suite 1,129 passed, 5 skipped, 129 subtests, no xfails | **VERIFIED** | Full-suite rerun at HEAD: 1,129 passed, 5 skipped, 129 subtests; grep finds zero `xfail` |
| C26 | 140–142 | Four review-found bugs fixed test-first | **VERIFIED** | Probe8: all four xfail tests present at HEAD, no markers; `8e4e0e2`, `0c80814` |
| C27 | 142–143 | JSON reports not regenerated for post-baseline components | **VERIFIED** | Artifacts end at `228f6cf`; seven commits `19b0e33..f5e9430` have no regenerated JUnit |
| C28 | 145–147 | P1 has no golden; P2/P3 golden cases cannot distinguish the policies | **VERIFIED** | `execution_cases.json` has only P2/P3; both cases pass `unresolved_ranks=()` and would produce identical output under either policy |
| C29 | 147–148 | `min_observations` counts whole history, not lookback | **VERIFIED** | `signals.py:66`; `evidence.py:120-121`; probe: 15 total valid / 3 in window still ranked |
| C30 | 148 | `MomentumRanks.price_basis` does not record a custom callable | **VERIFIED but understated** | It records `spec.price_basis`, which can be actively wrong or a mutated bogus string — F2 |
| C31 | 149 | Trades filter reuses the ADV window | **VERIFIED** | `universe.py:34` is used for both traded value and trades; `ExecutionSpec.adv_window_sessions` is separate (`adv.py:87`) |
| C32 | 149–150 | `sizing.py` accepts only `"P1"|"P2"|"P3"`, so callers must map long `PortfolioSpec` names | **VERIFIED (incomplete)** | True, but no production code consumes `PortfolioSpec` at all, and its default `P2_size_excluding_unresolved` is rejected by `size_orders` — F12 |
| C33 | 154–156 | Keys, decisions and session stream must be verified upstream; no cross-ISIN adjustment, ticker-reuse or termination inference | **VERIFIED** | `identity.py`, `universe.py`, `evidence.py`; probe confirms suspended vs delisted both `no_bar_on_session` |
| C34 | 157–161 | Only normal cash delivery and normal cum/ex dividends; unsupported units quarantined | **VERIFIED** | `share_actions.py`, `dividends.py`, `core.py`; quarantine retained |
| C35 | 162–164 | Lags are research assumptions; late evidence never rewrites earlier cash | **VERIFIED** | `ExecutionSpec.trade_settlement_lag_sessions` comment; `dividends.py:47-49` rejects payment before effective date |
| C36 | 165–168 | "The kernel ... does not infer exchange sessions, signals, target weights, ADV ..." | **CONTRADICTED as written** | Post-baseline `signals.py`, `adv.py`, `universe.py`, `boundary.py` do compute signals, ADV, membership and month-end fallback. The word "kernel" may mean `core.py` only, but the paragraph sits under remaining limitations for the whole slice — F23 |
| C37 | 169–172 | Marks in current raw-share units; scenarios hypothetical, no statistics | **VERIFIED** | `ValuationPolicy.allow_scenario_statistics` rejected when true (`specs/__init__.py:158-159`) |

---

## C. Findings register

Severity is audit severity within the Stage-1 slice, not a restatement of P0 gate status. "Defect" means the code contradicts a stated/legislated contract; "limitation" means a disclosed or scope-bounded behavior; "doc" means a documentation/reporting inaccuracy.

### F1 — Sub-paise maximum-order cap rounds half-up and admits over-cap notionals

- **Type / severity:** confirmed defect / **Major**
- **Locations:** `src/nse_pipeline/backtest/fees.py:65` (buy), `:85` (sell); `src/nse_pipeline/backtest/accounting_types.py:27-29` (`money` uses `ROUND_HALF_UP`)
- **Evidence:** probe6 (`C:\Users\Kaustuvh\AppData\Local\Temp\opencode\audit_probe6.py`):

  | Declared cap | Price | Buy qty | Buy notional | Sell qty | Sell notional |
  |---:|---:|---:|---:|---:|---:|
  | `99.996` | `10` | 10 | `100.00` | 10 | `100.00` |
  | `99.994` | `10` | 9 | `90.00` | 9 | `90.00` |
  | `19.999` | `10` | 2 | `20.00` | 2 | `20.00` |

- **Cause:** `money(str(execution.max_order_notional))` quantizes the cap to paise **before** the integer search. A cap below a paise boundary can round upward to the next paise (maximum excess just under `0.005`), and the search then treats the rounded value as the hard limit.
- **Why tests miss it:** `tests/test_accounting_core.py:288-300` codifies `10.005 -> 10.01` as expected half-up behavior, and `invariants.py:62` mirrors the same rounding, so the independent replay shares the defect.
- **Impact:** a hard risk limit is exceeded by a small amount. Golden cases use whole-paise caps and cannot detect it. It is not a P0 gate opener, but the `fees`/`capacity_fill` PASS rows must be qualified until the rounding direction or the cap contract is made explicit.

### F2 — Custom `price_basis` callable is recorded as the spec string, not the callable

- **Type / severity:** confirmed defect (provenance) / **Major**
- **Locations:** `src/nse_pipeline/backtest/signals.py:112`; `MomentumRanks.price_basis` declared `str` at `:33`; `MomentumSpec.price_basis` is a three-value choice at `specs/__init__.py:81,90`; the callable is the separate `basis_price` argument at `signals.py:15,72-73`
- **Evidence:** probe10 (`audit_probe10.py`):
  - two materially different callables both emit `price_basis='close'`; one computes `100→200` (100%) while the real close series is `100→120` (20%);
  - a mutated `price_basis='bogus_basis'` plus a callable is recorded verbatim as `'bogus_basis'`;
  - neither `__name__`, `__qualname__`, hash, module nor source appears in `MomentumRanks` or `asdict`.
- **Impact:** the ranked output's audit trail can describe a price series that was not used. Reproducing a ranking from the record is impossible. The doc line 148 ("does not record a custom basis callable") understates the risk: the field is populated with a possibly false label.

### F3 — Fully priced book is labelled `mixed_date_reference_only`

- **Type / severity:** confirmed defect (diagnostic) / **Minor**
- **Location:** `src/nse_pipeline/backtest/valuation.py:99`
- **Evidence:** probe7 constructs a book with `coverage=1`, `completeness=1`, `missing_reference=0` and receives `reference_coverage_reason='mixed_date_reference_only'`. No test references this field anywhere in `tests/`.
- **Impact:** a consumer distinguishing defensible from mixed-date marks can misclassify a fully priced book. It does not change NAV.

### F4 — NAV and marked value are not quantized to paise

- **Type / severity:** confirmed behavior / contract-ambiguous / **Minor**
- **Location:** `src/nse_pipeline/backtest/valuation.py:85,100-102` (no `money()`/`quantize`)
- **Evidence:** probe7: 100 shares at `1.00015` gives `nav=300.015`, `marked_value_priced=300.015`; the test-quality probe produced `8200.500` for a `80.005` mark.
- **Assessment:** fees, notionals and caps are paise-quantized, while NAV is not. The docs require "defensible NAV" but do not state a quantization rule, so this is a contract ambiguity rather than a proven violation. It should be decided explicitly before downstream reporting.

### F5 — `hold_receivable` creates a claim that the kernel can never settle

- **Type / severity:** confirmed limitation / **Major**
- **Locations:** `src/nse_pipeline/backtest/dividends.py:44-45` (returns `due=None`); `src/nse_pipeline/backtest/core.py:246-247` (raises when `claim.due is None`)
- **Evidence:** probe9b: after accrual, `dividend:D1` remains `200.00` with `due=None`; `core.settle` is impossible; there is no API to attach a later verified payment date or amount.
- **Impact:** NAV permanently includes a receivable that can never convert to cash, even if verified payment evidence arrives later. The doc calls this "hold-receivable payment scheduling" (line 40) and marks the dividend row PASS (line 119); the behavior is more accurately "hold forever, unreconcilable".

### F6 — `min_observations` counts whole history rather than the lookback window

- **Type / severity:** confirmed limitation (disclosed) / **Minor**
- **Locations:** `signals.py:66`; `evidence.py:120-121`
- **Evidence:** probe10: `SPARSE` has 15 valid observations total but only 3 in the last 20 sessions (4 including the anchor), yet is ranked because `min_observations=15`. A windowed reading would exclude it.
- **Disclosure:** docs lines 147–148. Tests indirectly pin the whole-history semantic (`test_accounting_signals.py:117-124,186-190`).

### F7 — `anchor_rule`, `observation_rule`, `tie_break` are dead configuration

- **Type / severity:** confirmed limitation / **Minor** (latent)
- **Locations:** declared/validated at `specs/__init__.py:82-85,91-93`; zero production reads (`signals.py` hardcodes exact session offset at `:94`, valid-observation counting at `:66`, key-ascending tie break at `:110`)
- **Evidence:** probe10 mutates each field on a constructed frozen spec; ordering and anchor are byte-identical to baseline.
- **Impact:** none today because each choice admits exactly one value. It becomes a real defect the moment a second value is added without wiring it into `signals.py`.

### F8 — Universe is an as-of liquidity screen, not point-in-time membership

- **Type / severity:** confirmed limitation (disclosed) / **Major**
- **Locations:** `universe.py:69-90`; `evidence.py:117-118`
- **Evidence:** probe10: `SUSPENDED` and `DELISTED` both appear only as `no_bar_on_session`; `GAPPED` (two intermediate missing sessions) is eligible because it has a valid bar on `as_of`; no constituent dates, suspension state or termination state exists.
- **Note:** all ten `UniverseSpec` fields **are** read (`universe.py:34,47,49,51,55,57,60,61,64,90`). The limitation is enforcement scope, not dead configuration.
- **Disclosure:** docs lines 154–156. The doc's PASS(component) at line 126 is defensible only for the named screen, not for index membership.

### F9 — ADV conflates a missing bar with a zero-volume session and returns `0.00` for a never-seen key

- **Type / severity:** confirmed limitation / **Major**
- **Locations:** `adv.py:52-56` (docstring), `:66-67` (skip missing bar), `:87-88` (capacity ignores `traded_sessions`)
- **Evidence:** the ADV subagent probe: one `traded_value=1000` bar in 20 sessions yields `total=1000, traded_sessions=1, value=50.00, capacity=2.50`; a key absent from every session yields `value=0.00, capacity=0.00`; explicit zero bars over 20 sessions yield `total=0, traded_sessions=20`.
- **Contract conflict:** `adv.py:1` says "unknown evidence yields None, never an estimate", but a never-seen key returns `0.00`, and a 1-of-20 sample is indistinguishable from a 20-of-20 sample except for the exposed-but-ignored `traded_sessions`.
- **Impact:** understates capacity for data gaps (conservative), dilutes a single large print over the full window, and breaks the nullable-unknown contract on which callers may rely.

### F10 — Boundary silently omits months with zero observed sessions

- **Type / severity:** confirmed limitation (disclosed, codified) / **Minor**
- **Locations:** `boundary.py:139-140,147-154`
- **Evidence:** probe7: sessions `2024-01-31, 02-29, 05-31, 06-28` emit signals for Jan, Feb and May only; March/April leave no decision, counter or `_signaled` entry. `tests/test_accounting_boundary.py:82-85` explicitly asserts that an unobserved February gets no signal. Docs line 127 marks causal month-end NOT IMPLEMENTED.
- **Impact:** a downstream consumer cannot distinguish "no rebalance required" from "data absent for two months" without an independent calendar comparison.

### F11 — `KnownCalendar` performs no business-day or holiday validation

- **Type / severity:** confirmed limitation / **Minor**
- **Locations:** `boundary.py:25-57` (only nonempty unique dates, sort, `dates[-1]`), `:59-71` (`check` only compares observed sessions to the supplied list)
- **Evidence:** the boundary subagent probe: a calendar containing Saturday `2024-06-29` is accepted and signals that Saturday; a calendar excluding `2024-06-17` rejects an observed session on that date; uncovered months silently fall back to observed-month change.
- **Impact:** correctness depends entirely on caller-supplied calendars, consistent with docs lines 154–156 ("must be independently verified upstream") but not stated for the calendar specifically.

### F12 — `PortfolioSpec` has no production consumer and its default sizing policy is rejected by `sizing`

- **Type / severity:** confirmed limitation + incomplete doc / **Major**
- **Locations:** `specs/__init__.py:168-194` (default `sizing_policy="P2_size_excluding_unresolved"` at `:172`); `sizing.py:11` (`POLICIES=("P1","P2","P3")`); `tests/test_accounting_sizing.py:154` explicitly rejects the long name
- **Evidence:** grep across `src/` finds `PortfolioSpec` only in its own declaration and `validate_policy_combination`; all ten fields are unread. `size_orders("P2_size_excluding_unresolved", ...)` raises `ValueError`.
- **Doc issue:** line 149–150 says callers must map the long names, but no mapper exists and no production path consumes a `PortfolioSpec`; a caller cannot map the *default* by identity. The doc discloses a vocabulary mismatch but not a zero-consumer interface.

### F13 — `validate_policy_combination` is never called in production

- **Type / severity:** confirmed limitation / **Minor**
- **Location:** `specs/__init__.py:218-222`; only `tests/test_specs.py:97-108` imports it
- **Impact:** P3 sizing can be run without `ValuationPolicy.sizing_uses_screen_value=True`, and P1/P2 without the corresponding policy validation, because nothing enforces the cross-spec combination.

### F14 — Declared spec fields that no production code reads

- **Type / severity:** confirmed dead configuration / **Minor**
- **Evidence:** `audit_fields.py` AST/regex survey plus manual confirmation:

  | Spec | Unread fields | Declared at |
  |---|---|---|
  | `MomentumSpec` | `anchor_rule`, `observation_rule`, `tie_break` | `specs/__init__.py:82,83,85` |
  | `ExecutionSpec` | `fill_timing`, `max_participation_rate`, `settlement_funding`, `unfilled_remainder`, `circuit_limit_pct`, `hold_suspended_positions` | `:99,103,108,109,110,111` |
  | `ValuationPolicy` | `dividend_cash_phase`, `series_precedence`, `carried_position_is`, `absence_review_sessions`, `sizing_uses_screen_value`, `allow_scenario_statistics` | `:138-143` |
  | `CostSpec` | `schedule_basis`, `monetary_rounding` | `:206,207` |
  | `PortfolioSpec` | all ten fields | `:169-178` |

- **Impact:** validated configuration creates a false impression of implemented behavior. Each field admits one value today (except the boolean/participation fields), so the risk is latent, but it is a specification-surface credibility issue.

### F15 — `Bar` accepts `valid_ohlcv=True` with all OHLC fields `None`

- **Type / severity:** confirmed limitation / **Minor**
- **Location:** `observations.py:31-45`
- **Evidence:** probe10: `Bar(..., open=None, high=None, low=None, close=None, valid_ohlcv=True)` is accepted.
- **Mitigation:** consumers fail closed: `signals.py:48` returns no close basis, `universe.py:53-54` reports `price_unknown`. No silent valuation occurs.

### F16 — Committed verification artifacts do not cover seven post-baseline commits

- **Type / severity:** confirmed provenance gap (disclosed) / **Major**
- **Locations:** `verification/accounting-focused.xml` (90 cases), `verification/accounting-regression.xml` (940 cases); `foundation_commit=head_at_verification=76adf010...` in `verification/accounting-component-results.json:3-4`
- **Evidence:** artifacts were generated from the dirty tree that became `228f6cf`; commits `19b0e33`, `c6d1b14`, `3405330`, `23c155d`, `8e4e0e2`, `0c80814`, `f5e9430` have no regenerated JUnit. No post-baseline module (`test_accounting_identity`, `_observations`, `_signals`, `_universe`, `_adv`, `_boundary`, `_evidence`, `_sizing`) appears in either artifact.
- **Disclosure:** docs lines 142–143. The gap is real but candidly stated; the audit re-ran the suite at HEAD to compensate (1,129 passed, 5 skipped, 129 subtests).

### F17 — Machine report contradicts the doc matrix at HEAD

- **Type / severity:** confirmed contradiction / **Major**
- **Locations:** `scripts/report_stage1_accounting.py:15-46` (hardcoded `IMPLEMENTED`/`DEFERRED`), `:84-86` (deferred rows forced to `NOT IMPLEMENTED`); `verification/accounting-component-results.json`
- **Evidence:** re-running the report at HEAD produces 14 PASS / 14 NOT IMPLEMENTED. `identity`, `universe`, `signals`, `adv`, `boundary`, `evidence` are `NOT IMPLEMENTED` in JSON, while `docs/STAGE1_ACCOUNTING_VERIFICATION.md:124-129` calls them PASS(component) or credits their modules. `sizing_budget` is mapped to `("core","fees")`, so `tests/test_accounting_sizing.py` is never consulted (F20).
- **Impact:** the authoritative machine-readable artifact and the human matrix disagree about the same commit. A consumer reading only JSON would conclude the post-baseline components do not exist.

### F18 — Authorization checks are weaker than the doc implies

- **Type / severity:** confirmed governance weakness / **Major**
- **Locations:** `scripts/report_stage1_accounting.py:64-69,102-110`; `scripts/verify_stage1_inventory.py:47-49,65`
- **Evidence:**
  - `head_at_verification` is recorded (`:109`) but never compared with `foundation_commit` (`:108`); running the report at `f5e9430` succeeds and labels the artifact with the baseline.
  - `integration_authorized` (`:64-69`) checks row completeness/pass status and regression counts, but not that the JUnit came from the current HEAD.
  - `integrity_ok` (`:102-104`) requires `only_accepted_exception` to be true, which is defined as **exactly one** acknowledged change (`verify_stage1_inventory.py:48`). A perfectly clean inventory would produce `only_accepted_exception=false` and therefore fail `integrity_ok`, even though `all_hashes_match=true`.
  - The inventory CLI exits `0` when `all_hashes_match` is true even if `only_accepted_exception=false` (`:65`), so the CLI and the report disagree about a clean tree.
- **Impact:** the gate is fail-closed for the current state, but the checks are coupled to one historical exception rather than to the integrity condition, and they do not bind evidence to the tested revision.

### F19 — Doc internal inconsistency about skips

- **Type / severity:** documentation defect / **Minor**
- **Locations:** `docs/STAGE1_ACCOUNTING_VERIFICATION.md:132` ("FAIL / SKIP | None ... no deferred family mislabeled SKIP") vs `:139` ("1,129 passed, 5 skipped")
- **Evidence:** baseline artifacts have 0 skips; the HEAD rerun has 5 skips, all optional PDF tests (`pypdf`/`fitz` absent from the current `.venv`; the foundation venv has both, which explains the baseline 0). The two statements are about different commits but are presented in the same document without that qualifier.

### F20 — `sizing_budget` report row is backed by the wrong test module

- **Type / severity:** report defect / **Minor**
- **Location:** `scripts/report_stage1_accounting.py:24`
- **Evidence:** `"sizing_budget": (..., ("core","fees"))`. `tests/test_accounting_sizing.py` is never referenced by the report, so the P1/P2/P3 sizing tests do not contribute to the row that claims "rank-sequential supplied budgets".

### F21 — Sizing test docstring claims P1/P2/P3 golden coverage; no P1 golden exists

- **Type / severity:** test documentation defect / **Minor**
- **Locations:** `tests/test_accounting_sizing.py:1`; `tests/golden/execution_cases.json` (only `"policy": "P2"` and `"policy": "P3"`)
- **Evidence:** P1 is covered only by an inline test (`:91-98`); the P3 "screen value" test (`:111-118`) is non-discriminating because `size_orders` has no screen-value input and the case has sufficient cash.

### F22 — `data/README.md` is inventoried but untracked

- **Type / severity:** reproducibility gap / **Minor**
- **Evidence:** inventory entry `data/README.md`, sha `230470d3...`, 2,647 bytes; `git ls-files data/README.md` empty; `git check-ignore` does not match it (it is not ignored, merely untracked). A clean clone without this file makes the inventory verifier report an `OSError` for that entry.
- **Note:** the file pre-existed the audit and was not created or modified by it. `data/README.md` is present in the inventory; the gap is git tracking, not inventory omission.

### F23 — "Kernel does not infer ... signals ... ADV" is stale relative to post-baseline modules

- **Type / severity:** documentation defect / **Minor**
- **Location:** `docs/STAGE1_ACCOUNTING_VERIFICATION.md:165-168`
- **Evidence:** `signals.py` computes momentum ranks, `adv.py` computes trailing ADV and capacity, `universe.py` builds membership, `boundary.py` schedules month-end fallback. The paragraph's "kernel" qualifier can be read as `core.py` only, but the paragraph is the remaining-limitations statement for the whole Stage-1 slice, and line 129 credits those modules as PASS(component).

---

## D. Accounting reconciliation

### D1. Eight golden cases — independent reproduction

`tests/test_accounting_reconciliation.py` was run directly: **13 passed**. The eight case IDs and their terminal expectations match the doc table (lines 64–75):

| Case | Independently expected terminal state | Test result |
|---|---|---|
| `reservation_cancel` | Cash/NAV 1,000; availability 700 while reserved, then 1,000 | PASS |
| `prefunded_purchase` | Fill: cash 1,000, liability 310, reserve 50, shares 8, available 690, NAV 1,540; settlement: cash 690, liability 0, NAV unchanged | PASS |
| `unsettled_sale` | Fill: cash 1,000, sale receivable 199, shares 3, NAV 1,499; settlement: cash 1,199, receivable 0 | PASS |
| `dividend_lag` | Accrual: cash 10,000, dividend receivable 2,000; payment: cash 12,000, receivable 0; NAV 112,000 on the flat quote | PASS |
| `split_up` | 100 raw shares → 200; quote 50 conserves 10,000; quote 51 values 10,200 | PASS |
| `split_down` | 200 raw shares at 49 value 9,800; mechanical effect removed, actual movement retained | PASS |
| `absence_reopening` | 100 shares; NAV absent with no current mark; reopened at 80 gives NAV 8,200 including cash 200 | PASS |
| `verified_proceeds` | Cash stays 200 while other receivable is 1,200; verified payment moves cash to 1,400 and receivable to zero | PASS |

### D2. Golden independence

- `tests/golden/*.json` were added at `76adf010`; production accounting was added at `228f6cf`.
- `git diff 76adf010..HEAD -- tests/golden/` is empty: the fixtures were never adjusted to match the implementation.
- `accounting_cases.json` and `execution_cases.json` carry `status: independent_oracle_only_not_production_P0_pass`; `valuation_cases.json` is a list without a status field.
- Negative replay tests at `tests/test_accounting_reconciliation.py:94-104` detect changed snapshots, truncated event streams and duplicated terminal events.

### D3. Replay independence

- `invariants.py` imports only stdlib (`collections`, `dataclasses`, `datetime`, `decimal`) and `accounting_types`; it imports no `core`, `fees` or `valuation`.
- Runtime probe4 wraps `core`, `fees` and `valuation` public callables with counters and runs `reconcile`: the call counts remain zero and the reconciliation passes.
- **Important caveat:** independence is structural, but not econometric. `invariants.py:39-40,62` re-implements the same half-up paise rounding and fee policy as production. It therefore cannot detect a shared rounding defect such as F1. The golden JSON is the only genuinely independent oracle for those values.

### D4. Fee and coverage controls

- Fee quote: `1,000 × 100.05 = 100,050` notional; per-component paise rounding gives brokerage `20.01`, GST `3.60`, STT `100.05`, stamp `15.01`, exchange `3.00`, total `141.67`; payable `100,191.67`. Verified against `execution_cases.json` and the production test.
- Coverage: operational completeness 1/2 and mixed-date reference coverage 2/3 → 1/6, with an unavailable ratio when a reference is missing. Verified against `valuation_cases.json` and `test_accounting_valuation.py`.
- The golden cases use whole-paise prices and caps, which is why F1 and F4 escape them.

---

## E. Causality and look-ahead controls

The audit attempted to break causality in five places. Four attempts were **refuted**; the fifth confirmed a disclosure.

| Control | Attempt | Result |
|---|---|---|
| Session ordering | Duplicate/earlier session, `datetime`, string date, duplicate key, re-ingest, bar/session mismatch | All raise; state unchanged. `observations.py:60-64`; `evidence.py:73-84` |
| Duplicate handling | Silent dedupe/overwrite | None; every duplicate path raises. `SessionStream` duplicate, `ObservationLedger` duplicate key, `RebalanceSchedule` duplicate all raise |
| Future `available_at` | Calendar available after the decision; `Evidence.require_applicable` before availability; bar used before close | Calendar excluded; `Evidence` raises; bar unavailable before `CLOSE_VALUATION`. `boundary.py:132-135`; `accounting_types.py:194-196`; `observations.py:47-49` |
| As-of queries | Future appends changing earlier `history`, `valid_count`, `keys_as_of` | All as-of queries are bounded by session index; earlier results unchanged. `evidence.py:117-133` |
| Late boundary catch-up | A month-end bar arriving after the next month | Explicit `OBSERVED_MONTH_CHANGE` with `late=True`; out-of-order ingestion rejected; two month-ends at one close raise and roll back. `boundary.py:139-146` |
| Month-gap omission | Months with no observed sessions | **Confirmed limitation** (F10): no decision records the miss; the behavior is codified by `tests/test_accounting_boundary.py:82-85` |

No look-ahead was found in identity, observations, evidence, signals, universe, ADV, boundary or valuation. `Bar.available_at` is derived from its own session's `CLOSE_VALUATION`, and valuation requires `mark.on == at.session` and `mark.available_at <= at` (`valuation.py:82-83`).

---

## F. Architecture and dependency isolation

### F1. Surface and packaging

- `src/nse_pipeline/backtest/__init__.py` is a one-line docstring; no imports, no `__all__`. No module in `backtest/` or `specs/` declares `__all__`.
- Importing `nse_pipeline.backtest` loads no `pandas`, `numpy` or `pyarrow`; only `nse_pipeline`, `.config`, `.contracts`, `.paths`.
- `specs/__init__.py` imports only `dataclasses`, `json`, `math`.
- The package is 23 files / 22 importable modules. The 15 accounting modules are `accounting_types`, `core`, `fees`, `share_actions`, `dividends`, `valuation`, `invariants`, `identity`, `observations`, `evidence`, `adv`, `boundary`, `signals`, `universe`, `sizing`; the other eight are legacy (`engine`, `freeze`, `history`, `sensitivity`, `series_fallback`, `successor`, `total_return`) plus `__init__`.

### F2. Import graph

- AST probe over all 22 importable modules: **0 cycles**.
- Accounting-core edges form a DAG. Module-level imports outside the core are limited to stdlib, `nse_pipeline.specs`, and sibling accounting modules.
- `core.py` lazily imports `.dividends` at `:328`; no cycle.
- No accounting module imports `nse_pipeline.strategy` (which does not exist), `get_data`, `requests`, `pyarrow`, or the legacy `engine`/`history`/`sensitivity` modules.
- Runtime confirmation: importing `nse_pipeline.backtest.core` leaves `pandas`, `duckdb`, `engine` and `get_data` unloaded.

### F3. Legacy isolation and the `src/get_data.py` change

- `git diff 76adf010..HEAD -- src/get_data.py` is two insertions: the `require_writable_output` import and one pre-`mkdir` guard call. No fetch/parse/manifest/adjustment logic changed.
- The guard (`verification.py:29-37`) raises only while `NSE_VERIFY_PROTECTED_ROOTS` is active; normal pipeline behavior is unchanged.
- Legacy modules still import `pandas` and the data catalog (`engine.py:48-52`, `series_fallback.py:76-80`, `successor.py:42-45`, `total_return.py:78-81`, `freeze.py:45-46`, `history.py:21-22`, `sensitivity.py:80-81`), but the empty `__init__` isolates them.

### F4. Untracked `analytics` package

- `src/nse_pipeline/analytics/__init__.py` is 101 bytes, untracked, not ignored, imports nothing, and has zero importers across `src`, `tests`, `scripts`, `docs` and `pyproject.toml`.
- `pyproject.toml` has `[tool.uv] package = false` and no build-system; pytest uses `pythonpath = ["src"]`.
- It is inert today and cannot affect the data inventory (which is scoped to `data/`).

### F5. Test isolation

- `tests/conftest.py:9-13` installs `protect_artifacts([repo/data])` at `pytest_configure`, before collection; `pytest_unconfigure` exits it.
- The guard blocks Python writes/deletes/renames/mkdir/chmod/utime under `data/`, and on Windows opens every pre-existing file with `FILE_SHARE_READ` so native overwrites fail.
- Caveat: a brand-new file created by a non-Python child process under `data/` is not blocked by the Windows handles; only the Python audit hook would catch it. The module docstring explicitly disclaims being a general sandbox.
- This audit did not run any command that writes under `data/`.

---

## G. Test quality

### G1. Counts and execution

| Suite | Collected | Result | Notes |
|---|---:|---|---|
| Focused (9 files) | 90 | **90 passed** in 3.18s | Exact doc command re-run at HEAD |
| Full `tests/` | 1,134 | **1,129 passed, 5 skipped, 129 subtests passed** in 144.46s | 5 skips are optional PDF tests (`pypdf`/`fitz` absent in `.venv`) |
| Post-baseline accounting modules | 194 | **194 passed** in 0.42s | `identity` 58, `observations` 23, `sizing` 36, `adv` 11, `boundary` 30, `evidence` 4, `signals` 19, `universe` 13 |
| Reconciliation | 13 | **13 passed** in 0.09s | Eight golden IDs + negative replay tests |

Baseline artifacts are internally consistent: focused XML 90 cases, regression XML 940 cases, 0 fail/error/skip; JUnit `tests` attr 1069 = 940 + 129 subtests. The difference between baseline 0 skips and HEAD 5 skips is fully explained by the current venv lacking the optional PDF dependencies that the foundation venv has.

### G2. Confirmed coverage gaps

1. **No P1 golden fixture.** `execution_cases.json` has P2/P3 only; the P1 test is inline. The module docstring overstates golden coverage (F21).
2. **P3 test is non-discriminating.** `test_p3_screen_value...` (`test_accounting_sizing.py:111-118`) cannot distinguish screen-value sizing from ordinary sizing: `size_orders` has no screen-value parameter and the case has enough cash.
3. **No custom `price_basis` provenance test.** Only the default `"close"` is asserted (`test_accounting_signals.py:80`); the F2 behavior is untested.
4. **No NAV quantization test.** All valuation tests use whole-rupee marks; F4 is untested.
5. **`reference_coverage_reason` is never asserted anywhere.** F3 is untested.
6. **No zero-session-month record test; the opposite is asserted** (`test_accounting_boundary.py:82-85`).
7. **ADV window tests are all default-3** for exact capacity values; `spec(window=2)` appears only in a before/after equality, so a hardcoded window would pass.
8. **No `PortfolioSpec` ↔ `sizing` integration test.** The only test asserts the spec's own default string is rejected (`test_accounting_sizing.py:154`).
9. **Three identity issue reasons untested:** `ambiguous_retirement` (`identity.py:241`), `unbound_key_among_multiple_live_keys` (`:201`), `ambiguous_new_key` (`:280`).
10. **No direct replay test file.** `tests/test_accounting_replay.py` does not exist; `reconcile` is exercised only through the eight golden cases.
11. **Sizing reasons `no_request` and `below_one_share` untested.**
12. **`bars_on` and `session_index` have no direct tests** (only indirect use).

### G3. Test-quality strengths

- Exact-value assertions dominate: full dataclass equality in boundary and evidence, exact `Fraction` momentum, exact fee breakdowns, exact reason tuples.
- Negative replay tests are genuinely adversarial (mutated snapshot, truncated stream, duplicated terminal event).
- Append-invariance tests are used repeatedly for causal queries.
- Four strict xfails from `c6d1b14` were genuinely removed; all four tests exist at HEAD without markers, and `xfail` appears zero times in `tests/`.
- `tests/test_accounting_report.py` has 18 hand-authored authorization regressions and deliberately checks that missing/skipped/failed components cannot authorize integration.

### G4. Weak/self-referential assertions

- `test_fixture_dates_are_hand_checked` (`test_accounting_boundary.py:46-49`) tests the fixture, not production.
- `catch_up` is compared to `run(...)` (`:251`), which uses the same `observe` loop; later assertions do add independent behavior.
- `tests/test_accounting_identity.py:44-45` constructs the expected observation with the same constructor as the actual.
- The forward/backward signal equality at `test_accounting_signals.py:95-97` uses the same function, though exact tie order is also asserted.

---

## H. Dataset integrity

### H1. Independent re-verification at HEAD

| Metric | Recorded | Recomputed | Verdict |
|---|---:|---:|---|
| `checked_files` | 8,153 | 8,153 | MATCH |
| `raw_files_checked` | 6,835 | 6,835 | MATCH |
| `database_files_checked` | 1 | 1 (`data/market.duckdb`) | MATCH |
| `errors` | 0 | 0 | MATCH |
| `unexpected_changes` | 0 | 0 | MATCH |
| `inventory_sha256` | `a4e3ac40...b7edee` | same | MATCH |
| `acknowledged_unrecovered_changes` | 1 | 1 | MATCH |
| `all_hashes_match` | false | false | MATCH |
| `only_accepted_exception` | true | true | MATCH |

Subtree counts: `data/raw/` 6,835; `data/processed/` 1,218; `data/reports/` 91; `data/manifests/` 7; `data/market.duckdb` 1; `data/README.md` 1.

### H2. The sole exception

`data/processed/dataset/dataset_manifest.json` is recorded as `5547ac45...` and currently hashes to `c304d08e...`. It is classified as acknowledged/unrecovered, not as a clean match. The file is ignored by git (`.gitignore:5`, `data/processed/`) and is not tracked. The baseline inventory was not rewritten.

### H3. Spot checks

- 200 random inventory entries re-hashed: 0 mismatches.
- The three largest files re-hashed: `data/raw/PR_bhav-master.zip` (1,350,215,625 bytes), `data/raw/nseBhav.zip` (395,945,492), `data/processed/adjusted/adjusted_bars.parquet` (399,315,367) — all match.
- Duplicate inventory paths: 0.
- No inventory entry escapes the checkout (the verifier raises if one does).

### H4. Reproducibility caveat

`data/README.md` is inventoried but untracked (F22). A clean clone without it will make `verify_stage1_inventory.py` report an `OSError` for that entry and exit non-zero. This is not a hash mismatch, but it is a reproducibility dependency that the inventory does not record as an external prerequisite.

### H5. What the audit did not do

No dataset file, manifest, inventory or report was created, deleted, renamed or rewritten. The inventory verifier was run with `--out` outside `data/`. The test suite's `protect_artifacts` guard was active for every pytest run.

---

## I. P0 gate matrix

The doc matrix mixes 14 implemented rows, 14 deferred rows, and six component-level PASS claims. The audit's status is derived from code reads, test execution and the machine report — not from the doc text.

| Contract / row | Doc status | Audit status | Basis |
|---|---|---|---|
| Settled cash, reservations, payables | PASS | **PASS (verified)** | Core tests + golden `prefunded_purchase`, `reservation_cancel` |
| Fee-inclusive integer buy/sell limits | PASS | **PASS (qualified: F1)** | Golden fee quote passes; sub-paise cap defect permits over-cap notional |
| Verified-key raw-share split/bonus/quarantine | PASS | **PASS (verified)** | `split_up`, `split_down`, actions tests |
| Normal cum/ex entitlement; accrual; verified/lag/hold settlement | PASS | **PASS (qualified: F5)** | Verified/lag settlement works; `hold_receivable` claim can never settle |
| Defensible NAV/gaps; operational/reference coverage; unknown-asset scenarios | PASS | **PASS (qualified: F3, F4)** | NAV blocks on unknown assets; coverage reason mislabelled; NAV not paise-quantized |
| Explicit verified consideration receipt | PASS | **PASS (verified)** | `verified_proceeds` + core tests |
| Named zero-friction liquidation | PASS | **PASS (verified)** | Valuation tests; scenario values explicitly hypothetical |
| Independent typed-event replay; immutable IDs; unresolved occupied slots | PASS | **PASS (verified, with caveat)** | 8 golden replays pass; replay shares production rounding (F1 caveat) |
| Artifact guards; inventory checker; fail-closed report authorization | PASS | **PASS (qualified: F18)** | Guard and 18 report regressions pass; HEAD/integrity coupling weaknesses |
| Causal identity keys, dated edges/retirements; `Bar` observations | PASS (component) | **PARTIAL** | Code + 58/23 tests pass; JSON report still says `identity: NOT IMPLEMENTED` |
| Documentary continuity | NOT IMPLEMENTED | **NOT IMPLEMENTED (verified)** | No module |
| Scoped historical adjustment | NOT IMPLEMENTED | **NOT IMPLEMENTED (verified)** | No module |
| Spec-driven universe; exact causal momentum ranks | PASS (component) | **PARTIAL** | Code + 32 tests pass; screen not PIT membership (F8); JSON says NOT IMPLEMENTED |
| Causal month-end scheduling | NOT IMPLEMENTED | **NOT IMPLEMENTED (verified)** | Only observed-month fallback; zero-session months omitted (F10) |
| Full lifecycle/series/termination; evidence-counting contracts | NOT IMPLEMENTED | **NOT IMPLEMENTED (verified, ambiguous row)** | Broad contract absent; `evidence.py` component exists and line 129 credits it |
| P1/P2/P3 rank-sequential sizing; trailing ADV; evidence ledger; boundary | PASS (component) | **PARTIAL** | Sizing/ADV/evidence/boundary code+tests exist; P1 no golden; ADV F9; boundary F10/F11; JSON says NOT IMPLEMENTED |
| Frozen-order perturbation and economic/provenance prefix contracts | NOT IMPLEMENTED | **NOT IMPLEMENTED (verified)** | No module |
| Separate scenario portfolios/statistics isolation; friction-aware liquidation | NOT IMPLEMENTED | **NOT IMPLEMENTED (verified)** | No module; scenario statistics rejected |
| FAIL/SKIP | "None" | **CONTRADICTED for HEAD** | 5 optional PDF skips at HEAD (F19) |
| Overall mandatory P0 gate | **CLOSED** | **CLOSED (verified)** | 14 PASS / 14 NOT IMPLEMENTED; `integration_authorized=false` |

**Gate conclusion:** no row is upgraded from NOT IMPLEMENTED to PASS by this audit, and no qualified PASS is promoted to an unqualified PASS. The gate is correctly CLOSED and migration remains unauthorized.

---

## J. Remediation roadmap

Ordered by risk, each item tied to a finding. These are targeted corrections, not refactoring proposals.

| Priority | Action | Finding | Scope |
|---:|---|---|---|
| 1 | Decide the `max_order_notional` contract: floor the cap (never exceed) or document a paise-rounded limit. Add sub-paise cap tests for both buy and sell and align `invariants.py:62`. | F1 | `fees.py:65,85`; `accounting_types.py:27-29`; `invariants.py:62`; `test_accounting_core.py:288-300` |
| 2 | Record custom basis provenance: callable `__qualname__`/module or a stable hash, plus a flag distinguishing built-in from callable. Add the two-callable test. | F2 | `signals.py:33,112`; `test_accounting_signals.py` |
| 3 | Resolve `hold_receivable`: either add an explicit settlement/attachment API for later verified payment evidence, or make NAV exclude the receivable and label it permanently unrealizable. | F5 | `dividends.py:44-45`; `core.py:246-247`; golden/doc |
| 4 | Bind the report to the tested revision: fail if `HEAD != foundation_commit` (or require an explicit `--allow-post-baseline`), and regenerate JUnit for post-baseline modules. | F16, F18 | `report_stage1_accounting.py:64-69,108-110`; `verification/` |
| 5 | Reconcile the machine report with the doc matrix: move implemented post-baseline components into `IMPLEMENTED` with their real test modules, and map `sizing_budget` to `test_accounting_sizing`. | F17, F20 | `report_stage1_accounting.py:15-46` |
| 6 | Define `reference_coverage_reason` semantics and add a fully-priced test. | F3 | `valuation.py:99`; valuation tests |
| 7 | Decide NAV quantization and test a sub-paise mark. | F4 | `valuation.py:85,100-102` |
| 8 | Make ADV fail closed on insufficient coverage: return `None` for a never-seen key and/or require a minimum `traded_sessions / window`, or document the zero-fill assumption as the contract and remove the "None" docstring promise. | F9 | `adv.py:1,52-56,66-67,87-88` |
| 9 | Emit an explicit skipped-month record (or a diagnostic counter) in the boundary schedule. | F10 | `boundary.py:139-154` |
| 10 | Wire or delete `PortfolioSpec` and `validate_policy_combination`; either add the long-name mapper or change the default to `"P2"`. | F12, F13 | `specs/__init__.py:168-222`; `sizing.py:11` |
| 11 | Remove, wire, or mark as reserved the unread spec fields; add a test that fails when a validated field has no production reader. | F7, F14 | `specs/__init__.py`; signals/execution/valuation |
| 12 | Add calendar validation (weekday/known-holiday) or document that calendars are raw caller assertions. | F11 | `boundary.py:25-57` |
| 13 | Track `data/README.md` or record it as an external prerequisite in the inventory. | F22 | `data/README.md`; inventory docs |
| 14 | Fix doc contradictions: skips at HEAD, "kernel does not infer signals/ADV", and the sizing golden claim. | F19, F21, F23 | `docs/STAGE1_ACCOUNTING_VERIFICATION.md`; `test_accounting_sizing.py:1` |

**Gate sequencing:** items 1–3 qualify currently-PASS rows and should be resolved before those rows are used as a migration baseline. Items 4–5 are verification-integrity prerequisites for any future authorization. Items 6–14 are completeness/quality work. None of them changes the present gate from CLOSED.

---

## K. Audit verdict and scope statement

### Verdict

1. **Documentation accuracy:** largely accurate and candid. Of 37 checked claims, 27 are fully verified, 7 are verified with qualification, 1 is partial, and 2 are contradicted or stale as written (the skip line, and "kernel does not infer signals/ADV"). The doc's central quantitative claims — counts, hashes, golden values, fee quote, coverage ratios, xfail history — all reproduce.
2. **Accounting slice:** materially sound. Eight independent golden cases pass against production and replay; causality/ordering/as-of controls hold; the accounting modules are dependency-isolated from data fetching and legacy code; no look-ahead was found.
3. **Accounting slice is not defect-free:** F1 (cap rounding) and F2 (provenance mislabel) are confirmed defects in implemented components; F3 is a confirmed diagnostic defect; F5 leaves an economic state permanently unreconcilable. These qualify, but do not erase, the affected PASS rows.
4. **Verification layer:** effective as a fail-closed report generator for the baseline state, but not a revision-bound attestation. It does not compare HEAD to the foundation, its integrity check is coupled to exactly one historical exception, and its machine-readable component list contradicts the current doc matrix.
5. **Overall P0 gate:** correctly **CLOSED**. The audit does not reopen it, does not authorize migration, and does not promote any NOT IMPLEMENTED contract.
6. **Dataset:** the 8,153-file inventory, 6,835 raw files and single DuckDB file were independently re-verified at HEAD with zero errors and zero unexpected changes; the sole exception is the disclosed manifest mismatch. No dataset state was modified.

### Explicit non-claims

- This audit did **not** verify the legacy engine, runner, strategy logic, data acquisition pipeline, adjusted-price construction or any investment performance.
- This audit did **not** run any legacy strategy simulation, generate performance metrics, or publish returns.
- This audit did **not** modify production code, tests, docs, datasets, manifests, inventories, golden fixtures, verification artifacts or `.git`.
- This audit did **not** certify that the unimplemented 14 P0 contracts are unnecessary; it confirms only that they remain unimplemented and that the gate remains closed.
- Findings marked "limitation" are not assertions of arithmetic error; they are scope/contract gaps, some of which the documentation already discloses.

### Final state

`git status --porcelain` after writing this file:

```
?? STAGE1_INDEPENDENT_TECHNICAL_AUDIT.md
?? data/README.md
?? src/nse_pipeline/analytics/
```

The first entry is this deliverable. The latter two pre-existed the audit and were not created, modified or staged by it.

---

## Appendix: reproducibility

### Commands executed (read-only or temp-output only)

```powershell
git rev-parse HEAD
git status --porcelain=v1
git diff 76adf010..HEAD --name-only -- tests/golden
git diff 76adf010..HEAD -- src/get_data.py
uv lock --check
git diff --check
python -m pytest tests/test_accounting_core.py tests/test_accounting_fees.py tests/test_accounting_actions.py `
  tests/test_accounting_dividends.py tests/test_accounting_valuation.py tests/test_accounting_reconciliation.py `
  tests/test_accounting_report.py tests/test_verification_isolation.py tests/test_inventory_verification.py `
  -q --no-header -p no:cacheprovider
python -m pytest tests -q --no-header -p no:cacheprovider
python scripts/report_stage1_accounting.py --out <temp>/stage1_report_head.json
python <temp>/audit_probe2.py      # inventory re-hash
```

All `--out` paths and probe scripts are under `C:\Users\Kaustuvh\AppData\Local\Temp\opencode\`.

### Probe inventory

| Probe | Purpose |
|---|---|
| `audit_probe1.py` | Affordability monotonicity/soundness, sell bounds |
| `audit_probe2.py` | Inventory re-hash, 200 random entries, three largest files, duplicate paths |
| `audit_probe3.py` | Cap rounding, identity chains, sizing |
| `audit_probe4.py` | Evidence hashes, artifact timestamps, replay independence |
| `audit_probe5.py` | Spec-field production-read survey |
| `audit_probe6.py` | Sub-paise maximum-cap admission |
| `audit_probe7.py` | Valuation reason, sub-paise NAV, ADV conflation, boundary gaps |
| `audit_probe8.py` | Four strict xfails from `c6d1b14` to HEAD |
| `audit_probe9.py`, `audit_probe9b.py` | Dividend causality, `hold_receivable`, split-with-unsettled, fee boundary |
| `audit_probe10.py` | Signals whole-history count, custom basis provenance, universe scope, dead momentum rules |
| `audit_fields.py` | Declared-field read survey |
| `audit-inventory.json` | HEAD inventory verification output |
