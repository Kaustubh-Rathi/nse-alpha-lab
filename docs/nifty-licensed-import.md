# Licensed Nifty effective-date history

`src/nse_pipeline/nifty/licensed.py` imports an externally licensed CSV offline.
It does **not** buy data, send email, access a vendor, authenticate an official
feed, update the research universe, or promote any data to an official catalogue.
The existing research universe is **not** an official historical Nifty universe.
Fundamentals are outside this importer.

## Input contract

UTF-8 CSV (BOM allowed), one row per exact constituent interval:

| Column | Required semantics |
|---|---|
| `index` | `NIFTY50` or `NIFTY500`; map historical index titles explicitly upstream |
| `symbol` | Contemporaneous exchange symbol, not today's symbol backfilled |
| `isin` | Contemporaneous share-class ISIN, 12-character syntax; no placeholders |
| `effective_from` | Strict `YYYY-MM-DD`, inclusive |
| `effective_to` | Strict `YYYY-MM-DD`, **exclusive**; blank only for an explicitly open interval |
| `source` | Nonempty delivery/document reference, retained per row |

Optional columns:

- `known_at`: timezone-aware ISO timestamp, e.g. `2008-01-01T00:00:00Z`.
  If this column exists, **every row** needs a valid timestamp. It means the
  availability of the **entire delivered row**, including its exit date, not
  merely the announcement of entry. Do not substitute ingestion time or an
  effective date for historical availability evidence.
- `security_type`: `equity` (default), `dvr`, `dummy`, or `other`.
- `tradable`: literal lowercase `true` (default) or `false`.

Provider-declared DVR/share classes remain separate by ISIN; company names are
not used to collapse them. Dummy rows must explicitly have `security_type=dummy`
and `tradable=false`; only these rows may have blank ISINs. `DUMMY*` symbols
without explicit classification are rejected. Unknown placeholders must be
classified upstream, not guessed. Other suspended/nontrading securities must be
explicitly marked `tradable=false` by the source; the importer cannot discover
historical tradability from a symbol. `tradable=true` is a source assertion, not
proof that a price exists or an order could execute on a particular date.

Duplicate intervals and overlapping intervals for the same symbol **or** ISIN
within an index are errors, even with different provenance/knowledge timestamps.
Adjacent intervals, including symbol renames, are allowed. The same security
can belong to both indices. No count of exactly 50 or 500 is imposed: share
classes and corporate-action dummy constituents can affect security counts.
ISIN syntax is checked, not exchange identity or check-digit authenticity.

Closed intervals must have `effective_from < effective_to`. Compact dates, ISO
week dates, surrounding whitespace, unknown columns, malformed row widths and
empty deliveries fail. No snapshot-to-interval conversion, forward/backward fill,
price-based constituent inference, invented identities, or today's-member
backfill is performed. An open-ended row does **not** imply coverage forever.

## External metadata JSON

Store this separately from the CSV. Keep actual contracts and licensed payloads
in approved private storage; reference them rather than committing licence text.
The following is a **manifest template**, not real coverage or a licence grant:

```json
{
  "licence_reference": "private-contract-store/REPLACE-WITH-APPROVED-REFERENCE",
  "source_sha256": "REPLACE-WITH-SHA256-OF-EXACT-CSV-BYTES",
  "membership_kind": "licensed_effective_intervals",
  "interval_semantics": "[effective_from,effective_to)",
  "coverage": []
}
```

`coverage: []` permits validated partial import but proves no complete dates.
After independently reviewing a vendor's coverage/gap report, add entries with
exactly these fields (the values below are **synthetic structural examples**):

```json
{
  "index": "NIFTY50",
  "start": "2008-01-31",
  "end": "2009-01-01",
  "complete": true,
  "reference": "private-reviewed-gap-report-reference"
}
```

Coverage ranges are **half-open**, separately attested for each index. False
`complete` entries do not count as coverage. Overlapping coverage attestations
are rejected; adjacent ones are allowed. A complete claim that extends beyond
all rows or has dates with no rows is rejected. A file checksum binds this
metadata to the precise delivery; it does **not** prove official provenance,
licence validity, or that no constituent was omitted. In particular, the
presence of one row on every day cannot establish a complete constituent list.
The external reviewed coverage attestation remains essential.

## Use

