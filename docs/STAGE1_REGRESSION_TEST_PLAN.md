# Stage-1 Regression Test Plan

**Status:** planning artifact, written before implementation. No test code, production code, golden, dataset, manifest, inventory, verification artifact, or git history is changed by this document.
**Repository:** `D:\fix_Investment_backtesting`
**Base commit:** `f5e943000764fade242b0482d67db2808991c076`
**Overall P0 gate:** **CLOSED.** This plan does not authorize migration, promotion of a deferred row, performance publication, or any claim that the audited defects are already fixed.
**Scope:** the Stage-1 accounting slice, the supporting verification/report layer, and P0 contracts A-H defined below. Legacy engine, ingestion, adjusted-price construction, and strategy performance remain out of scope.

## 1. Purpose and red-first rule

This document specifies regression tests **before** implementation. A test may name a planned API or field that does not exist yet; such a test starts red and must fail for the intended reason, not because of a fixture or import accident. Each test states its inputs, an independently derived expected result, the invariant it protects, and the condition under which it must fail. Tests are grouped by batch so that a defect cannot be closed by an unrelated green suite.

**Oracle independence rule.** No expected value may be obtained by calling the function under test, importing a production helper to derive the answer, copying a production formula into the assertion, or reading a value that the implementation just wrote. Expected values come only from:

1. hand arithmetic recorded in the test or its `working` field;
2. a test-only oracle implementing the definition independently (for example, brute-force integer search over quantities or exact `Fraction` arithmetic), using only the standard library;
3. an immutable JSON golden that predates the implementation and is never edited to match it.

**Golden governance.** `tests/golden/accounting_cases.json`, `execution_cases.json`, and `valuation_cases.json` are immutable. New vectors go into new files listed in section 13. Existing expected values are never rewritten. Every new golden carries `schema_version`, `derivation`, and a SHA-256 over its canonical JSON; the digest is computed by the test harness outside the code under test. A golden change requires an explicit plan amendment and a new case ID, never an in-place edit.

**Money and precision convention.** All monetary expectations are `Decimal`, never `float`. Per-component fees and settled cash are quantized to paise with `ROUND_HALF_UP`; valuation keeps an exact, authoritative `nav` and quantizes only once at the reporting boundary into `nav_reported`. There is no per-event NAV rounding, no cumulative drift, and strategy returns use the exact `nav` series; `nav_reported` is presentation-only and never feeds an event, settlement, or return calculation. Tests assert both values where both are observable. No test compares `Decimal` to `float` or relies on `==` between different numeric types without an explicit conversion. U-08 is resolved by D-03 and is not a prerequisite or blocking item.

**Test-family rule.** Every test has exactly one primary family from section 3 and at least one finding/contract tag. A test may cover more than one finding only when the expected result discriminates the same defect cluster; the primary tag is the one that owns the failure.

## 2. Contract vocabulary A-H

| Contract | Name | Required guarantee |
|---|---|---|
| A | Settlement and custody continuity | Every trade, settlement, reservation, claim, and release is an ordered event; value and share counts reconcile at every phase and after terminal settlement. |
| B | Scoped adjustment | A split, bonus, or dividend adjustment applies only to positions proven in scope on the effective date; it never mutates unrelated symbols, prior snapshots, or future-dated state. |
| C | Exact boundary | Caps, minimums, participation limits, and rounding boundaries are evaluated against the declared literal value with no hidden paise rounding and no epsilon. |
| D | Lifecycle | Every receivable/payable created by the kernel has a documented settlement path, due date or explicit unrealizable state; none can remain permanently orphaned while counted in NAV. |
| E | Evidence completeness | Every report row is bound to the tested revision and to named test evidence; missing, skipped, stale, or wrong-module evidence cannot yield `PASS` or authorize the gate. |
| F | Prefix invariance | A run over prefix `0..t` produces the same state and decisions whether or not future sessions `t+1..T` exist in the input store. |
| G | Scenario isolation | Scenario values are named, side-effect-free views; they never enter base NAV, cash, positions, or performance statistics, and one scenario cannot mutate another. |
| H | End-to-end integration | A full Stage-1 path preserves the accounting identity and terminal NAV while exercising adjustment, dividend, absence/reopen, sizing, fill, and settlement. |

## 3. Test families and ownership

| Family | Purpose | Planned test module(s) | Gate treatment |
|---|---|---|---|
| unit | One boundary, validator, or arithmetic rule in isolation | `tests/test_stage1_unit_boundaries.py`, `tests/test_stage1_unit_contracts.py` | Mandatory |
| cross-component | Two or more production components agree on a planned seam | `tests/test_stage1_contracts.py` | Mandatory |
| independent golden | Frozen vector reproduced without production arithmetic | `tests/test_stage1_goldens.py` | Mandatory, no skip |
| property | Quantified invariant over generated inputs | `tests/test_stage1_properties.py` | Mandatory |
| metamorphic | Relation between inputs/outputs that must hold under transformation | `tests/test_stage1_metamorphic.py` | Mandatory |
| negative | Invalid or ambiguous input must fail closed | `tests/test_stage1_negative.py` | Mandatory |
| replay | Typed-event replay reproduces terminal state | `tests/test_stage1_replay.py` | Mandatory |
| causality | No future data, phase, or artifact may affect a decision | `tests/test_stage1_causality.py` | Mandatory |
| authorization | Revision/evidence/gate checks fail closed | `tests/test_stage1_authorization.py` | Mandatory |
| dataset immutability | Inventory and dataset reads are non-mutating and complete | `tests/test_stage1_dataset.py` | Mandatory |
| mutation | A deliberate defect in each cluster is killed | `tests/test_stage1_mutation.py` plus `scripts/run_stage1_mutation_gate.py` | Mandatory, no skip |

Each test carries `pytest.mark.stage1`, exactly one `pytest.mark.batchN`, and exactly one `pytest.mark.family("<family>")` (module-level defaults are allowed). Markers are registered in `pytest.ini`/`pyproject.toml`; unknown, missing, or multiple family/batch markers fail the harness meta-test. The ownership manifest `tests/stage1_test_manifest.json` (new, test-only) maps test ID to family, finding/contract, batch, and mandatory flag. A test absent from the manifest is not counted as coverage.

## 4. Batch map

| Batch | Scope | Findings/contracts | Exit condition |
|---:|---|---|---|
| 0 | Harness, oracle independence, immutable-golden checks | All | Independent oracle reproduces anchors; no production import in the oracle; manifest complete |
| 1 | Fees, valuation diagnostics, precision, dividend lifecycle | F1, F3-F5 | All batch tests red for the documented reason before implementation |
| 2 | Provenance, momentum, dead config, universe scope, ADV, boundary, calendar, bar coherence | F2, F6-F11, F15 | Each causal/semantic boundary has a discriminating test |
| 3 | Specs, sizing, validation | F12-F14, F21 | Policy aliases and P1/P2/P3 vectors discriminate |
| 4 | Verification provenance, report alignment, authorization, docs, dataset | F16-F20, F22-F23 | Stale/mis-mapped/wrong-revision evidence cannot pass |
| 5 | P0 contracts A-H | A-H | One end-to-end anchor plus contract-specific negatives pass |
| 6 | Mutation and gate | All clusters | 100% of listed mutants killed by at least one mandatory test |

## 5. Planned observable contracts encoded by expected values

These are the **target** behaviours the tests describe. They are not present at `f5e9430`; naming them here prevents tests from silently blessing current bugs.

| Finding | Planned observable |
|---|---|
| F1 | The declared `max_order_notional` is floored to paise at use, never rounded upward, and bounds **principal only**; fees are a separate cash-budget constraint. `executed_notional <= declared_max_order_notional`, `FillQuote.applied_principal_cap` is recorded, and quantity, principal, fees, payable, and remaining buying power are separately observable. |
| F2 | `BasisProvenance(kind, basis_id, name, version, source_digest, parameters)` records authoritative identity. For built-ins `kind="builtin"`, `name=spec.price_basis`, and `price_basis` stays `"close"`; for callables `kind="callable"`, `price_basis="custom:<basis_id>"`, and the spec string is never substituted. `basis_id` is mandatory for lambdas, closures, partials, and stateful callables or construction raises; `source_digest=None` is an explicit unknown, not reproducibility. `parameters` records a parameterized transform. |
| F3 | `reference_coverage_reason` has exactly six states: `no_positions`, `missing_reference`, `zero_gross_notional`, `complete_current_date`, `dated_reference_only`, `mixed_current_and_dated_reference`. The independent `nav_reason` has exactly four: `nav_available`, `unpriced_positions`, `unknown_assets`, `unpriced_positions_and_unknown_assets`. Unknown assets never degrade coverage. |
| F4 | `Valuation.nav` is the exact authoritative Decimal sum; `nav_reported` is the single paise presentation rounding. Positions are never rounded before summation, no event re-rounds NAV, and returns use exact `nav`. |
| F5 | `confirm_dividend_payment` moves `accrued_held` to `confirmed` and emits `DIVIDEND_PAYMENT_CONFIRMED`; `void_dividend` emits `DIVIDEND_VOIDED` with a write-off. Held claims stay in NAV, remain unsettleable until confirmation, are disclosed by `unresolved_settlement_claims`, and cannot become permanent orphans. |
| F6 | `min_observations` is window-scoped over the closed `[anchor, as_of]` window; `None` requires the full `lookback_sessions + 1` window. The anchor is exact with no substitution; `history_valid_observations` is diagnostic only; the spec schema is 2. |
| F7 | `anchor_rule`, `observation_rule`, and `tie_break` are IMPLEMENTED (implicit) single-value dispatch/asserts; a second value is a schema error. Execution fields without a consumer remain RESERVED. |
| F11 | Only `authority="published_exchange"` calendars may be registered or fire `KNOWN_CALENDAR`; caller-asserted/inferred calendars are diagnostic and cannot signal. Weekend/holiday sessions require explicit `special_sessions` and citation. |
| F14 | The 27-field registry is exactly 10 IMPLEMENTED / 12 RESERVED / 0 OBSOLETE / 5 DISCONNECTED. Every field has one four-state disposition; the configuration row cannot pass while `disconnected > 0`. |
| F9 | A never-seen key yields `None`, not `0.00`; explicit zero-volume bars remain a known zero; `traded_sessions` is part of capacity admissibility. |
| F10 | `RebalanceSchedule.omissions` emits `MonthOmission` records for unobserved months and `pending_month` distinguishes an in-progress month from a completed signal. |
| F12 | Long names (`P1_freeze_on_unresolved`, `P2_size_excluding_unresolved`, `P3_size_including_unresolved_at_screen`) are canonical in `PortfolioSpec`, fixtures, and `SizingResult.policy`; short `P1/P2/P3` aliases are accepted only at the `size_orders` boundary and normalized once there. `resolve_sizing_policy(portfolio, valuation)` is the only cross-spec mapper, and `size_portfolio_orders(portfolio, valuation, ...)` is the only portfolio entry point. P3 requires caller-supplied screen value and screen price. |
| F18 | Inventory integrity is a relative state machine: clean and authorized-exception states pass; no hardcoded 8,153-file count or single historical hash gates the result. |
| F21 | A new sizing golden contains discriminating P1, P2, and P3 cases, not just P2/P3. P3 uses caller-supplied screen value and screen price; missing screen evidence yields `screen_value_unavailable` and consumes nothing. |

