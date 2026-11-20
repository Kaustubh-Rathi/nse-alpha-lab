# Official free NIFTY 500 historical membership archives: probe findings

**Research date:** 2026-11-14  
**Scope:** Public, unauthenticated first-party Nifty Indices archives and the repository's already-acquired official snapshots. No paid data, authentication, access-control bypass, or modifications under `data/` were used. Downloaded probe artifacts are research evidence in `verification/stage1/batch7/deep-research/official/`; they are not admitted membership records.

## Bottom line

The earlier broad claim that there is **“no free snapshot archive”** is **not supported**. Nifty Indices publicly serves historical type-4 monthly ZIPs, some of which contain full, dated NIFTY 500/CNX 500 constituent tables. This repository already has 26 exact-date official snapshots (19 NIFTY 50, seven NIFTY 500), and its documentary pipeline records 108 monthly NIFTY 500 snapshots from 2013-04-18 through 2022-03-31. Independent live probes retrieved and inspected additional examples, including full tables dated 2013, 2014, 2018, 2020, and 2022.

A second, particularly important first-party source broadens the free history beyond those snapshot archives: NSE's public `IndexInclExcl.xls` event workbook provides a name-based NIFTY 500 event ledger with 500 initial inclusion rows dated 1998-08-01 and recorded inclusion/exclusion changes through 2020-09-14. This is a substantial candidate baseline-plus-events avenue, and makes any earlier blanket “no free historical baseline” conclusion untenable. The supported, narrower limit is that the workbook is not itself a security-identifier-resolved daily state or a publication-time history: company names, event-date semantics, all rows, later transition coverage and reconciliation against official notices/snapshots still need validation. Separately, sampled type-4 archives no longer included NIFTY 500 PDFs from at least October 2022 onward. Free official data therefore reaches much further than previously asserted, but completeness and daily PIT correctness still require a careful event reconstruction and gap audit.

## First-party access path and request format

The official historical-data page is <https://www.niftyindices.com/reports/historical-data>. Its page HTML exposes report type 4 as **“Indices - Market Capitalisation & Weightage”** and its JavaScript posts to:

`POST https://www.niftyindices.com/reports/historical-data/Index/`

The page's JavaScript constructs a JSON object with these exact fields:

```json
{"SelectedReportType":"4","SelectedDate":"","MonthYear":"Oct 2020"}
```

`MonthYear` uses English abbreviated month plus a space plus four-digit year (e.g. `Mar 2022`). A normal public browser user-agent and `Content-Type: application/json` were sufficient; requests returned HTTP 200. A successful response has this shape:

```json
{
  "success": true,
  "reportName": "Indices - Market Capitalisation & Weightage",
  "data": [{
    "DownloadLink": "/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2020.zip",
    "FileName": "indices_dataOct2020.zip"
  }]
}
```

Download by joining `DownloadLink` to `https://www.niftyindices.com`, e.g. <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2020.zip>. The request is ordinary public retrieval; no cookies, login, or token were needed. The currently served monthly-reports page, <https://www.niftyindices.com/reports/monthly-reports>, also contains direct links for recent month packages, but the historical lookup POST provides older months.

Source-code context in this repository: `src/nse_pipeline/nifty/documentary.py` defines the same endpoint, posts the same three fields, describes type 4 as a market-cap/weightage ZIP, and selects NIFTY 500 PDF/CSV members. `src/nse_pipeline/nifty/snapshots.py` only admits dated snapshots with an explicit date in the document; it explicitly does not certify membership between snapshot dates. Those are useful conservative rules, not evidence that the archive is unavailable.

## Existing official snapshots: read-only verification

`data/processed/index_membership/catalog.json` contains **26** records: 19 `NIFTY50` records dated 2008-10-31 through 2026-10-30, and seven `NIFTY500` records dated 2015-10-30 through 2021-10-29. The matching source ZIP paths listed in the catalog exist under `data/raw/index_membership/`. Their manifests identify official HTTPS `niftyindices.com` URLs, exact source ZIP hashes, and document-explicit as-of evidence. Recounting the listed snapshot CSV rows matched the catalog on inspected examples.

