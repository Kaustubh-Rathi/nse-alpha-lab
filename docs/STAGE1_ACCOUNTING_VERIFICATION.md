# Stage 1 accounting component verification

Base: `76adf01`. This is an implementation/verification record, not a new
architecture plan. Overall P0 gate: **CLOSED**. No legacy runner migration,
ingestion rewrite, historical strategy run or performance publication.

## Dataset verification

`verification/foundation-postcommit.json` is the refreshed foundation report.
`verification/accounting-posttests.json` repeats the complete check after the
final accounting regressions. These reports record the foundation HEAD and the
tested working tree; the delivered implementation commit is recorded in delivery,
not falsely presented as an already-existing commit inside its own report.
The unlimited-background SHA256 job read all **8,153** inventoried files:
**6,835 raw files and one database** matched; there were no read errors or
unexpected changes. The preserved inventory hash is
`a4e3ac408f800e1a7d7623e04e455927ecc4dc6c1c063643ad204b8f49b7edee`.

The sole mismatch remains the user-accepted, unrecovered generated manifest:

| Artifact | Original SHA256 | Retained current SHA256 |
|---|---|---|
| `data/processed/dataset/dataset_manifest.json` | `5547ac458195acc1c4443dd042d9377ac4a79330533a764b038f4c89ff1d4ab9` | `c304d08ef89e3e91b738ba28c201bbcdb045f627ec6615bef87400aaf6d44293` |

It is **UNTRUSTED**, not an authoritative accounting input, not restored, and
not staged for commit. Neither this file nor the old production verification
report was overwritten. New verification outputs go under `verification/`.
The baseline inventory is never updated to conceal this exception.

## Production files

* `src/nse_pipeline/backtest/accounting_types.py`: immutable records, exact
  monetary boundaries, session phases, positions, orders, claims and events.
* `src/nse_pipeline/backtest/core.py`: atomic reservation, fill, settlement,
  action, entitlement/accrual and liability operations; no simulation loop.
* `src/nse_pipeline/backtest/fees.py`: per-component paise rounding,
  one adverse price adjustment and fee-inclusive integer affordability.
* `src/nse_pipeline/backtest/share_actions.py`: exact rational split/bonus terms.
* `src/nse_pipeline/backtest/dividends.py`: normal cum/ex-date terms and
  explicit verified/model/hold-receivable payment scheduling.
* `src/nse_pipeline/backtest/valuation.py`: dated defensible NAV, unknown
  assets, reference-notional diagnostics and explicitly labeled scenarios.
* `src/nse_pipeline/backtest/invariants.py`: independent replay of economic
  event facts, fees, claim settlements and share/delivery transformations.

Supporting changes: `scripts/verify_stage1_inventory.py`,
`scripts/report_stage1_accounting.py`,
`src/nse_pipeline/verification.py`, `src/get_data.py`, and focused tests.
The verification CLI now rejects protected output roots before expensive
dataset reads. This fixes a watchdog failure caused by `mkdir(exist_ok=True)`
swallowing an audit-hook permission error when the directory already existed.
Actual file writes had remained protected; the assertions were not weakened.

## Golden accounting reconciliation

The existing independent JSON expectations remain unchanged. Actual values
come from production operations and a separately implemented ledger replay.
**All eight accounting cases match every asserted expected field at each event,
and all eight independently replay to PASS.** The fee and coverage controls below
also match their independent JSON expectations through production components.
Point marks in the split controls are explicit hypothetical close quotes used
to test units, not prices allegedly known pre-open or an investment return curve.

