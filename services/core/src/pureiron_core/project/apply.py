"""번들 프로젝트 적용·백업 트리 복원(M5-0, plan D5·D6·D7). raw SQL만(No ORM).

`apply_tree`(= `pureiron-core project apply --tar`):
1. tar 안전 읽기(`read_tar`) — 정규화된 상대 경로의 일반 파일만. 절대 경로·`..`·링크·디렉터리·장치·
   중복 경로·압축 tar·크기 상한 초과는 거부한다. 디스크에 풀지 않고 메모리에서 읽는다.
2. `load_project`로 검증, 프로젝트 파일이 아닌 항목이 있으면 거부, 다시 계산한 content_hash와
   project_id가 번들 manifest 값과 같아야 한다.
3. 한 트랜잭션: 정의(`apply_definitions`) + `project_files` 전체 교체 + `project_state` 메타
   (`base_site_rev = site_rev`, 드래프트 게이트 기준점 ADR-025).

1·2에서 실패하면 DB에 닿지 않고, 3에서 실패하면 트랜잭션이 전부 되돌린다 — 어느 쪽이든 아무것도
바꾸지 않는다. 같은 project_id·version·hash와 같은 파일을 다시 적용하면 아무것도 쓰지 않는다
(Updater `switching` recover 재실행에 안전). 단 현장 드래프트가 남아 있으면(`site_rev ≠
base_site_rev`) 같은 트리라도 정의를 다시 적용하고 base를 옮긴다(M5-3 K8) — 적용까지 온 번들은
드래프트 게이트를 통과했으므로(드래프트 흡수 완료) 트리 정의가 현장 값을 대신한다. 첫 적용 뒤에는
`site_rev == base_site_rev`라 recover 재실행은 그대로 no-op이다.

`restore_tree`(= `project restore --from <dir>`, 롤백 B5)는 백업한 현재 유효 트리를 같은
방식으로 적용하되 project_state는 백업 스냅샷 그대로 쓴다(site_rev·base 포함, 리셋 없음).
백업 트리는 운영 값을 덮어쓴 트리라 content_hash가 스냅샷 값과 다를 수 있어 hash는 비교하지
않는다.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pureiron_core.platform.db import Connection
from pureiron_core.project.definitions import APPLY_LOCK_KEY, ApplyResult, apply_definitions
from pureiron_core.project.ports import ProjectState, ProjectTreeError
from pureiron_core.project.stub import fetch_state, write_state
from pureiron_project.canonical import dump
from pureiron_project.project import (
    PROJECT_FILE,
    Project,
    RelPathError,
    load_project,
    normalize_relpath,
)

# 번들 project.tar 상한. 2.2 MB legacy 태그 파일을 변환한 트리도 넉넉히 들어가고, 메모리에서
# 읽으므로 core 컨테이너 메모리를 지키는 값이다.
TREE_LIMIT_BYTES = 64 * 1024 * 1024
TREE_LIMIT_FILES = 10_000

_APPLY_META = """
UPDATE project_state
   SET project_id = $1, version = $2, content_hash = $3,
       base_site_rev = site_rev, applied_at = now(), updated_at = now()
 WHERE id
"""

_REPLACE_FILES = """
INSERT INTO project_files (path, content, sha256)
SELECT f.path, f.content, f.sha256
  FROM unnest($1::text[], $2::bytea[], $3::text[]) AS f(path, content, sha256)
