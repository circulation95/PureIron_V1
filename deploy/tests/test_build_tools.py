from __future__ import annotations

import json
from pathlib import Path

import pytest
from images import read_lock
from n_minus_1 import (
    WAIVERS,
    GateError,
    ReleaseRecord,
    Waiver,
    check_envelope,
    check_main_run,
    load_records,
    load_waivers,
    pick_previous,
    pytest_counts,
)
from repro_build import compare


def test_read_lock_ignores_comments(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    lock.write_text("# comment=ignored\nversion=1\ndigest = sha256:x\n", encoding="utf-8")
    assert read_lock(lock) == {"version": "1", "digest": "sha256:x"}


def test_repro_compare_reports_mismatches() -> None:
    first = {"core": "a", "collector": "b", "web": "c", "simulator": "d", "core-test": "e"}
    assert compare(first, dict(first)) == []
    assert compare(first, {**first, "web": "z"}) == ["web"]


def _record(directory: Path, name: str, epoch: int) -> None:
    body = {
        "name": name,
        "release": name,
        "schema": {"epoch": epoch, "app_expected": epoch, "app_max_compatible": epoch + 1},
        "images": {"core-test": {"tag": f"pureiron/core-test:{name}", "config_digest": "x"}},
    }
    (directory / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")


def test_pick_previous_epoch_release_in_numeric_order(tmp_path: Path) -> None:
    _record(tmp_path, "r0", 1)
    _record(tmp_path, "r2", 2)
    _record(tmp_path, "r10", 2)
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
    records = load_records(tmp_path)
    assert [r.name for r in records] == ["r0", "r2", "r10"]
    previous = pick_previous(records, 3)
    assert previous == ReleaseRecord("r10", "r10", 2, "pureiron/core-test:r10", "x")
    assert pick_previous(records, 2) == records[0]
    assert pick_previous(records, 1) is None


def test_waivers_are_scoped_to_previous_release(tmp_path: Path) -> None:
    path = tmp_path / "waivers.json"
    assert load_waivers(path, "r0") == []
    path.write_text(
        json.dumps({"r0": [{"test": "a.py::test_x", "reason": "global count"}]}), encoding="utf-8"
    )
    assert load_waivers(path, "r0") == [Waiver("a.py::test_x", "global count")]
    assert load_waivers(path, "r1") == []


@pytest.mark.parametrize(
    "entry",
    [
        {"test": "a.py::test_x", "reason": " "},
        {"test": "a.py", "reason": "r"},
        {"test": "a.py::", "reason": "r"},
        {"test": "a.py::test_", "reason": "r"},
        {"test": "a.py::TestX", "reason": "r"},
    ],
)
def test_waiver_needs_node_id_and_reason(tmp_path: Path, entry: dict[str, str]) -> None:
    path = tmp_path / "waivers.json"
    path.write_text(json.dumps({"r0": [entry]}), encoding="utf-8")
    with pytest.raises(GateError):
        load_waivers(path, "r0")


def test_repo_waivers_load() -> None:
    assert all(w.reason for w in load_waivers(WAIVERS, "r0"))


def test_waiver_file_shape_errors_are_gate_errors(tmp_path: Path) -> None:
    path = tmp_path / "waivers.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(GateError):
        load_waivers(path, "r0")
    path.write_text(json.dumps({"r0": {"test": "x"}}), encoding="utf-8")
    with pytest.raises(GateError):
        load_waivers(path, "r0")


def test_pytest_counts_reads_last_summary() -> None:
    out = "x.py ..s\n=== 1 passed, 9 skipped, 1 deselected in 0.69s ===\n"
    assert pytest_counts(out) == {"passed": 1, "skipped": 9, "deselected": 1}
    assert pytest_counts("=== 1 failed in 0.1s ===") == {"failed": 1}
    assert pytest_counts("=== 2 errors in 0.1s ===") == {"error": 2}
    assert pytest_counts("no summary") == {}


WAIVERS_ONE = [Waiver("a.py::test_x", "why")]


@pytest.mark.parametrize(
    ("returncode", "counts", "ok"),
    [
        (0, {"passed": 1, "skipped": 9, "deselected": 1}, True),
        (1, {"passed": 1, "failed": 1, "deselected": 1}, False),
        (0, {"skipped": 10, "deselected": 1}, False),
        (0, {"passed": 2}, False),
        (0, {"passed": 1, "deselected": 2}, False),
    ],
)
def test_check_main_run(returncode: int, counts: dict[str, int], ok: bool) -> None:
    assert (check_main_run(returncode, counts, WAIVERS_ONE) is None) is ok


@pytest.mark.parametrize(
    ("status", "ok"),
    [
        ({"cur": 2, "app_expected": 1, "app_max_compatible": 2, "checksum_ok": True}, True),
        ({"cur": 1, "app_expected": 1, "app_max_compatible": 2, "checksum_ok": True}, False),
        ({"cur": 2, "app_expected": 2, "app_max_compatible": 3, "checksum_ok": True}, False),
        ({"cur": 2, "app_expected": 1, "app_max_compatible": 1, "checksum_ok": True}, False),
        ({"cur": 2, "app_expected": 1, "app_max_compatible": 2, "checksum_ok": False}, False),
        ({}, False),
    ],
)
def test_check_envelope(status: dict[str, object], ok: bool) -> None:
    assert (check_envelope(status, previous_epoch=1, current_epoch=2) is None) is ok
