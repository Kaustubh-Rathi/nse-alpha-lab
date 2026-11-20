# NSE Alpha Lab (Streamlit UI)

A single-file Streamlit front-end for the repository's momentum rotation research
engine. It builds a real `EngineConfig`, runs the real
`nse_pipeline.backtest.momentum.MomentumRotationEngine`, and renders the result.

**Research preview. Not investment advice. Not a trading system.**

## Status: not visually tested

This app was verified with:

```
python -m py_compile apps/lab/app.py
```

It has **never been rendered**. Streamlit is not installed in the environment
where it was written, so no page, chart, widget or layout has been seen by a
human or an agent. Treat the first real launch as untested code: expect to fix
wiring, not logic.

## Install and run

`streamlit` is intentionally not a core dependency of this repository — the
engine and its tests must stay installable without a browser UI.

```bash
# 1. core dependencies (pandas, numpy, duckdb, pyarrow)
uv sync

# 2. UI extras
uv pip install -r apps/lab/requirements.txt
#    or: pip install -r apps/lab/requirements.txt

# 3. run from the repository root
streamlit run apps/lab/app.py
```

Run from the repository root. The app resolves the repository relative to its own
file, so the working directory does not strictly matter, but the documented
command is `streamlit run apps/lab/app.py`.

If `streamlit` is missing, running `python apps/lab/app.py` exits with a clear
message naming the install command rather than an opaque `ImportError`.

## Pages

| Page | What it does |
| --- | --- |
| **Home** | Research-preview warning banner, environment status, quickstart commands. |
| **Data** | Detects a data pack under `data/`, lists what it found, and runs the integrity check via `nse_pipeline.stage1_evidence` (in-process) or `scripts/verify_stage1_inventory.py` (subprocess). |
| **Create** | Strategy builder form → JSON spec preview. The **Run** button stays disabled until *"I understand this is an unvalidated research tool, not investment advice"* is ticked. |
| **Backtest** | Runs the engine on the built spec. With no data pack it shows the fetch instructions instead of a fake result. |
| **Results** | Equity curve, drawdown, total return / CAGR / Sharpe (rf=0) / max drawdown, itemised charges, trades and skips. |
| **Compare** | Loads `research/FINAL_RESULTS_public.csv` (falling back to `research/FINAL_RESULTS.csv`) and shows the 968-row table under a multiple-testing warning. |
| **Docs** | Limitations, disclaimer, data licensing pointer. |

## Design rules this app follows

- **No strategy logic here.** The app builds `EngineConfig` and calls `run()`;
  every metric it derives from the equity curve is defined on the Results page so
  it can be checked.
- **Graceful degradation.** Every optional import (`streamlit`, `pandas`,
  `numpy`, `duckdb`, `nse_pipeline`) is wrapped in `try`/`except`. A missing
  component produces an explanatory message on the page, never a traceback on
  every page and never a fabricated number.
- **Unknown equity stays unknown.** The engine writes `equity: None` when a
  holding cannot be priced (a delisted security under the `strict` delisting
  policy). Those sessions are charted as gaps and excluded from statistics; they
  are never interpolated or marked at a stale price.
- **Assumptions are labelled where the numbers are.** `same_close` entry timing
  (look-ahead by construction), incomplete rebalance calendars, and unpriced
  holdings each raise a warning next to the result.
- **The sweep is framed honestly.** The Compare page states that the best Sharpe
  is a maximum over 968 trials, not an expectation.

## Data

No data pack is installed in this repository, and the app does not download any.
To see a backtest instead of the "no data pack installed" message, fetch a pack
with the pipeline's acquisition stage and place it under `data/`.

Data is not redistributable; see the Docs page in the app and the repository's
licensing notes.
