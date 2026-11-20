# Scheme attachment adapter — offline, cited, unverified

`src/nse_pipeline/schemes/attachments.py` is a separate adapter for cached
ZIP attachments and direct PDFs, including historical ZIP-only filings.
It does **not** modify `pdf_extract.py`, discover/download filings, parse ratios,
write ledger entries, adjust prices, or mark any scheme term verified.
There is no year restriction: input bytes, not a year-specific filename, decide
whether a cached attachment is supported. This is **not** evidence of complete
2000–2026 acquisition or scheme verification.

## Usage

Summary-only proof using the existing cache manifest:

```powershell
uv run --no-project --with pypdf python src/nse_pipeline/schemes/attachments.py --manifest data/processed/schemes/pdf_text/manifest.json --root . --limit 5
```

Add `--out data/processed/schemes/attachments` to write per-attachment JSON
evidence. Output names are SHA256s of the complete serialized evidence;
existing files are compared, never overwritten. A source file and a ZIP
member path are never used as output filenames. `--out` omitted means no
evidence/report files are written. The summary is printed on stdout.

Optional OCR, **only after the operator separately supplies local tools**:

```powershell
uv run --no-project --with pypdf --with pymupdf python src/nse_pipeline/schemes/attachments.py --manifest cached-attachments.json --root . --limit 5 --ocr --ocr-language eng --ocr-dpi 150 --ocr-timeout 30 --out data/processed/schemes/attachments
```

The module never installs Python packages or OS tools. `uv --with` above is
an explicit caller choice of an isolated optional Python dependency; it does
not change project dependencies. Tesseract must already be on PATH, with the
requested language data. No OS installation is attempted. Without `--ocr`,
no renderer or Tesseract process runs. With `--ocr`, missing PyMuPDF/Tesseract
fails explicitly as `ocr_unavailable`, even if the PDF has native text.
OCR runs only on pages with a completely blank native text layer, not sparse
text pages. Native text is retained; OCR text is separate and labelled
`method=tesseract_local`, `ocr_performed=true`, `verified=false`.
OCR-derived numbers require independent reading, never automatic promotion.

Exit codes: **0** all bounded jobs succeeded (possibly `no_pdf_members`),
**1** at least one per-attachment failure, **2** invalid manifest/options or
manifest-wide failure. Missing inputs stay missing; URLs are never fetched,
so there are no retries or 401/403/429 bypasses.

## Manifest contract

Supports the existing `pdf_extract` URL-to-record JSON map, a JSON list,
`{"attachments": [...]}`, or JSONL objects:

```json
{
  "attachments": [
    {
      "source_url": "https://nsearchives.nseindia.com/corporate/example.zip",
      "cache_path": "data/raw/schemes/attachments/example.zip",
      "sha256": "<actual 64-character SHA256 of the original cached ZIP>",
      "announcement_date": "2005-03-01",
      "symbol": "EXAMPLE"
    }
  ]
}
```

`cache_path` (alias `path`) is relative to **`--root`**, not the manifest's
directory, matching existing manifests. Resolved paths, including symlinks,
must stay inside root. URL aliases in preference order are `attchmntFile`,
`attachment_url`, `pdf_url`, `url`, `source_url`; the attachment URL should
not be replaced with its announcement-API URL. A missing/blank source URL is
an explicit `source_url_missing` failure. Original row metadata is kept
under `source_metadata`, including announcement date if supplied. No document
date is invented. Missing hashes are allowed but labelled
`manifest_hash_verified=false`; supplied hashes must match exactly.
This flag checks bytes only, not the truth of document contents.

All input is cached/offline. Use a separate authorized acquisition process
to prepare ZIP cache paths and manifests. Respect NSE licensing restrictions;
the adapter does not redistribute source PDFs.

## Reusable API and citations

```python
from nse_pipeline.schemes.attachments import adapt_attachment

evidence = adapt_attachment(
    cached_bytes,
    source_url=attachment_url,
    expected_sha256=manifest_sha256,
    text_cache={},  # optional caller-owned SHA256/settings-keyed memory cache
)
for document in evidence["documents"]:
    for page in document["pages"]:
        print(page["archive_member"], page["page_ref"], page["text"])
```