## 6. Batch 0 - harness and oracle independence

**T0.1 `test_cap_oracle_is_independent`** (independent golden; F1)
- **Inputs:** cap table `{99.994, 99.996, 19.999, 100.00, 99.99, 0.00}`, price `10`, desired quantity `100`, zero costs/slippage.
- **Expected:** the test-only brute-force oracle floors each declared cap to paise at use (`99.994, 99.996, 19.999, 100.00, 99.99, 0.00` -> `99.99, 99.99, 19.99, 100.00, 99.99, 0.00`) and searches integer quantities `0..100` for the largest `q` with `q * 10 <= applied`, returning `9, 9, 1, 10, 9, 0` with `executed_notional` `90.00, 90.00, 10.00, 100.00, 90.00, 0.00`; it imports only `decimal`, `fractions`, and `itertools` (directly or transitively, no `nse_pipeline` import).
- **Invariant / fails if:** the oracle is independent of production arithmetic; fails if the oracle imports production code, if any quantity is obtained by calling `affordable_buy`/`bounded_sell`, or if the returned table differs by one share or one paise.

**T0.2 `test_new_goldens_are_immutable`** (independent golden; F1-F23, A-H)
- **Inputs:** every new golden named in section 13, plus `tests/golden/INDEX.json`.
- **Expected:** each file and each case has `schema_version`, nonempty `derivation`, unique case ID, and canonical-JSON SHA-256 equal to the digest recorded at review; the three existing goldens are byte-identical to their `f5e9430` blobs.
- **Invariant / fails if:** an expected value is edited to match implementation; fails if any existing golden blob changes, a new digest is absent, or a case is duplicated across files.

**T0.3 `test_test_manifest_covers_every_planned_test`** (unit; F1-F23, A-H)
- **Inputs:** `tests/stage1_test_manifest.json`, collected node IDs, and marker declarations.
- **Expected:** every planned test appears exactly once with one family, one batch, at least one finding/contract tag, and `mandatory: true`; the set difference in both directions is empty; unknown markers fail collection.
- **Invariant / fails if:** a test can exist without a family or be silently deselected; fails on a missing test, duplicate manifest ID, multiple family markers, or a mandatory test marked optional.

**T0.4 `test_fixture_builders_do_not_call_production`** (causality; F1-F23, A-H)
- **Inputs:** AST parse of `tests/stage1_support/` and the test-only oracle modules.
- **Expected:** imports are standard library or test-support only; no `nse_pipeline` or `scripts` import appears in a fixture builder or expected-value computation.
- **Invariant / fails if:** the oracle and implementation share the formula being tested; fails on any production import in the support/oracle layer, even an unused one.

## 7. Batch 1 - F1, F3-F5

### F1 - exact maximum-order cap

**T1.1 `test_cap_boundary_table_buy_and_sell`** (unit, F1)
- **Inputs:** price `10`, desired `100`, zero costs/slippage, declared caps `99.994`, `99.996`, `19.999`, `100.00`, `99.99`, `0.00`; run `affordable_buy` and `bounded_sell`.
- **Expected:** `FillQuote.applied_principal_cap` (declared cap floored to paise) `99.99, 99.99, 19.99, 100.00, 99.99, 0.00`; quantities `9, 9, 1, 10, 9, 0`; `executed_notional` `90.00, 90.00, 10.00, 100.00, 90.00, 0.00` for **both** sides. Cap `0` returns zero with reason `unaffordable_or_partial_disallowed` (buy) or `notional_limit_or_partial_disallowed` (sell), and no fee is charged.
- **Invariant / fails if:** no fill exceeds the declared cap and `executed_notional <= declared_max_order_notional`; fails if `99.994` or `99.996` admits 10 shares, if the applied cap is rounded up to `100.00`, if a zero cap returns a fill, or if buy and sell differ.

**T1.2 `test_cap_never_rounds_up_at_paise_boundary`** (negative, F1)
- **Inputs:** price `10`, desired `100`, declared caps `99.991`, `99.999`, `100.001`, `19.9999`, zero costs/slippage.
- **Expected:** applied caps `99.99, 99.99, 100.00, 19.99`; quantities `9, 9, 10, 1`; `executed_notional` `90.00, 90.00, 100.00, 10.00`. The applied cap is `ROUND_FLOOR` at use and every executed notional is `<=` its declared cap.
- **Invariant / fails if:** `money(cap)` or any half-up path rounds a sub-paise cap upward before comparison; fails on 10 shares for `99.999`, 2 shares for `19.9999`, or a fill above the declared cap.

**T1.3 `test_zero_cap_and_zero_budget`** (negative, F1)
- **Inputs:** cap `0`, cash `0`, price `10`, desired `10`, nonzero fee schedule.
- **Expected:** applied cap `0.00`, quantity `0`, `executed_notional` `0.00`, total fees `0.00`, payable `0.00`, buying power `0.00`, no reservation and no cash mutation.
- **Invariant / fails if:** a zero cap or zero budget creates a reservation, fee, or negative cash; fails on any nonzero amount.

**T1.4 `test_large_quantity_does_not_overflow_or_round`** (property, F1)
- **Inputs:** price `0.01`, desired `20_000_000`, declared caps `100_000.00` and `99_999.999`, zero costs/slippage, cash budget at least the cap.
- **Expected:** applied caps `100_000.00` and `99_999.99`; quantities `10_000_000` and `9_999_999`; `executed_notional` `100_000.00` and `99_999.99`; `Decimal` arithmetic is exact and no float/scientific notation appears.
- **Invariant / fails if:** binary search or paise conversion loses a share at eight-digit quantities; fails on any float, `1E+5`-style value, or a quantity whose notional exceeds the applied or declared cap.

**T1.5 `test_fee_inclusive_cash_is_not_the_cap`** (cross-component, F1)
- **Inputs:** declared cap `1_000.00`, cash `1_000.00`, price `100`, desired `10`, brokerage `100 bps`, all other fees `0`, zero slippage.
- **Expected:** applied principal cap `1_000.00`, quantity `9`, principal `executed_notional=900.00`, brokerage `9.00`, total fees `9.00`, payable `909.00`, remaining buying power `91.00`, remaining principal cap `100.00`. Ten shares would have principal exactly `1_000.00` but payable `1_010.00`, so it is rejected.
- **Invariant / fails if:** the cap is treated as fee-inclusive or cash is treated as principal-only; fails if quantity 10 fills, if payable omits the fee, or if fees expand the cap.

**T1.6 `test_cap_monotonicity_property`** (property, F1)
- **Inputs:** exhaustive declared caps `0..25_000` paise (`0.00..250.00`), price `10`, desired `25`, both sides, zero fees.
- **Expected:** let `applied = floor_to_paise(declared)`; for every cap the returned quantity is the unique largest integer `q <= 25` with `q * 10 <= applied`; `q == 25` iff `applied >= 250`; `q == 0` iff `applied < 10`.
- **Invariant / fails if:** monotonicity, exact maximality, or floor-at-use is broken; fails if `q+1` also fits, if `q` does not fit the applied cap, if a larger declared cap returns fewer shares, or if `executed_notional > declared_max_order_notional`.

### F3 - coverage-state table

**T1.7 `test_reference_coverage_state_table`** (independent golden, F3)
- **Inputs:** `coverage_state_cases.json` books B1-B8 at one `EventTime`, with the memo's inputs: B1 cash `0`, 100 @ mark `10`, 200 @ mark `5`; B2 cash `15,000`, 100 @ mark `100`, 100 @ reference `50`; B3 cash `15,000`, 100 @ mark `100`, 100 @ reference `500`; B4 cash `15,000`, 100 @ mark `100`, 100 with no mark/reference; B5 cash `1,000`, no positions; B6 cash `1,000`, 100 @ reference `50` only; B7 cash `1,000`, 100 @ mark `10`, plus an unknown `Claim("claim", OTHER, None, ...)`; B8 cash `0`, 100 shares blocked with `blocked_reason="x"`, mark `10`, reference `5`.
- **Expected:** B1 `complete_current_date`, `marked=2000`, `known_reference_subtotal=0`, `gross_reference_notional=2000`, completeness `1`, coverage `1`, `nav=2000`, `nav_available`; B2 `mixed_current_and_dated_reference`, `10000/5000/15000`, `1/2`, `2/3`, `nav=None`, `unpriced_positions`; B3 same state, `10000/50000/60000`, `1/2`, `1/6`, `None`, `unpriced_positions`; B4 `missing_reference`, `10000/None/None`, `1/2`, coverage `None`, `nav=None`, `unpriced_positions`; B5 `no_positions`, `0/0/0`, completeness `1`, coverage `None`, `nav=1000`, `nav_available`; B6 `dated_reference_only`, `0/5000/5000`, `0`, `0`, `None`, `unpriced_positions`; B7 `complete_current_date`, `1000/0/1000`, `1`, `1`, `nav=None`, `unknown_assets`; B8 `dated_reference_only`, `0/500/500`, `0`, `0`, `None`, `unpriced_positions`.
- **Invariant / fails if:** every state is distinguishable from every other and matches the exact closed vocabulary; fails if a fully priced book is `mixed_current_and_dated_reference`, if B5 is `zero_gross_notional`, if B7 coverage is degraded by the unknown claim, if B8's current mark overrides its blocked status, or if missing reference reports a numeric gross/coverage.

