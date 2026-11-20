# Expanded momentum sweep: universes, holds, stops and profit cut-offs

Research diagnostics only. The internal validation gate is still **open**; these are not published returns and the Sharpe ranking is **not** a model-selection result.

## What was run

- **968 configurations**, 968 publishable, 0 refused.
- Families: bottom_hold_exit (96), counterfactual_zero_cost (1), exit_grid_monthly (576), nifty50_exit_annual (144), superseded_diagnostic (1), universe_core (150).
- Universes: midcap150 (180), msc400 (180), nifty50 (150), nifty500 (278), smallcap250 (180).
- Cadences: 2y (72), 3y (72), annual (174), monthly (602), quarterly (24), semiannual (24).
- Exits: no overlay, 5/10/15/20 stop-loss, 5/10/15/20 take-profit and all 16 stop x target combinations. The full grid is run on the four monthly universes; NIFTY 50 gets the same grid on its annual rebalance calendar.
- Bottom 20/25 on 2- and 3-year holds is run on every monthly universe with no overlay, and with every exit overlay on NIFTY 500.
- Every evidence-dated rebalance is an exact membership date; the one superseded month-end diagnostic is the exception. Quarterly and longer holdings take the first evidence date at or after each 3-, 6-, 12-, 24- or 36-month boundary, so the first rebalance is the universe's first date and no early years are skipped.
- **Each run ends when its final scheduled hold completes**, not at the last bar date. Monthly 2017-2022 rows end in 2022-04; 2- and 3-year rows end in mid-2023. `last_signal_date` and `scheduled_horizon_end` in the CSV show the exact boundary.

## Read this before the ranking

- **The full table is sorted by Sharpe**, as requested. Sharpe is not comparable across windows: a mid/small-cap run built on 4.75 years of membership evidence (extended only long enough to finish its final hold) and a 13-year NIFTY 500 run are different samples.
- **The top Sharpe is a maximum over 968 trials.** No multiple-testing correction is applied; the ranking is a descriptive grid summary, not a new strategy claim.
- **Short windows and few rebalances are flagged.** A 3-year hold on a universe with only 4.75 years of membership evidence has two or three rebalances; a 3-year hold on the full window has five. Those rows are diagnostics, not strategies.
- **Stop/target proceeds sit in cash until the next scheduled rebalance.** With a 2- or 3-year hold, a 5% target can leave most of the book in cash for years; on the NIFTY 50 annual calendar it can sit for up to a year. A high Sharpe there is often a cash-allocation artefact.
- **Midcap 150 and Smallcap 250 are subsets of NIFTY 500.** MSC400 overlaps the same market. They are segment views, not independent universes.
- **NIFTY 50 is annual-snapshot only.** It has 13 rebalances and is a large-cap comparison, not a monthly index strategy.
- **No NIFTY 750.** The repo has no point-in-time NIFTY Total Market / Microcap 250 membership chain. NIFTY 500 plus Midcap 150 plus Smallcap 250 is still NIFTY 500, not 750 names. A 750-name test needs new constituent data and is not fabricated here.
- **No dividend adjustment.** Prices are split/bonus-adjusted only. The benchmark is a price index. A clean total-return alpha needs a verified NIFTY 50 total-return series, which is not on disk.

## Why there is no NIFTY 750 row

A 750-name universe needs a point-in-time constituent chain for the NIFTY Total Market (or NIFTY 500 plus Microcap 250). This repository has:

- NIFTY 500: 171 exact snapshots (107 monthly official sets through 2022-03, then 64 irregular anchors);
- NIFTY MidSmallcap 400, Midcap 150 and Smallcap 250: 57 monthly official sets each, 2017-07..2022-03;
- NIFTY 50: 13 annual exact snapshots, 2013-10..2025-10.

There is no Microcap 250 or Total Market membership file, and no point-in-time chain for the 250 names outside NIFTY 500. Midcap 150 and Smallcap 250 are already subsets of NIFTY 500, so their union is still the 500-name universe, not 750. Inventing the missing 250 from today's constituents would inject survivorship and look-ahead bias, so no such row is produced.

## Why the prices are not dividend-adjusted

