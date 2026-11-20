# 2000–2026 price/volume research

**Fundamentals are deferred. No alpha or complete-history claim is implied.**
The retained price snapshot ends **2026-11-06**. "2026" means the available
part of that year, not future sessions through December.

## Current entry points

From the repository root, using the existing environment:

```powershell
.\.venv\Scripts\python.exe src/get_data.py readiness --start 2000-01-03 --end 2026-11-06
.\.venv\Scripts\python.exe src/get_data.py dataset
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

`readiness` writes `data/reports/full-history-readiness.json` and exits **2**
while blockers remain. It is deliberately separate from the old raw-builder
`readiness.json`. Missing evidence never becomes zero missing events.
The manifest prefers the canonical scheme ledger and reports verified document
terms separately from executable events. It no longer hard-codes "0 verified"
or a cutoff unrelated to the actual database.

## Current CLI

`src/get_data.py` is the only entry point. These are the current commands for
the 2000–2026 repair work.

### `repair` — rebuild canonical evidence and the gate

```powershell
python src/get_data.py repair [--start 2000-01-03] [--end <cutoff>] [--apply-adjustment]
```

Runs, in order: canonical schemes → verified actions → (optionally) the
split/bonus adjustment export → readiness → the integrated dataset manifest.
Without `--apply-adjustment` the adjusted bars are **not** rebuilt, so the
action evidence is regenerated while the factor export stays as it is; with
the flag the export is rebuilt from the database (read-only on
`market.duckdb`). `repair` exits **2** while readiness blockers remain.

### `history` — bounded raw-share diagnostic (strict by default)

```powershell
python src/get_data.py history [--allow-incomplete] [--out DIR] [--cash 10000000] [--top-n 20]
```

`history` is **strict by default**: it re-checks readiness first and **refuses
to run while blockers remain**. `--allow-incomplete` is **diagnostic consent
only** — it records that the run is knowingly incomplete; it is never a
completeness waiver and it never converts a missing fact into a filled one.
Unknown terminal prices, unresolved schemes and identity gaps stay unknown and
are audited rather than settled. Outputs (equity curve, signals, trades,
ledger, impact audit, positions) are written under
`data/processed/backtest/full_history_research/` unless `--out` says
otherwise; `--cash` sets initial cash and `--top-n` the position count.

### `attachments` — bounded scheme-document ingestion

```powershell
python src/get_data.py attachments [--fetch] [--symbols S1 S2] [--years 2024 2025]
```

Scans the cached scheme PDF/ZIP manifest. `--fetch` enables **bounded** NSE
network discovery/downloads (`--max-requests`, `--max-symbols`,
`--max-attachments`). Ingestion is not verification: an extracted ratio is
evidence to be read and confirmed, never an automatically promoted term.

### `sensitivity` and `freeze` — diagnostics, strict by default

```powershell
python src/get_data.py sensitivity [--allow-incomplete] [--out DIR] [--max-variants 18]
python src/get_data.py freeze
```

`sensitivity` runs a grid over momentum lookback (21/63/126 sessions), position
count (10/20/40) and unresolved-event exclusion policy (audit-only vs
quarantine-around-events), and reports how much the measured behaviour depends
on those analyst choices. `freeze` explains *why* trading stops. Both are
**strict by default** and refuse while blockers remain; `--allow-incomplete` is
diagnostic consent only.

Both produce incomplete-data diagnostics. They publish **no equity total that
includes an unpriceable holding**, and no output claims alpha, unbiasedness or
a total return. Outputs land in `data/processed/backtest/sensitivity/` and
`data/processed/backtest/freeze/`, with reports
`data/reports/backtest-sensitivity.{json,md}` and
`data/reports/backtest-freeze-diagnostic.{json,md}`.

### `readiness` and `dataset`

```powershell
python src/get_data.py readiness [--start 2000-01-03] [--end <cutoff>] [--universe research|nifty50|nifty500]
python src/get_data.py dataset
```

`readiness` prints the gate and exits **2** while blockers remain; `dataset`
refreshes `data/processed/dataset/dataset_manifest.json` from the current
artifacts.

**Fundamentals are deferred.** None of these commands requires filing
fundamentals, because the strategy is price/volume only. Deferring
fundamentals does **not** defer dividends, corporate actions, security
identity or terminal proceeds.

## Current coverage snapshot

The JSON reports are authoritative; the table below only reads them (price
snapshot ends 2026-11-06).

| Area | Current figure | Source |
|---|---:|---|
| Prices | 10,792,672 rows / 6,677 sessions | `full-history-readiness.json` |
| Split/bonus actions | 364 terms verified, 343 execution-ready, 342 applied; 2,105 applied in total | `verified-actions.json`, `adjustment-coverage.json` |
| Adjusted bars | 1,770,456 rows with factor ≠ 1, 1,033 symbols | `adjustment-coverage.json` |
| Dividends | 46,137 distinct; 31,987 included; 14,150 excluded | `dividends-coverage.json` |
| Identity | 1,071 official edges + 9 API-derived (1,080 combined; 1 refused for an undocumented handover ratio); 1,384 unresolved; 492 resolved observations; 0 of 328 terminal proceeds resolved | `identity-coverage.json`, `identity-api-edges.json` |
| Calendar | 86 unresolved (85 within the observed span); 274 confirmed closures; 2,705 unassessed weekends | `calendar-remediation.json` |
| Schemes | 396 events; 25 candidate terms; 0 execution-ready | `canonical-schemes.json` |
| Readiness | 7 blockers; `ready_for_backtesting` false | `full-history-readiness.json` |
| Full-history diagnostic | 6,677 sessions; 203 trades; 143,876 impact-audit rows; final equity unknown | `data/processed/backtest/full_history_research/summary.json` |
| Sensitivity grid | 18 variants; strict equity unknown on 89.8–97.5% of sessions; 0 of 18 with a final equity | `backtest-sensitivity.json` |
| Freeze diagnostic | 4.11% of variant-sessions (4,945 of 120,186) have a knowable equity; 98.06% of no-order execution sessions are frozen by an unpriceable holding | `backtest-freeze-diagnostic.json` |
| Freeze cause | 98.80% missing data; 1.20% cross-series proxy only (never a mark) | `backtest-series-fallback.json` |
| Identity | 856 of 1,559 key-stabilization candidates proven; 703 refused with a reason | `identity-key-stability.json` |

These are coverage facts, not performance facts. They do not make the history
complete, and they support no alpha, unbiasedness or complete-history claim.

## What the diagnostics established about the strategy

The `history`, `sensitivity` and `freeze` diagnostics answer a question the
coverage tables cannot: **can the strategy actually be measured over
2000–2026?** The answer today is no, and the reason is specific.

- The strategy stops trading within the first one to two years. Across the 18
  grid variants the last executed trade falls between 2000-11-01 and
  2008-11-03, against a window that runs to 2026-11-06. For the focus
  variant `L21-N10-audit_only` the longest zero-trade streak is **4,482
  sessions (2008-10-03 to 2026-11-06)** and 285 of 322 rebalances produced
  no order.
- The cause is not a lack of opportunities. Once **one** held security stops
  being priceable in the date-local research universe, the book's equity
  becomes unknown, so no new position can be sized and the unpriceable holding
  can never be sold. The book can only shrink, and then it is frozen.
  `skip_buy_unknown_held_opening_value` is the largest skip bucket in every
  variant. Of 5,568 no-order execution sessions across the grid, **98.06% are
  this freeze**; the remaining 108 are the six warm-up sessions per variant
  before the first trade. **Zero** circuit, illiquidity or quarantine refusals
  independently caused a no-order session.
- **2009–2026 contains no session with a knowable equity in any of the 18
  variants.** 2000 is 66.8–94.4% knowable and 2003–2005 are 0% for every
  variant, so the measurable part of this history is a 2000–2002 fragment
  with a hole in the middle.
- The dominant reason a holding becomes unpriceable is
  `no_bar_in_date_local_eq_research_universe` (141,930 of 142,246 unpriceable
  position-days): the security leaves the `EQ`-series research universe and
  never returns. The 24 frozen securities split into 15 `data_gap_no_bars`,
  7 `left_eq_universe_series_migration` and 2 `identity_key_change`.
- The identity problem in this ledger is **not** a mismatched key: under the
  runner's own key derivation every position's held key resolves to its row.
  Three holdings are booked under a rename root rather than the ticker their
  bars carry (`BOSCHCHASY` under `KALYANBRAK`, `HINDUJAFO` under `ENNOREFO`,
  `ROOFITIND` under `RATNABLDG`) and the dated rename walk bridges them. The
  problem is a **missing verified edge**, not a wrong key.
- The equity series is therefore **unknown**, not low, on 89.8–97.5% of
  sessions, and no variant has a final equity. Terminal proceeds are
  unresolved for all 328 delisted rows, so the exits that would end the freeze
  cannot be reconstructed.
- A concrete root cause, and a defect in this repository's own identity
  layer: the security that froze `L21-N10-audit_only` is `REIAGRO`, whose last
  EQ bar is 2008-09-19 — identical to its last knowable-equity session. The
  official delisted list records the same company as `REIAGROLTD` (delisted
  2018-09-11), and **no verified identity edge binds the two**, because the
  official `symbolchange.csv` has no row for that rename. NSE's own
  corporate-actions API does record both symbols for "Rei Agro Limited" with
  the ISIN change bridged by a documented 10:1 face-value split
  (`Fv Split Rs.10/- To Re.1/`, ex-date 2009-02-05). The identity layer read
  only `symbolchange.csv`, so a rename that is provable from official NSE
  data was never applied. 5 of the 58 frozen symbols
  (`CENTEXT`, `NICCOCORP`, `REIAGRO`, `STERLITE`, `TVSELEC`) have such an
  official-API successor, covering **15,639 unpriceable position-days
  (11.0%)**; the other 47 symbols (87.4%) have no API-named successor and
  would need scheme terms, terminal proceeds or other external evidence.
- The unpriceable book is disclosed, not valued: the final-session magnitude of
  the unpriceable positions ranges from 190,130 (L21-N10-audit_only) to
  1,176,636 (L126-N10-quarantine), with last-observed marks up to 9,544 days
  stale. Those figures are an exposure disclosure, not a portfolio value.

**Consequence for the alpha question.** A 2000–2026 return for this strategy
cannot be computed today, because the measurable part of the history is
2000–2002 at best and even that is interrupted by unknown-equity sessions.
The productive next step is therefore not more strategy work but closing the
instrument-identity and terminal-proceeds gaps that end the freeze. Until the
freeze is broken, any "alpha" figure would describe a 1–8 year fragment, not
the requested window.

### An EQ→BE fallback would not fix this

The obvious shortcut — price a holding from another series — is not
available on this data. Every unpriceable position-day is classified in
`data/reports/backtest-series-fallback.{json,md}`:

| cause | position-days | share |
|---|---:|---:|
| `price_history_ends_security_not_in_delisting_list` | 138,118 | 97.10% |
| `price_history_ends_and_symbol_delisted` | 2,170 | 1.53% |
| `cross_series_other_isin_only` (a **proxy**, never a mark) | 1,705 | 1.20% |
| `invalid_or_missing_close` | 253 | 0.18% |
| `identity_key_mismatch` | 0 | 0.00% |

Read the other way, this is **140,541 position-days (98.80%) of missing
data** against **1,705 (1.20%) of instrument-classification boundary**. Only
those 1,705 have a bar for the same symbol in another series, and every one
of them has a different or unobservable ISIN — a **proxy**, not a valuation.
That is structural: all 2,660,766 pre-2011-06-22 EQ bars carry no ISIN at
all, so the same-instrument test cannot even be evaluated in the window that
holds the freeze. **97.10%** of the freeze is a price history that simply
ends.

Within the "no row at all" bucket, **137,474 (97.99%)** are sessions on which
the security's bars never appear again, and **2,814 (2.01%)** are sessions
*before* a bar reappears — a suspension or a data gap rather than a terminal
end. That 2,814 is the only part of the freeze that better source coverage
alone could recover.

The attribution reconciles exactly with the runner's own counters
(`invalid_close` 22 = 22; `quarantined` 294 = 231 row-closeable-but-policy-
withheld + 63 no-row-at-all; `blocked` 0 = 0; `no_bar` 141,930 + 63 =
141,993), because both sides use the runner's own key derivation and the held
key is recovered by replaying the trade ledger rather than guessed.

### Identity key stabilization (done, and measured as no gain)

Bars carry no ISIN before 2011-06-22, so a security's key used to flip from
`SYMBOL:` to `ISIN:` mid-life, stranding positions bought earlier. A
per-symbol stabilization map now gives **856 of 1,559** candidate symbols one
proven key for their whole life, built from the bars themselves with **no
price** used. **703 are refused** with the reason recorded: 343 on an official
identity edge, 272 with more than one ISIN, 39 with a handover gap over 10
sessions, 35 with a non-INE ISIN, 8 whose ISIN another symbol claims, and 6
observed under two official company names.

The fix removed **853 key-flip impacts** (2,014 → 1,161 rows) and cut
`symbol_fallback_identity_not_verified` signals from 1,778 to 1,009 — the
defect is genuinely fixed.

**Measured measurability gain: zero.** Known-equity variant-sessions stayed
at 4,945 of 120,186 (4.11%), unpriceable position-days at 142,246, and
variants with a final equity at 0 of 18. The reason is in the cause table
above: the two securities this cause froze (`HINDZINC`, `MADRASFERT`) account
for only 3 position-days, while the freeze is 97.10% end-of-price-history. No
safety rule was relaxed to produce a better number.

### What would break the freeze, in order of leverage

1. **Terminal proceeds for delisted holdings** — currently 0 of 328 resolved.
   Without verified consideration the exit cannot be modelled, so the equity
   series cannot be completed. This needs per-delisting documents (cash/stock
   consideration, ex-date, citation).
2. **Scheme terms and successor identities** — for holdings that ended because
   of a merger, demerger or capital reduction the terms and the successor's
   listing date are still unresolved (396 scheme events, 0 execution-ready).
3. **Security master for the unobserved delisted symbols** — 31 delisted rows
   have no bars observation at all.

### Identity edges from the official corporate-actions API (done, and measured)

NSE's official `symbolchange.csv` is **incomplete**: it has no row for the
rename `REIAGRO` → `REIAGROLTD`, although NSE's own corporate-actions API
records both symbols for "Rei Agro Limited" with the ISIN change bridged by a
documented 10:1 face-value split. The identity layer now also reads that API
and derives **9 additional edges** (8 same-ISIN, 1 split-bridged), all of them
new coverage absent from `symbolchange.csv`. The original official edge file
is untouched and the combined set keeps each edge's provenance
(`edge_source`).

Each API edge must clear the same fail-closed gate as an official edge before
it may remap a key: a dated step, no symbol reuse, no branch or merge, an
ordered bars handover, and a share count that is provably continuous across
the handover. The remap boundary is the **observed bars handover**, not the
API feed date, because the feed dates the successor's first *action*, which is
only an upper bound.

**The `REIAGRO` edge is refused, deliberately.** The company continuity is
proved; the share exchange **ratio at the handover is not documented** (the
raw 2008 API extract has zero rows for either symbol, and the last
`REIAGRO` close of 950.65 against the first `REIAGROLTD` open of 480.65 flags
that a share-count change occurred — a price observation that must never
become a ratio). The documented 10:1 split is dated 2009-02-05, on the
*successor's* side of the handover, so it does not document the handover
ratio. The edge is therefore reported with
`successor_continuity_proved_but_exchange_ratio_undocumented`, the holding
stays unpriceable, and its equity stays unknown.

**Measured effect: none, and that is the correct result.** The 8,980
`REIAGRO` position-days did not become measurable, because carrying the
position across would require the undocumented ratio. The 8 admissible
same-ISIN edges are applied but change nothing measurable: their symbols were
never held across a handover, and the freeze stops all trading in September
2008, before any of their 2010 handovers.

So the API-edge work adds **coverage of provable identity** but recovers **no
measurability**. Of the 142,246 unpriceable position-days, **15,639
(10.99%)** are reachable with a verified dated identity edge; the other
**126,607 (89.01%)** need terminal proceeds or scheme terms that no official
input in this repository records — which are document and vendor tasks, not
code tasks.

## Repairs and their limits

- Opening orders must not depend on that session's future close. Portfolio
  sizing now uses the open (or the last earlier mark for absent securities).
- Momentum uses configured liquidity/history filters, and the economic minimum
  price is the contemporaneous **raw** price, not a retrospectively adjusted level.
- Future ETF/fund classifications no longer erase earlier rows by symbol.
  Null-ISIN stock classification remains an explicit research heuristic.
- Duplicate security/session bars fail rather than overwrite fills; NumPy false
  validity flags fail execution; stamp duty is buy-only.
- Constant costs and flat circuits remain approximations, **not** an authenticated
  2000–2026 historical tax schedule or a fill guarantee. Daily volume/OHLC checks
  cannot prove that an opening order was executable.
- Default paths in refactored archive/dataset modules now resolve to repository
  `data/`, not `src/nse_pipeline/data/`.

Old `backtester-design.md`, `backtester-total-return.md`, and saved comparison
numbers predate these corrections. Keep them as historical artifacts, **not** as
current evidence of performance. New runs must use separate output directories.

## Corporate actions: evidence is not execution

Every usable event needs its actual security/share class, exact NSE trading
ex-date, explicit ratio/cash terms and provenance. Board, NCLT, record, allotment,
announcement and appointed dates are different concepts. A date near a price
break is not enough. Historical annual reports can verify terms without proving
the exchange ex-date; a price discontinuity must never supply the factor.

Scheme verification batches contain multiple legs, repeated lifecycle filings,
unlisted counterparties and no-consideration subsidiary mergers. A ratio in a
filing does not necessarily entitle the publicly traded filer's shareholders.
Do not merge on symbol alone, attach later schemes to earlier same-symbol
events, or guess a successor ticker from a company name.

Unresolved schemes need both a **signal-contamination check over the complete
lookback** and a held-position policy. Excluding new buys for five sessions does
not fix an existing holding. Excluding companies using future knowledge is
itself selection bias. Missing successor prices need pending entitlements and
valuation uncertainty, not fabricated liquid cash. In particular, an event-day
close cannot be credited as spendable cash before that day's opening orders.

ZIP/OCR tooling reduces document reading effort; it does not authorize automatic
promotion of ratios. Verify the source page, ratio direction, class, effective
leg and final/withdrawn lifecycle with an independent reading.

## External data still needed

1. Exact pre-2011 action dates/identities and the unresolved 2000–2004 anomalies.
   The 1,012 discontinuities are a review pool, not 1,012 proven splits. A large
   move can be a real crash, penny-price noise, suspension/resumption or a source
   error. Do not "repair" all of them by smoothing returns.
2. Unresolved merger/demerger/capital-reduction terms, successor identity,
   entitlement/listing/cash-settlement dates and terminal proceeds.
3. Historical dividends and rights. **Deferring fundamentals does not defer
   dividends**: shareholder cash distributions affect realized strategy returns.
   Pre-2011 dividend coverage remains incomplete.
4. Exchange calendar/special-session evidence and dated security/asset-class
   identity coverage. Neither HTTP 404 nor a stale delisting list proves closure.
5. **Only if trading historical Nifty constituents:** licensed effective-date
   membership. See `docs/nifty-licensed-import.md`. The research universe does not
   require it. The corrected draft enquiry covers Nifty 50 before 2008-01-31
   and Nifty 500 before 2013-04-18; post-baseline snapshots still do not prove
   exact daily effective intervals. Nothing has been purchased or emailed.

Until those data are available and reconciled, full-window runs are explicitly
**incomplete-data diagnostics**. Passing tests or obtaining a positive CAGR does
not establish alpha. An investable claim additionally needs out-of-sample
validation, realistic costs/liquidity, capacity and sensitivity to unresolved
positions.
