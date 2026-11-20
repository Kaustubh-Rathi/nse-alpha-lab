"""Tests for the bounded scheme-PDF extraction pipeline (no network).

PDF coverage proves: queue/index plumbing, cache+SHA256, rate-limited
download with stop-on-429, half-year API windows, pypdf text extraction
with scanned flagging, page-separated text output, ratios over extracted
text with page citations, fail-closed behaviour, and coverage math.
"""

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "pdf_extract", Path(__file__).resolve().parents[1]
    / "src/nse_pipeline/schemes/pdf_extract.py")
pdf_extract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pdf_extract)

# Sibling ratios module for cross-checks (same file the pipeline uses).
RATIOS_SPEC = importlib.util.spec_from_file_location(
    "ratios_for_pdf_extract", Path(__file__).resolve().parents[1]
    / "src/nse_pipeline/schemes/ratios.py")
ratios = importlib.util.module_from_spec(RATIOS_SPEC)
RATIOS_SPEC.loader.exec_module(ratios)

PDF_URL = ("https://nsearchives.nseindia.com/corporate/"
           "SANOFI_10052023134914_Reg30.pdf")
RATIO_TEXT = ("The Company shall issue 1 equity share to be issued for "
              "every 1 equity share held in Sanofi India Limited.")


def make_pdf_bytes(pages_text):
    """Build a real PDF with a text layer (skips if pypdf unavailable)."""
    try:
        from pypdf import PdfWriter
    except ImportError:
        raise unittest.SkipTest("pypdf not installed (run: uv run --with pypdf ...)")
    from pypdf import PdfWriter
    from pypdf.generic import ContentStream, DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for text in pages_text:
        page = writer.add_blank_page(width=612, height=792)
        # Register a real Helvetica font under /F1 in the existing
        # resources so pypdf can extract the text-showing stream.
        resources = page["/Resources"].get_object()
        if "/Font" not in resources:
            resources[NameObject("/Font")] = writer._add_object(DictionaryObject())
        fonts = resources["/Font"].get_object()
        font = writer._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
        }))
        fonts[NameObject("/F1")] = font
        stream = DecodedStreamObject()
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        data = f"BT /F1 10 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        stream.set_data(data)
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class FakeFetcher:
    """Injectable fetcher: maps URL -> bytes | HTTP status int | exception."""

    def __init__(self, mapping):
        self.mapping = dict(mapping)
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        value = self.mapping.get(url)
        if value is None:
            raise pdf_extract.HttpStatusError(404, url, "not found")
        if isinstance(value, int):
            raise pdf_extract.HttpStatusError(value, url, f"HTTP {value}")
        if isinstance(value, Exception):
            raise value
        return value


def limiter0():
    return pdf_extract.RateLimiter(0.0)


def job(url=PDF_URL, symbol="SANOFI", **kw):
    base = {"symbol": symbol, "announcement_date": "2023-05-10",
            "pdf_url": url, "source": "scheme-index",
            "scheme_id": "SLG-SANOFI-2023-05-10", "withdrawn": False}
    base.update(kw)
    return base


class WindowAndUrlTests(unittest.TestCase):
    def test_half_year_windows(self):
        self.assertEqual(pdf_extract.half_year_windows(2024),
                         [("01-01-2024", "30-06-2024"),
                          ("01-07-2024", "31-12-2024")])

    def test_api_url_format_and_symbol_validation(self):
        url = pdf_extract.api_url("sanofi", "01-01-2023", "30-06-2023")
        self.assertIn("api/corporate-announcements", url)
        self.assertIn("symbol=SANOFI", url)
        self.assertIn("from_date=01-01-2023", url)
        with self.assertRaises(ValueError):
            pdf_extract.api_url("BAD SYMBOL!", "01-01-2023", "30-06-2023")

    def test_is_scheme_row_and_dedup(self):
        self.assertTrue(pdf_extract.is_scheme_row(
            {"desc": "Scheme of Arrangement"}))
        self.assertTrue(pdf_extract.is_scheme_row(
            {"desc": "Updates",
             "attchmntText": "Order of NCLT in the Scheme of Arrangement"}))
        self.assertFalse(pdf_extract.is_scheme_row(
            {"desc": "Outcome of Board Meeting"}))
        row = {"symbol": "SANOFI", "an_dt": "10-May-2023 13:49:14",
               "attchmntFile": PDF_URL, "desc": "Scheme of Arrangement"}
        self.assertEqual(pdf_extract.dedup_key(row),
                         ("SANOFI", "10-May-2023 13:49:14", PDF_URL,
                          "Scheme of Arrangement"))
        api_jobs = pdf_extract.jobs_from_api_rows([row])
        self.assertEqual(api_jobs[0]["announcement_date"], "2023-05-10")


