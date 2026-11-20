# Free independent anchors for historical NIFTY membership

**Research run:** 2026-11-14. **Priority:** NIFTY 500; NIFTY 50 is a secondary comparison. **Question:** what free, independently obtainable evidence can anchor historical constituent membership without mistaking a proxy for official point-in-time (PIT) truth?

## Conclusion

There is no single free, proven-complete daily PIT source identified here. The strongest free evidence is a *combination*: (1) first-party NIFTY Indices inclusion/exclusion releases and dated constituent reports, (2) NSE/NIFTY Indices official constituent-list captures found in the Internet Archive, and (3) an open third-party reconstruction that can be tested against those primary records. Monthly index-fund disclosures are useful independent cross-checks, not index-membership snapshots. Maintain source-specific provenance and leave gaps unresolved until reconciled; do not present any of these independently as complete verified PIT membership.

A particularly important count warning: the five staged historical NIFTY 500 reports examined below each yield 501 apparent symbol rows, while the official NIFTY 500 inclusion/exclusion workbook contains event records but not an immutable complete constituent snapshot. The official NIFTY Indices factsheet itself reports 501 constituents for September 30, 2026 and 500 for July 31, 2026. The count can vary in published artifacts; **do not trim an apparent 501st symbol or infer correctness from the index's name**. Reconcile symbols, ISINs, document conventions and dated change releases.

## Evidence ranking

| Evidence | PIT value | Free/access | Main caveat |
|---|---|---|---|
| Official NIFTY Indices review press releases and dated constituent reports | Primary effective-date evidence when a release explicitly gives effective date; dated reports are primary dated membership observations | Public URLs; example files were downloaded without login | Releases identify changes, not always the full member list; report effective date and document as-of date must be distinguished; counts and extraction need reconciliation |
| NSE/NIFTY Indices official CSV responses replayed from the Internet Archive | Independent historical observation of first-party-host content | Public CDX and replay URLs | Capture time proves observation/availability, not the CSV's effective date; sparse and irregular |
| Official NIFTY Indices inclusion/exclusion workbook | Primary event list | Public XLS download | Event list is name/event-date based; no symbol, ISIN, weights, or daily complete state; the workbook's description calls events inclusions/exclusions, but effective-date semantics require source-level confirmation |
| Motilal Oswal Nifty 500 Index Fund monthly portfolio | Independent holdings/identity cross-check (ISIN-based) | Public AMC XLSX; public AMFI portfolio portal | Fund holdings are not index membership; implementation, cash, corporate actions, timing, derivatives and tracking differences matter |
| `aditya-jha/nse-historical-membership` | Useful independent reconstructed hypothesis and event parser | Public GitHub; code MIT, data CC0 as repo declares | Reconstructed, current-snapshot seeded, has inferred intervals and acknowledged gaps/cardinality drift; not first-party certification |
| `vishalvx/nifty-indices-datasets` | Practitioner cross-check for selected NIFTY indices | Public GitHub; MIT repository license | Does not list NIFTY 500 among its datasets; reconstructed monthly binary/equal-weight records |
| Historical OHLCV for today's constituents | Not membership evidence | Often freely downloadable | Survivorship bias; price existence does not prove index membership |

## Primary official records inspected

### Inclusion/exclusion workbook

NSE/NIFTY Indices' [IndexInclExcl.xls](https://archives.nseindia.com/content/indices/IndexInclExcl.xls) was downloaded to `verification/stage1/batch7/deep-research/alternative-anchors/IndexInclExcl.xls` and inspected with its `Nifty 500` sheet. It has **2,495 rows and four columns** (`Index Name`, `Event Date`, `Scrip Name`, `Description`), dates from **1998-08-01 to 2020-09-14**, **1,498 inclusion** and **997 exclusion** descriptions; the initial date has 500 inclusion records. It records company names rather than contemporary symbols/ISINs and is an event record, not a periodically complete snapshot. Company-name changes, mergers, demergers, duplicate/share classes and event-date semantics need reconciliation against first-party releases and security identifiers before constructing intervals. Do not assume the workbook alone supplies availability/announcement timestamps or a gap-free daily state.

### Dated official reports

Official NIFTY Indices archives at the [market-capitalisation and weightage data page](https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_data) expose ZIP archives. The following archive files were downloaded to `verification/stage1/batch7/deep-research/alternative-anchors/` and their NIFTY 500 PDFs inspected using PDF text extraction:

| File | Printed report date | Apparent member-symbol rows |
|---|---:|---:|
| `indices_dataMar2018/NIFTY_500_Mar2018.pdf` | March 28, 2018 | 501 |
| `indices_dataMar2019/NIFTY_500_Mar2019.pdf` | March 29, 2019 | 501 |
| `indices_dataMar2020/NIFTY_500_Mar2020.pdf` | March 31, 2020 | 501 |
| `indices_dataApr2021/NIFTY_500_Apr2021.pdf` | April 30, 2021 | 501 |
| `indices_dataMar2022/NIFTY_500_Mar2022.pdf` | March 31, 2022 | 501 |

These are dated first-party observations containing symbols, security names, industry, prices and index weights. The 501 count is an **apparent extracted count** and is explicitly left for reconciliation, not treated as a verified target count. Reports are sparse snapshots, not a continuous history. Source archive URL pattern: `https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataMar2022.zip` (replace period for the other archives).

First-party [press releases](https://www.niftyindices.com/Press_Release/) are particularly valuable for effective dates. For example, the [August 23, 2024 replacement release](https://www.niftyindices.com/Press_Release/ind_prs23082024.pdf) lists NIFTY 500 exclusions and inclusions; the [August 10, 2026 release](https://niftyindices.com/Press_Release/ind_prs10082026.pdf) says the review changes become effective September 30, 2026 (close September 29), and enumerates the NIFTY 500 changes. Releases provide transition evidence but do not by themselves give all constituents, so seed from dated reports and apply only fully reconciled events. Preserve announcement/known-at date separately from the effective date.

The official [NIFTY 500 factsheet](https://niftyindices.com/Factsheet/ind_nifty_500.pdf) includes methodology and a no.-of-constituents field. Search and inspection surfaced a stated **500** for July 31, 2026 and **501** for September 30, 2026. This reinforces that report row counts must be checked against the specific as-of document and not normalized automatically. Current NIFTY 500 methodology describes semiannual reviews and eligibility screens; a current methodology is not proof that historical rules were identical. See [NIFTY Indices methodology documents](https://www.niftyindices.com/Methodology/).

## Internet Archive: free first-party-hosted historical CSVs

The companion research note [`NIFTY_PIT_WEB_ARCHIVES.md`](NIFTY_PIT_WEB_ARCHIVES.md) records CDX queries, source URLs, local hashes and full retrieval details. The exact official NIFTY Indices URL family `niftyindices.com/IndexConstituent/ind_nifty500list.csv` returned 11 distinct successful response payloads after CDX digest collapse, observed from **2018-10-04 through 2026-10-22**; NSE archive-host aliases add additional observations. Eleven replayed files were validated as CSVs with expected header and nonempty symbols. Their apparent row counts range **501 to 504** (including 503 and 504), so do not silently trim or assert target membership. The earliest staged capture is not evidence of earliest possible archive coverage.

CDX/replay is a public, unauthenticated access path, not an official effective-date archive. A capture timestamp establishes when the Internet Archive observed a response, not when the list became effective. Site cache/update delay and capture timing make it unsuitable for assigning an effective date by itself. The recommended use is a sparse observed-state cross-check against neighboring dated reports/review releases, keeping raw bytes, original URL, replay timestamp, CDX metadata and SHA-256. Full details and replay examples: [Internet Archive CDX API](https://web.archive.org/cdx/search/cdx) and [companion report](NIFTY_PIT_WEB_ARCHIVES.md).

## Fund/ETF portfolio disclosure: independent but imperfect cross-check

### Motilal Oswal Nifty 500 Index Fund

The AMC's [fund page](https://www.motilaloswalmf.com/mutual-funds/motilal-oswal-nifty-500-index-fund) says the open-ended scheme tracks the NIFTY 500 Index and lists a downloadable month-end portfolio. The scheme began in September 2019 according to the page/Scheme Information Document; it cannot provide pre-inception evidence. The AMC's [portfolio-download page](https://www.motilaloswalmf.com/downloads/scheme-portfolio-details) and AMFI's [Portfolio Disclosure portal](https://www.amfiindia.com/online-center/portfolio-disclosure) are public routes; AMFI's [monthly reports catalog](https://www.amfiindia.com/research-information/amfi-monthly) also links downloadable month-specific reports. The scheme document indexed from AMFI says monthly portfolio with ISIN is to be hosted in downloadable format within 10 calendar days of month-end. This is a disclosure obligation, not a guarantee of preserved historical URLs or complete archive coverage.

One first-party workbook was actually downloaded and inspected: [October 2026 NIFTY 500 scheme portfolio](https://www.motilaloswalmf.com/content/dam/motilal-mf/sheets/fund-csvs/Month_End_Portfolio_October_2026/YO18.xlsx), saved as `verification/stage1/batch7/deep-research/alternative-anchors/MOMF_Nifty500_Oct2026.xlsx`. It is labelled “MONTHLY PORTFOLIO STATEMENT AS ON OCTOBER 31, 2026,” names the scheme, includes ISIN, quantity, market/fair value and percent-to-net-assets, and reports a grand total of 100%. There are **501 equity rows with unique ISINs** in the workbook. The inspected workbook states derivative exposure is “Nil” at period end; that is a fact about this one month only, not a general property of the fund. Keep raw security categories and any residual cash/other lines rather than counting every row as an equity constituent.

Use a portfolio as an *independent cross-check*: compare fund equity ISINs and names with the dated index report/release and flag unexplained omissions/additions. It is not a substitute for membership. An index fund may use sampling/replication techniques, hold cash and receivables, rebalance after index changes, carry corporate-action/unlisted or newly listed positions, hold derivatives, lend securities, or have small tracking differences. Month-end holdings are snapshots on a different clock from the index effective date; a disclosure can be published after month end. A security being held does not establish index inclusion, and a constituent not held at a snapshot does not establish exclusion. Inspect all rows, footnotes and exposures in each workbook; do not infer “one position = one index member.”

### Broader fund disclosures and annual reports

AMFI offers monthly portfolio and annual-report discovery through its [Portfolio Disclosure](https://www.amfiindia.com/online-center/portfolio-disclosure) and [Research & Information](https://www.amfiindia.com/research-information/amfi-monthly) pages. Scheme-specific SIDs/annual reports can state benchmark, portfolio disclosure practices, tracking and turnover context. They are useful for finding/reporting source files and explaining implementation, but annual reports are low-frequency and their financial statements are not index constituent registers. No evidence was found that a free annual report alone supplies exact effective-date membership. Terms are governed by the source publisher; these pages did not establish a blanket redistribution licence for portfolio workbooks. Record the actual source URL and its stated terms rather than assuming “public” means unrestricted reuse.

## Open datasets and practitioner code

### `aditya-jha/nse-historical-membership`

The public [repository](https://github.com/aditya-jha/nse-historical-membership) says its index membership history is reconstructed from public NSE Indices press releases/circulars. It includes NIFTY 500 and NIFTY 50 among 41 equity indices; `COVERAGE.md` says membership data through 2026-10-13. The README exposes methodology and validation claims and, importantly, discloses pre-2018 cardinality drift, inferred include/exclude intervals, a current-snapshot seed and that NIFTY 500 currently has 499 rows versus its target. Repo-declared code licence is MIT; its separate `LICENSE-DATA` declares CC0 for its data files. Raw NSE PDFs are not redistributed by the project. This makes it a strong free reconstruction to inspect and test, not an independent primary source or guaranteed complete PIT truth. Compare individual rows and transition dates to actual releases and snapshots; distinguish circular-backed records from inferred records.

### `vishalvx/nifty-indices-datasets`

The [repository](https://github.com/vishalvx/nifty-indices-datasets) describes monthly binary/equal-weight constituent reconstructions from official press releases and circulars. Listed coverage is NIFTY 50 (2008 onward), NIFTY Midcap 50 and Smallcap 50 (2019 onward), and NIFTY500 Momentum 50 (2024 onward). It does **not** list the NIFTY 500 broad index. Repository says MIT. It can provide a secondary NIFTY 50 comparison but not a NIFTY 500 anchor; monthly reconstruction should be checked against effective-date releases.

### Rejected as membership anchors

- Kaggle [`rahupau/historical-data-for-nifty-500`](https://www.kaggle.com/datasets/rahupau/historical-data-for-nifty-500) describes historical NIFTY 500/index-market data from 1999-06-07 to 2022-03-31 and labels the dataset CC0. It is price/index history, not verified dated constituent membership. A dataset keyed to today's constituents or with historical OHLCV does not address survivorship bias.
- [`ninad164/auto-portfolio-management`](https://github.com/ninad164/auto-portfolio-management) has no dated universe snapshots in the inspected snapshot directory; its manifest is header-only and describes scaffolding, not an extant historical membership series.
- Community copies of the current constituents CSV or checked-in Git files can help discover schemas and leads, but commit dates/file dates do not establish the effective date of membership. Do not elevate an editable mirror over the publisher's original document.

## Institutional repositories and open-data availability

Searches for institutional repository / open-data alternatives did not identify an institutional repository with a free, documented, security-level NIFTY 500 historical membership series and effective dates. The official sources located were publisher-hosted index reports/releases and exchange-hosted event records, not a separately curated academic dataset. This is a bounded negative finding from the sources inspected, not proof that no repository or dataset exists. The open practitioner reconstruction above is the most relevant reusable third-party table found, with the caveats already disclosed by its maintainers.

## Free triangulation workflow

1. **Create evidence records, not a “truth” CSV first.** For each item preserve index, original URL, publisher, retrieval time, document/report date, explicit effective date (if any), announcement/known-at date, archive capture timestamp when applicable, raw bytes, SHA-256, parser/version and the literal reported count.
2. **Seed only from dated first-party NIFTY 500/NIFTY 50 reports.** Extract symbols and names; retain the raw rows and report's printed date. Resolve ISINs from contemporaneous first-party source documents, not today's symbol table alone.
3. **Apply only explicit first-party change events at the stated effective close.** Keep event date, publication time and effective date separate. Link each add/remove to its announcement and reconcile simultaneous replacements, share classes, demergers and listing events.
4. **Use the official workbook as a discovery/event cross-check**, not a snapshot source. Reconcile names, dates and descriptions against releases; report unexplained event gaps.
5. **Compare Wayback CSV captures as availability observations.** Never assign capture date as the membership effective date. Compare exact symbols/ISIN identities with the nearest dated snapshot/review announcements; retain unresolvable discrepancies and all observed row counts.
6. **Use month-end index-fund holdings as a separate independent check.** Normalize by ISIN, preserve non-equity and cash lines, and account for disclosure/rebalance timing. Explain each fund-only or index-only security; do not fill index membership from holdings.
7. **Compare the open reconstruction, including its source labels**, against the primary events/snapshots. Treat differences as audit cases, not majority voting among datasets seeded from the same current snapshot.
8. **Leave unknown periods unknown.** Do not forward-fill a sparse annual/monthly snapshot into daily history unless the assumption is explicit and separately validated; do not use OHLCV or current constituents to fill missing membership.

A PIT claim requires both effective membership evidence and, where “what was knowable then” matters, historically supported availability/announcement time. A dated snapshot series can verify membership only at its observation/effective date, not automatically every intervening day.

## Terms and access notes

- Official NSE/NIFTY Indices documents, XLS/PDF/CSV downloads, AMFI disclosures and AMC spreadsheets inspected were publicly reachable without a paid account or login during this research. No download of third-party paid data was required. Public access is **not** treated as a grant of redistribution/commercial rights; verify source terms for the intended use.
- `aditya-jha` explicitly provides MIT code and CC0 data terms; those repository declarations apply to its own code/data, not to NSE's original publications or marks.
- `vishalvx` declares MIT for its repository; check the repo's current licence files and dataset attribution before reuse.
- Kaggle's cited dataset claims CC0 on its page, but that terms label does not turn prices into membership evidence or establish rights to underlying exchange content.
- Internet Archive availability is not a licence grant for the underlying official CSV.

## Source index

- [Official NIFTY Indices constituent/report archives](https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_data)
- [Official NIFTY Indices press releases](https://www.niftyindices.com/Press_Release/)
- [Official NIFTY 500 factsheet](https://niftyindices.com/Factsheet/ind_nifty_500.pdf)
- [NSE archive-hosted inclusion/exclusion workbook](https://archives.nseindia.com/content/indices/IndexInclExcl.xls)
- [NIFTY Indices equity methodology](https://www.niftyindices.com/Methodology/)
- [Motilal Oswal Nifty 500 Index Fund](https://www.motilaloswalmf.com/mutual-funds/motilal-oswal-nifty-500-index-fund)
- [AMFI Portfolio Disclosure](https://www.amfiindia.com/online-center/portfolio-disclosure)
- [AMFI monthly reports](https://www.amfiindia.com/research-information/amfi-monthly)
- [SEBI mutual funds landing / circulars](https://www.sebi.gov.in/legal/circulars.html)
- [Internet Archive CDX API](https://web.archive.org/cdx/search/cdx)
- [Companion Wayback capture evidence note](NIFTY_PIT_WEB_ARCHIVES.md)
- [Open reconstructed membership dataset](https://github.com/aditya-jha/nse-historical-membership)
- [Selected NIFTY practitioner datasets](https://github.com/vishalvx/nifty-indices-datasets)
