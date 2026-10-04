"""site-db(TimescaleDB) 연결 풀. raw SQL만(No ORM, ADR-001).

site-db는 필수지만, 기동 시 DB가 아직 없더라도 core는 죽지 않고 `/readyz`로 미준비를 알린다.
풀은 지연 생성되고 실패하면 다음 사용 때 다시 시도한다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import asyncpg
from asyncpg.pool import PoolConnectionProxy

log = logging.getLogger(__name__)

type Pool = asyncpg.Pool[asyncpg.Record]
type Connection = asyncpg.Connection[asyncpg.Record] | PoolConnectionProxy[asyncpg.Record]


class DatabaseUnavailableError(RuntimeError):
    pass


class Database:
    def __init__(
        self,
        dsn: str,
        *,
        password: str | None = None,
        max_size: int = 10,
        connect_timeout_s: float = 5.0,
    ) -> None:
        self._dsn = dsn
        self._password = password
        self._max_size = max_size
        self._connect_timeout_s = connect_timeout_s
        self._pool: Pool | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            await self._ensure_pool()
        except DatabaseUnavailableError as exc:
            log.warning("site-db not reachable at startup, will retry on use: %s", exc)

    async def close(self) -> None:
        async with self._lock:
            if self._pool is not None:
                await self._pool.close()
                self._pool = None

    async def _ensure_pool(self) -> Pool:
        async with self._lock:
            if self._pool is None:
                try:
                    self._pool = await asyncpg.create_pool(
                        self._dsn,
                        password=self._password,
                        min_size=1,
                        max_size=self._max_size,
                        timeout=self._connect_timeout_s,
                    )
                except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
                    raise DatabaseUnavailableError(str(exc)) from exc
            return self._pool

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[Connection]:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                yield conn
        except (OSError, asyncpg.exceptions.ConnectionDoesNotExistError) as exc:
            raise DatabaseUnavailableError(str(exc)) from exc

    async def ping(self, timeout_s: float = 2.0) -> bool:
        try:
            async with self.connection() as conn:
                return await conn.fetchval("SELECT 1", timeout=timeout_s) == 1
        except (DatabaseUnavailableError, asyncpg.PostgresError, TimeoutError):
            return False


async def connect_once(
    dsn: str, *, password: str | None, timeout_s: float = 10.0
) -> asyncpg.Connection[asyncpg.Record]:
    """CLI(migrate·project)용 단일 연결."""
    try:
        return await asyncpg.connect(dsn, password=password, timeout=timeout_s)
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        raise DatabaseUnavailableError(str(exc)) from exc
