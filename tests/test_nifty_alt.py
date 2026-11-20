from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.nifty.alt import (  # noqa: E402
    AltMember, AltSnapshot, classify_source, diff_symbols, import_third_party_tickers,
    import_wayback_csv, import_wikipedia_changes, import_wikipedia_current,
    ledger_to_snapshot, load_alt_snapshot, members_on_alt,
    sha256, write_alt_snapshot,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


def wayback_csv(symbols):
    lines = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    for i, s in enumerate(symbols):
        lines.append(f"Company {s},IT,{s},EQ,INE{i:08d}0")
    return ("\n".join(lines) + "\n").encode()


def wayback_provenance(content, capture="20210903113122"):
    return {"sha256": sha256(content), "capture_timestamp": capture,
            "original_url": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
            "replay_url": f"https://web.archive.org/web/{capture}id_/https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
            "retrieved_at_utc": "2026-11-08T12:00:00Z"}


class NiftyAltTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def wayback_snapshot(self, symbols=("AAA", "BBB", "CCC")):
        content = wayback_csv(symbols)
        return import_wayback_csv(content, "NIFTY50", wayback_provenance(content))

    def test_wayback_csv_round_trip_preserves_isin(self):
        snapshot = self.wayback_snapshot()
        self.assertEqual(snapshot.as_of, date(2021, 9, 3))
        self.assertEqual(snapshot.metadata["membership_kind"], "wayback_capture_observed")
        self.assertEqual(snapshot.metadata["as_of_basis"],
                         "wayback_capture_timestamp_not_document_date")
        csv_path, manifest = write_alt_snapshot(snapshot, self.root / "processed")
        source = self.root / "source.csv"
        source.write_bytes(wayback_csv(("AAA", "BBB", "CCC")))
        loaded = load_alt_snapshot(csv_path, manifest, source)
        self.assertEqual(loaded.members, snapshot.members)
        self.assertEqual(loaded.members[0].isin, "INE000000000")

    def test_wayback_rejects_wrong_host_and_missing_capture(self):
        content = wayback_csv(("AAA",))
        prov = wayback_provenance(content)
        prov["original_url"] = "https://example.com/ind_nifty50list.csv"
        with self.assertRaisesRegex(ValueError, "official HTTPS"):
            import_wayback_csv(content, "NIFTY50", prov)
        prov = wayback_provenance(content)
        prov["capture_timestamp"] = "2021-09-03"
        with self.assertRaisesRegex(ValueError, "capture_timestamp"):
            import_wayback_csv(content, "NIFTY50", prov)
        prov = wayback_provenance(content)
        prov["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            import_wayback_csv(content, "NIFTY50", prov)

    def test_wayback_exact_date_only_no_gap_fill(self):
        snapshot = self.wayback_snapshot()
        self.assertEqual(len(members_on_alt([snapshot], "NIFTY50", date(2021, 9, 3))), 3)
        for day in (date(2021, 9, 2), date(2021, 10, 29), date(2000, 1, 3)):
            with self.assertRaises(LookupError):
                members_on_alt([snapshot], "NIFTY50", day)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            members_on_alt([snapshot, snapshot], "NIFTY50", date(2021, 9, 3))

    def test_wayback_rejects_duplicates_bad_isin_and_count(self):
        dup = AltSnapshot("NIFTY50", date(2021, 9, 3),
                          (AltMember("AAA"), AltMember("AAA")),
                          dict(self.wayback_snapshot().metadata,
                               expected_security_count=2))
        with self.assertRaisesRegex(ValueError, "duplicate symbol"):
            write_alt_snapshot(dup, self.root / "p1")
        bad = AltSnapshot("NIFTY50", date(2021, 9, 3),
                          (AltMember("AAA", isin="nope"),),
                          dict(self.wayback_snapshot().metadata,
                               expected_security_count=1))
        with self.assertRaisesRegex(ValueError, "invalid ISIN"):
            write_alt_snapshot(bad, self.root / "p2")

    def test_alt_cannot_masquerade_as_official_snapshot(self):
        snapshot = self.wayback_snapshot()
        meta = dict(snapshot.metadata, membership_kind="dated_official_snapshot")
        fake = AltSnapshot("NIFTY50", snapshot.as_of, snapshot.members, meta)
        with self.assertRaisesRegex(ValueError, "must not claim dated_official_snapshot"):
            write_alt_snapshot(fake, self.root / "p3")

    def test_wikipedia_current_list_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no list as-of date"):
            import_wikipedia_current([{"symbol": "AAA"}], {})

    def test_wikipedia_change_ledger_validation(self):
        prov = {"page_url": "https://en.wikipedia.org/wiki/NIFTY_50",
                "retrieved_at_utc": "2026-11-08T12:00:00Z"}
        rows = [{"excluded": "Old Co", "included": "New Co", "date": "2010-04-08"}]
        ledger = import_wikipedia_changes(rows, prov)
        self.assertEqual(ledger[0].effective_date, date(2010, 4, 8))
        with self.assertRaisesRegex(ValueError, "2005 scope"):
            import_wikipedia_changes(
                [{"excluded": "A", "included": "B", "date": "2004-01-01"}], prov)
        with self.assertRaisesRegex(ValueError, "ISO effective date"):
            import_wikipedia_changes(
                [{"excluded": "A", "included": "B", "date": "8 April 2010"}], prov)

    def test_ledger_without_baseline_cannot_make_snapshot(self):
        prov = {"page_url": "https://en.wikipedia.org/wiki/NIFTY_50",
                "retrieved_at_utc": "2026-11-08T12:00:00Z"}
        ledger = import_wikipedia_changes(
            [{"excluded": "Old Co", "included": "New Co", "date": "2010-04-08"}], prov)
        with self.assertRaisesRegex(ValueError, "no verified baseline"):
            ledger_to_snapshot(ledger, "NIFTY50", None)

    def test_third_party_tickers_preserve_raw_suffix(self):
        content = b'TICKERS,Company\nAAA.NS,Company A\nBBB.BO,Company B\n'
        rows = import_third_party_tickers(content, {"sha256": sha256(content)})
        self.assertEqual(rows[0], {"raw_ticker": "AAA.NS", "symbol": "AAA",
                                   "company": "Company A"})
        self.assertEqual(rows[1]["raw_ticker"], "BBB.BO")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            import_third_party_tickers(content, {"sha256": "0" * 64})

    def test_classify_source_trust_tiers(self):
        self.assertEqual(classify_source(
            "https://web.archive.org/web/x/https://archives.nseindia.com/x")["trust"],
            "official_archive")
        self.assertEqual(classify_source(
            "https://en.wikipedia.org/wiki/NIFTY_50")["trust"], "secondary_editable")
        self.assertEqual(classify_source(
            "https://github.com/NV2017/NIFTY_BSE_500_Combined")["trust"], "third_party")
        self.assertEqual(classify_source(
            "https://www.bseindia.com/markets/x")["trust"], "exchange_current_only")
        self.assertEqual(classify_source(
            "https://www.connect2nse.com/iislNet/")["trust"], "rejected")

    def test_diff_symbols(self):
        before = self.wayback_snapshot(("AAA", "BBB", "CCC"))
        content = wayback_csv(("AAA", "DDD", "CCC"))
        after = import_wayback_csv(content, "NIFTY50", wayback_provenance(content))
        diff = diff_symbols(before, after)
        self.assertEqual(diff["added"], ["DDD"])
        self.assertEqual(diff["removed"], ["BBB"])
        self.assertEqual(diff["unchanged"], ["AAA", "CCC"])

    def test_tamper_detection_on_load(self):
        snapshot = self.wayback_snapshot()
        csv_path, manifest = write_alt_snapshot(snapshot, self.root / "processed")
        source = self.root / "source.csv"
        source.write_bytes(wayback_csv(("AAA", "BBB", "CCC")))
        original = csv_path.read_bytes()
        csv_path.write_bytes(original + b"DDD,Company D,,EQ\n")
        with self.assertRaisesRegex(ValueError, "snapshot digest"):
            load_alt_snapshot(csv_path, manifest, source)
        csv_path.write_bytes(original)
        source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "source digest"):
            load_alt_snapshot(csv_path, manifest, source)

    def test_delivered_samples_have_verified_provenance(self):
        root = Path(__file__).resolve().parents[1]
        raw, proc = root / "data/raw/nifty_alt", root / "data/processed/nifty_alt"
        s50 = load_alt_snapshot(proc / "NIFTY50_2021-09-03.csv",
                                proc / "NIFTY50_2021-09-03.json",
                                raw / "ind_nifty50list_20210903.csv")
        self.assertEqual(len(s50.members), 50)
        self.assertTrue(all(m.isin for m in s50.members))
        self.assertEqual(s50.metadata["capture_timestamp"], "20210903113122")
        s500 = load_alt_snapshot(proc / "NIFTY500_2022-05-04.csv",
                                 proc / "NIFTY500_2022-05-04.json",
                                 raw / "ind_nifty500list_20220504.csv")
        self.assertEqual(len(s500.members), 501)
        self.assertEqual(sum(m.series == "BE" for m in s500.members), 2)
        ledger = json.loads((raw / "wikipedia_nifty50_changes_rev1379683379.json").read_text())
        self.assertEqual(ledger["revision"], 1379683379)
        self.assertEqual(len(ledger["rows"]), 47)
        self.assertEqual(ledger["rows"][0]["effective_date"], "2005-02-25")
        tickers = json.loads((proc / "thirdparty_NV2017_2019-03-19.json").read_text())
        self.assertEqual(len(tickers["rows"]), 550)
        with self.assertRaisesRegex(ValueError, "no verified baseline"):
            ledger_to_snapshot((), "NIFTY50", None)


if __name__ == "__main__":
    unittest.main()
