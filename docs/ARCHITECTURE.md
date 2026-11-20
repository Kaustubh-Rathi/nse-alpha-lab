# Architecture — NSE-only, SOLID, feature-based

**Primary = NSE official. Third-party (Kaggle/Yahoo/Wayback) = THIRDPARTY-* cross-check only.**

## Structure (actual)

```
src/
  get_data.py                    CLI entrypoint (only flat file, by design)
  nse_pipeline/                  ← CANONICAL, feature-based
    config.py                    Settings (SRP)
    contracts.py                 Protocols (DIP/ISP)
    download/client.py           NSE HTTP, rate-limit, hash, resume
    build/catalog.py             prices+actions + DuckDB
    audit/calendar.py            calendar/delisting audit
    audit/gaps.py                old-bhavcopy probe
    audit/readiness.py           evidence-based full-history gate (fundamentals deferred)
    adjust/factors.py            split/bonus only
    universe/builder.py          research eligibility (NOT Nifty)
    identity/lineage.py          dated edges
    backtest/engine.py           shared-cash, next-session, costs, circuit
    backtest/history.py          bounded-memory raw-share historical diagnostic
    backtest/successor.py        successor/entitlement safety audit
    backtest/total_return.py     gross total-return layer
    backtest/accounting_types.py immutable financial records and phase keys
    backtest/core.py             isolated delayed-settlement accounting operations
    backtest/fees.py             integer affordability and paise-rounded fees
    backtest/share_actions.py    exact raw-share split/bonus terms
    backtest/dividends.py        explicit normal entitlement/payment scheduling
    backtest/valuation.py        defensible NAV and reference-only diagnostics
    backtest/invariants.py       independent typed-event economic replay
    backtest/continuity.py       dated documentary identity boundaries (no similarity joins)
    backtest/scoped_adjustment.py versioned security-scoped adjusted-series view
    backtest/lifecycle.py        dated suspension/delisting/non-trading states
    backtest/scenarios.py        isolated hypothetical scenario portfolio/statistics
    backtest/liquidation.py      friction-aware, capacity-bounded liquidation estimate
    dataset/manifest.py          integrated dataset
    actions/older.py             pre-2011 NSE API
    actions/verified.py          explicit action terms + exact ex-date/identity normalization
    actions/triage.py            needs_review buckets
    actions/dividends.py         dividend events + gross ex-date reinvestment factor
    actions/specials.py          FIN/INT/SPL multi-amount specials (NSE 2% rule)
    actions/pre2011_queue.py     ranked pre-2011 queue
    actions/pre2011_circulars.py issuer-document terms
    actions/pre2011_verify.py    pre-2011 verification pass
    fundamentals/index.py        filing index (no values)
    schemes/indexer.py           announcements index
    schemes/ledger.py            scheme ledger
    schemes/canonical.py         all known scheme-days + reconciled verified evidence
    schemes/attachments.py       bounded ZIP/PDF/OCR ingestion (not verification)
    schemes/acquire_attachments.py cached/bounded attachment acquisition
    schemes/pdf_extract.py       PDF text extraction
    schemes/ratios.py            PDF ratio extractor (fail-closed)
    nifty/snapshots.py           official exact-date importer
    nifty/licensed.py            strict licensed effective-interval import and as-of lookup
    nifty/documentary.py         exhaustive monthly + press releases
    nifty/alt.py                 Wayback/Wikipedia (THIRDPARTY)
    nifty/proxy.py               interval carry + replay (PROXY)
    nifty/purchase.py            purchase/enquiry pack
    nifty/vendor.py              purchase comparison
    thirdparty/kaggle_nifty.py   THIRDPARTY
    thirdparty/kaggle_ohlcv.py   THIRDPARTY (Yahoo adj)
    thirdparty/yahoo.py          THIRDPARTY cross-check
tests/                           root-level feature and cross-stage regression tests
    stage1_harness.py           test-only Batch 6 composition harness (no legacy imports)
data/{raw,processed,reports,manifests}/
docs/ARCHITECTURE.md           this file
```

