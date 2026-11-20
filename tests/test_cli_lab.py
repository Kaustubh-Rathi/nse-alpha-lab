"""Tests for the command line surface: argparse wiring and spec validation.

These are deliberately fast and hermetic. Every test either exercises the
parser directly (no subprocess, no I/O) or runs against a temporary fixture
directory. Nothing here touches ``data/``, the network, or another strategy's
engine state, so the suite stays runnable on a machine with no data pack.

The contract under test:

* every subcommand exists and has ``--help``;
* ``verify`` refuses a pack whose bytes do not match its manifest, and never
  needs git;
* ``fetch`` refuses a download that fails hash verification;
* an invalid strategy spec is rejected with a message naming every problem and
  the valid values, rather than silently running with a default;
* the bundled example specs are themselves valid.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from nse_pipeline import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples" / "specs"

#: pytest's ``tmp_path`` and its temp base are unusable in this repository.
#:
#: ``tests/conftest.py`` calls ``protect_artifacts``, which installs a global
#: ``sys.addaudithook``. Audit hooks cannot be removed, and at interpreter exit
#: the environment's ``tempfile`` cleanup tries to remove everything under the
#: temp base -- pytest's ``pytest-of-<user>`` parent included -- which the hook
#: refuses with ``PermissionError``. Fixtures here therefore write into an
#: explicit directory next to the repo instead, and are cleaned up by hand.
_TMP_BASE = REPO_ROOT / ".cli-test-artifacts"


@pytest.fixture
def tmp_path(request):
    """A writable scratch directory that does not trip the artifact guard."""
    root = _TMP_BASE / "t"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_"
                   for ch in request.node.name)[:60]
    directory = root / safe
    # A test may legitimately write next to its own directory (the path-escape
    # case does), so each test gets its own root and the whole root is removed.
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        # A test may have chdir'd into this directory (monkeypatch.chdir), and
        # Windows cannot remove a directory that is a live working directory --
        # move the process out before deleting.
        try:
            os.chdir(REPO_ROOT)
        except OSError:
            pass
        shutil.rmtree(directory, ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            # An empty shell may survive if a handle was still open during
            # teardown; there is never any file left inside it.
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def run_cli(argv: list[str]) -> int:
    """Call ``main`` and return an exit code.

    ``main`` already converts argparse's ``SystemExit`` into an exit code, so
    this is only a thin spelling for readability in assertions.
    """
    return cli.main(argv)


def write_pack(root: Path, files: dict[str, bytes],
               manifest_name: str = "manifest.json") -> Path:
    """Create a data pack plus a matching manifest and return the manifest path."""
    entries = []
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.append({
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        })
    manifest = root / manifest_name
    manifest.write_text(json.dumps({"files": entries}), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# parser wiring: every subcommand exists and documents itself
# ---------------------------------------------------------------------------

def test_top_level_help_lists_every_subcommand(capsys):
    """The root help must name each of the five documented subcommands."""
    exit_code = run_cli(["--help"])
    output = capsys.readouterr().out
    assert exit_code == 0
    for command in ("verify", "fetch", "backtest", "sweep", "list"):
        assert command in output, f"{command} missing from top-level help"


@pytest.mark.parametrize("command", ["verify", "fetch", "backtest", "sweep", "list"])
def test_each_subcommand_has_help(command, capsys):
    """Every subcommand --help must succeed and print a usage line."""
    exit_code = run_cli([command, "--help"])
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "usage: nse-alpha" in output
    assert command in output


def test_verify_help_documents_the_manifest_format(capsys):
    """verify --help is the manifest's documentation: it must show the schema."""
    exit_code = run_cli(["verify", "--help"])
    output = capsys.readouterr().out
    assert exit_code == 0
    # The three fields a manifest entry carries.
    for field in ("path", "sha256", "size"):
        assert field in output
    # The schema must be shown as a concrete JSON example, not prose alone.
    assert '"files"' in output
    # And the guarantees that make verification trustworthy.
    assert "git" in output.lower()
    assert "exit status" in output.lower()


