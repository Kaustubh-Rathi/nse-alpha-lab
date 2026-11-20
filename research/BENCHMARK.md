# Benchmark daily-close evidence: NIFTY 50, NIFTY 500, 10Y G-Sec

Generated: `2026-11-18T19:26:45Z`

Script: `verification/research/alpha-backtest/build_benchmark.py`

## Sources

- `data/raw/nseIndices.zip`: 3,419 daily `ind_close_all_DDMMYYYY.csv` files, filename dates 2012-02-21..2025-12-31; latest `Index Date` in any row 2025-12-31; 0 rows dated 2026 or later.
- `data/raw/yahoo/idx_NSEI_max_1d.csv`: Yahoo ^NSEI daily bars, 2007-09-17..2026-11-08; `Close` used as the index level (not `Adj Close`).
- No network access was used.

## Coverage

| series | source | rows | first date | last date | covers 2013-04-18..2026-09-30? |
|---|---|---:|---|---|---|
| NIFTY50_NSE | NSE, exact `Nifty 50` | 2,508 | 2015-11-09 | 2025-12-31 | No — starts 2015-11-09, ends 2025-12-31 |
| NIFTY500_NSE | NSE, exact `Nifty 500` | 2,508 | 2015-11-09 | 2025-12-31 | No — starts 2015-11-09, ends 2025-12-31 |
| G_SEC_10Y_NSE | NSE, exact `Nifty 10 yr Benchmark G-Sec` | 2,391 | 2015-11-09 | 2025-12-31 | No — starts 2015-11-09, ends 2025-12-31 |
| NIFTY50_YAHOO | Yahoo ^NSEI `Close` | 4,697 | 2007-09-17 | 2026-11-08 | Yes |

Full-window series: **NIFTY50_YAHOO**. Partial-window series: **NIFTY50_NSE, NIFTY500_NSE, G_SEC_10Y_NSE, NIFTY500_SPLICED_NSE, NIFTY50_SPLICED_NSE**.

## Spliced coverage (documented renames)

| series | rename chain | rows | first date | last date | covers 2013-04-18..2025-12-31? | covers 2026? |
|---|---|---:|---|---|---|---|
| NIFTY500_SPLICED_NSE | S&P CNX 500 -> CNX 500 -> Nifty 500 | 3,419 | 2012-02-21 | 2025-12-31 | Yes | No — ends 2025-12-31 |
| NIFTY50_SPLICED_NSE | S&P CNX Nifty -> CNX Nifty -> Nifty 50 | 3,419 | 2012-02-21 | 2025-12-31 | Yes | No — ends 2025-12-31 |

These are documented index renames (the same index, renamed), not aliases inferred from similarity. The spliced NIFTY 500 ends 2025-12-31 and therefore does not cover 2026. The unspliced source series remain in `series` unchanged alongside the spliced series.

## Splice evidence

| series | boundary | old last date / close | new first date / close | implied move | old-era daily sigma | old-era max abs | z-score | abs percentile | decision |
|---|---:|---|---|---:|---:|---:|---:|---:|---|
| NIFTY500_SPLICED_NSE | 1 (S&P CNX 500 -> CNX 500) | 2013-02-07 / 4,713.25 | 2013-02-08 / 4,682.55 | -0.6514% | 0.8687% | 2.7891% | -0.75 | 60.9% | concatenate |
| NIFTY500_SPLICED_NSE | 2 (CNX 500 -> Nifty 500) | 2015-11-06 / 6,650.10 | 2015-11-09 / 6,641.40 | -0.1308% | 1.0189% | 6.7110% | -0.13 | 12.6% | concatenate |
| NIFTY50_SPLICED_NSE | 1 (S&P CNX Nifty -> CNX Nifty) | 2013-02-07 / 5,938.80 | 2013-02-08 / 5,903.50 | -0.5944% | 0.8959% | 2.7512% | -0.66 | 56.8% | concatenate |
| NIFTY50_SPLICED_NSE | 2 (CNX Nifty -> Nifty 50) | 2015-11-06 / 7,954.30 | 2015-11-09 / 7,915.20 | -0.4916% | 1.0340% | 5.9151% | -0.48 | 46.4% | concatenate |

No boundary exceeded the 10% hard limit and every implied move was inside the old-era daily-return distribution, so every boundary used `concatenate` with no adjustment factor. The spliced series are level-continuous by construction because the index level was not rescaled.

## NIFTY 50 cross-source check

Date-aligned NSE `Nifty 50` vs Yahoo ^NSEI `Close`: **2,497** overlapping sessions (2015-11-09..2025-12-31).

| metric | value |
|---|---:|
| mean absolute difference | 0.000322 |
| max absolute difference | 0.000781 (2025-12-26) |
| max relative difference | 4.768e-06% (2016-12-01) |
| relative differences > 0.1% | 0 |
| closes equal at 2 decimal places | 2,497 |

**Result: agree.** The sources agree: the largest relative difference is far below 0.1%, and the maximum absolute difference is smaller than two-decimal rounding noise. The two series were compared, not averaged. The comparison covers only the NSE exact-name span, so it does not independently validate Yahoo before 2015-11-09.

## Data quirks and gaps

