"""장비·태그 정의 저장(M3a 최소 apply 경로). raw SQL만(No ORM).

`apply_definitions`는 검증된 프로젝트 트리(`pureiron_project.load_project`)의 장비·태그를
path 기준으로 upsert 하고, 빠진 것은 `retired_at`만 표시한다(ADR-021: 같은 path는 같은 id로
되살아나고, 다른 path에 id를 재사용하지 않는다). 실제로 바뀐 것이 있을 때만
`definitions_rev`(= plan_rev)를 올린다.
정식 project apply(edit lock·site_rev, M5)가 이 함수를 감싼다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import cast

import asyncpg

from pureiron_core.platform.db import Connection, Database, DatabaseUnavailableError
from pureiron_core.project.ports import (
    Definitions,
    DefinitionsUnavailableError,
    DeviceDef,
    ProjectStateError,
    TagDef,
)
from pureiron_project.project import Project
from pureiron_project.schema import Kind

# apply-definitions끼리 직렬화한다(동시 실행 시 rev 계산·retire가 섞이지 않게). 'PIRD'.
_APPLY_LOCK_KEY = 0x50495244
# core 백그라운드 조회 제한 시간(half-open DB 연결에서 plan 갱신이 멈추지 않게).
_QUERY_TIMEOUT_S = 5.0

_UPSERT_DEVICES = """
WITH up AS (
    INSERT INTO devices (id, protocol, definition)
    SELECT d.id, d.protocol, d.definition::jsonb
      FROM unnest($1::text[], $2::text[], $3::text[]) AS d(id, protocol, definition)
    ON CONFLICT (id) DO UPDATE
       SET protocol = EXCLUDED.protocol, definition = EXCLUDED.definition, retired_at = NULL
     WHERE devices.definition IS DISTINCT FROM EXCLUDED.definition
        OR devices.protocol IS DISTINCT FROM EXCLUDED.protocol
        OR devices.retired_at IS NOT NULL
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted) AS added, count(*) FILTER (WHERE NOT inserted) AS updated
  FROM up
"""

# 참고: INSERT … ON CONFLICT는 충돌한 행에도 serial 값을 하나씩 소모한다(id에 빈 번호가 생길 뿐
# 정확성 문제는 없다). 대규모 반복 apply가 문제가 되면 M5에서 신규 path만 INSERT 하도록 나눈다.
_UPSERT_TAGS = """
WITH up AS (
    INSERT INTO tags (path, device_id, definition)
    SELECT t.path, t.device_id, t.definition::jsonb
      FROM unnest($1::text[], $2::text[], $3::text[]) AS t(path, device_id, definition)
    ON CONFLICT (path) DO UPDATE
       SET device_id = EXCLUDED.device_id, definition = EXCLUDED.definition, retired_at = NULL
     WHERE tags.definition IS DISTINCT FROM EXCLUDED.definition
        OR tags.device_id IS DISTINCT FROM EXCLUDED.device_id
        OR tags.retired_at IS NOT NULL
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted) AS added, count(*) FILTER (WHERE NOT inserted) AS updated
  FROM up
"""

_RETIRE_TAGS = """
UPDATE tags SET retired_at = now()
 WHERE retired_at IS NULL AND NOT (path = ANY($1::text[]))
"""

_RETIRE_DEVICES = """
UPDATE devices SET retired_at = now()
 WHERE retired_at IS NULL AND NOT (id = ANY($1::text[]))
