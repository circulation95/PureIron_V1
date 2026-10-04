"""`project_state` 단일 행 저장소(M1 골격). 잠금은 단일 UPDATE … RETURNING으로 경쟁 없이 잡는다."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pureiron_core.platform.db import Connection, Database
from pureiron_core.project.ports import EditLockConflictError, ProjectState, ProjectStateError

_COLUMNS = (
    "project_id, version, content_hash, base_site_rev, site_rev, "
    "edit_lock_owner, edit_lock_gen, applied_at"
)


def _row_to_state(row: Mapping[str, object]) -> ProjectState:
    def text(key: str) -> str | None:
        value = row[key]
        if value is not None and not isinstance(value, str):
            raise ProjectStateError(f"project_state.{key} has unexpected type")
        return value

    def integer(key: str) -> int | None:
        value = row[key]
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ProjectStateError(f"project_state.{key} has unexpected type")
        return value

    applied_at = row["applied_at"]
    if applied_at is not None and not isinstance(applied_at, datetime):
        raise ProjectStateError("project_state.applied_at has unexpected type")
    return ProjectState(
        project_id=text("project_id"),
        version=text("version"),
        content_hash=text("content_hash"),
        base_site_rev=integer("base_site_rev") or 0,
        site_rev=integer("site_rev") or 0,
        edit_lock_owner=text("edit_lock_owner"),
        edit_lock_gen=integer("edit_lock_gen"),
        applied_at=applied_at,
    )


async def _fetch(conn: Connection) -> ProjectState:
    row = await conn.fetchrow(f"SELECT {_COLUMNS} FROM project_state WHERE id")
    if row is None:
        raise ProjectStateError("project_state row missing (run `pureiron-core migrate up`)")
    return _row_to_state(dict(row.items()))


class PgProjectStateStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self) -> ProjectState:
        async with self._db.connection() as conn:
            return await _fetch(conn)

    async def acquire_edit_lock(self, owner: str, generation: int) -> int:
        async with self._db.connection() as conn:
            site_rev = await conn.fetchval(
                """
                UPDATE project_state
                   SET edit_lock_owner = $1, edit_lock_gen = $2, updated_at = now()
                 WHERE id
                   AND (edit_lock_owner IS NULL OR (edit_lock_owner = $1 AND edit_lock_gen = $2))
                RETURNING site_rev
                """,
                owner,
                generation,
            )
            if site_rev is None:
                raise EditLockConflictError((await _fetch(conn)).edit_lock_owner)
            if not isinstance(site_rev, int):
                raise ProjectStateError("project_state.site_rev has unexpected type")
            return site_rev

    async def release_edit_lock(self, owner: str) -> None:
        async with self._db.connection() as conn:
            state = await conn.fetchrow(
                """
                UPDATE project_state
                   SET edit_lock_owner = NULL, edit_lock_gen = NULL, updated_at = now()
                 WHERE id AND edit_lock_owner = $1
                RETURNING site_rev
                """,
                owner,
            )
            if state is None:
                holder = (await _fetch(conn)).edit_lock_owner
                if holder is not None:
                    raise EditLockConflictError(holder)

    async def apply(self, *, project_id: str, version: str, content_hash: str) -> ProjectState:
        """적용 기록: base_site_rev = 현재 site_rev(드래프트 게이트 기준점, ADR-025)."""
        async with self._db.connection() as conn:
            await conn.execute(
                """
                UPDATE project_state
                   SET project_id = $1, version = $2, content_hash = $3,
                       base_site_rev = site_rev, applied_at = now(), updated_at = now()
                 WHERE id
                """,
                project_id,
                version,
                content_hash,
            )
            return await _fetch(conn)

    async def restore(self, snapshot: Mapping[str, object]) -> ProjectState:
        """백업 스냅샷 복원(롤백 경로, plan B5). site_rev·base_site_rev도 함께 되돌린다."""
        state = _row_to_state(
            {
                **snapshot,
                "applied_at": datetime.fromisoformat(str(snapshot["applied_at"]))
                if snapshot.get("applied_at")
                else None,
            }
        )
        async with self._db.connection() as conn:
            await conn.execute(
                """
                UPDATE project_state
                   SET project_id = $1, version = $2, content_hash = $3, base_site_rev = $4,
                       site_rev = $5, edit_lock_owner = $6, edit_lock_gen = $7, applied_at = $8,
                       updated_at = now()
                 WHERE id
                """,
                state.project_id,
                state.version,
                state.content_hash,
                state.base_site_rev,
                state.site_rev,
                state.edit_lock_owner,
                state.edit_lock_gen,
                state.applied_at,
            )
            return await _fetch(conn)
