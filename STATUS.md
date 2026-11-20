# Status

**Status: Research preview — validation in progress.**

Version 0.1.0. This is the first public release of NSE Alpha Lab. It is a working
research and backtesting tool whose core accounting is tested, and whose *interpretation*
is still under active validation. The distinction between those two things is what this
document is for.

If you are deciding whether to rely on a number from this repository, read the "Not yet
validated" section first.

| | |
|---|---|
| Project state | Research preview — validation in progress |
| Version | 0.1.0 |
| Test suite | Runs offline with no data pack and no network. Tests that need deliberately non-public content (`verification/` evidence, the separate data pack) skip with a visible reason instead of failing. CI runs the suite on Ubuntu with Python 3.14; no Windows or macOS CI job exists, so this document makes no three-OS claim. |
| Internal consistency checks | 348 / 348 passing, recomputed from the published artefacts (`research/validate_alpha_output.py`) |
| Results published | All 968 configurations, including the null result |
| Live trading | Not supported, not planned for this release |
| Investment advice | No — see [DISCLAIMER.md](DISCLAIMER.md) |

---

## 1. What has been checked

These items are verified in code, covered by the test suite, and independently
recomputed by the validation harness (348 checks, all passing). Each check **fails
closed**: a missing or unparseable input raises rather than defaulting to zero.

### Accounting identities

- Cash, position, and equity reconcile to the rupee at every session of every run. The
  equity series is rebuilt from the trade ledger and compared against the engine's own
  output.
- The total-return identity — final equity ÷ initial equity − 1 — is independently
  recomputed and compared to the reported figure in every row of the results table.
- No unknown or NaN equity points appear in any published run.
- The gross-of-costs counterfactual has strictly higher final equity than the
  net-of-costs run, on identical trades. This is a sign check on the entire charge stack:
  if the charges had the wrong sign anywhere, this fails.
- The equity series is strictly sorted by date and free of duplicate sessions.

### The charge stack

Every trade is charged at the fill, and the charge is stored on the trade so it can be
audited individually:

| Component | Rate applied |
|---|---|
| Brokerage | 2 bps of notional, capped at ₹2,000 per order |
| STT | 10 bps, both sides |
| Stamp duty | 1.5 bps, buy side only |
| Exchange charges | 0.3 bps |
| GST | 18% of brokerage |
| Slippage | 5 bps, both sides, charged as explicit cash |

Checks that pass on this:

- Buy and sell charges are strictly positive for every charge-bearing run.
- The six components sum exactly to the stored total for each trade and for each run.
- Charges are recomputed from notional and side and compared against the stored value;
  any divergence fails the run.
- Turnover, bps-of-turnover and charges-as-percentage-of-initial-cash are recomputed from
  the trade list.
- A zero-cost run emits exactly zero charges on every component.

### Split and bonus mechanics

- Split and bonus adjustment factors are applied before the engine sees a price; the
  engine consumes an already-adjusted series and does not apply corporate actions itself,
  so it cannot silently double-count one.
- Share-action terms are computed in exact raw-share arithmetic matches rather than
  floats, so a 2:1 split or a 1:1 bonus produces exactly the expected position change.
- Dated corporate-action events carry provenance; an event with no verified terms is left
  unresolved rather than guessed.
- No holding is ever marked at a stale or invented price. A position with no bar on a
  session is flagged and counted, never marked at yesterday's close and never at zero.

### Deterministic reruns

- Two runs over identical inputs produce byte-identical output: trade list, equity curve
  and metrics. Ranking ties break on the security key, not on dictionary or file order.
- The engine is pure — it performs no I/O and reads no clock — so rerun stability is a
  property of the code rather than of the environment.
- The published sweep can be regenerated from the shard files and reproduces the table.

### Internal consistency checks (348)

`research/validation.json` records **348 checks, 0 failures**. They fall into five groups:

| Group | Count | What it covers |
|---|---:|---|
| Evidence-row contract checks | 149 | Equity integrity, drawdown depth/dates/durations, charge reconciliation, universe-hold accounting, per config |
| Flagship-row checks | 71 | The six headline rows, reproduced against the CSV |
| Sweep CSV checks | 44 | Row counts, sort order, rank contiguity, header uniqueness, family and universe counts, charge totals |
| **Independent recomputation** | **36** | Beta, alpha (linear and geometric), tracking error and OLS t-statistics recomputed from the aligned return series for each config × benchmark pair |
| Benchmark, calendar and universe | 48 | Coverage, session counts, membership non-emptiness, cadence composition |

The 36 recomputations are the important ones: they do not reuse the engine's output, they
recompute the statistic from the raw aligned returns and compare.

---

## 2. Not yet validated

These items are known and open. They are not oversights — they are the remaining work,
and each one is capable of changing the sign or the size of a headline number.

### Dividend-adjusted returns

**Results are price-vs-price, not total return.** Prices are split/bonus adjusted only.
Dividends are recorded as dated events with ex-dates and amounts, but they are neither
deducted from the price series nor credited as cash, because doing so consistently needs,
for every ex-date, the shares held, the withholding treatment and a reinvestment
convention — none of which is defined in this repository yet.

The benchmark is also a price index, so the comparison is internally consistent on the
benchmark axis, but **both sides understate total return**. The sign of the net alpha bias
depends on the strategy's dividend yield relative to the NIFTY 50's, and that has not been
established. Until it is, no number here should be described as a total-return result.

### Post-2022 NIFTY 500 membership regularity

Official monthly constituent snapshots run to 2022-03-31 (108 of them). From 2022-03-31 to
2026-09-30 membership is carried as **65 official event anchors** with `CoverageUnknown`
between them — dates that fall between anchors do not have a proven constituent set.

