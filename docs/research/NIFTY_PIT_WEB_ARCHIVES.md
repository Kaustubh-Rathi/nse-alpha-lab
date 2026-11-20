# Free web archives for NIFTY 500 constituent lists

**Research run:** 2026-11-14. **Scope:** public, unauthenticated archive indexes and replayed files for exact, first-party NIFTY 500 constituent-list URLs. No access controls were bypassed. This establishes the existence of free archived observations; it does not establish a complete point-in-time (PIT) history.

## Finding

The prior blanket claim that no free archived official constituent files exist is **false**. The Internet Archive CDX index returned successful captures of the official `niftyindices.com` and NSE archive-host CSVs, and replayed files were downloaded and validated. I staged eleven distinct successful response bodies in `verification/stage1/batch7/deep-research/web-archives/`.

These are undated constituent-list files. A capture timestamp records when the archive observed a response/availability, **not** the membership effective date. None of the captures below may be represented as an official effective-date snapshot without separate date evidence. This distinction is also the explicit policy in [`src/nse_pipeline/nifty/alt.py`](../../src/nse_pipeline/nifty/alt.py): Wayback CSVs are `wayback_capture_observed`, and `as_of` means capture date only.

## Work already in the repository

`src/nse_pipeline/nifty/alt.py` already has the provenance-aware importer `import_wayback_csv`: it requires an official HTTPS original URL, a 14-digit capture timestamp, replay/source hash, and retrieval time; validates the expected five-column CSV shape and records `as_of_basis=wayback_capture_timestamp_not_document_date`. `docs/ARCHITECTURE.md` describes it as Wayback/Wikipedia alternative data, and the project README/audit distinguish Wayback from official dated membership. Before this research, `verification/stage1/batch7/` contained official dated membership PDFs/history and reconciliation artifacts, but no `deep-research/web-archives/` directory or these archived undated CSVs. No pre-existing web-archive CSV artifact was found there.

## Index queries and returned records

Queries are direct CDX API URLs; `collapse=digest` collapses repeated payloads, not capture dates. These results were observed during this run:

