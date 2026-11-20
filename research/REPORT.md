# NIFTY 500 momentum vs NIFTY 50: benchmark-relative alpha backtest

**STATUS: RESEARCH PREVIEW — validation in progress. This is a research diagnostic, not a validated alpha claim and not an investable strategy.** The internal validation gate is still open (`ready_for_alpha_claim` is `false` in `data/reports/full-history-readiness.json`). This report is published as evidence of what can be computed on today's data, and as a record of exactly how far the data can support the conclusion.

Generated from `strategy-results.json`, `benchmark-series.json`, `alpha-results.json`, `validation.json` and `FINAL_RESULTS.csv`. Every number below is rendered from those files, not transcribed by hand.

## 0. Answer in one paragraph

The primary evidence-dated portfolio (NIFTY 500, top 10 by 63-session momentum, next-open fills, monthly to 2022-03 and official-anchor dates after, split/bonus-adjusted, `last_close_writeoff`) returned **907.97%** from the 2013-05-31 starting mark (first fill 2013-06-03) to 2026-09-30 (19.30% CAGR, 27.62% volatility, Sharpe 0.778 at rf=0). Its maximum drawdown was **-55.30%**; it ran **574 calendar days / 385 sessions** from the 2018-08-28 peak to the 2020-03-24 trough, then recovered on 2021-04-06 after **378 calendar days / 257 sessions**, for a total underwater span of **952 calendar days / 642 sessions**. The longest underwater episode lasted **952 calendar days / 642 sessions**.

Against the **NIFTY 50** price index over **2013-06-03..2026-09-30**, beta is **0.95**, annualised Jensen alpha is **10.70% linear / 11.29% geometric**, tracking error 23.04%, information ratio **0.38**, with up/down capture 102.98% / 91.58%. The alpha t-statistic is **1.67 OLS / 1.62 Newey-West(5)**; the 21-session circular block bootstrap p-value is **0.096**. At the 5% level the positive NIFTY 50 alpha is **not statistically significant**. Against the NIFTY 500 over the shorter 2013-2025 window, alpha is **5.56%** (t 0.89 Newey-West); that is a robustness view, not the headline.

The opposite portfolio — the **20 weakest** 63-session performers in the same universe, on the same dates, with the same charges — returned **-64.01%** (-7.51% CAGR), with a **-91.60%** maximum drawdown that began at the 2015-04-16 peak, fell until 2020-03-25, and had not recovered by 2026-09-30 (4,185 calendar days underwater). Its alpha against the same NIFTY 50 window is **-15.46%** (t -2.13 Newey-West, which clears the 5% threshold), and against the NIFTY 500 it is **-20.57%** (t -2.96). That is the sign test for the momentum story: if the top-20 alpha were a momentum premium, the bottom-20 portfolio should sit on the losing side of it.

Every return above is **net of the actual charge stack**, not a gross series with a fee comment. The primary config paid **₹15,178,940** across its trades (₹7,901,819 on buys, ₹7,277,121 on sells). Re-running the identical strategy with every charge set to zero compounds at **22.36%** instead of **19.30%**; the charges therefore cost **3.06% of CAGR**. Section 4.1 itemises them.

That is a real, reproducible benchmark-relative diagnostic on the available data, but it is **not** evidence of a durable edge: the post-2022 membership is point evidence at irregular official dates, dividends are excluded, delisting proceeds are assumed, and the validation blockers below are unresolved. The estimate is reported so it can be attacked, not cited.

Validation: **348/348 checks passed**.

## 1. What was asked and what was run

The required test is an alpha backtest against the NIFTY 50 on all data currently available. The repository had no benchmark-relative layer: `docs/QUANT_REPOSITORY_AUDIT.md` records that there is no index price series in `src/`, so excess return, beta, Jensen alpha, information ratio, tracking error and capture were "not computable". This run supplies the benchmark evidence from local files and computes those statistics. It also runs the **opposite selection** (the 20 weakest 63-session performers) as a sign test, reports **drawdown duration** in calendar days and sessions, and itemises every buy and sell charge actually paid.

It does **not** close the internal validation gate. It is a research diagnostic under that gate, not a production result.

## 2. Data, universe and calendar

### 2.1 Prices