**T1.8 `test_fully_priced_book_is_complete_not_mixed`** (unit / regression, F3)
- **Inputs:** three positions, each with a current-session close available at valuation phase; no references. Also the B5 empty-book control.
- **Expected:** reason `complete_current_date`, `unresolved_positions == ()`, `missing_reference_count == 0`, `known_reference_subtotal == 0`, coverage `Fraction(1)`, gross equals the marked value exactly, `nav_reason == "nav_available"`. The empty book reports `no_positions`, not `complete_current_date` or `zero_gross_notional`.
- **Invariant / fails if:** the presence of a zero reference subtotal changes the label; fails on `mixed_current_and_dated_reference` for any all-priced book or on a zero-holding book sharing the fully priced state.

**T1.9 `test_unknown_asset_blocks_nav_but_not_coverage`** (negative, F3)
- **Inputs:** B7 (fully priced plus unknown claim); a second book with one unresolvable position plus the same unknown claim; the exact closed-vocabulary tuples for both diagnostic fields.
- **Expected:** B7 keeps `reference_coverage_reason="complete_current_date"` while `nav is None` and `nav_reason="unknown_assets"`; the mixed book reports `mixed_current_and_dated_reference` with `nav_reason="unpriced_positions_and_unknown_assets"`. The allowed coverage tuple is exactly `("no_positions", "missing_reference", "zero_gross_notional", "complete_current_date", "dated_reference_only", "mixed_current_and_dated_reference")` and the allowed NAV tuple is exactly `("nav_available", "unpriced_positions", "unknown_assets", "unpriced_positions_and_unknown_assets")`.
- **Invariant / fails if:** an unknown asset manufactures a numeric NAV, degrades coverage, or invents a reason string; fails if either field leaves its declared set or if precedence puts `unknown_assets` before `unpriced_positions`.

### F4 - precision policy

**T1.10 `test_sub_paise_nav_is_exact_then_reported`** (unit, F4)
- **Inputs:** 3 shares, price `33.335`, cash `0`, zero liabilities/receivables.
- **Expected:** `marked_value_priced == Decimal("100.005")`, `nav == Decimal("100.005")`, `nav_reported == Decimal("100.01")`; the report exposes both fields and never rewrites the authoritative `nav`.
- **Invariant / fails if:** early rounding discards the half-paise or `nav_reported` feeds back into state; fails if marked value is `100.00` or `nav` is `100.01`.

**T1.11 `test_per_position_rounding_is_not_applied`** (property, F4)
- **Inputs:** two positions of 1 share each at `0.005`, cash `0`; and 200 positions of 1 share at `0.005`.
- **Expected:** `nav` is `0.010` and `1.000`; `nav_reported` is `0.01` and `1.00`; rounding once at the total differs from rounding each position (`0.02`/`2.00`) and from rounding each addend. No event or intermediate valuation re-quantizes the running NAV.
- **Invariant / fails if:** a per-position or per-addend quantization leaks into the total; fails on `0.02`, `2.00`, cumulative drift across revaluations, or any result that depends on position count beyond the exact sum.

**T1.12 `test_report_serializes_exact_and_reported_as_strings`** (cross-component, F4)
- **Inputs:** the T1.10 valuation fed through the report serializer; a return series computed from consecutive `nav` values.
- **Expected:** JSON contains `"nav": "100.005"` and `"nav_reported": "100.01"` as strings; `"marked_value_priced": "100.005"`; returns are derived from the exact `nav` series and are unaffected by changing only `nav_reported`'s presentation format. No binary float and no truncated/rounded authoritative field.
- **Invariant / fails if:** JSON numbers lose precision, exact and reported collapse to one field, the serializer rounds `nav`, or any return/event consumes `nav_reported`.

### F5 - dividend receivable lifecycle

**T1.13 `test_hold_receivable_is_in_nav_but_unsettleable`** (unit, F5)
- **Inputs:** cash `10_000`, existing 1,000 shares, dividend `2.00` per share, `dividend_settlement_mode="hold_receivable"`, mark `100`; accrual at `2024-06-03` phase 30; no verified payment yet.
- **Expected:** claim `dividend:d` amount `2_000.00`, `due is None`, `unresolved_reason="hold_receivable"`, lifecycle state `accrued_held`; `settle` at any phase raises `unbound_payment_evidence`; cash remains `10_000.00` and `cash_available` remains `10_000.00`; `dividend_receivable=2_000`; `nav=112_000.00`; `nav_reason="nav_available"`; `unresolved_settlement_claims == ("dividend:d",)`.
- **Invariant / fails if:** a held claim can be settled by inference or lag, is omitted from NAV or from the disclosure, or is treated as spendable cash; fails on cash `12_000` before evidence, a synthesized due date, `nav=110_000`, or a missing `unresolved_settlement_claims` entry.

**T1.14 `test_confirm_verified_payment_then_settle`** (replay, F5)
- **Inputs:** T1.13 state; `confirm_dividend_payment("d", VerifiedPayment(on=2024-06-10, available_at=..., citation=...), at=EventTime(2024-06-10, CLOSE_VALUATION))` (phase 60 with a past payment date); then `settle` on the due session.
- **Expected:** state `confirmed`; `basis="verified"`, `unresolved_reason=""`; `due == Due(POST_OPEN_SETTLEMENT, session_index=index(2024-06-10)+1)` and **not** same-session settlement; event `DIVIDEND_PAYMENT_CONFIRMED` with `event_id="confirm-dividend:d"`, `amount=2_000.00`, `payment_on=2024-06-10`, and empty postings; `nav` and `dividend_receivable` unchanged at `112_000.00`/`2_000.00`; `unresolved_settlement_claims == ()` **only if `U-12` is approved as recommended by `D-05`; while `U-12` is OPEN this clause is `DECISION REQUIRED` and is not a Batch 1 pass clause**; settlement on the next observed session pays exactly `2_000.00`, cash becomes `12_000.00`, receivable `0`, NAV `112_000.00`; a second settlement raises `claim_absent`. Control: confirming at phase 30 with `payment.on == at.session` yields `Due(POST_OPEN_SETTLEMENT, on=at.session)` and may settle in the same session's phase 50.
- **Invariant / fails if:** the claim remains orphaned, pays twice/zero, or settles in a phase that has already passed; fails on same-session settlement at phase 60, cash other than `12_000.00`, a missing due date, a NAV jump at confirmation, or a second cash credit.

**T1.15 `test_confirm_rejects_mismatched_amount_phase_and_impossible_date`** (negative, F5)
- **Inputs:** T1.13 held claim for `2_000.00`; confirmations with amount `1_999.99` and `2_000.01`; `payment.on` before the ex-date; `payment.available_at > at`; confirmation at `POST_OPEN_SETTLEMENT`; each call compared against an unchanged snapshot.
- **Expected:** each call raises a field-specific `ValueError` (`conflicting_dividend_payment_amount`, `payment_precedes_entitlement_date`, `payment_evidence_not_yet_available`, `confirmation_phase_not_allowed`) and changes nothing: state remains `accrued_held`, `due is None`, cash `10_000.00`, `unresolved_settlement_claims == ("dividend:d",)`, no event appended.
- **Invariant / fails if:** a partial, early, future-evidence, or same-as-settlement-phase confirmation is accepted; fails on any state mutation, any appended event, or a restated receivable.

**T1.16 `test_duplicate_void_writeoff_and_terminal_disclosure`** (independent golden, F5)
- **Inputs:** `dividend_lifecycle_cases.json` cases `hold_confirm_settle`, `duplicate_confirm`, `void_held`, and `terminal_open`; cash `10_000`, 1,000 shares marked `20`, ₹2/share; confirmation `2024-06-10`; duplicate confirmation of the confirmed claim; a separate held claim voided with verified evidence at phase 30; terminal close attempted with a held claim and again after void.
- **Expected:** `hold_confirm_settle` reaches cash `12_000.00`, marked `20_000.00`, receivables `0.00`, no open claims, NAV `32_000.00`. `duplicate_confirm` raises `already_confirmed` with no second `DIVIDEND_PAYMENT_CONFIRMED` event and no cash change. `void_held` emits `DIVIDEND_VOIDED` with `event_id="void-dividend:d"` and postings `(("dividend_receivable", -2_000.00), ("dividend_writeoff", 2_000.00))`, removes the claim, leaves cash `10_000.00`, lowers NAV by `2_000.00`, and clears the disclosure; voiding a confirmed claim raises `not_voidable`. `terminal_open` raises `open_unrealizable_claim` naming `dividend:d` and its `unresolved_settlement_claims` entry before confirmation/void, and succeeds only after the claim is confirmed-and-settled or voided.
- **Invariant / fails if:** settlement mode, duplication, or voiding creates or destroys cash, leaves a residual claim, or drops it silently; fails on any open claim at terminal close without disclosure, a retained receivable after void, a second confirmation event, or a terminal NAV mismatch.

## 8. Batch 2 - F2, F6-F11, F15

### F6 - lookback-window observation count

**T2.1 `test_min_observations_counts_window_not_history`** (unit, F6)
- **Inputs:** 10 sessions `s0..s9`, `as_of=s9`, `lookback_sessions=3` (anchor `s6`), `min_observations=3`; valid bars on `s0..s6` and `s9`; missing/invalid on `s7,s8`.
- **Expected:** `window_sessions == 4` (`s6..s9`); `window_valid_observations == 2`; `history_valid_observations == 8` is reported for diagnostics only; the key is unrankable with reason `insufficient_window_observations`, `momentum is None`, and no `_censored` suffix because the window lies inside the observed stream.
- **Invariant / fails if:** old history is counted toward the current lookback; fails if the key is ranked, if `history_valid_observations` affects eligibility, or if the reason mentions censoring without a censored left edge.

**T2.2 `test_exact_window_minimum_and_none_full_window`** (unit, F6)
- **Inputs:** T2.1 with `s7` valid and `s8` missing; window count `3`, `min_observations=3`; prices `100` at `s6`, `110` at `s9`. Second run: the same window with `min_observations=None`; third: `s8` also valid and `None`.
- **Expected:** with `min=3` the key is ranked; momentum `Fraction(11,10)-1 == 1/10`; `valid_observations` reports the window count `3`, not the history count `9`. With `None` and 3 valid observations the key is unrankable `insufficient_window_observations` because `None` means the full `lookback_sessions + 1 == 4` window; with all 4 valid it ranks. `min_observations` outside `1..4` raises a schema error.
- **Invariant / fails if:** exactly-minimum coverage is rejected, `None` is treated as zero or as any positive count, momentum uses a different anchor, or the reported count is the history count.

