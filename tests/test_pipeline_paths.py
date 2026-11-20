from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class PipelinePathTests(unittest.TestCase):
    def test_refactored_modules_use_repository_data_root(self):
        from nse_pipeline.audit.gaps import DEFAULT_DATA as gaps
        from nse_pipeline.dataset.manifest import DEFAULT_DATA as dataset
        from nse_pipeline.nifty.documentary import ROOT as documentary
        from nse_pipeline.thirdparty.kaggle_ohlcv import ROOT as thirdparty
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(gaps, root / "data")
        self.assertEqual(dataset, root / "data")
        self.assertEqual(documentary, root)
        self.assertEqual(thirdparty, root)
