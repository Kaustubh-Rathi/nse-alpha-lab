"""Offline attachment safety, deterministic provenance, optional local OCR."""

import contextlib
import importlib.util
import io
import json
import stat
import struct
import subprocess
import tempfile
import types
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "scheme_attachments", Path(__file__).resolve().parents[1]
    / "src/nse_pipeline/schemes/attachments.py")
attachments = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(attachments)

PDF = b"%PDF-1.4\nfixture (parser mocked in safety tests)"
URL = "https://nsearchives.nseindia.com/corporate/historical.zip"
NO_DEPS = {"pypdf": False, "pymupdf": False, "tesseract": None}


def make_zip(members, compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=compression) as archive:
            for name, content in members:
                archive.writestr(name, content)
    return stream.getvalue()


def page_records(*texts):
    return [{"page": index, "page_ref": f"p.{index}", "text": text,
             "native_text": text, "ocr_text": None, "method": "fixture",
             "ocr_performed": False} for index, text in enumerate(texts, 1)]


def real_pdf(texts):
    """Small real PDF, generated without a PDF-library test dependency."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in texts:
        page_number = len(objects) + 1
        kids.append(f"{page_number} 0 R")
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_number + 1} 0 R >>").encode())
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    content = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(content))
        content += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(content)
    content += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    content += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return content + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


class ZipSafetyTests(unittest.TestCase):
    def rejected(self, content, code, limits=None):
        with self.assertRaises(attachments.AttachmentError) as raised:
            attachments.pdf_members(content, limits or attachments.Limits())
        self.assertEqual(raised.exception.code, code)

    def test_direct_pdf_and_pdf_members_are_in_memory(self):
        self.assertEqual(attachments.pdf_members(PDF)[0]["content"], PDF)
        payload = make_zip([("folder/", b""), ("folder/a.pdf", PDF), ("readme.txt", b"notes")])
        result = attachments.pdf_members(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["archive_member"], "folder/a.pdf")
        self.assertEqual(result[0]["member_index"], 1)

    def test_pdf_signature_not_extension_decides_content(self):
        self.assertEqual(len(attachments.pdf_members(make_zip([("document.bin", PDF)]))), 1)
        self.rejected(make_zip([("fake.pdf", b"<html>blocked</html>")]), "invalid_pdf_signature")
        self.rejected(b"<html>blocked</html>", "unsupported_attachment")
        self.rejected(b" \n%PDF-1.4", "unsupported_attachment")

    def test_malicious_paths_fail_whole_archive(self):
        for name in ("../evil.pdf", "a/../evil.pdf", "..\\evil.pdf", "/evil.pdf",
                     "C:/evil.pdf", "C:evil.pdf", "\\\\server\\evil.pdf", "\\evil.pdf",
                     "a//b.pdf", "a/./b.pdf", "a.pdf:stream", "CON.pdf", "a./b.pdf",
                     "a /b.pdf", "folder/NUL.txt"):
            with self.subTest(name=name):
                self.rejected(make_zip([("good.pdf", PDF), (name, PDF)]), "unsafe_member_path")

    def test_nul_filename_rejected_even_after_zipinfo_truncation(self):
        content = make_zip([("abc.pdf", PDF)]).replace(b"abc.pdf", b"a\x00c.pdf")
        self.rejected(content, "unsafe_member_path")

    def test_symlink_and_special_members_rejected(self):
        for mode in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            info = zipfile.ZipInfo("link.pdf")
            info.create_system = 3
            info.external_attr = (mode | 0o777) << 16
            self.rejected(make_zip([(info, b"target.pdf")]), "unsafe_member_type")
        info = zipfile.ZipInfo("windows-link.pdf")
        info.create_system = 0
        info.external_attr = 0x400
        self.rejected(make_zip([(info, PDF)]), "unsafe_member_type")

    def test_encryption_flag_in_zip_rejected_without_password_attempt(self):
        payload = bytearray(make_zip([("encrypted.pdf", PDF)]))
        local = payload.index(b"PK\x03\x04")
        central = payload.index(b"PK\x01\x02")
        struct.pack_into("<H", payload, local + 6, 1)
        struct.pack_into("<H", payload, central + 8, 1)
        self.rejected(bytes(payload), "encrypted_archive")
        struct.pack_into("<H", payload, central + 8, 0)
        self.rejected(bytes(payload), "encrypted_archive")

    def test_corrupt_zip_and_crc_in_ignored_member_rejected(self):
        self.rejected(b"PKbroken", "corrupt_archive")
        payload = make_zip([("a.pdf", PDF), ("notes.txt", b"CRC_SENTINEL")])
        self.rejected(payload.replace(b"CRC_SENTINEL", b"CRC_CORRUPT!"), "corrupt_archive")
        self.rejected(payload[:-4], "corrupt_archive")

    def test_member_count_bound_before_zipfile_construction(self):
        payload = make_zip([("a.pdf", PDF), ("b.pdf", PDF)])
        with patch.object(attachments.zipfile, "ZipFile", side_effect=AssertionError("must not parse")):
            self.rejected(payload, "member_count_limit", attachments.Limits(max_members=1))

    def test_forged_directory_count_cannot_bypass_allocation_bound(self):
        payload = bytearray(make_zip([("a.pdf", PDF), ("b.pdf", PDF)]))
        end = payload.rindex(b"PK\x05\x06")
        struct.pack_into("<HH", payload, end + 8, 1, 1)
        with patch.object(attachments.zipfile, "ZipFile", side_effect=AssertionError("must not parse")):
            self.rejected(bytes(payload), "member_count_limit", attachments.Limits(max_members=1))

    def test_attachment_member_and_total_byte_bounds(self):
        payload = make_zip([("a.pdf", PDF), ("b.pdf", PDF)])
        self.rejected(payload, "attachment_size_limit", attachments.Limits(max_attachment_bytes=10))
        self.rejected(payload, "member_size_limit", attachments.Limits(max_member_bytes=10))
        self.rejected(payload, "total_size_limit", attachments.Limits(max_total_bytes=len(PDF)))
        self.rejected(PDF, "member_size_limit", attachments.Limits(max_total_bytes=10))

    def test_deflate_bomb_and_non_pdf_bomb_rejected(self):
        for name in ("bomb.pdf", "ignored.txt"):
            payload = make_zip([(name, b"A" * 100_000)], zipfile.ZIP_DEFLATED)
            self.rejected(payload, "compression_ratio_limit")

    def test_valid_deflated_pdf_and_empty_zip(self):
        payload = make_zip([("a.pdf", PDF)], zipfile.ZIP_DEFLATED)
        self.assertEqual(attachments.pdf_members(payload)[0]["content"], PDF)
        self.assertEqual(attachments.pdf_members(make_zip([])), [])

    def test_nested_zip_and_unsupported_compression_fail_explicitly(self):
        self.rejected(make_zip([("inner.zip", make_zip([("a.pdf", PDF)]))]), "nested_archive")
        self.rejected(make_zip([("a.pdf", PDF)], zipfile.ZIP_BZIP2), "unsupported_compression")

    def test_zip64_and_appended_data_fail_explicitly(self):
        payload = bytearray(make_zip([("a.pdf", PDF)]))
        end = payload.rindex(b"PK\x05\x06")
        struct.pack_into("<HH", payload, end + 8, 65535, 65535)
        self.rejected(bytes(payload), "unsupported_archive")
        self.rejected(make_zip([]) + b"trailing", "corrupt_archive")

    def test_invalid_limits(self):
        for limits in (attachments.Limits(max_members=0), attachments.Limits(max_member_bytes=1.5),
                       attachments.Limits(max_compression_ratio=float("nan"))):
            with self.assertRaises(ValueError):
                attachments.pdf_members(PDF, limits)


class ProvenanceTests(unittest.TestCase):
    def test_missing_source_url_and_unsafe_archive_do_not_call_pdf_parser(self):
        for payload, url, code in ((PDF, "", "source_url_missing"),
                                   (make_zip([("a.pdf", PDF), ("../evil.pdf", PDF)]), URL, "unsafe_member_path")):
            with patch.object(attachments, "extract_pdf_pages") as extract:
                with self.assertRaises(attachments.AttachmentError) as raised:
                    attachments.adapt_attachment(payload, source_url=url)
                self.assertEqual(raised.exception.code, code)
                extract.assert_not_called()

    def test_attachment_wide_page_and_text_limits_count_duplicate_evidence(self):
        payload = make_zip([("a.pdf", PDF), ("b.pdf", PDF)])
        for limits, code in ((attachments.Limits(max_pages=1), "page_count_limit"),
                             (attachments.Limits(max_text_chars=6), "text_size_limit")):
            with patch.object(attachments, "extract_pdf_pages", return_value=page_records("1234")):
                with self.assertRaises(attachments.AttachmentError) as raised:
                    attachments.adapt_attachment(payload, source_url=URL, limits=limits)
                self.assertEqual(raised.exception.code, code)

    def test_per_page_provenance_duplicates_and_no_verification(self):
        payload = make_zip([("z.pdf", PDF), ("a.pdf", PDF), ("a.pdf", PDF)])
        digest = attachments.sha256(payload)
        with patch.object(attachments, "extract_pdf_pages", return_value=page_records("Cover", "1 for 2")) as extract:
            result = attachments.adapt_attachment(payload, source_url=URL, source_path="cache.zip",
                                                   expected_sha256=digest,
                                                   metadata={"announcement_date": "2005-03-01"})
        self.assertEqual(extract.call_count, 1)
        docs = result["documents"]
        self.assertEqual([(d["archive_member"], d["member_index"]) for d in docs],
                         [("a.pdf", 1), ("a.pdf", 2), ("z.pdf", 0)])
        self.assertIsNone(docs[0]["duplicate_of"])
        self.assertEqual(docs[1]["duplicate_of"], docs[0]["document_id"])
        page = docs[1]["pages"][1]
        self.assertEqual(page["page_ref"], "p.2")
        self.assertEqual(page["source_url"], URL)
        self.assertEqual(page["attachment_sha256"], digest)
        self.assertEqual(page["pdf_sha256"], attachments.sha256(PDF))
        self.assertEqual(page["archive_member"], "a.pdf")
        self.assertEqual(page["member_index"], 2)
        self.assertFalse(page["verified"])
        self.assertTrue(page["requires_independent_review"])
        self.assertNotIn("ratio_num", result)
        self.assertTrue(result["manifest_hash_verified"])
        self.assertEqual(result["source_metadata"]["announcement_date"], "2005-03-01")
        docs[0]["pages"][0]["text"] = "modified"
        self.assertEqual(docs[1]["pages"][0]["text"], "Cover")

    def test_cross_source_cache_keeps_each_citation_and_is_deterministic(self):
        cache = {}
        with patch.object(attachments, "extract_pdf_pages", return_value=page_records("text")) as extract:
            a = attachments.adapt_attachment(PDF, source_url="https://a", text_cache=cache)
            b = attachments.adapt_attachment(PDF, source_url="https://b", text_cache=cache)
            again = attachments.adapt_attachment(PDF, source_url="https://a", text_cache=cache)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(a, again)
        self.assertEqual(b["documents"][0]["pages"][0]["source_url"], "https://b")
        self.assertFalse(a["manifest_hash_verified"])

    def test_hash_mismatch_and_malformed_manifest_hash_fail_before_parser(self):
        with patch.object(attachments, "extract_pdf_pages") as extract:
            for expected, code in (("0" * 64, "hash_mismatch"), (7, "invalid_manifest_hash")):
                with self.assertRaises(attachments.AttachmentError) as raised:
                    attachments.adapt_attachment(PDF, source_url=URL, expected_sha256=expected)
                self.assertEqual(raised.exception.code, code)
            extract.assert_not_called()

    def test_scanned_and_no_pdf_statuses_do_not_imply_terms(self):
        with patch.object(attachments, "extract_pdf_pages", return_value=page_records("")):
            result = attachments.adapt_attachment(PDF, source_url=URL)
        self.assertEqual(result["documents"][0]["status"], "scanned_no_text")
        result = attachments.adapt_attachment(make_zip([("readme.txt", b"notes")]), source_url=URL)
        self.assertEqual(result["status"], "no_pdf_members")


class ExtractionAndOcrTests(unittest.TestCase):
    def fake_reader(self, *texts, encrypted=False):
        return types.SimpleNamespace(is_encrypted=encrypted,
                                     pages=[types.SimpleNamespace(extract_text=lambda t=t: t) for t in texts])

    def test_no_text_extraction_dependency_is_explicit(self):
        with patch.object(attachments, "capabilities", return_value=NO_DEPS):
            with self.assertRaises(attachments.AttachmentError) as raised:
                attachments.extract_pdf_pages(PDF)
        self.assertEqual(raised.exception.code, "pdf_text_extract_unavailable")

    def test_requested_ocr_unavailable_fails_even_with_a_text_layer(self):
        for deps in (NO_DEPS, {"pypdf": True, "pymupdf": True, "tesseract": None},
                     {"pypdf": True, "pymupdf": False, "tesseract": "tesseract"}):
            with patch.object(attachments, "capabilities", return_value=deps):
                with self.assertRaises(attachments.AttachmentError) as raised:
                    attachments.adapt_attachment(PDF, source_url=URL, ocr=True)
                self.assertEqual(raised.exception.code, "ocr_unavailable")

    def test_native_text_page_and_char_bounds_encryption(self):
        deps = {**NO_DEPS, "pypdf": True}
        cases = [(self.fake_reader("a", "b"), attachments.Limits(max_pages=1), "page_count_limit"),
                 (self.fake_reader("12345"), attachments.Limits(max_text_chars=4), "text_size_limit"),
                 (self.fake_reader("a", encrypted=True), attachments.Limits(), "encrypted_pdf"),
                 (self.fake_reader(), attachments.Limits(), "invalid_pdf")]
        for reader, limits, code in cases:
            with patch.object(attachments, "capabilities", return_value=deps), patch.dict(
                    "sys.modules", {"pypdf": types.SimpleNamespace(PdfReader=lambda _: reader)}):
                with self.assertRaises(attachments.AttachmentError) as raised:
                    attachments.extract_pdf_pages(PDF, limits=limits)
                self.assertEqual(raised.exception.code, code)

    def test_default_does_not_run_ocr_and_preserves_blank_pages(self):
        reader = self.fake_reader("Cover", "")
        with patch.object(attachments, "capabilities", return_value={**NO_DEPS, "pypdf": True}), patch.dict(
                "sys.modules", {"pypdf": types.SimpleNamespace(PdfReader=lambda _: reader)}), patch.object(
                attachments, "_ocr_page", side_effect=AssertionError("OCR was not requested")):
            pages = attachments.extract_pdf_pages(PDF)
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1]["text"], "")
        self.assertFalse(pages[1]["ocr_performed"])

    def test_opt_in_ocr_only_blank_pages_retains_native_and_marks_method(self):
        reader = self.fake_reader("Cover", "")

        class Document(list):
            is_encrypted = False
            def close(self):
                self.closed = True

        document = Document(["rendered cover", "rendered scanned page"])
        deps = {"pypdf": True, "pymupdf": True, "tesseract": "local-tesseract"}
        with patch.object(attachments, "capabilities", return_value=deps), patch.dict(
                "sys.modules", {"pypdf": types.SimpleNamespace(PdfReader=lambda _: reader),
                                "pymupdf": types.SimpleNamespace(open=lambda **_: document)}), patch.object(
                attachments, "_ocr_page", return_value="OCR 1 for 2") as ocr:
            result = attachments.adapt_attachment(PDF, source_url=URL, ocr=True)
        pages = result["documents"][0]["pages"]
        self.assertEqual(ocr.call_count, 1)
        self.assertEqual(ocr.call_args.args[0], "rendered scanned page")
        self.assertEqual(pages[0]["text"], "Cover")
        self.assertEqual(pages[1]["ocr_text"], "OCR 1 for 2")
        self.assertEqual(pages[1]["native_text"], "")
        self.assertEqual(pages[1]["method"], "tesseract_local")
        self.assertFalse(pages[1]["verified"])
        self.assertTrue(document.closed)

    def test_tesseract_subprocess_timeout_and_pixel_bound(self):
        page = types.SimpleNamespace(rect=types.SimpleNamespace(width=612, height=792),
                                     get_pixmap=lambda **_: types.SimpleNamespace(save=lambda _: None))
        with patch.object(attachments.subprocess, "run", side_effect=subprocess.TimeoutExpired("tesseract", 1)):
            with self.assertRaises(attachments.AttachmentError) as raised:
                attachments._ocr_page(page, "tesseract", "eng", 150, 1, attachments.Limits())
        self.assertEqual(raised.exception.code, "ocr_timeout")
        with patch.object(attachments.subprocess, "run") as run:
            with self.assertRaises(attachments.AttachmentError) as raised:
                attachments._ocr_page(page, "tesseract", "eng", 150, 1,
                                      attachments.Limits(max_ocr_pixels=10))
            run.assert_not_called()
        self.assertEqual(raised.exception.code, "ocr_pixel_limit")

    def test_invalid_signature_and_invalid_ocr_settings(self):
        with self.assertRaises(attachments.AttachmentError) as raised:
            attachments.extract_pdf_pages(b"not a PDF")
        self.assertEqual(raised.exception.code, "invalid_pdf_signature")
        for settings in ({"ocr_language": "../eng"}, {"ocr_dpi": 1000}, {"ocr_timeout": 0}):
            with self.assertRaises(ValueError):
                attachments.extract_pdf_pages(PDF, **settings)

    def test_real_pdf_in_zip_per_page_citations_when_dependency_available(self):
        deps = attachments.capabilities()
        if not (deps["pypdf"] or deps["pymupdf"]):
            self.skipTest("Optional pypdf/PyMuPDF not installed")
        pdf = real_pdf(["Cover page", "One new share for every two shares held"])
        result = attachments.adapt_attachment(make_zip([("2005/scheme.pdf", pdf)]), source_url=URL)
        pages = result["documents"][0]["pages"]
        self.assertEqual(len(pages), 2)
        self.assertIn("One new share", pages[1]["text"])
        self.assertEqual(pages[1]["page_ref"], "p.2")
        self.assertEqual(pages[1]["archive_member"], "2005/scheme.pdf")


class ManifestAndCliTests(unittest.TestCase):
    def test_existing_url_map_manifest_offline_and_original_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original.zip"
            payload = make_zip([("scheme.pdf", PDF)])
            source.write_bytes(payload)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({URL: {"cache_path": "original.zip", "sha256": attachments.sha256(payload)}}))
            with patch.object(attachments, "extract_pdf_pages", return_value=page_records("Text", "")):
                summary = attachments.scan_manifest(manifest, root=root, out=root / "evidence")
                repeated = attachments.scan_manifest(manifest, root=root, out=root / "evidence")
            self.assertEqual(summary, repeated)
            self.assertEqual(summary["documents"], 1)
            self.assertEqual(summary["pages"], 2)
            self.assertEqual(summary["text_pages"], 1)
            self.assertEqual(summary["verified_terms"], 0)
            self.assertTrue(summary["offline"])
            self.assertEqual(source.read_bytes(), payload)
            artifacts = list((root / "evidence").glob("*.json"))
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(json.loads(artifacts[0].read_text())["documents"][0]["pages"][1]["page_ref"], "p.2")

    def test_jsonl_and_attachments_list_are_bounded_and_keep_filing_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = {"attchmntFile": URL, "cache_path": "old.zip", "announcement_date": "2000-01-01"}
            for name, content in (("list.json", json.dumps({"attachments": [row, row]})),
                                  ("index.jsonl", json.dumps(row) + "\n" + json.dumps(row))):
                path = root / name
                path.write_text(content)
                jobs = attachments.load_manifest(path, root=root, limit=1)
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["source_url"], URL)
                self.assertEqual(jobs[0]["metadata"]["announcement_date"], "2000-01-01")

    def test_missing_or_outside_cache_paths_never_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps([
                {"url": URL}, {"url": URL, "path": "../outside.pdf"},
                {"url": URL, "path": "missing.pdf"},
            ]))
            summary = attachments.scan_manifest(manifest, root=root)
            self.assertEqual(summary["statuses"], {"failed": 3})
            self.assertEqual([r["error_code"] for r in summary["records"]],
                             ["cache_path_missing", "cache_path_outside_root", "FileNotFoundError"])

    def test_no_artifact_overwrite_even_on_digest_name_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = attachments.write_artifact(Path(directory), {"text": "original"})
            self.assertEqual(path, attachments.write_artifact(Path(directory), {"text": "original"}))
            path.write_bytes(b"foreign data")
            with self.assertRaises(attachments.AttachmentError):
                attachments.write_artifact(Path(directory), {"text": "original"})
            self.assertEqual(path.read_bytes(), b"foreign data")

    def test_cli_missing_requested_ocr_exits_nonzero_with_explicit_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.pdf").write_bytes(PDF)
            (root / "manifest.json").write_text(json.dumps([{"url": URL, "path": "a.pdf"}]))
            output = io.StringIO()
            with patch.object(attachments, "capabilities", return_value=NO_DEPS), contextlib.redirect_stdout(output):
                status = attachments.main(["--manifest", str(root / "manifest.json"), "--root", str(root), "--ocr"])
            self.assertEqual(status, 1)
            self.assertEqual(json.loads(output.getvalue())["records"][0]["error_code"], "ocr_unavailable")

    def test_signoff_validator_accepts_valid_and_rejects_bad_rows(self):
        row = {"event_id": "CANON-A-2019-06-01",
               "filing_url": "https://nsearchives.nseindia.com/corporate/a.pdf",
               "page_ref": "p.1", "snippet_verified_on_page": True, "verified_by": "r",
               "ratio_num": 2, "ratio_den": 3, "cash_per_share": None,
               "ratio_direction": attachments.SIGNOFF_RATIO_DIRECTION,
               "transferor_symbol": "A", "transferor_isin": "INE000A01004",
               "transferee_symbol": "B", "transferee_isin": "INE000B01003",
               "successor_symbol": "B", "successor_isin": "INE000B01003",
               "official_ex_date": "2019-06-01", "listing_date": "2019-08-01"}
        self.assertEqual(attachments.validate_signoff_row(row), [])
        for patch, code in (({"filing_url": "https://example.com/a.pdf"}, "filing_url"),
                            ({"ratio_direction": "holder_per_successor"}, "ratio_direction"),
                            ({"successor_isin": "B"}, "successor_isin"),
                            ({"listing_date": "2019-13-01"}, "listing_date"),
                            ({"_pending": True}, "pending_template")):
            bad = {**row, **patch}
            errors = attachments.validate_signoff_row(bad)
            self.assertTrue(any(code in e for e in errors), errors)
        tmpl = attachments.signoff_template("CANON-A-2019-06-01", "A", "2019-06-01")
        self.assertTrue(attachments.validate_signoff_row(tmpl))

    def test_cli_bad_manifest_and_invalid_limits_exit_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text("not json")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(attachments.main(["--manifest", str(path)]), 2)
                self.assertEqual(attachments.main(["--manifest", str(path), "--max-members", "0"]), 2)


if __name__ == "__main__":
    unittest.main()