The prepared price series adjusts for splits and bonuses only. Dividends are recorded in the corporate-actions table with ex-dates and amounts, but they are **not** subtracted from the historical price series and not credited as cash. Producing a total-return series would require, for every ex-date, the shares held, the dividend per share, withholding-tax treatment and reinvestment convention. The pipeline does not define that calculation, so applying dividends only to the strategy would mix bases.

The benchmarks are also price indices (Yahoo `^NSEI`, NSE NIFTY 500). Comparing strategy price returns with a price index is internally consistent on the benchmark axis, but both sides understate total return. The sign of the net alpha bias depends on the strategy's dividend yield relative to NIFTY 50, which is not verified here. A defensible total-return alpha needs a verified NIFTY 50 total-return series and a dividend-reinvestment rule; neither is available in this repository, so the headline stays price-vs-price and every row says so.

## Top 25 by Sharpe

| rank | configuration | universe | hold | exit | years | CAGR | Sharpe | max DD | alpha vs N50 | t NW5 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `smallcap250_top_N25_L63_monthly_sl15_tp10` | smallcap250 | monthly | sl15_tp10 | 4.66 | 19.33% | 1.3362 | -26.99% | 13.96% | 2.2354 |
| 2 | `smallcap250_top_N25_L63_monthly_sl15_tp20` | smallcap250 | monthly | sl15_tp20 | 4.66 | 23.22% | 1.3269 | -31.02% | 16.04% | 2.2498 |
| 3 | `smallcap250_top_N25_L63_monthly_sl15_tp15` | smallcap250 | monthly | sl15_tp15 | 4.66 | 21.25% | 1.3159 | -27.48% | 14.96% | 2.1879 |
| 4 | `smallcap250_top_N20_L63_monthly_sl15_tp15` | smallcap250 | monthly | sl15_tp15 | 4.66 | 20.85% | 1.2856 | -30.07% | 14.88% | 2.0967 |
| 5 | `midcap150_top_N20_L63_semiannual_none` | midcap150 | semiannual | none | 4.92 | 30.16% | 1.2737 | -38.34% | 18.23% | 2.4926 |
| 6 | `smallcap250_top_N25_L63_monthly_sl15_tp05` | smallcap250 | monthly | sl15_tp05 | 4.66 | 15.04% | 1.2661 | -23.66% | 11.31% | 2.1241 |
| 7 | `smallcap250_top_N20_L63_monthly_sl15_tp20` | smallcap250 | monthly | sl15_tp20 | 4.66 | 22.37% | 1.2644 | -34.29% | 15.46% | 2.0876 |
| 8 | `smallcap250_top_N25_L63_monthly_sl10_tp20` | smallcap250 | monthly | sl10_tp20 | 4.66 | 19.48% | 1.2606 | -30.18% | 13.70% | 2.0286 |
| 9 | `smallcap250_top_N25_L63_monthly_sl10_tp10` | smallcap250 | monthly | sl10_tp10 | 4.66 | 15.85% | 1.2525 | -25.90% | 11.68% | 1.9864 |
| 10 | `msc400_top_N20_L63_monthly_sl15_tp10` | msc400 | monthly | sl15_tp10 | 4.66 | 18.34% | 1.2521 | -27.79% | 13.26% | 2.1035 |
| 11 | `midcap150_top_N25_L63_semiannual_none` | midcap150 | semiannual | none | 4.92 | 28.11% | 1.2496 | -37.59% | 16.38% | 2.4674 |
| 12 | `smallcap250_top_N25_L63_monthly_sl10_tp15` | smallcap250 | monthly | sl10_tp15 | 4.66 | 17.71% | 1.2491 | -26.53% | 12.68% | 1.9759 |
| 13 | `smallcap250_top_N20_L63_monthly_sl15_tp10` | smallcap250 | monthly | sl15_tp10 | 4.66 | 18.06% | 1.2373 | -29.00% | 13.00% | 2.0171 |
| 14 | `msc400_top_N25_L63_monthly_sl15_tp10` | msc400 | monthly | sl15_tp10 | 4.66 | 17.46% | 1.2356 | -26.35% | 12.36% | 2.0369 |
| 15 | `smallcap250_top_N25_L63_monthly_sl20_tp05` | smallcap250 | monthly | sl20_tp05 | 4.66 | 15.12% | 1.2009 | -23.65% | 11.03% | 1.9750 |
| 16 | `midcap150_top_N20_L63_quarterly_none` | midcap150 | quarterly | none | 4.66 | 26.19% | 1.2000 | -38.74% | 15.26% | 2.0056 |
| 17 | `smallcap250_top_N25_L63_monthly_sl20_tp10` | smallcap250 | monthly | sl20_tp10 | 4.66 | 18.22% | 1.1993 | -28.61% | 12.57% | 1.9297 |
| 18 | `msc400_top_N25_L63_monthly_sl15_tp20` | msc400 | monthly | sl15_tp20 | 4.66 | 20.26% | 1.1971 | -31.68% | 13.55% | 1.9737 |
| 19 | `msc400_top_N20_L63_monthly_sl20_tp10` | msc400 | monthly | sl20_tp10 | 4.66 | 18.46% | 1.1959 | -26.22% | 12.83% | 1.9753 |
| 20 | `smallcap250_top_N25_L63_monthly_sl10_tp05` | smallcap250 | monthly | sl10_tp05 | 4.66 | 12.39% | 1.1934 | -22.90% | 9.47% | 1.9358 |
| 21 | `msc400_top_N10_L63_monthly_sl20_tp05` | msc400 | monthly | sl20_tp05 | 4.66 | 17.40% | 1.1843 | -20.65% | 13.42% | 2.0351 |
| 22 | `smallcap250_top_N20_L63_monthly_sl20_tp15` | smallcap250 | monthly | sl20_tp15 | 4.66 | 20.21% | 1.1799 | -31.85% | 13.83% | 1.8674 |
| 23 | `smallcap250_top_N25_L63_monthly_sl20_tp20` | smallcap250 | monthly | sl20_tp20 | 4.66 | 21.48% | 1.1762 | -35.63% | 14.12% | 1.8932 |
| 24 | `msc400_top_N25_L63_monthly_sl15_tp15` | msc400 | monthly | sl15_tp15 | 4.66 | 18.31% | 1.1702 | -29.80% | 12.49% | 1.8796 |
| 25 | `msc400_top_N25_L63_monthly_sl20_tp10` | msc400 | monthly | sl20_tp10 | 4.66 | 17.47% | 1.1689 | -24.75% | 11.87% | 1.9068 |

