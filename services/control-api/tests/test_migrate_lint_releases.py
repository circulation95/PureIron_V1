"""migrate lint의 release 기록 경로는 카탈로그별이다(M4-3 G10, M4-0 보고서 5-#1).

- core 카탈로그(기본 `deploy/releases`)는 지금과 같다 — 하위 `central/` 기록을 읽지 않는다.
- control-api 카탈로그(`pureiron-control migrate lint`)는 `deploy/releases/central`만 본다 —
  현장 release 기록(core epoch 5)이 control-db epoch를 "release됨"으로 만들지 않는다.
- 기록이 하나도 없으면(지금 V1: 중앙 release 기록은 M4 마감 r5에서 처음 생긴다) base의 모든 epoch를
  release된 것으로 본다(core와 같은 엄격한 쪽 — epoch 항목 변경 금지).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pureiron_control.cli import CENTRAL_RELEASES_DIR
from pureiron_control.cli import main as control_cli
from pureiron_core.platform.migrate import EXIT_LINT, EXIT_OK
from pureiron_core.platform.migrate_lint import RELEASES_DIR, lint_directory

FILES = {
    "0001_base.sql": "CREATE TABLE t (id int PRIMARY KEY);",
    "0002_two.sql": "CREATE TABLE two (x int);",
    "0003_three.sql": "CREATE TABLE three (x int);",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _write(directory: Path, names: list[str], epochs: dict[int, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(FILES[name], encoding="utf-8", newline="\n")
    lines = ["epochs:"] + [f"  {epoch}: {name}" for epoch, name in sorted(epochs.items())]
    (directory / "epochs.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _record(repo: Path, relative: str, name: str, app_expected: int) -> Path:
    path = repo / relative / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": {"app_expected": app_expected}}), encoding="utf-8")
    return path


def _repo(tmp_path: Path, *, site: int | None, central: int | None) -> Path:
    """base: epoch 1 = 0001, epoch 2 = 0002. 그 뒤 작업 트리에서 epoch 2를 0003으로 늘린다."""
    repo = tmp_path / "repo"
    directory = repo / "migrations"
    _write(directory, ["0001_base.sql", "0002_two.sql"], {1: "0001_base.sql", 2: "0002_two.sql"})
    _git(repo.parent, "init", "-q", "-b", "master", str(repo))
    if site is not None:
        _record(repo, RELEASES_DIR, "r4", site)
    if central is not None:
        _record(repo, CENTRAL_RELEASES_DIR, "c0", central)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _write(directory, ["0003_three.sql"], {1: "0001_base.sql", 2: "0003_three.sql"})
    return directory


def _messages(directory: Path, releases_dir: str) -> list[str]:
    report = lint_directory(directory, base_ref="master", releases_dir=releases_dir)
    return [issue.message for issue in report.issues]


def test_central_path_constant_matches_release_record() -> None:
    assert f"{RELEASES_DIR}/central" == CENTRAL_RELEASES_DIR


def test_central_catalog_uses_only_central_records(tmp_path: Path) -> None:
    """현장 기록이 epoch 5여도 중앙 기록(epoch 1) 기준이면 epoch 2는 아직 release 전이다."""
    directory = _repo(tmp_path, site=5, central=1)
    assert _messages(directory, CENTRAL_RELEASES_DIR) == []


def test_site_records_do_not_release_central_epochs(tmp_path: Path) -> None:
    """중앙 기록이 없으면 현장 기록(epoch 5)은 보지 않고 모든 epoch를 release된 것으로 본다."""
    directory = _repo(tmp_path, site=1, central=None)
    messages = _messages(directory, CENTRAL_RELEASES_DIR)
    assert any("released epoch 2" in m for m in messages), messages


def test_core_default_ignores_central_subdirectory(tmp_path: Path) -> None:
    """기본 인자(core)는 기존 동작 그대로: `deploy/releases/*.json`만, `central/`은 안 본다."""
    released_by_central_only = _repo(tmp_path / "a", site=None, central=1)
    messages = _messages(released_by_central_only, RELEASES_DIR)
    assert any("released epoch 2" in m for m in messages), messages
    default_call = lint_directory(released_by_central_only, base_ref="master")
    assert [i.message for i in default_call.issues] == messages
    released_by_site = _repo(tmp_path / "b", site=1, central=5)
    assert _messages(released_by_site, RELEASES_DIR) == []


def test_central_records_may_not_be_modified_or_deleted(tmp_path: Path) -> None:
    directory = _repo(tmp_path, site=None, central=1)
    record = directory.parent / CENTRAL_RELEASES_DIR / "c0.json"
    record.write_text(json.dumps({"schema": {"app_expected": 0}}), encoding="utf-8")
    messages = _messages(directory, CENTRAL_RELEASES_DIR)
    assert any("release record modified" in m for m in messages), messages
    record.unlink()
    messages = _messages(directory, CENTRAL_RELEASES_DIR)
    assert any("release record deleted" in m for m in messages), messages


@pytest.mark.parametrize(("central", "code"), [(1, EXIT_OK), (None, EXIT_LINT)])
def test_cli_lint_reads_central_records(tmp_path: Path, central: int | None, code: int) -> None:
    """`pureiron-control migrate lint`가 실제로 중앙 경로를 넘긴다(현장 기록 epoch 5는 무관)."""
    directory = _repo(tmp_path, site=5, central=central)
    assert control_cli(["migrate", "lint", str(directory), "--base-ref", "master"]) == code
