# Write your first alpha and test it

This walks from a clean checkout to a strategy specification you can run, inspect and test.
It assumes Python and `uv`, not prior knowledge of this repository.

Watch the [full walkthrough](assets/demo.mp4). The [alpha anatomy diagram](assets/images/alpha-anatomy.png)
shows how a specification maps onto the engine.

> **Research preview, not investment advice.** Read [DISCLAIMER.md](../DISCLAIMER.md) before relying on any output.

## 1. What you will build

The pre-specified N10 primary: NIFTY 500, top 10 by 63-session momentum, monthly rebalance with
next-open fills, the full Indian charge stack, no exit overlay, benchmarked against the NIFTY
50. It was fixed before the 968-configuration sweep, not chosen from it.

An **alpha is a JSON strategy specification**, not a Python subclass. The engine is fixed;
the JSON selects the ranking window, holding count, cadence, exit overlay, cost model and
dates. You do not write a `Strategy` class.

## 2. Prerequisites

| Requirement | Detail |
|---|---|
| Python | 3.12, 3.13 or 3.14 (`>=3.12,<3.15`) |
| uv | Dependency and virtualenv manager used here |
| Docker | Optional, only for the container lane |
| Data pack | Not needed to install, import, run tests or open the UI |

On Windows use PowerShell. POSIX syntax is shown only where it differs.

## 3. Install and smoke-test

```powershell
git clone https://github.com/Kaustubh-Rathi/nse-alpha-lab.git
cd nse-alpha-lab
uv sync
uv run pytest -q
```

`uv sync` creates `.venv` and installs core dependencies plus the `dev` group. The suite is
offline: no network, no `data/` read, no pack. Tests needing the non-public `verification/`
payload or a pack skip with a visible reason. Expect a shape like this, with your own counts:

```text
N passed, M skipped, K xfailed in <time>
```

The documented CI run is 1,952 passed, 68 skipped, 1 xfailed on Ubuntu with Python 3.14.
There is no Windows or macOS CI job, so no three-OS claim. On Windows, temp-directory
permission errors can fail unrelated tests; treat those as environment issues, not engine
failures.

## 4. Get the data pack

**There is no public download today.** The 1.0.0 pack is built, verified file-by-file and uploaded
to a private Hugging Face dataset, `manydream/nse-alpha-lab-data-pack` (commit `9cf24b8`). Kaggle
staging (`kaustubh-rathi/nse-alpha-lab-data-pack`) is prepared but not uploaded; Zenodo has not
been uploaded. [docs/PUBLISH.md](PUBLISH.md) is the authoritative record. The pack is not in git;
it is a checksummed tree with `manifest.json` at its root and bars under `processed/adjusted/`.

Access to the private dataset is granted by the release owner. Authenticate with a Hugging
Face token that can read that repository (`hf auth login`, or `huggingface-cli login`), then:

```powershell
uv sync --extra fetch
$env:PYTHONPATH='src'
uv run python -m nse_pipeline fetch --source hf `
  --from manydream/nse-alpha-lab-data-pack --dest data