| Golden case | Independently expected terminal accounting |
|---|---|
| Reservation/cancel | Cash/NAV 1,000; availability 700 while reserved, then 1,000 |
| Prefunded purchase | Fill: C 1,000, L 310, R 50, shares 8, available 690, NAV 1,540. Settlement: C 690, L 0; NAV unchanged |
| Unsettled sale | Fill: C 1,000, sale R 199, shares 3, NAV 1,499. Settlement: C 1,199, sale R 0 |
| Dividend lag | Accrual: C 10,000, dividend R 2,000. Payment: C 12,000, R 0. NAV 112,000 with the fixture's flat quote |
| Split-up control | 100 raw shares become 200; mechanical quote 50 conserves 10,000; quote 51 values holdings at 10,200 |
| Split-down control | 200 raw shares at quote 49 value holdings at 9,800; mechanical effect is removed, actual movement retained |
| Absence/reopening | Shares remain 100; NAV absent with no current mark; reopened at 80 gives NAV 8,200 including cash 200 |
| Verified proceeds | C stays 200 while other R is 1,200; verified payment changes C to 1,400 and R to zero |
| Fee quote | 1,000 at reference 100 with 5 bps adverse buy price: fill 100.05; fees 141.67; payable 100,191.67 |
| Economic coverage | Operational completeness stays 1/2; mixed-date reference coverage changes 2/3 to 1/6; missing reference makes that ratio unavailable |

## Verification commands/results

Verification interpreter:
`C:/Users/Kaustuvh/AppData/Local/Temp/opencode/stage1-foundation-venv/Scripts/python.exe`.

* `python scripts/verify_stage1_inventory.py --out verification/accounting-posttests.json`
* `python -m pytest tests/test_accounting_core.py tests/test_accounting_fees.py tests/test_accounting_actions.py tests/test_accounting_dividends.py tests/test_accounting_valuation.py tests/test_accounting_reconciliation.py tests/test_accounting_report.py tests/test_verification_isolation.py tests/test_inventory_verification.py -q --no-header -p no:cacheprovider --junitxml=verification/accounting-focused.xml`
* `python -m pytest tests -q --no-header -p no:cacheprovider --junitxml=verification/accounting-regression.xml`
* `python scripts/report_stage1_accounting.py`
* `git diff --check`; `uv lock --check`; syntax and JSON validation.

Final test counts, per-family test IDs, review disposition, evidence hashes and
post-test integrity results are recorded in
`verification/accounting-component-results.json`. The generator reads actual
JUnit results rather than inferring success from a test-file name. A missing,
failed or skipped mandatory family keeps the gate closed; the authorization
checks have 18 hand-authored report regressions.

Final results: **90 focused passed** (1.92s); **940 full-suite passed plus
129 subtests passed** (89.22s); **zero failures/skips**. Post-test SHA256 read all
8,153 files with zero errors/unexpected changes; all 6,835 raw files and the
database match. The accepted manifest remains the only mismatch
(`all_hashes_match=false`, `only_accepted_exception=true`); it is not a clean
all-files-match claim. Syntax parsing, lock validation, whitespace checks and
no-diff checks for golden expectations, ingestion and legacy runners also pass.

Two-axis review found three standards/correctness issues (unheld-key action
cancellation, sell limits, replay execution constraints) and two spec issues
(ambiguous dividend cash leakage and invalid empty-ledger opening delivery).
All were reproduced and fixed. Follow-ups also fixed independently paise-rounded
maximum caps, unknown-entitlement scenario values, and missed settlement phases.

### Production-component P0 matrix

PASS means **only the named subset**, not the complete original golden family.
The JSON report lists all mandatory rows separately and computes the closed gate.

| Status | Implemented or outstanding contract |
|---|---|
| PASS | Settled cash/reservations/payables; normal delayed trade settlement |
| PASS | Fee-inclusive integer buy/sell limits; rank-sequential supplied budgets |
| PASS | Verified-key raw-share split/bonus; fractional/ambiguous-unit quarantine |
| PASS | Normal cum/ex entitlement; accrual; explicit verified/lag/hold settlement |
| PASS | Defensible NAV/gaps; operational/reference coverage; unknown-asset scenarios |
| PASS | Explicit verified consideration receipt; named zero-friction liquidation |
| PASS | Independent typed-event replay; immutable IDs; unresolved occupied slots |
| PASS | Artifact guards; inventory checker; fail-closed report authorization tests |
| PASS (component) | Causal identity keys, dated edges/retirements, `Bar` observations (`identity.py`, `observations.py`) |
| NOT IMPLEMENTED | Documentary continuity; scoped historical adjustment |
| PASS (component) | Spec-driven universe; exact causal momentum ranks (`universe.py`, `signals.py`) |
| NOT IMPLEMENTED | Causal month-end scheduling |
| NOT IMPLEMENTED | Full lifecycle/series/termination and evidence-counting contracts |
| PASS (component) | P1/P2/P3 rank-sequential sizing (`sizing.py`); trailing ADV (`adv.py`); evidence ledger (`evidence.py`, `boundary.py`). P1 is an unconfirmed interpretation |
| NOT IMPLEMENTED | Frozen-order perturbation and economic/provenance prefix contracts |
| NOT IMPLEMENTED | Separate scenario portfolios/statistics isolation; friction-aware liquidation |
| FAIL / SKIP | None in final focused/full test results; no deferred family mislabeled SKIP |
| **CLOSED** | **Overall mandatory P0 gate; no migration authorization** |

