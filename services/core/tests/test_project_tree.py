"""번들 project.tar 안전 읽기·검증·결정적 tar(M5-0, plan D6) — DB 없이 확인하는 부분."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path
from typing import cast

import pytest

from pureiron_core.platform.db import Connection
from pureiron_core.project import apply
from pureiron_core.project.apply import apply_tree, load_tree, read_tar, tree_from_tar
from pureiron_core.project.export import tree_tar
from pureiron_core.project.ports import ProjectState, ProjectStateError, ProjectTreeError
from pureiron_core.project.stub import state_from_snapshot
from pureiron_project.project import load_project

ACME = Path(__file__).resolve().parents[3] / "packages" / "project-tools" / "fixtures" / "acme"


def _acme_files() -> dict[str, bytes]:
    project = load_project(ACME)
    assert project.ok, project.problems
    return {f.relpath: f.path.read_bytes() for f in project.files}


def _tar(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            tar.addfile(info, None if data is None else io.BytesIO(data))
    return buffer.getvalue()


def _file(name: str, data: bytes = b"[]\n") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def _special(name: str, kind: bytes, link: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    return info, None


def _with_acme(*extra: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    return _tar(*(_file(rel, data) for rel, data in sorted(_acme_files().items())), *extra)


REJECTED = {
    "absolute": (_with_acme(_file("/etc/passwd")), "absolute path"),
    "parent": (_with_acme(_file("tags/../../escape.yaml")), "invalid relative path"),
    "dot-prefix": (_with_acme(_file("./alarms/x.yaml")), "must be normalized"),
    "double-slash": (_with_acme(_file("alarms//x.yaml")), "must be normalized"),
    "backslash": (_with_acme(_file("alarms\\x.yaml")), "must be normalized"),
    "symlink": (_with_acme(_special("alarms/x.yaml", tarfile.SYMTYPE, "/etc")), "regular files"),
    "hardlink": (
        _with_acme(_special("alarms/x.yaml", tarfile.LNKTYPE, "project.yaml")),
        "regular files",
    ),
    "directory": (_with_acme(_special("alarms", tarfile.DIRTYPE)), "regular files"),
    "fifo": (_with_acme(_special("alarms/x.yaml", tarfile.FIFOTYPE)), "regular files"),
    "duplicate": (_with_acme(_file("project.yaml", b"name: x\n")), "duplicate path"),
    # 리뷰 L-1: 파일/디렉터리 충돌·비UTF-8 이름은 load_tree의 OSError 전에 ProjectTreeError
    "file-then-dir": (_tar(_file("tags"), _file("tags/x.yaml")), "path conflict"),
    "dir-then-file": (_with_acme(_file("devices")), "path conflict"),
    "non-utf8": (_with_acme(_file("alarms/\udcff.yaml")), "not UTF-8"),
    "not-a-tar": (b"not a tar archive" * 64, "not a valid tar"),
    "compressed": (gzip.compress(_with_acme(), mtime=0), "not a valid tar"),
}


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_unsafe_tar_is_rejected(case: str) -> None:
    data, message = REJECTED[case]
    with pytest.raises(ProjectTreeError, match=message):
        tree_from_tar(data)


def test_size_and_file_count_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _with_acme()
    monkeypatch.setattr(apply, "TREE_LIMIT_BYTES", len(data) - 1)
    with pytest.raises(ProjectTreeError, match="limit"):
        read_tar(data)
    monkeypatch.setattr(apply, "TREE_LIMIT_BYTES", len(data))
    monkeypatch.setattr(apply, "TREE_LIMIT_FILES", len(_acme_files()) - 1)
    with pytest.raises(ProjectTreeError, match="more than"):
        read_tar(data)


def test_non_project_and_invalid_content_is_rejected() -> None:
    with pytest.raises(ProjectTreeError, match=r"non-project files: README.md"):
        tree_from_tar(_with_acme(_file("README.md", b"# acme\n")))
    with pytest.raises(ProjectTreeError, match=r"non-project files: screens/.gitkeep"):
        tree_from_tar(_with_acme(_file("screens/.gitkeep", b"")))
    files = _acme_files()
    files["alarms/line1.yaml"] = b"- id: x\n  tag: no/such\n  type: above\n  setpoint: 1\n"
    with pytest.raises(ProjectTreeError, match="unknown tag"):
        load_tree(files)


def test_tree_is_canonical_and_tar_round_trip_is_byte_identical() -> None:
    """서식이 다른 입력도 정규형 바이트로 저장하고, tar → 트리 → tar가 바이트 단위로 같다."""
    canonical = load_tree(_acme_files())
    files = dict(_acme_files())
    files["project.yaml"] = b'{"schema_version": 1, "project_id": "acme", "name": "ACME Line 1",\n'
    files["project.yaml"] += b' "absorbs": {"site-a": 3}, "historian": {"retention_days": 400}}\n'
    reformatted = load_tree(files)
    assert reformatted.files == canonical.files
    assert reformatted.content_hash == canonical.content_hash == load_project(ACME).content_hash()
    data = tree_tar(canonical.files)
    again = tree_from_tar(data)
    assert again.files == canonical.files
    assert tree_tar(again.files) == data
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == sorted(canonical.files)
    assert {(m.mtime, m.uid, m.gid, m.mode, m.uname) for m in members} == {(0, 0, 0, 0o644, "")}


class _Untouchable:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"DB touched ({name}) before the tree was checked")


@pytest.mark.parametrize(
    ("project_id", "content_hash", "message"),
    [("acme", "sha256:" + "0" * 64, "content hash mismatch"), ("other", None, "project_id")],
)
async def test_hash_or_project_id_mismatch_is_rejected_before_db(
    project_id: str, content_hash: str | None, message: str
) -> None:
    tree = load_tree(_acme_files())
    with pytest.raises(ProjectTreeError, match=message):
        await apply_tree(
            cast(Connection, _Untouchable()),
            tree,
            project_id=project_id,
            version="v1",
            content_hash=content_hash or tree.content_hash,
        )


def test_malformed_state_snapshot_is_a_project_state_error() -> None:
    """리뷰 L-4: 키 누락·날짜 형식 오류는 traceback(exit 1)이 아니라 ProjectStateError(exit 4)."""
    snapshot = ProjectState("acme", "v1", "sha256:x", 3, 3, None, None, None).to_snapshot()
    assert state_from_snapshot(snapshot).site_rev == 3
    missing = {k: v for k, v in snapshot.items() if k != "site_rev"}
    with pytest.raises(ProjectStateError, match="site_rev"):
        state_from_snapshot(missing)
    with pytest.raises(ProjectStateError, match="snapshot"):
        state_from_snapshot({**snapshot, "applied_at": "yesterday"})


@pytest.mark.parametrize(
    ("version", "site_rev", "base_site_rev", "noop"),
    [
        pytest.param("v1", 3, 3, True, id="same-tree-no-drafts"),
        pytest.param("v1", 5, 3, False, id="same-tree-drafts-left"),  # K8: 재적용·base 이동
        pytest.param("v2", 3, 3, False, id="other-version"),
    ],
)
def test_already_applied_needs_same_meta_and_no_site_drafts(
    version: str, site_rev: int, base_site_rev: int, noop: bool
) -> None:
    """M5-3 K8(M5-2 리뷰 LOW-4): 같은 트리라도 드래프트가 남았으면 no-op이 아니다."""
    current = ProjectState("acme", "v1", "sha256:x", base_site_rev, site_rev, None, None, None)
    assert (
        apply.already_applied(current, project_id="acme", version=version, content_hash="sha256:x")
        is noop
    )