- Source: `data/processed/adjusted/adjusted_bars.parquet`.
- Basis: **split/bonus-adjusted only**. Dividends are not applied. This is not a total-return series. The benchmark is also a price index, so the comparison is price-vs-price; the strategy's omitted dividend yield remains a downward bias on its own return.
- Why dividends are not applied: the adjustment pipeline normalises splits and bonuses only. Dividend records exist in the corporate-actions table with ex-dates and amounts, but turning them into a total-return series needs the shares held on every ex-date, withholding-tax treatment and a reinvestment convention. None of that is defined here. Applying dividends to the strategy but not to the price benchmark would mix bases, so both sides stay price-only. A verified NIFTY 50 total-return series is not on disk; until it is, the sign of the net alpha bias cannot be established.
- Load window: 2012-06-01..2026-09-30 (warm-up for the 63-session lookback). Performance starts on the session before the first executed fill, so the pre-deployment cash period is excluded.
- Costs charged on every buy and every sell: 2 bps brokerage (₹2,000 cap/order), 10 bps STT each side, 1.5 bps stamp duty buy-only, 0.3 bps exchange, 18% GST on brokerage, 5 bps slippage. Slippage is charged as an explicit cash amount rather than moved into the fill. These are stylised constants, not the changing 2000-2026 tax/fee schedule. Integer shares, equal-weight target, next-session-open fills.

### 2.2 Universe and the calendar defect

The NIFTY 500 membership evidence is two-lane:

| lane | dates | character |
|---|---:|---|
| Batch 8 | 108 snapshots, 2013-04-18..2022-03-31 | monthly, official snapshots |
| Batch 9 | 65 anchors, 2022-03-31..2026-09-30 | **irregular point evidence**; dates between anchors are `CoverageUnknown` |

The archived momentum runs rebalanced on **calendar month-ends** and accepted a universe only when an exact snapshot fell on that date. That combination is defective: **39 of 162** monthly signal dates had no snapshot. The pre-fix engine sold the entire book when the target list was empty; the current engine holds it unchanged. Neither is a rebalance. The defect is largest in 2022-2026 and is why the archived 781% sweep is not used here.

This run uses the **evidence dates themselves** as the rebalance calendar through the engine's explicit `rebalance_dates` override:

- 107 batch-8 monthly snapshot dates, 2013-05-31..2022-03-31.
- 64 batch-9 official anchors, 2022-04-07..2026-09-30.
- Total: **171 rebalances**, each with an exact point-in-time membership set.
- Gap between consecutive rebalances: median 30 days, maximum **154 days**; 13 gaps exceed 45 days. The average cadence is roughly monthly, but the post-2022 dates are clustered around corporate-action events and can be several months apart. **This is an event-anchored strategy after 2022, not a strict monthly one.**

Universe resolution: 499–505 members per date (median 501); 47 unresolved symbol-snapshot pairs dropped, never substituted. Identity: 3503 stable keys, 487147 bars re-keyed across symbol changes, 28 unresolved identity cases.

**There is no NIFTY 750 test here.** The repository has point-in-time sets for NIFTY 500 (107 monthly official sets to 2022-03 plus 64 irregular anchors), NIFTY MidSmallcap 400 / Midcap 150 / Smallcap 250 (57 monthly sets each, 2017-07..2022-03) and NIFTY 50 (13 annual snapshots). It has no NIFTY Total Market or Microcap 250 constituent history. Midcap 150 and Smallcap 250 are already subsets of NIFTY 500, so their union is still the 500-name universe, not 750. Reconstructing the missing 250 names from today's constituents would inject survivorship and look-ahead bias, so the 750-universe question is recorded as a data gap rather than answered with a synthetic series.

### 2.3 Benchmarks

| series | source | coverage | use |
|---|---|---|---|
| `NIFTY50_YAHOO` | Yahoo ^NSEI `Close` | 2007-09-17..2026-11-08 | **primary benchmark** (NIFTY 50, full 2013-2026 window) |
| `NIFTY50_SPLICED_NSE` | NSE archive, `S&P CNX Nifty` → `CNX Nifty` → `Nifty 50` | 2012-02-21..2025-12-31 | NIFTY 50 cross-check (independent vendor/splice) |
| `NIFTY500_SPLICED_NSE` | NSE archive, documented rename splice `S&P CNX 500` → `CNX 500` → `Nifty 500` | 2012-02-21..2025-12-31 | universe robustness benchmark |
| `G_SEC_10Y_NSE` | NSE archive, Nifty 10 yr Benchmark G-Sec | 2015-11-09..2025-12-31 | risk-free proxy |

The **required comparison is the NIFTY 50**. The full 2013-2026 window uses Yahoo ^NSEI because it is the only NIFTY 50 series covering 2026; the NSE spliced NIFTY 50 is used as a cross-check, and the NIFTY 500 is retained only as a universe robustness view. The splice is a documented index rename, not an alias inferred from similarity. Boundary moves are small and continuous (see `BENCHMARK.md` and the `splices` block in `benchmark-series.json`). The NSE and Yahoo NIFTY 50 levels agree on all 2,497 overlapping sessions to within 0.000781 index points.