## Post-baseline components

Added in later commits, each with its own tests: `observations.py`, `evidence.py`,
`adv.py`, `boundary.py`, `identity.py`, `universe.py`, `signals.py`, `sizing.py`.
Full suite afterwards: **1,129 passed, 5 skipped, 129 subtests passed**, no xfails.
Four review-found bugs were fixed test-first: a stale old-symbol or retired-symbol
observation minting a second key, a bare-string `series`, and a whitespace-only
ISIN in `Bar`. The JSON reports under `verification/` were **not** regenerated for
these components and do not cover them. The overall P0 gate remains **CLOSED**.

Open design notes: P1 has no golden case, and the golden P2/P3 cases cannot tell
the two apart, so P1 (freeze the round while any rank is unresolved) is an
interpretation. Momentum `min_observations` counts the whole history, not the
lookback window. `MomentumRanks.price_basis` does not record a custom basis
callable. The trades filter reuses the ADV window. `sizing.py` accepts only
`"P1"|"P2"|"P3"`, so callers must map the long `PortfolioSpec` names.

## Remaining limitations and closed gates

* Caller-supplied security keys, documentary decisions and the complete
  observed-session stream must be independently verified upstream. No
  cross-ISIN adjustment, ticker-reuse inference or termination inference exists.
* Only normal cash-account delivery and normal cum/ex-date dividend entitlement
  are supported. Shorting, borrowing, net settlement funding, resale before
  delivery, exceptional due bills, fractional-share cash-in-lieu, ambiguous
  same-day split/dividend units and automatic quarantine resolution are not
  implemented. Unsupported units remain quarantined, never rounded into cash.
* Lags are explicit research assumptions, not verified historical NSE schedules.
  Verified payment-date evidence is used only when available at the event's
  information horizon; late evidence never rewrites earlier cash.
* The kernel is event-driven, not an engine. It does not infer exchange sessions,
  signals, target weights, ADV, auction depth, intraday impact or circuit fills.
  Full universe/identity/lifecycle, causal runner integration and end-to-end
  prefix tests remain separate mandatory gates.
* Marks and reference prices must be in current raw-share units. A stale
  reference is only a diagnostic; unknown financial claims block NAV.
* Scenario values and zero-friction liquidation subtotals are hypothetical,
  not a valuation band or a source of strategy statistics.

## 2026-11-14 — Batch 4 reconciliation addendum

### Status of the record above

The sections above are the historical `76adf01`/`f5e9430` record, not current
evidence. Their base/commit qualifiers are retained unchanged. The following
files are likewise historical:

* `verification/foundation-postcommit.json`
* `verification/accounting-posttests.json`
* `verification/accounting-focused.xml`
* `verification/accounting-regression.xml`
* `verification/accounting-component-results.json`

They must be marked `SUPERSEDED` in `verification/stage1/index.json`. The index
is append-only, so supersession is a status addition: these artifacts are not
deleted, overwritten or presented as current evidence. The corrections below
are dated corrections to the historical text; they do not rewrite it. Where a
claim is superseded, the original sentence remains and this addendum names the
superseding commit or evidence.

### Superseded claims