Public helpers: `Limits`, `AttachmentError.code`, `capabilities()`,
`pdf_members()`, `extract_pdf_pages()`, `load_manifest()`, `scan_manifest()`
and `write_artifact()`. Use the repository's usual `PYTHONPATH=src` when
importing the package; the script-path CLI needs no path setup.

Every page retains source URL/path, whole-attachment SHA256, PDF SHA256,
archive member name, original zero-based member index, one-based page number,
`page_ref` (`p.N`), text method, native/OCR text and an unverified marker.
For direct PDFs, member name/index are null and both hashes match.
Duplicate names/content are not silently discarded: sorting is by member
name then original index, each document has its own member identity, and
same-content members expose `duplicate_of`. Text is parsed once per digest
and settings, without copying another source's URL into a citation. Cache
state changes performance, not evidence serialization. The scan bounds its
cross-attachment text cache by evicting above 16 entries or 8 million stored
text/native-text characters. Callers of `adapt_attachment` own their cache.

Text is returned page-by-page, including blank pages. A document with no
usable text is `scanned_no_text`; a ZIP with no PDFs is `no_pdf_members`.
Neither status supplies missing terms. Attachment `extracted` means text
extraction ran, **not** that the document is searchable, correct or verified.
Blank/sparse pages still require manual review and potentially further OCR.

## Fail-closed resource limits

Defaults (overridable via `Limits`, with major byte/page limits also on CLI):

| Resource | Default |
| --- | ---: |
| Manifest bytes / selected records | 8 MiB / 50 (CLI max 1000) |
| Original cached attachment | 32 MiB |
| ZIP members, including ignored files/directories | 200 |
| Individual uncompressed member | 32 MiB |
| Total uncompressed members | 128 MiB |
| Per-member uncompressed/compressed ratio | 100 |
| Pages, each PDF and whole attachment including duplicates | 300 |
| Native + OCR characters, each PDF and whole attachment | 2,000,000 |
| OCR image pixels per page | 16,000,000 |
| OCR DPI / timeout | 150 / 30 seconds per OCR page |

Rejects path traversal with slash or backslash, roots/UNC/Windows drives,
NULs, control characters, ADS colons, DOS reserved names and trailing
dot/space aliases; Unix symlinks/special files and Windows reparse points;
encryption flags in central or local headers; encrypted PDFs; malformed
archives/CRC errors, including in ignored files; unsupported compression;
nested ZIPs; multi-disk/ZIP64 archives; byte/member/page/text/ratio overflows;
and `.pdf` members without exact `%PDF-` signatures. Only stored/deflated,
single-disk ZIPs are supported. No member paths are extracted to disk.
Central-directory counts and bounds are checked before allocating ZipInfos,
then metadata and bounded reads are checked before any PDF extraction.
The entire archive is rejected atomically on a member safety failure.

PDF libraries are **not a security sandbox**: page/text checks cannot enforce
hard PDF-parser decompression, CPU or memory ceilings. Run hostile PDFs in an
OS-sandboxed worker with process resource limits if that threat model applies.
Tesseract subprocesses have explicit per-page timeouts and bounded image
dimensions. OCR images exist only in a temporary directory, then are removed.

## Tests and bounded proof

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p test_scheme_attachments.py -v
uv run --no-project --with pypdf python -m unittest discover -s tests -p test_scheme_attachments.py -v
uv run --no-project --with pymupdf python -m unittest discover -s tests -p test_scheme_attachments.py -v
uv run --no-project --with pypdf python -m unittest discover -s tests -p test_pdf_extract.py -v
```

Results: **35/35** attachment tests pass with pypdf and independently with
PyMuPDF; the base environment passes 34 with one real-PDF test skipped.
The unchanged `pdf_extract` regression suite passes **22/22**.

The cached five-job PDF proof extracted **31 cited pages** (four unique PDF
hashes, five source filings), with **0 OCR pages / 0 verified terms**.
No ZIP attachments were present in the inspected scheme-cache directories;
three sampled cached PR archives had no PDF members. ZIP-to-PDF extraction
is proven with a real two-page fixture, not claimed as historical coverage.
See `data/reports/scheme-attachments.json` for proof counts and limitations.
