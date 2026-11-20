"""The inventory report exposes all eight D-18 states through one shared predicate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy

import pytest

from nse_pipeline.stage1_evidence import (
    INTEGRITY_OK_STATES,
    inventory_integrity_ok,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "golden" / "report_matrix_cases.json").read_text(encoding="utf-8"))
VERIFY = runpy.run_path(str(ROOT / "scripts" / "verify_stage1_inventory.py"))
ORIGINAL = b"original bytes"
ACCEPTED = b"accepted bytes"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def setup_tree(tmp_path: Path, *, baseline: bytes = ORIGINAL, actual: bytes = ORIGINAL,
               path: str = "data/raw/source") -> tuple[Path, Path]:
    artifact = tmp_path / path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(actual)
    inventory = tmp_path / "data/manifests/dataset_hashes.json"
    inventory.parent.mkdir(parents=True, exist_ok=True)
    inventory.write_text(json.dumps({"data": [{"path": path, "sha256": sha(baseline)}]}),
                         encoding="utf-8")
    return artifact, inventory


def acknowledged(path: str = "data/raw/source", *, original: bytes = ORIGINAL,
                 accepted: bytes = ACCEPTED) -> list[dict]:
    return [{"path": path, "original_sha256": sha(original), "accepted_sha256": sha(accepted)}]


def test_inventory_reads_bytes_and_discloses_unexpected_change(tmp_path):
    artifact, inventory = setup_tree(tmp_path)
    first = VERIFY["verify"](tmp_path)
    assert first["state"] == "CLEAN"
    assert first["integrity_ok"] is True
    assert first["all_hashes_match"] is True
    assert first["checked_files"] == 1
    assert first["raw_files_checked"] == 1
    assert first["inventory_sha256"] == sha(inventory.read_bytes())
    assert first["only_accepted_exception"] is False

    artifact.write_bytes(b"abc")
    changed = VERIFY["verify"](tmp_path)
    assert changed["state"] == "UNEXPECTED_CHANGE"
    assert not changed["all_hashes_match"]
    assert changed["integrity_ok"] is False
    assert changed["unexpected_changes"][0]["after_sha256"] == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    assert changed["acknowledged_unrecovered_changes"] == []
    assert changed["acknowledged_changes"] == []


def test_clean_inventory_is_integrity_ok_and_exits_zero(tmp_path):
    setup_tree(tmp_path)
    result = VERIFY["verify"](tmp_path)
    assert result["state"] == "CLEAN"
    assert result["integrity_ok"] is True
    assert result["exception_absent"] == []
    assert VERIFY["exit_code"](result) == 0


def test_authorized_exception_is_integrity_ok_and_exits_zero(tmp_path):
    setup_tree(tmp_path, actual=ACCEPTED)
    result = VERIFY["verify"](tmp_path, acknowledged=acknowledged())
    assert result["state"] == "AUTHORIZED_EXCEPTION"
    assert result["integrity_ok"] is True
    assert result["only_accepted_exception"] is True
    assert len(result["acknowledged_changes"]) == 1
    assert result["unexpected_changes"] == []
    assert VERIFY["exit_code"](result) == 0


def test_authorized_exception_absent_is_recorded_and_ok(tmp_path):
    setup_tree(tmp_path, actual=ORIGINAL)
    result = VERIFY["verify"](tmp_path, acknowledged=acknowledged())
    assert result["state"] == "AUTHORIZED_EXCEPTION_ABSENT"
    assert result["integrity_ok"] is True
    assert result["exception_absent"] == ["data/raw/source"]
    assert result["only_accepted_exception"] is False
    assert VERIFY["exit_code"](result) == 0


def test_unexpected_change_is_not_integrity_ok_and_exits_one(tmp_path):
    setup_tree(tmp_path, actual=b"changed")
    result = VERIFY["verify"](tmp_path)
    assert result["state"] == "UNEXPECTED_CHANGE"
    assert result["integrity_ok"] is False
    assert VERIFY["exit_code"](result) == 1


def test_missing_file_is_distinct_from_read_error(tmp_path, monkeypatch):
    artifact, _ = setup_tree(tmp_path)
    artifact.unlink()
    missing = VERIFY["verify"](tmp_path)
    assert missing["state"] == "MISSING_FILE"
    assert missing["missing_files"] == ["data/raw/source"]
    assert missing["read_errors"] == []
    assert missing["errors"][0]["path"] == "data/raw/source"
    assert VERIFY["exit_code"](missing) == 1

    artifact.write_bytes(ORIGINAL)
    original_open = Path.open

    def denied(self, *args, **kwargs):
        if self.resolve() == artifact.resolve():
            raise PermissionError("permission denied by test")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    broken = VERIFY["verify"](tmp_path)
    assert broken["state"] == "READ_ERROR"
    assert broken["missing_files"] == []
    assert broken["read_errors"] == ["data/raw/source"]
    assert VERIFY["exit_code"](broken) == 1


def test_authorized_exception_mismatch_is_rejected(tmp_path):
    setup_tree(tmp_path, actual=b"neither original nor accepted")
    result = VERIFY["verify"](tmp_path, acknowledged=acknowledged())
    assert result["state"] == "AUTHORIZED_EXCEPTION_MISMATCH"
    assert result["integrity_ok"] is False
    assert VERIFY["exit_code"](result) == 1


def test_exception_list_changed_when_acknowledged_path_is_absent_or_rebased(tmp_path):
    setup_tree(tmp_path)
    absent = VERIFY["verify"](tmp_path, acknowledged=acknowledged("data/raw/other"))
    assert absent["state"] == "EXCEPTION_LIST_CHANGED"
    assert absent["integrity_ok"] is False

    _, inventory = setup_tree(tmp_path)
    inventory.write_text(json.dumps({"data": [{"path": "data/raw/source", "sha256": sha(b"rebased")}]}),
                         encoding="utf-8")
    rebased = VERIFY["verify"](tmp_path, acknowledged=acknowledged())
    assert rebased["state"] == "EXCEPTION_LIST_CHANGED"
    assert VERIFY["exit_code"](rebased) == 1


def test_missing_file_precedes_unexpected_change_in_one_run(tmp_path):
    changed, _ = setup_tree(tmp_path, actual=b"changed")
    second = tmp_path / "data/raw/second"
    second.write_bytes(b"second")
    inventory = tmp_path / "data/manifests/dataset_hashes.json"
    entries = json.loads(inventory.read_text(encoding="utf-8"))
    entries["data"].append({"path": "data/raw/second", "sha256": sha(b"right")})
    inventory.write_text(json.dumps(entries), encoding="utf-8")
    second.unlink()
    result = VERIFY["verify"](tmp_path)
    assert result["state"] == "MISSING_FILE"
    assert len(result["unexpected_changes"]) == 1


def test_all_eight_states_match_the_hand_authored_golden(tmp_path):
    states = [row["state"] for row in GOLDEN["inventory_states"]]
    assert states == [
        "CLEAN", "AUTHORIZED_EXCEPTION", "AUTHORIZED_EXCEPTION_ABSENT",
        "UNEXPECTED_CHANGE", "MISSING_FILE", "READ_ERROR",
        "EXCEPTION_LIST_CHANGED", "AUTHORIZED_EXCEPTION_MISMATCH",
    ]
    assert set(states) == set(INTEGRITY_OK_STATES) | {
        "UNEXPECTED_CHANGE", "MISSING_FILE", "READ_ERROR",
        "EXCEPTION_LIST_CHANGED", "AUTHORIZED_EXCEPTION_MISMATCH",
    }
    for row in GOLDEN["inventory_states"]:
        case = tmp_path / row["name"]
        actual = ACCEPTED if row["state"] == "AUTHORIZED_EXCEPTION" else (
            b"neither" if row["state"] == "AUTHORIZED_EXCEPTION_MISMATCH" else ORIGINAL)
        if row["state"] == "UNEXPECTED_CHANGE":
            actual = b"changed"
        artifact, _ = setup_tree(case, actual=actual)
        ack = () if row["state"] == "CLEAN" else acknowledged()
        if row["state"] in ("UNEXPECTED_CHANGE", "MISSING_FILE", "READ_ERROR"):
            ack = ()
        if row["state"] == "MISSING_FILE":
            artifact.unlink()
        if row["state"] == "READ_ERROR":
            artifact.unlink()
            artifact.mkdir()
        if row["state"] == "EXCEPTION_LIST_CHANGED":
            ack = acknowledged("data/raw/other")
        result = VERIFY["verify"](case, acknowledged=ack)
        assert result["state"] == row["state"], row["name"]
        assert result["integrity_ok"] is row["integrity_ok"]
        assert (VERIFY["exit_code"](result) == 0) is (row["cli_exit"] == 0)


def test_cli_and_report_share_one_integrity_predicate():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "accounting_report_shared", ROOT / "scripts" / "report_stage1_accounting.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.inventory_integrity_ok is inventory_integrity_ok
    for state in ("CLEAN", "AUTHORIZED_EXCEPTION", "AUTHORIZED_EXCEPTION_ABSENT"):
        assert inventory_integrity_ok({"state": state, "integrity_ok": True}) is True
    for state in ("UNEXPECTED_CHANGE", "MISSING_FILE", "READ_ERROR",
                  "EXCEPTION_LIST_CHANGED", "AUTHORIZED_EXCEPTION_MISMATCH"):
        assert inventory_integrity_ok({"state": state, "integrity_ok": False}) is False


def test_inventory_report_keeps_backward_compatible_keys(tmp_path):
    setup_tree(tmp_path)
    result = VERIFY["verify"](tmp_path)
    for key in ("checked_files", "unexpected_changes", "errors",
                "acknowledged_unrecovered_changes", "only_accepted_exception",
                "inventory_sha256", "all_hashes_match", "raw_files_checked",
                "database_files_checked", "generated_manifest_trust",
                "state", "integrity_ok", "missing_files", "read_errors",
                "acknowledged_changes", "exception_absent"):
        assert key in result, key
    assert result["generated_manifest_trust"] == "UNTRUSTED_NOT_AN_AUTHORITATIVE_INPUT"
