"""auth 저장소(`users`·`sessions`) 공통 site-db 연결: DB 오류를 `AuthUnavailableError`로 바꾼다."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg

from pureiron_core.auth.ports import AuthUnavailableError
from pureiron_core.platform.db import Connection, Database, DatabaseUnavailableError


@asynccontextmanager
async def auth_connection(db: Database) -> AsyncGenerator[Connection]:
    try:
        async with db.connection() as conn:
            yield conn
    except (DatabaseUnavailableError, asyncpg.PostgresError) as exc:
        raise AuthUnavailableError(f"site-db error: {exc}") from exc