Consequences, all disclosed in the reports:

- After 2022-03 the strategy is **event-anchored, not strictly monthly**.
- The gap between consecutive rebalances has a median of 30 days and a **maximum of 154
  days**; 13 gaps exceed 45 days.
- The post-2022 calendar is endogenous to the evidence: corporate-action anchors cluster,
  which adds turnover that a fixed monthly calendar would not have.
- Where no snapshot exists for a scheduled date, the engine **holds the book unchanged**
  and counts it as an unproven hold. It does not rebalance and it does not liquidate. The
  published evidence-dated runs report zero unproven holds because every rebalance lands
  on a proven date.

### NIFTY 750

**There is no NIFTY 750 test in this repository.** The membership chain does not exist on
disk: there is no point-in-time constituent history for NIFTY Total Market or Microcap 250.
Midcap 150 and Smallcap 250 are subsets of NIFTY 500, so their union is still a 500-name
universe, not 750. Reconstructing the missing 250 names from *today's* constituents would
inject survivorship and look-ahead bias, so no synthetic 750-name universe was created. This
is recorded as a data gap, not answered with a fabricated series.

### Live execution, liquidity and market impact

The engine models a decision and a fill, not a market. Not modelled:

- **Market impact** — a 10-name equal-weight book trading ₹4.1 billion of notional
  cumulatively would move prices.
- **ADV / participation limits** — no position size is bounded by observed volume.
- **Circuit limits and trading halts** — a limit-down session would not fill at the open.
- **Suspensions and thin names** — a suspended holding is carried, but a real order book
  would not have existed.
- **Capital-gains tax** — not modelled at all; SEBI turnover fees and depository charges
  are also absent.
- **The changing historical fee schedule** — the charge stack above is a 2026-style
  constant applied across 2013–2026, not a reconstruction of each rate change.

The next-open fill is honest about its own assumption: a signal formed on close *T*
executes at the open of *T+1*, never at the close that formed it.

### Regime robustness

The headline covers one 13-year sample containing two large drawdowns. There is **no
out-of-sample holdout, no walk-forward analysis and no sub-period stability test** beyond
the 2013–2022 / 2022–2026 split shown in `research/REPORT.md`. A strategy that works in one
sample and one regime has not been shown to work.

### Multiple-testing bias

**968 configurations were tested and the table is ranked by Sharpe. No multiple-testing
correction is applied anywhere in this repository.**

- The highest Sharpe (1.3362) is the maximum of 968 trials, not an expectation.
- Only **26 of 968** configurations have a positive Newey–West t above 1.96, about the
  **24** expected by chance in the upper 2.5% tail if nothing had any edge. Another **43**
  are significantly negative, so the negative tail is the excess; the positive tail is
  roughly at chance, not below it.
- The median excess CAGR is **−9.73%**: the typical configuration loses to the NIFTY 50.
- Only **220 of 968** configurations beat NIFTY 50 on CAGR (22.7%), and **none of the 150
  NIFTY 50 configurations** beat the index they are drawn from.
- The pre-specified primary configuration — chosen before the sweep, not from it — is
  **not significant**: Newey-West(5) t = 1.62, block-bootstrap p = 0.096.

The pre-specified configuration is the result that carries inferential weight. Everything
else in the table is a descriptive sensitivity map. Any future claim that picks a
config *after* seeing 968 results must first apply a correction we have not yet implemented.

### Also open

- **Terminal proceeds for delisted holdings.** The `last_close_writeoff` policy exits at the
  last observed close. Optimistic if true proceeds were zero. One writeoff affects the
  primary configuration.
- **Identity resolution.** 28 identity cases remain unresolved; 3,503 stable keys are used
  and 487,147 bars are re-keyed across symbol changes. Unresolved cases are dropped, never
  substituted.
- **Rights, demergers and special corporate actions.** Tracked as dated events with
  provenance; some terms remain unresolved rather than assumed.
- **Fundamentals.** Out of scope for this release.

---

## 3. What this status means for you

| If you want to… | Then… |
|---|---|
| Read the research and critique it | Go ahead. The full data and the full 968-row table are published precisely so the findings can be attacked. |
| Learn how a charge-aware backtester is built | Fine. The accounting and causality rules are enforced in code and tested. |
| Reuse the engine for your own strategy research | Fine, but validate on your own data and treat all output as price-vs-price research, not total return. |
| Quote a Sharpe ratio from this repository | Do not do it without the multiple-testing disclosure. Read the [Results section of the README](README.md#results). |
| Trade this | No. Not supported. Not investment advice. |

---

## 4. Reporting a problem

Corrections are welcome, especially to the provenance in
[DATA_LICENSES.md](DATA_LICENSES.md) and to anything in this file. Include the
configuration id (`config_id` column), the file, and what you expected instead.

## Related documents

- [README.md](README.md) — overview, quickstart, results, limitations
- [DISCLAIMER.md](DISCLAIMER.md) — not investment advice
- [DATA_LICENSES.md](DATA_LICENSES.md) — data provenance and redistribution terms
- [NOTICE](NOTICE) — attribution and third-party licences
- [research/REPORT.md](research/REPORT.md) — the primary research report
- [research/SWEEP_REPORT.md](research/SWEEP_REPORT.md) — the 968-configuration sweep
- [research/FINAL_RESULTS_public.csv](research/FINAL_RESULTS_public.csv) — the full result table
- [research/CSV_COLUMNS.md](research/CSV_COLUMNS.md) — column-by-column documentation