* **Historical claim:** “The kernel is event-driven, not an engine. It does not
  infer exchange sessions, signals, target weights, ADV, auction depth,
  intraday impact or circuit fills.”

  **Superseded by** `3405330` (momentum signals and universe membership),
  `23c155d` (trailing ADV, boundary and evidence ledger), and the corrected
  semantics in `1ac9a21`. `src/nse_pipeline/backtest/signals.py` and
  `src/nse_pipeline/backtest/adv.py` now exist with component tests in
  `tests/test_accounting_signals.py` and `tests/test_accounting_adv.py`. This
  does not make either contract PASS: `P0-SIGNALS` remains `CONTRACT_FAIL` and
  `P0-ADV` remains `CONTRACT_FAIL` at the statuses recorded in
  `docs/STAGE1_P0_CONTRACT_MATRIX.md`; their component evidence is in
  `verification/stage1/batch2/`, while Batch 3 adds only the specification
  disposition proof.

* **Historical claim:** “P1 has no golden case”.

  **Superseded by** `7972e8f`. `tests/golden/policy_cases.json` now contains
  hand-derived P1/P2/P3 cases, including the P1 whole-round freeze cases
  `rank0_unresolved_cash900_p1` and `sizing_p1_not_first`. The golden is
  hand-derived from the D-14 policy meanings, not produced by the sizer. The
  owner approved design defaults 4 and 5 on 2026-11-14 (long names canonical;
  P3 requires an explicit screen value and screen price), so the earlier
  "pending" note in `verification/stage1/batch3/summary.json` is historical;
  the contract rows still remain non-PASS. Evidence:
  `verification/stage1/batch3/summary.json` (F21).

* **Historical claim:** “the golden P2/P3 cases cannot tell the two apart”.

  **Superseded by** `7972e8f`. Batch 3 added discriminating P2/P3 cases in
  `tests/golden/policy_cases.json`, including the same unresolved-rank fixture
  under P1/P2/P3, the P3 screen-value clip and the P3-versus-P2 slot case.
  Their filled-notional and reason vectors differ pairwise, so the golden now
  distinguishes the policies. Evidence:
  `verification/stage1/batch3/summary.json` (F21).

* **Historical claim:** “Momentum `min_observations` counts the whole history,
  not the lookback window.”

  **Superseded by** `1ac9a21`. `min_observations` is window-scoped over the
  closed interval `[anchor, as_of]`; `None` means the full window;
  `history_valid_observations` is diagnostic only and cannot admit a
  short-window key. Evidence:
  `tests/test_accounting_signals.py::test_min_observations_counts_window_not_history`
  and `verification/stage1/batch2/summary.json` (F6).

* **Historical claim:** “`MomentumRanks.price_basis` does not record a custom
  basis callable.”

  **Superseded by** `1ac9a21`. `BasisProvenance` records
  `kind`/`basis_id`/`name`/`version`/`source_digest`/`parameters`; built-ins
  keep their built-in `price_basis`, while custom bases emit
  `custom:<basis_id>` and never the spec label. Evidence:
  `tests/test_accounting_signals.py` (T2.16–T2.18) and
  `verification/stage1/batch2/summary.json` (F2).

* **Historical claim:** “`sizing.py` accepts only `"P1"|"P2"|"P3"`, so callers
  must map the long `PortfolioSpec` names.”

  **Superseded by** `7972e8f`. Long policy names are canonical; short
  `P1`/`P2`/`P3` aliases are accepted only at the `size_orders` boundary and
  normalized once. `size_orders` rejects a `PortfolioSpec` directly.
  `resolve_sizing_policy` and `size_portfolio_orders` now exist, with
  `size_portfolio_orders` as the `PortfolioSpec` entry point that validates and
  resolves the policy before sizing. Evidence:
  `tests/test_accounting_sizing.py`, `tests/test_specs.py`, and
  `verification/stage1/batch3/summary.json` (F12/F13).

### Scope and gate effect

These corrections are component-level. They do not promote any contract row,
and passing component tests do not convert a non-PASS contract status into a
PASS. The overall P0 gate remains **CLOSED**, and integration remains
unauthorized. The historical gate statement above is not softened by these
component results.

### Batch 4 authorization machinery

Batch 4 replaces the report/inventory authorization machinery with a 29-row
revision-bound model. The model includes `P0-E2E`, eight inventory states, a
mandatory/optional skip split, and `data/README.md` tracking. The exact
evidence is in `verification/stage1/batch4/`; `DESIGN.md` there defines the
red-before, focused/regression, inventory, component-results, checker and
summary artifacts. This replacement changes verification machinery only; it
does not promote a contract row or open the gate.

