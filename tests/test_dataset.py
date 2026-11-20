from __future__ import annotations
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class DatasetTests(unittest.TestCase):
    def test_manifest_has_sql_and_limitations(self):
        # Locate built manifest if present; otherwise test build on temp copy structure is covered by integration run
        manifest = Path(__file__).resolve().parents[1] / "data" / "processed" / "dataset" / "dataset_manifest.json"
        if not manifest.exists():
            self.skipTest("dataset not built yet")
        j = json.loads(manifest.read_text())
        self.assertIn("dataset_sql", j)
        self.assertIn("bars_adjusted", j["dataset_sql"])
        self.assertTrue(len(j["limitations"]) >= 3)
        self.assertIn("adjusted_bars.parquet", j["outputs"])


if __name__ == "__main__":
    unittest.main()