## SOLID
- **SRP:** one feature folder = one reason to change.
- **OCP:** new NSE source = new module in `download/`, no edit to `build/`.
- **LSP/ISP/DIP:** `contracts` Protocols; stages depend on abstractions + `Settings`.
- **No duplication:** shims deleted; `src/get_data.py` is the sole flat file (CLI entrypoint, standard pattern).

## Commands
`uv run python src/get_data.py fetch|update|build|validate`
`uv run python src/get_data.py repair [--apply-adjustment]` — canonical schemes → verified actions → optional adjustment export → readiness → manifest; exits 2 while blockers remain
`uv run python src/get_data.py readiness [--start] [--end] [--universe]` — evidence gate; exits 2 while blockers remain
`uv run python src/get_data.py history [--allow-incomplete] [--out] [--cash] [--top-n]` — bounded raw-share diagnostic, **strict by default**; refuses while blockers remain
`uv run python src/get_data.py attachments [--fetch] [--symbols] [--years]` — bounded scheme-document ingestion, not verification
`uv run python src/get_data.py dataset` — refresh the integrated manifest
`uv run python -m nse_pipeline.audit.calendar --cutoff 2026-11-07` (or `src/get_data.py validate`)
`uv run python -m nse_pipeline.dataset.manifest`
`uv run python -m unittest discover -s tests -v`

## Stage 1 foundation: settlement and session ordering

**2026-11-13 user-approved clarification:** delayed trade settlement with an
explicit positive session lag supersedes the errata's immediate cash-at-fill
wording for trades. Fills recognize economic holdings and receivables/payables;
settled cash moves only on actual settlement. The NAV/reservation distinction
is unchanged. Immediate-cash arithmetic can be checked after a paired fill and
settlement, but is not the delayed model's fill-time cash balance.

This contract implements the approved final plan **with FINAL_PLAN_ERRATA
overrides**. The isolated accounting components now implement the supported
slice below; this is not a claim that legacy runners follow it. No legacy
execution loop is migrated here.

### Settlement (cash account, no borrowing or unsettled-proceeds funding)

Separate economic ownership, settlement obligations and order reservations:

* `C`: total settled cash; `R_sale`, `R_div`, `R_other`: valued receivables;
  `L`: outstanding purchase/fee/other payables; `B_orders`: open-buy reservations;
  `B_payables`: settled cash earmarked to discharge L. No margin, shorting,
  settlement netting or spending of expected proceeds.
* **NAV = C + R_sale + R_div + R_other + marked_holdings - L**, only when all
  relevant assets and obligations have defensible valuations. This extends the
  errata's zero-liability formula; subtracting reservations from NAV is forbidden.
  An unknown amount blocks NAV. Stale reference notionals never fill the gap.
* **Available cash = C - B_orders - B_payables**. Fully prefund each purchase
  and fee: `B_payables = L` for supported purchase/fee obligations. A payable is
  deducted once from NAV, and its cash earmark once from spending capacity;
  these are different constraints, not two expenses. Unsupported or unfunded
  liabilities block new purchases and fail the cash-account invariant.
* **Purchase fill:** economic shares increase at trade time (including unsettled
  holdings). Replace the filled order reservation with a payable and matching
  cash earmark for actual slipped notional plus fees; C does not change until
  settlement. Slippage changes price only. At settlement reduce C, L and its
  earmark together; NAV is unchanged by settlement itself. Partial fills release
  unused reservations; the unfilled remainder expires unless explicitly reissued.
  Delivery status is tracked separately; resale before delivery is unsupported.
* **Sale fill:** remove economic shares and recognize a net sale receivable
  (notional less fees); do not credit C. At settlement exchange the receivable
  for C exactly once. Same-open sells cannot fund buys. Do not both net fees
  from this receivable and book them again as a payable.
* **Dividend:** establish eligible cum-dividend quantity before ex-date fills;
  accrue R_div on the ex-date, never cash. Use admissible verified payment dates
  where available, otherwise require an explicit positive-lag research setting
  (calendar days or exchange sessions) and compare multiple lags plus
  hold-receivable. Ex-date buyers are not entitled; entitled ex-date sellers keep
  their receivable. No automatic reinvestment or record-date-holdings shortcut.
