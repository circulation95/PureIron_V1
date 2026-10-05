"""롤백 시 프로젝트 복원 입력(M2d VM 실측 회귀)."""

from __future__ import annotations

import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from pureiron_updater import backup
from pureiron_updater.ports import Compose, ComposeTarget


class RecordingCompose:
    def __init__(self) -> None:
        self.mounts: dict[Path, str] = {}

    def run_core(
        self,
        target: ComposeTarget,
        args: Sequence[str],
        *,
        mounts: Mapping[Path, str] | None = None,
    ) -> str:
        self.mounts = dict(mounts or {})
        return ""


def _complete_backup(tmp_path: Path) -> Path:
    directory = tmp_path / "g2"
    directory.mkdir()
    (directory / backup.PROJECT_FILE).write_bytes(b'{"site_rev": 3}')
    (directory / backup.COMPLETE).write_bytes(b"")
    return directory


def test_restore_mounts_reader_owned_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """서비스(UMask 0027)가 만든 root 0640 백업은 컨테이너 uid가 못 읽는다 → 그 uid 소유 사본."""
    directory = _complete_backup(tmp_path)
    chowned: list[tuple[Path, int, int]] = []
    if sys.platform != "win32":
        monkeypatch.setattr(backup.os, "geteuid", lambda: 0)
        monkeypatch.setattr(backup.os, "chown", lambda p, u, g: chowned.append((Path(p), u, g)))
    compose = RecordingCompose()
    target = ComposeTarget(tmp_path / "g1")
    backup.restore_project(cast(Compose, compose), target, directory, reader_uid=10001)

    ((host, container),) = compose.mounts.items()
    assert container == "/run/pureiron-backup/project.json"
    assert host == directory / backup.RESTORE_FILE
    assert host.read_bytes() == b'{"site_rev": 3}'
    if sys.platform != "win32":
        assert stat.S_IMODE(host.stat().st_mode) == 0o400
        assert chowned == [(host, 10001, 10001)]


def test_restore_rerun_replaces_copy(tmp_path: Path) -> None:
    """crash 뒤 recover가 롤백을 다시 돌려도(읽기 전용 사본이 이미 있어도) 실패하지 않는다."""
    directory = _complete_backup(tmp_path)
    compose = RecordingCompose()
    target = ComposeTarget(tmp_path / "g1")
    for _ in range(2):
        backup.restore_project(cast(Compose, compose), target, directory, reader_uid=None)
    assert (directory / backup.RESTORE_FILE).read_bytes() == b'{"site_rev": 3}'