The table starts with short-window and cash-heavy rows. Check the `years`, `flag_short_window`, `flag_few_rebalances` and `notes` columns in `FINAL_RESULTS.csv` before reading anything into the order.

## Universe comparison: top 10, monthly, no exit

| configuration | universe | start | end | CAGR | Sharpe | max DD | alpha vs N50 | t NW5 |
|---|---|---|---|---|---|---|---|---|
| `nifty500_top_N10_L63_monthly_none` | NIFTY 500 exact-date membership | 2013-05-31 | 2026-09-30 | 19.30% | 0.7782 | -55.30% | 10.70% | 1.6166 |
| `msc400_top_N10_L63_monthly_none` | NIFTY_MidSmallcap_400 | 2017-07-31 | 2022-04-29 | 16.04% | 0.6996 | -46.97% | 8.75% | 0.7969 |
| `midcap150_top_N10_L63_monthly_none` | NIFTY_Midcap_150 | 2017-07-31 | 2022-04-29 | 14.05% | 0.6644 | -43.71% | 5.30% | 0.5657 |
| `smallcap250_top_N10_L63_monthly_none` | NIFTY_Smallcap_250 | 2017-07-31 | 2022-04-29 | 9.11% | 0.4535 | -53.19% | 2.43% | 0.2024 |
| `nifty50_top_N10_L63_annual_none` | NIFTY 50 annual exact snapshots | 2013-10-31 | 2026-09-30 | 9.05% | 0.5630 | -44.93% | -0.64% | -0.2201 |