The G-Sec series is a **price** index; coupons are excluded, so it understates the true risk-free rate. The zero-rate view is reported alongside it.

## 3. Strategy configurations

| configuration | signal dates | trades | buys/sells | final equity (₹) | total | CAGR | vol | max DD | max DD days | max DD sessions | longest DD days | Sharpe (rf=0) | mean cash | writeoffs | unproven holds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N10, no stop (**primary**) | 171 | 2080 | 1131/949 | 100,797,469 | 907.97% | 19.30% | 27.62% | -55.30% | 952 | 642 | 952 | 0.778 | 0.05% | 1 | 0 |
| N10, 10% stop | 171 | 2182 | 1157/1025 | 95,223,860 | 852.24% | 18.78% | 23.98% | -41.63% | 861 | 582 | 861 | 0.839 | 11.85% | 1 | 0 |
| N20, no stop | 171 | 3802 | 2075/1727 | 85,217,797 | 752.18% | 17.78% | 24.58% | -56.71% | 1,218 | 822 | 1,218 | 0.790 | 0.04% | 1 | 0 |
| N20 bottom, no stop (reversal) | 171 | 4457 | 2720/1737 | 3,599,374 | -64.01% | -7.51% | 25.94% | -91.60% | 4,185 | 2,839 | 4,185 | -0.170 | 0.08% | 2 | 0 |
| N10, month-end/exact (diagnostic) | 161 | 1651 | 874/777 | 136,400,112 | 1,264.00% | 22.08% | 27.91% | -55.30% | 952 | 642 | 952 | 0.856 | 0.12% | 1 | 38 |

`signal dates` counts the scheduled rebalance dates inside the performance window, excluding the warm-up. Every evidence-dated row rebalances on all of them. The `as-committed` row has 161 scheduled month-end dates in-window, of which 123 had an exact snapshot and 38 held the book unchanged; it also has 11 warm-up holds before the first fill. It is a diagnostic, not a candidate strategy. `max DD days` is the calendar duration of the deepest drawdown from its peak to recovery (or to the end of the window if unrecovered); `max DD sessions` is the same span in trading sessions; `longest DD days` is the longest peak-to-recovery underwater episode, which can be a shallower but longer decline. Section 4.2 gives the exact dates. Every evidence-dated row in this table has **zero** unproven-universe holds.

The bottom-20 row is the mirror image of the breadth row: it buys the weakest 63-session performers instead of the strongest, on the same dates and universe. It is a counterfactual sign test, not a recommendation.

## 4. Headline performance vs NIFTY 50

| configuration | benchmark | window | n | strategy CAGR | strategy vol | strategy max DD | strategy Sharpe (0 rf) | benchmark CAGR | excess CAGR |
|---|---|---|---|---|---|---|---|---|---|
| N10, no stop (**primary**) | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3276 | 19.45% | 27.65% | -55.30% | 0.782 | 10.77% | 8.68% |
| N10, 10% stop | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3276 | 18.93% | 24.00% | -41.63% | 0.843 | 10.77% | 8.16% |
| N20, no stop | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3276 | 17.92% | 24.62% | -56.71% | 0.794 | 10.77% | 7.15% |
| N20 bottom, no stop (reversal) | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3276 | -7.56% | 25.99% | -91.60% | -0.172 | 10.77% | -18.33% |
| N10, month-end/exact (diagnostic) | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3276 | 22.26% | 27.96% | -55.30% | 0.860 | 10.77% | 11.49% |
| N10, no stop (primary, robustness) | NIFTY 50 (NSE spliced) | 2013-06-03..2025-12-31 | 3098 | 19.60% | 27.82% | -55.30% | 0.784 | 12.74% | 6.87% |
| N10, no stop (primary, robustness) | NIFTY 500 (spliced) | 2013-06-03..2025-12-31 | 3098 | 19.60% | 27.82% | -55.30% | 0.784 | 14.17% | 5.43% |

`excess CAGR` is the geometric annualised strategy return minus the geometric annualised benchmark return over the same aligned window. It is not Jensen alpha; the beta-adjusted intercept is in the next table. The first five rows use the NIFTY 50 (Yahoo) full window; the last two rows are robustness views and are labelled accordingly. The benchmark-aligned strategy CAGR here (19.45%) differs from the full equity-curve CAGR in Section 3 (19.30%) because alignment drops sessions without a benchmark close and starts at the first return date.

### 4.1 Charges actually paid (buy and sell)

