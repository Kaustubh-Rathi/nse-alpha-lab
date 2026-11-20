"""Legacy reports must point at the authoritative evidence, never restate it.

``data/reports/readiness.json`` and ``data/reports/pre2011-actions-coverage.json``
are legacy surfaces. They must not carry a stale verdict or a stale numeric
claim: they point at ``data/reports/full-history-readiness.json`` and at the
normalized/applied layer, and every number they do show is derived from the
current artifacts at write time.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
REPORTS = ROOT / "data" / "reports"
sys.path.insert(0, str(SRC))

from nse_pipeline.build import catalog  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "legacy_older_actions", SRC / "nse_pipeline/actions/older.py")
older = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(older)

AUTHORITATIVE = "data/reports/full-history-readiness.json"
VERIFIED_ACTIONS = "data/reports/verified-actions.json"
ADJUSTMENT_COVERAGE = "data/reports/adjustment-coverage.json"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def false_prices_adjusted_claims(node, trail: str = "") -> list[str]:
    """Every place that still claims a layer is unadjusted with a false flag."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{trail}.{key}" if trail else key
            if key == "prices_adjusted" and value is False:
                found.append(where)
            found.extend(false_prices_adjusted_claims(value, where))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(false_prices_adjusted_claims(item, f"{trail}[{index}]"))
    return found


def synthetic_reports(root: Path, applied: int = 342, ready: int = 343) -> Path:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "verified-actions.json").write_text(json.dumps({
        "normalized_action_rows": 6189,
        "verified_terms_rows": 6056,
        "execution_ready_actions": ready,
        "actually_applied_actions": None,
        "application_status": "not_run; readiness is not application",
    }), encoding="utf-8")
    (reports / "adjustment-coverage.json").write_text(json.dumps({
        "pre2011": {"applied": applied, "verified_multipliers": 364,
                    "split_or_bonus_tagged_rows": 359},
        "verified_actions": {"application_status": "adjustment_export_completed",
                             "actually_applied_actions": applied},
    }), encoding="utf-8")
    (reports / "full-history-readiness.json").write_text(json.dumps({
        "generated_at": "2026-11-09T12:23:58.213884+00:00",
        "requested_start": "2000-01-03",
        "requested_end": "2026-11-06",
        "ready_for_backtesting": False,
        "blockers": [{"code": "corporate_actions", "reason": "Coverage is incomplete.",
                      "next_action": "Acquire cited actions."}],
    }), encoding="utf-8")
    return reports


