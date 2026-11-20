# NIFTY 500 / NIFTY 50 community point-in-time membership audit

**Audit date:** 2026-11-14  
**Scope:** free, publicly retrievable community datasets and a concrete press-release-based repair path. This is a research audit, not data ingestion. Candidate samples remain under `verification/stage1/batch7/deep-research/community/`; nothing was added to production data or `data/`. No code or tests were changed.

## Executive finding

There is no single free candidate that should be treated as a complete, exact, bitemporal NIFTY 500 history without qualification. The most useful immediate starting point is Aditya Jha's event-derived interval dataset, pinned at [`0786cd9`](https://github.com/aditya-jha/nse-historical-membership/tree/0786cd9159742f4f2012dcd5f01c1f224b0b019b). Its staged membership CSV has 6,670 intervals across 41 indices; the NIFTY 500 slice has 1,151 intervals, 970 distinct symbols, and 516 rows marked `snapshot_floor`. It is useful as a reproducible event-backed subset, but its interval table does not carry publication/`known_at` dates, and the inferred baseline rows cannot be promoted to historically knowable facts simply because later events let us walk backward from a current list.

A concrete free repair candidate is possible for part of that slice: connect interval opening and closing symbols to the repository's parsed press-release event JSON, and carry the `publication_date` from the matched event. On the staged NIFTY 500 CSV, exact `(index, symbol, action, effective_date)` matches to parsed JSON yielded **495 of 1,151 intervals (43.0%) with at least one endpoint carrying a non-null parsed publication date; 103 (9.0%) have both endpoints carrying one.** Another 432 have at least one matched effective-date event but lack a publication date on that parsed record. These are **recoverable-date candidates, not independently verified publication dates**: the underlying official PDFs still need to be checked. They are not a complete “what was knowable at every date” history.

The key discipline is endpoint-specific provenance: a `source_url` on an interval supports the event that opened it. A note such as `closed by ind_prs....pdf` points toward a candidate closing event; it does **not** itself provide that closing notice's publication date. The closure must be joined to that separate parsed event and matched on the excluded symbol and effective date. Likewise, a full census inferred at the baseline date is not established by future reverse-backtracking.

## Method and reproducibility

### Pinned source and staged samples

All primary repository claims below use immutable commit links. The staged Aditya data was downloaded from that pinned tree:

- Membership: [`index_membership_history.csv`](https://raw.githubusercontent.com/aditya-jha/nse-historical-membership/0786cd9159742f4f2012dcd5f01c1f224b0b019b/index_history/data/index_membership_history.csv)
- Current NIFTY 500 seed: [`nifty500.csv`](https://raw.githubusercontent.com/aditya-jha/nse-historical-membership/0786cd9159742f4f2012dcd5f01c1f224b0b019b/index_history/data/current_snapshot/nifty500.csv)
- Parsed notices: [`index_history/data/parsed/`](https://github.com/aditya-jha/nse-historical-membership/tree/0786cd9159742f4f2012dcd5f01c1f224b0b019b/index_history/data/parsed)
- Owner README: [pinned README](https://github.com/aditya-jha/nse-historical-membership/blob/0786cd9159742f4f2012dcd5f01c1f224b0b019b/README.md)
- Code and data licenses: [MIT code license](https://github.com/aditya-jha/nse-historical-membership/blob/0786cd9159742f4f2012dcd5f01c1f224b0b019b/LICENSE), [CC0 data license](https://github.com/aditya-jha/nse-historical-membership/blob/0786cd9159742f4f2012dcd5f01c1f224b0b019b/LICENSE-DATA)

Hashes below are SHA-256 of the staged bytes, not Git blob IDs. CSV row counts exclude the header.

| Staged file | Rows / shape | SHA-256 | Notes |
|---|---:|---|---|
| `aditya_membership.csv` | 6,670 rows; 41 indices; 1,348 distinct symbols | `79a46c7cdd171ca47bc5168f1aa1c4fc9e84b48ada428e9191ccf8017cbe0ee` | Same bytes as the pinned headline membership CSV. |
| `aditya_nifty500_snapshot.csv` | 501 data rows; 501 distinct symbols | `0ea3b59173bc0b46669a8a7285cb0d55c184b01dbe38691d51a931619bd5e5aa` | Measured row/symbol count; snapshot is a seed/current-list fact, not a historic baseline archive. |
| `prathmesh_nifty500.csv` | 131,271 rows; 261 distinct month-end dates | `3c03b45fb5da1c9596d9aaf7a51f39a336c8cc2772a2fb4c7410f3c9b0c9ac15` | Monthly fund-holdings-derived/reconstructed membership proxy; see caveats below. |
| `rishabh_membership_calendar.csv` | 1,428 rows; 1,111 distinct symbols | `6e63daff6396b8adbe8f72fbb2a40a01a93a378959a68db2128d9e62bca8c717` | Snapshot-patched calendar. |
| `rishabh_events.csv` | 2,397 rows; 240 distinct event dates | `1a416353c248e96bacded5787de09b06b33756a9416f8ea14641e4ee2c6224b1` | Includes multiple event-source types. |
| `vishal_nifty50.csv` | 216 rows (months); 106 columns | `47e2eeddab49d13b8083fe061c3b418bb960e196c48d063bc003d49fe10d4dff` | Monthly binary NIFTY 50 reconstruction, not NIFTY 500. |
| `vishal_nifty500momentum50.csv` | 25 rows (months); 56 columns | `37afd7fbea8714cfe2ab57f7219e5699bf94193263e947e8938136fb9f71f335` | NIFTY 500 Momentum 50 is a different index; `EXCLUSIONS:` also appears as a column. |
| `bkkb_final.xlsx` | 717,791 bytes; `FLAT_LIST` has 11,709 data rows | `dc43dbcd473a8bc36c333c9fc61be9779affee0e59aca88e884517c00a3cb7fd` | Inspected directly with `zipfile`/XML; `NIFTY 500` contributes 2,003 rows across four dates, `2006-02-02` to `2026-03-29` (501, 501, 501, and 500 rows respectively), with 501 distinct symbols. The `TODAY` sheet dimensions are `A1:AB501`; no quality conclusion is inferred from dimensions alone. |

### NIFTY 500 interval census in the Aditya sample

The local slice is `index_name == "Nifty 500"` from the staged CSV. The reported interval convention is half-open: **`[valid_from, valid_to)`**. The repository README explicitly documents this and its query uses `valid_to > date`, not `>=`.

| Measure | Observed |
|---|---:|
| Interval rows | 1,151 |
| Distinct symbols | 970 |
| Effective-date span represented | `2014-01-01` to `2026-09-30` |
| `press_release` rows | 632 |
| `snapshot_floor` rows | 516 |
| `merger` rows | 2 |
| `snapshot` rows | 1 |
| Rows with opening `source_url` | 634 (632 `press_release` + 2 `merger`) |
| Rows without opening `source_url` | 517 (516 `snapshot_floor` + 1 `snapshot`) |
| Distinct opening source URLs | 83 |
| Concurrent symbols on `2014-01-01` | 516 |
| Concurrent symbols on `2018-04-02` | 503 |
| Concurrent symbols on `2020-09-14` | 504 |
| Concurrent symbols on `2026-09-30` | 499 |

The 499 at the final represented date is not a claim that the official target count is 499: it is the size of this reconstruction's active set. The pinned README itself warns that the current NIFTY 500 table has 499 entries matching the then-published list and documents pre-2018 cardinality drift. The sample's very large 2014 floor count also means a valid-looking interval start at `2014-01-01` must not be interpreted as proof of a complete, point-in-time 2014-01-01 census.

### Official-host archive comparison: candidate, not ground truth

A host-produced comparison is recorded at [`verification/stage1/batch7/deep-research/host-archive-baseline-comparison.json`](../../verification/stage1/batch7/deep-research/host-archive-baseline-comparison.json). Its metadata says it compared 11 official-host archived CSV observations with the local NIFTY 500 CSV using the documented half-open interval rule, and that its `baseline_sha256` matches the staged community CSV hash above. **The host reported exact symbol-set agreement in 0/11 observations**; intersections range from **460/501** at the 2018-10-04 capture to **497/501** at the 2026-09-29 capture. This is material contrary evidence against treating the community file as ground truth, even though overlap improves in later observations.

Interpret this as a reconciliation signal, not a verdict about which list was effective on the capture date: the JSON explicitly labels capture dates as observations only, not effectivity, and notes that no aliases or dummy/DVR symbols were silently removed. Some differences may be explained by symbol/issuer identity changes, DVR or dummy lines, but that is not established for every mismatch. **Do not auto-map identities or assume such explanations account for the full discrepancy.** Keep the official archived symbols and community interval symbols side by side, investigate each difference against dated official notices and security-master evidence, and preserve unresolved disagreements. The host artifact is a pre-existing measurement; this audit cites it without recalculating it.

### Direct publication-date repair census

I retrieved the JSON files linked by either the interval opening `source_url` or a `closed by <pdf>` note, at the same pinned commit. For each NIFTY 500 interval:

1. Opening match: find a NIFTY 500 parsed event whose `included` list contains the symbol and whose `effective_date == valid_from`.
2. Closing match: if `valid_to` is non-null and the note names a closing PDF, find that PDF's NIFTY 500 parsed event whose `excluded` list contains the symbol and whose `effective_date == valid_to`.
3. Count a directly recoverable publication date only when that exact endpoint event has a non-null `publication_date` in its parsed JSON. Do not fill a missing date from the PDF filename or a neighboring event.

This produced the following endpoint-level counts:

| Endpoint evidence | Count of NIFTY 500 interval endpoints |
|---|---:|
| Opening endpoint matched to an exact parsed event/effective date | 620 / 1,151 |
| Of those, event has non-null `publication_date` | 299 / 1,151 |
| Closing endpoint matched to an exact parsed event/effective date | 614 / 1,151 |
| Of those, event has non-null `publication_date` | 299 / 1,151 |
| Intervals with at least one exact endpoint event, regardless of publication date | 927 / 1,151 |
| Intervals with at least one exact endpoint whose parsed event contains a non-null publication date | **495 / 1,151 (43.0%) recoverable-date candidates** |
| Intervals with both opening and closing endpoints whose parsed events contain dates | **103 / 1,151 (9.0%) candidates** |

These endpoint rows overlap: 299 opening and 299 closing dates do not sum to 598 distinct intervals. In particular, **516 intervals are labeled `snapshot_floor`**, and none gets a directly dated opening event from this matching procedure. A subset of those floor rows does have a directly dated closing event: a closing notice can establish when that membership ended, but cannot retroactively establish when the member entered or the complete baseline composition.

The parsed JSON schema has both `publication_date` and `effective_date`, but the interval CSV schema does not have a `known_at` field. At this pinned revision some relevant parsed notices have null publication dates even where the effective date is known. For example, the pinned parsed record [`ind_prs27022014.json`](https://raw.githubusercontent.com/aditya-jha/nse-historical-membership/0786cd9159742f4f2012dcd5f01c1f224b0b019b/index_history/data/parsed/ind_prs27022014.json) has `effective_date: 2014-03-28`, `publication_date: null`, and separate included/excluded arrays by index. Conversely, parsed notices with a date can establish the announcement/publication date independently of their effective date.

**Interpretation:** these matches identify a publication-date *recovery candidate subset*, not yet an independently verified set for event-time knowability. Check each matched source notice against the official PDF before treating its publication date as verified. This does not establish a complete historical as-known universe. For a dated membership fact, retain at least `(index, symbol, effective_from/to, event_action, event_effective_date, publication_date, source_pdf_url, match_status)` and a distinct official-PDF verification status; preserve null publication dates as null. Never copy the opening notice's date to the closing edge.

## Candidate assessment and ranking

### 1. Aditya Jha — best practical open starting point, with explicit caveats

Pinned revision: [`0786cd9159742f4f2012dcd5f01c1f224b0b019b`](https://github.com/aditya-jha/nse-historical-membership/tree/0786cd9159742f4f2012dcd5f01c1f224b0b019b). It has a broad 41-index interval table, parsed primary notices, a current snapshot seed, documented half-open semantics, validation notes, and separate MIT (code) / CC0 (data) licenses. Its owner describes strong coverage from 2017 onward, broad-index press-release coverage from 2014, and pre-2018 cardinality drift; the README reports some internal consistency failures before 2018 and a current NIFTY 500 count of 499. The interval source categories show that baseline/floor inference is material, not incidental. The publication-date join above yields 495 intervals with at least one recoverable-date candidate, but those dates are not official-PDF-verified here and do not make the undated or floor rows bitemporal.

**Use:** event-by-event research where the event can be independently matched to the original parsed notice; explicitly exclude or flag floor/snapshot/inferred facts for strict knowability tests. **Do not:** assume all members at `2014-01-01` or another backtracked anchor were historically observable at that date.

### 2. Prathmesh Gupta — broad NIFTY 500 monthly coverage, but mixed-source proxy

Pinned revision: [`2cb41455d07418748f9bfce2039ca6d241fab011`](https://github.com/prathmeshgupta/nifty-pit-universe/tree/2cb41455d07418748f9bfce2039ca6d241fab011). Its README claims 261 continuous monthly snapshots from 2005-01 to 2026-09 and 131,271 NIFTY 500 observations. The staged CSV confirms those row/month counts and has `as_of_date` and `pit_available_date`, plus issuer/ISIN fields. The README describes a mixed method: reverse-engineered official NSE reconstitution events through 2019-08, then Motilal Oswal NIFTY 500 fund disclosures from 2019-09 onward. It also describes starting the reverse-delta engine from a 2019-09 baseline. This is useful breadth and the disclosure-availability date is preferable to a bare month-end for some as-known analyses, but the stated `pit_available_date` is a monthly disclosure availability convention, not the publication time for each historical inclusion/exclusion event. The reported MIT license is a repository license; it does not by itself settle redistribution rights for source-derived or third-party fund-disclosure data. Validate baseline construction, monthly counts (the README reports 500–506), identity/ISIN transitions, and event dates before treating it as exact membership.

### 3. BKKB20 — worthwhile independent reconstruction, verification burden remains high

Pinned revision: [`208102da76dde2e4f3121cb5fbd26ea7def5a9ca`](https://github.com/BKKB20/nse-index-history/tree/208102da76dde2e4f3121cb5fbd26ea7def5a9ca). Its README claims 28 indices from 2004–2026, reverse-constructed from NSE circulars, and explicitly reports three unprocessed scanned 2018–2019 PDFs and weaker pre-2010 name/ticker mappings. This is valuable independent event coverage for cross-checking. I found no declared repository license at the pinned revision; the code and data should therefore not be assumed redistributable. The XLSX sample was staged and hashed, but its sheets/row counts were not independently measured here.

### 4. Rishabh Jain — useful recent interval patch, not event-exact in the snapshot era

Pinned revision: [`3eebab8368369d7f6805d3ad25c7bd6e5c4cb7db`](https://github.com/rishabhjain0295-web/nifty500-momentum-backtest/tree/3eebab8368369d7f6805d3ad25c7bd6e5c4cb7db). Its README describes using NSE's `IndexInclExcl.xls` through `2020-09-14`, then six Wayback snapshots (2020-07 to 2026-05) to infer changes within multi-month windows. The staged calendar contains 1,428 rows / 1,111 symbols; the staged events table has 2,397 rows. The README reports 318 unresolved historical names (about 25%) and says 2020–2026 changes are approximate. It is a potentially useful complementary patch, but snapshot observation dates are not the original announcement dates or necessarily the effective dates. No repository license was declared at the pinned revision.

### 5. Vishal VX — narrower binary snapshots

Pinned revision: [`2422d1460d33ea8cfd3ed2031473dce8df56f731`](https://github.com/vishalvx/nifty-indices-datasets/tree/2422d1460d33ea8cfd3ed2031473dce8df56f731). The repository README says NIFTY 50 monthly reconstruction begins in 2008, while NIFTY 500 Momentum 50 begins in 2024; it is not a NIFTY 500 constituent history. The staged files contain 216 monthly rows for NIFTY 50 and 25 for Momentum 50. The repository declares MIT for code, but that does not independently establish rights in underlying factual source data. Useful for secondary membership cross-checks, not as the primary NIFTY 500 source.

## Historical “current constituents” repository search

A periodically committed `ind_nifty500list.csv` can act as a dated snapshot only if commits genuinely preserve multiple versions. Commit history at the sampled paths found:

| Repository/path | Relevant history found | Assessment |
|---|---|---|
| [`chaitanyarahalkar/Financial-Info-Extractor`](https://github.com/chaitanyarahalkar/Financial-Info-Extractor) `ind_nifty500list.csv` | One data-file commit, 2018-06-17; repo MIT | One old snapshot, not a time series. |
| [`Hpareek07/NSEData`](https://github.com/Hpareek07/NSEData) `ind_nifty500list.csv` | One data-file commit, 2020-03-08; repo MIT | One old snapshot, not a time series. |
| [`kprohith/nse-stock-analysis`](https://github.com/kprohith/nse-stock-analysis) `ind_nifty500list.csv` | One data-file commit, 2019-06-14; no license declared | One old snapshot, not a time series. |
| [`Ayush21031/Finance`](https://github.com/Ayush21031/Finance) `ind_nifty500list.csv` | One data-file commit, 2024-07-06; no license declared | One old snapshot, not a time series. |
| [`varunsrik/nifty_dashboard`](https://github.com/varunsrik/nifty_dashboard) `data/nifty_500_constituents.csv` | Two commits, 2025-04-28 and 2025-04-30; GPL-3.0 repo | Too few close-together versions to establish a historical calendar. |
| [`ganeshbiyer/Nse_Historical_Data`](https://github.com/ganeshbiyer/Nse_Historical_Data) `nifty500_symbols.csv` | 28 commits between 2024-11-05 and 2025-12-31 | Potentially dated snapshots over about 14 months; still not event histories and needs per-commit diff/source validation. No license declared. |

This search included repositories with a checked-in current-list CSV and inspected per-path commit history; a filename containing `historical` is not itself evidence of historical membership. None of the sampled simple current-list repositories provides a long, validated snapshot series comparable to the event-derived candidates. Historical GitHub snapshots also encode commit time, not necessarily the NSE list's publication or effective date.

## PIT semantics and known-as-of warning

There are three distinct dates/concepts:

1. **Effective date:** when an index change takes effect; used in the membership interval.
2. **Publication/announcement date:** when the source notice was published; this can be earlier than the effective date and is relevant to whether the change was knowable.
3. **Snapshot or reconstruction anchor date:** the date of a current list used to initialize backward reconstruction; it does not prove the complete historical membership set was known at that date unless the source snapshot itself is archived and dated.

The Aditya CSV has effective intervals and source/provenance fields but no exported `known_at` or announcement-date column. A future press release closing an interval is evidence of an ending, not evidence that the interval's opening was announced then. A future current constituent list walked backward can also omit members that left before the seed and cannot, without an archived baseline, prove complete historical composition. Consequently, these community files can support **effective-date membership** more readily than **bitemporal, as-known membership**.

There is also a boundary discrepancy worth preserving in downstream work: the CSV owner's code/README define `valid_to` as exclusive (`[valid_from, valid_to)`), while the repository's existing test helper uses an inclusive end comparison. For example, a row ending `2022-09-30` is not active on `2022-09-30` under owner semantics, but is included by that helper. Do not silently normalize this conflict; use the owner's documented half-open rule for the community dataset and flag any repository test/query that differs.

## Official-source cross-checks and licensing

The event repair should use the official Nifty Indices/NSE-hosted press release linked by the parsed record and compare its stated publication and effective dates to the parser output. The local dataset's example `source_url` is the primary-host URL form, e.g. [`ind_prs20022015.pdf`](https://niftyindices.com/Press_Release/ind_prs20022015.pdf); the pinned parsed JSON records the URL, event action lists, publication date when parsed, and effective date. An additional representative official notice noted during this audit has publication date `2022-09-01` and effective date `2022-09-30`; another has publication date `2025-02-21` and effective date `2025-03-28`. The pinned parsed tree also has multiple notices for the same effective date (including `2025-03-13`, `2025-03-21`, and `2025-03-25` publications effective `2025-03-28`), so matching must use the interval's exact source notice / event symbol and action rather than effective date alone. These are different fields and must remain different in the repaired table.

License conclusions are deliberately narrow:

- Aditya's pinned repository explicitly separates MIT code from CC0 data.
- Prathmesh advertises MIT at repository level; upstream source material and third-party disclosure rights need separate review.
- Vishal's MIT code license is not by itself a data license.
- BKKB20 and Rishabh had no license declaration found at the pinned revision.
- A public GitHub download URL is repeatable access, not proof of permission to republish a dataset. Prefer storing source URLs, hashes and derived facts under the project's applicable policy rather than copying/re-hosting raw third-party material.

## Recommended practical path (free)

1. **Use Aditya's pinned event and interval artifacts as an auditable candidate, not an unquestioned truth set.** Preserve commit SHA, source file hash, interval source category, and half-open semantics.
2. **Build a sidecar event ledger from parsed JSON** for NIFTY 500: one row per notice × action × symbol with `publication_date`, `effective_date`, source PDF URL, parser filename, and raw JSON/hash. Keep unknown publication dates null.
3. **Join interval starts and ends independently.** Start must match an inclusion event and exact `valid_from`; end must match an exclusion event and exact `valid_to`, using the separate `closed by ...` notice only as a lookup pointer. Do not transfer one endpoint's source or date to the other.
4. **Partition confidence explicitly:** exact event + publication date; exact event but missing publication date; inferred/snapshot-floor; snapshot-derived approximate; unresolved/ambiguous. For strict as-known tests, only the first partition has a directly observable event date, and even there the event's symbol/index/date match should be checked against the official notice.
5. **Treat baseline membership as a separate problem.** Anchor the baseline only to an archived constituent snapshot with a defensible observation/publication date, or mark the baseline set as unknown. Do not claim that reverse-delta reconstruction establishes all members at the baseline date or before it.
6. **Cross-check transitions independently.** Use official Nifty Indices notices and, where available, a second reconstruction (BKKB20/Prathmesh/Rishabh) as corroboration rather than as a substitute for the primary record. Record disagreements instead of selecting a convenient source silently.
7. **Keep the repair non-production initially.** The current audit's samples remain in `verification/stage1/batch7/deep-research/community/`; promotion would require separate evidence, licensing and data-contract review.

## Bottom line

For free research, Aditya's pinned parsed notices plus the 495 intervals with at least one publication-date recovery candidate form the clearest near-term event-evidence lead found here; independently verify each official PDF before treating dates as confirmed. This does not establish a full PIT census: 516 rows start at `snapshot_floor`, source events have missing announcement dates, and the interval table lacks `known_at`. The host's 11-observation archive comparison—0/11 exact set matches, intersections from 460/501 to 497/501—is a reconciliation warning: it reinforces that this community CSV is a **lead, not ground truth**, but does not prove all interval membership is wrong. Some differences may be aliases, DVRs, or dummy lines; that possibility is not a blanket explanation. Keep mismatches unresolved until investigated, and do not auto-map symbols. Prathmesh offers much longer monthly reach but mixes event reconstruction with fund disclosures and a later baseline; Rishabh patches recent years using approximate snapshots; BKKB20 is a useful independent reconstruction with known gaps and no declared license. A responsible backtest can use these as layered evidence, but should label membership confidence and avoid claiming no-lookahead historical knowledge for inferred baseline or snapshot-derived rows.