| Exact URL alias queried | CDX query | Successful distinct-payload captures returned |
|---|---|---|
| `niftyindices.com/IndexConstituent/ind_nifty500list.csv` (host/path-normalized family, including www and scheme variants) | [CDX query](https://web.archive.org/cdx/search/cdx?url=niftyindices.com/IndexConstituent/ind_nifty500list.csv&output=json&filter=statuscode:200&collapse=digest) | 11 returned 200 rows after digest collapse, timestamps 2018-10-04 10:56:32 through 2026-10-22 15:09:13 UTC. Captures: `20181004105632`, `20190201175107`, `20240207233933`, `20250616113621`, `20250815235304`, `20260107050129`, `20260502114023`, `20260827161651`, `20260921150535`, `20261009150703`, `20261022150913`. The uncollapsed query also returned 301 redirect observations (`2021-02-04`, `2021-02-08`, `2025-08-24`) and a 2026-10-16 `warc/revisit` row; these are not extra independently retrievable payloads. |
| `archives.nseindia.com/content/indices/ind_nifty500list.csv` | [CDX query](https://web.archive.org/cdx/search/cdx?url=archives.nseindia.com/content/indices/ind_nifty500list.csv&output=json&filter=statuscode:200&collapse=digest) | Four returned 200 rows, 2022-05-04 through 2024-02-26 (`20220504103923`, `20221009160959`, `20230404164710`, `20240226224931`). A later uncollapsed lookup showed a 2025-07-11 403 HTML response; excluded as an error body. |
| `nsearchives.nseindia.com/content/indices/ind_nifty500list.csv` | [CDX query](https://web.archive.org/cdx/search/cdx?url=nsearchives.nseindia.com/content/indices/ind_nifty500list.csv&output=json&filter=statuscode:200&collapse=digest&fl=timestamp,original,statuscode,mimetype,digest,length) | Two returned 200 rows, 2026-05-30 and 2026-09-29 (`20260530123407`, `20260929204533`). |
| `www1.nseindia.com/content/indices/ind_nifty500list.csv` / `nseindia.com/content/indices/ind_nifty500list.csv` / `www.nseindia.com/content/indices/ind_nifty500list.csv` | [www1 query](https://web.archive.org/cdx/search/cdx?url=www1.nseindia.com/content/indices/ind_nifty500list.csv&output=json&filter=statuscode:200&collapse=digest&fl=timestamp,original,statuscode,mimetype,digest,length); [bare host query](https://web.archive.org/cdx/search/cdx?url=nseindia.com/content/indices/ind_nifty500list.csv&output=json&filter=statuscode:200&collapse=digest&fl=timestamp,original,statuscode,mimetype,digest,length); [www host query](https://web.archive.org/cdx/search/cdx?url=www.nseindia.com/content/indices/ind_nifty500list.csv&output=json&filter=statuscode:200&collapse=digest&fl=timestamp,original,statuscode,mimetype,digest,length) | One shared observation, `20200725142023`, original URL `https://www1.nseindia.com/content/indices/ind_nifty500list.csv`. All three queries returned that same row (same CDX digest); count it once, not three times. |

The principal alias query also exposed same-payload repeats whose later timestamps were hidden by digest collapsing (for example, the CDX output included `20250821044405` with the same CDX digest as `20250815235304`). Do not infer one capture per unique payload or use digest as a membership date. The broader prefix probe for the NIFTY Indices host was inconclusive due to a transport error; it is not used to claim exhaustive URL discovery.

### Alias discovery

The current first-party endpoint is [NIFTY Indices' CSV](https://niftyindices.com/IndexConstituent/ind_nifty500list.csv). Search results and software references also surfaced NSE-hosted aliases including `https://archives.nseindia.com/content/indices/ind_nifty500list.csv`, `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv`, and the older `https://www1.nseindia.com/content/indices/ind_nifty500list.csv`. Search also found references to `nseindia.com/content/indices/ind_nifty500list.csv`. These references were used only to broaden exact CDX queries; search snippets and community mirrors were not treated as proof of official historic membership. The CDX results above confirm retrievable observations for the first-party URLs themselves.

## Replayed files staged and verified

Replay form: `https://web.archive.org/web/<14-digit-timestamp>id_/<original-url>`. All eleven local files below were retrieved from these replay URLs. Each passed a content check: UTF-8 CSV header exactly `Company Name,Industry,Symbol,Series,ISIN Code`, rows parsed as CSV, non-empty symbols, and symbol count equals row count. Thus these are not HTML error pages. Row counts are reported as observed, not normalized to an assumed 500; several captures contain more than 500 records and must be reconciled rather than truncated.

| Capture timestamp (UTC; observation only) | Original URL family | CSV records / unique symbols | Local artifact | SHA-256 (local bytes) |
|---|---|---:|---|---|
| 2018-10-04 10:56:32 | `www.niftyindices.com/IndexConstituent/...` | 501 / 501 | [`20181004105632.csv`](../../verification/stage1/batch7/deep-research/web-archives/20181004105632.csv) | `3de9fd8823bbb27357e441deeac1e5493b4a58345e0db54366578bcaaee458fe` |
| 2019-02-01 17:51:07 | `niftyindices.com/IndexConstituent/...` | 501 / 501 | [`20190201175107.csv`](../../verification/stage1/batch7/deep-research/web-archives/20190201175107.csv) | `7aacc85a6ca563021f92e045c74597b2b6d74b96f268e2a031ffd8d59b201d58` |
| 2020-07-25 14:20:23 | `www1.nseindia.com/content/indices/...` | 501 / 501 | [`20200725142023.csv`](../../verification/stage1/batch7/deep-research/web-archives/20200725142023.csv) | `2fa31bea7006761d61a3a22c1114f752157cfeeef82de3b669d21ae12a11b524` |
| 2022-05-04 10:39:23 | `archives.nseindia.com/content/indices/...` | 501 / 501 | [`20220504103923.csv`](../../verification/stage1/batch7/deep-research/web-archives/20220504103923.csv) | `0aef47e9f02528b439bfc3e150fa7c3b5bbd32be527b8217c15f8c5aac615c42` |
| 2022-10-09 16:09:59 | `archives.nseindia.com/content/indices/...` | 501 / 501 | [`20221009160959.csv`](../../verification/stage1/batch7/deep-research/web-archives/20221009160959.csv) | `b65254f8c187eb15c4e60f6e8dfdc3a9ba22ba419a828ffdbc7b1ab352663d16` |
| 2023-04-04 16:47:10 | `archives.nseindia.com/content/indices/...` | 501 / 501 | [`20230404164710.csv`](../../verification/stage1/batch7/deep-research/web-archives/20230404164710.csv) | `0bf9136a59cdb0ad87c7a1b5ff568ed47ffc792bf14027206c778baad7fec9e3` |
| 2024-02-07 23:39:33 | `www.niftyindices.com/IndexConstituent/...` | 501 / 501 | [`20240207233933.csv`](../../verification/stage1/batch7/deep-research/web-archives/20240207233933.csv) | `0189acf6590bc224688c1e74bdc249fe867efd159791b01a86477c048a2e5e71` |
| 2024-02-26 22:49:31 | `archives.nseindia.com/content/indices/...` | 501 / 501 | [`20240226224931.csv`](../../verification/stage1/batch7/deep-research/web-archives/20240226224931.csv) | `c5f542379945f52ddf36e17ba38260691f909818386e2ed94a02999890d890a8` |
| 2025-06-16 11:36:21 | `www.niftyindices.com/IndexConstituent/...` | 503 / 503 | [`20250616113621.csv`](../../verification/stage1/batch7/deep-research/web-archives/20250616113621.csv) | `72368471eae3c86cfc4832f997f4d2ffecf696969afd3f11adfa2894cbc3ccde` |
| 2026-05-30 12:34:07 | `nsearchives.nseindia.com/content/indices/...` | 504 / 504 | [`20260530123407.csv`](../../verification/stage1/batch7/deep-research/web-archives/20260530123407.csv) | `cb16be94b1be5453be4a0a3adb5dbe42577b3d46d1bebcbc5f8952be8cf44e31` |
| 2026-09-29 20:45:33 | `nsearchives.nseindia.com/content/indices/...` | 501 / 501 | [`20260929204533.csv`](../../verification/stage1/batch7/deep-research/web-archives/20260929204533.csv) | `c043bdc21e6080f119a86eb28cdc9b3a6009d3baffa9b5d35e9ca41b195b4501` |

All listed local SHA-256 hashes are hashes of the exact staged response bytes; CDX's `digest` is a separate archive index digest and is not the SHA-256. Hashes are all distinct for the eleven staged bodies, so there were no byte-identical files among this sample. Captures not downloaded in this sample are not being represented as verified files. The differing list counts (including 503/504) and differences in data should be investigated against contemporaneous NIFTY 500 review announcements/methodology before any constituent-history use.

## Common Crawl probe

The public Common Crawl collection index catalog was reachable at [collinfo.json](https://index.commoncrawl.org/collinfo.json); it advertises public collection-specific CDX APIs and crawl date ranges back to 2008. I queried collection endpoints directly for the exact official CSV aliases, including:

- [CC-MAIN-2025-47: `www.niftyindices.com` exact URL](https://index.commoncrawl.org/CC-MAIN-2025-47-index?url=https%3A%2F%2Fwww.niftyindices.com%2FIndexConstituent%2Find_nifty500list.csv&output=json) — returned HTTP 404 (no matching record in that collection, as reported by API status; not evidence that no other collection has it).
- [CC-MAIN-2024-10: NSE archive exact URL](https://index.commoncrawl.org/CC-MAIN-2024-10-index?url=archives.nseindia.com%2Fcontent%2Findices%2Find_nifty500list.csv&output=json&matchType=exact) — returned HTTP 404.
- [CC-MAIN-2023-50: NSE archive URL query](https://index.commoncrawl.org/CC-MAIN-2023-50-index?url=https%3A%2F%2Farchives.nseindia.com%2Fcontent%2Findices%2Find_nifty500list.csv&output=json) — returned HTTP 504 during this run.
- [CC-MAIN-2025-47: domain-style variant](https://index.commoncrawl.org/CC-MAIN-2025-47-index?url=www.niftyindices.com%2FIndexConstituent%2Find_nifty500list.csv&output=json&matchType=domain) — returned 502; another exact variant also returned 404.

These are limited collection/URL probes, with both negative responses and transient errors. No Common Crawl payload was downloaded or counted as verified in this run. Common Crawl does not currently add coverage to the confirmed Wayback results here; retry exact URL and prefix/domain queries over all catalog collections in a follow-up if broader recall is needed. Do not describe either the Wayback or Common Crawl failures as proof the files never existed.

## Community mirrors: useful leads, not source substitutes

Web search returned community copies, including [`kprohith/nse-stock-analysis`'s `ind_nifty500list.csv`](https://github.com/kprohith/nse-stock-analysis/blob/master/ind_nifty500list.csv), [`Hpareek07/NSEData`](https://github.com/Hpareek07/NSEData/blob/master/ind_nifty500list.csv), and other repos. They can be useful for discovering old schemas or locating candidate versions/commit histories, but are editable third-party copies: their checked-in date or Git commit time does not prove when constituents were effective, and they are not included in the verified artifacts/counts above. The same caution applies to Kaggle/community lists. The strongest free evidence found here is the replay of original official-host responses.

## Coverage limits and recommended free anchor strategy

- The successful capture dates are sparse and irregular. There is no proof of a continuous daily, monthly, or even annual archive, nor that every CDX record/alias was searched. The earliest verified observation in the staged sample is 2018-10-04; it is not the earliest possible NIFTY 500 history.
- Several files contain more than 500 data rows. The importer’s structural validation accepts rows but does not establish compliance with the index’s effective-member definition. Keep all raw rows and flags, and reconcile full symbol/ISIN membership and series against contemporaneous official review releases before making analytical snapshots.
- A list observation can lag an announced change, be served between review dates, or differ by site cache/update timing. Capture time is an availability bound only. Do not relabel it as an effective date or backfill membership between observations.
- **Practical free anchor plan:** regularly query the exact `niftyindices.com`, `archives.nseindia.com`, and `nsearchives.nseindia.com` URLs in CDX; retain raw responses, replay URL, exact capture timestamp, HTTP/content-type evidence, retrieval time, SHA-256, parser version, header, row/member counts, and CDX digest. Deduplicate *identical bytes* by SHA-256 while retaining every capture timestamp and URL as provenance. In parallel, discover official NIFTY 500 review/press-release PDFs on NIFTY Indices and NSE, whose own announced effective dates can anchor effective membership. Treat CSV captures only as observed states bounded by neighboring dated evidence, and leave gaps unknown. Keep community Git histories as leads/cross-checks only. This yields a useful free sparse evidence series, not a guaranteed complete PIT feed.

## Reuse manifest and cross-snapshot comparison

A machine-readable [`manifest.json`](../../verification/stage1/batch7/deep-research/web-archives/manifest.json) now accompanies the raw CSVs. It records for each artifact the exact original and replay URLs, capture timestamp, trust label `archive_observation_not_effective_date`, actual retrieval timestamp (`null`, because it was not recorded), SHA-256, byte length, parsed row count, unique symbol/ISIN counts, parsed header, and CDX record metadata (status, mimetype, archive digest, indexed length). Replay response headers and HTTP status are explicitly `null`: the successful retrieval process did not preserve them. This keeps CDX metadata separate from replay-response evidence. The manifest also contains the exact queried CDX URLs, result/failure classifications, and pairwise symbol/ISIN set comparisons. The manifest creation time is null for the same reason; it is not reconstructed from filesystem metadata.

The comparisons use exact field values after trimming surrounding whitespace. Across 11 captures (55 pairs), the **2024-02-07** and **2024-02-26** files have identical symbol sets (501) and identical ISIN sets (501), despite different bytes, capture URLs, and row ordering/content representation. This is a **semantic membership-set duplicate** under those two identifiers, not a byte duplicate and not proof the two lists were effective on either capture date. No other pair in this sample has both identical symbol and ISIN sets. The manifest retains the full pairwise overlaps and exclusive sets so callers can choose a stricter equivalence definition (for example, symbol-to-ISIN mapping or all row fields).

### Exact rows relevant to observed counts

No rows have been removed or reinterpreted in the raw files. The 501/503/504 counts are parsed CSV data-row counts, including the rows below. “Dummy” and “DVR” are literal company-name markers, not an assertion about index methodology. `line` is the physical CSV line number including the header as line 1. Every non-`EQ` row is also preserved in `special_rows` in the manifest; the fact a constituent CSV has `BE` series does not by itself make that row a dummy or an extra record.

| Capture | CSV line | Company Name | Symbol | Series | ISIN Code | Count observation |
|---|---:|---|---|---|---|---|
| 2018-10-04 | 448 | Tata Motors Ltd DVR | TATAMTRDVR | EQ | IN9155A01020 | One additional DVR line in the 501-row CSV; deleting this line would yield 500, but no such deletion is applied or recommended without index methodology evidence. |
| 2019-02-01 | 448 | Tata Motors Ltd DVR | TATAMTRDVR | EQ | IN9155A01020 | Same observation. |
| 2020-07-25 | 448 | Tata Motors Ltd DVR | TATAMTRDVR | EQ | IN9155A01020 | Same observation. |
| 2022-05-04 | 447 | Tata Motors Ltd DVR | TATAMTRDVR | EQ | IN9155A01020 | Same observation. |
| 2022-10-09 | 446 | Tata Motors Ltd DVR | TATAMTRDVR | EQ | IN9155A01020 | Same observation. |
| 2023-04-04 | 447 | Tata Motors Ltd DVR | TATAMTRDVR | EQ | IN9155A01020 | Same observation. |
| 2024-02-07 | 447 | Tata Motors Ltd DVR | TATAMTRDVR | EQ | IN9155A01020 | Same observation. |
| 2024-02-26 | 447 | Tata Motors Ltd DVR | TATAMTRDVR | EQ | IN9155A01020 | Same observation. |
| 2025-06-16 | 145 | Dummy Aditya Birla Fashion and Retail Ltd. | DUMMYABFRL | BE | DUM647O01011 | First of three dummy-labelled rows; all three together account numerically for the difference between 500 and 503. |
| 2025-06-16 | 146 | Dummy Siemens Ltd. | DUMMYSIEMS | EQ | DUM003A01024 | Second dummy-labelled row. |
| 2025-06-16 | 147 | Dummy2 Raymond Ltd. | DUMMYRAYMN | BE | DUM2301A0101 | Third dummy-labelled row. |
| 2026-05-30 | 151 | Dummy Vedanta Ltd. 1 | DUMMYVEDL1 | EQ | DU1205A01025 | First of four dummy-labelled rows; all four together account numerically for the difference between 500 and 504. |
| 2026-05-30 | 152 | Dummy Vedanta Ltd. 2 | DUMMYVEDL2 | EQ | DU2205A01025 | Second dummy-labelled row. |
| 2026-05-30 | 153 | Dummy Vedanta Ltd. 3 | DUMMYVEDL3 | EQ | DU3205A01025 | Third dummy-labelled row. |
| 2026-05-30 | 154 | Dummy Vedanta Ltd. 4 | DUMMYVEDL4 | EQ | DU4205A01025 | Fourth dummy-labelled row. |
| 2026-09-29 | 151 | Dummy HEG Ltd. | DUMMYHEG | EQ | DUM545A01024 | One dummy-labelled row in the 501-row CSV. |

The recurring Tata Motors DVR line explains the numerical 501-versus-500 difference for the eight captures from 2018 through 2024, but is **not** marked dummy and has `Series=EQ`. In June 2025, three literal dummy rows explain 503 relative to 500; in May 2026 four explain 504; in September 2026 one explains 501. These are arithmetic observations only: the files do not state whether these records are temporary corporate-action placeholders, their effective windows, or how NIFTY's count is defined. Several captures additionally contain `BE` rows; those do not explain the 501/503/504 totals by row arithmetic and are not removed. See manifest `special_rows` for every exact non-EQ row, including symbol, series, ISIN, and line number.

## Retrieval citations

- Internet Archive CDX API format and search service: [CDX Server API](https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server) and [web.archive.org CDX endpoint](https://web.archive.org/cdx/search/cdx).
- Official NIFTY Indices current constituent CSV endpoint: <https://niftyindices.com/IndexConstituent/ind_nifty500list.csv>.
- Official NSE archive-host endpoint observed in CDX: <https://archives.nseindia.com/content/indices/ind_nifty500list.csv>; related later archive-host <https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv>.
- Archive replay URLs for each artifact are reproducible by combining its table timestamp and original URL with the `https://web.archive.org/web/<timestamp>id_/` prefix. Example: [2022-05-04 replay](https://web.archive.org/web/20220504103923id_/https://archives.nseindia.com/content/indices/ind_nifty500list.csv).
- Common Crawl public index catalog: <https://index.commoncrawl.org/collinfo.json>.