class LegacyReportPointerTests(unittest.TestCase):
    """The writers must derive their numbers from the current artifacts."""

    def test_legacy_readiness_points_at_the_authoritative_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = synthetic_reports(Path(tmp))
            payload = catalog.legacy_readiness(reports, None, None)
            self.assertFalse(payload["ready_for_backtesting"])
            self.assertEqual(payload["superseded_by"], AUTHORITATIVE)
            self.assertEqual(payload["authoritative_report"], AUTHORITATIVE)
            self.assertEqual(payload["status"], "superseded_pointer")
            self.assertFalse(payload["authoritative_ready_for_backtesting"])
            self.assertEqual(payload["blocking_work"][0]["code"], "corporate_actions")
            self.assertEqual(payload["blocker_codes"], ["corporate_actions"])

    def test_legacy_readiness_numbers_are_derived_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = synthetic_reports(Path(tmp))
            pre2011 = catalog.legacy_readiness(reports, None, None)
            block = pre2011["verified_coverage"]["pre2011_actions"]
            self.assertEqual(block["execution_ready_actions"], 343)
            self.assertEqual(block["actually_applied_actions"], 342)
            self.assertEqual(block["terms_verified_rows"], 6056)
            self.assertEqual(block["normalized_action_rows"], 6189)
            self.assertEqual(block["factor_application"]["applied_pre2011_actions"], 342)
            self.assertEqual(block["factor_application"]["application_status"],
                             "adjustment_export_completed")
            # A different current artifact must move the reported numbers.
            synthetic_reports(Path(tmp), applied=7, ready=9)
            moved = catalog.legacy_readiness(reports, None, None)
            moved_block = moved["verified_coverage"]["pre2011_actions"]
            self.assertEqual(moved_block["execution_ready_actions"], 9)
            self.assertEqual(moved_block["actually_applied_actions"], 7)
            self.assertEqual(moved_block["factor_application"]["applied_pre2011_actions"], 7)

    def test_legacy_readiness_keeps_the_raw_inventory_labelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = synthetic_reports(Path(tmp))
            pre2011 = read_json(reports / "pre2011-actions-coverage.json") \
                if (reports / "pre2011-actions-coverage.json").exists() else {
                    "official_raw_rows": 9099, "official_dated_rows": 9099,
                    "earliest": "2000-01-03", "latest": "2010-12-31", "complete": False}
            block = catalog.legacy_readiness(reports, None, pre2011)["verified_coverage"][
                "pre2011_actions"]
            self.assertEqual(block["raw_inventory_rows"], 9099)
            self.assertEqual(block["raw_inventory_label"], "tags_only_nse_api_inventory")
            self.assertFalse(block["raw_inventory_complete"])
            self.assertEqual(block["report"], "data/reports/pre2011-actions-coverage.json")
            self.assertEqual(block["normalized_report"], VERIFIED_ACTIONS)
            self.assertEqual(block["adjustment_report"], ADJUSTMENT_COVERAGE)

    def test_legacy_readiness_never_claims_the_applied_layer_is_unadjusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = synthetic_reports(Path(tmp))
            payload = catalog.legacy_readiness(reports, None, {"official_raw_rows": 9099})
            self.assertEqual(false_prices_adjusted_claims(payload), [])
            self.assertNotIn("prices_adjusted", payload["verified_coverage"]["pre2011_actions"])

    def test_missing_authoritative_report_is_stated_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "reports"
            reports.mkdir()
            payload = catalog.legacy_readiness(reports, None, None)
            self.assertFalse(payload["ready_for_backtesting"])
            self.assertEqual(payload["blocking_work"][0]["code"],
                             "authoritative_readiness_report_missing")
            self.assertIsNone(payload["authoritative_ready_for_backtesting"])
            block = payload["verified_coverage"]["pre2011_actions"]
            self.assertIsNone(block["execution_ready_actions"])
            self.assertIsNone(block["actually_applied_actions"])

    def test_pre2011_coverage_points_at_the_normalized_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = synthetic_reports(root)
            raw = root / "raw"
            raw.mkdir()
            rows = [{"symbol": "AAA", "subject": "Bonus 1:1", "exDate": "04-Jan-2000",
                     "isin": "INE000000001"}]
            (raw / "nse-api-2000.json").write_text(json.dumps(rows))
            result = older.build(raw, root / "out", root / "out/coverage.json", reports=reports)
            self.assertEqual(result["superseded_by"], AUTHORITATIVE)
            self.assertEqual(result["scope"], "raw_tag_inventory")
            normalized = result["normalized_actions"]
            self.assertEqual(normalized["normalized_action_rows"], 6189)
            self.assertEqual(normalized["terms_verified_rows"], 6056)
            self.assertEqual(normalized["execution_ready_actions"], 343)
            self.assertEqual(normalized["actually_applied_actions"], 342)
            self.assertEqual(normalized["factor_application"]["applied_pre2011_actions"], 342)
            self.assertEqual(normalized["reports"]["normalized_actions"], VERIFIED_ACTIONS)
            # The raw inventory numbers stay intact and stay labelled.
            self.assertEqual(result["official_raw_rows"], 1)
            self.assertEqual(result["raw_inventory"]["rows"], 1)
            self.assertEqual(result["raw_inventory"]["kind"], "tags_only_nse_api_inventory")
            self.assertFalse(result["raw_inventory"]["complete"])
            self.assertEqual(false_prices_adjusted_claims(result), [])

    def test_pre2011_coverage_without_reports_states_nothing_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            (raw / "nse-api-2000.json").write_text(json.dumps(
                [{"symbol": "AAA", "subject": "Agm", "exDate": "04-Jan-2000"}]))
            result = older.build(raw, root / "out", root / "out/coverage.json")
            normalized = result["normalized_actions"]
            self.assertIsNone(normalized["execution_ready_actions"])
            self.assertIsNone(normalized["actually_applied_actions"])