* **Liabilities:** fees reduce wealth at the fill, not again on payment. Other
  verified obligations require explicit recognition/payment events and full
  prefunding; unknown terms remain unresolved. Reservations are not liabilities.
  Verified redemption consideration is a receivable until paid, not an invented
  sale or immediate spendable cash.

Conservation is evaluated on recognition **and** settlement ledgers separately:
`C_t = C_0 + sum(actual settled receipts - actual settled payments)`;
`R_t = R_0 + sum(recognized claims - settled receipts)`; and
`L_t = L_0 + sum(recognized obligations - settled payments)`, by account.
Trade-date purchase/sale recognition has zero C delta under the delayed model.
Dividend accrual is not a cash receipt; earmarking is not a cash payment. Initial
balances and separately declared external flows must be included explicitly.

Trade settlement lag must be supplied explicitly in exchange sessions; a fixed
lag is a declared research assumption, **not** a claim about historical NSE
T+2/T+1/T+0 regimes. A verified dated settlement schedule can supersede it in a
later component. No hidden default is permitted. Only lag >= 1 is supported in
the foundation contract; same-session settlement requires a separate phase model.
Date-only dividend payments on a session are credited **after opening fills**;
non-session payments map to that phase of the first subsequent observed session,
and are spendable at the next opening. Late documentary evidence never rewrites
prior decisions or cash. Principal, fees and cash are Decimal amounts, rounded
once per order/component to paise (ROUND_HALF_UP); valuations need not round each
position before aggregation. The declared hard principal cap is the one deliberate
exception: it is floored to paise at use (`99.996` becomes `99.99`) on both buy and
sell, so rounding can never admit a fill above the declared maximum; fees never
expand it. NAV is the exact unrounded Decimal; `nav_reported = money(nav)` exists
only at the presentation boundary. `nav` is `None` whenever any financial asset or
obligation lacks a known value; separate `nav_reason` and reference-coverage
diagnostics never substitute a stale reference for a current mark. Fixtures identify
whether amounts are unrounded.

### Deterministic session order

Economic event keys are `(session, phase, sequence, stable_event_id)`, not run
UUIDs or eventual dataset endpoints. Within fills: sells first, then buys in
frozen signal rank/key order. Every item is applied exactly once. A decision sees
only evidence available by its phase and economically effective where required.

| Phase | Events and admissible inputs |
|---|---|
| 10 PRE_OPEN_EVIDENCE | Admit timestamped evidence known by pre-open; preserve publication/availability and effective time separately. Date-only observations first become known after close, unless a source establishes an earlier phase. |
| 20 PRE_OPEN_SETTLEMENT | Settle previously scheduled, due trade payables/receivables from prior sessions. No advance use of unscheduled future receipts. Reconcile earmarks and cash. |
| 30 PRE_OPEN_ACTIONS | Snapshot cum-dividend economic entitlements, then apply proven identity changes and split/bonus/conversion events to eligible pre-open holdings and unsettled-delivery records. Same-day split/dividend terms must specify consistent share units and ordering or remain unresolved. Restate or cancel pending orders on an action; never use stale share units or create extra cash. |
| 40 OPEN_FILLS | Execute the prior close's frozen orders using the current open only, prior as-of capacity/budgets and funded cash. Recheck fees, integer quantities, earmarks and slot limits atomically. No current-session high/low/close/volume/traded value or date-only cash credits. |
| 50 POST_OPEN_SETTLEMENT | Credit verified/modeled date-only dividend or other scheduled cash settlements and extinguish their receivables. Cannot retroactively fund phase-40 orders. |
| 60 CLOSE_EVIDENCE_AND_VALUATION | Admit the completed session's observations and newly available documentary evidence; resolve identity/lifecycle as-of, mark defensible prices, report unresolved assets and NAV or None, and audit cash/receivable/payable/share conservation. No synthetic last-session disposal. |
| 70 CLOSE_SIGNALS_AND_ORDERS | Generate momentum/universe decisions from phase-60 evidence; select targets and reserve fully funded orders for the next observed opening. Reservation changes do not change the just-computed NAV. An unobserved future month-end cannot trigger a signal. |
| 80 CLOSE_AUDIT | Save closing cash, reservations, receivables, liabilities and order snapshots. Run-level metadata and later diagnoses are excluded from economic prefix comparisons, not erased from provenance. |