NIFTY 50 is annual-snapshot only and covers 2013-2025. The other three are built from membership evidence covering 2017-07..2022-03; each run stops when its final scheduled hold completes (2022-04 for monthly, mid-2023 for 2/3-year), so their CAGRs and Sharpe ratios are not a like-for-like extension of the NIFTY 500 row.

## Stop-loss grid: NIFTY 500, top 10, monthly, no target

| configuration | stop | CAGR | Sharpe | max DD | alpha vs N50 | t NW5 | trades |
|---|---|---|---|---|---|---|---|
| `nifty500_top_N10_L63_monthly_none` |  | 19.30% | 0.7782 | -55.30% | 10.70% | 1.6166 | 2080 |
| `nifty500_top_N10_L63_monthly_sl05` | 5.00% | 17.52% | 0.8594 | -40.74% | 11.93% | 2.0721 | 2358 |
| `nifty500_top_N10_L63_monthly_sl10` | 10.00% | 18.78% | 0.8385 | -41.63% | 12.06% | 1.9527 | 2182 |
| `nifty500_top_N10_L63_monthly_sl15` | 15.00% | 17.88% | 0.7742 | -46.99% | 10.69% | 1.7058 | 2121 |
| `nifty500_top_N10_L63_monthly_sl20` | 20.00% | 18.08% | 0.7621 | -48.80% | 10.54% | 1.6346 | 2093 |

## Take-profit grid: NIFTY 500, top 10, monthly, no stop

| configuration | target | CAGR | Sharpe | max DD | alpha vs N50 | t NW5 | trades |
|---|---|---|---|---|---|---|---|
| `nifty500_top_N10_L63_monthly_none` |  | 19.30% | 0.7782 | -55.30% | 10.70% | 1.6166 | 2080 |
| `nifty500_top_N10_L63_monthly_tp05` | 5.00% | 9.62% | 0.5908 | -42.46% | 4.33% | 0.9351 | 2967 |
| `nifty500_top_N10_L63_monthly_tp10` | 10.00% | 14.72% | 0.7655 | -42.99% | 8.03% | 1.5844 | 2758 |
| `nifty500_top_N10_L63_monthly_tp15` | 15.00% | 13.67% | 0.6755 | -55.71% | 6.33% | 1.1480 | 2575 |
| `nifty500_top_N10_L63_monthly_tp20` | 20.00% | 15.57% | 0.7248 | -58.22% | 7.77% | 1.3415 | 2483 |

## Best stop x target combinations: NIFTY 500, top 10, monthly

| configuration | CAGR | Sharpe | max DD | alpha vs N50 | t NW5 | trades |
|---|---|---|---|---|---|---|
| `nifty500_top_N10_L63_monthly_sl05_tp20` | 12.31% | 0.7946 | -41.10% | 8.34% | 1.8535 | 2794 |
| `nifty500_top_N10_L63_monthly_sl20_tp10` | 13.84% | 0.7660 | -39.57% | 8.05% | 1.6489 | 2786 |
| `nifty500_top_N10_L63_monthly_sl10_tp10` | 11.81% | 0.7568 | -37.80% | 7.50% | 1.6882 | 2890 |
| `nifty500_top_N10_L63_monthly_sl10_tp20` | 13.61% | 0.7550 | -41.79% | 8.24% | 1.6090 | 2626 |
| `nifty500_top_N10_L63_monthly_sl05_tp10` | 9.40% | 0.7272 | -42.36% | 6.49% | 1.6504 | 3026 |
| `nifty500_top_N10_L63_monthly_sl05_tp15` | 10.36% | 0.7251 | -38.84% | 6.85% | 1.6087 | 2878 |
| `nifty500_top_N10_L63_monthly_sl15_tp10` | 12.04% | 0.7166 | -39.88% | 6.99% | 1.4997 | 2828 |
| `nifty500_top_N10_L63_monthly_sl10_tp15` | 11.94% | 0.7108 | -36.12% | 7.02% | 1.4667 | 2719 |
| `nifty500_top_N10_L63_monthly_sl20_tp20` | 14.35% | 0.7102 | -51.88% | 7.65% | 1.3695 | 2506 |
| `nifty500_top_N10_L63_monthly_sl15_tp20` | 12.81% | 0.6778 | -50.13% | 6.76% | 1.2681 | 2557 |

