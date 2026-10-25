"""현장 운영 값 PATCH(M5-2, plan D1'·P1~P3): 허용 필드 병합·정규형 정책과 한 트랜잭션 적용.

허용 필드(D1'): 알람 `setpoint`·`deadband`·`on_delay_s`·`priority`, 태그 `description`·`eu`·
`record.{deadband,max_interval_s}`. 장비 연결·태그 주소·추가·삭제는 Git 정본 경로로만 바꾼다.
`None` 값 = 그 필드 제거(스키마 기본값으로), `setpoint`만은 필수라 `None`이 오류다.

저장(P2): 기존 정의(정규형) + 변경 → 스키마 검증 → `omit_defaults` → 정규형(`dump`→`load`, NFC·
JCS 숫자). 결과가 기존과 같으면 아무것도 쓰지 않는다(no-op: site_rev·definitions_rev·감사 그대로).

트랜잭션(P3): 정의 적용 advisory lock(apply·restore와 직렬화 — 행·project_state를 반대 순서로
잡아 교착하지 않게) → `project_state FOR UPDATE`(Updater 잠금 획득의 단일 UPDATE와 직렬화) →
잠금 중이면 `EditLockedError` → 대상 행 `FOR UPDATE`(없거나 retire면 `DefinitionNotFoundError`)
→ definition UPDATE → `site_rev`·`definitions_rev` += 1 → 감사 `project.patch`(같은 트랜잭션,
실패하면 전부 롤백). `definitions_rev`가 오르면 PlanKeeper가 plan·recorder·알람·쓰기 정의를
기존 경로로 다시 읽는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import cast

import asyncpg

from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSource, AuditWriter
from pureiron_core.platform.db import Connection, Database, DatabaseUnavailableError
from pureiron_core.project.definitions import APPLY_LOCK_KEY
from pureiron_core.project.ports import (
    DefinitionNotFoundError,
    EditLockedError,
    PatchInvalidError,
    PatchResult,
    ProjectStateError,
)
from pureiron_project.canonical import CanonicalError, Json, dump, load
from pureiron_project.schema import Kind, omit_defaults, validate

ALARM_FIELDS = frozenset({"setpoint", "deadband", "on_delay_s", "priority"})
TAG_FIELDS = frozenset({"description", "eu", "record"})
RECORD_FIELDS = frozenset({"deadband", "max_interval_s"})
_REQUIRED = frozenset({"setpoint"})
_RECORD = "record"
_QUERY_TIMEOUT_S = 5.0

LOCK_STATE_SQL = "SELECT site_rev, edit_lock_owner FROM project_state WHERE id FOR UPDATE"
LOCK_ALARM_SQL = (
    "SELECT definition FROM alarms WHERE alarm_id = $1 AND retired_at IS NULL FOR UPDATE"
)
LOCK_TAG_SQL = (
    "SELECT definition FROM tags"
    " WHERE path = $1 AND retired_at IS NULL AND definition IS NOT NULL FOR UPDATE"
)
UPDATE_ALARM_SQL = "UPDATE alarms SET definition = $2::jsonb WHERE alarm_id = $1"
UPDATE_TAG_SQL = "UPDATE tags SET definition = $2::jsonb WHERE path = $1"
BUMP_REVS_SQL = """
UPDATE project_state
   SET site_rev = site_rev + 1, definitions_rev = definitions_rev + 1, updated_at = now()
 WHERE id