**T2.3 `test_anchor_is_exact_and_schema_two_only`** (property, F6)
- **Inputs:** 100 valid sessions before the lookback; anchor `s100` invalid while `s99` and `s103` are valid, interior missing, `min_observations=3`; a second stream with a valid anchor and more pre-window sessions; `MomentumSpec().schema_version`; a v1 payload; `min_observations=6` with `lookback_sessions=3`.
- **Expected:** the invalid anchor yields `no_valid_bar_on_anchor` and momentum `None`; neither `s99` nor `s103` is substituted and adding valid pre-window sessions leaves the verdict unchanged. An `as_of` with `s100 < L` yields `no_anchor_session`, `anchor=None`. The specification is `MomentumSpec schema 2`: `MomentumSpec().schema_version == 2`; a v1 payload raises `unsupported_spec_schema` with an explicit migration message; `min_observations > lookback_sessions + 1` raises a schema error.
- **Invariant / fails if:** a nearest/earliest session is substituted, a past window is silently reinterpreted, minting valid observations before the anchor changes eligibility, or schema 1 is accepted without migration.

### F7 - validated but dead momentum controls

**T2.4 `test_momentum_controls_are_implemented_single_value`** (negative, F7/F14)
- **Inputs:** the accepted defaults `anchor_rule="exact_session_offset"`, `observation_rule="valid_observations"`, `tie_break="key_asc"`; a ranking vector with a genuine tie (`TIE_A`/`TIE_Z`, both `1/8`); second values `anchor_rule="last_observed"`, `observation_rule="all_bars"`, `tie_break="momentum_desc"`; execution fields with no consumer.
- **Expected:** each default is actually consulted by the ranker: the anchor test proves exact-session dispatch, the T2.1/T2.2 count proves window dispatch, and reversing the eligible iterable leaves the `TIE_A, TIE_Z` order unchanged. Each second value raises `ValueError` naming the field at schema validation; it is never silently ignored. Under the D-16 four-state registry, the accepted single-value controls are `IMPLEMENTED` via explicit dispatch/assert, execution-only fields are `RESERVED` with a named contract and a no-runtime-effect test, and `DISCONNECTED` must reach zero before the configuration row passes.
- **Invariant / fails if:** a validated field has no production reader and is neither implemented nor reserved; fails if an unsupported value is accepted, if a default is described as configurable while being dead, if an execution-only field lacks its no-runtime-effect test, or if the tie order depends on input order.

### F8 - as-of liquidity screen

**T2.5 `test_universe_is_as_of_and_future_blind`** (causality, F8)
- **Inputs:** ledger with `s1..s5`; build membership at `s3`; append high-volume `s4,s5` and future constituent statuses; rebuild at `s3`.
- **Expected:** eligible keys, exclusion reasons, and order are identical before and after appending future data; `Membership.as_of == s3`; no future bar influences volume, ADV, price, or listed-age screens.
- **Invariant / fails if:** future liquidity or future listing status leaks into a past screen; fails on any difference in eligible keys or reasons.

**T2.6 `test_screen_is_not_labelled_official_membership`** (negative, F8)
- **Inputs:** `GAPPED` key with two intermediate missing sessions but a valid `as_of` bar; default `build_universe` with no `MembershipEvidence` port; a second call requesting official membership with no membership evidence supplied.
- **Expected:** the default returns `Membership(kind="research_eligibility")` labelled `RESEARCH_ELIGIBILITY_ONLY`, explicitly non-official, with `GAPPED` eligible; suspension/delisting still appears only as the disclosed `no_bar_on_session` screen reason. The official-membership request without a `MembershipEvidence` port raises `unsupported_membership_kind`; the research screen remains non-official and P0-UNIVERSE stays `CONTRACT_BLOCKED` without membership evidence.
- **Invariant / fails if:** a liquidity screen masquerades as point-in-time index membership; fails if an official request succeeds without evidence, if the `RESEARCH_ELIGIBILITY_ONLY` label is absent, if P0-UNIVERSE is claimed `PASS`, or if a gap is reported as continuous membership.

### F9 - ADV missing versus zero

**T2.7 `test_never_seen_key_has_unknown_adv`** (unit, F9)
- **Inputs:** 20 observed sessions, key absent from every one; `window_sessions=20`.
- **Expected:** `TrailingMean.exact is None`, `.value is None`, `.traded_sessions == 0`, reason `no_observations`; `capacity_notional` is `None`.
- **Invariant / fails if:** absence is converted to a numeric estimate; fails on `0.00` or `2.50` capacity for a never-seen key.

**T2.8 `test_explicit_zero_volume_is_known_zero`** (unit, F9)
- **Inputs:** 20 valid bars for the key, each `traded_value=0`, `volume=0`, `trades=0`.
- **Expected:** `exact == 0`, `value == 0.00`, `traded_sessions == 20`, reason empty; capacity `0.00`; the result is distinguishable from T2.7 by `exact is not None`.
- **Invariant / fails if:** explicit zero trading is treated as missing or vice versa; fails if `exact` is `None` or if `traded_sessions` is not 20.

**T2.9 `test_partial_adv_coverage_is_not_a_full_window`** (property, F9)
- **Inputs:** one valid bar with `1000` traded value and 19 missing bars; window `20`.
- **Expected:** `total=1000.00`, `value=50.00`, `traded_sessions=1`, `coverage=Fraction(1,20)`, reason `partial_coverage`; default `capacity_notional` is `None`. With an explicit caller opt-in `allow_partial_adv=True`, capacity is `2.50` and the result is labelled `partial`.
- **Invariant / fails if:** partial coverage silently produces a full-confidence capacity; fails if the 1-of-20 result is indistinguishable from 20-of-20, or if the opt-in changes the reported coverage.

### F10 - omitted boundary months

**T2.10 `test_unobserved_months_emit_omission_records`** (cross-component, F10)
- **Inputs:** observed sessions `2024-01-31, 2024-02-29, 2024-05-31, 2024-06-28`, no calendars.
- **Expected:** decisions `[(01-31, no signal), (02-29, signal Jan), (05-31, signal Feb, omitted Mar/Apr), (06-28, signal May)]`; no June signal without a following observed month; `omissions == (MonthOmission(month=(2024,3), reason="no_observed_sessions", first_observed_after=2024-05-31, calendar_coverage=None), MonthOmission(month=(2024,4), reason="no_observed_sessions", first_observed_after=2024-05-31, calendar_coverage=None))`; no fabricated signal, counter, or `_signaled` entry for March/April.
- **Invariant / fails if:** a data gap is indistinguishable from "no rebalance"; fails if March/April is silent, if a synthetic signal is emitted, if a `MonthOmission` lacks `first_observed_after`/`calendar_coverage`, or if an omission is recorded for a month that was observed.

**T2.11 `test_pending_month_distinguishes_in_progress_from_signal`** (unit, F10)
- **Inputs:** sessions `2024-01-31` (no signal yet), `2024-02-15` (month change signals Jan), `2024-05-31` (month change signals Feb), then append `2024-06-28`.
- **Expected:** before the append, decisions carry `[(Jan31, no signal, pending Jan), (Feb15, signal Jan, pending Feb), (May31, signal Feb, pending May, skipped Mar/Apr)]`; after the append, May is signaled and `pending_month` is June.
- **Invariant / fails if:** a mid-month decision is mistaken for a completed month, or an in-progress month is reported as skipped; fails on a missing pending month or a duplicated signal.

### F11 - calendar validation

**T2.12 `test_only_published_calendar_may_register_or_fire`** (negative, F11)
- **Inputs:** (a) a `KnownCalendar(authority="CALLER_ASSERTED")` and (b) `authority="inferred_weekday"` listing plausible month-end weekdays; (c) a `KnownCalendar(authority="published_exchange", complete_months=..., special_sessions=(), source_sha256=..., coverage_end=...)` with a citation covering the same dates; each offered to `RebalanceSchedule` and each fed the same observed-session stream.
- **Expected:** (a) and (b) may be constructed for diagnostics but registration raises `calendar_not_authoritative`; neither emits a `KNOWN_CALENDAR` signal even when its dates match the observed sessions. (c) registers and fires exactly at its last published session. A calendar without `authority`, `source_sha256`, `coverage_end`, or citation raises `missing_calendar_citation`.
- **Invariant / fails if:** a caller assertion or inferred weekday list is promoted to exchange authority; fails on any `KNOWN_CALENDAR` signal from (a)/(b), acceptance of a citation-free calendar, or rejection of the published artifact.

**T2.13 `test_special_sessions_and_incomplete_months_require_explicit_citation`** (negative, F11)
- **Inputs:** published June-2024 calendar containing Saturday `2024-06-29` without `special_sessions`; the same date listed in `special_sessions` with a citation; a published calendar omitting observed `2024-06-17`; an end-of-history stream whose final month has no published last session.
- **Expected:** the uncited Saturday raises `missing_special_session_citation`; with the explicit special-session citation it is accepted and may fire. The omitted `2024-06-17` does not silently fall back: the month is uncovered/incomplete, the observed-month fallback is used with `late=True` and a diagnostic, and no `KNOWN_CALENDAR` signal is emitted. Late evidence cannot rewrite a prior decision; `finalize()` reports the end-of-history gap as `history_ended` and never signals.
- **Invariant / fails if:** a weekend/holiday session enters a calendar by assertion alone, a contradiction silently falls back to observed-month logic, a late calendar rewrites a past decision, or an end-of-history gap fabricates a signal.

### F15 - bar coherence

**T2.14 `test_valid_bar_requires_close_and_quality_agrees`** (negative, F15)
- **Inputs:** `Bar(open=None, high=None, low=None, close=None, valid_ohlcv=True)`; a close-only `Bar(close=100, open=None, high=None, low=None, valid_ohlcv=True)`; a fully populated coherent bar; a partial `Bar(valid_ohlcv=False)`; mismatched `(valid_ohlcv=True, bar_quality="degraded")`, `(valid_ohlcv=True, bar_quality="complete")` with missing OHLC, and `(valid_ohlcv=False, bar_quality="complete")`.
- **Expected:** `valid_ohlcv=True` with no `close` raises `ValueError("valid_ohlcv requires close")`; a close-only bar is accepted with `bar_quality="close_only"`; a fully populated coherent bar is accepted with `bar_quality="complete"`; a partial bar with `valid_ohlcv=False` is accepted as `bar_quality="degraded"` and remains unusable downstream. Inconsistent `valid_ohlcv`/`bar_quality` pairs raise `ValueError("bar_quality_mismatch")`.
- **Invariant / fails if:** a valid flag certifies an absent close; fails if `close_only` is rejected, if a partial bar is marked `complete`, or if any consumer treats a `degraded` bar as usable.