Hand-calculated fixtures must pin every recognition and settlement phase, including
the zero-liability limit. Passing fixture/oracle checks is **not** passing P0
production-component tests. Full-history migration and performance publication
remain blocked on the complete P0 gate.

### Foundation provenance

Baseline: `c659d25` / tag `pre-stage1`, authored with the existing local commit
identity `dream excellence <Kaustubh-Rathi@users.noreply.github.com>` (from the
local wealthx repository; no Git identity configuration changed). Baseline tests:
`812 passed, 5 skipped, 129 subtests`. The baseline input inventory is
`data/manifests/dataset_hashes.json`; it is not a consumed-input run manifest.

Foundation implementation lives in `nse_pipeline.paths`,
`nse_pipeline.build.portable` and `nse_pipeline.specs`. Existing catalog readers
use the portable adapter; legacy signal/accounting/execution algorithms have
not been migrated. The writer and reader share the same view definitions and
preserve the writer's selected partitions. Missing relocated sources fail
closed, including optional action sources named by existing views; no read
operation rewrites the persistent database.

Path precedence is explicit argument > `NSE_DATA_DIR` > repository `data`.
Relative environment overrides are repository-relative; explicit relative
arguments are caller-relative. Settings resolves at construction; compatibility
constants in legacy modules are import-time snapshots of this policy. External
documentary provenance paths may be absolute; full artifact-manifest portability
is a later task, not a claim made by the DuckDB adapter.

Use `uv sync --locked` for runtime plus development dependencies, or
`uv sync --locked --extra pdf` to include the PDF backends. The project retains
its existing `package = false` configuration; pytest adds `src` to its import
path. For `python -m nse_pipeline...` from an unrelated working directory,
explicitly set `PYTHONPATH` to the checkout's `src` directory; alternatively
invoke the absolute `src/get_data.py` path. A clean verification environment was
created under `%LOCALAPPDATA%/Temp/opencode/stage1-foundation-venv`, leaving the
original `.venv` unchanged.

Independent expected data live in `tests/golden/{accounting,valuation,execution}_cases.json`.
The test-only Decimal/Fraction oracles audit those hand calculations; no legacy
engine supplies expected results. The accounting milestone exercises these
fixtures through production portfolio, dividend, fee and valuation operations
with independent ledger replay. Identity, universe, signals, P1/P2/P3 sizing and ADV
estimation now exist as tested components. Batch 5 added documentary continuity,
scoped historical adjustment, dated lifecycle states, isolated scenario portfolios
and friction-aware liquidation as synthetic-fixture domain contracts, while
execution/prefix and end-to-end contracts remain outstanding. These fixtures do
**not** authorize migration; production documents, membership, calendars and
liquidation evidence are still absent.

The specification vocabulary is explicit. Long `PortfolioSpec` policy names
(`P1_freeze_on_unresolved`, `P2_size_excluding_unresolved`,
`P3_size_including_unresolved_at_screen`) are canonical in serialized specs,
goldens and `SizingResult.policy`; the short `P1`/`P2`/`P3` aliases are accepted
only at the `size_orders` boundary and normalized once. `size_portfolio_orders`
is the only `PortfolioSpec` entry point: it runs `validate_run_specs` before any
order and maps `max_positions` to sizing slots. P3 requires an explicit
caller-supplied screen value and screen price for every unresolved rank, never a
stale mark, remains capped by settled cash, and reports `screen_value_unavailable`
without consuming cash or a slot when that evidence is absent. The D-16
four-state registry classifies all 27 audited fields, recording the
10 implemented / 12 reserved / 0 obsolete / 5 disconnected baseline and the
post-wiring 15 / 12 / 0 / 0 counts. The configuration row remains
`CONTRACT_PARTIAL` until clause-level contract evidence exists.
Trade and dividend lag values remain explicit research inputs, not hidden
defaults or asserted historical exchange settlement schedules.

