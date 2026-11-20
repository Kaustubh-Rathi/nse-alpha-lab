"""Offline tests for bounded optional fetching/discovery in acquire_attachments.

All network behavior uses injected fake fetchers; no real HTTP is attempted.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scheme_acquire_attachments",
    ROOT / "src/nse_pipeline/schemes/acquire_attachments.py",
)
acquire = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acquire)

PDF = b"%PDF-1.4\nfake scheme pdf"
API_URL = (
    "https://www.nseindia.com/api/corporate-announcements?"
    "index=equities&symbol=RELIANCE&from_date=01-01-2020&to_date=30-06-2020"
)
ATT_URL = "https://nsearchives.nseindia.com/corporate/scheme.pdf"
ZIP_URL = "https://archives.nseindia.com/corporate/scheme.zip"


def make_zip(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return stream.getvalue()


def fake_fetcher(responses):
    """responses: dict url -> Response or list of fallback Response."""
    calls = []

    def fetch(url, timeout, max_bytes):
        calls.append(url)
        value = responses[url]
        if isinstance(value, list):
            return value.pop(0)
        return value

    fetch.calls = calls
    return fetch


class ValidateUrlTests(unittest.TestCase):
    def test_allowlisted_api_and_attachments(self):
        acquire.validate_url(API_URL, "api")
        acquire.validate_url(ATT_URL)
        acquire.validate_url(ZIP_URL)
        acquire.validate_url(ATT_URL + "?download=1")

    def test_rejects_non_allowlisted(self):
        for url in (
            "http://nsearchives.nseindia.com/x.pdf",
            "https://example.com/x.pdf",
            "https://nsearchives.nseindia.com/x.txt",
            "https://nsearchives.nseindia.com/x.pdf#frag",
            "https://nsearchives.nseindia.com:8443/x.pdf",
            "",
        ):
            with self.subTest(url=url):
                with self.assertRaises(acquire.AcquisitionError):
                    acquire.validate_url(url)

    def test_api_kind_rejects_attachment_host(self):
        with self.assertRaises(acquire.AcquisitionError):
            acquire.validate_url(ATT_URL, "api")


class NetworkBatchTests(unittest.TestCase):
    def test_offline_never_calls_fetcher(self):
        fetcher = fake_fetcher({ATT_URL: acquire.Response(200, PDF)})
        batch = acquire.NetworkBatch(fetch=False, fetcher=fetcher)
        with self.assertRaises(acquire.BatchStopped) as raised:
            batch.request(ATT_URL)
        self.assertEqual(raised.exception.code, "offline")
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(batch.requests, 0)

    def test_denial_stops_batch(self):
        fetcher = fake_fetcher({ATT_URL: acquire.Response(403)})
        batch = acquire.NetworkBatch(fetch=True, fetcher=fetcher, min_interval=1)
        with self.assertRaises(acquire.BatchStopped) as raised:
            batch.request(ATT_URL)
        self.assertEqual(raised.exception.code, "http_denied_or_rate_limited")
        self.assertEqual(batch.requests, 1)

    def test_redirect_chain_is_budgeted_per_hop(self):
        fetcher = fake_fetcher({
            ATT_URL: acquire.Response(302, location=ZIP_URL),
            ZIP_URL: acquire.Response(200, PDF),
        })
        batch = acquire.NetworkBatch(fetch=True, fetcher=fetcher, max_requests=2)
        body = batch.request(ATT_URL)
        self.assertEqual(body, PDF)
        self.assertEqual(batch.requests, 2)
        self.assertEqual(fetcher.calls, [ATT_URL, ZIP_URL])

    def test_redirect_to_non_allowlisted_host_stops_without_extra_hop(self):
        fetcher = fake_fetcher({
            ATT_URL: acquire.Response(302, location="https://example.com/x.pdf"),
        })
        batch = acquire.NetworkBatch(fetch=True, fetcher=fetcher)
        with self.assertRaises(acquire.BatchStopped):
            batch.request(ATT_URL)
        self.assertEqual(fetcher.calls, [ATT_URL])

    def test_request_budget_exhausted(self):
        fetcher = fake_fetcher({
            ATT_URL: acquire.Response(200, PDF),
            ZIP_URL: acquire.Response(200, PDF),
        })
        batch = acquire.NetworkBatch(fetch=True, fetcher=fetcher, max_requests=1)
        batch.request(ATT_URL)
        with self.assertRaises(acquire.BatchStopped) as raised:
            batch.request(ZIP_URL)
        self.assertEqual(raised.exception.code, "request_budget_exhausted")


class CandidateDiscoveryTests(unittest.TestCase):
    def test_candidates_keep_only_scheme_rows(self):
        rows = [
            {"symbol": "RELIANCE", "desc": "Scheme of arrangement",
             "attchmntFile": ATT_URL, "an_dt": "15-Jan-2020 10:00:00"},
            {"symbol": "RELIANCE", "desc": "Dividend declaration",
             "attchmntFile": "https://nsearchives.nseindia.com/corporate/dividend.pdf"},
        ]
        candidates, rejected = acquire.candidates_from_rows(rows, "RELIANCE")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_url"], ATT_URL)
        self.assertEqual(candidates[0]["announcement_date"], "2020-01-15")
        self.assertEqual(rejected, [])

    def test_offline_discovery_returns_plan_only(self):
        result = acquire.discover_scheme_filings(
            ["RELIANCE"], [2020], network=acquire.NetworkBatch(fetch=False))
        self.assertEqual(len(result["windows_planned"]), 2)
        self.assertEqual(result["windows_completed"], 0)
        self.assertEqual(result["candidates"], [])

    def test_bounded_discovery_with_fake_fetcher(self):
        payload = json.dumps([
            {"symbol": "RELIANCE", "desc": "Merger scheme",
             "attchmntFile": ATT_URL, "an_dt": "15-Jan-2020 10:00:00"},
        ]).encode()
        windows = [w["url"] for w in acquire._windows(["RELIANCE"], [2020], 3)]
        fetcher = fake_fetcher({url: acquire.Response(200, payload) for url in windows})
        batch = acquire.NetworkBatch(fetch=True, fetcher=fetcher, max_requests=10)
        result = acquire.discover_scheme_filings(
            ["RELIANCE"], [2020], network=batch, max_attachments=10)
        self.assertEqual(result["windows_completed"], 2)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["source_url"], ATT_URL)


class SignoffTemplateTests(unittest.TestCase):
    def test_templates_are_pending_and_reject_until_filled(self):
        rows = [{"attchmntFile": ATT_URL, "desc": "Scheme of arrangement"}]
        templates = acquire.manual_signoff_templates(rows, "CANON-RELIANCE-2020-01-15", "RELIANCE", "2020-01-15")
        self.assertEqual(len(templates), 1)
        self.assertTrue(templates[0]["_pending"])
        self.assertEqual(templates[0]["official_ex_date"], "2020-01-15")
        self.assertEqual(templates[0]["ratio_direction"], acquire.SIGNOFF_RATIO_DIRECTION)
        # non-official URLs never enter the review queue
        self.assertEqual(acquire.manual_signoff_templates(
            [{"attchmntFile": "https://example.com/x.pdf"}], "E", "S", "2020-01-15"), [])

class RunAndCliTests(unittest.TestCase):
    def test_offline_run_pending_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            announcements = root / "ann.json"
            announcements.write_text(json.dumps([
                {"symbol": "RELIANCE", "desc": "Scheme of amalgamation",
                 "attchmntFile": ATT_URL, "an_dt": "15-Jan-2020 10:00:00"},
            ]))
            result = acquire.run(root=root, announcements=announcements)
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["cached_count"], 0)
            self.assertEqual(result["fetch_enabled"], False)
            self.assertEqual(result["candidates"][0]["acquisition_status"], "pending_offline")
            self.assertEqual(result["requests"], 0)

    def test_bounded_fetch_publishes_hash_named_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sources.json"
            sources.write_text(json.dumps([{"source_url": ATT_URL}]))
            fetcher = fake_fetcher({ATT_URL: acquire.Response(200, PDF)})
            result = acquire.run(root=root, sources=sources, out=root / "out",
                                 fetch=True, fetcher=fetcher, max_requests=2)
            self.assertEqual(result["requests"], 1)
            self.assertEqual(result["cached_count"], 1)
            self.assertEqual(result["candidates"][0]["acquisition_status"], "downloaded")
            self.assertIn("candidate_manifest", result)
            self.assertIn("report", result)

    def test_fetch_without_out_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                acquire.run(root=Path(directory), fetch=True)

    def test_cli_offline_exits_zero(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(output):
                status = acquire.main(["--root", directory])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["fetch_enabled"], False)

    def test_cli_fetch_without_out_exits_two(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with contextlib.redirect_stdout(output):
                status = acquire.main(["--root", directory, "--fetch"])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