**T2.15 `test_valid_bar_rejects_incoherent_ohlc`** (negative, F15)
- **Inputs:** fully populated bars with `high=90, low=100`; `close=110, high=105`; `open=90, low=95`; `open=-1`; and the coherent control `open=100, high=105, low=95, close=102`; also a close-only control.
- **Expected:** each ordering violation raises `ValueError("incoherent_ohlc")` with the offending fields named, because a complete bar must satisfy `low <= min(open, close) <= max(open, close) <= high`; a negative value raises `ValueError("negative_price")`; the coherent bar is accepted as `bar_quality="complete"`, and the close-only control is accepted as `bar_quality="close_only"` without an OHLC-ordering check.
- **Invariant / fails if:** `valid_ohlcv=True` can certify impossible or negative prices; fails on any accepted incoherent tuple, acceptance of a negative value, or rejection of either coherent control.

### F2 - basis provenance

**T2.16 `test_callable_basis_provenance_is_identified`** (unit, F2)
- **Inputs:** a module-level callable `custom_basis` with a fixed source body recorded in `provenance_vectors.json`, invoked with `basis_id="custom_basis_v1"` and `parameters={}`; spec `price_basis="close"` plus that callable; one valid session and anchor. Built-in control: `price_basis="close"` with no callable.
- **Expected:** built-in control reports `kind="builtin"`, `basis_id is None`, `name="close"`, and `price_basis="close"`. The callable reports `kind="callable"`, `basis_id="custom_basis_v1"`, `name="provenance_vectors.custom_basis"`, `source_digest` equal to the golden SHA-256 of its normalized source, `parameters` an immutable mapping, and `price_basis="custom:custom_basis_v1"`; it is **not** the spec string and not the literal `"custom"`. Re-running the same callable in a fresh process yields an identical provenance record and digest.
- **Invariant / fails if:** provenance names the configuration rather than the executed code; fails if `price_basis` is `"close"`, if `basis_id` is dropped, if the digest is computed by the production function, if the record is not process-stable, or if the recorded source changes without a new version.

**T2.17 `test_same_name_callables_and_parameterized_transforms_are_distinct`** (negative, F2)
- **Inputs:** callables `alpha.basis` and `beta.basis`, each `__name__ == "basis"`, returning different prices (`11` and `13`), same spec string, each with an explicit `basis_id`; two closures from one factory with captured multipliers `2` and `3` and `basis_id="scale_2"`/`"scale_3"`, `parameters={"scale": 2}`/`{"scale": 3}`.
- **Expected:** names `alpha.basis` and `beta.basis`; different `source_digest` values for different source; `price_basis` `custom:alpha_v1` and `custom:beta_v1`; the closures share `name` and source digest but differ by `basis_id` and `parameters`, so no two behaviorally different callables share an identical provenance record. Neither callable collapses to `"basis"`, `"custom"`, or the built-in spec string.
- **Invariant / fails if:** the rank output is attributable to the wrong callable; fails on equal identifiers, equal digests for different source, missing module/qualname, identical provenance for the two closures, or a parameterized transform whose parameters are not recorded.

**T2.18 `test_callable_without_stable_identity_requires_basis_id`** (negative, F2)
- **Inputs:** a lambda, a closure, a `functools.partial`, and a stateful callable object, each used as `basis_price` without `basis_id`; each repeated with an explicit `basis_id`. Control: a module-level stateless named function without `basis_id`.
- **Expected:** each unidentifiable callable raises `ValueError("basis_id_required")` before any bar is read or rank produced; with a supplied `basis_id` each constructs a provenance record carrying that ID and `source_digest=None` where source is unavailable, recorded as an explicit unknown with no reproducibility claim. The stateless module-level control is accepted using module + qualname without an explicit ID.
- **Invariant / fails if:** an unidentifiable callable is silently recorded as `<lambda>` or under the spec string; fails on acceptance without `basis_id`, on `source_digest=None` presented as reproducible, or on wrongful rejection of a named stateless function.

## 9. Batch 3 - F12-F14, F21

### F12 - policy aliases and default mapping

**T3.1 `test_long_and_short_policy_names_are_equivalent`** (metamorphic, F12)
- **Inputs:** for each pair `{P1, P1_freeze_on_unresolved}`, `{P2, P2_size_excluding_unresolved}`, `{P3, P3_size_including_unresolved_at_screen}`: cash `900`, desired `[550,450]`, price `1`, zero fees, slots `2`, `unresolved_ranks=(0,)`, and for P3 caller-supplied `screen_values=(550,)` and `screen_prices=(1,)`.
- **Expected:** short and long forms return identical `filled`, `remaining_cash`, `reasons`, and `fees`; `SizingResult.policy` is the canonical long name in every call. The discriminator is P1 `(0,0)/900`, reasons `("frozen_unresolved","frozen_unresolved")`; P2 `(0,450)/450`, reasons `("excluded_unresolved","filled")`; P3 `(550,350)/0`, reasons `("filled_screen_value","filled")`. Passing `"P2"` to `PortfolioSpec(sizing_policy=...)` raises `unknown_sizing_policy`; the alias is accepted only at the `size_orders` boundary.
- **Invariant / fails if:** a documented long name is rejected or maps to the wrong policy; fails on any difference between pairs, on a short `SizingResult.policy`, on P1/P2/P3 sharing the same result, or on a short alias accepted in `PortfolioSpec`.

**T3.2 `test_portfolio_default_is_consumable_as_p2`** (cross-component, F12)
- **Inputs:** `PortfolioSpec()` with no overrides; its `sizing_policy`; the T3.1 vector; `ValuationPolicy(sizing_uses_screen_value=False)`.
- **Expected:** the default literal is `"P2_size_excluding_unresolved"`; `resolve_sizing_policy(default, valuation)` returns that canonical name; `size_orders` returns `filled=(0,450)`, `remaining=450`, reasons `("excluded_unresolved","filled")`, `policy="P2_size_excluding_unresolved"`. The production entry point `size_portfolio_orders(default, valuation, ...)` returns the same tuple without the caller knowing a short code.
- **Invariant / fails if:** the default cannot be passed to the production sizer; fails on `ValueError`, on `P1`/`P3` mapping, on a resolver that only works in tests, or on a non-canonical `SizingResult.policy`.

**T3.3 `test_p1_p2_p3_discriminating_golden`** (independent golden, F12/F21)
- **Inputs:** `sizing_policy_cases.json` cases `rank0_unresolved_cash900_p1`, `_p2`, `_p3`; exact vector in T3.1 with `unresolved_ranks=(0,)`, and P3 screen value/price `550`/`1`; fixtures store canonical long names.
- **Expected:** P1 `filled=(0,0)`, `remaining=900`, fees `0`, reasons `("frozen_unresolved","frozen_unresolved")`; P2 `(0,450)`, `450`, reasons `("excluded_unresolved","filled")`; P3 `(550,350)`, `0`, reasons `("filled_screen_value","filled")`. Additional cases `sizing_p3_screen_clip` (cash `100_000`, desired `[120_000,30_000]`, unresolved `(0,)`) fills `(100_000,0)` with `("filled_screen_value","no_cash")`, while P2 fills `(0,30_000)` and leaves `70_000`; `sizing_p3_unavailable` with no screen price fills `(0,50_000)` with `("screen_value_unavailable","filled")` and consumes no slot.
- **Additional D-14 cases:** `sizing_p3_slot` (cash `100_000`, desired `[40_000,40_000,40_000]`, slots `2`, unresolved `(1,)`) fills P3 `(40_000,40_000,0)` with reasons `("filled","filled_screen_value","no_slot")` and remaining `20_000`, while P2 fills `(40_000,0,40_000)`; `sizing_p1_not_first` (unresolved `(1,)`) freezes P1 at `(0,0)` with `("frozen_unresolved","frozen_unresolved")` while P2 fills rank 0; `sizing_p2_fees` (cash `100_000`, desired `[100_000]`, no unresolved rows, brokerage `100 bps` capped at `100`, no other fees) fills `99_900`, charges `100`, and leaves `0`.
- **Invariant / fails if:** the golden cannot distinguish the three policies; fails if any two policies share a terminal tuple/reasons, if P3 creates cash beyond `900`/`100_000`, if screen value is inferred from a stale mark, or if a missing screen input consumes cash or a slot.

### F13 - policy-combination validation wiring

**T3.4 `test_policy_validation_runs_before_sizing`** (cross-component, F13)
- **Inputs:** `size_portfolio_orders` with `PortfolioSpec(sizing_policy="P3_size_including_unresolved_at_screen")` and `ValuationPolicy(sizing_uses_screen_value=False)`; then with `True` plus screen value/price; repeat for P1/P2 with both boolean values.
- **Expected:** P3/false raises `ValueError("screen-value sizing must be explicitly enabled...")` from `resolve_sizing_policy` before any quote, fill, fee, or cash mutation; P3/true proceeds. P1/P2/true raises the inverse mismatch; P1/P2/false proceeds. `size_orders` alone never performs the cross-spec mapping or silently infers screen evidence.
- **Invariant / fails if:** the validator is reachable only from tests or is skipped on one path; fails if invalid combinations reach `size_orders`, produce a fill, infer a screen value, or leave a partial state.

**T3.5 `test_policy_validation_does_not_override_scenario_restriction`** (negative, F13/F14)
- **Inputs:** valid P3/true pair plus `ValuationPolicy(allow_scenario_statistics=True)`; and a `scenario_only=True` portfolio with `disposal_policy="retire_zero"`.
- **Expected:** scenario statistics raise regardless of policy pairing; retirement is accepted only for the explicit scenario portfolio; no base portfolio silently inherits `retire_zero`.
- **Invariant / fails if:** cross-spec wiring weakens an existing fail-closed rule; fails if scenario statistics or base-portfolio retirement is accepted.

### F14 - declared but unread spec fields

