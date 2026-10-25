"""현재 유효 프로젝트 트리 export(M5-0, plan D5·D7): 저장된 정규형 파일 + DB 운영 값 → tar.

유효 트리 = `project_files`(마지막 apply·restore의 정규형 파일)에 현재 DB 정의를 덮어쓴 것이다 —
현장 PATCH(M5-2)로 바뀐 운영 값이 백업·드래프트에 실린다. 덮어쓰기 단위는 항목 전체다:
`devices/<id>.yaml`은 그 장비 정의, `tags/*.yaml`·`alarms/*.yaml`은 목록 안의 같은 `path`·`id` 항목.
DB에서 retire된 항목은 파일 값을 그대로 둔다(파일에서 빼는 것은 Git 정본 경로만, D1).

현장 드래프트(M5-2 P5, `export --draft`)는 같은 유효 트리 tar에 `draft-meta.json`
(`{site_rev, base_site_rev, content_hash}`, content_hash = 유효 트리 `load_tree` 값)을 곁들인다.
서명·묶음(`draft.tar`)은 site key를 가진 Updater(`pureiron-site project export`)가 만든다.

tar는 번들 빌더(`deploy/bundle/build_bundle.py` `write_project_tar`)와 같은 형식이다: 경로
UTF-8 바이트 순, PAX, mtime 0·root 소유·0644 일반 파일. 그래서 같은 트리는 같은 바이트이고, 덮어쓸
운영 값이 없으면 번들 project.tar와도 바이트가 같다(export → apply → export 동일).
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Mapping
from typing import cast

from pureiron_core.platform.db import Connection
from pureiron_core.project.ports import ProjectState, ProjectTreeError
from pureiron_core.project.stub import fetch_state
from pureiron_project.canonical import CanonicalError, Json, dump, load

STATE_FILE = "state.json"
"""`export --with-state` 디렉터리의 project_state 스냅샷(`ProjectState.to_snapshot()`)."""
TREE_FILE = "project.tar"
"""같은 디렉터리의 유효 트리. `project_files`가 비어 있으면 만들지 않는다(state만 복원)."""
DRAFT_META_FILE = "draft-meta.json"
"""`export --draft` 디렉터리의 `{site_rev, base_site_rev, content_hash}`(옆의 `project.tar`)."""
_TAR_MODE = 0o644


def tree_tar(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(files, key=lambda r: r.encode("utf-8")):
            data = files[rel]
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mtime = 0
            info.mode = _TAR_MODE
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _definitions(rows: list[Mapping[str, object]], key: str) -> dict[str, Json]:
    out: dict[str, Json] = {}
    for row in rows:
        raw = row["definition"]
        value: Json = json.loads(raw) if isinstance(raw, str) else cast(Json, raw)
        out[str(row[key])] = value
    return out


def _overlay_list(data: Json, current: Mapping[str, Json], key: str) -> Json:
    if not isinstance(data, list):
        return data
    items: list[Json] = []
    for item in data:
        ident = item.get(key) if isinstance(item, dict) else None
        items.append(current[ident] if isinstance(ident, str) and ident in current else item)
    return items


def _overlay(
    rel: str,
    data: Json,
    devices: Mapping[str, Json],
    tags: Mapping[str, Json],
    alarms: Mapping[str, Json],
) -> Json:
    directory = rel.split("/", 1)[0]
    if directory == "devices" and isinstance(data, dict):
        ident = data.get("id")
        return devices[ident] if isinstance(ident, str) and ident in devices else data
    if directory == "tags":
        return _overlay_list(data, tags, "path")
    if directory == "alarms":
        return _overlay_list(data, alarms, "id")
    return data


async def effective_files(conn: Connection) -> tuple[ProjectState, dict[str, bytes]]:
    """(project_state, 유효 트리 파일). 한 스냅샷에서 읽는다(apply 도중 값이 섞이지 않게).

    `project_files`가 비어 있으면(트리 apply 전, 예: `apply-definitions`만 쓴 스택) 파일도 없다.
    """
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        state = await fetch_state(conn)
        stored = await conn.fetch("SELECT path, content FROM project_files ORDER BY path")
        devices = await conn.fetch(
            "SELECT id, definition FROM devices WHERE retired_at IS NULL AND definition IS NOT NULL"
        )
        tags = await conn.fetch(
            "SELECT path, definition FROM tags WHERE retired_at IS NULL AND definition IS NOT NULL"
        )
        alarms = await conn.fetch(
            "SELECT alarm_id, definition FROM alarms WHERE retired_at IS NULL"
        )
    current = (
        _definitions([dict(r.items()) for r in devices], "id"),
        _definitions([dict(r.items()) for r in tags], "path"),
        _definitions([dict(r.items()) for r in alarms], "alarm_id"),
    )
    files: dict[str, bytes] = {}
    for row in stored:
        rel, content = str(row["path"]), row["content"]
        if not isinstance(content, bytes):
            raise ProjectTreeError(f"project_files.{rel} content has unexpected type")
        try:
            data = load(content.decode("utf-8"), rel)
        except (CanonicalError, UnicodeDecodeError) as exc:
            raise ProjectTreeError(f"stored project file {rel} is unreadable: {exc}") from exc
        files[rel] = dump(_overlay(rel, data, *current)).encode("utf-8")
    return state, files