## Bottom 20/25 on 2- and 3-year holds

| configuration | universe | rebalances | CAGR | Sharpe | max DD | alpha vs N50 | t NW5 | trades |
|---|---|---|---|---|---|---|---|---|
| `midcap150_bottom_N20_L63_2y_none` | midcap150 | 3 | 6.75% | 0.4105 | -61.11% | -2.50% | -0.3421 | 59 |
| `midcap150_bottom_N20_L63_3y_none` | midcap150 | 2 | 9.97% | 0.6181 | -38.22% | 2.07% | 0.3580 | 58 |
| `midcap150_bottom_N25_L63_2y_none` | midcap150 | 3 | 5.83% | 0.3767 | -61.26% | -3.43% | -0.5078 | 74 |
| `midcap150_bottom_N25_L63_3y_none` | midcap150 | 2 | 9.21% | 0.5909 | -39.62% | 1.10% | 0.2011 | 71 |
| `msc400_bottom_N20_L63_2y_none` | msc400 | 3 | 1.96% | 0.2020 | -83.35% | -4.18% | -0.3570 | 65 |
| `msc400_bottom_N20_L63_3y_none` | msc400 | 2 | -7.14% | -0.2478 | -70.02% | -14.35% | -1.8061 | 56 |
| `msc400_bottom_N25_L63_2y_none` | msc400 | 3 | 2.89% | 0.2386 | -80.58% | -4.11% | -0.3781 | 80 |
| `msc400_bottom_N25_L63_3y_none` | msc400 | 2 | 4.91% | 0.3389 | -58.80% | -2.18% | -0.2909 | 72 |
| `nifty500_bottom_N20_L63_2y_none` | nifty500 | 7 | -0.84% | 0.0931 | -80.35% | -8.79% | -1.3751 | 254 |
| `nifty500_bottom_N20_L63_3y_none` | nifty500 | 5 | -0.44% | 0.1118 | -84.61% | -8.24% | -1.2255 | 178 |
| `nifty500_bottom_N25_L63_2y_none` | nifty500 | 7 | 0.54% | 0.1441 | -77.90% | -7.61% | -1.2647 | 318 |
| `nifty500_bottom_N25_L63_3y_none` | nifty500 | 5 | 1.80% | 0.1979 | -79.91% | -6.42% | -1.0108 | 222 |
| `smallcap250_bottom_N20_L63_2y_none` | smallcap250 | 3 | 1.48% | 0.1826 | -83.40% | -5.15% | -0.4609 | 63 |
| `smallcap250_bottom_N20_L63_3y_none` | smallcap250 | 2 | -0.73% | 0.0805 | -72.10% | -7.58% | -0.8705 | 56 |
| `smallcap250_bottom_N25_L63_2y_none` | smallcap250 | 3 | 3.13% | 0.2489 | -80.96% | -4.40% | -0.4464 | 78 |
| `smallcap250_bottom_N25_L63_3y_none` | smallcap250 | 2 | 1.89% | 0.1958 | -72.04% | -5.25% | -0.6225 | 72 |

Every monthly universe therefore has a bottom 20/25 2- and 3-year result. The 2-year/3-year overlay grid is run on NIFTY 500, which is the headline universe; the other universes carry the no-overlay core rows so the holding-period comparison is not NIFTY 500-only. The best-Sharpe bottom 20/25 2- or 3-year row is not automatically the best reversal strategy. With only a handful of rebalances (five to seven on NIFTY 500; two to three on the 2017-2022 universes), the result is a few decisions, not a durable monthly signal. Here are the best NIFTY 500 overlay rows in that family:

