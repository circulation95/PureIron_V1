"""`users` 테이블 저장소(asyncpg raw SQL, plan M3b D3). 계정은 지우지 않고 `enabled`만 바꾼다."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from pureiron_core.auth.ports import (
    AuthUnavailableError,
    Principal,
    Role,
    User,
    UserExistsError,
    UserNotFoundError,
)
from pureiron_core.platform.db import Connection, Database, DatabaseUnavailableError

# 로그인·세션 검증 경로의 조회 제한 시간(half-open DB 연결에서 요청이 멈추지 않게).
_QUERY_TIMEOUT_S = 5.0

_COLUMNS = "user_id, username, display_name, role, password_hash, enabled, created_at"
SELECT_BY_NAME_SQL = f"SELECT {_COLUMNS} FROM users WHERE username = $1"
SELECT_ALL_SQL = f"SELECT {_COLUMNS} FROM users ORDER BY username"
INSERT_SQL = f"""
INSERT INTO users (username, display_name, role, password_hash)
VALUES ($1, $2, $3, $4)
RETURNING {_COLUMNS}
"""
UPDATE_PASSWORD_SQL = f"""
UPDATE users SET password_hash = $2, updated_at = now() WHERE username = $1
RETURNING {_COLUMNS}
"""
UPDATE_ENABLED_SQL = f"""
UPDATE users SET enabled = $2, updated_at = now() WHERE username = $1
RETURNING {_COLUMNS}
"""


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: int
    username: str
    display_name: str | None
    role: Role
    password_hash: str
    enabled: bool
    created_at: datetime

    def principal(self) -> Principal:
        return Principal(self.user_id, self.username, self.role, self.display_name)

    def public(self) -> User:
        return User(
            user_id=self.user_id,
            username=self.username,
            role=self.role,
            display_name=self.display_name,
            enabled=self.enabled,
            created_at=self.created_at,
        )


def _record(row: asyncpg.Record) -> UserRecord:
    return UserRecord(
        user_id=int(row["user_id"]),
        username=str(row["username"]),
        display_name=row["display_name"],
        role=Role(row["role"]),
        password_hash=str(row["password_hash"]),
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
    )


class PgUserStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    @asynccontextmanager
    async def _connection(self) -> AsyncGenerator[Connection]:
        try:
            async with self._db.connection() as conn:
                yield conn
        except (DatabaseUnavailableError, asyncpg.PostgresError) as exc:
            raise AuthUnavailableError(f"site-db error: {exc}") from exc

    async def get(self, username: str) -> UserRecord | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(SELECT_BY_NAME_SQL, username, timeout=_QUERY_TIMEOUT_S)
        return None if row is None else _record(row)

    async def list_all(self) -> tuple[UserRecord, ...]:
        async with self._connection() as conn:
            rows = await conn.fetch(SELECT_ALL_SQL, timeout=_QUERY_TIMEOUT_S)
        return tuple(_record(row) for row in rows)

    async def add(
        self, *, username: str, display_name: str | None, role: Role, password_hash: str
    ) -> UserRecord:
        async with self._connection() as conn:
            try:
                row = await conn.fetchrow(
                    INSERT_SQL,
                    username,
                    display_name,
                    int(role),
                    password_hash,
                    timeout=_QUERY_TIMEOUT_S,
                )
            except asyncpg.UniqueViolationError:
                raise UserExistsError(username) from None
        if row is None:
            raise AuthUnavailableError("INSERT INTO users returned no row")
        return _record(row)

    async def set_password(self, username: str, password_hash: str) -> UserRecord:
        return await self._update(UPDATE_PASSWORD_SQL, username, password_hash)

    async def set_enabled(self, username: str, enabled: bool) -> UserRecord:
        return await self._update(UPDATE_ENABLED_SQL, username, enabled)

    async def _update(self, sql: str, username: str, value: object) -> UserRecord:
        async with self._connection() as conn:
            row = await conn.fetchrow(sql, username, value, timeout=_QUERY_TIMEOUT_S)
        if row is None:
            raise UserNotFoundError(username)
        return _record(row)