uv run python -m nse_pipeline verify --pack data
```

`uv sync` does not install the `nse-alpha` console script (`[tool.uv] package = false`), so
the module form is deliberate. `fetch` accepts `--manifest PATH`; on a first fetch the manifest
is not on disk yet, so fetch without it and verify afterwards. If you received the archive,
extract it so `data/manifest.json` and `data/processed/` sit at the top of `data/`, check its
SHA-256 in [docs/PUBLISH.md](PUBLISH.md), then verify.

The engine looks for data at `$NSE_DATA_DIR` first, then `./data` relative to the repository
root. Set it in PowerShell with `$env:NSE_DATA_DIR = 'D:\path\to\pack'`.

The official 1.0.0 pack nests bars under `processed/adjusted/`. **`backtest --pack DIR`
expects a flat `DIR/adjusted_bars.parquet`**, as the synthetic demo has. For the official
pack, use `NSE_DATA_DIR` or `./data`, not `--pack`. `verify --pack data` is fine.

## 5. Read the primary spec

`examples/specs/primary.json` in full:

```json
{
  "name": "primary",
  "description": "Pre-specified primary: monthly top-10 momentum on the point-in-time NIFTY 500, full charges, no exit overlay.",
  "universe": "nifty500",
  "benchmark": "nifty50",
  "top_n": 10,
  "direction": "top",
  "lookback_sessions": 63,
  "cadence": "monthly",
  "stop_loss_pct": null,
  "take_profit_pct": null,
  "cost_model": "full_charges",
  "start": "2013-01-31",
  "end": "2026-09-30"
}
```

| Field | Type, default, allowed | Effect |
|---|---|---|
| `universe` | string, required | Any non-empty label naming the intended universe. The CLI prints it but does not filter bars by membership. |
| `benchmark` | string or null, default null; `nifty50`, `nifty500`, `nifty200`, `msci400`, `midcap150`, `smallcap250`, `anderson_cio_index` | Label only. The CLI does not compute benchmark-relative statistics. |
| `top_n` | integer, default 20, `>= 1` | Names held after each rebalance, equal weight. |
| `direction` | `top` or `bottom`, default `top` | `top` buys the strongest trailing return; `bottom` is a reversal counterfactual. |
| `lookback_sessions` | integer, default 63, `>= 1` | Trailing sessions used to rank. |
| `cadence` | `monthly`, `quarterly`, `semiannual`, `annual`; default `monthly` | Rebalance frequency, using the last observed session of each period. |
| `stop_loss_pct`, `take_profit_pct` | number or null, default null, `0 < x < 100` | Close-based exit after an x% fall or rise, filled at the next open. |
| `cost_model` | `full_charges` or `zero_cost`, default `full_charges` | Full stack, or a gross counterfactual with no charges. |
| `start`, `end` | ISO date or null, default null | Inclusive first and last bar dates. |
| `name`, `description` | string or null, default null | Labels for humans; no effect on the test. |

Unknown fields fail closed: `unknown field(s): ...` rather than a silent change. **CLI scope:**
`backtest` applies dates, but `universe` and `benchmark` are labels. It does not filter bars by
universe or compute benchmark statistics; the published membership join and alpha table come
from the research harness.

## 6. Write your own

```powershell
Copy-Item examples/specs/primary.json my_alpha.json
```

Set `"name": "my_alpha"` and describe what you changed. The shipped primary already uses
`top_n: 10`, so keep it as the clean baseline, then make at most one safe edit:

| Edit | Effect |
|---|---|
| `top_n` | More names mean smaller weights and more orders; fewer concentrate the book. |
| `lookback_sessions` | Longer reacts more slowly; shorter turns over more and is noisier. |
| `stop_loss_pct` | Adds a close-based exit that can leave proceeds in cash until the next rebalance. |

Changing a field after seeing the result is a new trial, not a correction. With 968
configurations published without a multiple-testing correction, an extra trial inflates the
bias. Write the change down before you run it, or label the file as a separate trial.

## 7. Run it

Two ways to invoke the same engine. Use one.

**Source checkout, without installing.** `uv sync` deliberately does not install the project
(`[tool.uv] package = false`), so `src` must be on `PYTHONPATH` for the module form:

```powershell
$env:PYTHONPATH='src'
uv run python -m nse_pipeline backtest my_alpha.json
```

This form needs the official pack at `./data` or under `$NSE_DATA_DIR`; do not pass `--pack`
for the nested 1.0.0 layout.

**With the console script.** `uv pip install -e .` installs this checkout in editable mode and
creates the `nse-alpha` entry point. Re-run it after changing `pyproject.toml`; source edits
need no reinstall.

```powershell
uv pip install -e .
uv run nse-alpha backtest my_alpha.json
```

**Export the full result:**

```powershell
uv run nse-alpha backtest my_alpha.json --out my_alpha_result.json
```

The file is `{"summary": {...}, "result": {...}}`. `summary` holds the counts, final cash and
equity, `unpriceable_sessions` and `total_return_pct`; `result` holds `equity` (one row per
session), `trades`, `skips`, `exits`, `final_positions` and `final_cash`. Trades carry `cost`
and `reason`; skips and exits carry reasons. The JSON is the audit trail.

With `--out`, the console summary has this shape; values depend on the pack:

```text
Backtest: my_alpha
==================
  universe           nifty500
  benchmark          nifty50
  top_n              10
  direction          top
  lookback_sessions  63
  cadence            monthly
  cost_model         full_charges
  start              2013-01-31
  end                2026-09-30