class DiscoveryTests(unittest.TestCase):
    def _window_payload(self, symbol="BALKRISIND", dates=None):
        rows = []
        for i, (an_dt, date) in enumerate(dates or
                [("15-May-2024 10:00:00", "2024-05-15")]):
            rows.append({
                "symbol": symbol, "an_dt": an_dt,
                "desc": "Scheme of Arrangement",
                "attchmntFile": (f"https://nsearchives.nseindia.com/"
                                 f"corporate/{symbol}_{i}01012024100000_Reg30.pdf"),
                "attchmntText": "Board approved a scheme"})
        return json.dumps(rows).encode()

    def test_half_year_discovery_filters_and_dedups(self):
        fetcher = FakeFetcher({
            pdf_extract.api_url("BALKRISIND", "01-01-2024", "30-06-2024"):
                self._window_payload(),
            pdf_extract.api_url("BALKRISIND", "01-07-2024", "31-12-2024"):
                self._window_payload(dates=[("15-May-2024 10:00:00",
                                             "2024-05-15")]),  # duplicate row
        })
        found = pdf_extract.discover_scheme_filings(
            "BALKRISIND", (2024,), fetcher, limiter0())
        self.assertIsNone(found["stopped"])
        self.assertEqual(found["windows_tried"], 2)
        self.assertEqual(len(found["rows"]), 1)  # deduped across windows
        self.assertEqual(len(fetcher.calls), 2)  # one request per window

    def test_discovery_stops_on_429(self):
        fetcher = FakeFetcher({
            pdf_extract.api_url("BALKRISIND", "01-01-2024", "30-06-2024"): 429})
        found = pdf_extract.discover_scheme_filings(
            "BALKRISIND", (2024,), fetcher, limiter0())
        self.assertIn("429", found["stopped"])
        self.assertEqual(len(fetcher.calls), 1)  # no second window after 429

    def test_discovery_rejects_other_symbol_and_non_pdf(self):
        payload = json.dumps([
            {"symbol": "OTHER", "an_dt": "x", "desc": "Scheme of Arrangement",
             "attchmntFile": "https://x/A.pdf"},
            {"symbol": "BALKRISIND", "an_dt": "y", "desc": "Outcome of Board",
             "attchmntFile": "https://x/B.pdf"},
            {"symbol": "BALKRISIND", "an_dt": "z", "desc": "Merger",
             "attchmntFile": ""},
        ]).encode()
        fetcher = FakeFetcher(
            {pdf_extract.api_url("BALKRISIND", "01-01-2023", "30-06-2023"):
             payload})
        found = pdf_extract.discover_scheme_filings(
            "BALKRISIND", (2023,), fetcher, limiter0())
        self.assertEqual(found["rows"], [])


class DownloadTests(unittest.TestCase):
    def test_download_caches_with_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "pdfs"
            payload = b"%PDF-1.7 fake body"
            fetcher = FakeFetcher({PDF_URL: payload})
            manifest = {}
            record = pdf_extract.download_one(
                PDF_URL, cache, manifest, fetcher, limiter0())
            self.assertEqual(record["status"], "downloaded")
            self.assertEqual(record["sha256"],
                             pdf_extract.compute_sha256(payload))
            self.assertTrue((cache / record["cache_path"].
                             split("pdfs")[-1].lstrip("\\/")).exists()
                            or Path(record["cache_path"]).exists())
            # Second call hits cache: no additional fetch.
            record2 = pdf_extract.download_one(
                PDF_URL, cache, manifest, fetcher, limiter0())
            self.assertEqual(record2["status"], "cached")
            self.assertEqual(len(fetcher.calls), 1)

    def test_not_a_pdf_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = FakeFetcher({PDF_URL: b"<html>blocked</html>"})
            record = pdf_extract.download_one(
                PDF_URL, Path(tmp) / "pdfs", {}, fetcher, limiter0())
            self.assertEqual(record["status"], "not_a_pdf")
            self.assertFalse(Path(record["cache_path"]).exists())

    def test_download_stops_on_429_and_403(self):
        for status in (429, 403):
            with tempfile.TemporaryDirectory() as tmp:
                fetcher = FakeFetcher({PDF_URL: status})
                records, stopped = pdf_extract.download_jobs(
                    [job()], Path(tmp) / "pdfs", {}, fetcher, limiter0())
                self.assertTrue(stopped, f"status {status} must stop")
                self.assertEqual(records[0]["status"], "stopped")

    def test_download_continues_after_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = FakeFetcher({})  # every URL 404s
            jobs = [job(url="https://x/A.pdf"), job(url="https://x/B.pdf")]
            records, stopped = pdf_extract.download_jobs(
                jobs, Path(tmp) / "pdfs", {}, fetcher, limiter0())
            self.assertFalse(stopped)
            self.assertEqual([r["status"] for r in records],
                             ["fetch_failed", "fetch_failed"])

    def test_rate_limiter_enforces_interval(self):
        sleeps = []
        clock = {"t": 100.0}
        limiter = pdf_extract.RateLimiter(
            2.0, clock=lambda: clock["t"], sleeper=sleeps.append)
        limiter.wait()   # first call: no sleep
        limiter.wait()   # clock frozen -> sleeps the full interval
        self.assertEqual(limiter.calls, 2)
        self.assertTrue(sleeps and abs(sleeps[0] - 2.0) < 1e-9)


