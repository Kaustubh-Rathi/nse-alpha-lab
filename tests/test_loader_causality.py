"""Future fund classification cannot erase prior stock observations."""
from pathlib import Path
import sys
import tempfile
import unittest

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nse_pipeline.backtest.engine import load_bars_from_duckdb, load_adjusted_bars


class LoaderCausalityTests(unittest.TestCase):
    def test_future_isin_does_not_filter_historical_null_isin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dbpath, parquet = root / "market.duckdb", root / "adjusted.parquet"
            with duckdb.connect(str(dbpath)) as db:
                db.execute("CREATE TABLE bars AS SELECT DATE '2000-01-03' date, 'A' symbol, 'EQ' series, "
                           "NULL::VARCHAR isin, 10.0 AS open, 10.0 AS high, 10.0 AS low, 10.0 AS close, "
                           "10.0 previous_close, 10000 volume, true valid_ohlcv")
                db.execute("INSERT INTO bars SELECT DATE '2020-01-03', 'A', 'EQ', 'INF999', 10,10,10,10,10,10000,true")
                db.execute("CREATE VIEW cash_equity_candidates AS SELECT *, 'SYMBOL:' || symbol security_key "
                           "FROM bars WHERE symbol NOT IN (SELECT symbol FROM bars WHERE isin LIKE 'INF%')")
                db.execute("COPY (SELECT *, open adj_open, high adj_high, low adj_low, close adj_close, "
                           "previous_close adj_previous_close, 1.0 adjustment_factor, 0 n_future_actions, false is_adjusted "
                           "FROM bars) TO ? (FORMAT PARQUET)", [str(parquet)])
            for loader, path in ((load_bars_from_duckdb, dbpath), (load_adjusted_bars, parquet)):
                with self.subTest(loader=loader.__name__):
                    bars = loader(str(path), "2000-01-01", "2000-12-31")
                    self.assertEqual(bars["symbol"].tolist(), ["A"])


if __name__ == "__main__":
    unittest.main()
