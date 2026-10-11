"""`audit_log` 기록·조회(asyncpg raw SQL, plan M3b D12).

`record`는 best-effort(실패 시 경고 로그, 예외 없음), `record_with(conn, …)`는 호출자
트랜잭션 안에서 쓰고 실패를 그대로 올린다(M3b-2 알람 ACK = 저널·감사 같은 트랜잭션).
조회는 최신순·`[start, end)`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import cast

import asyncpg

from pureiron_core.audit.ports import (
    MAX_QUERY_ROWS,
    AuditEntry,
    AuditQuery,
    AuditRecord,
    AuditUnavailableError,
)
from pureiron_core.platform.db import Connection, Database, DatabaseUnavailableError

log = logging.getLogger(__name__)

# 감사 기록은 요청 경로에서 끝까지 기다리므로 짧게 둔다(실패해도 요청은 성공한다).
_WRITE_TIMEOUT_S = 3.0
_QUERY_TIMEOUT_S = 10.0

INSERT_SQL = """
INSERT INTO audit_log (username, action, target, detail, source)
VALUES ($1, $2, $3, $4::jsonb, $5)
"""
# 조건이 없는 인자는 NULL로 넘겨 하나의 SQL로 둔다(동적 SQL 조립 없음).
SELECT_SQL = """
SELECT id, time, username, action, target, detail::text AS detail, source
  FROM audit_log
 WHERE ($1::text IS NULL OR username = $1)
   AND ($2::text IS NULL OR action = $2)
   AND ($3::timestamptz IS NULL OR time >= $3)
   AND ($4::timestamptz IS NULL OR time < $4)
 ORDER BY time DESC, id DESC
 LIMIT $5
"""


def _detail(text: str) -> Mapping[str, object]:
    value: object = json.loads(text)
    return cast("dict[str, object]", value) if isinstance(value, dict) else {"value": value}


class PgAuditStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_with(self, conn: Connection, entry: AuditEntry) -> None:
        await conn.execute(
            INSERT_SQL,
            entry.username,
            entry.action.value,
            entry.target,
            json.dumps(dict(entry.detail), sort_keys=True, default=str),
            entry.source.value,
            timeout=_WRITE_TIMEOUT_S,
        )

    async def record(self, entry: AuditEntry) -> None:
        try:
            async with self._db.connection() as conn:
                await self.record_with(conn, entry)
        # best-effort 경계: 연결 상태 오류(InterfaceError)도 요청을 실패시키지 않는다.
        except (DatabaseUnavailableError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            log.warning(
                "audit record dropped (action=%s user=%s): %s", entry.action, entry.username, exc
            )

    async def query(self, query: AuditQuery) -> tuple[AuditRecord, ...]:
        limit = max(1, min(query.limit, MAX_QUERY_ROWS))
        try:
            async with self._db.connection() as conn:
                rows = await conn.fetch(
                    SELECT_SQL,
                    query.username,
                    query.action,
                    query.start,
                    query.end,
                    limit,
                    timeout=_QUERY_TIMEOUT_S,
                )
        except (DatabaseUnavailableError, asyncpg.PostgresError) as exc:
            raise AuditUnavailableError(f"site-db error: {exc}") from exc
        return tuple(
            AuditRecord(
                id=int(row["id"]),
                time=row["time"],
                username=row["username"],
                action=str(row["action"]),
                target=row["target"],
                detail=_detail(str(row["detail"])),
                source=str(row["source"]),
            )
            for row in rows
        )
