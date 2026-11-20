from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.nifty.snapshots import (
    Member, import_mcwb, import_pdf_pages, join_pdf_pages, load_snapshot,
    members_on, sha256, validate, write_snapshot,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "report.zip"

    def snapshot(self, count=50, evidence="*Last day of trading was 30-Oct-2026"):
        report = ("Annexure II- Nifty 50 Index : October 2026\n\n"
                  "Sr. No,Security Symbol,Security Name\n"
                  + "".join(f"{i},STOCK{i},Company {i}\n" for i in range(1, count + 1))
                  + evidence + "\n")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("nifty50_mcwb.csv", report)
        self.source.write_bytes(output.getvalue())
        provenance = {"sha256": sha256(output.getvalue()),
                      "url": "https://www.niftyindices.com/monthly/report.zip",
                      "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        return import_mcwb(self.source, "nifty50_mcwb.csv", provenance)

    def test_round_trip_and_digest_validation(self):
        snapshot = self.snapshot()
        csv_path, manifest = write_snapshot(snapshot, self.root / "processed")
        loaded = load_snapshot(csv_path, manifest, self.source)
        self.assertEqual(loaded.members, snapshot.members)
        self.assertEqual(loaded.as_of, date(2026, 10, 30))
        self.assertTrue(all(not m.isin for m in loaded.members))

    def test_missing_date_never_uses_retrieval_or_filename_date(self):
        with self.assertRaisesRegex(ValueError, "explicit last trading date"):
            self.snapshot(evidence="Report downloaded 2026-11-08")

    def test_51_securities_are_not_silently_truncated(self):
        snapshot = self.snapshot(count=51)
        self.assertEqual(len(snapshot.members), 51)

    def test_report_date_can_be_a_special_sunday_session(self):
        snapshot = self.snapshot(evidence="* Last day of trading was 30-October-2016")
        self.assertEqual(snapshot.as_of, date(2016, 10, 30))

    def test_official_title_and_date_variants(self):
        variants = (
            ("S&P CNX NIFTY INDEX", "Last day of trading was 31 October-2008",
             "Sr. No,Security Symbol,Industry\n", date(2008, 10, 31)),
            ("Annexure II-  CNX Nifty Index : October 2013",
             "Last day of trading was 31-Oct-2013",
             "Sr. No,Security Symbol,Security Name\n", date(2013, 10, 31)),
            ("Annexure II-  Nifty 50 Index : October 2017",
             "Last day of trading was 31-Oct 2017",
             "Sr. No,Security Symbol,Security Name\n", date(2017, 10, 31)),
        )
        for title, evidence, header, expected in variants:
            with self.subTest(title=title):
                report = (title + "\n\n" + header
                          + "".join(f"{i},STOCK{i},Company {i}\n"
                                    for i in range(1, 51))
                          + evidence + "\n")
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    archive.writestr("report.csv", report)
                self.source.write_bytes(output.getvalue())
                provenance = {"sha256": sha256(output.getvalue()),
                              "url": "https://www.niftyindices.com/monthly/report.zip",
                              "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
                snapshot = import_mcwb(self.source, "report.csv", provenance)
                self.assertEqual(snapshot.as_of, expected)
                self.assertEqual(len(snapshot.members), 50)

    def test_mcwb_header_without_company_names(self):
        report = ("S&P CNX NIFTY INDEX\n\nSr. No,Security Symbol,Industry\n"
                  + "".join(f"{i},STOCK{i},INDUSTRY\n" for i in range(1, 51))
                  + "Last day of trading was 31 October-2008\n")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("report.csv", report)
        self.source.write_bytes(output.getvalue())
        provenance = {"sha256": sha256(output.getvalue()),
                      "url": "https://www.niftyindices.com/monthly/report.zip",
                      "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        snapshot = import_mcwb(self.source, "report.csv", provenance)
        self.assertTrue(all(not member.company_name for member in snapshot.members))

    def pdf_pages(self, count=5, date_line="October 31, 2017"):
        page = ("Symbol Security Name Industry Close Price Index Mcap (Rs. Crores) Weightage (%)\n"
                " \nConstituents of Nifty 500\n" + date_line + "\n"
                + "".join(f"STOCK{i} Company {i} INDUSTRY 100.00 0.1{i}\n"
                          for i in range(1, count + 1)))
        return [page]

    def test_pdf_pages_round_trip_with_retained_text(self):
        pages = self.pdf_pages()
        pdf_bytes = b"official pdf member"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("NIFTY_500_Oct2017.pdf", pdf_bytes)
        self.source.write_bytes(output.getvalue())
        provenance = {"sha256": sha256(output.getvalue()),
                      "url": "https://www.niftyindices.com/indices.zip",
                      "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        text_name = "NIFTY_500_Oct2017.pdf.txt"
        (self.root / text_name).write_bytes(join_pdf_pages(pages).encode("utf-8"))
        snapshot = import_pdf_pages(pages, "NIFTY500", pdf_bytes, provenance,
                                    "NIFTY_500_Oct2017.pdf", text_name)
        self.assertEqual(snapshot.as_of, date(2017, 10, 31))
        self.assertEqual(len(snapshot.members), 5)
        csv_path, manifest = write_snapshot(snapshot, self.root / "processed")
        loaded = load_snapshot(csv_path, manifest, self.source)
        self.assertEqual(loaded.members, snapshot.members)
        (self.root / text_name).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "source text digest"):
            load_snapshot(csv_path, manifest, self.source)

    def test_pdf_requires_one_date_and_a_regular_table(self):
        pdf_bytes = b"official pdf member"
        provenance = {"sha256": "0" * 64,
                      "url": "https://www.niftyindices.com/indices.zip",
                      "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        with self.assertRaisesRegex(ValueError, "document-stated date"):
            import_pdf_pages(self.pdf_pages(date_line="Report generated 2017"), "NIFTY500",
                             pdf_bytes, provenance, "NIFTY_500_Oct2017.pdf")
        first, second = self.pdf_pages(count=4), self.pdf_pages(count=2)
        with self.assertRaisesRegex(ValueError, "irregular"):
            import_pdf_pages([second[0], first[0]], "NIFTY500", pdf_bytes, provenance,
                             "NIFTY_500_Oct2017.pdf")

    def test_after_cutoff_rejected(self):
        with self.assertRaisesRegex(ValueError, "after cutoff"):
            self.snapshot(evidence="*Last day of trading was 30-Nov-2026")

    def test_duplicate_symbols_and_count_mismatch(self):
        snapshot = self.snapshot()
        with self.assertRaisesRegex(ValueError, "duplicate symbol"):
            validate(replace(snapshot, members=(snapshot.members[0],) * 50))
        with self.assertRaisesRegex(ValueError, "security count"):
            validate(replace(snapshot, members=snapshot.members[:-1]))

    def test_nonofficial_and_approximate_sources_rejected(self):
        snapshot = self.snapshot()
        for changes in ({"source_url": "https://niftyindices.com.attacker.example/report"},
                        {"membership_kind": "rule_based_approximation"},
                        {"as_of_basis": "retrieval_date"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate(replace(snapshot, metadata=dict(snapshot.metadata, **changes)))

    def test_no_forward_or_backward_fill(self):
        snapshot = self.snapshot()
        self.assertEqual(len(members_on([snapshot], "NIFTY50", snapshot.as_of)), 50)
        for day in (date(2000, 1, 3), date(2026, 10, 29), date(2026, 11, 6)):
            with self.assertRaises(LookupError):
                members_on([snapshot], "NIFTY50", day)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            members_on([snapshot, snapshot], "NIFTY50", snapshot.as_of)

    def test_source_and_snapshot_tampering(self):
        snapshot = self.snapshot()
        csv_path, manifest = write_snapshot(snapshot, self.root / "processed")
        original = csv_path.read_bytes()
        csv_path.write_bytes(original + b"OTHER,Other,\n")
        with self.assertRaisesRegex(ValueError, "snapshot digest"):
            load_snapshot(csv_path, manifest, self.source)
        csv_path.write_bytes(original)
        self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "source digest"):
            load_snapshot(csv_path, manifest, self.source)

    def test_wrong_zip_member_digest_rejected(self):
        snapshot = self.snapshot()
        csv_path, manifest = write_snapshot(snapshot, self.root / "processed")
        metadata = json.loads(manifest.read_text())
        metadata["source_member_sha256"] = "0" * 64
        manifest.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "source member digest"):
            load_snapshot(csv_path, manifest, self.source)

    def test_no_overwrite(self):
        snapshot = self.snapshot()
        write_snapshot(snapshot, self.root / "processed")
        with self.assertRaises(FileExistsError):
            write_snapshot(snapshot, self.root / "processed")

    def test_invalid_isin_rejected(self):
        snapshot = self.snapshot()
        members = (Member("STOCK1", "Company 1", "not-an-isin"),) + snapshot.members[1:]
        with self.assertRaisesRegex(ValueError, "invalid ISIN"):
            validate(replace(snapshot, members=members))

    def test_downloaded_snapshots_have_verified_original_sources(self):
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root / "data/processed/index_membership/catalog.json").read_text())
        snapshots = [load_snapshot(root / row["snapshot_csv"], root / row["snapshot_manifest"],
                                   root / row["source_path"]) for row in catalog]
        self.assertEqual(len(snapshots), 26)
        self.assertEqual(sum(len(snapshot.members) for snapshot in snapshots), 4458)
        self.assertEqual(sum(snapshot.index_code == "NIFTY500" for snapshot in snapshots), 7)
        self.assertEqual(sum(snapshot.index_code == "NIFTY50" for snapshot in snapshots), 19)
        self.assertEqual(
            sorted(snapshot.as_of.isoformat() for snapshot in snapshots
                   if snapshot.index_code == "NIFTY50"),
            ["2008-10-31", "2009-10-30", "2010-10-29", "2011-10-31", "2012-10-31",
             "2013-10-31", "2014-10-31", "2015-10-30", "2016-10-30", "2017-10-31",
             "2018-10-31", "2019-10-31", "2020-10-30", "2021-10-29", "2022-10-31",
             "2023-10-31", "2024-10-31", "2025-10-31", "2026-10-30"])
        self.assertEqual(
            sorted(snapshot.as_of.isoformat() for snapshot in snapshots
                   if snapshot.index_code == "NIFTY500"),
            ["2015-10-30", "2016-10-30", "2017-10-31", "2018-10-31",
             "2019-10-31", "2020-10-30", "2021-10-29"])
        self.assertTrue(all(not member.isin for snapshot in snapshots for member in snapshot.members))
        self.assertTrue(any(member.symbol == "DUMMYTATAM"
                            for snapshot in snapshots for member in snapshot.members))


if __name__ == "__main__":
    unittest.main()
