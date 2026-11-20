# R2 — Impact of the look-ahead fix on the published results

**AUTHORITATIVE VERSION — measured against the CORRECTED (rename-aware) fix.**
Reader guidance:

- **Use the numbers in Part B (and the "Superseded" caveat below them).** They are the
  authoritative result.
- **Part A is SUPERSEDED.** It measured the first-generation strict fix (`chosen = None`;
  drop any symbol whose own ticker had no bar ≤ snapshot). That fix over-dropped rename
  cases and caused a real delta (N20 breadth −10.75 pp). The corrected fix keeps renamed
  securities and **every published trading metric is back to identical**.
- **Bottom line: the published results do not need regeneration.** The corrected fix is a
  no-op for every strategy number in the published artefact set. Only two *universe
  metadata/diagnostic* fields move (see B2).

Fix under test (current `src/nse_pipeline/backtest/momentum_grid.py::resolve_membership_to_keys`):
nearest-preceding bar for the snapshot's own ticker when it exists; otherwise fall back to
the symbol's canonical key and keep it **if that key already had a bar ≤ `as_of`**
(rename-aware); only drop and count when the canonical key itself has no history ≤ `as_of`.
New regression tests `tests/test_membership_no_future_mapping.py`: `5 passed in 0.48s`.

---

# Part B — AUTHORITATIVE: measurements with the corrected fix

## B1. Remaining leak census (real data, corrected function)

Method: real identity-resolved frame (5,804,413 rows, 3,503 keys, 2012-06-01..2026-09-30)
+ real NIFTY 500 snapshot chain (171 snapshots in window, 85,741 (symbol, snapshot) pairs).
Independent per-pair census plus the function's own `_unresolved_symbol_dates`.

**MEASURED output:**

```
frame 5,804,413 rows, 171 snapshots, 85,741 pairs
CORRECTED function: _unresolved_symbol_dates = 70
OLD behaviour: _unresolved_symbol_dates = 47

pairs examined=85,741 present=85,694 absent=47
OLD mapped a future key for        : 35 pairs
corrected keeps via rename fallback: 12 pairs on 12 dates
CORRECTED still drops              : 23 pairs on 16 dates, 15 symbols
date range of remaining affected   : 2023-07-20..2026-06-23
affected symbols: ['ABLBL', 'ADVENTHTL', 'ENRIN', 'ITCHOTELS', 'JIOFIN', 'KWIL',
'RAYMONDLSL', 'RAYMONDREL', 'SANOFICONR', 'SKFINDUS', 'TMCV', 'VAML', 'VEDPOWER',
'VISL', 'VOGL']
```

| quantity | old (published) | first strict fix | **corrected fix** |
|---|---:|---:|---:|
| pair maps to a future key | 35 | 35 | **23** |
| `_unresolved_symbol_dates` | 47 | 82 | **70** |
| distinct snapshot dates whose key set changes vs published | — | 27 | **16** |
| distinct symbols still dropped | — | 18 | **15** |
| date range | — | 2015-03-31..2026-06-23 | **2023-07-20..2026-06-23** |

- The 12 rename cases (IBVENTURES 2015-03-31, CASTEXTECH 2015-06-30, PATANJALI on 10
  dates 2022-09-30..2023-10-26) are **now correctly kept** via the canonical-key fallback:
  `12 pairs on 12 dates`.
- The 23 remaining drops are all genuinely future listings (e.g. JIOFIN 2023-07-20,
  RAYMONDLSL/SANOFICONR 2024-09, ITCHOTELS 2025-01-29, ENRIN/ABLBL 2025-06, TMCV/
  ADVENTHTL 2025-11, SKFINDUS 2025-12, KWIL 2026-02, VAML/VISL/VEDPOWER/VOGL 2026-06).
- **Critical check:** every one of the 23 remaining dropped keys has **no bar on its
  snapshot date** (measured: of the 35 originally dropped keys only 1 had a bar on its
  snapshot date — `ISIN:INE274G01010` on 2015-03-31 — and that one is now kept). The
  engine's `targets()` only ranks names observed on the signal session, so these 23 can
  never be selected. This is why no result moves.

Old-vs-corrected resolved-set diff: **16 dates changed, 23 keys dropped, 0 added**
(member counts fall by 1–4 on those dates; old 502..505 → new 501..502).

## B2. Flagship re-run with the corrected code

Re-ran `research/run_alpha_backtest.py` unmodified (only data paths/output redirected to
the real read-only store + scratch output), 435.7 s. Result JSON is 4,765,350 bytes —
**the same size as the published run's `strategy-results.json`**.

**MEASURED — every headline metric is zero-delta:**