- **Exact-name NSE coverage starts 2015-11-09.** Earlier rows use the old index names; the NSE export contains the documented rename chains `S&P CNX Nifty` -> `CNX Nifty` -> `Nifty 50` and `S&P CNX 500` -> `CNX 500` -> `Nifty 500`. The unspliced series stay exact-name only; the separate `*_SPLICED_NSE` series apply those documented renames with the boundary evidence below.
- **No 2026 data in the NSE zip.** The `2025` folder contains 2025-01-01..2025-12-31 (249 files) and is the latest folder; the last filename date and the latest row date are both 2025-12-31. Both spliced series therefore end on 2025-12-31.
- **G-Sec is sparse.** `G_SEC_10Y_NSE` has 2,391 rows, 117 fewer sessions than the Nifty 50 calendar, with 17 calendar gaps > 5 days (largest 10 days).
- **Weekend rows are retained and reported.** The NSE exact series each contain 5 weekend rows: 2023-11-12 (Sun), 2024-01-20 (Sat), 2024-03-02 (Sat), 2024-05-18 (Sat), 2025-02-01 (Sat). Yahoo contains 2 weekend rows: 2025-02-01 (Sat), 2026-11-08 (Sun); the 2026-11-08 bar is outside the strategy window.
- **NSE/Yahoo session sets differ.** 11 NSE dates are absent from Yahoo (including special/weekend sessions) and 2 Yahoo dates inside the NSE span are absent from the zip (`2015-12-01`, `2016-06-20`). Value statistics use only shared dates.
- **Row-date format quirk, not a date swap.** 3 files (318 index rows) in April 2023 carry `Index Date` in `MM-DD-YYYY` order: `06042023`, `10042023`, `11042023`. The Nifty 50 close in each file matches Yahoo on the filename date, so the filename remains authoritative; no day/month-swapped filename was detected. Details are in the JSON.
- **Yahoo final bar is a Sunday.** The last Yahoo row is 2026-11-08 (Sunday), outside the strategy window; it is retained for transparency but is not used by a window ending 2026-09-30.

### Per-series validation

| series | rows | duplicate dates | non-Mon-Fri rows | non-positive closes | gaps > 5 days |
|---|---:|---:|---:|---:|---:|
| NIFTY50_NSE | 2,508 | 0 (none) | 5 (listed above) | 0 (none) | 0 (none) |
| NIFTY500_NSE | 2,508 | 0 (none) | 5 (listed above) | 0 (none) | 0 (none) |
| G_SEC_10Y_NSE | 2,391 | 0 (none) | 5 (listed above) | 0 (none) | 17 (max 10 days) |
| NIFTY50_YAHOO | 4,697 | 0 (none) | 2 (listed above) | 0 (none) | 1 (max 6 days) |
| NIFTY500_SPLICED_NSE | 3,419 | 0 (none) | 11 (listed above) | 0 (none) | 1 (max 6 days) |
| NIFTY50_SPLICED_NSE | 3,419 | 0 (none) | 11 (listed above) | 0 (none) | 1 (max 6 days) |

Top calendar gaps > 5 days (up to 10 per series):
- NIFTY50_NSE: none.
- NIFTY500_NSE: none.
- G_SEC_10Y_NSE (17 gaps): 2025-09-08..2025-09-18 (10d), 2025-09-26..2025-10-06 (10d), 2025-10-06..2025-10-16 (10d), 2025-10-21..2025-10-31 (10d), 2025-12-19..2025-12-29 (10d), 2022-06-30..2022-07-08 (8d), 2022-11-28..2022-12-06 (8d), 2025-08-26..2025-09-03 (8d), 2025-12-11..2025-12-19 (8d), 2022-09-26..2022-10-03 (7d).
- NIFTY50_YAHOO (1 gaps): 2014-10-01..2014-10-07 (6d).
- NIFTY500_SPLICED_NSE (1 gaps): 2014-10-01..2014-10-07 (6d).
- NIFTY50_SPLICED_NSE (1 gaps): 2014-10-01..2014-10-07 (6d).

### Nifty 50 / Nifty 500 name histories

| name | rows | first date | last date |
|---|---:|---|---|
| `S&P CNX Nifty` | 244 | 2012-02-21 | 2013-02-07 |
| `CNX Nifty` | 667 | 2013-02-08 | 2015-11-06 |
| `Nifty 50` | 2,508 | 2015-11-09 | 2025-12-31 |
| `S&P CNX 500` | 244 | 2012-02-21 | 2013-02-07 |
| `CNX 500` | 667 | 2013-02-08 | 2015-11-06 |
| `Nifty 500` | 2,508 | 2015-11-09 | 2025-12-31 |

Broad 500-family names: `CNX 500` 2013-02-08..2015-11-06 (667 rows); `Nifty 500` 2015-11-09..2025-12-31 (2,508 rows); `S&P CNX 500` 2012-02-21..2013-02-07 (244 rows).
The other 16 names containing `500` are thematic/derived indices (for example `Nifty500 Shariah`, `NIFTY500 Value 50`, `Nifty500 Momentum 50`); their full coverage is enumerated in the JSON `nse_alias_scan`.

## What this series can and cannot support

`NIFTY50_YAHOO` is the **primary benchmark** for the alpha report: it is the only series that spans the full strategy window 2013-04-18..2026-09-30, and it supports daily close-to-close returns, excess returns, and alpha/beta over that window. It agrees with the exact-name NSE `Nifty 50` series on all 2,497 overlapping sessions to within 0.000781 index points, but it cannot provide intraday data, total-return/dividend-adjusted returns, or independent validation before 2015-11-09. `NIFTY50_SPLICED_NSE` is the NSE cross-check and `NIFTY500_SPLICED_NSE` is the universe robustness benchmark: both cover 2012-02-21..2025-12-31, both use the documented renames with normal boundary moves concatenated unchanged, and both stop at 2025-12-31, so neither covers 2026-01-01..2026-09-30; the NSE zip has no 2026 data. The unspliced exact-name series start 2015-11-09 and end 2025-12-31, and `G_SEC_10Y_NSE` additionally has 117 missing sessions. Do not average the NSE and Yahoo sources and do not rescale the spliced levels.

## Reproduce

```powershell
.venv\Scripts\python.exe verification\research\alpha-backtest\build_benchmark.py
```
