"""프로젝트 적용·업데이트 전 백업·롤백 복원(plan M2 `switching`·`backed_up`·`rolling_back`, D6·D7).

`backups/g<new gen>/`:
- `db.dump` — `pg_dump -Fc`, tag_history·Timescale 내부(chunk·CAGG 물질화) 데이터 제외
- `project/` — `pureiron-core project export --with-state`: `state.json`(project_state) +
  `project.tar`(현재 유효 트리: 적용된 정규형 파일 + DB 운영 값). 트리를 아는 core만 만든다
- `project.json` — project_state 스냅샷(version·content_hash·site_rev·base). 트리 이전 형식과 같다
- `compose/` — 현재(직전) generation의 compose.yml·.env·manifest.json
- `secrets.txt` — secret 이름 목록(값은 담지 않는다)
- `COMPLETE` — 위가 모두 끝났다는 표시(마지막에 기록)

**어느 core 이미지가 무엇을 하는가.** 트리 명령(`apply --tar`·`export --with-state`·
`restore --from <dir>`)은 schema `app_expected ≥ PROJECT_TREE_EPOCH`(0013 `project_files`가 있는
epoch)인 core만 안다.
- `switching`: **새(target) 이미지**가 번들 project.tar를 적용한다. target이 트리 이전
  release(되돌림 release 등)면 메타만 기록하는 이전 명령을 쓴다.
- `backed_up`·`rolling_back`: **직전(applied) 이미지**가 백업을 만들고, 롤백 때 같은 이미지가
  그 백업을 되돌린다. 백업 형식을 쓴 이미지가 읽으므로 N-1 이미지가 모르는 형식은 생기지 않고,
  롤백은 실패한 새 이미지에 기대지 않는다(롤백 사유가 바로 그 이미지일 수 있다). 스키마는
  전진만 하므로 직전 이미지의 epoch 이상이 늘 있다.
- 한계: 직전 이미지가 트리 이전(r3 이하)이면 백업에 트리가 없어 롤백은 project_state만
  되돌린다 — 실패한 업데이트가 적용한 정의는 남는다(r3 현장은 번들 정의를 받은 적이 없다).
  첫 트리 release가 한 번 성공한 뒤부터는 정의까지 되돌린다. 이 롤백은 조용히 끝내지 않는다 —
  `restore_project`가 False를 돌려주면 Updater가 detail에 `E_DEFINITIONS_LEFT`를 붙이고 alarm을
  세운다(수동 복구는 updater README runbook).

**쓰기 mount는 신뢰하지 않는다.** `project/`는 컨테이너 uid 소유라 그 안의 항목은 컨테이너가
만든 것이다. root updater는 링크를 따르지 않고(`lstat`·`O_NOFOLLOW`) 정해진 일반 파일만 읽는다 —
심어 둔 심볼릭·하드 링크로 호스트 파일이 백업·mount 사본을 거쳐 컨테이너로 새지 않게 한다.

백업은 매번 디렉터리를 새로 만든다(재실행 멱등). 롤백은 project 트리·state만 되돌린다 — 운영
이력(사용자·ACK·감사·tag_history)과 스키마는 되돌리지 않는다. 적용·복원 명령은 모두 다시
실행해도 같다(core가 같은 트리 재적용을 no-op으로 처리한다).
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from pathlib import Path

from pureiron_bundle.manifest import Manifest
from pureiron_updater.ports import Compose, ComposeTarget
from pureiron_updater.state import atomic_write

PROJECT_TREE_EPOCH = 5
"""core가 `project_files`와 트리 명령을 아는 첫 schema epoch(0013, M5-0)."""
PROJECT_FILE = "project.json"
RESTORE_FILE = "project.restore.json"
"""롤백 때 core 컨테이너에 mount하는 `project.json` 읽기 전용 사본(컨테이너 사용자 소유 0400)."""
TREE_DIR = "project"
RESTORE_DIR = "project.restore"
"""롤백 때 mount하는 `project/` 읽기 전용 사본."""
STATE_FILE = "state.json"
TREE_FILE = "project.tar"
BUNDLE_TREE = "project/project.tar"
"""번들 안 프로젝트 트리 경로(추출 디렉터리 기준)."""
APPLY_FILE = "project.apply.tar"
COMPLETE = "COMPLETE"
_CONTAINER_PROJECT = "/run/pureiron-backup/project.json"
_CONTAINER_RESTORE_DIR = "/run/pureiron-backup/project"
_CONTAINER_EXPORT_DIR = "/run/pureiron-backup-out"
_CONTAINER_TREE = "/run/pureiron-project/project.tar"


_NOFOLLOW = 0 if sys.platform == "win32" else os.O_NOFOLLOW
"""Windows에는 없다 — 거기서는 `lstat` 사전 검사만 한다(현장은 Linux)."""


class BackupError(Exception):
    pass


def tree_aware(manifest: Manifest) -> bool:
    return manifest.product_release.schema.app_expected >= PROJECT_TREE_EPOCH


def _restrict(path: Path, mode: int, reader_uid: int | None) -> None:
    """서비스(`UMask=0027`)가 만든 root 소유 파일은 core 컨테이너 사용자(`reader_uid`, 기본 10001)가
    못 읽는다(M2d VM 실측) → 그 uid 소유로 바꾼다(secret 파일과 같은 방식, root일 때만 chown).
    core 이미지는 release마다 동결이라 core 쪽 변경으로는 이전 release로의 롤백을 고칠 수 없다."""
    if sys.platform == "win32":  # Windows chmod는 읽기 전용 속성 — 다음 실행의 unlink를 막는다
        return
    os.chmod(path, mode)
    if reader_uid is not None and os.geteuid() == 0:
        os.chown(path, reader_uid, reader_uid)


def _is_regular(path: Path) -> bool:
    """링크를 따르지 않고 일반 파일인가(없으면 False)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _read_regular(path: Path) -> bytes:
    """링크를 따르지 않고, 링크가 하나뿐인 일반 파일만 읽는다(쓰기 mount 결과물, 모듈 docstring)."""
    if not _is_regular(path):
        raise BackupError(f"{path} is not a regular file")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise BackupError(f"{path}: {exc}") from exc
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError(f"{path} is not a single-link regular file")
        return file.read()