| config | total return | CAGR | Sharpe | max DD | trades | alpha_linear | t_NW(5) | IR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| N10 top, no stop (**primary**) | 907.97% → 907.97% (0) | 19.296% (0) | 0.7782 (0) | −55.30% (0) | 2080 (0) | 10.6967% (0) | 1.6166 (0) | 0.3769 (0) |
| N10 top, 10% stop | 852.24% (0) | 18.779% (0) | 0.8385 (0) | −41.63% (0) | 2182 (0) | 12.0584% (0) | 1.9527 (0) | 0.3775 (0) |
| **N20 top no stop (breadth)** | **752.18% (0)** | **17.7765% (0)** | **0.7901 (0)** | **−56.708% (0)** | **3802 (0)** | **8.4824% (0)** | **1.5373 (0)** | **0.3734 (0)** |
| N20 bottom no stop | −64.01% (0) | −7.506% (0) | −0.1705 (0) | −91.60% (0) | 4457 (0) | −15.4569% (0) | −2.1305 (0) | −0.8745 (0) |
| N10 gross of costs | 1304.44% (0) | 22.357% (0) | 0.8696 (0) | −53.31% (0) | 2084 (0) | 13.2310% (0) | 1.9980% (0) | 0.5109 (0) |
| as-committed month-end | 1264.00% (0) | 22.084% (0) | 0.8560 (0) | −55.30% (0) | 1651 (0) | 12.9334% (0) | 1.9824 (0) | 0.4947 (0) |

For every one of the six configs: **equity curve differs on 0 of 3,300 sessions; every
metric equal at full Decimal precision; skip-reason counts, charges, drawdown episodes and
alpha statistics identical** (all `delta 0.00000000` in the alpha comparison).

Stronger still — comparing the corrected run with the published-source
`verification/research/alpha-backtest/strategy-results.json` document-by-document
(everything except `generated_at_utc`):

```
configs identical: True
calendar identical: True
identity identical: True
window identical: True
  universe.members_per_date: published={'min': 499, 'median': 501, 'max': 505} corrected={'min': 499, 'median': 501, 'max': 502}
  universe.unresolved_symbol_snapshot_pairs: published=47 corrected=70
```

**So the entire 4.7 MB flagship result document is byte-identical except two metadata
scalars**: `universe.unresolved_symbol_snapshot_pairs` (47 → 70) and
`universe.members_per_date.max` (505 → 502). No published trading metric, equity curve,
trade count, charge, drawdown or alpha statistic changes.

## B3. How many of the 968 sweep configs can still move

**MEASURED (static, real universes + `run_sweep.py` config grid and cadence selection):**

- **163 of 968 configs** have ≥1 affected snapshot date in their calendar (was 224 under
  the first strict fix): **151 monthly, 6 quarterly, 6 semiannual — all nifty500**.
  The other 805 configs (690 on msc400/midcap150/smallcap250/nifty50 + 115 nifty500
  annual/2y configs) touch no affected date at all.
- **Zero of the 163 can actually change a trade**, because all 23 remaining dropped keys
  have no bar on their snapshot date and `targets()` cannot rank a name with no bar on the
  signal session (verified for the flagship via 6/6 exact-equality re-run above; and
  previously via direct `MomentumRotationEngine.targets()` sweeps showing target lists
  change only on dates whose dropped key had a bar — of which none remain).
- Flagship-mapped sweep rows now touch 16 affected dates (month_end_exact: 0), but all are
  unselectable-key dates.

## B4. Recommendation (authoritative)

**Do NOT regenerate the published results. No errata on any number is required.** The
corrected fix reproduces every published strategy metric exactly (verified: 6/6 flagship
configs identical, full result document identical apart from two metadata scalars).

The only things that are stale under the corrected fix are **metadata/diagnostic fields**:

- `universe.unresolved_symbol_snapshot_pairs`: published 47, corrected 70.
- `universe.members_per_date.max` (and the `499–505` range text in `REPORT.md` line 55):
  published max 505, corrected max 502.
- `FINAL_RESULTS.csv` `universe_members_min/max` columns would shift on nifty500 rows if
  the table is ever rebuilt for another reason (currently 500/506 in the published CSV).

Suggested handling: a short note/errata on the universe-resolution diagnostic only (or fix
those fields the next time the pipeline is run for any other reason). **No regime of
regeneration — full, partial or config-level — is required for the published numbers.**
If the team wants belt-and-braces, re-run the nifty500 universe rows once (278 rows) and
confirm every `total_return_pct`/`sharpe_rf0` matches; the flagship-level evidence above
already predicts exact equality.