| Index | Snapshot as-of | Constituent rows | Manifest's date evidence | Official archive URL | ZIP SHA-256 |
|---|---:|---:|---|---|---|
| NIFTY 500 | 2015-10-30 | 500 | `Constituents of Nifty 500 October 30, 2015` | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2015.zip> | `4516bcfc6aa1b90a669191f42a979d92004962ec123114075e6514768154e251` |
| NIFTY 500 | 2018-10-31 | 501 | `Constituents of NIFTY 500 October 31, 2018` | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2018.zip> | `124f94b04e047ffe18fe80b17b5fea5d8cf92593d3d75b71d0e90cbaae98c195` |
| NIFTY 500 | 2020-10-30 | 501 | `Constituents of Nifty 500 / October 30, 2020` | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2020.zip> | `62079b5a9c43c475f7f558453a4e27461cdf0ef06c056a521f852a4d85bde0c4` |
| NIFTY 500 | 2021-10-29 | 501 | `Constituents of NIFTY 500 October 29, 2021` | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2021.zip> | `b89d9206bfeadc3d613cba7bf4406607ed47a7e66a22702a1587a5a21749948b` |

Example manifest: `data/processed/index_membership/NIFTY500_2018-10-31.json`; it records the ZIP digest, inner PDF filename and digest, 19 source pages, page row counts, and snapshot CSV digest. The ZIP hash and row count match the direct live download and extraction in this research run. Counts can exceed 500 (e.g. 501) and must not be forced to 500: the source table is the evidence, and the project already treats these as explicit observed rows.

`src/nse_pipeline/nifty/proxy.py` lines 34-37 independently describes these 26 existing records as the ground-truth anchors for its explicitly labelled proxy. That module does not claim coverage between dates.

## NSE official inclusion/exclusion workbook: event baseline and current-host probe