Every trade is charged at the fill: brokerage at 2 bps of notional capped at ₹2,000 per order, STT at 10 bps on both sides, stamp duty at 1.5 bps buy-only, exchange charges at 0.3 bps, GST at 18% of brokerage, and slippage at 5 bps on both sides. The engine deducts the total from cash and stores it on the trade. `validate_alpha_output.py` independently recomputes each trade's charge from its notional and side and fails the run if the stored cost differs.

| configuration | buy notional (₹) | sell notional (₹) | buy charges (₹) | sell charges (₹) | total charges (₹) | bps of turnover | charges / initial cash |
|---|---|---|---|---|---|---|---|
| N10, no stop (**primary**) | 4,134,183,545 | 4,139,362,535 | 7,901,819 | 7,277,121 | 15,178,940 | 18.35 | 151.79% |
| N10, 10% stop | 4,161,384,911 | 4,183,456,184 | 7,965,548 | 7,368,315 | 15,333,863 | 18.38 | 153.34% |
| N20, no stop | 3,108,078,346 | 3,109,524,917 | 5,955,078 | 5,491,421 | 11,446,499 | 18.41 | 114.46% |
| N20 bottom, no stop (reversal) | 489,366,195 | 481,153,982 | 937,626 | 849,718 | 1,787,344 | 18.42 | 17.87% |
| N10, month-end/exact (diagnostic) | 2,740,376,295 | 2,760,666,575 | 5,230,777 | 4,844,890 | 10,075,667 | 18.32 | 100.76% |
| N10, zero costs (counterfactual) | 5,196,668,366 | 5,186,675,057 | 0 | 0 | 0 | 0.00 | 0.00% |

Primary charge itemisation:

| charge | ₹ paid |
|---|---|
| brokerage | 1,610,412 |
| gst | 289,874 |
| stt | 8,273,546 |
| stamp | 620,128 |
| exchange | 248,206 |
| slippage | 4,136,773 |

On ₹8,273,546,080 of turnover the primary config paid **₹15,178,940**, or **18.35 bps**. Cumulatively that is **151.79%** of the ₹10,000,000 starting cash. The zero-cost counterfactual finishes at ₹140,444,347 versus ₹100,797,469 net; that is a **3.06% CAGR drag** over the full window. The charge stack is fixed in nominal bps over 2013-2026, so the drag is an estimate of the current schedule, not a reconstruction of each historical rate change.

### 4.2 Maximum drawdown: depth and duration

| configuration | max DD | peak | trough | recovery | peak→trough days | peak→trough sessions | trough→recovery days | trough→recovery sessions | total underwater days | total underwater sessions | longest episode days | longest episode sessions | % sessions underwater |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N10, no stop (**primary**) | -55.30% | 2018-08-28 | 2020-03-24 | 2021-04-06 | 574 | 385 | 378 | 257 | 952 | 642 | 952 | 642 | 92.94% |
| N10, 10% stop | -41.63% | 2018-05-09 | 2019-09-19 | 2020-09-16 | 498 | 336 | 363 | 246 | 861 | 582 | 861 | 582 | 93.00% |
| N20, no stop | -56.71% | 2018-01-08 | 2020-03-24 | 2021-05-10 | 806 | 543 | 412 | 279 | 1,218 | 822 | 1,218 | 822 | 93.00% |
| N20 bottom, no stop (reversal) | -91.60% | 2015-04-16 | 2020-03-25 | — | 1,805 | 1,222 | — | — | 4,185 | 2,839 | 4,185 | 2,839 | 99.39% |
| N10, month-end/exact (diagnostic) | -55.30% | 2018-08-28 | 2020-03-24 | 2021-04-06 | 574 | 385 | 378 | 257 | 952 | 642 | 952 | 642 | 92.73% |

For the primary config the deepest drawdown ran from the **2018-08-28** peak to the **2020-03-24** trough — **574 calendar days / 385 sessions** — and recovered on **2021-04-06**, **378 calendar days / 257 sessions** later. The full underwater span was **952 calendar days / 642 sessions**. The longest drawdown episode, regardless of depth, was **952 calendar days / 642 sessions**. Sessions are equity-curve points, so they count trading sessions; calendar days include weekends and holidays. `% sessions underwater` is the share of sessions below the running high-water mark, not the share of losing sessions; a strategy that makes a new high only briefly can be underwater most of the time while still compounding.

## 5. Alpha and benchmark-relative statistics