Also note the two prior conclusions change status:
- The earlier "N20 breadth −10.75 pp" delta was an artefact of the **over-dropping in the
  first strict fix**, not of the original leak. It is superseded (see Part A).
- The original leak's 23 genuinely-future mappings changed no holdings even under the
  first fix (the leaked keys had no bar on the signal session); the corrected fix
  eliminates them without touching any selectable member.

---

# Part A — SUPERSEDED: measurements with the first-generation strict fix

> **SUPERSEDED — do not use these figures for the published results.** These were measured
> against the intermediate `chosen = None` fix that dropped any snapshot symbol whose own
> ticker had no bar ≤ `as_of`. That version over-dropped 12 rename cases and *created* a
> real delta that the corrected fix removes. Retained for audit trail only.

## A1. Census (first strict fix) — SUPERSEDED

Real data, old function body extracted verbatim from the parent repo (body SHA-256
`6b558a71e621874c8729be448cebb0f733adbbfe06bf1647ff2627df7106a1a9`).

| universe | snapshots | pairs examined | affected pairs | affected snapshots | affected symbols |
|---|---:|---:|---:|---:|---:|
| nifty500 | 171 | 85,741 | 35 | 27 | 18 |
| msc400 | 57 | 22,800 | 0 | 0 | 0 |
| midcap150 | 57 | 8,550 | 0 | 0 | 0 |
| smallcap250 | 57 | 14,250 | 0 | 0 | 0 |
| nifty50 | 13 | 652 | 0 | 0 | 0 |
| **total** | | **131,993** | **35** | **27** | **18** |

- Earliest 2015-03-31, latest 2026-06-23. `_unresolved_symbol_dates` 47 → **82**.
- Resolved sets changed on 27 dates: 35 keys dropped, 0 added; member counts fell 1–4
  (max/date 505 → 502; min/median unchanged).
- 23 keys genuinely future; 12 keys had earlier bars under another ticker (rename cases);
  only 1 of the 35 keys had a bar on its snapshot date (`ISIN:INE274G01010`, 2015-03-31,
  `IBSEC`→`IBVENTURES`).
- Affected symbols: `ABLBL, ADVENTHTL, CASTEXTECH, ENRIN, IBVENTURES, ITCHOTELS, JIOFIN,
  KWIL, PATANJALI, RAYMONDLSL, RAYMONDREL, SANOFICONR, SKFINDUS, TMCV, VAML, VEDPOWER,
  VISL, VOGL`.

## A2. Flagship re-run (first strict fix) — SUPERSEDED

Re-ran `research/run_alpha_backtest.py` unmodified against the real data (414.8 s); 5 of 6
configs byte-identical to published; the breadth row moved:

| config | total return | CAGR | Sharpe | max DD | trades | alpha_linear | t_NW(5) |
|---|---:|---:|---:|---:|---:|---:|---:|
| N20 top, no stop (breadth) | 752.18% → **741.43% (−10.75 pp)** | 17.7765% → 17.6624% (−0.114 pp) | 0.7901 → 0.7861 (−0.0040) | −56.708% → −56.674% | 3802 → 3828 | 8.4824% → 8.3862% (−0.096 pp) | 1.5373 → 1.5185 |
| N10 primary, N10 sl10, N20 bottom, gross, month-end | no change (0/3,300 sessions) | — | — | — | — | — | — |

Mechanism (measured with the real `MomentumRotationEngine.targets()`): the N20/N25 target
list differed on exactly **1 of 171** rebalance dates — 2015-03-31, where the over-dropping
fix removed the already-trading `ISIN:INE274G01010`; N10 and N20-bottom differed on 0
dates. The delta was therefore caused by the fix's over-drop, not by the original leak.

## A3. Config impact (first strict fix) — SUPERSEDED

224 of 968 configs had ≥1 affected snapshot in their calendar (all nifty500: 152 monthly,
54 2y, 6 each annual/semiannual/quarterly); 744 could not move. This is superseded by
B3 (163 touch an affected date under the corrected fix, and none can change results).

---

## Evidence files (scratch, outside the repo)

`%TEMP%\r2impact\`: `census_new.py` + `measure-nifty500.json` (corrected census),
`strategy-results-fixed.json` (corrected flagship run), `compare-flagship.json`,
`alpha-results-fixed.json`, `compare-flagship-alpha.json`, `step2-configs.json`
(corrected), plus Part-A evidence (`measure-*.json` originals, `attribution-targets.json`,
`affected-key-lineage.csv`, `compare-sample.json`, `frame-meta.json`).
Nothing under `D:\fix_Investment_backtesting` was modified except this file.
