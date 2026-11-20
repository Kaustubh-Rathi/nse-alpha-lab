"""Full-history claims must depend on coverage, not passing unit tests."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class ReadinessTests(unittest.TestCase):
    def test_missing_evidence_blocks_not_fundamentals(self):
        from nse_pipeline.audit.readiness import evaluate_evidence
        result = evaluate_evidence({}, "2000-01-03", "2026-11-06")
        self.assertFalse(result["ready_for_backtesting"])
        self.assertEqual(result["requirements"]["fundamentals"], "deferred_not_required")
        self.assertNotIn("nifty_membership", result["blocker_codes"])
        self.assertIn("corporate_actions", result["blocker_codes"])

    def test_counts_alone_never_establish_complete_actions(self):
        from nse_pipeline.audit.readiness import evaluate_evidence
        result = evaluate_evidence({"prices": {"first": "2000-01-03", "last": "2026-11-06",
                                              "rows": 100, "duplicate_rows": 0},
                                    "corporate_actions": {"verified": 47, "applied": 47}},
                                   "2000-01-03", "2026-11-06")
        self.assertFalse(result["ready_for_backtesting"])
        self.assertIn("corporate_actions", result["blocker_codes"])

    def test_nifty_only_required_when_requested(self):
        from nse_pipeline.audit.readiness import evaluate_evidence
        result = evaluate_evidence({}, "2000-01-03", "2026-11-06", universe="nifty50")
        self.assertIn("nifty_membership", result["blocker_codes"])

    def test_report_build_is_readonly_and_reports_actual_cutoff(self):
        from nse_pipeline.audit.readiness import build_readiness
        import duckdb
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with duckdb.connect(str(root / "market.duckdb")) as db:
                db.execute("CREATE TABLE bars(date DATE, symbol VARCHAR, series VARCHAR, valid_ohlcv BOOLEAN)")
                db.execute("INSERT INTO bars VALUES ('2000-01-03','A','EQ',true), ('2026-11-06','A','EQ',true)")
            result = build_readiness(root, "2000-01-03", "2026-11-07")
            self.assertEqual(result["evidence"]["prices"]["last"], "2026-11-06")
            self.assertIn("price_window", result["blocker_codes"])
            self.assertFalse(result["ready_for_backtesting"])
            self.assertTrue((root / "reports" / "full-history-readiness.json").exists())


if __name__ == "__main__":
    unittest.main()