**T3.6 `test_field_disposition_registry_is_complete_and_exact`** (independent golden, F14)
- **Inputs:** the `FieldDisposition` registry (`specs/dispositions.py` or test-only), the production AST reader survey, the behaviour-test manifest, and `spec_field_consumers.json` covering all 27 declared fields with spec, field, disposition, reader, behaviour test, and owner/issue/expiry where applicable.
- **Expected:** exactly 27 unique `(spec, field)` entries with `implemented == 10`, `reserved == 12`, `obsolete == 0`, `disconnected == 5`. Every IMPLEMENTED entry names a non-test production reader and a behaviour test that fails when the field is ignored; every RESERVED entry has one legal value, a named contract, owner, rationale, and a no-runtime-effect test; every DISCONNECTED entry names its implemented counterpart, owner, issue, and expiry; OBSOLETE is empty. The observed map equals the golden exactly.
- **Invariant / fails if:** a field is unclassified or duplicated; fails on any count other than `10/12/0/5`, an IMPLEMENTED field without a reader or behaviour test, a reservation without rationale, or a DISCONNECTED entry without owner/issue/expiry.

**T3.7 `test_configuration_row_fails_while_disconnected`** (mutation, F14)
- **Inputs:** a synthetic report generated from the registry with the five DISCONNECTED fields; a test-only replacement registry where those five are wired to their named counterparts (`disconnected == 0`); a synthetic `ghost` field absent from the real registry, fed to the same survey helper.
- **Expected:** while `disconnected > 0` the configuration row is `FAIL`/`CONTRACT_PARTIAL` with reason `disconnected_fields=5` and the five IDs listed, and cannot be `PASS`. With `disconnected == 0` and all reader/behaviour checks green, the row may pass. The survey classifies `ghost` as `unregistered` and fails the completeness check naming `ghost`.
- **Invariant / fails if:** the configuration row passes while `disconnected > 0`; fails if the survey is a hardcoded allowlist that cannot detect a new field, if a disconnected field is relabelled IMPLEMENTED without a reader, or if the read control is falsely flagged.

### F21 - discriminating sizing golden

**T3.8 `test_sizing_golden_covers_all_three_policies`** (independent golden, F21)
- **Inputs:** `sizing_policy_cases.json`; every case is evaluated by hand or by a brute-force cash allocator; fixtures store the canonical long names.
- **Expected:** policy set is exactly `{P1_freeze_on_unresolved, P2_size_excluding_unresolved, P3_size_including_unresolved_at_screen}`; at least one case has pairwise different `filled` tuples for all three policies; the P1 case is a golden, not only an inline test; the test module docstring's coverage claim matches the file.
- **Invariant / fails if:** a policy is missing or non-discriminating; fails if P1 exists only inline, if two policies share the terminal vector, if a short code appears in the fixture, or if the docstring claims coverage the file does not contain.

**T3.9 `test_p3_screen_case_discriminates_from_p2`** (metamorphic, F21)
- **Inputs:** `sizing_p3_screen_clip`: cash `100_000`, desired `[120_000, 30_000]`, slots `2`, `unresolved_ranks=(0,)`, screen value `120_000`, screen price `1`, zero fees; the same vector under P2. Also `sizing_p3_unavailable` with the screen price omitted.
- **Expected:** P3 fills rank `0` with `100_000` (`filled_screen_value`, clipped by cash) and rank `1` with `0` (`no_cash`), remaining `0`; P2 fills rank `0` with `0` (`excluded_unresolved`) and rank `1` with `30_000`, remaining `70_000`. With the screen price missing, P3 fills rank `0` with `0` (`screen_value_unavailable`) and rank `1` with `50_000` under the `sizing_p3_unavailable` vector, consuming no cash or slot for the unavailable rank.
- **Invariant / fails if:** the screen case has enough cash to pass under any policy, screen value creates cash, a missing screen input still consumes cash/slot, or P2 and P3 produce identical output.

## 10. Batch 4 - F16-F20, F22-F23

### F16 - evidence revision and module coverage

**T4.1 `test_report_rejects_evidence_from_another_revision`** (authorization, F16/F18)
- **Inputs:** synthetic focused JUnit with every mandatory row PASS, regression `10/10` PASS, clean inventory; evidence metadata says `head_at_verification="f5e9430"` and `foundation_commit="76adf01"`; `--out` under the temp directory.
- **Expected:** exit code `2`, `integration_authorized=False`, `overall_p0_gate="CLOSED"`, reason `evidence_revision_mismatch`; no report is written outside temp and no existing artifact is overwritten.
- **Invariant / fails if:** a stale JUnit can authorize the current tree; fails on exit `0`, `authorized=True`, a missing reason, or a write into `verification/` or `data/`.

**T4.2 `test_regenerated_junit_covers_post_baseline_modules`** (cross-component, F16)
- **Inputs:** the committed focused/regression JUnit and their evidence manifest, after regeneration at HEAD.
- **Expected:** the testcase set includes nonempty groups for `test_accounting_identity`, `_observations`, `_signals`, `_universe`, `_adv`, `_boundary`, `_evidence`, and `_sizing`; metadata records the tested HEAD; counts exclude pytest subtests and say so.
- **Invariant / fails if:** a module added after the baseline has no committed evidence; fails on a missing module, a baseline-only revision, or an inferred subtest count.

### F17 - machine report versus doc matrix

**T4.3 `test_component_rows_match_documented_matrix`** (cross-component, F17)
- **Inputs:** `report_matrix_cases.json`, the production `IMPLEMENTED`/`DEFERRED` maps, and the matrix in `docs/STAGE1_ACCOUNTING_VERIFICATION.md`.
- **Expected:** every component has the same status in all three places; `signals`, `universe`, `adv`, `boundary`, `evidence`, and `sizing_budget` are `IMPLEMENTED` with their actual test modules; only genuinely unimplemented contracts remain `DEFERRED`.
- **Invariant / fails if:** the machine report contradicts the document at HEAD; fails on any status disagreement, a deferred name that has production tests, or an implemented name whose module is absent.

### F18 - relative inventory integrity and authorization

**T4.4 `test_clean_inventory_passes_without_a_historical_exception`** (authorization, F18)
- **Inputs:** temp root with a two-entry inventory and two matching files, zero acknowledged exceptions, zero errors; call `verify(root)` and the report integrity predicate; run the verifier CLI in a temp checkout.
- **Expected:** `checked_files=2`, `all_hashes_match=True`, `only_accepted_exception=False`, `integrity_ok=True`, CLI exit `0`. A one-byte mismatch yields `all_hashes_match=False`, `integrity_ok=False`, CLI exit `1`.
- **Invariant / fails if:** integrity requires exactly one historical exception or a fixed 8,153/hash constant; fails on a clean-tree false negative, a dirty-tree false positive, or a CLI/report disagreement.

**T4.5 `test_authorization_requires_current_evidence_and_no_mandatory_skip`** (authorization, F18)
- **Inputs:** four synthetic evidence sets: (a) current revision, all PASS; (b) current revision, one mandatory test skipped; (c) stale revision, all PASS; (d) current revision, one mandatory test FAIL.
- **Expected:** only (a) authorizes; (b), (c), (d) close the gate with reasons `mandatory_skip`, `evidence_revision_mismatch`, `mandatory_failure` respectively. In every closed case `integration_authorized=False` and `overall_p0_gate="CLOSED"`.
- **Invariant / fails if:** skip/fail/stale evidence is counted as success; fails on any closed case authorizing or reason collapse.

### F19 - one skip accounting statement

**T4.6 `test_skip_accounting_has_one_source_of_truth`** (cross-component, F19)
- **Inputs:** `verification/skip_policy.json` (five optional PDF test IDs), `docs/STAGE1_ACCOUNTING_VERIFICATION.md`, and the latest JUnit skip set.
- **Expected:** the document contains exactly one skip statement, naming five optional PDF IDs and zero mandatory skips; the JUnit skip set equals that ID set; no line says `FAIL / SKIP | None` while five skips exist.
- **Invariant / fails if:** the doc contradicts itself or the artifact; fails on two skip counts, an undocumented skip, or a mandatory test in the skip set.

### F20 - sizing report row

**T4.7 `test_sizing_budget_row_is_backed_by_sizing_tests`** (authorization, F20)
- **Inputs:** synthetic focused JUnit where all rows except `tests.test_accounting_sizing` PASS, and the sizing test is `SKIP`; an alternate where only `tests.test_accounting_sizing` is FAIL.
- **Expected:** `sizing_budget` status is `SKIP` and `FAIL` respectively; `PASS` is impossible without the sizing module. The row's `test_results` names at least one `tests.test_accounting_sizing::...` case.
- **Invariant / fails if:** the row is backed by unrelated `core`/`fees` tests; fails if a sizing skip/fail leaves `sizing_budget` PASS or the module is absent from `test_results`.

### F22 - tracked inventory inputs

**T4.8 `test_inventoried_data_readme_is_tracked`** (dataset immutability, F22)
- **Inputs:** `data/README.md`, its inventory entry and SHA-256, `git ls-files --error-unmatch data/README.md`.
- **Expected:** the path is tracked at HEAD, its blob hash matches the inventory entry, and `git check-ignore` reports not ignored; a clean checkout therefore contains the file.
- **Invariant / fails if:** the inventory references a file absent from a clean clone; fails on untracked/ignored status or a blob/inventory hash mismatch.

**T4.9 `test_every_inventory_entry_is_tracked_or_external`** (dataset immutability, F22)
- **Inputs:** all inventory paths, `git ls-files`, and an explicit (normally empty) `EXTERNAL_PREREQUISITES` set.
- **Expected:** every entry is either tracked at HEAD or named in `EXTERNAL_PREREQUISITES` with a fetch instruction; the two sets are disjoint and their union equals all entries.
- **Invariant / fails if:** an untracked local file is silently required for verification; fails on any unclassified entry or an external prerequisite without instructions.

### F23 - stale limitation statement

**T4.10 `test_limitations_text_lists_post_baseline_modules`** (cross-component, F23)
- **Inputs:** the remaining-limitations paragraph in `docs/STAGE1_ACCOUNTING_VERIFICATION.md`.
- **Expected:** the paragraph states that `signals`, `adv`, `universe`, and `boundary` are implemented post-baseline components, and the stale sentence "kernel does not infer ... signals ... ADV" is absent or explicitly scoped to `core.py` only.
- **Invariant / fails if:** the document still claims the whole slice does not compute signals/ADV; fails on the stale phrase, a missing module list, or a scope qualifier that contradicts the matrix.

## 11. Batch 5 - P0 contracts A to H

