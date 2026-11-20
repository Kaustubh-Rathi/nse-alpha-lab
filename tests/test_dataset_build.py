import json
from pathlib import Path
import sys
import tempfile
import unittest

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nse_pipeline.dataset.manifest import build_dataset


class DatasetBuildTests(unittest.TestCase):
    def test_actual_cutoff_canonical_ledger_and_no_required_fundamentals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for folder in ("adjusted", "universe", "actions_triage", "ledger"):
                (root / "processed" / folder).mkdir(parents=True)
            pd.DataFrame({"close": [10.0]}).to_parquet(root / "processed/adjusted/adjusted_bars.parquet")
            pd.DataFrame({"date": ["2000-01-03"]}).to_parquet(root / "processed/universe/universe_observations.parquet")
            pd.DataFrame({"status": ["unresolved"]}).to_parquet(root / "processed/actions_triage/triage.parquet")
            canonical = root / "processed/ledger/canonical-scheme-ledger.jsonl"
            canonical.write_text(json.dumps({"terms_verified": True, "execution_ready": False}) + "\n")
            with duckdb.connect(str(root / "market.duckdb")) as db:
                db.execute("CREATE TABLE bars AS SELECT DATE '2000-01-03' date")
                db.execute("CREATE VIEW cash_equity_candidates AS SELECT * FROM bars")
            result = build_dataset(root)
            self.assertEqual(result["cutoff"], "2000-01-03")
            self.assertEqual(result["scheme_coverage"]["terms_verified"], 1)
            self.assertEqual(result["scheme_coverage"]["execution_ready"], 0)
            self.assertIn(str(canonical).replace("\\", "/"), result["dataset_sql"]["scheme_ledger"])
            self.assertNotIn("fundamentals_index", result["dataset_sql"])
            self.assertIn("sha256", result["outputs"]["scheme-ledger.jsonl"])


if __name__ == "__main__":
    unittest.main()