def test_backtest_help_documents_spec_fields_and_valid_values(capsys):
    """backtest --help must enumerate the spec schema and the valid values."""
    exit_code = run_cli(["backtest", "--help"])
    output = capsys.readouterr().out
    assert exit_code == 0
    for field in ("universe", "benchmark", "top_n", "direction",
                  "lookback_sessions", "cadence", "stop_loss_pct",
                  "take_profit_pct", "cost_model", "start", "end"):
        assert field in output, f"spec field {field} not documented"
    for value in ("top", "bottom", "monthly", "quarterly",
                  "full_charges", "zero_cost"):
        assert value in output, f"valid value {value} not documented"


def test_bare_invocation_prints_help_and_signals_usage_error(capsys):
    """No subcommand is a usage error (exit 2), not a silent success."""
    exit_code = run_cli([])
    output = capsys.readouterr().out
    assert exit_code == cli.EXIT_USAGE
    assert "usage: nse-alpha" in output


def test_unknown_subcommand_is_a_usage_error(capsys):
    """An unknown command is refused by argparse as a usage error (exit 2)."""
    exit_code = run_cli(["does-not-exist"])
    assert exit_code == cli.EXIT_USAGE


@pytest.mark.parametrize("command", ["backtest", "verify", "fetch"])
def test_required_arguments_are_enforced(command, capsys):
    """A missing required argument is a usage error, never a traceback.

    ``backtest`` requires a spec path. ``verify`` defaults to the repository
    root and ``fetch`` to the data root, so for those the requirement is that
    the bare form parses and reports a real error rather than crashing.
    """
    if command == "backtest":
        exit_code = run_cli(["backtest"])
        assert exit_code == cli.EXIT_USAGE
        assert "SPEC_JSON" in capsys.readouterr().err
    elif command == "fetch":
        # --from is required for every source; it is validated after parsing.
        exit_code = cli.main(["fetch", "--source", "local"])
        assert exit_code == cli.EXIT_USAGE
        assert "--from is required" in capsys.readouterr().err
    else:
        # Bare verify is valid syntax. With no pack in the repository it must
        # report "no manifest found" as a usage error, not crash.
        exit_code = cli.main(["verify"])
        assert exit_code == cli.EXIT_USAGE
        assert "no manifest found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# spec validation
# ---------------------------------------------------------------------------

def valid_spec(**overrides) -> dict:
    spec = {
        "universe": "nifty500",
        "benchmark": "nifty500",
        "top_n": 20,
        "direction": "top",
        "lookback_sessions": 63,
        "cadence": "monthly",
        "stop_loss_pct": None,
        "take_profit_pct": None,
        "cost_model": "full_charges",
        "start": "2013-01-31",
        "end": "2026-09-30",
    }
    spec.update(overrides)
    return spec


def test_valid_spec_has_no_problems():
    assert cli.validate_spec(valid_spec()) == []


@pytest.mark.parametrize("field,value", [
    ("direction", "sideways"),
    ("cadence", "weekly"),
    ("cost_model", "cheap"),
    ("benchmark", "sensex"),
])
def test_invalid_enum_values_are_named_with_their_valid_set(field, value):
    """A bad enum value must be rejected AND the valid set must be printed."""
    problems = cli.validate_spec(valid_spec(**{field: value}))
    assert problems, f"{field}={value} was wrongly accepted"
    text = "\n".join(problems)
    assert field in text
    assert repr(value) in text
    # The valid values must appear so the caller can fix it from the error.
    valid = {
        "direction": cli.VALID_DIRECTIONS,
        "cadence": cli.VALID_CADENCES,
        "cost_model": cli.VALID_COST_MODELS,
        "benchmark": cli.VALID_BENCHMARKS,
    }[field]
    for allowed in valid:
        assert allowed in text


