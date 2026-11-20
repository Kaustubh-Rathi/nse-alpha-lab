# Results CSV columns

Documentation for `FINAL_RESULTS_public.csv`, the public-facing results table
produced from the internal research sweep by `research/make_public_csv.py`.

## Status of these numbers

Every row carries `validation_status = "research_preview"`. That means:

- the figures come from an internal research sweep, not a live or audited track record;
- they have not been independently replicated;
- they are published so that others can inspect and refute them, not as a recommendation.

Costs assume a fixed slippage model and static brokerage assumptions rather than real fills.
Dividends are excluded on both sides and the benchmarks are price indices, so reported
alpha is measured against price returns, not total returns.

## Conventions

- **Percent** columns are plain numbers where `12.5` means 12.5%, not a 0.125 fraction.
- **Fraction** columns are where `0.15` means 15%.
- **INR** columns are Indian rupees.
- **Dates** are `YYYY-MM-DD`. A blank means the event did not happen, for example a
  drawdown that was never recovered.
- **Blank is meaningful**: blanks are left blank rather than filled with zero, because
  zero would misstate the result.

The file has **6 rows** and **124 columns**.

## Columns

| # | Column | Unit | Meaning |
|---:|---|---|---|
| 1 | `rank_by_sharpe` | rank | Position of this configuration when all configurations are sorted by Sharpe ratio (risk-free rate 0), best first. |
| 2 | `rank_by_cagr` | rank | Position when sorted by compound annual growth rate, best first. |
| 3 | `rank_by_alpha` | rank | Position when sorted by annualised Jensen alpha versus the NIFTY 50, best first. |
| 4 | `config_id` | identifier | Deterministic readable identifier of the configuration: universe, selection side, position count, lookback, rebalance cadence and exit overlay. |
| 5 | `sweep_family` | category | Which sweep grid produced this configuration (for example 'exit_grid_monthly', 'universe_core'). Rows in the same family share a generation process. |
| 6 | `universe` | category | Stock universe key used to select candidates, e.g. 'nifty500', 'midcap150', 'smallcap250'. |
| 7 | `universe_label` | text | Human-readable description of the universe. |
| 8 | `selection_side` | category | 'strongest' ranks by highest momentum (long the leaders); 'weakest' ranks by lowest momentum (long the laggards). |
| 9 | `top_n` | stocks | Number of positions held at each rebalance. |
| 10 | `universe_snapshots` | count | Number of point-in-time membership snapshots used to define the investable universe. |
| 11 | `universe_first_snapshot` | date (YYYY-MM-DD) | Date of the earliest membership snapshot used. |
| 12 | `universe_last_snapshot` | date (YYYY-MM-DD) | Date of the latest membership snapshot used. |
| 13 | `universe_members_min` | stocks | Smallest number of constituents seen across the snapshots. |
| 14 | `universe_members_max` | stocks | Largest number of constituents seen across the snapshots. |
| 15 | `membership_note` | text | Caveat about how complete or regular the membership history is. |
| 16 | `lookback_sessions` | trading sessions | Momentum lookback length, in sessions, used to rank stocks. |
| 17 | `cadence` | category | Rebalance frequency: 'monthly', 'quarterly', 'semiannual', 'annual', '2y' or '3y'. |
| 18 | `holding_period_months` | months | Nominal holding period implied by the cadence. |
| 19 | `calendar_mode` | category | How rebalance dates were chosen: 'evidence' (from the recorded snapshot calendar) or 'month_end_exact'. |
| 20 | `stop_loss_pct` | fraction | Stop-loss trigger: exit a position once it falls this fraction below entry. |
| 21 | `take_profit_pct` | fraction | Take-profit trigger: exit a position once it rises this fraction above entry. |
| 22 | `exit_overlay` | category | Name of the stop-loss / take-profit pair applied, or 'none'. |
| 23 | `cost_model` | category | 'full_charges' applies brokerage, taxes, stamp duty, exchange fees, GST and slippage; 'zero_cost' is a frictionless counterfactual and is not investable. |
| 24 | `scheduled_rebalances` | count | Number of rebalance dates the schedule fired. |
| 25 | `signal_dates_with_trades` | count | Number of rebalance dates on which at least one trade actually executed. |
| 26 | `held_without_membership_snapshot` | count | Times a holding was kept because no fresh membership snapshot existed for the date, rather than being sold. |
| 27 | `last_signal_date` | date (YYYY-MM-DD) | Date the most recent momentum ranking was computed. |
| 28 | `first_fill` | date (YYYY-MM-DD) | Date of the first executed trade. |
| 29 | `last_fill` | date (YYYY-MM-DD) | Date of the last executed trade. |
| 30 | `performance_start` | date (YYYY-MM-DD) | First date included in the performance measurement window. |
| 31 | `performance_end` | date (YYYY-MM-DD) | Last date included in the performance measurement window. |
| 32 | `scheduled_horizon_end` | date (YYYY-MM-DD) | Date the schedule intended to run to, before data availability was considered. |
| 33 | `sessions` | trading sessions | Number of trading sessions in the performance window. |
| 34 | `window_years` | years | Length of the performance window in calendar years. |
| 35 | `calendar_days` | calendar days | Length of the performance window in calendar days. |
| 36 | `trades` | count | Total executed trades (buys plus sells). |
| 37 | `buys` | count | Executed buy orders. |
| 38 | `sells` | count | Executed sell orders. |
| 39 | `skips` | count | Orders that were generated but did not execute. |
| 40 | `skip_reasons` | text | Counts of skip causes, e.g. 'buy:insufficient_cash=2; sell:no_bar_delisted_or_suspended=8'. |
| 41 | `assumed_last_close_writeoffs` | count | Positions written off at the last available close because the stock stopped trading (delisting or long suspension). |
| 42 | `initial_capital_inr` | INR | Starting portfolio value. |
| 43 | `final_capital_inr` | INR | Ending portfolio value, after costs and write-offs. |
| 44 | `total_return_pct` | percent | Simple return over the window, without annualisation. |
| 45 | `cagr_pct` | percent per year | Compound annual growth rate over the window. |
| 46 | `annualised_volatility_pct` | percent per year | Standard deviation of daily returns, annualised. |
| 47 | `sharpe_rf0` | ratio | Mean daily excess return (risk-free rate assumed 0) divided by daily volatility, annualised. |
| 48 | `max_drawdown_pct` | percent | Largest peak-to-trough decline of the equity curve. |
| 49 | `max_dd_peak_date` | date (YYYY-MM-DD) | Date of the peak before the largest drawdown. |
| 50 | `max_dd_trough_date` | date (YYYY-MM-DD) | Date of the trough of the largest drawdown. |
| 51 | `max_dd_recovery_date` | date (YYYY-MM-DD) | Date the equity curve regained the prior peak; blank if it never did. |
| 52 | `max_dd_peak_to_trough_days` | calendar days | Calendar days from peak to trough of the largest drawdown. |
| 53 | `max_dd_peak_to_trough_sessions` | trading sessions | Trading sessions from peak to trough of the largest drawdown. |
| 54 | `max_dd_trough_to_recovery_days` | calendar days | Calendar days from trough back to the prior peak; blank if unrecovered. |
| 55 | `max_dd_trough_to_recovery_sessions` | trading sessions | Trading sessions from trough back to the prior peak; blank if unrecovered. |
| 56 | `max_dd_total_days` | calendar days | Total calendar days under water for the largest drawdown. |
| 57 | `max_dd_total_sessions` | trading sessions | Total sessions under water for the largest drawdown. |
| 58 | `max_dd_recovered` | boolean | Whether the equity curve recovered to its prior peak after the largest drawdown. |
| 59 | `longest_dd_peak_date` | date (YYYY-MM-DD) | Peak date of the longest drawdown, which may differ from the deepest one. |
| 60 | `longest_dd_trough_date` | date (YYYY-MM-DD) | Trough date of the longest drawdown. |
| 61 | `longest_dd_recovery_date` | date (YYYY-MM-DD) | Recovery date of the longest drawdown; blank if unrecovered. |
| 62 | `longest_dd_total_days` | calendar days | Duration of the longest drawdown in calendar days. |
| 63 | `longest_dd_total_sessions` | trading sessions | Duration of the longest drawdown in sessions. |
| 64 | `drawdown_episodes` | count | Number of distinct drawdown episodes. |
| 65 | `sessions_underwater` | trading sessions | Sessions where the equity curve was below its running peak. |
| 66 | `pct_sessions_below_running_peak` | percent | Share of sessions where the equity curve was below its running peak. |
| 67 | `benchmark` | text | Benchmark used for beta and alpha, including its price or total-return basis. |
| 68 | `benchmark_window_start` | date (YYYY-MM-DD) | First date of the benchmark measurement window. |
| 69 | `benchmark_window_end` | date (YYYY-MM-DD) | Last date of the benchmark measurement window. |
| 70 | `benchmark_sessions` | trading sessions | Number of benchmark observations in the window. |
| 71 | `benchmark_cagr_pct` | percent per year | Benchmark CAGR over the same window. |
| 72 | `strategy_cagr_aligned_pct` | percent per year | Strategy CAGR recomputed over exactly the benchmark window, so the comparison is like-for-like. |
| 73 | `excess_cagr_pct` | percent per year | Aligned strategy CAGR minus benchmark CAGR. |
| 74 | `beta` | ratio | Sensitivity of strategy daily returns to benchmark daily returns. |
| 75 | `jensen_alpha_linear_pct` | percent per year | Jensen alpha from a linear regression of daily excess returns on the benchmark; annualised. |
| 76 | `jensen_alpha_geometric_pct` | percent per year | Jensen alpha computed on geometric (compounded) returns; differs from the linear figure when returns are volatile. |
| 77 | `tracking_error_pct` | percent per year | Annualised standard deviation of the strategy-minus-benchmark return. |
| 78 | `information_ratio` | ratio | Excess CAGR divided by tracking error. |
| 79 | `up_capture_pct` | percent | Strategy return captured in benchmark-up sessions relative to the benchmark. |
| 80 | `down_capture_pct` | percent | Strategy return captured in benchmark-down sessions relative to the benchmark; lower is better. |
| 81 | `r_squared` | ratio 0-1 | Share of strategy return variance explained by the benchmark. |
| 82 | `alpha_t_ols` | t-statistic | Ordinary-least-squares t-statistic for the alpha term. Above about 2 is conventionally labelled significant, though this is only one window. |
| 83 | `alpha_t_newey_west_5` | t-statistic | t-statistic for alpha using Newey-West standard errors with 5 lags, which correct for autocorrelation and are the more conservative figure. |
| 84 | `nifty500_benchmark` | text | Secondary benchmark (NIFTY 500) used for the companion block of columns. |
| 85 | `nifty500_window_start` | date (YYYY-MM-DD) | First date of the NIFTY 500 measurement window. |
| 86 | `nifty500_window_end` | date (YYYY-MM-DD) | Last date of the NIFTY 500 measurement window. |
| 87 | `nifty500_sessions` | trading sessions | Number of NIFTY 500 observations in the window. |
| 88 | `nifty500_benchmark_cagr_pct` | percent per year | NIFTY 500 CAGR over the window. |
| 89 | `nifty500_strategy_cagr_aligned_pct` | percent per year | Strategy CAGR recomputed over the NIFTY 500 window. |
| 90 | `nifty500_excess_cagr_pct` | percent per year | Aligned strategy CAGR minus NIFTY 500 CAGR. |
| 91 | `nifty500_beta` | ratio | Sensitivity to NIFTY 500 daily returns. |
| 92 | `nifty500_alpha_linear_pct` | percent per year | Annualised Jensen alpha versus the NIFTY 500 (linear). |
| 93 | `nifty500_alpha_geometric_pct` | percent per year | Annualised Jensen alpha versus the NIFTY 500 (geometric). |
| 94 | `nifty500_tracking_error_pct` | percent per year | Annualised tracking error versus the NIFTY 500. |
| 95 | `nifty500_information_ratio` | ratio | Excess CAGR divided by tracking error, versus the NIFTY 500. |
| 96 | `nifty500_up_capture_pct` | percent | Up-capture versus the NIFTY 500. |
| 97 | `nifty500_down_capture_pct` | percent | Down-capture versus the NIFTY 500. |
| 98 | `nifty500_r_squared` | ratio 0-1 | Share of return variance explained by the NIFTY 500. |
| 99 | `nifty500_t_ols` | t-statistic | OLS t-statistic for alpha versus the NIFTY 500. |
| 100 | `nifty500_t_newey_west_5` | t-statistic | Newey-West (5 lag) t-statistic for alpha versus the NIFTY 500. |
| 101 | `buy_notional_inr` | INR | Total value of executed buys. |
| 102 | `sell_notional_inr` | INR | Total value of executed sells. |
| 103 | `turnover_inr` | INR | Total traded value. |
| 104 | `buy_charges_inr` | INR | All transaction costs incurred on buys. |
| 105 | `sell_charges_inr` | INR | All transaction costs incurred on sells. |
| 106 | `total_charges_inr` | INR | Total transaction costs, equal to the sum of their components below. |
| 107 | `brokerage_inr` | INR | Brokerage paid. |
| 108 | `gst_inr` | INR | Goods and services tax on brokerage and exchange fees. |
| 109 | `stt_inr` | INR | Securities transaction tax. |
| 110 | `stamp_inr` | INR | Stamp duty. |
| 111 | `exchange_inr` | INR | Exchange transaction charges. |
| 112 | `slippage_inr` | INR | Assumed cost of filling at a worse price than the reference price. |
| 113 | `charges_bps_of_turnover` | basis points | Total costs expressed as basis points of turnover. |
| 114 | `charges_pct_of_initial_cash` | percent | Total costs expressed as a percentage of starting capital. |
| 115 | `flag_few_rebalances` | boolean | True when the run has too few rebalances for the ranking statistics to be reliable. |
| 116 | `flag_short_window` | boolean | True when the measurement window is short relative to the lookback and rebalance cadence. |
| 117 | `flag_exit_cash_until_rebalance` | boolean | True when a stop-loss or take-profit exit left the slot in cash until the next scheduled rebalance rather than redeploying immediately. |
| 118 | `universe_overlap_note` | text | How this universe overlaps the others in the sweep, so correlated rows are not mistaken for independent evidence. |
| 119 | `flag_superseded` | boolean | True when a later, corrected run replaced this configuration. |
| 120 | `price_basis` | text | How adjusted prices were built, e.g. split/bonus adjusted with dividends excluded. |
| 121 | `benchmark_basis` | text | Price or total-return basis of the benchmark index. |
| 122 | `dividend_treatment` | text | Whether dividends are included, and on which side of the comparison. |
| 123 | `notes` | text | Free-text remarks about the run, including data caveats. |
| 124 | `validation_status` | category | Public validation state of this row. 'research_preview' means the numbers come from an internal research sweep, have not been independently replicated, and are published to invite scrutiny rather than as a track record. |

## What differs from the internal file

Removed internal bookkeeping columns: `gate_status`, `publishable`, `refusal`, `alpha_error`, `nifty500_alpha_error`, `evidence_label`, `is_flagship`.

Those columns described the team's internal review process rather than the strategies,
so they are dropped. `validation_status` replaces them with one plain public status.

Renamed for clarity: `direction` to `selection_side`; `costs` to `cost_model`; `pct_sessions_underwater` to `pct_sessions_below_running_peak`; `t_ols` to `alpha_t_ols`; `t_newey_west_5` to `alpha_t_newey_west_5`; `alpha_linear_pct` to `jensen_alpha_linear_pct`; `alpha_geometric_pct` to `jensen_alpha_geometric_pct`; `initial_equity_inr` to `initial_capital_inr`; `final_equity_inr` to `final_capital_inr`; `delisting_writeoffs` to `assumed_last_close_writeoffs`; `unproven_universe_holds` to `held_without_membership_snapshot`; `family` to `sweep_family`; `flag_universe_overlap` to `universe_overlap_note`.

Values made readable: `top` becomes `strongest` and `bottom` becomes `weakest` in
`selection_side`; `all_charges` becomes `full_charges` and `zero` becomes `zero_cost`
in `cost_model`.
