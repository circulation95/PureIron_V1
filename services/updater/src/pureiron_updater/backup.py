"""업데이트 전 백업과 롤백 시 프로젝트 복원(plan M2 `backed_up`·`rolling_back`, ADR-024).

`backups/g<new gen>/`:
- `db.dump` — `pg_dump -Fc`, tag_history·Timescale 내부(chunk·CAGG 물질화) 데이터 제외
- `project.json` — `pureiron-core project export`(project_state: version·content_hash·site_rev·base)
- `compose/` — 현재(직전) generation의 compose.yml·.env·manifest.json
- `secrets.txt` — secret 이름 목록(값은 담지 않는다)
- `COMPLETE` — 위가 모두 끝났다는 표시(마지막에 기록)

백업은 매번 디렉터리를 새로 만든다(재실행 멱등). 롤백은 `project.json`만 되돌린다 — 운영 이력
(사용자·ACK·감사·tag_history)과 스키마는 되돌리지 않는다.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pureiron_updater.ports import Compose, ComposeTarget
from pureiron_updater.state import atomic_write

PROJECT_FILE = "project.json"
COMPLETE = "COMPLETE"
_CONTAINER_PROJECT = "/run/pureiron-backup/project.json"


class BackupError(Exception):
    pass


def make_backup(
    compose: Compose, current: ComposeTarget, backup_dir: Path, secrets_dir: Path
) -> None:
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    (backup_dir / "compose").mkdir(parents=True)
    compose.dump_db(current, backup_dir / "db.dump")
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


def restore_project(compose: Compose, target: ComposeTarget, backup_dir: Path) -> None:
    """롤백: 백업한 project_state(site_rev·base 포함)를 그대로 되돌린다(site_rev 리셋 없음)."""
    if not (backup_dir / COMPLETE).is_file():
        raise BackupError(f"{backup_dir} is not a complete backup")
    compose.run_core(
        target,
        ["project", "restore", "--from", _CONTAINER_PROJECT],
        mounts={backup_dir / PROJECT_FILE: _CONTAINER_PROJECT},
    )