**T5.1 `test_buy_custody_and_settlement_continuity`** (replay, A)
- **Inputs:** initial cash `100_000.00`; order for 1,000 shares at `90`, reserve `100_000.00`; zero costs/slippage, `trade_settlement_lag_sessions=1`.
- **Expected:** after the opening buy, cash `100_000.00`, order reserve `0`, buy payable `90_000.00`, shares `1_000`, delivered `0`, NAV `100_000.00`; after next-session settlement, cash `10_000.00`, payable `0`, delivered `1_000`, NAV `100_000.00`.
- **Invariant / fails if:** value is not conserved across trade and settlement; fails if cash is debited at trade, delivery precedes payment, the reserve is not released, or NAV differs from `100_000.00` at either phase.

**T5.2 `test_settlement_before_due_or_twice_is_rejected`** (negative, A)
- **Inputs:** T5.1 buy claim; settle at the trade phase, at `T-1`, and twice at `T+1`.
- **Expected:** each premature or repeated call raises `claim_not_due`/`claim_absent`; after the first valid settlement, cash stays `10_000.00` and delivered remains `1_000`; the invalid calls append no event and mutate no posting.
- **Invariant / fails if:** a claim can be paid early or twice; fails on any cash/delivery change outside the single valid settlement or any event append on failure.

**T5.3 `test_split_scope_is_limited_to_the_named_security`** (cross-component, B)
- **Inputs:** 1,000 AAA and 500 BBB delivered; 2:1 AAA split effective at `T`; pending BBB order; split evidence scoped to AAA.
- **Expected:** AAA becomes `2_000` shares and `2_000` delivered, BBB remains `500`/`500`, the BBB order is untouched, and marked value is unchanged when AAA price halves from `10` to `5`; applying the same action to BBB raises `wrong_security`.
- **Invariant / fails if:** an adjustment leaks across symbols or order books; fails if BBB quantity changes, the BBB order is canceled, or value changes without an economic event.

**T5.4 `test_adjustment_cannot_apply_before_effective_date`** (negative, B)
- **Inputs:** AAA split effective `2024-06-10`; snapshot and attempted application at `2024-06-07`; replay of the same snapshot.
- **Expected:** `ValueError("action_not_yet_effective")`; AAA remains `1_000`, prior snapshot unchanged, no event appended; the same action succeeds at `2024-06-10`.
- **Invariant / fails if:** an ex-date adjustment is applied retroactively; fails on pre-effective share mutation or any prior-snapshot change.

**T5.5 `test_minimum_and_cap_boundary_is_exact`** (unit / boundary, C)
- **Inputs:** price `10`, `min_order_notional=100.00`, caps `99.99` and `100.00`, desired `10`, zero fees.
- **Expected:** at cap `99.99`, quantity `0`, reason `unaffordable_or_partial_disallowed` because the maximum 9-share notional `90.00` is below the minimum; at cap `100.00`, quantity `10`, notional `100.00`, reason `filled`. No epsilon admits 9 at the minimum or 10 at `99.99`.
- **Invariant / fails if:** a minimum or cap is treated with a tolerance; fails on a nonzero fill below the minimum or above the cap, or on rejection at the exact boundary.

**T5.6 `test_every_claim_has_a_terminal_path`** (cross-component, D)
- **Inputs:** buy payable, sale receivable, confirmed dividend, and a held dividend claim; audit of open claims at each phase; terminal close attempted before confirmation, after confirm-and-settle, and after void.
- **Expected:** every scheduled claim has a due phase/date and settles exactly once; the held claim has `due=None`, state `accrued_held`, and appears in `unresolved_settlement_claims`; terminal close before evidence raises `open_unrealizable_claim` naming it and cannot silently drop it. After confirm-and-settle or a verified `void_dividend` write-off, all claims are empty, the disclosure is empty, and cash equals the hand sum; void lowers NAV by the written-off amount without changing cash.
- **Invariant / fails if:** a claim is counted without a terminal settlement path, a held claim is silently dropped from NAV or the audit, or terminal close succeeds while an unresolved claim remains; fails on a permanent orphan, a missing disclosure entry, or a close that ignores it.

**T5.7 `test_report_rows_have_revision_bound_evidence`** (authorization, E)
- **Inputs:** generated report at HEAD, evidence sidecars, mandatory row list, and JUnit testcases.
- **Expected:** every `PASS` row has at least one named `PASS` testcase from its declared module, nonzero tests, no skips, and a sidecar revision equal to HEAD; every `DEFERRED` row has no `PASS` testcase and carries the mandatory-before-migration reason.
- **Invariant / fails if:** a row is supported by stale, skipped, empty, or unrelated evidence; fails on any `PASS` without evidence or any deferred row promoted by a green adjacent suite.

**T5.8 `test_prefix_run_equals_direct_run`** (replay, F)
- **Inputs:** deterministic event/ledger stream for sessions `2024-01-02..2024-01-05`; run prefix through `2024-01-03`, then append the remaining sessions via `catch_up`; independently run the full stream directly and project both to the `2024-01-03` cutoff.
- **Expected:** prefix state, full state truncated at the cutoff, and append state have identical cash, positions, delivered shares, claims, reservations, decisions, and canonical event hash; the full run after `2024-01-05` has the same terminal values as prefix-plus-append.
- **Invariant / fails if:** future data changes a past state or append diverges from direct execution; fails on any field/hash difference, including decision order or event IDs.

**T5.9 `test_scenarios_are_isolated_views`** (unit, G)
- **Inputs:** 10 AAA marked `100`; 10 BBB unresolved with reference `50`; cash `1_000`; no receivables/liabilities.
- **Expected:** base `nav is None` (unresolved), `marked=1_000`, `known_reference_subtotal=500`, `gross_reference_notional=1_500`, coverage `2/3`, `reference_coverage_reason="mixed_current_and_dated_reference"`, `nav_reason="unpriced_positions"`; scenarios are exactly `zero_recovery=2_000` and `last_reference=2_500`; re-reading the base after scenario construction returns identical values, and no scenario mutates marks, positions, cash, or another scenario.
- **Invariant / fails if:** scenario values enter base NAV or leak between scenarios; fails on a numeric base NAV, a changed base after scenario evaluation, or a scenario amount other than `2_000`/`2_500`.

**T5.10 `test_scenario_statistics_are_rejected`** (negative, G)
- **Inputs:** `ValuationPolicy(allow_scenario_statistics=True)`; a scenario-only portfolio with `disposal_policy="retire_zero"`; a base portfolio with the same disposal policy.
- **Expected:** scenario statistics raise `ValueError`; zero-recovery retirement is accepted only in the scenario portfolio; base retirement raises `scenario_only_required`; no performance statistic is produced from a scenario-valued series.
- **Invariant / fails if:** a scenario is treated as an investable return series or leaks into base policy; fails on acceptance of statistics or base retirement.

**T5.11 `test_end_to_end_stage1_anchor`** (independent golden, H)
- **Inputs:** initial cash `100_000.00`; buy 5,000 AAA at `10` and 2,500 BBB at `20`, zero fees/slippage; ₹5,000 verified dividend; 2:1 AAA split; BBB absent then reopens and is sold at `18` for `45_000.00`; AAA mark `5` at the end.
- **Expected:** terminal cash `50_000.00`, AAA `10_000` shares, BBB `0`, marked value `50_000.00`, receivables `0`, liabilities `0`, `nav=100_000.00`, `nav_reported=100_000.00`, `nav_reason="nav_available"`, `unresolved_settlement_claims == ()`; NAV path from exact `nav` after settlement `100_000`, after dividend `105_000`, after split `105_000`, after BBB re-mark/redemption `100_000`.
- **Invariant / fails if:** the full path loses or creates value, double counts the dividend, mis-scales the split, or cannot sell a reopened absent name; fails on any terminal amount or share count other than the hand-derived anchor.

## 12. Batch 6 - mutation and gate

**T6.1 `test_mutation_baseline_is_green`** (mutation)
- **Inputs:** the full Stage-1 suite before any mutant is applied; `tests/mutation/stage1_mutants.json`.
- **Expected:** zero failures/errors and zero mandatory skips; every mutant ID has exactly one production edit and at least one named killing test; the baseline hash of each edited file is recorded.
- **Invariant / fails if:** a mutant is evaluated against a red or incomplete baseline; fails on any baseline failure, missing mutant entry, or mutant with no killing test.

**T6.2 `test_every_mutant_is_killed_once`** (mutation)
- **Inputs:** each row of the table below applied singly in an isolated temp worktree; the listed tests and the unmutated control.
- **Expected:** control passes; the mutant is killed by at least one listed mandatory test, with the failure naming the intended invariant; kill rate `1.00`; no mutant requires two edits or changes a golden.
- **Invariant / fails if:** a defect cluster is untested or a test passes for the wrong reason; fails on any surviving mutant, control failure, or failure caused by fixture/import breakage rather than the mutated rule.

**T6.3 `test_gate_rejects_any_mandatory_skip_or_deselection`** (authorization)
- **Inputs:** Stage-1 suite run with `-m "stage1"`; manifest-declared mandatory node IDs; an attempted deselection of one mandatory test; one `xfail(strict=True)` quarantine.
- **Expected:** selected mandatory set equals the manifest set; skipped count is `0`; an xfail is reported separately and never as `PASS`; any missing, deselected, skipped, or xfailed mandatory test sets `overall_p0_gate="CLOSED"`.
- **Invariant / fails if:** a filter, skip, or xfail can hide a mandatory failure; fails if the gate opens on incomplete selection or counts an xfail as pass.