"""


@dataclass(frozen=True, slots=True)
class ApplyResult:
    rev: int
    changed: bool
    devices_added: int
    devices_updated: int
    devices_retired: int
    tags_added: int
    tags_updated: int
    tags_retired: int

    def as_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Rows:
    device_ids: list[str]
    device_protocols: list[str]
    device_defs: list[str]
    tag_paths: list[str]
    tag_devices: list[str]
    tag_defs: list[str]


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _rows(project: Project) -> _Rows:
    if not project.ok:
        raise ProjectStateError("project has problems: " + "; ".join(project.problems))
    rows = _Rows([], [], [], [], [], [])
    for f in sorted(project.files, key=lambda f: f.relpath):
        if f.kind is Kind.DEVICE and isinstance(f.data, dict):
            rows.device_ids.append(str(f.data["id"]))
            rows.device_protocols.append(str(f.data["protocol"]))
            rows.device_defs.append(_encode(f.data))
        elif f.kind is Kind.TAG_LIST and isinstance(f.data, list):
            for tag in f.data:
                if isinstance(tag, dict):
                    rows.tag_paths.append(str(tag["path"]))
                    rows.tag_devices.append(str(tag["device"]))
                    rows.tag_defs.append(_encode(tag))
    return rows


def _count(result: str) -> int:
    """`UPDATE n` 상태 문자열 → n."""
    return int(result.rsplit(" ", 1)[-1])


async def apply_definitions(conn: Connection, project: Project) -> ApplyResult:
    """검증된 프로젝트의 장비·태그 정의를 한 트랜잭션으로 반영한다."""
    rows = _rows(project)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _APPLY_LOCK_KEY)
        devices = await conn.fetchrow(
            _UPSERT_DEVICES, rows.device_ids, rows.device_protocols, rows.device_defs
        )
        tags = await conn.fetchrow(_UPSERT_TAGS, rows.tag_paths, rows.tag_devices, rows.tag_defs)
        tags_retired = _count(await conn.execute(_RETIRE_TAGS, rows.tag_paths))
        devices_retired = _count(await conn.execute(_RETIRE_DEVICES, rows.device_ids))
        if devices is None or tags is None:  # 집계 SELECT는 항상 한 행
            raise ProjectStateError("definition upsert returned no summary row")
        counts = (
            int(devices["added"]),
            int(devices["updated"]),
            devices_retired,
            int(tags["added"]),
            int(tags["updated"]),
            tags_retired,
        )
        changed = any(counts)
        if changed:
            rev = await conn.fetchval(
                "UPDATE project_state SET definitions_rev = definitions_rev + 1, updated_at = now()"
                " WHERE id RETURNING definitions_rev"
            )
        else:
            rev = await conn.fetchval("SELECT definitions_rev FROM project_state WHERE id")
        if not isinstance(rev, int):  # 트랜잭션 안에서 실패시켜 upsert·retire도 되돌린다
            raise ProjectStateError("project_state row missing (run `pureiron-core migrate up`)")
    return ApplyResult(rev, changed, *counts)


def _decode(raw: object) -> Mapping[str, object]:
    value: object = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise DefinitionsUnavailableError("definition is not a JSON object")
    return cast("dict[str, object]", value)


class PgDefinitionsSource:
    """활성 정의를 site-db에서 읽는다. DB·스키마 문제는 `DefinitionsUnavailableError`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def rev(self) -> int:
        try:
            async with self._db.connection() as conn:
                rev = await conn.fetchval(
                    "SELECT definitions_rev FROM project_state WHERE id", timeout=_QUERY_TIMEOUT_S
                )
        except (DatabaseUnavailableError, asyncpg.PostgresError, OSError, TimeoutError) as exc:
            raise DefinitionsUnavailableError(str(exc)) from exc
        if not isinstance(rev, int):
            raise DefinitionsUnavailableError("project_state row missing")
        return rev

    async def load(self) -> Definitions:
        try:
            async with self._db.connection() as conn:
                return await _load(conn)
        except (DatabaseUnavailableError, asyncpg.PostgresError, OSError, TimeoutError) as exc:
            raise DefinitionsUnavailableError(str(exc)) from exc


async def _load(conn: Connection) -> Definitions:
    # rev와 행을 같은 스냅샷에서 읽는다(apply 도중에 섞이지 않게).
    async with conn.transaction(isolation="repeatable_read", readonly=True):
        rev = await conn.fetchval(
            "SELECT definitions_rev FROM project_state WHERE id", timeout=_QUERY_TIMEOUT_S
        )
        device_rows = await conn.fetch(
            "SELECT id, definition FROM devices"
            " WHERE retired_at IS NULL AND definition IS NOT NULL ORDER BY id",
            timeout=_QUERY_TIMEOUT_S,
        )
        tag_rows = await conn.fetch(
            "SELECT id, path, device_id, definition FROM tags"
            " WHERE retired_at IS NULL AND definition IS NOT NULL AND device_id IS NOT NULL"
            " ORDER BY id",
            timeout=_QUERY_TIMEOUT_S,
        )
    if not isinstance(rev, int):
        raise DefinitionsUnavailableError("project_state row missing")
    return Definitions(
        rev=rev,
        devices=tuple(DeviceDef(str(r["id"]), _decode(r["definition"])) for r in device_rows),
        tags=tuple(
            TagDef(int(r["id"]), str(r["path"]), str(r["device_id"]), _decode(r["definition"]))
            for r in tag_rows
        ),
    )