def _check_export(out: Path) -> None:
    """`export --with-state` 결과는 정확히 state.json(+ project.tar) 일반 파일이어야 한다."""
    names = sorted(entry.name for entry in os.scandir(out))
    if STATE_FILE not in names or not set(names) <= {STATE_FILE, TREE_FILE}:
        raise BackupError(
            f"project export --with-state wrote {names}, expected {STATE_FILE}[, {TREE_FILE}]"
        )
    for name in names:
        if not _is_regular(out / name):
            raise BackupError(f"project export --with-state: {name} is not a regular file")


def _stage(source: Path, staged: Path, reader_uid: int | None) -> None:
    """컨테이너에 mount할 0400 사본. 이전 실행의 사본이 있어도 바꿔 쓴다(recover 재실행)."""
    staged.unlink(missing_ok=True)
    atomic_write(staged, _read_regular(source))
    _restrict(staged, 0o400, reader_uid)


def apply_project(
    compose: Compose,
    target: ComposeTarget,
    manifest: Manifest,
    extract_dir: Path,
    *,
    reader_uid: int | None,
) -> None:
    """`switching`: 새 이미지로 번들 프로젝트를 적용한다(정의·project_files·state, 한 트랜잭션)."""
    project = manifest.project
    meta = [
        *("--project-id", project.project_id, "--version", project.version),
        *("--content-hash", project.content_hash),
    ]
    if not tree_aware(manifest):
        compose.run_core(target, ["project", "apply", *meta])
        return
    staged = extract_dir / APPLY_FILE
    _stage(extract_dir.joinpath(*BUNDLE_TREE.split("/")), staged, reader_uid)
    compose.run_core(
        target,
        ["project", "apply", "--tar", _CONTAINER_TREE, *meta],
        mounts={staged: _CONTAINER_TREE},
    )


def make_backup(
    compose: Compose,
    current: ComposeTarget,
    backup_dir: Path,
    secrets_dir: Path,
    *,
    tree: bool,
    reader_uid: int | None,
) -> None:
    """`tree`면 직전 이미지가 `export --with-state`로 state + 유효 트리를 쓴다(아니면 state만)."""
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    (backup_dir / "compose").mkdir(parents=True)
    compose.dump_db(current, backup_dir / "db.dump")
    if tree:
        out = backup_dir / TREE_DIR
        out.mkdir()
        _restrict(out, 0o700, reader_uid)
        compose.run_core(
            current,
            ["project", "export", "--with-state", "--out", _CONTAINER_EXPORT_DIR],
            writable={out: _CONTAINER_EXPORT_DIR},
        )
        _check_export(out)
        snapshot = _read_regular(out / STATE_FILE).decode("utf-8", errors="replace")
    else:
        snapshot = compose.run_core(current, ["project", "export"])
    if not snapshot.strip().startswith("{"):
        raise BackupError(f"project export returned no JSON snapshot: {snapshot[:200]!r}")
    atomic_write(backup_dir / PROJECT_FILE, snapshot.encode())
    for source in (current.compose_file, current.env_file, current.manifest_file):
        shutil.copy2(source, backup_dir / "compose" / source.name)
    names = (
        sorted(p.name for p in secrets_dir.iterdir() if p.is_file()) if secrets_dir.is_dir() else []
    )
    atomic_write(backup_dir / "secrets.txt", "".join(f"{n}\n" for n in names).encode())
    atomic_write(backup_dir / COMPLETE, b"")


def restore_project(
    compose: Compose, target: ComposeTarget, backup_dir: Path, *, reader_uid: int | None
) -> bool:
    """롤백: 백업한 트리(있으면)와 project_state(site_rev·base 포함)를 그대로 되돌린다.

    `project/`가 있으면 그것을 만든 직전 이미지(`target`)가 트리 + state를 한 트랜잭션으로 되돌리고,
    없으면(트리 이전 백업) `project.json`의 state만 되돌린다. 둘 다 읽기 전용 사본을 mount한다.
    정의까지 되돌렸으면 True, state만 되돌렸으면 False(모듈 docstring 한계).
    """
    if not (backup_dir / COMPLETE).is_file():
        raise BackupError(f"{backup_dir} is not a complete backup")
    tree_dir = backup_dir / TREE_DIR
    if os.path.lexists(tree_dir / STATE_FILE):
        staged_dir = backup_dir / RESTORE_DIR
        shutil.rmtree(staged_dir, ignore_errors=True)
        staged_dir.mkdir()
        for name in (STATE_FILE, TREE_FILE):
            if os.path.lexists(tree_dir / name):  # 링크·일반 파일 아님은 _stage가 거부
                _stage(tree_dir / name, staged_dir / name, reader_uid)
        _restrict(staged_dir, 0o700, reader_uid)
        compose.run_core(
            target,
            ["project", "restore", "--from", _CONTAINER_RESTORE_DIR],
            mounts={staged_dir: _CONTAINER_RESTORE_DIR},
        )
        return True
    staged = backup_dir / RESTORE_FILE
    _stage(backup_dir / PROJECT_FILE, staged, reader_uid)
    compose.run_core(
        target,
        ["project", "restore", "--from", _CONTAINER_PROJECT],
        mounts={staged: _CONTAINER_PROJECT},
    )
    return False
