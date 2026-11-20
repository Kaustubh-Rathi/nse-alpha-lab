from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nse_pipeline.thirdparty.kaggle_nifty import (
    HISTORICAL_TO_KAGGLE, MEMBERSHIP_KIND, THIRDPARTY_LABEL, canonical_symbol,
    compare_to_official, import_weights_zip, lookup_observation,
    precision_recall, sha256, validate_observation, write_outputs,
)

TEMP_ROOT = Path.home() / "AppData" / "Local" / "Temp" / "opencode"
if not TEMP_ROOT.exists():
    TEMP_ROOT = None


def make_zip(tickers, rows):
    """rows: [(date_str, {ticker: weight})]; missing tickers default 0."""
    lines = ["DATE," + ",".join(tickers)]
    for day, weights in rows:
        lines.append(day + "," + ",".join(str(weights.get(t, 0)) for t in tickers))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("weights.csv", "\n".join(lines) + "\n")
        archive.writestr("LICENSE.txt", "CC BY-NC-SA 4.0\n")
    return output.getvalue()


class KaggleNiftyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_zip(self, tickers, rows, name="kaggle.zip"):
        content = make_zip(tickers, rows)
        path = self.root / name
        path.write_bytes(content)
        provenance = {"sha256": sha256(content),
                      "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        return path, provenance

    def test_import_parses_wide_weights_and_drops_zeros(self):
        path, prov = self.write_zip(
            ["AAA", "BBB", "CCC"],
            [("2024-01-31", {"AAA": 60.0, "BBB": 40.0}),
             ("2024-02-29", {"AAA": 50.0, "BBB": 30.0, "CCC": 20.0})])
        observations = import_weights_zip(path, prov)
        self.assertEqual(len(observations), 2)
        first, second = observations
        self.assertEqual(first.as_of, date(2024, 1, 31))
        self.assertEqual(dict(first.weights), {"AAA": 60.0, "BBB": 40.0})
        self.assertEqual(dict(second.weights), {"AAA": 50.0, "BBB": 30.0, "CCC": 20.0})
        self.assertEqual(first.metadata["source_label"], THIRDPARTY_LABEL)
        self.assertEqual(first.metadata["membership_kind"], MEMBERSHIP_KIND)
        self.assertIs(first.metadata["is_official"], False)

    def test_rejects_weight_sum_not_100(self):
        path, prov = self.write_zip(
            ["AAA", "BBB"], [("2024-01-31", {"AAA": 60.0, "BBB": 30.0})])
        with self.assertRaisesRegex(ValueError, "expected ~100"):
            import_weights_zip(path, prov)

    def test_rejects_negative_and_non_numeric_weights(self):
        content = make_zip(["AAA"], [("2024-01-31", {"AAA": -5.0})])
        path = self.root / "neg.zip"
        path.write_bytes(content)
        prov = {"sha256": sha256(content), "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        with self.assertRaisesRegex(ValueError, "negative weight"):
            import_weights_zip(path, prov)
        text = "DATE,AAA\n2024-01-31,not-a-number\n"
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("weights.csv", text)
        path.write_bytes(output.getvalue())
        prov = {"sha256": sha256(output.getvalue()), "retrieved_at_utc": "2026-11-08T10:00:00+00:00"}
        with self.assertRaisesRegex(ValueError, "non-numeric weight"):
            import_weights_zip(path, prov)

    def test_never_claims_official_membership(self):
        path, prov = self.write_zip(
            ["AAA"], [("2024-01-31", {"AAA": 100.0})])
        observations = import_weights_zip(path, prov)
        validate_observation(observations[0])  # valid as third-party
        meta = dict(observations[0].metadata, membership_kind="dated_official_snapshot")
        from nse_pipeline.thirdparty.kaggle_nifty import KaggleObservation
        with self.assertRaisesRegex(ValueError, "never claim dated_official_snapshot"):
            validate_observation(KaggleObservation(observations[0].as_of,
                                                   observations[0].weights, meta))
        prov = dict(prov, membership_kind="dated_official_snapshot")
        with self.assertRaisesRegex(ValueError, "never claim dated_official_snapshot"):
            import_weights_zip(path, prov)

    def test_rejects_duplicate_dates_unsorted_and_bad_provenance(self):
        path, prov = self.write_zip(
            ["AAA"],
            [("2024-02-29", {"AAA": 100.0}), ("2024-01-31", {"AAA": 100.0})])
        with self.assertRaisesRegex(ValueError, "ascending"):
            import_weights_zip(path, prov)
        path, prov = self.write_zip(
            ["AAA"],
            [("2024-01-31", {"AAA": 100.0}), ("2024-01-31", {"AAA": 100.0})])
        with self.assertRaisesRegex(ValueError, "duplicate DATE"):
            import_weights_zip(path, prov)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            import_weights_zip(path, dict(prov, sha256="0" * 64))

    def test_exact_date_lookup_never_fills_gaps(self):
        path, prov = self.write_zip(
            ["AAA"], [("2024-01-31", {"AAA": 100.0})])
        observations = import_weights_zip(path, prov)
        self.assertEqual(lookup_observation(observations, date(2024, 1, 31)).as_of,
                         date(2024, 1, 31))
        for missing in (date(2024, 1, 30), date(2024, 2, 1), date(2023, 12, 31)):
            with self.assertRaises(LookupError):
                lookup_observation(observations, missing)

    def test_alias_normalisation_applied_only_in_comparison(self):
        self.assertEqual(canonical_symbol("INFOSYSTCH"), "INFY")
        self.assertEqual(canonical_symbol("HEROHONDA"), "HEROMOTOCO")
        self.assertEqual(canonical_symbol("BAJAJ-AUTO"), "BAJAJAUTO")
        self.assertEqual(canonical_symbol("SSLT"), "VEDL")
        self.assertEqual(canonical_symbol("RELIANCE"), "RELIANCE")
        self.assertEqual(HISTORICAL_TO_KAGGLE["INFOSYSTCH"], "INFY")
        # Stored symbols stay raw Kaggle tickers.
        path, prov = self.write_zip(
            ["INFY", "AAA"], [("2024-01-31", {"INFY": 50.0, "AAA": 50.0})])
        observations = import_weights_zip(path, prov)
        self.assertEqual(dict(observations[0].weights), {"INFY": 50.0, "AAA": 50.0})

    def test_precision_recall_math(self):
        scores = precision_recall({"A", "B", "C"}, {"A", "B", "D"})
        self.assertEqual(scores, {"tp": 2, "fp": 1, "fn": 1,
                                  "precision": 0.6667, "recall": 0.6667, "f1": 0.6667})
        perfect = precision_recall({"A"}, {"A"})
        self.assertEqual(perfect["precision"], 1.0)
        self.assertEqual(perfect["recall"], 1.0)

    def test_compare_scores_exact_dates_only(self):
        path, prov = self.write_zip(
            ["INFY", "AAA"], [("2024-01-31", {"INFY": 50.0, "AAA": 50.0})])
        observations = import_weights_zip(path, prov)
        official = {("NIFTY50", "2024-01-31"): {"INFOSYSTCH", "AAA"},
                    ("NIFTY50", "2024-01-30"): {"INFOSYSTCH", "AAA"}}
        result = compare_to_official(observations, official)
        self.assertEqual(len(result["exact_date_overlaps"]), 1)
        overlap = result["exact_date_overlaps"][0]
        self.assertEqual(overlap["raw"]["precision"], 0.5)  # INFY != INFOSYSTCH raw
        self.assertEqual(overlap["alias_normalised"]["precision"], 1.0)
        self.assertEqual(overlap["alias_normalised"]["recall"], 1.0)
        self.assertEqual(overlap["official_only_raw"], ["INFOSYSTCH"])
        self.assertEqual(len(result["non_overlapping_official_dates"]), 1)
        self.assertEqual(result["non_overlapping_official_dates"][0]["official_as_of"],
                         "2024-01-30")
        # Nearest date is noted but explicitly unscored.
        self.assertEqual(result["non_overlapping_official_dates"][0]
                         ["nearest_kaggle_date_unscored"], "2024-01-31")

    def test_write_outputs_label_every_row_thirdparty(self):
        path, prov = self.write_zip(
            ["AAA", "BBB"], [("2024-01-31", {"AAA": 70.0, "BBB": 30.0})])
        observations = import_weights_zip(path, prov)
        obs_path, summary_path = write_outputs(observations, self.root / "out")
        with obs_path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["source_label"] == THIRDPARTY_LABEL for r in rows))
        self.assertTrue(all(r["is_official"] == "false" for r in rows))
        self.assertTrue(all(r["membership_kind"] == MEMBERSHIP_KIND for r in rows))
        self.assertEqual({r["symbol"] for r in rows}, {"AAA", "BBB"})
        self.assertAlmostEqual(sum(float(r["weight_pct"]) for r in rows), 100.0)
        with summary_path.open(newline="", encoding="utf-8") as stream:
            summary = list(csv.DictReader(stream))
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["n_constituents"], "2")
        self.assertAlmostEqual(float(summary[0]["weight_sum"]), 100.0)

    def test_delivered_dataset_structure(self):
        root = Path(__file__).resolve().parents[1]
        zip_path = root / "data/raw/kaggle/historical-nifty-50-constituent-weights-20y.zip"
        if not zip_path.exists():
            self.skipTest("delivered Kaggle ZIP not present")
        provenance_path = root / "data/raw/kaggle/provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        content = zip_path.read_bytes()
        self.assertEqual(provenance["zip_sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(provenance["license"], "CC-BY-NC-SA-4.0")
        self.assertEqual(provenance["source_label"], THIRDPARTY_LABEL)
        self.assertIs(provenance["is_official"], False)
        self.assertNotEqual(provenance.get("kaggle_username"), "")
        observations = import_weights_zip(
            zip_path, {"sha256": provenance["zip_sha256"],
                       "retrieved_at_utc": provenance["downloaded_at_utc"]})
        self.assertTrue(len(observations) >= 100)
        self.assertEqual(observations[0].as_of, date(2008, 1, 31))
        tickers = {symbol for obs in observations for symbol, _ in obs.weights}
        self.assertEqual(len(tickers), 100)
        for obs in observations:
            self.assertEqual(len(obs.weights), 50)
            self.assertAlmostEqual(sum(w for _, w in obs.weights), 100.0, places=3)

    def test_delivered_dataset_matches_official_after_normalisation(self):
        from nse_pipeline.thirdparty.kaggle_nifty import load_official_symbols
        root = Path(__file__).resolve().parents[1]
        zip_path = root / "data/raw/kaggle/historical-nifty-50-constituent-weights-20y.zip"
        if not zip_path.exists():
            self.skipTest("delivered Kaggle ZIP not present")
        provenance = json.loads((root / "data/raw/kaggle/provenance.json")
                                .read_text(encoding="utf-8"))
        observations = import_weights_zip(
            zip_path, {"sha256": provenance["zip_sha256"],
                       "retrieved_at_utc": provenance["downloaded_at_utc"]})
        official = load_official_symbols(root / "data/processed/index_membership")
        comparison = compare_to_official(observations, official)
        # Kaggle ends 2025-08-31 and uses calendar dates, so only a subset
        # of the 19 official Nifty 50 dates overlap exactly.
        self.assertGreater(len(comparison["exact_date_overlaps"]), 0)
        self.assertGreater(len(comparison["non_overlapping_official_dates"]), 0)
        norm = comparison["aggregate_alias_normalised"]
        self.assertEqual(norm["micro_precision"], 1.0)
        self.assertEqual(norm["micro_recall"], 1.0)
        raw = comparison["aggregate_raw"]
        # Raw misses (if any) must be exclusively documented aliases.
        raw_only = set()
        for row in comparison["exact_date_overlaps"]:
            raw_only.update(row["kaggle_only_raw"])
        self.assertTrue(set(raw_only) <= set(HISTORICAL_TO_KAGGLE.values()))


if __name__ == "__main__":
    unittest.main()