class ExtractionTests(unittest.TestCase):
    def test_text_extracted_and_page_citation(self):
        pdf_bytes = make_pdf_bytes(["Cover page. ", RATIO_TEXT])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scheme.pdf"
            path.write_bytes(pdf_bytes)
            pages, status = pdf_extract.extract_pages_safe(path)
            self.assertEqual(status, "text_extracted")
            row = pdf_extract.build_batch_row(
                job(), {"status": "downloaded", "cache_path": str(path),
                        "sha256": pdf_extract.compute_sha256(pdf_bytes)},
                pages, status, "scheme.txt")
            self.assertEqual((row["ratio_num"], row["ratio_den"]), (1, 1))
            self.assertEqual(row["page_ref"], "p.2")
            self.assertTrue(row["snippet"])
            self.assertFalse(row["ratio_from_price"])
            ratios.validate_ratio_record(row)

    def test_scanned_no_text_flagged_never_inferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blank.pdf"
            path.write_bytes(make_pdf_bytes([" "]))
            pages, status = pdf_extract.extract_pages_safe(path)
            self.assertEqual(status, "scanned_no_text")
            row = pdf_extract.build_batch_row(
                job(), {"status": "downloaded", "cache_path": str(path)},
                pages, status, None)
            self.assertIsNone(row["ratio_num"])
            self.assertEqual(row["status"], "scanned_no_text")
            self.assertIn("OCR", row["extraction"]["ocr_note"])

    def test_no_explicit_ratio_fails_closed(self):
        pages = ["The Board has approved a Scheme of Arrangement. "
                 "Details are in the scheme document."]
        row = pdf_extract.build_batch_row(
            job(), {"status": "cached", "cache_path": "x.pdf"}, pages,
            "text_extracted", "x.txt")
        self.assertIsNone(row["ratio_num"])
        self.assertIsNone(row["cash_per_share"])
        self.assertEqual(row["status"], "no_explicit_ratio")

    def test_text_file_has_page_markers_and_no_ratio_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            pages = ["First page text.", "Second page text."]
            path = pdf_extract.write_text_file(
                out, "scheme.txt", pages,
                {"pdf_url": PDF_URL, "symbol": "SANOFI",
                 "announcement_date": "2023-05-10",
                 "sha256": "abc123", "extract_status": "text_extracted"})
            content = path.read_text(encoding="utf-8")
            self.assertIn("===== PAGE 1 =====", content)
            self.assertIn("===== PAGE 2 =====", content)
            self.assertIn(PDF_URL, content)
            self.assertNotIn("ratio_num", content)  # header makes no claims

    def test_negated_cash_demoted_but_legit_cash_kept(self):
        # A doc that says "no cash consideration" must not admit a cash leg,
        # even when a bare amount matches (face-value false positive).
        pages = ["There is no cash consideration or other consideration. "
                 "Equity share of INR 10 each fully paid up."]
        row = pdf_extract.build_batch_row(
            job(), {"status": "cached", "cache_path": "x.pdf"}, pages,
            "text_extracted", "x.txt")
        self.assertEqual(row["status"], "needs_review_negated_cash")
        self.assertIsNone(row["cash_per_share"])
        self.assertTrue(row["cash_demoted"])
        # Contrast: an explicit cash leg without negation stays a candidate.
        legit = ["cash consideration of Rs. 25 per equity share shall be paid "
                 "to each shareholder"]
        row2 = pdf_extract.build_batch_row(
            job(), {"status": "cached", "cache_path": "x.pdf"}, legit,
            "text_extracted", "x.txt")
        self.assertEqual(row2["status"], "candidate")
        self.assertEqual(row2["cash_per_share"], 25.0)

    def test_face_value_and_cost_cash_demoted(self):
        face_value = ["Shares having face value of Rs.2/- "
                      "(Rupees Two only), with one vote per equity share."]
        row = pdf_extract.build_batch_row(
            job(), {"status": "cached", "cache_path": "x.pdf"}, face_value,
            "text_extracted", "x.txt")
        self.assertEqual(row["status"], "needs_review_face_value_cash")
        self.assertIsNone(row["cash_per_share"])
        self.assertEqual(row["cash_amount_rejected"], 2.0)

        cost = ["The Court doth further order payment of Rs.2,500/- "
                "in aggregate as the cost of this petition."]
        row2 = pdf_extract.build_batch_row(
            job(), {"status": "cached", "cache_path": "x.pdf"}, cost,
            "text_extracted", "x.txt")
        self.assertEqual(row2["status"], "needs_review_cost_cash")
        self.assertIsNone(row2["cash_per_share"])
        self.assertEqual(row2["cash_amount_rejected"], 2500.0)

    def test_sparse_text_flag_for_image_heavy_pdf(self):
        pages = ["Only the cover letter of the filing is text."] + [""] * 48
        row = pdf_extract.build_batch_row(
            job(), {"status": "cached", "cache_path": "x.pdf"}, pages,
            "text_extracted", "x.txt")
        self.assertEqual(row["extraction"]["status"], "text_extracted")
        self.assertTrue(row["extraction"]["sparse_text"])
        dense = pdf_extract.build_batch_row(
            job(), {"status": "cached", "cache_path": "x.pdf"},
            ["D" * 2000], "text_extracted", "x.txt")
        self.assertFalse(dense["extraction"]["sparse_text"])

    def test_extract_missing_pypdf_fails_closed(self):
        # Point the extractor's sibling at a missing file: missing PDF path.
        pages, status = pdf_extract.extract_pages_safe(Path("nope-missing.pdf"))
        self.assertEqual(pages, [])
        self.assertEqual(status, "pdf_text_extract_unavailable")


