"""End-to-end CLI contract for the repaired ``get_data.py`` commands.

These tests pin the *observable* contract of the repaired commands: exit
codes and JSON shapes, exercised through the real command line in a
subprocess.

Every command runs against a throwaway data directory: ``--data-dir`` always
points inside the per-test temporary tree and the child process uses that
tree as its working directory, so the repository's own ``data/`` directory is
never read or written by these tests.

Pinned contract:

* ``readiness`` exits 2 while blockers remain and writes a readiness JSON
  with ``blocker_codes``, ``ready_for_backtesting=false``,
  ``ready_for_alpha_claim=false`` and ``requirements.fundamentals ==
  deferred_not_required``.
* ``repair`` exits nonzero (2) while unresolved and prints JSON containing
  ``schemes``/``actions``/``readiness`` keys.
* ``dataset`` prints JSON with ``cutoff`` and ``scheme_coverage``.
* ``history`` without ``--allow-incomplete`` refuses with a nonzero exit and
  a message naming the blockers, and never fabricates a run.
* ``history --allow-incomplete`` on a tiny dataset either runs or fails loudly.
* ``attachments --help`` exits 0, and every subcommand's ``--help`` exits 0
  while the top-level help lists every expected subcommand.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
GET_DATA = SRC / "get_data.py"

EXPECTED_SUBCOMMANDS = ("fetch", "update", "build", "validate", "readiness",
                         "dataset", "repair", "history", "attachments",
                         "sensitivity", "freeze")

START = "2000-01-03"
END = "2026-11-06"
TIMEOUT = 300


def venv_python() -> Path | None:
    """Interpreter used for every CLI subprocess (the repo venv when present)."""
    for candidate in (REPO / ".venv" / "Scripts" / "python.exe",
                      REPO / ".venv" / "bin" / "python",
                      REPO / ".venv" / "bin" / "python3"):
        if candidate.exists():
            return candidate
    return None


class CliContractTests(unittest.TestCase):
    """Contract of the CLI, not of the individual pipeline modules."""

    maxDiff = None

    def setUp(self) -> None:
        self.python = venv_python()
        if self.python is None:
            self.skipTest("repo venv interpreter not found")
        self._tmp = tempfile.TemporaryDirectory(prefix="cli_contract_")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        try:
            self._seed_tiny_dataset()
        except Exception as exc:  # environment dependent
            self.skipTest(f"cannot build the temporary dataset: {exc!r}")

    # ------------------------------------------------------------------
    # fixtures
    # ------------------------------------------------------------------
    def _seed_tiny_dataset(self) -> None:
        """A minimal, honest dataset: two sessions, one scheme ledger row."""
        import duckdb
        import pandas as pd

        for folder in ("adjusted", "universe", "actions_triage", "ledger"):
            (self.data / "processed" / folder).mkdir(parents=True, exist_ok=True)
        pd.DataFrame({
            "date": [pd.Timestamp("2000-01-03"), pd.Timestamp("2000-01-04")],
            "symbol": ["AAA", "AAA"],
            "series": ["EQ", "EQ"],
            "adj_close": [10.0, 10.5],
        }).to_parquet(self.data / "processed/adjusted/adjusted_bars.parquet")
        pd.DataFrame({
            "ex_date": [pd.Timestamp("2000-01-03")],
            "symbol": ["AAA"], "series": ["EQ"],
            "share_multiplier": [1.0], "decision": ["applied"],
        }).to_parquet(self.data / "processed/adjusted/action_audit.parquet")
        pd.DataFrame({"date": ["2000-01-03"], "symbol": ["AAA"]}).to_parquet(
            self.data / "processed/universe/universe_observations.parquet")
        pd.DataFrame({"date": ["2000-01-03"], "symbol": ["AAA"], "status": ["unresolved"]}).to_parquet(
            self.data / "processed/actions_triage/triage.parquet")
        (self.data / "processed/ledger/canonical-scheme-ledger.jsonl").write_text(
            json.dumps({"terms_verified": True, "execution_ready": False}) + "\n",
            encoding="utf-8")
        db_path = self.data / "market.duckdb"
        if db_path.exists():
            db_path.unlink()
        with duckdb.connect(str(db_path)) as db:
            db.execute("CREATE TABLE bars(date DATE, symbol VARCHAR, series VARCHAR, isin VARCHAR, "
                       "open DOUBLE, close DOUBLE, previous_close DOUBLE, volume BIGINT, "
                       "valid_ohlcv BOOLEAN)")
            db.execute("INSERT INTO bars VALUES "
                       "('2000-01-03','AAA','EQ','INE000A',10.0,10.0,10.0,100000,true), "
                       "('2000-01-04','AAA','EQ','INE000A',10.2,10.5,10.0,120000,true)")
            db.execute("CREATE VIEW cash_equity_candidates AS SELECT * FROM bars")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def run_cli(self, args, timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
        """Run the real CLI against the temporary data directory."""
        self.assertTrue(str(self.data).startswith(str(self.root)),
                        "the CLI must only ever see the temporary data directory")
        cmd = [str(self.python), str(GET_DATA), "--data-dir", str(self.data), *args]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["COLUMNS"] = "200"          # keep argparse usage lines unwrapped
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            return subprocess.run(cmd, cwd=str(self.root), env=env, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.fail(f"CLI timed out after {timeout}s: {' '.join(cmd)}")

    @staticmethod
    def _stdout_json(proc: subprocess.CompletedProcess) -> dict:
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
            raise AssertionError(
                f"stdout is not JSON ({exc}); rc={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}") from exc

    # ------------------------------------------------------------------
    # help contract
    # ------------------------------------------------------------------
    def test_help_lists_every_expected_subcommand(self):
        proc = self.run_cli(["--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = "{" + ",".join(EXPECTED_SUBCOMMANDS) + "}"
        self.assertIn(expected, proc.stdout)
        for name in EXPECTED_SUBCOMMANDS:
            self.assertIn(name, proc.stdout)

    def test_every_subcommand_help_exits_zero(self):
        for name in EXPECTED_SUBCOMMANDS:
            with self.subTest(subcommand=name):
                proc = self.run_cli([name, "--help"])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)
                self.assertIn(name, proc.stdout)

    def test_attachments_help_exits_zero(self):
        proc = self.run_cli(["attachments", "--help"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage:", proc.stdout)

    # ------------------------------------------------------------------
    # readiness
    # ------------------------------------------------------------------
    def test_readiness_exits_two_and_writes_readiness_json(self):
        proc = self.run_cli(["readiness", "--start", START, "--end", END])
        self.assertEqual(proc.returncode, 2,
                         f"readiness must exit 2 while blockers remain; stderr={proc.stderr!r}")

        report_path = self.data / "reports" / "full-history-readiness.json"
        self.assertTrue(report_path.exists(), "readiness must write its readiness JSON")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["blocker_codes"], "unresolved evidence must name its blockers")
        self.assertFalse(report["ready_for_backtesting"])
        self.assertFalse(report["ready_for_alpha_claim"])
        self.assertEqual(report["requirements"]["fundamentals"], "deferred_not_required")

        printed = self._stdout_json(proc)
        self.assertFalse(printed["ready_for_backtesting"])
        self.assertFalse(printed["ready_for_alpha_claim"])
        self.assertEqual(printed["blocker_codes"], report["blocker_codes"])
        self.assertEqual(printed["requirements"]["fundamentals"], "deferred_not_required")

    # ------------------------------------------------------------------
    # repair
    # ------------------------------------------------------------------
    def test_repair_exits_two_and_prints_report_json(self):
        proc = self.run_cli(["repair", "--start", START, "--end", END])
        self.assertNotEqual(proc.returncode, 0, "repair must not report success while unresolved")
        self.assertEqual(proc.returncode, 2,
                         f"repair must exit 2 while unresolved; stderr={proc.stderr!r}")

        payload = self._stdout_json(proc)
        for key in ("schemes", "actions", "readiness"):
            self.assertIn(key, payload)
        readiness = payload["readiness"]
        self.assertFalse(readiness["ready_for_backtesting"])
        self.assertTrue(readiness["blocker_codes"])

    # ------------------------------------------------------------------
    # dataset
    # ------------------------------------------------------------------
    def test_dataset_prints_cutoff_and_scheme_coverage(self):
        proc = self.run_cli(["dataset"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self._stdout_json(proc)
        self.assertIn("cutoff", payload)
        self.assertIn("scheme_coverage", payload)
        self.assertEqual(payload["cutoff"], "2000-01-04")
        self.assertEqual(payload["scheme_coverage"]["ledger_rows"], 1)
        self.assertEqual(payload["scheme_coverage"]["terms_verified"], 1)
        self.assertEqual(payload["scheme_coverage"]["execution_ready"], 0)

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------
    def test_history_refuses_without_allow_incomplete(self):
        out = self.root / "history_strict"
        proc = self.run_cli(["history", "--start", START, "--end", END, "--out", str(out)])
        self.assertNotEqual(proc.returncode, 0, "an unresolved history run must not report success")

        combined = proc.stdout + proc.stderr
        readiness_path = out / "readiness.json"
        self.assertTrue(readiness_path.exists(), "the refusal must still publish its readiness")
        report = json.loads(readiness_path.read_text(encoding="utf-8"))
        self.assertTrue(report["blocker_codes"])
        for code in report["blocker_codes"]:
            self.assertIn(code, combined, f"the refusal must name the blocker {code}")

        # A refused run must not fabricate one.
        self.assertFalse((out / "summary.json").exists())
        self.assertFalse((out / "trades.csv").exists())
        default_out = self.data / "processed" / "backtest" / "full_history_research"
        self.assertFalse((default_out / "summary.json").exists())

    def test_history_with_allow_incomplete_runs_or_fails_loudly(self):
        out = self.root / "history_loose"
        proc = self.run_cli(["history", "--start", START, "--end", END, "--out", str(out),
                             "--allow-incomplete"])
        if proc.returncode == 0:
            summary = self._stdout_json(proc)
            self.assertEqual(summary["mode"], "full_history_research_diagnostic")
            self.assertTrue(summary["allow_incomplete"])
            self.assertFalse(summary["ready_for_backtesting"])
            self.assertFalse(summary["ready_for_alpha_claim"])
            self.assertIn("readiness", summary)
            self.assertTrue((out / "summary.json").exists())
        else:
            combined = proc.stdout + proc.stderr
            self.assertRegex(combined, r"error|Error|Traceback|failed|required",
                             "a failed diagnostic must say why it failed")
            self.assertFalse((out / "summary.json").exists(),
                             "a failed run must not leave a successful-looking summary")


if __name__ == "__main__":
    unittest.main()