| configuration | benchmark | window | n | strat CAGR | bench CAGR | beta | alpha (linear) | alpha (geometric) | tracking error | information ratio | up capture | down capture | R² | t (OLS) | t (NW5) | bootstrap p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| N10, no stop (**primary**) | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3,276 | 19.45% | 10.77% | 0.95 | 10.70% | 11.29% | 23.04% | 0.38 | 102.98% | 91.58% | 0.306 | 1.67 | 1.62 | 0.096 |
| N10, 10% stop | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3,276 | 18.93% | 10.77% | 0.71 | 12.06% | 12.81% | 21.62% | 0.38 | 85.85% | 73.68% | 0.227 | 2.06 | 1.95 | — |
| N20, no stop | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3,276 | 17.92% | 10.77% | 0.96 | 8.48% | 8.85% | 19.15% | 0.37 | 104.14% | 95.32% | 0.396 | 1.60 | 1.54 | — |
| N20 bottom, no stop (reversal) | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3,276 | -7.56% | 10.77% | 0.95 | -15.46% | -14.33% | 20.96% | -0.87 | 96.98% | 115.90% | 0.351 | -2.66 | -2.13 | — |
| N10, month-end/exact (diagnostic) | NIFTY 50 (Yahoo) | 2013-06-03..2026-09-30 | 3,276 | 22.26% | 10.77% | 0.96 | 12.93% | 13.80% | 23.24% | 0.49 | 106.19% | 92.32% | 0.310 | 2.01 | 1.98 | — |
| N10, no stop (primary, robustness) | NIFTY 50 (NSE spliced) | 2013-06-03..2025-12-31 | 3,098 | 19.60% | 12.74% | 0.95 | 9.14% | 9.57% | 23.15% | 0.30 | 102.49% | 92.94% | 0.308 | 1.38 | 1.33 | 0.178 |
| N10, no stop (primary, robustness) | NIFTY 500 (spliced) | 2013-06-03..2025-12-31 | 3,098 | 19.60% | 14.17% | 1.11 | 5.56% | 5.72% | 21.30% | 0.26 | 124.14% | 119.68% | 0.418 | 0.92 | 0.89 | 0.369 |

Read this table carefully:

- `alpha (linear)` is the per-session regression intercept × 252. `alpha (geometric)` is `(1 + intercept)^252 - 1`.
- `information ratio` is the repository's definition: geometric annualised excess return / tracking error, not mean active return / tracking error.
- `up capture` / `down capture` are mean strategy return / mean benchmark return over positive / negative benchmark sessions. Values above 100% mean the strategy amplifies that direction.
- The t-statistics are for the daily regression intercept; Newey-West uses 5 lags. The bootstrap resamples 21-session circular blocks (2,000 replications, fixed seed) and reports a two-sided p-value for a zero intercept.
- **Only the primary × NIFTY 50 (Yahoo) cell is the pre-specified headline.** The NIFTY 50 NSE spliced and NIFTY 500 rows are robustness views; no multiple-testing correction is applied.

### 5.1 Segment view (primary configuration)

| segment | benchmark | aligned window | n | strategy CAGR | benchmark CAGR | excess CAGR | beta | alpha (linear) | information ratio | t (NW5) |
|---|---|---|---|---|---|---|---|---|---|---|
| monthly membership 2013 2022 | NIFTY 50 (Yahoo) | 2013-06-03..2022-03-31 | 2165 | 23.34% | 13.27% | 10.07% | 0.91 | 12.15% | 0.44 | 1.48 |
| monthly membership 2013 2022 | NIFTY 50 (NSE spliced) | 2013-06-03..2022-03-31 | 2167 | 23.32% | 13.26% | 10.06% | 0.91 | 12.10% | 0.44 | 1.47 |
| monthly membership 2013 2022 | NIFTY 500 (spliced) | 2013-06-03..2022-03-31 | 2167 | 23.32% | 14.41% | 8.91% | 1.05 | 9.12% | 0.42 | 1.21 |
| anchor membership 2022 2026 | NIFTY 50 (Yahoo) | 2022-04-01..2026-09-30 | 1111 | 12.22% | 6.04% | 6.18% | 1.09 | 8.03% | 0.26 | 0.72 |
| anchor membership 2022 2026 | NIFTY 50 (NSE spliced) | 2022-04-01..2025-12-31 | 931 | 11.39% | 11.52% | -0.13% | 1.13 | 1.47% | -0.01 | 0.12 |
| anchor membership 2022 2026 | NIFTY 500 (spliced) | 2022-04-01..2025-12-31 | 931 | 11.39% | 13.62% | -2.23% | 1.35 | -3.77% | -0.10 | -0.34 |

The 2013-2022 segment is the clean monthly-membership era. The 2022-2026 segment is the irregular-anchor era; the NIFTY 50 (Yahoo) view covers the full segment through 2026-09-30, while the NSE series end 2025-12-31. The NIFTY 500 rows remain as universe robustness, not the required comparison.