## 2026-11-14 — Batch 5 domain-contract addendum

Batch 5 adds five labelled-synthetic domain modules and extends the dividend
path. None of this addendum is production evidence.

| Module | Contract | What is proven with synthetic fixtures | Missing source |
|---|---|---|---|
| `continuity.py` | `P0-CONTINUITY` | Dated publisher/document/citation/digest boundaries; future documents invisible; conflicts, duplicate edges and cycles reported; no similarity joins | Authoritative dated NSE/company documents |
| `scoped_adjustment.py` | `P0-SCOPED-ADJ` | Immutable raw prices; exact rational factors only inside security/date scope; point-in-time versus ex-post views; wrong-security/digest factors rejected | Verified corporate-action terms |
| `lifecycle.py` | `P0-LIFECYCLE` | States only from dated available records; suspension, delisting, temporary non-trading and unknown distinguished; terminal proceeds null unless verified; missing bar never a state | Authoritative exchange lifecycle records and terminal proceeds |
| `scenarios.py` | `P0-SCENARIO` | Separate scenario cash/holdings/NAV; immutable reference; hypothetical labels; authoritative statistics rejected | A production reference book and certified stress run |
| `liquidation.py` | `P0-FULL-LIQUIDATION` | Dated sell-side costs charged once per order; adverse slippage; capacity-limited integer fills; unknown price/capacity yields `None` | Dated real cost schedules and verified market capacity |
| `dividends.py` + `core.py` | `P0-DIVIDEND` | Full gross payment only under `gross_dividend_no_withholding_model`; opening-balance confirm/void/correct as immutable ledger events; `nav_reason` and `unresolved_settlement_claims` | Production withholding/partial-payment evidence |

**Tests and fixtures.** New focused modules are
`tests/test_accounting_continuity.py`,
`test_accounting_scoped_adjustment.py`, `test_accounting_lifecycle.py`,
`test_accounting_evidence_contract.py`, `test_accounting_scenarios.py`,
`test_accounting_liquidation.py`, `test_accounting_batch5_integration.py` and
`test_accounting_dividend_decisions.py`; `test_accounting_dividends.py` and
`test_accounting_replay.py` are extended. New hand-derived goldens are
`continuity_cases.json`, `scoped_adjustment_cases.json`,
`lifecycle_cases.json`, `scenario_cases.json`, `liquidation_cases.json`,
`batch5_integration_cases.json` and `report_matrix_cases_v2.json`. The Batch 4
goldens remain byte-identical.

**Status effect.** The matrix moves to **6 PASS / 11 PARTIAL / 6 FAIL /
3 BLOCKED / 3 NOT_IMPLEMENTED**. `P0-CONTINUITY` becomes `CONTRACT_BLOCKED`;
`P0-DIVIDEND`, `P0-SCOPED-ADJ`, `P0-LIFECYCLE`, `P0-SCENARIO`,
`P0-FULL-LIQUIDATION` and `P0-IDENTITY` become `CONTRACT_PARTIAL`;
`P0-EXECUTION`, `P0-PREFIX` and `P0-E2E` remain `CONTRACT_NOT_IMPLEMENTED`.
No synthetic-only row is promoted to `CONTRACT_PASS`. The overall P0 gate
remains **CLOSED**, and migration, historical strategy execution and
performance publication remain unauthorized. Revision-bound Batch 5 evidence
is in `verification/stage1/batch5/`.

## 2026-11-14 — Batch 6 end-to-end addendum

The test-only harness (`tests/stage1_harness.py`) composes the public components
into one causal flow over the hand-derived golden (`tests/golden/e2e_cases.json`).
Its frozen-order perturbation, economic/provenance prefix invariance and
end-to-end reconciliation checks are regression coverage only; they do not
advance any contract status.

`P0-EXECUTION`, `P0-PREFIX` and `P0-E2E` remain `CONTRACT_NOT_IMPLEMENTED`;
the test-only modules `tests/test_stage1_harness.py`,
`tests/test_stage1_component_contracts.py`,
`tests/test_stage1_frozen_order_perturbation.py`,
`tests/test_stage1_prefix_invariance.py` and
`tests/test_stage1_e2e_reconciliation.py` do not constitute production-contract
evidence. `P0-VERIFY` is corrected to `CONTRACT_PASS` because its Batch 4 findings
were resolved. The overall gate stays **CLOSED**.