@unittest.skipUnless((REPORTS / "readiness.json").exists(),
                     "legacy readiness report has not been generated")
class GeneratedLegacyReportTests(unittest.TestCase):
    """The checked-in legacy reports must already carry the pointers."""

    def test_readiness_is_a_pointer_and_keeps_the_false_verdict(self):
        report = read_json(REPORTS / "readiness.json")
        self.assertFalse(report["ready_for_backtesting"])
        self.assertEqual(report["superseded_by"], AUTHORITATIVE)
        self.assertEqual(report["authoritative_report"], AUTHORITATIVE)
        self.assertTrue((ROOT / AUTHORITATIVE).exists())

    def test_readiness_numbers_match_the_current_artifacts(self):
        report = read_json(REPORTS / "readiness.json")
        verified = read_json(REPORTS / "verified-actions.json")
        adjustment = read_json(REPORTS / "adjustment-coverage.json")
        block = report["verified_coverage"]["pre2011_actions"]
        self.assertEqual(block["execution_ready_actions"],
                         verified["execution_ready_actions"])
        self.assertEqual(block["terms_verified_rows"], verified["verified_terms_rows"])
        self.assertEqual(block["normalized_action_rows"], verified["normalized_action_rows"])
        self.assertEqual(block["actually_applied_actions"], adjustment["pre2011"]["applied"])

    def test_readiness_has_no_stale_prices_adjusted_claim(self):
        report = read_json(REPORTS / "readiness.json")
        self.assertEqual(false_prices_adjusted_claims(report), [])
        self.assertNotIn("prices_adjusted", report["verified_coverage"]["pre2011_actions"])

    def test_readiness_blocking_work_is_derived_from_the_authoritative_report(self):
        report = read_json(REPORTS / "readiness.json")
        authoritative = read_json(REPORTS / "full-history-readiness.json")
        self.assertEqual(report["blocker_codes"], authoritative["blocker_codes"])
        self.assertEqual([item["code"] for item in report["blocking_work"]],
                         [item["code"] for item in authoritative["blockers"]])

    def test_pre2011_coverage_report_points_at_the_normalized_layer(self):
        report = read_json(REPORTS / "pre2011-actions-coverage.json")
        self.assertEqual(report["superseded_by"], AUTHORITATIVE)
        self.assertEqual(report["scope"], "raw_tag_inventory")
        normalized = report["normalized_actions"]
        verified = read_json(REPORTS / "verified-actions.json")
        adjustment = read_json(REPORTS / "adjustment-coverage.json")
        self.assertEqual(normalized["execution_ready_actions"],
                         verified["execution_ready_actions"])
        self.assertEqual(normalized["actually_applied_actions"], adjustment["pre2011"]["applied"])
        self.assertEqual(normalized["factor_application"]["applied_pre2011_actions"],
                         adjustment["pre2011"]["applied"])
        # The original raw inventory numbers are intact and labelled as such.
        self.assertEqual(report["official_raw_rows"], report["raw_inventory"]["rows"])
        self.assertEqual(report["raw_inventory"]["kind"], "tags_only_nse_api_inventory")
        self.assertGreater(report["official_raw_rows"], 0)

    def test_pre2011_coverage_report_has_no_stale_prices_adjusted_claim(self):
        report = read_json(REPORTS / "pre2011-actions-coverage.json")
        self.assertEqual(false_prices_adjusted_claims(report), [])


if __name__ == "__main__":
    unittest.main()