A complementary public first-party file is [NSE's `IndexInclExcl.xls`](https://archives.nseindia.com/content/indices/IndexInclExcl.xls). Another research effort had already staged it at `verification/stage1/batch7/deep-research/alternative-anchors/IndexInclExcl.xls`; I did not redownload or duplicate its workbook analysis. Read-only verification of the existing bytes gives 820,736 bytes, OLE/XLS signature `d0cf11e0a1b11ae1`, and SHA-256 `8869bb7c4df67403131a494a8cc65509e80828f9438bc150b506cdbf55378046`, matching the reported hash. `xlrd` confirms a `Nifty 500` worksheet with one header plus 2,495 event rows and columns `Index Name`, `Event Date`, `Scrip Name`, `Description`. Independent recount: 1,498 `Inclusion into Index`, 997 `Exclusion from Index`; the 500 rows on 1998-08-01 are all inclusions; latest event date is 2020-09-14 (one inclusion and one exclusion). The workbook therefore supplies a substantial **official name/event baseline** before the monthly dated archive series starts, but events are names, not historical symbols/ISINs, and the workbook alone does not assert the publication/known-at timestamp or provide full constituent rows on every day. Event date must not silently be treated as announcement or effective-close time without checking source semantics.

### Bounded newer-source check

On 2026-11-14 I made unauthenticated GETs (browser user-agent) to the exact workbook URL and plausible first-party host/file variants; no workbook was written or changed. Results:

| URL | HTTP / content | Bytes / SHA-256 | `Last-Modified` | Interpretation |
|---|---|---:|---|---|
| <https://archives.nseindia.com/content/indices/IndexInclExcl.xls> | 200, `application/vnd.ms-excel`, OLE signature | 820,736 / `8869bb7c4df67403131a494a8cc65509e80828f9438bc150b506cdbf55378046` | 2020-09-22 08:10:21 GMT | Same bytes as staged workbook; sheet's event maximum is 2020-09-14. |
| <https://nsearchives.nseindia.com/content/indices/IndexInclExcl.xls> | 200, `application/vnd.ms-excel`, OLE signature | 820,736 / same SHA-256 | 2020-09-22 08:10:21 GMT | Alias serves byte-identical workbook; no newer event extension. |
| `https://www.nseindia.com/content/indices/IndexInclExcl.xls` | 404 | — | — | No workbook at this tested URL. |
| `https://www1.nseindia.com/content/indices/IndexInclExcl.xls` | TLS handshake error | — | — | Probe inconclusive; not a confirmed negative. |
| `https://archives.nseindia.com/content/indices/IndexInclExcl.xlsx` and corresponding `nsearchives` URL | 404 | — | — | No `.xlsx` variant at these exact paths. |
| Tested variants `Index_Incl_Excl.xls`, `Index_Incl_Excl.xlsx`, `IndexInclusionExclusion.xls`, `Index_Inclusion_Exclusion.xls`, `IndexConstituent.xls` on both NSE archive hosts | 404 | — | — | Bounded filename guesses, not exhaustive discovery. |

The current `Last-Modified` and identical mirror establish only that these endpoints serve the same workbook version whose spreadsheet rows stop at 2020-09-14. A bounded search of current Nifty Indices monthly-report HTML found no link or filename reference to `IndexInclExcl`; search queries did not identify a newer official variant. **No newer first-party workbook was found by this bounded probe; no claim is made that a later ledger cannot exist under another URL or as subsequent review releases/circulars.** The authoritative free next step is to continue the workbook's event sequence using the official dated NIFTY Indices inclusion/exclusion press releases and validate against later dated membership reports (e.g. the 2021/2022 archive reports) and NSE-hosted observation captures. Do not duplicate the existing workbook or its extraction; reconcile it as a new candidate baseline/event source.

## Live unauthenticated archive probes

All successful type-4 lookup requests below returned HTTP 200 JSON. ZIPs returned HTTP 200 and had ZIP local-file signature `PK\x03\x04` (hex prefix `504b0304`). PDF members had `%PDF-` prefix (`255044462d`). The report dates below came from the PDF's repeated `Constituents of ...` heading, or, for April 2013, the CSV's explicit `Date` and `Index Name` columns. The member counts are extracted table rows, not an assumption of 500.

| Lookup month | ZIP URL / filename | ZIP HTTP; bytes; SHA-256 | NIFTY/CNX 500 member | Embedded snapshot date; extracted rows | Inner member SHA-256 | HTTP `Last-Modified` |
|---|---|---|---|---|---|---|
| Apr 2013 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataApr2013.zip> (`indices_dataApr2013.zip`) | 200; 59,715; `4942c0f021abcd6ed9702c343c2696ec44cfa8022c7e37448f8a78c075c4a16d` | `indices_dataApr2013/cnx500_Apr2013.csv` | 2013-04-18; 500 rows, 500 distinct symbols; all rows `CNX 500`, date `18-04-2013` | `5c08683c64c5cdc1c4aff623b4df5a5eb8e5d5fd3123521ca694f96303053468` | 2017-05-20 14:02 GMT |
| Oct 2013 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2013.zip> | 200; 636,744; `a6b5e1db29dd4d6dc215be54abcfcecc51b25b9993a0ac55cf24587fca191b9a` | `cnx500_Oct2013.pdf` | 2013-10-31; 500 rows over 19 pages | `17881863350149e50531fd020aaad75d72b7f39abdf7262df4e87808d1103a7b` | Not captured in this probe |
| Oct 2014 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2014.zip> | 200; 654,167; `326818b807b2efe6d62169aa4a383cf149e21b3f31acf45654db442bf22a671e` | `cnx500_Oct2014.pdf` | 2014-10-31; 500 rows over 19 pages | `44f51ebf63c45359adef46d16ef97bade73447f9271361b851efcbb1a21684c7` | 2017-05-20 14:03 GMT |
| Mar 2018 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataMar2018.zip> | 200; 2,694,245; `305403cfdf38817cf31dc8bf2b22bc69f6d579a11a80d588958b5cf20fd494c7` | `NIFTY_500_Mar2018.pdf` | 2018-03-28; 501 rows over 19 pages | `2ef28a9a018bb78e75356a05305da11aae0973eed66316af5b4c52ecd86ce4c8` | 2018-05-02 07:15 GMT |
| Oct 2018 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2018.zip> | 200; 5,342,734; `124f94b04e047ffe18fe80b17b5fea5d8cf92593d3d75b71d0e90cbaae98c195` | `NIFTY_500_Oct2018.pdf` | 2018-10-31; 501 rows over 19 pages | `93bd599c7c8f8465162cff5adcaaa205ad0efda17c20a0517caa10bd91504a4d` | 2018-11-02 08:40 GMT |
| Oct 2020 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2020.zip> | 200; 5,343,930; `62079b5a9c43c475f7f558453a4e27461cdf0ef06c056a521f852a4d85bde0c4` | `NIFTY_500_Oct2020.pdf` | 2020-10-30; 501 rows over 19 pages | `f1b256a00bb8dcf22627e70f33ddc28c16a7184d99898adddaef325228bd9d60` | 2020-11-02 09:50 GMT |
| Mar 2022 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataMar2022.zip> | 200; 5,475,885; `44a182c845eac8ddd6864abe535d02c6ce9bdecc0adbf11a16e25133cc44b969` | `NIFTY_500_Mar2022.pdf` | 2022-03-31; 501 rows over 19 pages | `804feaf625eb5ea1a47094349fe372eba652666c1854450ce0046e6ceac33b08` | 2022-04-01 06:50 GMT |
| Oct 2022 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2022.zip> | 200; 637,519; `d7d6e17a01105cf20dd311f50e544dbcbc2979600b78cda9857583c72c66ce19` | **No NIFTY 500 PDF**; ZIP contains five PDFs: NIFTY 50, Bank, Financial Services, Midcap Select, IT | No NIFTY 500 snapshot in this package | — | Not captured in this probe |
| Oct 2023 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2023.zip> | 200; 637,453; `f854b625bfe34aaf997a3d9c2e36b3d961f67d4464542a7a0958aa4441a2940c` | **No NIFTY 500 PDF**; five non-NIFTY500 index PDFs | No NIFTY 500 snapshot in this package | — | 2023-10-31 12:50 GMT |
| Oct 2024 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2024.zip> | 200; 772,216; `4cfe006e942f637cffd4a50550bb166bb770c98053e8e5c78fdd48cc82e7141d` | **No NIFTY 500 PDF**; six PDFs for other indices | No NIFTY 500 snapshot in this package | — | 2024-10-31 13:10 GMT |
| Mar 2024 | <https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataMar2024.zip> | 200; 637,430; `a5dc521e224c021d97e60a0c3b9d061ea6e6c49f0e793e9422a5bb2c995dd7cf` | **No NIFTY 500 PDF**; five PDFs for other indices | No NIFTY 500 snapshot in this package | — | 2024-03-28 12:50 GMT |

The directly retained ZIP/PDF probe files are under `verification/stage1/batch7/deep-research/official/`, named by month and archive filename. This keeps these observations outside `data/` and avoids admitting them as canonical evidence. For example, the 2018-10 ZIP is byte-for-byte identical to the repository's existing source ZIP by SHA-256.

The April 2013 type-4 report is an important edge case: the complete CNX 500 table is a CSV (explicit columns `Date,Index Name,Symbol,Security Name,...`) and the snapshot date is April 18, not month end. The repository's `documentary.py` already has a fail-closed `import_type4_csv` for this report variant. It is not evidence for all of April or earlier dates.

## What “publication date” means here

The document-stated **snapshot date** and the web server's HTTP `Last-Modified` header are different fields. Examples: snapshot 2018-03-28 vs `Last-Modified: 2018-05-02`; snapshot 2020-10-30 vs 2020-11-02; snapshot 2022-03-31 vs 2022-04-01. For 2024-10 the header is 2024-10-31. The 2013/2014 ZIPs report 2017 modification timestamps, which likely reflect file migration or republication rather than original release timing. No claim is made that `Last-Modified` equals the original publication, public-availability, or point-in-time tradability timestamp. The repository's existing 2018 manifest `retrieved_at_utc` (2026-11-08) records local collection, not source publication. For point-in-time backtesting, availability timestamps remain a separate unresolved requirement.

## Coverage, limits, and honest interpretation

- The checked-in official catalog is **annual** for NIFTY 500 (seven exact October dates, 2015-2021), not 26 NIFTY 500 snapshots. The NIFTY 50 account in the same catalog has 19 annual anchors.
- The documentary coverage report, `data/reports/nifty-doc-coverage.json`, records 108 NIFTY 500 snapshots, first 2013-04-18 and last 2022-03-31, and labels 214 months missing over its broad requested horizon. These are already assembled snapshots in `data/processed/nifty_doc/`; no reason to retrieve those again before targeting actual gaps. A documentary count is not by itself a verified continuous event history.
- The NSE inclusion/exclusion workbook adds 500 starting name records from 1998-08-01 and 1,995 subsequent named event rows ending 2020-09-14. This can potentially bridge substantially earlier history when reconciled, but does not, without stable identity mapping and verified event semantics, constitute a ready-to-use symbol/ISIN interval table.
- Type-4 archive lookup succeeded for sampled 2013-2014, 2018, 2020, 2022, 2023, and 2024 dates. NIFTY 500 was present in sampled ZIPs through March 2022, including March 2022; it was absent in sampled later archives (April 2022, September/October 2022, February/October 2023, March/October 2024, and sampled later packages). The repository coverage report's October probe likewise records NIFTY 500 PDFs in October packages for 2013-2021 and none for October 2022-2026.
- A 200 response of `{"success":false,"message":"No Data Found"}` was observed for type 4 at October 2009, October 2010, October 2012, and March 2013. It means the tested report lookup returned no data; it does not prove that no independently hosted historic source exists.
- The check was not an exhaustive request-by-request census of every archive month. Do not infer availability of unprobed months or a complete first/last archive range from sample dates. The existing 108-snapshot documentary run provides stronger observed month coverage for 2013-04 through 2022-03, but individual missing months still require lookup and artifact inspection.
- Complete dated snapshots are useful as anchors, but they do not answer membership on intervening trading dates, off-cycle changes, effective-date exceptions, reversals, symbol/ISIN mapping, or when the report became publicly available. A press-release change list is not a substitute for an independently verified baseline and fully reconciled event ledger.

## Related official-host archived CSV evidence

The adjacent research note [`NIFTY_PIT_WEB_ARCHIVES.md`](NIFTY_PIT_WEB_ARCHIVES.md) records Internet Archive replays of official-host NIFTY 500 CSV URLs, including response files captured in 2022, 2023, 2024, 2025, and 2026. Those captures are free official-host **observations** with validated 501-504 CSV rows, but their capture timestamps are not effective dates; they complement the XLS event ledger and dated type-4 reports rather than replacing them. In particular, absence of NIFTY 500 PDFs from sampled 2022+ type-4 ZIPs is not absence of all later official-host membership-list evidence. Use the captured CSVs as comparison evidence with their archive timestamps and caveats intact.

## Other first-party paths checked

- The current public historical-data page and its ordinary AJAX endpoint work as described above. The current monthly-reports page exposes direct links for recent packages. Their existence further contradicts “no public archive,” although report contents vary by month and index.
- `https://www.connect2nse.com/iislNet/` loads a public login page. Its page links back to the public Nifty Indices reports; no anonymous complete membership-history endpoint was identified there. No login attempt was made.
- Public `nsearchives.nseindia.com` paths are used elsewhere by this repository for NSE circulars, actions, and market files, but this research found no first-party NIFTY 500 constituents archive path there. This is a bounded search result, not proof of nonexistence.
- Search-engine probes returned no useful primary-source results for legacy IISL/IndiaIndexServices historical constituent archives. The live first-party Nifty Indices endpoint and actual ZIP/PDF bytes were therefore the evidence used. No authenticated or restricted IISL product was accessed.

## Free next steps: close gaps, don't duplicate anchors

1. Use `data/processed/nifty_doc/` snapshots and `data/processed/index_membership/catalog.json` as the existing anchors. Build a date/month matrix against `data/reports/nifty-doc-coverage.json`; request only missing periods, especially gaps inside 2013-04..2022-03, rather than re-downloading all files.
2. For each requested type-4 month, retain the raw lookup JSON, status, response hash, exact download URL, HTTP status/content type, ZIP signature/hash, ZIP member names, and source member bytes/hash. Classify the result as **lookup no-data**, **ZIP returned but no NIFTY500/CNX500 member**, **complete dated list**, or **unvalidated/other**. Do not silently convert file-name dates into snapshot dates.
3. Scan all candidate months from April 2013 through March 2022 (plus any independently discovered earlier/later source) and test both PDF and CSV formats. April 2013 demonstrates CSV; later archives demonstrate PDFs. Validate embedded index name, explicit document date, every constituent row across all pages, duplicate identities, completeness/regularity, and exceptions. Keep CNX 500 documents under an explicitly documented historical name mapping rather than dropping them.
4. Treat official artifacts as source evidence only. Reconcile each snapshot against bracketing snapshots and official change notices; preserve unapplied events until symbol identity, effective date, corrections, and any off-cycle changes are manually resolved. Follow `src/nse_pipeline/nifty/snapshots.py`'s exact-date/no-fill semantics.
5. Separately establish report publication/availability timing from reliable contemporaneous evidence before claiming a file was usable on its nominal snapshot date. HTTP `Last-Modified` is only a server header and can show later migration, as the 2013/2014 files demonstrate.
6. After the full free archive census, report residual gaps precisely. A request for a **complete daily effective-interval NIFTY 500 history** remains much stronger than the existence of **free official dated snapshots**; only that narrower unresolved requirement may eventually justify another source or subscription.

## Repository evidence references

- `src/nse_pipeline/nifty/documentary.py`: official endpoint constants/request payload; report-type 4 ZIP discovery; PDF and type-4 CSV import; dates are taken from documents; changelog events remain unapplied.
- `src/nse_pipeline/nifty/snapshots.py`: explicit-date and provenance validation; `members_on` is exact-date-only and raises for missing dates.
- `src/nse_pipeline/nifty/proxy.py`, lines 34-37: explicit inventory of 26 official snapshot anchors and their date ranges.
- `verification/stage1/batch7/deep-research/alternative-anchors/IndexInclExcl.xls`: existing research-stage official XLS, read-only hash/worksheet/count verification recorded above; it was not copied, re-downloaded, or promoted into `data/`.
- `data/processed/index_membership/catalog.json`: 26 catalog entries with source URLs, source paths, inner members, and snapshot paths.
- `data/reports/index-membership-coverage.json`: annual official catalog summary and the repository's prior bounded October type-4 probe (source of the 2013-2021 October presence / 2022-2026 absence observation).
- `data/reports/nifty-doc-coverage.json`: documentary snapshot span/count and missing-month summary.
- `verification/stage1/batch7/deep-research/official/`: research-only bytes downloaded for the probes in this report; not canonical data.