## 2026-11-16 - Batch 8 evidence addendum

Batch 8 adds external evidence and typed, non-promoting loaders. It does not run
the strategy and publishes no returns or performance statistics.

**Calendar (`P0-BOUNDARY`).** The published-calendar lane covers all 96 months
of 2018-2025 and all 10 months of 2026 through October: 1,983 sessions, 147
closures and 13 special sessions, with 10 contradictions resolved. The loader
emits month-end signals only for a month whose every source was available by
that month's last-session close. OD-B9-1 records the owner's digest-bound
`CalendarOwnerAcceptance` through 2026-10-31, so `P0-BOUNDARY` is promoted;
the acceptance does not authorize any strategy return.

**Continuity (`P0-CONTINUITY`).** Two independently audited documentary lanes
are bound:

| Lane | Boundaries | Effective coverage | Catalogue | Retention |
|---|---:|---|---|---|
| NSE Indices press releases | 59 (30 mergers, 29 demergers) | 2018-02-05..2026-09-07 | 1,113 releases + 5 references | all committed |
| NSE-filed company announcements | 14 (7 mergers, 6 demergers, 1 composite scheme) | 2018-03-31..2022-05-03 legal scheme dates | 302 documents | 14 primary PDFs + 302 text extracts; 288 PDFs and 60 containers in a disclosed external cache |

The combined loader binds 73 boundary objects and 1,415 document digests, but
the typed ledger admits 63 records and fails closed on 10 excluded records with
5 `ambiguous_successor`/`duplicate_boundary` issues at the 2026-11-13 probe:
the Batch 10 parent extension collides with history-lane edges on the same
source key. Both corpora are screened subsets, so `coverage.complete=false` and
`require_coverage` refuses continuous coverage even inside a declared proven
interval. Residual parent gaps are 2018-01-01..2018-02-04 and
2026-09-08..2026-11-16; history gaps are 2018-01-01..2018-03-30 and
2022-05-04..2022-05-30. No `symbol_change`/`isin_change` edge and no owner
acceptance of the current revision exist; the signed request chose "require
complete corpus" (OD-B9-2). The history lane proves legal scheme
effective/appointed dates, not NSE Indices index-effective dates. The
2026-11-17 audit rehashed 1,113/1,113 PDFs and 5/5 references with zero
mismatches. The row
remains `CONTRACT_BLOCKED`.

**Membership (`P0-UNIVERSE`).** The self-contained lane contains 108 exact-date
NIFTY 500 snapshots (2013-04-18..2022-03-31), their sidecars, 54,072 one-day
`MembershipInterval` records, 108 one-day `MembershipCoverageWindow` records and
917 distinct symbols (695 files, 102.8 MB). The separate delta audit covers 107
delta intervals: 569/569 removals and 570/570 additions are explained, with 45
alias pairs and one disclosed timing mismatch. The typed loader verifies every
retained byte and reconstruction binding and exposes only evidence available at
the caller's decision. Dates between snapshots remain `CoverageUnknown`; the
post-2022 chain is absent; the 2026-11-15 current CSV contributes no interval.
The lane is `READY_FOR_OWNER_REVIEW`, not `CONTRACT_PASS`; the row remains
`CONTRACT_BLOCKED`.

**Tests.** `test_accounting_continuity_evidence.py` (23 tests),
`test_accounting_continuity.py` (13 tests), `test_nifty_membership_intervals.py`
(13 tests) and `test_nifty_membership_evidence.py` (13 tests) cover digest
tampering, authority/venue refusal, future-evidence invisibility,
late-evidence non-backfill, duplicate/conflict refusal, external-cache
metadata-only handling, exact-date membership and coverage refusal.

**Status effect.** No status is promoted. `P0-CONTINUITY`, `P0-UNIVERSE` and
`P0-BOUNDARY` remain blocked or partially evidenced as recorded in the matrix.
The overall P0 gate remains **CLOSED**.