"""


@dataclass(frozen=True, slots=True)
class ProjectTree:
    """검증된 프로젝트 트리. `files` = 정규형 바이트(경로 → 내용), `project_files`에 그대로 둔다."""

    project: Project
    files: Mapping[str, bytes]

    @property
    def content_hash(self) -> str:
        return self.project.content_hash()

    @property
    def project_id(self) -> object:
        for f in self.project.files:
            if f.relpath == PROJECT_FILE and isinstance(f.data, dict):
                return f.data.get("project_id")
        return None

    def digests(self) -> dict[str, str]:
        return {rel: hashlib.sha256(content).hexdigest() for rel, content in self.files.items()}


@dataclass(frozen=True, slots=True)
class TreeApplyResult:
    state: ProjectState
    changed: bool
    """무엇이든 썼는지. 같은 트리 재적용(no-op)이면 False."""
    files_changed: bool
    definitions: ApplyResult | None
    """정의 적용 결과. no-op이거나 트리 없는 복원이면 None."""

    def as_json(self) -> dict[str, object]:
        return {
            "state": self.state.to_snapshot(),
            "changed": self.changed,
            "files_changed": self.files_changed,
            "definitions": None if self.definitions is None else self.definitions.as_json(),
        }


def read_tar(data: bytes) -> dict[str, bytes]:
    """tar 바이트 → {정규 상대 경로: 내용}. 안전하지 않은 항목이 하나라도 있으면 거부한다."""
    if len(data) > TREE_LIMIT_BYTES:
        raise ProjectTreeError(f"project tar is {len(data)} bytes (limit {TREE_LIMIT_BYTES})")
    files: dict[str, bytes] = {}
    dirs: set[str] = set()
    try:
        # "r:" = 압축 없는 tar만(압축 폭탄 방지). 크기 상한은 위에서 원본 바이트로 이미 건다.
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for member in tar:
                rel = _member_path(member, files, dirs)  # 내용을 읽기 전에 종류·경로부터 거른다
                files[rel] = _member_content(tar, member)
    except tarfile.TarError as exc:
        raise ProjectTreeError(f"project tar is not a valid tar archive: {exc}") from exc
    return files


def _member_path(member: tarfile.TarInfo, seen: Mapping[str, bytes], dirs: set[str]) -> str:
    """검사를 통과한 정규 경로. `dirs`(앞 파일들의 상위 디렉터리)에 이 경로의 상위를 더한다."""
    name = member.name
    if not member.isreg():
        raise ProjectTreeError(f"project tar entry {name!r}: only regular files are allowed")
    try:
        name.encode("utf-8")  # tarfile은 비UTF-8 바이트를 surrogate로 남긴다
        rel = normalize_relpath(name)
    except UnicodeEncodeError:
        raise ProjectTreeError(f"project tar entry {name!r}: name is not UTF-8") from None
    except RelPathError as exc:
        raise ProjectTreeError(f"project tar entry {name!r}: {exc}") from None
    # `./a`·`a//b`·역슬래시처럼 정규화하면 바뀌는 이름도 거부한다 — 같은 파일의 두 이름을 막는다.
    if rel != name:
        raise ProjectTreeError(f"project tar entry {name!r}: path must be normalized ({rel!r})")
    if rel in seen:
        raise ProjectTreeError(f"project tar entry {name!r}: duplicate path")
    # `a`와 `a/b`: 같은 이름이 파일이면서 디렉터리일 수 없다(load_tree가 디스크에 쓰다 깨지기 전에)
    parents = ["/".join(rel.split("/")[:i]) for i in range(1, rel.count("/") + 1)]
    if rel in dirs or any(parent in seen for parent in parents):
        raise ProjectTreeError(f"project tar entry {name!r}: file/directory path conflict")
    if len(seen) >= TREE_LIMIT_FILES:
        raise ProjectTreeError(f"project tar has more than {TREE_LIMIT_FILES} files")
    dirs.update(parents)
    return rel


def _member_content(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    handle = tar.extractfile(member)
    if handle is None:
        raise ProjectTreeError(f"project tar entry {member.name!r}: unreadable")
    with handle:
        return handle.read()


def load_tree(files: Mapping[str, bytes]) -> ProjectTree:
    """파일 집합을 임시 디렉터리에 써서 `load_project`로 검증한다(project-tools와 같은 규칙)."""
    with tempfile.TemporaryDirectory(prefix="pureiron-project-") as tmp:
        root = Path(tmp)
        for rel, content in files.items():
            path = root.joinpath(*rel.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        project = load_project(root)
    if not project.ok:
        raise ProjectTreeError("project is invalid: " + "; ".join(project.problems))
    known = {f.relpath for f in project.files}
    # load_project가 무시하는 파일(README 등)도 저장·export 대상이 아니므로 섞여 오면 거부한다.
    unknown = sorted(rel for rel in files if rel not in known)
    if unknown:
        raise ProjectTreeError(f"project tar has non-project files: {', '.join(unknown)}")
    canonical = {f.relpath: dump(f.data).encode("utf-8") for f in project.files}
    return ProjectTree(project=project, files=canonical)


def tree_from_tar(data: bytes) -> ProjectTree:
    return load_tree(read_tar(data))


async def _stored_digests(conn: Connection) -> dict[str, str]:
    rows = await conn.fetch("SELECT path, sha256 FROM project_files")
    return {str(r["path"]): str(r["sha256"]) for r in rows}


async def _replace_files(conn: Connection, tree: ProjectTree) -> bool:
    """`project_files`를 트리로 교체한다. 이미 같으면 쓰지 않고 False."""
    digests = tree.digests()
    if await _stored_digests(conn) == digests:
        return False
    paths = sorted(tree.files)
    await conn.execute("DELETE FROM project_files")
    await conn.execute(
        _REPLACE_FILES, paths, [tree.files[p] for p in paths], [digests[p] for p in paths]
    )
    return True


def already_applied(
    current: ProjectState, *, project_id: str, version: str, content_hash: str
) -> bool:
    """no-op 후보: 같은 project_id·version·hash이고 남은 현장 드래프트가 없다(K8). 저장 파일
    digest 비교는 호출자가 이어서 한다."""
    same_meta = (current.project_id, current.version, current.content_hash) == (
        project_id,
        version,
        content_hash,
    )
    return same_meta and current.site_rev == current.base_site_rev


async def apply_tree(
    conn: Connection, tree: ProjectTree, *, project_id: str, version: str, content_hash: str
) -> TreeApplyResult:
    """번들 트리 적용(위 3단계). hash·project_id가 manifest와 다르면 DB에 닿기 전에 거부한다."""
    if tree.content_hash != content_hash:
        raise ProjectTreeError(
            f"content hash mismatch: tree {tree.content_hash}, expected {content_hash}"
        )
    if tree.project_id != project_id:
        raise ProjectTreeError(
            f"project_id mismatch: tree {tree.project_id!r}, expected {project_id!r}"
        )
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", APPLY_LOCK_KEY)
        current = await fetch_state(conn)
        if (
            already_applied(
                current, project_id=project_id, version=version, content_hash=content_hash
            )
            and await _stored_digests(conn) == tree.digests()
        ):
            return TreeApplyResult(current, changed=False, files_changed=False, definitions=None)
        definitions = await apply_definitions(conn, tree.project)
        files_changed = await _replace_files(conn, tree)
        await conn.execute(_APPLY_META, project_id, version, content_hash)
        state = await fetch_state(conn)
    return TreeApplyResult(
        state, changed=True, files_changed=files_changed, definitions=definitions
    )


async def restore_tree(
    conn: Connection, tree: ProjectTree | None, state: ProjectState
) -> TreeApplyResult:
    """백업 복원: 트리(있으면) 재적용 + project_state 스냅샷, 한 트랜잭션. 다시 실행해도 같다."""
    if tree is not None and state.project_id is not None and tree.project_id != state.project_id:
        raise ProjectTreeError(
            f"backup tree project_id {tree.project_id!r} != state {state.project_id!r}"
        )
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", APPLY_LOCK_KEY)
        definitions = None if tree is None else await apply_definitions(conn, tree.project)
        files_changed = tree is not None and await _replace_files(conn, tree)
        await write_state(conn, state)
        restored = await fetch_state(conn)
    return TreeApplyResult(
        restored, changed=True, files_changed=files_changed, definitions=definitions
    )