RETURNING site_rev
"""


class PatchKind(StrEnum):
    ALARM = "alarm"
    TAG = "tag"


_FIELDS = {PatchKind.ALARM: ALARM_FIELDS, PatchKind.TAG: TAG_FIELDS}
_LIST_KIND = {PatchKind.ALARM: Kind.ALARM_LIST, PatchKind.TAG: Kind.TAG_LIST}


def _canonical(value: object, source: str) -> dict[str, Json]:
    """정규형 JSON(파일 `dump`→`load`와 같은 값 — apply가 저장하는 정의와 비교할 수 있게)."""
    try:
        data = load(dump(cast(Json, value)), source)
    except CanonicalError as exc:
        raise PatchInvalidError(str(exc)) from exc
    if not isinstance(data, dict):
        raise PatchInvalidError(f"{source}: definition is not an object")
    return data


def _check_fields(kind: PatchKind, changes: Mapping[str, object]) -> None:
    if not changes:
        raise PatchInvalidError("patch body is empty")
    unknown = sorted(set(changes) - _FIELDS[kind])
    if unknown:
        raise PatchInvalidError(f"{kind.value} fields not patchable on site: {unknown}")
    required_null = sorted(key for key in _REQUIRED & changes.keys() if changes[key] is None)
    if required_null:
        raise PatchInvalidError(f"required fields cannot be removed: {required_null}")
    if _RECORD in changes and (record := changes[_RECORD]) is not None:
        if not isinstance(record, Mapping) or not record:
            raise PatchInvalidError("record must be a non-empty object or null")
        unknown = sorted(set(cast(Mapping[str, object], record)) - RECORD_FIELDS)
        if unknown:
            raise PatchInvalidError(f"record fields not patchable on site: {unknown}")


def _apply_changes(target: dict[str, object], changes: Mapping[str, object]) -> None:
    for key, value in changes.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = value


def normalize(kind: PatchKind, definition: Mapping[str, object]) -> dict[str, Json]:
    """저장 정규형: `dump`→`load`(NFC·JCS 숫자) + 스키마 기본값 생략."""
    document = _canonical(definition, f"<{kind.value}>")
    normalized = omit_defaults(_LIST_KIND[kind], [document])
    if not isinstance(normalized, list) or not isinstance(item := normalized[0], dict):
        raise PatchInvalidError(f"{kind.value} definition is not an object")
    return item


def merge(
    kind: PatchKind, current: Mapping[str, object], changes: Mapping[str, object]
) -> dict[str, Json]:
    """기존 정의 + 허용 필드 변경 → 검증된 정규형 정의(기본값 생략). 순수 함수."""
    _check_fields(kind, changes)
    merged: dict[str, object] = dict(_canonical(current, "<current>"))
    flat = {k: v for k, v in changes.items() if k != _RECORD}
    _apply_changes(merged, flat)
    if _RECORD in changes:
        record_changes = changes[_RECORD]
        if record_changes is None:
            merged.pop(_RECORD, None)
        else:
            existing = merged.get(_RECORD)
            record = (
                dict(cast(Mapping[str, object], existing)) if isinstance(existing, dict) else {}
            )
            _apply_changes(record, cast(Mapping[str, object], record_changes))
            if record:
                merged[_RECORD] = record
            else:
                merged.pop(_RECORD, None)
    document = _canonical(merged, "<patch>")
    problems = validate(_LIST_KIND[kind], [document])
    if problems:
        raise PatchInvalidError("; ".join(p.replace("$[0]", "$", 1) for p in problems))
    return normalize(kind, document)


def _flatten(definition: Mapping[str, Json]) -> dict[str, Json]:
    """`record.deadband`처럼 한 단계 펼친 값(감사 before/after 비교용)."""
    out: dict[str, Json] = {}
    for key, value in definition.items():
        if key == _RECORD and isinstance(value, dict):
            out.update({f"{_RECORD}.{sub}": item for sub, item in value.items()})
        else:
            out[key] = value
    return out


def changed_fields(
    before: Mapping[str, Json], after: Mapping[str, Json]
) -> tuple[dict[str, Json], dict[str, Json]]:
    """바뀐 필드만(before, after). 없던·지운 필드는 None."""
    old, new = _flatten(before), _flatten(after)
    keys = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
    return {k: old.get(k) for k in keys}, {k: new.get(k) for k in keys}


def _decode(raw: object) -> dict[str, Json]:
    value: object = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ProjectStateError("stored definition is not a JSON object")
    return cast(dict[str, Json], value)


async def patch_definition(
    conn: Connection,
    audit: AuditWriter,
    kind: PatchKind,
    ident: str,
    changes: Mapping[str, object],
    *,
    username: str,
) -> PatchResult:
    """P3 한 트랜잭션. 본문 검사(422)는 DB에 닿기 전에 한다."""
    _check_fields(kind, changes)
    lock_sql, update_sql = (
        (LOCK_ALARM_SQL, UPDATE_ALARM_SQL)
        if kind is PatchKind.ALARM
        else (LOCK_TAG_SQL, UPDATE_TAG_SQL)
    )
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", APPLY_LOCK_KEY)
        state = await conn.fetchrow(LOCK_STATE_SQL, timeout=_QUERY_TIMEOUT_S)
        if state is None:
            raise ProjectStateError("project_state row missing (run `pureiron-core migrate up`)")
        if state["edit_lock_owner"] is not None:
            raise EditLockedError(f"edit lock held by {state['edit_lock_owner']!r}")
        row = await conn.fetchrow(lock_sql, ident, timeout=_QUERY_TIMEOUT_S)
        if row is None:
            raise DefinitionNotFoundError(f"{kind.value} {ident!r} not found")
        # 기본값을 적어 둔 파일에서 온 정의도 같은 정규형으로 비교한다(값이 같으면 no-op).
        before = normalize(kind, _decode(row["definition"]))
        after = merge(kind, before, changes)
        if after == before:
            return PatchResult(site_rev=int(state["site_rev"]), definition=after, changed=False)
        encoded = json.dumps(after, ensure_ascii=False, sort_keys=True, allow_nan=False)
        await conn.execute(update_sql, ident, encoded, timeout=_QUERY_TIMEOUT_S)
        site_rev = await conn.fetchval(BUMP_REVS_SQL, timeout=_QUERY_TIMEOUT_S)
        if not isinstance(site_rev, int):
            raise ProjectStateError("project_state.site_rev has unexpected type")
        old, new = changed_fields(before, after)
        key = "id" if kind is PatchKind.ALARM else "path"
        await audit.record_with(
            conn,
            AuditEntry(
                action=AuditAction.PROJECT_PATCH,
                source=AuditSource.API,
                username=username,
                target=f"{kind.value}:{ident}",
                detail={
                    "kind": kind.value,
                    key: ident,
                    "before": old,
                    "after": new,
                    "site_rev": site_rev,
                },
            ),
        )
    return PatchResult(site_rev=site_rev, definition=after, changed=True)


class PgProjectPatcher:
    """`ProjectPatchPort` 구현. DB 오류(감사 기록 실패 포함 — 트랜잭션은 이미 롤백)는
    `ProjectStateError`(API 503)로 바꾼다."""

    def __init__(self, db: Database, audit: AuditWriter) -> None:
        self._db = db
        self._audit = audit

    async def _patch(
        self, kind: PatchKind, ident: str, changes: Mapping[str, object], username: str
    ) -> PatchResult:
        _check_fields(kind, changes)
        try:
            async with self._db.connection() as conn:
                return await patch_definition(
                    conn, self._audit, kind, ident, changes, username=username
                )
        except (
            DatabaseUnavailableError,
            asyncpg.PostgresError,
            asyncpg.InterfaceError,
            OSError,
            TimeoutError,
        ) as exc:
            raise ProjectStateError(f"site-db error: {exc}") from exc

    async def patch_alarm(
        self, alarm_id: str, changes: Mapping[str, object], *, username: str
    ) -> PatchResult:
        return await self._patch(PatchKind.ALARM, alarm_id, changes, username)

    async def patch_tag(
        self, path: str, changes: Mapping[str, object], *, username: str
    ) -> PatchResult:
        return await self._patch(PatchKind.TAG, path, changes, username)