Data-integrity incident: the initial failing CLI path test inadvertently invoked
the legacy dataset command against the real default data directory. A complete
SHA256 comparison of all 8,153 inventoried data files found exactly one content
change: `data/processed/dataset/dataset_manifest.json` was regenerated. Raw files,
the database and all other inventoried files were unchanged. The original
manifest bytes were not backed up and have not been reconstructed or falsely
reported as restored. The baseline inventory retains its original hash. The CLI
test now relocates the source tree as well as data, so a broken default cannot
write into this checkout. No strategy performance was generated or published.

**Integrity exception disposition (user accepted 2026-11-13): CLOSED_WITH_UNRECOVERED_MISMATCH.**
The exact initiating test was the original
`tests/test_foundation_paths.py::test_cli_uses_environment_root_from_unrelated_cwd`,
executed by `.venv/Scripts/python.exe -m pytest tests/test_foundation_paths.py -q
--no-header -p no:cacheprovider`. Its subprocess ran `src/get_data.py dataset`;
the old `DEFAULT_DATA` ignored `NSE_DATA_DIR`, the dataset dispatch called
`build_dataset()`, and its final `write_text()` overwrote the existing manifest.
The current test uses relocated source and `readiness`, not production datasets.

Original SHA256: `5547ac458195acc1c4443dd042d9377ac4a79330533a764b038f4c89ff1d4ab9`.
Retained regenerated SHA256: `c304d08ef89e3e91b738ba28c201bbcdb045f627ec6615bef87400aaf6d44293`.
Git has no tracked version or unreachable recovery object; a filesystem search
of this workspace and the approved OpenCode temp tree found no matching backup.
No claim is made about backups outside that inspected scope. The current file
is preserved **untrusted**, excluded from commits and from authoritative inputs
until a separately authorized rebuild or a matching original backup is supplied.
The baseline inventory is not changed. No timestamp, content or original hash is
fabricated to conceal the mismatch.

`tests/conftest.py` now protects the production data tree before collection.
Scoped Python audit hooks block mutation, and Windows no-write/no-delete sharing
handles protect existing files against native DuckDB writes and child processes
even when Python startup hooks are bypassed. Python children also inherit the
audit policy through verification-only `sitecustomize`. This is a verification
safety measure, not a sandbox for hostile executables. Temporary fixture trees
remain writable. Regression tests exercise the exact dataset CLI failure path,
direct Python mutation, an isolated child and native DuckDB COPY.

Foundation verification: `848 passed, 129 subtests`, no failures/skips, in the
clean PDF-enabled environment; the 31 new foundation tests also pass as a
standalone group. `uv lock --check`, Python syntax parsing and `git diff --check`
pass. Full changed-file, command, test and integrity details are recorded in
`data/reports/stage1-foundation-verification.json` (preserved historical report).
The reviewed foundation is committed as `76adf01`. Refreshed integrity reports
are outside production data at `verification/foundation-postcommit.json` and
`verification/accounting-posttests.json`. The accounting milestone's current
results, exact file list, golden reconciliation and unsupported cases are in
`docs/STAGE1_ACCOUNTING_VERIFICATION.md` and the machine-readable
`verification/accounting-component-results.json`. The overall P0 production gate
remains **CLOSED**; runner migration and performance publication are unauthorized.

## Legacy full-history status (not a Stage 1 acceptance result)

See `docs/FULL_HISTORY.md` and `data/reports/full-history-readiness.json`.
Code correctness and documentary term verification are separate from complete,
executable historical data. Neither passing tests nor a positive return proves
alpha. Fundamentals are not required for the current price/volume strategy;
dividends, corporate actions, security identity and terminal proceeds still are.

Current gate: **7 blockers** (calendar, corporate actions, schemes, identity,
terminal proceeds, dividends, execution capabilities); `ready_for_backtesting`
and `ready_for_alpha_claim` are both false. The coverage counts behind those
blockers live in the JSON reports: 10,792,672 price rows over 6,677 sessions;
342 pre-2011 split/bonus actions applied of 343 execution-ready (364 terms
verified) and 2,105 applied in total; 1,770,456 adjusted bars over 1,033
symbols; 46,137 distinct dividend events with 31,987 included; 1,071 identity
edges with 1,384 unresolved and 0 of 328 terminal proceeds resolved; 86
unresolved calendar dates against 274 confirmed closures; 396 canonical scheme
events with 25 candidate terms and 0 execution-ready.