class EndToEndTests(unittest.TestCase):
    def test_run_produces_text_ratios_and_manifest(self):
        pdf_bytes = make_pdf_bytes([RATIO_TEXT])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            index = tmp / "index.jsonl"
            index.write_text(json.dumps({
                "symbol": "SANOFI", "announcement_date": "2023-05-10",
                "an_dt": "10-May-2023 13:49:14", "desc": "Scheme of Arrangement",
                "attchmntFile": PDF_URL, "withdrawn": False}) + "\n")
            queue = tmp / "queue.json"
            queue.write_text("[]")
            fetcher = FakeFetcher({PDF_URL: pdf_bytes})
            coverage = pdf_extract.run(
                tmp, index, queue, tmp / "no-ledger.jsonl",
                tmp / "out", limit=50, min_interval=0.0,
                use_api=False, fetcher=fetcher)
            self.assertEqual(coverage["pdfs_downloaded"], 1)
            self.assertEqual(coverage["text_extracted_pct"], 100.0)
            self.assertEqual(coverage["ratios_found_candidates"], 1)
            self.assertFalse(coverage["ratios_inferred_from_price"])
            self.assertTrue((tmp / "out" / "ratios_batch.jsonl").exists())
            self.assertTrue((tmp / "out" / "manifest.json").exists())
            self.assertTrue(list((tmp / "out").glob("*.txt")))
            row = json.loads((tmp / "out" / "ratios_batch.jsonl")
                             .read_text().splitlines()[0])
            self.assertEqual(row["page_ref"], "p.1")
            self.assertEqual(row["symbol"], "SANOFI")

    def test_jobs_from_index_dedups_and_caps(self):
        rows = [{"symbol": "S", "announcement_date": "2024-01-01",
                 "attchmntFile": PDF_URL},
                {"symbol": "S", "announcement_date": "2024-01-01",
                 "attchmntFile": PDF_URL},  # dup
                {"symbol": "S", "announcement_date": "2024-02-01",
                 "attchmntFile": "https://x/B.pdf"}]
        jobs = pdf_extract.jobs_from_index(rows, limit=1)
        self.assertEqual(len(jobs), 1)

    def test_summarize_extrapolation(self):
        rows = [{"status": "candidate", "extraction": {"status": "text_extracted"}},
                {"status": "scanned_no_text", "extraction": {"status": "scanned_no_text"}}]
        coverage = pdf_extract.summarize(rows, [{"status": "downloaded"},
                                                {"status": "cached"}], {}, 10.0)
        self.assertEqual(coverage["text_extracted_pct"], 50.0)
        self.assertEqual(coverage["scanned_pct"], 50.0)
        self.assertEqual(coverage["ratios_found_candidates"], 1)
        self.assertFalse(coverage["ratios_inferred_from_price"])
        self.assertAlmostEqual(coverage["extrapolated_330_jobs_hours"],
                               330 * 5.0 / 3600.0, places=2)


if __name__ == "__main__":
    unittest.main()