@pytest.mark.parametrize("field,value", [
    ("top_n", 0),
    ("top_n", -5),
    ("lookback_sessions", 0),
    ("stop_loss_pct", 100),
    ("stop_loss_pct", 150),
    ("stop_loss_pct", 0),
    ("take_profit_pct", -5),
])
def test_out_of_range_numbers_are_rejected(field, value):
    problems = cli.validate_spec(valid_spec(**{field: value}))
    assert problems, f"{field}={value} was wrongly accepted"
    assert field in "\n".join(problems)


@pytest.mark.parametrize("field", ["top_n", "lookback_sessions"])
def test_non_integer_counts_are_rejected(field):
    """A float or string where a count belongs is an error, not a coercion."""
    for value in (2.5, "20", True):
        problems = cli.validate_spec(valid_spec(**{field: value}))
        assert problems, f"{field}={value!r} was wrongly accepted"


@pytest.mark.parametrize("field", ["start", "end"])
def test_non_iso_dates_are_rejected(field):
    for value in ("31/01/2013", "2013-13-01", "2013", "yesterday"):
        problems = cli.validate_spec(valid_spec(**{field: value}))
        assert problems, f"{field}={value!r} was wrongly accepted"
        assert field in "\n".join(problems)


def test_start_after_end_is_rejected():
    problems = cli.validate_spec(valid_spec(start="2026-09-30", end="2013-01-31"))
    assert problems
    assert "start" in "\n".join(problems)


def test_missing_universe_is_rejected():
    spec = valid_spec()
    del spec["universe"]
    problems = cli.validate_spec(spec)
    assert problems
    assert "universe" in "\n".join(problems)


def test_unknown_field_is_rejected_not_ignored():
    """A typo must fail loudly: silently dropping a stop-loss changes results."""
    problems = cli.validate_spec(valid_spec(**{"stop_loss": 20}))
    assert problems
    text = "\n".join(problems)
    assert "stop_loss" in text
    assert "unknown field" in text


def test_spec_root_must_be_an_object():
    problems = cli.validate_spec([valid_spec()])
    assert problems
    assert "JSON object" in "\n".join(problems)


def test_multiple_problems_are_all_reported():
    """One run must surface every fault, not just the first one found."""
    spec = valid_spec(direction="sideways", cadence="weekly", top_n=0,
                      start="31/01/2013")
    problems = cli.validate_spec(spec)
    text = "\n".join(problems)
    assert len(problems) >= 4
    for field in ("direction", "cadence", "top_n", "start"):
        assert field in text


# ---------------------------------------------------------------------------
# spec loading through the CLI
# ---------------------------------------------------------------------------