### 5.2 Calendar years (primary configuration)

| year | strategy | NIFTY 50 (Yahoo) | NIFTY 500 (spliced, robustness) | excess vs NIFTY 50 |
|---|---|---|---|---|
| 2013* | 17.29% | 5.31% | 4.99% | 11.98% |
| 2014 | 101.16% | 31.39% | 37.82% | 69.77% |
| 2015 | 6.52% | -4.06% | -0.72% | 10.58% |
| 2016 | -5.57% | 3.01% | 3.84% | -8.58% |
| 2017 | 54.34% | 28.65% | 35.91% | 25.69% |
| 2018 | -17.10% | 3.15% | -3.38% | -20.25% |
| 2019 | -8.51% | 12.02% | 7.66% | -20.53% |
| 2020 | 15.12% | 14.90% | 16.67% | 0.22% |
| 2021 | 77.60% | 24.12% | 30.19% | 53.48% |
| 2022 | 13.08% | 4.33% | 3.02% | 8.75% |
| 2023 | 50.57% | 20.03% | 25.76% | 30.55% |
| 2024 | 15.74% | 8.80% | 15.16% | 6.94% |
| 2025 | -19.32% | 10.51% | 6.69% | -29.83% |
| 2026 | 11.60% | -13.43% | — | 25.03% |

The year marked `*` is partial: 2013 runs from the first fill on 2013-06-03, not from January. 2026 is partial for the NIFTY 50 column (window ends 2026-09-30) and absent for NIFTY 500 (the NSE series ends 2025-12-31). The strategy and benchmark columns are computed from the same aligned NIFTY 50 dates for each year; the NIFTY 500 column is shown only as a robustness reference and is not used in the excess calculation.

### 5.3 Bottom-20 reversal: the opposite selection

| configuration | strategy CAGR | benchmark CAGR | beta | alpha (linear) | tracking error | information ratio | t (OLS) | t (NW5) |
|---|---|---|---|---|---|---|---|---|
| N10, no stop (**primary**) | 19.45% | 10.77% | 0.95 | 10.70% | 23.04% | 0.38 | 1.67 | 1.62 |
| N20 bottom, no stop (reversal) | -7.56% | 10.77% | 0.95 | -15.46% | 20.96% | -0.87 | -2.66 | -2.13 |

Both books are long-only, equal-weight, charge-paying and rebalanced on the same evidence dates. The only difference is the sign of the ranking. If the top-20 alpha were a momentum premium, the bottom-20 portfolio would sit on the losing side of it. On this data the two alphas have **opposite signs, which is what a pure momentum premium predicts**. Against the NIFTY 50 the Newey-West t-statistics are 1.62 for the top book and -2.13 for the bottom book; against the NIFTY 500 the corresponding pair is 0.89 and -2.96, with no multiple-testing adjustment. The bottom book is the stronger test of the data: a reversal portfolio that also earns positive alpha would indicate that the result is a size/liquidity/beta artifact rather than momentum.

## 6. Risk-free sensitivity (primary vs NIFTY 50)

| return frequency / window | risk-free | window | strategy CAGR | benchmark CAGR | beta | alpha (linear) | strategy Sharpe | benchmark Sharpe |
|---|---|---|---|---|---|---|---|---|
| daily, full window | 0 (zero-rate) | 2013-06-03..2026-09-30 | 19.45% | 10.77% | 0.95 | 10.70% | 0.782 | 0.714 |
| daily, G-Sec overlap | 6.49% p.a. | 2015-11-09..2025-12-31 | 14.33% | 12.74% | 0.94 | 4.21% | 0.399 | 0.434 |
| monthly, full window | 0 (zero-rate) | 2013-07-31..2026-09-30 | 19.36% | 10.76% | 1.10 | 9.09% | 0.779 | 0.727 |
| monthly, G-Sec overlap | 6.49% p.a. | 2015-11-30..2025-12-31 | 13.58% | 12.26% | 1.09 | 3.12% | 0.377 | 0.413 |

The zero-rate view is comparable to the archived sweep's `sharpe_zero_rf`. The G-Sec view is the more demanding test: it subtracts a positive risk-free rate from both sides. Because the G-Sec series is a price index with coupons excluded, its rate is a lower bound on the true risk-free rate, so the G-Sec alpha is an **upper bound** on the CAPM alpha under a true cash rate.

## 7. Why the archived 781% sweep is not the alpha answer

