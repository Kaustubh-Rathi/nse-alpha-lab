# NSE Alpha Lab

**A reproducible research and backtesting lab for NSE momentum-rotation strategies.**

NSE Alpha Lab is a Python research workspace for testing systematic equity strategies on
National Stock Exchange of India (NSE) data. It contains a point-in-time index-membership
pipeline, a split/bonus-adjusted price store, a charge-aware momentum rotation engine, a
1,000-configuration sensitivity sweep with the full results published in this repository,
and a Streamlit UI for building and running your own specifications.

Every result in this repository is published, including the results that do not support
the strategy. The headline finding is a **null result** and this README leads with it.

---

> ## ⚠️ Research preview — not validated, not investment advice
>
> This is an **early-stage research project**. It is a backtesting lab, not a trading
> system. Nothing here has been validated for live deployment, and none of it is
> investment advice, a recommendation, or a solicitation.
>
> **Read the multiple-testing disclosure in [Results](#results) before quoting any
> number from this repository.** The highest Sharpe ratio in the published table is the
> *maximum* of 968 trials, not the performance you should expect from a strategy.
> See [DISCLAIMER.md](DISCLAIMER.md) and [STATUS.md](STATUS.md).

---

## Contents

- [What this is](#what-this-is)
- [Quickstart](#quickstart)
- [What's in the repository](#whats-in-the-repository)
- [Getting data](#getting-data)
- [Results](#results)
- [Limitations](#limitations)
- [Licence](#licence)
- [Citation](#citation)

---

## What this is

NSE Alpha Lab answers one question honestly: **does a simple cross-sectional momentum
rotation strategy on Indian equities beat the NIFTY 50, once every charge is paid and the
research is corrected for the number of configurations tried?**

The lab is built to make that question falsifiable rather than to produce a flattering
answer. Concretely it provides:

| Component | What it does |
|---|---|
| **Point-in-time universes** | Official NIFTY 500 / MidSmallcap 400 / Midcap 150 / Smallcap 250 / NIFTY 50 membership snapshots, keyed by date. A backtest on date *T* sees only constituents that were in the index on *T*. |
| **Corporate-action handling** | Splits and bonuses are applied to build an adjusted price series. Share actions, dividends, rights and demergers are tracked as dated events with provenance. |
| **Momentum rotation engine** | `MomentumRotationEngine` — equal-weight, long-only, integer shares, next-open fills, with configurable lookback, cadence, position count, selection direction and stop-loss / take-profit overlays. |
| **Full Indian charge stack** | Brokerage, STT, stamp duty, exchange charges, GST and slippage, charged on every buy and every sell as explicit cash amounts that can be audited trade by trade. |
| **Sensitivity sweep** | A 968-configuration grid across five universes, six holding periods, both selection directions and 25 exit overlays. The complete result table is published. |
| **Benchmark-relative statistics** | Beta, Jensen alpha (linear and geometric), tracking error, information ratio, up/down capture, Newey-West t-statistics, block-bootstrap p-values, and drawdown depth and duration. |
| **Validation harness** | 348 fail-closed internal consistency checks that independently recompute accounting identities, charges, drawdowns and alpha regressions. |

The design rules are enforced in code, not just documented: no look-ahead (a signal formed
on close *T* can only execute at the open of *T+1*), no invented prices, no invented
corporate actions, deterministic tie-breaking, and integer shares only.

---

## Quickstart

### Option A — Docker (UI)

Requires Docker with Compose v2.

```bash
git clone <this-repository-url> nse-alpha-lab
cd nse-alpha-lab
docker compose -f docker/compose.yaml up lab
```

Then open **http://localhost:8501**.

The `lab` service is the default and the only service a bare `up` starts. It serves the
Streamlit research UI: build a strategy spec, inspect the data, run a backtest, and compare
against the published 968-configuration table.

Other lanes are opt-in profiles:

```bash
# Shell inside the container (pytest, CLI commands)
docker compose -f docker/compose.yaml --profile cli run --rm cli

# Network-enabled lane, the only service that fetches data
docker compose -f docker/compose.yaml --profile fetch run --rm fetch --help

# PDF lane — builds with the optional `pdf` extra (AGPL; see NOTICE)
docker compose -f docker/compose.yaml --profile pdf build pdf
```

There is deliberately **no Postgres and no Redis**. Storage is an embedded DuckDB file plus
parquet, opened in-process, and backtests never touch the network.

### Option B — CLI / library

Requires Python 3.12–3.14 and [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repository-url> nse-alpha-lab
cd nse-alpha-lab
uv sync
uv run pytest -q
```

Run the lab from Python:

```bash
export PYTHONPATH=src
uv run python -c "import nse_pipeline; print(nse_pipeline.__name__)"
```

Reproduce the headline backtest from a specification file:

```bash
uv run python -m nse_pipeline backtest examples/specs/primary.json
```

Run the sensitivity sweep (long-running; see `research/REPORT.md` for timings):

```bash
uv run python -m nse_pipeline sweep
```

The lab runs on **Linux, macOS and Windows**. The commands above use POSIX shell syntax;
on Windows use `$env:PYTHONPATH='src'` and `uv run` in PowerShell.

> **No data pack is bundled with this repository.** `uv sync` gives you the code and the
> test suite, which runs offline with no data. To run a backtest you need the data pack —
> see [Getting data](#getting-data).

---

## What's in the repository

```
nse-alpha-lab/
├── README.md                 This file
├── LICENSE                   Apache License 2.0
├── NOTICE                    Attribution notices and third-party licences
├── CITATION.cff              Machine-readable citation metadata
├── STATUS.md                 What is checked, what is not yet validated
├── DISCLAIMER.md             Not investment advice
├── DATA_LICENSES.md          Data provenance and redistribution terms
├── pyproject.toml            Dependencies and project metadata
├── uv.lock                   Locked dependency versions
│
├── src/nse_pipeline/         The library
│   ├── backtest/             Momentum engine, accounting, charges, exits, invariants
│   ├── nifty/                Point-in-time index membership evidence and importers
│   ├── adjust/               Split/bonus adjustment factors
│   ├── actions/              Corporate-action evidence and verification
│   ├── universe/             Research eligibility and universe construction
│   ├── analytics/            Metrics and statistics
│   ├── audit/                Calendar, gap and readiness audits
│   └── ...                   download, build, identity, schemes, fundamentals
│
├── tests/                    pytest suite (runs offline, no data required)
│   └── golden/               Frozen golden-case JSON fixtures
│
├── research/                 The published study
│   ├── REPORT.md             Primary report: pre-specified config, alpha, charges
│   ├── SWEEP_REPORT.md       The 968-configuration sweep narrative
│   ├── FINAL_RESULTS_public.csv  All 968 runs, one row each — the curated public table
│   ├── FINAL_RESULTS.csv     The same 968 runs in the internal research schema
│   ├── FLAGSHIP_RESULTS.csv  The six headline configurations
│   ├── BENCHMARK.md          Benchmark construction and splice evidence
│   └── *.py                  Reproducible build scripts
│
├── apps/lab/                 Streamlit research UI
├── examples/specs/           Example strategy specification files
├── docker/                   Multi-stage Dockerfile and Compose file
├── docs/                     Architecture notes and audit records
└── scripts/                  Data-build and verification utilities
```

**Where to start reading:** [research/REPORT.md](research/REPORT.md) for the primary
result, then [research/SWEEP_REPORT.md](research/SWEEP_REPORT.md) for the sensitivity
grid, then [STATUS.md](STATUS.md) for an honest list of what has and has not been checked.

---

## Getting data

The data pack is **not** distributed inside the git repository — it is too large for git
and is published separately as a versioned, checksummed archive.

### 1. Download the pack

The pack is published as a versioned, checksummed archive on the project's data page.
[docs/PUBLISH.md](docs/PUBLISH.md) records the current authoritative location and the exact
command for each supported host. Unpack it so the repository root contains a `data/`
directory:

```bash
nse-alpha-lab/
└── data/
    ├── manifest.json         SHA-256 digest and byte size for every file below
    ├── processed/            Identity-resolved adjusted bars, catalog, universe snapshots
    ├── manifests/            Upstream provenance for every source series
    ├── reports/              Readiness, coverage and audit reports
    └── provenance/           Upstream-to-shipped hash map
```

The CLI can fetch and verify the pack for you instead:

```bash
uv sync --extra fetch        # adds the Kaggle and Hugging Face clients
uv run python -m nse_pipeline fetch \
  --source kaggle --from <owner>/nse-alpha-lab-data-pack \
  --dest data --manifest data/manifest.json
```

### 2. Verify it

Every pack ships with a `manifest.json` at its root listing each file's path, byte size and
SHA-256 digest. Verify before use — the CLI refuses to continue on a mismatch:

```bash
uv pip install -e .          # makes `python -m nse_pipeline` importable
uv run python -m nse_pipeline verify --pack data
```

A corrupted or incomplete pack reports a `MISMATCH` line per affected file and exits
non-zero; the payload is then deleted rather than left usable.

### 3. Point the lab at it

The engine looks for the data directory in this order:

1. `$NSE_DATA_DIR` (set automatically to `/app/data` inside Docker),
2. `./data` relative to the repository root.

Inside Docker, `./data` on the host is bind-mounted to `/app/data` in the container, so the
host CLI and the container always agree on exactly one dataset.

### Data sources and terms

The pack is assembled from public NSE publications, index-membership snapshots, and
cross-check series. Per-source provenance, the licence position for each source, and the
redistribution terms are documented in [DATA_LICENSES.md](DATA_LICENSES.md). If you
redistribute the pack or derivatives of it, read that file first.

If you prefer to build the pack yourself instead of downloading it, the acquisition and
verification pipeline is in `src/nse_pipeline/download/`, `src/nse_pipeline/nifty/` and
`scripts/`; it is network-enabled and deliberately separated from the backtesting code.

---

## Results

All numbers below are **net of the full charge stack**, **split/bonus adjusted**, and
**price-vs-price** (dividends excluded on both sides). The full table is
[research/FINAL_RESULTS_public.csv](research/FINAL_RESULTS_public.csv) — 968 rows, one per
configuration, with every intermediate statistic and a `validation_status` column on every
row. Column-by-column documentation is in
[research/CSV_COLUMNS.md](research/CSV_COLUMNS.md). The narrative is
[research/SWEEP_REPORT.md](research/SWEEP_REPORT.md).

### Read this before any number in the table

> ### 🚩 Multiple-testing disclosure
>
> **968 strategy configurations were tested.** The table is sorted by Sharpe ratio, so the
> top row is the *maximum* of 968 trials, not a strategy expectation. If you took the
> best of 968 coin flips you would also get a flattering number.
>
> - **No multiple-testing correction is applied anywhere in this repository.**
> - **Sharpe ratios are not comparable across rows.** Rows have different windows
>   (4.66 years to 13.1 years), different rebalance counts, and different amounts of time
>   parked in cash.
> - **The highest Sharpe belongs to the shortest window.** All 25 top-Sharpe rows span
>   only 4.66–4.92 years.
> - **Only 26 of 968 configurations clear t = 1.96**, where roughly **48** would be
>   expected by chance alone at the 5% level if no configuration had any edge.
>
> The pre-specified primary configuration — fixed before the sweep was run — is the
> result that matters, and it is **not statistically significant**. See below.

### The pre-specified result

`nifty500_top_N10_L63_monthly_none` — NIFTY 500, top 10 by 63-session momentum, monthly
rebalance, next-open fills, no exit overlay — was specified before the sweep and is not
selected from it.

| Metric | Value |
|---|---|
| Window | 2013-05-31 → 2026-09-30 (13.1 years) |
| Starting capital | ₹10,000,000 |
| Final capital | ₹100,797,469 |
| **Total return** | **907.97%** |
| **CAGR** | **19.30%** |
| Annualised volatility | 27.62% |
| **Sharpe ratio (rf = 0)** | **0.7782** |
| **Maximum drawdown** | **−55.30%** (2018-08-28 → 2020-03-24, recovered 2021-04-06) |
| Trades | 2,080 (1,131 buys / 949 sells) |
| Charges paid | ₹15,178,940 (18.35 bps of turnover) |

Against the NIFTY 50 over the same window: beta 0.95, Jensen alpha 10.70% linear,
tracking error 23.04%, information ratio 0.38.

| Significance test | Value | Verdict |
|---|---|---|
| Alpha t-statistic, OLS | 1.67 | — |
| **Alpha t-statistic, Newey-West (5 lags)** | **1.62** | Below the 1.96 threshold |
| **Bootstrap p-value (21-session blocks, 2,000 reps)** | **0.096** | Above the 0.05 level |
| **Statistically significant at 5%?** | — | **No** |

**The primary configuration's alpha is economically large but not statistically
significant.** With t = 1.62 and a bootstrap p-value of 0.096, this data cannot distinguish
the result from noise. That is the finding. It is reported as the finding.

Charges matter: the same strategy with every charge set to zero compounds at 22.36%
instead of 19.30%. The charge stack costs **3.06% of CAGR**.

### The sweep: 968 configurations

| Statistic | Value | Reading |
|---|---|---|
| Configurations tested | **968** | Five universes, six holding periods, both directions, 25 exit overlays |
| Highest Sharpe in the table | **1.3362** | `smallcap250_top_N25_L63_monthly_sl15_tp10` — a **maximum over 968 trials** |
| Window of that row | 4.66 years | Smallest possible window in the grid |
| Window range, top 25 rows | **4.66 – 4.92 years** | Every top-Sharpe row is a short-window row |
| Rows spanning the full 13-year window | **428 / 968** | Comparable, long-window evidence for less than half the grid |
| Rows beating NIFTY 50 on CAGR | **220 / 968** | 22.7% — none of them a NIFTY 50 row (see below) |
| Median excess CAGR vs NIFTY 50 | **−9.73%** | The median configuration *lost* to the index by ~10 pp/yr |
| Rows with t > 1.96 | **26 / 968** | vs. ~**48** expected by chance |
| Median Sharpe across all rows | 0.20 | The centre of the grid is close to zero |

Two things follow from this table, and they point in opposite directions:

1. **There is structure in the data.** The top of the grid is not random: small and
   mid-cap universes with stop-loss overlays dominate the top 25, and the mirror-image
   "buy the weakest names" portfolio loses badly (−64.01% total, −15.46% alpha,
   Newey-West t = −2.13). Opposite signs on opposite selections is what a genuine momentum
   premium looks like.
2. **Most of the grid still loses to the index.** Only 22.7% of configurations beat
   NIFTY 50 on CAGR and the median configuration trails by 9.73 pp/yr. Beating the
   benchmark has not been shown to be a reliable property of this strategy family.

**Which configurations beat the index?** The 428 long-window rows break down as 278 NIFTY
500 rows (13.1 years) plus 150 NIFTY 50 rows (12.68 years). The 220 configurations that beat
NIFTY 50 on CAGR come from exactly four groups:

| Group | Rows beating NIFTY 50 | Out of | Window |
|---|---:|---:|---|
| NIFTY 500 | 58 | 278 | 13.1 years |
| MidSmallcap 400 | 61 | 180 | 4.66 years |
| Smallcap 250 | 61 | 180 | 4.66 years |
| Midcap 150 | 40 | 180 | 4.66–5.9 years |
| **NIFTY 50** | **0** | **150** | 12.68 years |

Two things stand out. First, **not one of the 150 NIFTY 50 configurations beat the index it
is built from** — unsurprising, since a 10-name subset of a 50-name index with charges
attached is not a route to beating it, but worth stating plainly. Second, the winners are
concentrated in the short mid/small-cap windows: **162 of the 220** come from the three
2017–2022 universes, whose runs span 4.66–5.9 years depending on the holding period. That
is the same short-window grid whose Sharpe ratios dominate the ranking. Over the full
13-year window, **58 of 278 NIFTY 500 configurations (21%) beat the index** — roughly one in
five, which is the honest summary. The short-window rows that dominate the Sharpe ranking
cannot be extended to the full period.

### Why the top of the table is not the answer

- **The window is short.** NIFTY Midcap 150 / Smallcap 250 / MidSmallcap 400 membership
  evidence covers 2017-07 to 2022-03 (4.75 years), and each run stops when its final
  scheduled holding period completes. A 4.66-year Sharpe is not comparable to a
  13.1-year Sharpe on the same scale.
- **Several of them sit mostly in cash.** With a stop-loss plus take-profit overlay on a
  long holding period, exit proceeds sit in cash until the next rebalance. That lowers
  volatility and inflates Sharpe. The `flag_exit_cash_until_rebalance` column marks
  every affected row.
- **The universes overlap.** Midcap 150 and Smallcap 250 are subsets of NIFTY 500, and
  MidSmallcap 400 overlaps it. They are segment views of one market, not independent
  confirmations.

The full column-by-column documentation of the CSV is in
[research/CSV_COLUMNS.md](research/CSV_COLUMNS.md).

---

## Limitations

These are known, open limitations. They are listed here so the results can be read with
the right amount of suspicion. [STATUS.md](STATUS.md) tracks the same list with the
current validation state of each item.

1. **Dividends are excluded — results are price-vs-price.** Prices are split/bonus
   adjusted only. The benchmark is also a price index, so the comparison is internally
   consistent on the benchmark axis, but both sides understate total return. The sign of
   the net alpha bias depends on the strategy's dividend yield relative to the NIFTY 50's,
   and that is not established here. **No row in this repository is a total-return result.**
2. **Post-2022 NIFTY 500 membership is point evidence at irregular dates.** Monthly
   official snapshots run to 2022-03-31; from then on membership is anchored to official
   events (64 anchors) with `CoverageUnknown` between them. Gap between consecutive
   rebalances has a median of 30 days and a maximum of 154. **After 2022 this is an
   event-anchored strategy, not a strict monthly one.**
3. **There is no NIFTY 750 test.** The repository has no point-in-time NIFTY Total Market
   or Microcap 250 constituent chain. A 750-name universe was not fabricated from today's
   constituents, because that would inject survivorship and look-ahead bias. This is a
   data gap, recorded as such.
4. **Live execution is not modelled.** The engine fills at the next session's open with
   integer shares. It does not model market impact, ADV limits, circuit limits,
   suspensions, or capital-gains tax. A ₹10M portfolio that compounds to ₹100M in 10-name
   equal-weight positions would face real capacity constraints that this model ignores.
5. **Regime robustness is not established.** The result spans one 13-year sample
   containing two large drawdowns. There is no out-of-sample holdout, no walk-forward
   analysis, and no stability test across sub-periods beyond the 2013–2022 / 2022–2026
   split in the report.
6. **Multiple-testing bias is uncorrected.** 968 configurations were evaluated and the
   best selected by Sharpe. No correction (e.g. Deflated Sharpe, White's Reality Check,
   Bonferroni) is applied. The 26-vs-48 significant results are the direct symptom.
7. **Terminal proceeds for delisted holdings are assumed.** The `last_close_writeoff`
   policy exits a delisted holding at its last observed close. That is optimistic if the
   true proceeds were zero.
8. **The charge schedule is a 2026-style constant.** Brokerage, STT, stamp duty, exchange
   fees, GST and slippage are held fixed across 2013–2026. Indian rates changed several
   times in that window.
9. **Single-vendor NIFTY 50 series for the long window.** The headline benchmark is the
   Yahoo `^NSEI` price series, because it is the only NIFTY 50 series covering 2026. An
   NSE-spliced NIFTY 50 is used as an independent cross-check (the two agree to within
   0.0008 index points on all 2,497 overlapping sessions).

---

## Licence

- **Code and documentation:** [Apache License 2.0](LICENSE) —
  Copyright 2026 Kaustubh Rathi. See [NOTICE](NOTICE) for attribution and
  third-party licence notes.
- **Data:** see [DATA_LICENSES.md](DATA_LICENSES.md) for per-source provenance and
  redistribution terms. The data pack is distributed under its own terms, which are
  separate from the code licence.
- **PyMuPDF** is AGPL-3.0 and is used **only** in the optional `pdf` extra
  (PDF extraction in `research/pdf`). It is not required to run the lab, the CLI, or the
  UI. Do not enable that extra in a deployment unless you can comply with AGPL-3.0.

## Citation

If you use this work, cite it:

```
Rathi, Kaustubh (2026). NSE Alpha Lab (Version 0.1.0) [Computer software].
https://github.com/<owner>/nse-alpha-lab
```

Machine-readable metadata is in [CITATION.cff](CITATION.cff).

## Disclaimer

Not investment advice. Past performance does not predict future returns. This is
research and educational software. Read [DISCLAIMER.md](DISCLAIMER.md) in full.