| configuration | rebalances | CAGR | Sharpe | max DD | alpha vs N50 | t NW5 | trades |
|---|---|---|---|---|---|---|---|
| `nifty500_bottom_N20_L63_2y_tp20` | 7 | 1.91% | 0.2070 | -62.17% | -1.72% | -0.4057 | 268 |
| `nifty500_bottom_N25_L63_2y_tp20` | 7 | 1.67% | 0.1922 | -59.15% | -2.08% | -0.5119 | 333 |
| `nifty500_bottom_N20_L63_2y_tp15` | 7 | 1.06% | 0.1465 | -58.61% | -2.29% | -0.5672 | 269 |
| `nifty500_bottom_N25_L63_2y_tp10` | 7 | 0.90% | 0.1393 | -49.09% | -1.57% | -0.4713 | 340 |
| `nifty500_bottom_N20_L63_2y_tp10` | 7 | 0.71% | 0.1198 | -52.04% | -1.62% | -0.4504 | 273 |
| `nifty500_bottom_N25_L63_2y_tp15` | 7 | 0.72% | 0.1196 | -56.60% | -2.77% | -0.7150 | 335 |
| `nifty500_bottom_N25_L63_2y_sl05_tp20` | 7 | 0.31% | 0.1144 | -15.01% | 0.11% | 0.0970 | 350 |
| `nifty500_bottom_N25_L63_2y_sl05_tp10` | 7 | 0.22% | 0.0961 | -15.77% | 0.10% | 0.1016 | 350 |
| `nifty500_bottom_N20_L63_3y_tp20` | 5 | -0.11% | 0.0595 | -65.18% | -3.39% | -0.7818 | 192 |
| `nifty500_bottom_N20_L63_3y_tp15` | 5 | -0.17% | 0.0501 | -61.08% | -3.13% | -0.7567 | 193 |

Those rows are cash-heavy by construction: a 5% target on a 3-year hold can leave most of the book in cash for years. `flag_exit_cash_until_rebalance` marks every such row in the CSV.

## Flagship rows in the full table

| configuration | start | end | total return | CAGR | Sharpe | max DD | alpha vs N50 | t NW5 | charges |
|---|---|---|---|---|---|---|---|---|---|
| `nifty500_top_N10_L63_monthly_none` | 2013-05-31 | 2026-09-30 | 907.97% | 19.30% | 0.7782 | -55.30% | 10.70% | 1.6166 | 15,178,940 |
| `nifty500_top_N10_L63_monthly_sl10` | 2013-05-31 | 2026-09-30 | 852.24% | 18.78% | 0.8385 | -41.63% | 12.06% | 1.9527 | 15,333,863 |
| `nifty500_top_N20_L63_monthly_none` | 2013-05-31 | 2026-09-30 | 752.18% | 17.78% | 0.7901 | -56.71% | 8.48% | 1.5373 | 11,446,499 |
| `nifty500_bottom_N20_L63_monthly_none` | 2013-05-31 | 2026-09-30 | -64.01% | -7.51% | -0.1705 | -91.60% | -15.46% | -2.1305 | 1,787,344 |
| `nifty500_top_N10_L63_monthly_none_zero_cost` | 2013-05-31 | 2026-09-30 | 1304.44% | 22.36% | 0.8696 | -53.31% | 13.23% | 1.9980 | 0 |
| `nifty500_top_N10_L63_monthly_none_month_end_exact` | 2013-05-31 | 2026-09-30 | 1264.00% | 22.08% | 0.8560 | -55.30% | 12.93% | 1.9824 | 10,075,667 |

The primary is still `nifty500_top_N10_L63_monthly_none`. Its alpha is economically large but not statistically significant at 5% (t 1.62 Newey-West, bootstrap p 0.096). Nothing in this sweep changes that pre-specified result.

## Files

- `FINAL_RESULTS.csv` - all rows, sorted by Sharpe, with ranks and flags.
- `FLAGSHIP_RESULTS.csv` - the six headline rows only.
- `sweep-shard-*.jsonl` - raw shard output, one JSON object per run.
- `SWEEP_REPORT.md` - this report.

## Data limits

- NIFTY 750 / NIFTY Total Market: no point-in-time membership chain on disk.
- Dividends: not applied; no verified total-return benchmark.
- Pre-2013: outside the backtest window.
- Post-2022 NIFTY 500: irregular evidence anchors, not strict monthly.
- Delisting: `last_close_writeoff`; terminal proceeds are unknown.
- Benchmark: NIFTY 50 price index (Yahoo ^NSEI) for the headline; NIFTY 500 spliced NSE as a robustness column.
- Status: research preview. Validation in progress.