| run | calendar | engine behaviour on empty targets | total | CAGR | status |
|---|---|---|---|---|---|
| 144-config sweep (archived) | month-end, exact snapshot only | pre-fix: liquidates on 39 empty-target dates | 781.03% | 17.91% | **not used** — data-gap liquidation |
| post-fix repro (2013-04-18 start) | month-end, exact snapshot only | post-fix: holds on 39 empty-target dates | 1,231.04% | 21.65% | not a rebalance — superseded |
| this run, as-committed | month-end, exact snapshot only | post-fix: holds on empty targets | 1,264.00% | 22.08% | diagnostic comparison only |
| this run, evidence-dated (**used**) | 171 official evidence dates | post-fix, explicit calendar | 907.97% | 19.30% | **headline research estimate** |

The defect is mechanical, not a judgment call: with `target_keys = []`, the pre-fix rebalance loop sold every position not in the empty target set — that is, everything — and then bought nothing. The post-fix engine skips the rebalance and holds. The evidence-dated run replaces the empty-target dates with real evidence dates; among the month-end runs, it is the only one that actually rebalances on a proven universe.

The archived and repro CAGRs include their own pre-deployment cash warm-up; the two "this run" rows start at the first fill. The total-return comparison is therefore directional, not exact. The like-for-like comparison is **as-committed vs evidence-dated**, because both start at the same first fill and differ only in the calendar.

## 8. What this result can and cannot support

**Can support:**

- A reproducible benchmark-relative diagnostic over the full available price history, with every rebalance tied to an official membership date.
- A statement of direction and rough magnitude: the strategy's beta, alpha, tracking error, information ratio and capture on this data.
- A recorded, auditable reason why the archived month-end sweep must not be cited.

**Cannot support:**

- A publishable alpha claim. The continuity and universe validation contracts remain blocked; `ready_for_alpha_claim` is `false`.
- A strict monthly strategy after 2022-03. Membership is point evidence at irregular anchors; dates between anchors are `CoverageUnknown`. The post-2022 result is event-anchored.
- A total-return comparison. Dividends are excluded from both sides; the strategy's omitted dividend yield is a downward bias, but the size of that bias differs from the index's.
- An unbiased terminal-proceeds estimate. `last_close_writeoff` assumes a delisted holding exits at its last observed close. The run reports 1 such writeoff for the primary config. This is optimistic if the true proceeds were zero.
- Capacity, market-impact, circuit-limit, suspension, tax or borrow realism. The engine fills at the next open with integer shares and a fixed cost model; it does not model ADV, impact, or capital-gains tax.
- A tradable reversal strategy. The bottom-20 book is a sign test for the ranking, not a recommendation; it buys falling names without a distress filter, and its concentration and turnover are not modelled for capacity.
- A historical charge schedule. Brokerage, STT, stamp duty, exchange fees, GST and slippage are held at 2026-style constants for the whole 2013-2026 window; actual Indian rates changed several times. SEBI turnover fees, depository-participant charges and capital-gains tax are not modelled at all.
- Intraday or daily-marked tradability. The equity curve is close-marked on NSE sessions.

**Statistical caveats:**

- The alpha t-statistics assume stationary returns and a linear single-factor relationship. Newey-West and the block bootstrap address autocorrelation and non-normality only partially.
- The strategy is concentrated (10 names), so the alpha estimate is sensitive to a small number of holdings and years.
- The post-2022 calendar is endogenous to the evidence: corporate-action anchors can cluster, adding turnover. The report discloses the dates rather than smoothing them.
- The primary benchmark is a single-vendor Yahoo ^NSEI price series. The NSE spliced NIFTY 50 cross-check uses a documented rename chain with continuous boundaries but stops at 2025-12-31.

### 8.1 Expanded universe, holding-period and exit sweep

`FINAL_RESULTS.csv` now holds **968 runs**, sorted by Sharpe: five point-in-time universes (NIFTY 500, MSC400, Midcap 150, Smallcap 250, NIFTY 50), top and bottom selection, 10/20/25 names, six holding periods on the four monthly universes (monthly through 3-year), the 25 exit overlays on monthly rebalances for those universes, the same 25 overlays on NIFTY 50's annual calendar, and the 25 overlays on bottom 20/25 2- and 3-year holds for NIFTY 500. `SWEEP_REPORT.md` narrates the results and flags the comparability traps. The headlines:

