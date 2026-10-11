"""site-db(TimescaleDB) 연결 풀. raw SQL만(No ORM, ADR-001).

site-db는 필수지만, 기동 시 DB가 아직 없더라도 core는 죽지 않고 `/readyz`로 미준비를 알린다.
풀은 지연 생성되고 실패하면 다음 사용 때 다시 시도한다.
"""

from __future__ import annotations

import asyncio
import contextlib
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
        acquire_timeout_s: float = 5.0,
    ) -> None:
        self._dsn = dsn
        self._password = password
        self._max_size = max_size
        self._connect_timeout_s = connect_timeout_s
        # 연결 획득 제한 = 반환(release) 제한. 획득에 새 연결 connect(connect_timeout_s)가 들어갈
        # 수 있어 같은 5s를 기본으로 둔다 — 정상 반환(reset)은 ms 단위다.
        self._acquire_timeout_s = acquire_timeout_s
        self._pool: Pool | None = None
        self._lock = asyncio.Lock()
        # 기다리던 쪽이 취소돼 백그라운드로 넘어간 반환 task(끝날 때까지 강한 참조를 든다).
        self._releasing: set[asyncio.Task[None]] = set()

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
        """풀에서 연결 하나를 빌린다. 획득·반환 모두 `acquire_timeout_s` 안에 끝난다.

        asyncpg 0.31.0(`asyncpg/pool.py`)에서 `acquire(timeout=)`는 획득 대기(슬롯 대기·새 연결
        포함, `Pool._acquire` 877-899: `wait_for`)를 제한하고, 그 값을 `ch._timeout`에 남겨(888)
        반환의 기본 budget으로도 쓴다(`Pool.release` 930-937). 반환은 `asyncio.shield`로 감싸져 바깥
        취소로 끊기지 않지만, 그 안의 취소 완료 대기와 reset이 이 budget을 넘으면 연결을
        `terminate()`하고 예외를 낸다(`PoolConnectionHolder.release` 221-249). 그래서 쿼리가 바깥
        취소로 끊긴 뒤에도 반환 대기가 이 시간 안에 끝나고 슬롯도 풀린다.

        획득 시간 초과는 그 사실이 드러나는 메시지의 `DatabaseUnavailableError`가 된다. 본문(쿼리)
        이나 반환의 오류는 예전 그대로 아래 `except`가 바꾼다. 본문이 실패했으면 반환 오류는 버린다.
        """
        pool = await self._ensure_pool()
        try:
            try:
                conn = await pool.acquire(timeout=self._acquire_timeout_s)
            except TimeoutError as exc:
                raise DatabaseUnavailableError(
                    f"connection acquire timed out ({self._acquire_timeout_s:g}s)"
                ) from exc
            try:
                yield conn
            except BaseException:
                # 본문 예외(취소 포함)가 이긴다: 반환이 budget을 넘겨 낸 TimeoutError가
                # CancelledError를 덮으면 취소가 사라진다. 반환에 실패한 연결은 asyncpg가
                # terminate한다.
                with contextlib.suppress(Exception):
                    await self._release(pool, conn)
                raise
            await self._release(pool, conn)  # budget = 획득 때 남긴 acquire_timeout_s
        # TimeoutError도 OSError의 하위 클래스라 본문 쿼리·반환의 시간 초과도 여기서 바뀐다(원인은
        # __cause__). project API는 이를 503으로 내보낸다.
        except (OSError, asyncpg.exceptions.ConnectionDoesNotExistError) as exc:
            raise DatabaseUnavailableError(str(exc) or type(exc).__name__) from exc

    async def _release(self, pool: Pool, conn: PoolConnectionProxy[asyncpg.Record]) -> None:
        """반환을 소유한 task로 돌려 그 예외를 항상 회수한다(V2-1).

        asyncpg 반환은 `asyncio.shield(holder.release(budget))`다. Python 3.12 `shield`는 바깥
        await가 취소되면 안쪽 task의 완료 콜백을 떼어 내서, 그 반환이 budget을 넘겨 실패하면 아무도
        예외를 읽지 않아 `Task exception was never retrieved`가 남는다(half-open DB에서 쓰기
        시도마다). 여기 task는 바깥 취소에 끊기지 않으므로(`shield`) asyncpg의 shield도 취소되지
        않고, 예외가 이 task로 올라와 완료 콜백이 읽는다. task는 budget(`acquire_timeout_s`) 안에
        끝난다.
        """
        task = asyncio.create_task(pool.release(conn))
        self._releasing.add(task)
        task.add_done_callback(self._release_done)
        await asyncio.shield(task)

    def _release_done(self, task: asyncio.Task[None]) -> None:
        self._releasing.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            # 기다리던 쪽이 있었다면 그쪽도 같은 예외를 받는다. 취소된 뒤라면 연결은 asyncpg가
            # terminate했고 슬롯도 풀렸다 — 반복될 수 있어 debug로만 남긴다.
            log.debug("connection release failed: %r", exc)

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
