"""롤백 시 프로젝트 복원 입력(M2d VM 실측 회귀)."""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
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
        writable: Mapping[Path, str] | None = None,
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

    def as_root() -> int:
        return 0

    def record_chown(path: str | os.PathLike[str], uid: int, gid: int) -> None:
        chowned.append((Path(path), uid, gid))

    if sys.platform != "win32":
        monkeypatch.setattr(os, "geteuid", as_root)
        monkeypatch.setattr(os, "chown", record_chown)
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


class ExportingCompose(RecordingCompose):
    """`export --with-state`가 쓰기 mount에 state + 트리를 쓰는 core 흉내(트리를 아는 이미지)."""

    def __init__(self) -> None:
        super().__init__()
        self.writable: dict[Path, str] = {}
        self.args: list[str] = []

    def run_core(
        self,
        target: ComposeTarget,
        args: Sequence[str],
        *,
        mounts: Mapping[Path, str] | None = None,
        writable: Mapping[Path, str] | None = None,
    ) -> str:
        self.mounts, self.writable, self.args = dict(mounts or {}), dict(writable or {}), list(args)
        for host in self.writable:
            (host / backup.STATE_FILE).write_text('{"site_rev": 7}', encoding="utf-8")
            (host / backup.TREE_FILE).write_bytes(b"tree")
        return ""

    def dump_db(self, target: ComposeTarget, out: Path) -> None:
        out.write_bytes(b"PGDMP")


def _current(tmp_path: Path) -> ComposeTarget:
    current = ComposeTarget(tmp_path / "g1")
    current.directory.mkdir()
    for path in (current.compose_file, current.env_file, current.manifest_file):
        path.write_text("x", encoding="utf-8")
    return current


def test_tree_backup_writes_through_a_writable_mount_then_restores_read_only(
    tmp_path: Path,
) -> None:
    compose = ExportingCompose()
    current = _current(tmp_path)
    directory = tmp_path / "backups" / "g2"
    backup.make_backup(
        cast(Compose, compose),
        current,
        directory,
        tmp_path / "secrets",
        tree=True,
        reader_uid=None,
    )
    ((out, container),) = compose.writable.items()
    assert out == directory / backup.TREE_DIR
    assert compose.args == ["project", "export", "--with-state", "--out", container]
    assert (directory / backup.PROJECT_FILE).read_bytes() == b'{"site_rev": 7}'
    assert (directory / backup.COMPLETE).is_file()

    for _ in range(2):  # recover가 롤백을 다시 돌려도 사본을 바꿔 쓴다
        backup.restore_project(cast(Compose, compose), current, directory, reader_uid=None)
    ((staged, mounted),) = compose.mounts.items()
    assert staged == directory / backup.RESTORE_DIR
    assert compose.args == ["project", "restore", "--from", mounted]
    assert (staged / backup.TREE_FILE).read_bytes() == b"tree"
    assert (staged / backup.STATE_FILE).read_bytes() == b'{"site_rev": 7}'
    if sys.platform != "win32":
        assert stat.S_IMODE(staged.stat().st_mode) == 0o700
        assert stat.S_IMODE((staged / backup.TREE_FILE).stat().st_mode) == 0o400


def test_tree_backup_without_state_is_a_backup_error(tmp_path: Path) -> None:
    class SilentCompose(ExportingCompose):
        def run_core(
            self,
            target: ComposeTarget,
            args: Sequence[str],
            *,
            mounts: Mapping[Path, str] | None = None,
            writable: Mapping[Path, str] | None = None,
        ) -> str:
            return ""

    with pytest.raises(backup.BackupError, match=r"state.json"):
        backup.make_backup(
            cast(Compose, SilentCompose()),
            _current(tmp_path),
            tmp_path / "backups" / "g2",
            tmp_path / "secrets",
            tree=True,
            reader_uid=None,
        )


# ── 리뷰 M-2: 쓰기 mount 결과물은 컨테이너 uid가 만든 것 — 링크를 따르지 않는다 ─────────────────

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlink·hard link는 POSIX에서만")


class PlantingCompose(ExportingCompose):
    """손상된 core 이미지: export 결과 자리에 호스트 파일을 가리키는 링크를 심는다."""

    def __init__(self, plant: Callable[[Path], None]) -> None:
        super().__init__()
        self.plant = plant

    def run_core(
        self,
        target: ComposeTarget,
        args: Sequence[str],
        *,
        mounts: Mapping[Path, str] | None = None,
        writable: Mapping[Path, str] | None = None,
    ) -> str:
        super().run_core(target, args, mounts=mounts, writable=writable)
        for host in self.writable:
            self.plant(host)
        return ""


def _host_secret(tmp_path: Path) -> Path:
    secret = tmp_path / "host-secret"
    secret.write_text('{"root-only": true}', encoding="utf-8")
    return secret


def _backup_with(tmp_path: Path, compose: ExportingCompose) -> Path:
    directory = tmp_path / "backups" / "g2"
    backup.make_backup(
        cast(Compose, compose),
        _current(tmp_path),
        directory,
        tmp_path / "secrets",
        tree=True,
        reader_uid=None,
    )
    return directory


@posix_only
@pytest.mark.parametrize("name", [backup.STATE_FILE, backup.TREE_FILE])
def test_backup_rejects_symlink_planted_by_the_container(tmp_path: Path, name: str) -> None:
    secret = _host_secret(tmp_path)

    def plant(out: Path) -> None:
        (out / name).unlink()
        (out / name).symlink_to(secret)

    with pytest.raises(backup.BackupError, match=re.escape(name)):
        _backup_with(tmp_path, PlantingCompose(plant))
    assert not (tmp_path / "backups" / "g2" / backup.COMPLETE).exists()


@posix_only
def test_backup_rejects_hard_link_planted_by_the_container(tmp_path: Path) -> None:
    secret = _host_secret(tmp_path)

    def plant(out: Path) -> None:
        (out / backup.STATE_FILE).unlink()
        os.link(secret, out / backup.STATE_FILE)

    with pytest.raises(backup.BackupError, match="single-link"):
        _backup_with(tmp_path, PlantingCompose(plant))


def test_backup_rejects_unexpected_export_entries(tmp_path: Path) -> None:
    def plant(out: Path) -> None:
        (out / "extra").mkdir()

    with pytest.raises(backup.BackupError, match=r"expected state\.json"):
        _backup_with(tmp_path, PlantingCompose(plant))


@posix_only
def test_restore_does_not_follow_a_symlink_in_the_backup_tree(tmp_path: Path) -> None:
    """백업 뒤 트리 항목이 링크로 바뀌어 있어도 링크 대상을 사본으로 만들어 mount하지 않는다."""
    compose = ExportingCompose()
    directory = _backup_with(tmp_path, compose)
    tree = directory / backup.TREE_DIR / backup.TREE_FILE
    tree.unlink()
    tree.symlink_to(_host_secret(tmp_path))
    with pytest.raises(backup.BackupError, match="not a regular file"):
        backup.restore_project(
            cast(Compose, compose), ComposeTarget(tmp_path / "g1"), directory, reader_uid=None
        )
    assert not (directory / backup.RESTORE_DIR / backup.TREE_FILE).exists()