def test_invalid_spec_file_exits_usage_with_readable_error(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(valid_spec(direction="sideways")), encoding="utf-8")
    exit_code = cli.main(["backtest", str(path)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "invalid strategy spec" in captured.err
    assert "sideways" in captured.err
    assert "top" in captured.err and "bottom" in captured.err


def test_missing_spec_file_exits_usage(tmp_path, capsys):
    exit_code = cli.main(["backtest", str(tmp_path / "nope.json")])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "spec file not found" in captured.err


def test_malformed_spec_json_exits_usage(tmp_path, capsys):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    exit_code = cli.main(["backtest", str(path)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "not valid JSON" in captured.err


def test_load_spec_returns_the_parsed_object(tmp_path):
    path = tmp_path / "good.json"
    spec = valid_spec(name="unit")
    path.write_text(json.dumps(spec), encoding="utf-8")
    assert cli.load_spec(path) == spec


# ---------------------------------------------------------------------------
# bundled example specs
# ---------------------------------------------------------------------------

def test_bundled_example_specs_all_exist_and_validate():
    """The three shipped specs must exist and satisfy the documented schema."""
    for name in ("primary.json", "stop_loss.json", "bottom20.json"):
        path = EXAMPLES / name
        assert path.is_file(), f"missing bundled spec {name}"
        loaded = cli.load_spec(path)  # raises CliError if invalid
        assert cli.validate_spec(loaded) == []


def test_bundled_specs_differ_in_ways_that_change_results():
    """The examples must be distinct strategies, not three copies of one."""
    specs = {name: cli.load_spec(EXAMPLES / f"{name}.json")
             for name in ("primary", "stop_loss", "bottom20")}
    assert specs["primary"]["direction"] == "top"
    assert specs["bottom20"]["direction"] == "bottom"
    assert specs["stop_loss"]["stop_loss_pct"] is not None
    assert specs["primary"]["stop_loss_pct"] is None


def test_list_reports_bundled_specs_without_error(capsys):
    exit_code = cli.main(["list"])
    output = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    for name in ("primary", "stop_loss", "bottom20"):
        assert name in output


def test_list_strict_passes_when_every_spec_is_valid(capsys):
    exit_code = cli.main(["list", "--strict"])
    assert exit_code == cli.EXIT_OK


# ---------------------------------------------------------------------------
# verify: SHA256 + size, no git
# ---------------------------------------------------------------------------

def test_verify_passes_on_an_intact_pack(tmp_path, capsys):
    write_pack(tmp_path, {"bars.parquet": b"binary payload",
                          "meta/info.json": b'{"rows": 2}'})
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    output = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "PASSED" in output


def test_verify_detects_a_corrupted_file(tmp_path, capsys):
    """Flipping one byte must be caught by the SHA256 comparison."""
    write_pack(tmp_path, {"bars.parquet": b"original bytes"})
    (tmp_path / "bars.parquet").write_bytes(b"tampered bytes")
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "MISMATCH" in captured.out
    assert "FAILED" in captured.err


def test_verify_detects_a_missing_file(tmp_path, capsys):
    write_pack(tmp_path, {"a.bin": b"a", "b.bin": b"b"})
    (tmp_path / "b.bin").unlink()
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "MISSING" in captured.out


def test_verify_detects_a_size_mismatch_without_hashing(tmp_path, capsys):
    """Size is checked first, so a truncated file is reported as a size fault."""
    manifest = write_pack(tmp_path, {"a.bin": b"abcdef"})
    (tmp_path / "a.bin").write_bytes(b"abc")
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "size" in captured.out
    assert manifest.exists()


def test_verify_never_invokes_git(tmp_path, monkeypatch, capsys):
    """Verification is a content check: no git binary may be executed."""
    write_pack(tmp_path, {"a.bin": b"payload"})

    def forbid(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("verify must not spawn a subprocess")

    monkeypatch.setattr(cli.subprocess, "run", forbid)
    monkeypatch.setattr(cli.subprocess, "Popen", forbid)
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    assert exit_code == cli.EXIT_OK


def test_verify_refuses_an_empty_manifest(tmp_path, capsys):
    """An empty manifest verifies nothing, so it must not report success."""
    (tmp_path / "manifest.json").write_text('{"files": []}', encoding="utf-8")
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "no files" in captured.err


def test_verify_refuses_a_malformed_manifest(tmp_path, capsys):
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "not valid JSON" in captured.err


def test_verify_refuses_a_manifest_entry_without_a_sha256(tmp_path, capsys):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"files": [{"path": "a.bin", "size": 1}]}), encoding="utf-8")
    (tmp_path / "a.bin").write_bytes(b"a")
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "sha256" in captured.err


def test_verify_refuses_a_path_that_escapes_the_pack(tmp_path, capsys):
    """A '../' entry must be refused, never followed outside the pack."""
    container = tmp_path / "container"
    container.mkdir()
    outside = container / "outside-secret.bin"
    outside.write_bytes(b"secret")
    pack = container / "pack"
    pack.mkdir()
    payload = b"secret"
    (pack / "manifest.json").write_text(json.dumps({"files": [{
        "path": "../outside-secret.bin",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }]}), encoding="utf-8")
    exit_code = cli.main(["verify", "--pack", str(pack)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "escapes the pack directory" in captured.out


def test_manifest_aliases_and_mapping_form_are_accepted(tmp_path):
    """'entries' and a {path: {...}} mapping are documented alternatives."""
    root = tmp_path / "entries"
    root.mkdir()
    (root / "a.bin").write_bytes(b"abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    (root / "manifest.json").write_text(
        json.dumps({"entries": [{"path": "a.bin", "sha256": digest, "size": 3}]}),
        encoding="utf-8")
    assert cli.verify_pack(root, root / "manifest.json")["passed"] is True

    mapped = tmp_path / "mapped"
    mapped.mkdir()
    (mapped / "b.bin").write_bytes(b"xyz")
    (mapped / "manifest.json").write_text(
        json.dumps({"files": {"b.bin": {
            "sha256": hashlib.sha256(b"xyz").hexdigest(), "size": 3}}}),
        encoding="utf-8")
    assert cli.verify_pack(mapped, mapped / "manifest.json")["passed"] is True


def test_verify_uses_the_first_default_manifest_name(tmp_path, capsys):
    write_pack(tmp_path, {"a.bin": b"a"}, manifest_name="pack.json")
    exit_code = cli.main(["verify", "--pack", str(tmp_path)])
    assert exit_code == cli.EXIT_OK


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def test_fetch_local_copies_and_verifies(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"payload")
    manifest = write_pack(tmp_path / "ref", {"a.bin": b"payload"})
    exit_code = cli.main(["fetch", "--source", "local", "--from", str(source),
                          "--dest", str(tmp_path / "dest"),
                          "--manifest", str(manifest)])
    output = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "PASSED" in output


def test_fetch_refuses_and_removes_a_hash_mismatch(tmp_path, capsys):
    """A pack that fails verification must be deleted, not left usable."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"good payload")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [{
        "path": "a.bin", "sha256": "0" * 64, "size": len(b"good payload")}]}),
        encoding="utf-8")
    dest = tmp_path / "dest"
    exit_code = cli.main(["fetch", "--source", "local", "--from", str(source),
                          "--dest", str(dest), "--manifest", str(manifest)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "REFUSING" in captured.err
    # The payload directory is gone, so nothing partial can be used downstream.
    assert not dest.exists()


def test_fetch_requires_a_from_reference(tmp_path, capsys):
    exit_code = cli.main(["fetch", "--dest", str(tmp_path / "d")])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "--from is required" in captured.err


def test_fetch_rejects_a_bad_kaggle_reference(tmp_path, capsys):
    exit_code = cli.main(["fetch", "--source", "kaggle", "--from", "no-slash",
                          "--dest", str(tmp_path / "d")])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "OWNER/DATASET" in captured.err


def test_fetch_rejects_a_non_url_reference(tmp_path, capsys):
    exit_code = cli.main(["fetch", "--source", "url", "--from", "not a url",
                          "--dest", str(tmp_path / "d")])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "http" in captured.err.lower()


@pytest.mark.parametrize("source", ["kaggle", "hf"])
def test_fetch_reports_a_missing_backend_library(source, tmp_path, capsys):
    """A missing optional backend prints install instructions, not a traceback."""
    reference = "owner/dataset" if source == "kaggle" else "owner/dataset"
    exit_code = cli.main(["fetch", "--source", source, "--from", reference,
                          "--dest", str(tmp_path / "d")])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "pip install" in captured.err


def test_fetch_refuses_a_non_empty_destination_without_force(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"a")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "existing.bin").write_bytes(b"keep me")
    exit_code = cli.main(["fetch", "--source", "local", "--from", str(source),
                          "--dest", str(dest)])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "already exists" in captured.err
    assert (dest / "existing.bin").exists()


def test_fetch_without_manifest_says_the_pack_is_unverified(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"a")
    exit_code = cli.main(["fetch", "--source", "local", "--from", str(source),
                          "--dest", str(tmp_path / "dest")])
    output = capsys.readouterr().out
    assert exit_code == cli.EXIT_OK
    assert "NOT hash-verified" in output


# ---------------------------------------------------------------------------
# engine config mapping
# ---------------------------------------------------------------------------

def test_zero_cost_model_maps_to_zero_charges():
    """The counterfactual cost preset must actually zero every charge."""
    config = cli.build_engine_config(valid_spec(cost_model="zero_cost"))
    for attribute in ("brokerage_bps", "stt_bps_buy", "stt_bps_sell",
                      "stamp_bps", "exchange_bps", "gst_on_brokerage_pct",
                      "slippage_bps"):
        assert int(getattr(config, attribute)) == 0, attribute


def test_full_charges_keeps_the_engine_defaults():
    config = cli.build_engine_config(valid_spec(cost_model="full_charges"))
    assert int(config.slippage_bps) == 5
    assert int(config.stt_bps_buy) == 10


def test_percent_specs_become_positive_fractions():
    """A 20% stop is 0.20 for ExitPolicy, which takes magnitudes below 1."""
    from decimal import Decimal

    config = cli.build_engine_config(
        valid_spec(stop_loss_pct=20, take_profit_pct=10))
    assert config.exit_policy.stop_loss_pct == Decimal("0.20")
    assert config.exit_policy.take_profit_pct == Decimal("0.10")


def test_no_overlay_produces_an_inactive_policy():
    config = cli.build_engine_config(valid_spec())
    assert config.exit_policy.active is False


def test_spec_label_distinguishes_cost_models():
    """Two runs differing only in charges must not share a result label."""
    full = cli.build_engine_config(valid_spec(cost_model="full_charges"))
    zero = cli.build_engine_config(valid_spec(cost_model="zero_cost"))
    assert cli.spec_label(valid_spec(cost_model="full_charges"), full) != \
        cli.spec_label(valid_spec(cost_model="zero_cost"), zero)


def test_spec_label_matches_the_documented_shape():
    label = cli.spec_label(valid_spec(), cli.build_engine_config(valid_spec()))
    assert label.startswith("monthly-N20-L63-")
    assert "primary" not in label


# ---------------------------------------------------------------------------
# sweep wrapper
# ---------------------------------------------------------------------------

def test_sweep_merge_rejects_shard_flags(tmp_path, capsys):
    exit_code = cli.main(["sweep", "merge", "--shard", "0"])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_USAGE
    assert "--shard" in captured.err


def test_sweep_reports_a_missing_research_script(tmp_path, monkeypatch, capsys):
    """A missing research script is an error message, never a traceback."""
    monkeypatch.setattr(cli, "RESEARCH_DIR", tmp_path)
    exit_code = cli.main(["sweep", "run"])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "research script not found" in captured.err


def test_sweep_relays_a_nonzero_child_status(tmp_path, monkeypatch, capsys):
    """The wrapper's exit code must reflect the child's, so CI notices."""
    (tmp_path / "run_sweep.py").write_text("raise SystemExit(3)", encoding="utf-8")
    monkeypatch.setattr(cli, "RESEARCH_DIR", tmp_path)
    exit_code = cli.main(["sweep", "run"])
    captured = capsys.readouterr()
    assert exit_code == cli.EXIT_FAIL
    assert "exited with status 3" in captured.err


def test_sweep_child_env_makes_src_importable(monkeypatch, tmp_path):
    """A relative PYTHONPATH must be re-anchored before the child changes cwd."""
    monkeypatch.chdir(tmp_path)
    env = cli._child_env()
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert str((REPO_ROOT / "src").resolve()) in entries
    assert "" not in entries