| Mutant | Deliberate defect | Must be killed by |
|---|---|---|
| M01 | F1: compare cap after `money(cap)` half-up rounding | T1.1, T1.2 |
| M02 | F1: treat fees as part of the principal cap | T1.5 |
| M03 | F1: bypass cap check when cap is zero | T1.3 |
| M04 | F1: off-by-one in affordability search (`mid+1` admitted) | T1.1, T1.6 |
| M05 | F2: record every callable as the built-in spec string `"close"` | T2.16, T2.17 |
| M06 | F2: record only `__name__` and drop the mandatory `basis_id`/closure guard | T2.17, T2.18 |
| M07 | F3: force reason `mixed_current_and_dated_reference` | T1.7, T1.8 |
| M08 | F3: missing reference contributes numeric zero and complete gross | T1.7, T1.9 |
| M09 | F4: quantize each position before summation | T1.11 |
| M10 | F4: collapse `nav` into `nav_reported` or feed `nav_reported` back into state | T1.10, T1.12 |
| M11 | F5: auto-settle a `hold_receivable` claim at accrual | T1.13, T1.16, T5.6 |
| M12 | F5: leave `due=None` after `confirm_dividend_payment` | T1.14, T1.16 |
| M13 | F6: count valid observations over whole history | T2.1, T2.3 |
| M14 | F6: exclude the anchor from the window count | T2.2 |
| M15 | F7/F14: silently accept an unsupported momentum control | T2.4 |
| M16 | F8: include future bars in an as-of screen | T2.5 |
| M17 | F8: allow `official_index` membership label | T2.6 |
| M18 | F9: return `0.00` for a never-seen key | T2.7 |
| M19 | F9: treat 1-of-20 coverage as a full-window ADV | T2.9 |
| M20 | F10: omit skipped-month records without a diagnostic | T2.10 |
| M21 | F10: report an in-progress month as completed/skipped | T2.11 |
| M22 | F11: register a caller-asserted calendar or admit an uncited special session | T2.12, T2.13 |
| M23 | F12: reject a canonical long policy name or map P2 to P3 | T3.1, T3.2, T3.8 |
| M24 | F13: skip `resolve_sizing_policy`/`validate_policy_combination` in the production path | T3.4 |
| M25 | F14: hide a new unregistered field or relabel a DISCONNECTED field as IMPLEMENTED | T3.6, T3.7 |
| M26 | F15: accept `valid_ohlcv=True` with all OHLC absent | T2.14 |
| M27 | F16: authorize evidence without comparing its revision | T4.1 |
| M28 | F17/F20: keep a component deferred or map sizing to core/fees | T4.3, T4.7 |
| M29 | F18: require exactly 8,153 files or the historical inventory hash | T4.4 |
| M30 | F18: require exactly one acknowledged exception for integrity | T4.4 |
| M31 | F19: restate a second, contradictory skip count | T4.6 |
| M32 | F21: remove the P1 case from the sizing golden | T3.8, T3.9 |
| M33 | F22: leave `data/README.md` untracked in a clean checkout | T4.8, T4.9 |
| M34 | F23: restore the stale "kernel does not infer signals/ADV" claim | T4.10 |
| M35 | B: apply a split to every position in the portfolio | T5.3, T5.4 |
| M36 | D: drop a held/voided claim from the open-claim audit | T5.6, T1.16 |
| M37 | F: allow a future session to mutate prefix state | T5.8 |
| M38 | G: fold scenario value into base NAV or statistics | T5.9, T5.10 |
| M39 | H: mis-scale the split or debit cash at trade instead of settlement | T5.11, T5.1 |
| M40 | F5: emit no `DIVIDEND_VOIDED` write-off and retain the receivable | T1.16, T5.6 |

## 13. Golden fixtures and hand-derived anchors

| Golden file | Case(s) and input | Independent derivation and expected anchor |
|---|---|---|
| `cap_boundary_cases.json` | T1.1-T1.6 declared cap/price/desired table; zero and fee cases | Floor each declared cap to paise at use: `99.994 -> 99.99`, `99.996 -> 99.99`, `19.999 -> 19.99`, `100.00 -> 100.00`, `99.99 -> 99.99`, `0 -> 0`; integer search gives quantities `9/9/1/10/9/0` and `executed_notional` `90/90/10/100/90/0`, all `<=` the declared cap. |
| `provenance_vectors.json` | T2.16-T2.18 callable source, two same-name callables, parameterized closures, closures without `basis_id` | SHA-256 of normalized source computed with `hashlib` at review; `kind`/`basis_id`/`name`/`source_digest`/`parameters` per D-08; identifiers are module + qualname (`alpha.basis`, `beta.basis`, `provenance_vectors.custom_basis`); `source_digest=None` is the recorded explicit unknown. |
| `coverage_state_cases.json` | T1.7-T1.9 books B1-B8 | Six coverage states and four NAV reasons exactly as T1.7: coverage `1`, `2/3`, `1/6`, `None`, `None`, `0`, `1`, `0`; gross `2000`, `15000`, `60000`, `None`, `0`, `5000`, `1000`, `500`; B1/B7 `complete_current_date`, B5 `no_positions`, B6/B8 `dated_reference_only`; B7 `unknown_assets`. |
| `precision_cases.json` | T1.10-T1.12 `3 x 33.335`, `2 x 0.005`, `200 x 0.005` | Exact authoritative sums `nav = 100.005`, `0.010`, `1.000`; one final half-up presentation quantization gives `nav_reported = 100.01`, `0.01`, `1.00`; returns use the exact series. |
| `dividend_lifecycle_cases.json` | T1.13-T1.16 hold/confirm/settle, duplicate confirm, void write-off, terminal disclosure | `1,000 x 2.00 = 2,000`; held `nav=112,000` and `unresolved_settlement_claims=("dividend:d",)`; confirmation at phase 60 due next session; `10,000 + 2,000 = 12,000`; void postings `-2,000` receivable / `+2,000` write-off, cash unchanged; zero residual claims after settlement or void. |
| `adjustment_cases.json` | T5.3-T5.4 2:1 AAA split, BBB control, early application | `1,000 x 2 = 2,000`, `500` unchanged, value `1,000 x 10 = 2,000 x 5`; early date must be rejected. |
| `sizing_policy_cases.json` | T3.1-T3.3, T3.8-T3.9 cash `900`, desired `[550,450]`, unresolved `(0,)`, screen value `550`, screen price `1`; plus `sizing_p3_screen_clip` (cash `100,000`, desired `[120,000,30,000]`) | Canonical long names; P1 `(0,0)/900`; P2 `(0,450)/450`; P3 `(550,350)/0` with `filled_screen_value`; clip case P3 `(100,000,0)` vs P2 `(0,30,000)/70,000`; missing screen evidence yields `screen_value_unavailable`. |
| `report_matrix_cases.json` | T4.1-T4.7 expected statuses, revisions, modules, skip set | Hand-authored expected status map, five optional PDF skip IDs, and post-baseline module list; no production report output copied into the golden. |
| `end_to_end_stage1.json` | T5.11 terminal anchor and NAV path | `100,000 - 50,000 - 50,000 + 5,000 + 45,000 = 50,000` cash; `5,000 x 2 = 10,000` AAA at `5 = 50,000`; NAV `100,000`; path `100k, 105k, 105k, 100k`. |
| `spec_field_consumers.json` | T3.6-T3.7 27-field disposition registry | Each entry carries spec, field, one of `IMPLEMENTED`/`RESERVED`/`OBSOLETE`/`DISCONNECTED`, reader, behaviour test, and owner/issue/expiry; counts must be `10/12/0/5` and never a short allowlist. |
| `stage1_test_manifest.json` | T0.3, T6.1-T6.3 node IDs, family, batch, tags, mandatory | Hand-maintained coverage ledger; IDs are collected node IDs, not test-name patterns or counts. |

## 14. Execution commands

All commands are read-only with respect to production artifacts; reports go to the temp directory. The `stage1` and `batchN` markers are registered in the pytest configuration and are additional to the single family marker.

```powershell
# Batch 0-1
python -m pytest -q --no-header -p no:cacheprovider -m "stage1 and batch1"
# Batch 2-3
python -m pytest -q --no-header -p no:cacheprovider -m "stage1 and (batch2 or batch3)"
# Batch 4-5
python -m pytest -q --no-header -p no:cacheprovider -m "stage1 and (batch4 or batch5)"
# Independent goldens and properties
python -m pytest -q --no-header -p no:cacheprovider -m "stage1 and (golden or property or metamorphic)"
# Full Stage-1 plan suite
python -m pytest -q --no-header -p no:cacheprovider -m stage1
# Existing regression suite
python -m pytest tests -q --no-header -p no:cacheprovider
# Mutation gate (after implementation only)
python scripts/run_stage1_mutation_gate.py --manifest tests/mutation/stage1_mutants.json
# Inventory and report, temp output only
python scripts/verify_stage1_inventory.py --out "$env:TEMP\stage1_inventory.json"
python scripts/report_stage1_accounting.py --out "$env:TEMP\stage1_report.json"
```

Expected mutation command output: `baseline=pass`, `mutants=40`, `killed=40`, `survived=0`, `score=1.00`, exit `0`. Any survivor exits nonzero and prints the mutant ID, edit, and failing-test expectation.

## 15. Skip policy and gate rules

1. **Zero mandatory skips.** A Stage-1 test marked mandatory may not call `pytest.skip`, use `skipif`, be deselected by `-m`/`-k`/`--ignore`, or be missing from the manifest. Any of these closes the gate.
2. **The five optional PDF tests are the only permitted skips.** They are legacy dependency skips, must be named in `skip_policy.json`, and never count toward Stage-1 coverage. Their skip reason must be an absent optional dependency, not a missing P0 fixture.
3. **No platform, Python-version, or network skips.** A missing local dataset or tool is a visible setup failure, not a skip; a Stage-1 test must not depend on network access.
4. **Strict xfail quarantine only.** `@pytest.mark.xfail(strict=True, reason="F#: ...")` is allowed only for a newly reported defect, must execute, and is reported as `xfail`, never `PASS`. The gate remains `CLOSED` while any mandatory quarantine exists.
5. **No golden skip.** Independent goldens and mutation tests never skip; a missing golden or digest is a collection failure.
6. **Counts reconcile.** The report states `selected`, `passed`, `failed`, `skipped`, `xfailed`, and `missing`; a mandatory count mismatch is a failure even when all executed tests pass.
7. **Skip disclosure.** The verification document carries one skip statement matching `skip_policy.json`; adding a skip requires the same review as adding a test.
8. **Tests are evidence, not promotion.** A passing contract test supplies evidence only. No P0 contract becomes `PASS` and the overall gate does not open from test results alone; promotion requires the revision-bound report, complete non-skipped evidence, and owner authorization, and the gate remains `CLOSED` until then.

## 16. Exit criteria for this plan

This plan is satisfied only when: every Batch 0-6 test exists in the manifest and is red for its documented reason or green after its planned fix; all 40 mutants are killed; mandatory skips are zero; the three existing goldens are byte-identical to `f5e9430`; new goldens carry independent derivations; the report is bound to the tested HEAD; a clean inventory passes without a historical-exception count; and `git status --porcelain` shows only the intended implementation files plus this plan. Until then the overall P0 gate remains **CLOSED** and migration remains unauthorized.