Ran <sessions> sessions, <rebalances> rebalances.
  trades            <count>
  exits             <count>
  skips             <count>
  final cash        <decimal>
  final equity      <decimal>
  total return      <percent>%
  written to        my_alpha_result.json
```

If a holding was delisted and could not be priced, a `NOTE` line names the sessions with
unknown equity. **Scope again:** this is a mechanics run over the pack's bars, not the published
study; the research harness applies membership and computes alpha.

## 8. No data pack? Run the synthetic mechanics demo

**Synthetic output is not a result. It is not market data, not evidence for or against the
strategy, and must not be cited, screenshotted as performance, or compared with the published
reports.** The prices come from a seeded random number generator; the only purpose is to
exercise the CLI end to end.

```powershell
uv run python examples/synthetic_demo/build_pack.py
$env:PYTHONPATH='src'
uv run python -m nse_pipeline backtest examples/synthetic_demo/hello_alpha.json `
  --pack examples/synthetic_demo/pack `
  --out examples/synthetic_demo/result.json
```

The builder writes `examples/synthetic_demo/pack/`, ignored by git. The spec lives outside
`examples/specs/` so it cannot be mistaken for a research specification. On the current
fixture the run prints (abridged):

```text
Backtest: hello_alpha
=====================
Ran 1304 sessions, 60 rebalances.
  trades            533
  exits             0
  skips             16
  final equity      8886353.0450245146
  total return      -11.1365%
