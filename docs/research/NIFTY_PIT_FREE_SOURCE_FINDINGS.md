# Free historical NIFTY membership: verified sources and reconstruction plan

Research date: 2026-11-14.

## Conclusion

**There are substantially more free options than the earlier investigation found.**
The claim that historical NIFTY 500 membership requires a paid subscription was
premature. Public official event records, dated official constituent reports,
archived official-host CSVs, and open reconstructions can be combined without
buying data.

This research establishes obtainable source evidence, **not** complete
decision-time membership for every historical session. No production membership
was admitted, no `data/` files were changed, and no P0 contract is promoted by
these findings.

## Strongest discoveries

| Free source | Independently checked result | Appropriate use |
|---|---|---|
| [NSE inclusion/exclusion workbook](https://archives.nseindia.com/content/indices/IndexInclExcl.xls) | NIFTY 500 sheet: **2,495 records**, 1998-08-01 through 2020-09-14; **500 initial inclusion records**, 1,498 inclusions and 997 exclusions overall | Historical starting census/event discovery. Entries use company names, not security identifiers; event-date meaning and announcement timing need documentary verification. |
| [NSE Indices historical reports](https://www.niftyindices.com/reports/historical-data) | The repository already contains **108 NIFTY 500 snapshots**, with printed dates from 2013-04-18 through 2022-03-31. Live downloads confirmed complete constituent tables in sampled archive ZIPs. | Dated primary-source anchors, not automatic membership carry between dates or proof of same-day public availability. |
| [Internet Archive CDX for the official CSV](https://web.archive.org/cdx/search/cdx?url=niftyindices.com/IndexConstituent/ind_nifty500list.csv&output=json&filter=statuscode:200&collapse=digest) and NSE-host aliases | **11 downloaded CSV observations**, 2018-10-04 through 2026-09-29, with checked headers, hashes and counts. Broader index results include additional captures. | Independent observations of official-host lists. Capture timestamps are not effective dates. |
| [Aditya Jha reconstruction, pinned revision](https://github.com/aditya-jha/nse-historical-membership/tree/0786cd9159742f4f2012dcd5f01c1f224b0b019b) | NIFTY 500: **1,151 intervals**, 970 historical symbols. Agent matching found **495 intervals with at least one publication-dated endpoint candidate** in upstream parsed notices; 103 have both endpoints dated. | Locate primary change notices and repair endpoint provenance. Parsed JSON still needs verification against the official PDFs; inferred baseline rows are not ground truth. |
| [Prathmesh Gupta monthly reconstruction, pinned revision](https://github.com/prathmeshgupta/nifty-pit-universe/tree/2cb41455d07418748f9bfce2039ca6d241fab011) | **261 monthly dates**, 2005-01 through 2026-09; 131,271 rows, 500–506 rows per month | Independent comparison/lead. Mixes reconstructed events with fund disclosures, so it is not an official continuous membership archive. |

The numbers above describe different source types and overlapping periods.
They must not be added together as continuous verified coverage.

## 1. Official monthly archives are accessible free

The public historical-data page uses:

```text
POST https://www.niftyindices.com/reports/historical-data/Index/
Content-Type: application/json

{"SelectedReportType":"4","SelectedDate":"","MonthYear":"Oct 2020"}
```

The response supplies a relative `DownloadLink`. Direct examples independently
checked locally:

- [April 2013 ZIP](https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataApr2013.zip):
  contains `indices_dataApr2013/cnx500_Apr2013.csv`, explicitly dated
  **2013-04-18**, rather than an assumed month-end.
- [October 2018 ZIP](https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataOct2018.zip):
  contains `NIFTY_500_Oct2018.pdf`, explicitly dated **2018-10-31**.
- [March 2022 ZIP](https://www.niftyindices.com/Indices_-_Market_Capitalisation_and_Weightage/indices_dataMar2022.zip):
  contains `NIFTY_500_Mar2022.pdf`, explicitly dated **2022-03-31**.

Existing files were overlooked during the earlier investigation:

- `data/processed/index_membership/`: 19 NIFTY 50 and seven NIFTY 500
  exact-date snapshots.
- `data/processed/nifty_doc/`: **225 NIFTY 50 snapshot files covering 224
  distinct months** (2008-01–2026-10), and **108 NIFTY 500 files covering 108
  distinct months** (2013-04–2022-03).
- `data/reports/nifty-doc-coverage.json`: existing missing-period and unapplied
  event inventory.

These were inspected read-only. New work should reuse their evidence and target
actual gaps, not redownload every month.

**Important limitation:** sampled later type-4 packages did not contain NIFTY
500 reports. For example, the downloaded October 2024 ZIP contains no NIFTY 500
member. A successful ZIP download is therefore not evidence of constituent
coverage for that index.

Printed report dates, current HTTP `Last-Modified` headers, and public
availability dates are separate facts. In particular, historical files can have
later migration timestamps. Do not pretend a report was known at its printed
as-of date without supporting availability evidence.

## 2. The official workbook provides an older starting point

The downloaded [official workbook](https://archives.nseindia.com/content/indices/IndexInclExcl.xls)
has a `Nifty 500` sheet with columns:

```text
Index Name | Event Date | Scrip Name | Description
```

Host inspection with `xlrd` confirmed:

- 2,495 data records across 250 distinct event dates.
- Earliest event date: **1998-08-01**, with 500 inclusion records.
- Latest event date: **2020-09-14**.
- 1,498 `Inclusion into Index` and 997 `Exclusion from Index` records.
- SHA-256:
  `8869bb7c4df67403131a494a8cc65509e80828f9438bc150b506cdbf55378046`.

This is a useful primary-source historical census/event lead, not a ready-made
bitemporal table. It lacks announcement timestamps and contemporary
symbols/ISINs. Resolve names using dated evidence, not fuzzy matching or today's
symbol map. Net inclusion/exclusion counts alone do not prove completeness.

## 3. Archived official CSVs add newer checkpoints

The eleven staged CSVs contain **501–504 security rows**, all with distinct
symbols and populated ISIN fields. These counts are observations, not errors to
be corrected by truncating to 500. DVR and temporary/dummy-labelled entries need
explicit treatment.

Host review independently confirmed every staged file's SHA-256, byte size and
CSV row count against
`verification/stage1/batch7/deep-research/web-archives/manifest.json`.
Two February 2024 captures have different bytes but the same symbol/ISIN set:
byte-level deduplication and membership-set comparison answer different
questions.

The manifest retains exact original and replay URLs, CDX metadata and capture
timestamps. Retrieval timestamps/response headers that were not recorded are
left null rather than invented.

## 4. Community data is useful, but requires cross-checking

Other concrete free leads:

- [BKKB20/nse-index-history](https://github.com/BKKB20/nse-index-history):
  independent workbook reconstruction from circulars; useful for finding
  disagreements and older notices.
- [rishabhjain0295-web/nifty500-momentum-backtest](https://github.com/rishabhjain0295-web/nifty500-momentum-backtest):
  uses the official inclusion/exclusion workbook and later archive observations;
  snapshot-era event dates are approximate.
- [vishalvx/nifty-indices-datasets](https://github.com/vishalvx/nifty-indices-datasets):
  a NIFTY 50 comparison source. NIFTY500 Momentum 50 is a different index and
  must not be confused with NIFTY 500.
- [Motilal Oswal Nifty 500 Index Fund disclosures](https://www.motilaloswalmf.com/downloads/scheme-portfolio-details):
  public portfolio workbooks supply ISIN-based cross-checks. Fund holdings are
  not an authoritative index-membership register.

Host review of the Prathmesh sample confirmed that all 261
`pit_available_date` values are exactly **10 calendar days after month-end**.
That is a dataset convention, not independently observed publication timing.
Do not treat it as historical `known_at` evidence.

Host comparison of the Aditya baseline with the eleven archive observations,
using its documented half-open intervals, produced **zero exact symbol-set
matches**. Some differences may involve aliases, DVRs, temporary constituents
or timing. No automatic identity mapping or source selection was performed.
The full discrepancies are retained in
`verification/stage1/batch7/deep-research/host-archive-baseline-comparison.json`.
This is a reconciliation queue, not proof that every differing row is wrong.

### Correction to earlier membership-test claims

The upstream Aditya intervals use **`[valid_from, valid_to)`**, but the existing
local evidence-test helper uses an inclusive exit boundary. Its transition
checks skip the exact effective exit day, so their passing result does not
establish correct interval-boundary behavior.

The exported CSV also lacks announcement timestamps, although some upstream
parsed notices retain publication dates. It must not be described as complete
as-known membership based solely on `valid_from`/`valid_to`. No code or test
correction is included in this research-only work.

## Recommended free implementation

1. **Inventory existing evidence first.** Reuse the 108 official NIFTY 500
   anchors and existing unapplied changelog. Build a date/source/availability
   coverage matrix before deciding what to fetch.
2. **Recover the older event ledger from the official workbook.** Preserve
   literal company names and event dates. Reconcile identities and date
   semantics with dated exchange/index documents.
3. **Bridge snapshots with official notices.** Keep announcement and effective
   timestamps separately; apply explicit inclusions/exclusions only within
   reconciled intervals. Include off-cycle notices, amendments and reversals.
4. **Target post-March-2022 gaps.** Use dated announcements, archived official
   CSV checkpoints and independently sourced fund disclosures. Holdings may
   corroborate or challenge membership, but cannot establish it by themselves.
5. **Use community histories as discovery tools.** Recover notice URLs and
   candidate publication dates; verify each against the primary PDF. Never
   inherit inferred baseline membership silently.
6. **Reconcile both ends of each interval.** The opening source cannot supply
   the closing event's announcement date. Membership queries must use
   half-open endpoints and filter evidence by historical availability.
7. **Publish coverage honestly.** Certify only reconciled periods; keep unknown
   periods unavailable. Do not open the overall P0 gate or publish historical
   strategy results merely because downloads succeed.

**Decision:** proceed with the free documentary reconstruction route. A paid
feed is not a prerequisite for continuing. Exact full-history completeness
remains a question to test against the source inventory, not something this
research either guarantees or rules out.

Public download access does not by itself grant redistribution rights. The
Aditya repository declares MIT code and CC0 data separately; those declarations
do not license the original exchange publications. Other candidates' terms are
documented individually in the supporting audit.

## Supporting reports and evidence

- [Official archive probes](NIFTY_PIT_OFFICIAL_ARCHIVES.md)
- [Web-archive probes and provenance](NIFTY_PIT_WEB_ARCHIVES.md)
- [Community dataset audit and immutable source revisions](NIFTY_PIT_COMMUNITY_AUDIT.md)
- [Fund disclosures and alternative anchors](NIFTY_PIT_ALTERNATIVE_ANCHORS.md)
- Research artifacts: `verification/stage1/batch7/deep-research/`

Host checks covered the official workbook's counts/date range, the existing
snapshot-file inventory, four downloaded archive ZIPs, all eleven staged
web-archive CSVs and the principal community CSV measurements. They do not
certify every parsed endpoint, historical identity mapping or historical
publication time.