From the repository root in PowerShell:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m nse_pipeline.nifty.licensed D:\private\members.csv D:\private\manifest.json --start 2000-01-01 --end 2026-11-08
.\.venv\Scripts\python.exe -m nse_pipeline.nifty.licensed D:\private\members.csv D:\private\manifest.json --start 2000-01-01 --end 2026-11-08 --require-full-history
.\.venv\Scripts\python.exe -m nse_pipeline.nifty.licensed D:\private\members.csv D:\private\manifest.json --index NIFTY50 --asof 2008-01-31 --known-at 2008-01-31T09:00:00+05:30 --tradable-only
```

The first command prints a coverage report even when history is incomplete.
`--require-full-history` exits **2** on any missing interval for either index.
Use `--end 2027-01-01` to request all of calendar 2026, **not** to assert that
future dates or unreceived data are available. CLI defaults request that full
calendar range and will correctly report missing coverage unless attested.
No import results or manifests are written by these commands. Query output
includes licensed rows: handle redirected stdout according to the licence.

Python API:

```python
import json
from pathlib import Path
from nse_pipeline.nifty.licensed import import_licensed_csv

history = import_licensed_csv(
    Path("D:/private/members.csv"),
    json.loads(Path("D:/private/manifest.json").read_text(encoding="utf-8")),
)
report = history.coverage_report("2000-01-01", "2026-11-08")
history.require_full_history("2000-01-01", "2026-11-08")  # raises if incomplete
rows = history.membership_asof("NIFTY50", "2008-01-31", tradable_only=True)
```

As-of queries use `effective_from <= asof < effective_to` and require a complete
coverage attestation on that date by default. `require_complete=False` is an
explicit inspection escape hatch for partial rows, **not** permission to label
the result a complete universe. Audit queries retain nontrading/dummy rows by
default; trading consumers should request `tradable_only=True`.

If `known_at` is supplied to the query, rows must also have `row.known_at <=
cutoff`; offset timestamps are compared in UTC. Without row timestamps this
query fails rather than claiming point-in-time knowledge. Effective coverage
and knowledge completeness are different: filtering can leave an incomplete or
empty result. Coverage reports therefore always say
`point_in_time_completeness_proven=false`. The importer does not implement
bitemporal correction/retraction versions; overlapping corrections must be
resolved and versioned externally, with a new digest-bound manifest. Do not
pass a restated final exit date with only an entry-announcement timestamp.

## Missing 2000 history: claim must fail

Tests use explicitly synthetic records with coverage beginning at Nifty 50
`2008-01-31` and Nifty 500 `2013-04-18`. For a requested half-open range
`[2000-01-01,2026-11-08)`, the report contains:

```json
{
  "full_history": false,
  "indices": {
    "NIFTY50": {"complete": false, "missing": [{"start": "2000-01-01", "end": "2008-01-31"}]},
    "NIFTY500": {"complete": false, "missing": [{"start": "2000-01-01", "end": "2013-04-18"}]}
  }
}
```

`require_full_history` raises `ValueError: full-history claim rejected`. These
test intervals demonstrate the gate, not real licensed data or continuity of
the retained public snapshots.

## Enquiry repair and external blockers

The retained documentary baselines in this project imply these **inclusive**
pre-baseline missing windows:

| Index | Missing window | First retained snapshot |
|---|---|---|
| Nifty 50 | 2000-01-01 through **2008-01-30** | 2008-01-31 |
| Nifty 500 | 2000-01-01 through **2013-04-17** | 2013-04-18 |

These are findings about this project's retained archive, not fresh web claims
that no other historical evidence exists. **Public monthly snapshots are not
continuous daily effective history**, including after the baselines. The
corrected purchase enquiry requests both pre-baseline windows and separately
itemised exact effective dates through the requested research cutoff; overlap
with snapshots is reconciliation evidence, not a reason to exclude interval
delivery. Both baseline joins and intra-month changes need sample verification.

For backwards compatibility, `purchase.py` retains `GAP_END=2007-12-31`, the
`gap_request.start/end/sample_dates` common-subset fields and 96-month/52,800-row
arithmetic. **None describes the full request.** The authoritative fields are
`gap_request.missing_windows` and `gap_request.exact_effective_history`.
`include_post_baseline_repurchase=false` means avoid repricing duplicate
snapshots, **not** reject licensed exact effective-date intervals. The retained
legacy Mermaid diagram's shorter report/sample labels are only a minimum subset;
the JSON `pay_guard` and repaired enquiry require the complete per-index scope.

Actual 2000 coverage, complete constituent counts, gaps, effective-date and
availability semantics, corrections, share-class/ISIN mappings, trading flags,
local-research licence, and an itemised quote must be confirmed by the provider
and independently reviewed. No current price or coverage promise is assumed.
No licensed delivery or valid contract is fabricated by this work, and nothing
was purchased or emailed. A full 2000–2026 official-history claim remains blocked
until these external data and evidence requirements are met.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_nifty_licensed.py -v
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_purchase.py -v
```

Tests cover schema/date validation, checksums/licence references, duplicates and
symbol/ISIN overlaps, half-open boundaries, share classes/dummies/nontrading rows,
knowledge cutoffs, coverage gaps and false/full-history claims, no backfill,
CLI failure status, and the enquiry's old gap-only bug.