- The **primary configuration is unchanged** and is still the pre-specified result. The sweep is a sensitivity map, not a search for a better backtest.
- **NIFTY 750 is absent because the membership chain does not exist.** No synthetic 750-name universe was created.
- **Runs stop at the end of the final scheduled holding period**, not at the last bar date. The 2017-2022 universes therefore do not carry a multi-year buy-and-hold tail: monthly rows end in 2022-04, and 2/3-year rows in mid-2023.
- **Bottom 20/25 long-hold overlays are NIFTY 500-only.** Every monthly universe has the no-overlay 2- and 3-year bottom test; the 25-overlay grid on those holds is run on the headline universe.
- **Every row is price-only.** Dividends are excluded on both sides, so no row is a total-return result.
- **Sharpe is not comparable across rows.** The 2017-2022 universes have 4.75 years of membership evidence and stop when their final scheduled hold completes; NIFTY 50 has 13 annual snapshots; 2- and 3-year holds have few rebalances; and exit proceeds sit in cash until the next rebalance. A high rank can be a short-window or cash-allocation artefact.
- **The top Sharpe is a maximum over 968 trials.** No multiple-testing correction is applied. The ranking is a descriptive map of the grid, not a new strategy claim or a model-selection result.
- **Midcap 150 and Smallcap 250 are subsets of NIFTY 500, and MSC400 overlaps it.** They are segment views, not independent markets.

## 9. Gate status and blockers

From `data/reports/full-history-readiness.json`:

- `ready_for_backtesting`: **false**
- `ready_for_alpha_claim`: **false**
- `overall_validation_gate`: **OPEN** (research preview)
- Blockers: `calendar`, `corporate_actions`, `schemes`, `identity`, `terminal_proceeds`, `dividends`, `execution_capabilities`.

These are not formalities. `dividends` alone changes the level of every total return; `terminal_proceeds` changes the delisting assumption; `schemes` leaves rights/demergers unresolved; `identity` leaves 28 cases unresolved in this run. The alpha numbers above are computed **through** those gaps, not after closing them.

## 10. Reproduction and artifacts

```powershell
$env:PYTHONPATH='src'
.venv\Scripts\python.exe verification\research\alpha-backtest\build_benchmark.py
.venv\Scripts\python.exe verification\research\alpha-backtest\run_alpha_backtest.py
.venv\Scripts\python.exe verification\research\alpha-backtest\compute_alpha.py
.venv\Scripts\python.exe verification\research\alpha-backtest\build_final_csv.py
.venv\Scripts\python.exe verification\research\alpha-backtest\run_sweep.py --universe nifty500 --shard 0 --shards 2 --tag n500a
.venv\Scripts\python.exe verification\research\alpha-backtest\run_sweep.py --universe nifty500 --shard 1 --shards 2 --tag n500b
.venv\Scripts\python.exe verification\research\alpha-backtest\run_sweep.py --universe msc400,midcap150 --tag mid
.venv\Scripts\python.exe verification\research\alpha-backtest\run_sweep.py --universe smallcap250,nifty50 --tag small
.venv\Scripts\python.exe verification\research\alpha-backtest\merge_sweep.py
.venv\Scripts\python.exe verification\research\alpha-backtest\validate_alpha_output.py
.venv\Scripts\python.exe verification\research\alpha-backtest\build_report.py
```

The four sweep workers split the work by universe: NIFTY 500 in two shards and the 2017-2022 universes plus NIFTY 50 in two more. They take roughly 60-100 minutes on this machine and use about 1.2 GB resident each; allow at least 6 GB of free memory before running all four.

| artifact | content |
|---|---|
| `benchmark-series.json` | daily closes, splice evidence, coverage and cross-source validation |
| `BENCHMARK.md` | benchmark evidence narrative |
| `strategy-results.json` | per-config equity curves, metrics, drawdown durations, itemised charges, skip diagnostics, calendar |
| `alpha-results.json` | aligned evaluations, alpha/beta/IR/capture, t-stats, bootstrap, segments, calendar years |
| `FINAL_RESULTS.csv` | **all 968 runs**, one row each, sorted by Sharpe: universes, holds, directions, stops, targets, combinations, drawdown days, charges, flags |
| `FLAGSHIP_RESULTS.csv` | the six headline configurations in the same wide schema |
| `FLAGSHIP_EVIDENCE.csv` | legacy widest evidence view for the six headline rows (extra benchmark cross-checks and rebalance diagnostics) |
| `sweep-shard-*.jsonl` | raw sweep output, one JSON object per run, written incrementally |
| `SWEEP_REPORT.md` | sweep narrative, top-Sharpe table, universe/exit/hold comparisons and data-gap explanations |
| `validation.json` | 348 fail-closed checks, including an independent OLS recomputation, independent drawdown-duration recomputation and sweep-CSV identities |
| `REPORT.md` | this report |

Research use only. Nothing here is investment advice, a solicitation, or a claim of realised returns.
