"""Public data-root behavior, independent of the caller's working directory."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_path_precedence_and_settings_are_resolved_at_call_time(tmp_path, monkeypatch):
    from nse_pipeline.paths import REPO_ROOT, data_root, repo_relative, resolve_data
    from nse_pipeline.config import Settings

    monkeypatch.delenv("NSE_DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert data_root() == REPO_ROOT / "data"
    alternate = tmp_path / "alternate data"
    monkeypatch.setenv("NSE_DATA_DIR", str(alternate))
    assert data_root() == alternate
    assert Settings().data == alternate
    assert resolve_data("explicit") == tmp_path / "explicit"
    assert Settings(data=Path("explicit")).data == tmp_path / "explicit"
    assert repo_relative(REPO_ROOT / "src" / "get_data.py") == "src/get_data.py"
    with pytest.raises(ValueError):
        repo_relative(tmp_path)
    monkeypatch.setenv("NSE_DATA_DIR", "external-data")
    assert data_root() == REPO_ROOT / "external-data"


def test_cli_uses_environment_root_from_unrelated_cwd(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Relocate source as well: a failing default-resolution test must never
    # write into the real repository's production data tree.
    relocated = tmp_path / "relocated"
    shutil.copytree(root / "src", relocated / "src", ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ, NSE_DATA_DIR=str(tmp_path / "chosen"))
    result = subprocess.run(
        [sys.executable, str(relocated / "src/get_data.py"), "readiness"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stderr
    assert (tmp_path / "chosen/reports/full-history-readiness.json").exists()
    assert not (tmp_path / "data").exists()
    assert not (relocated / "data").exists()


@pytest.mark.parametrize("version", (2, 3))
def test_verifier_queue_reader_honors_arbitrarily_named_explicit_root(tmp_path, version):
    from importlib import import_module

    data = tmp_path / "explicitly chosen market"
    queue = data / f"processed/schemes/verified{version}/ranked-scheme-days.json"
    queue.parent.mkdir(parents=True)
    queue.write_text(json.dumps({"rows": [{"rank": 99, "symbol": "FIXTURE"}]}), encoding="utf-8")
    module = import_module(f"nse_pipeline.schemes.verify{version}")
    assert module.load_ranked(tmp_path, data=data) == [{"rank": 99, "symbol": "FIXTURE"}]
    assert not (tmp_path / "data").exists()