```

The seed is fixed, so these numbers should reproduce. They are still synthetic. A negative
number here is not a finding about momentum; it is a random walk with charges.

## 9. Test it at three levels

**(a) Targeted engine tests.** Ranking and holding logic, causality (a signal formed on close
T fills at the open of T+1; missing membership cannot widen the universe), and exits:

```powershell
uv run pytest -q tests/test_backtest_momentum.py tests/test_leakage_causality.py tests/test_backtest_exits.py
```

The README records 84 passed, 1 xfailed for this command on the current checkout.

**(b) Full offline suite.** No network and no data pack; tests needing the non-public
`verification/` payload or a pack skip with a visible reason:

```powershell
uv run pytest -q
```

The documented CI run is 1,952 passed, 68 skipped, 1 xfailed on Ubuntu with Python 3.14.
There is no Windows or macOS CI job; a local pass is not a three-OS guarantee.

**(c) Published-artifact validation.** `research/validation.json` records **348 checks, 0
failures** from the last published run, generated `2026-11-19T07:06:01.181956+00:00`. The
checks recompute accounting identities, the charge stack, drawdowns and alpha regressions, and
fail closed on missing or unparseable input. The harness is `research/validate_alpha_output.py`.
It needs three JSON inputs that are not in the public checkout:
`research/strategy-results.json`, `research/benchmark-series.json` and
`research/alpha-results.json`. It also reads `research/FINAL_RESULTS.csv` and
`research/FLAGSHIP_RESULTS.csv`, which are present. You cannot rerun the harness from the
public checkout alone. The record is auditable: every entry has a name, an `ok` flag and a `detail`
payload; the top level records `"passed": true`, `"checks_total": 348` and `"checks_failed": 0`.

## 10. Read the result honestly

Read in this order:

1. **Sessions and rebalances.** Check the window and cadence match the spec; a very low count means an
   incomplete calendar or sparse bars.
2. **Skips and exits.** Inspect `buy:insufficient_cash`, `sell:no_bar_delisted_or_suspended` or
   `no_proven_universe_hold_unchanged`; a skip is part of the result, not automatically a bug.
3. **Unpriceable sessions.** If `unpriceable_sessions` is non-zero, equity is unknown there; do not
   compute a Sharpe over unknown points without deciding how to treat them.
4. **Charges.** Every trade stores `cost`; a `full_charges` run showing zero is a bug, and a
   `zero_cost` run must show zero. The stack is brokerage, STT, stamp duty, exchange charges,
   GST and slippage.
5. **Then metrics.** The CLI stops at total return; it does not compute CAGR, annualised volatility,
   Sharpe or alpha. Use the `--out` ledger if needed, but use the research definitions to compare.

Read [CSV_COLUMNS](../research/CSV_COLUMNS.md), [REPORT](../research/REPORT.md),
[SWEEP_REPORT](../research/SWEEP_REPORT.md), [STATUS](../STATUS.md) and
[DISCLAIMER](../DISCLAIMER.md) before relying on a number.

> ### What this tutorial does not prove
>
> - **A clean run is not alpha.** It can still be noise, a data artefact or a cost assumption.
> - **The pre-specified primary is not statistically significant.** Against the NIFTY 50,
>   Newey-West(5) t = 1.62 and block-bootstrap p = 0.096; the data cannot distinguish it from
>   noise at 5%.
> - **968 configurations were tested without a multiple-testing correction.** The best row is
>   a maximum, not an expectation.
> - **A CLI run is not the published study.** `universe` is a label; the research harness applies
>   point-in-time membership and computes alpha.

## 11. Common failures

| Symptom | Cause | Remedy |
|---|---|---|
| `no adjusted bars file found.` | No pack under `./data`, `NSE_DATA_DIR` unset or wrong, or `--pack` missing. | Fetch and verify the pack, set `$env:NSE_DATA_DIR`, or for the synthetic demo pass its flat `--pack`. If you have no pack, do not cite the output. |
| `No module named nse_pipeline` | `src` is not on `PYTHONPATH` and the project is not installed. | `$env:PYTHONPATH='src'` or `uv pip install -e .`. |
| `unknown field(s): ...` | Typo or unsupported key. | Remove or rename it. The error lists valid fields; unknown fields fail closed. |
| `start: '...' is after end '...'` | Inverted window. | Swap or widen the dates. |
| `cadence: 'weekly' is not valid` | Unsupported cadence. | `monthly`, `quarterly`, `semiannual` or `annual`. |
| `direction: 'up' is not valid` | Unsupported direction. | `top` or `bottom`. |
| `cost_model: 'cheap' is not valid` | Unsupported cost model. | `full_charges` or `zero_cost`. |
| Streamlit missing | `lab` extra not installed. | `uv sync --extra lab`, or `uv pip install -r apps/lab/requirements.txt`. |
| `nse-alpha` not found | Console script not installed. | `uv pip install -e .`, or use `python -m nse_pipeline`. |
| CLI reads no bars from the official pack | `--pack` points at the nested pack root. | Use `$NSE_DATA_DIR` or `./data`; `--pack` expects flat bars. |

## 12. Next steps

**Sweep a grid.** After `uv pip install -e .`, run all four shards before merging:

```powershell
uv run nse-alpha sweep run --shard 0 --shards 4
# run shards 1, 2 and 3 the same way
uv run nse-alpha sweep merge
```

The command calls `research/run_sweep.py` and `research/merge_sweep.py`. **The published
968-row sweep is not byte-for-byte reproducible from the public checkout:**
`research/run_sweep.py` reads the deliberately unexported `verification/` payload, and
`research/benchmark-series.json` is absent. Use the sweep for local sensitivity, not as
regeneration of the published table.

**Open the UI.** `uv sync --extra lab`, or `uv pip install -r apps/lab/requirements.txt`, then:

```powershell
uv run streamlit run apps/lab/app.py
```

Open http://localhost:8501. The UI is a research preview, launched and captured with Playwright
for `docs/assets/ui/`; there is no automated browser test suite. Its pack detector looks for a
DuckDB file or the older partition layout, so **it does not yet recognise the 1.0.0 pack's
`processed/adjusted/` tree.** Use the CLI for a real pack; the Compare page still reads the
published CSV.

**Quote only after reading.** The pre-specified primary is **not statistically significant**:
Newey-West(5) t = 1.62, block-bootstrap p = 0.096. The sweep tested **968 configurations** with
**no multiple-testing correction**; read the reports before quoting.
