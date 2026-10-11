"""site-db 풀 래퍼: 연결 획득·반환 시간 제한(V1 N1)과 `PgRowStore` 오류 분류(R1 L9b).

가짜 pool은 asyncpg 0.31.0 `asyncpg/pool.py`의 다음 동작만 흉내 낸다(가정이 틀리면 테스트도 틀린다):
- `acquire(timeout=t)`는 획득 대기를 `wait_for(…, t)`로 제한하고, t를 그 연결의 반환 budget으로
  남긴다(`Pool._acquire`, `ch._timeout = timeout`).
- `release(conn)`은 `asyncio.shield(holder.release(budget))`다 — 바깥 취소로 끊기지 않는다. 쿼리가
  끊긴 연결이면 취소 완료를 budget까지 기다리고, 넘으면 연결을 terminate한 뒤 예외를 낸다
  (`PoolConnectionHolder.release`).
- 응답 없는(half-open) 서버: `execute(…, timeout=q)`는 q까지 기다렸다 `TimeoutError`를 낸다. 시간
  초과든 바깥 취소든 연결은 "취소 중"이 되고, 서버가 응답하지 않으니 그 취소는 끝나지 않는다.

async 함수의 `timeout` 인자(ruff ASYNC109)를 피하려고 asyncpg와 같은 이름의 인자를 받는 메서드는
동기 함수가 코루틴을 돌려준다.
"""

from __future__ import annotations

import asyncio
import gc
import time
from collections.abc import Coroutine
from typing import Any

import asyncpg
import pytest

from pureiron_core.historian.writer import HistoryWriter, PgRowStore, Row, RowsRejectedError
from pureiron_core.platform.db import Database, DatabaseUnavailableError


class FakeConn:
    def __init__(self, *, hangs: int | None = None) -> None:
        self.hangs = hangs  # 멈추는 execute 횟수(None = 계속). 그 뒤 execute는 바로 연결 오류
        self.cancelling = False
        self.executing = asyncio.Event()
        self.execute_cancelled = False  # 바깥 취소가 execute 도중에 걸렸는가
        self.terminated = False

    def execute(
        self, query: str, *args: object, timeout: float | None = None
    ) -> Coroutine[Any, Any, str]:
        return self._execute(timeout)

    async def _execute(self, limit_s: float | None) -> str:
        if self.hangs is not None:
            if self.hangs == 0:
                raise ConnectionResetError("db down")
            self.hangs -= 1
        self.executing.set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), limit_s)
        except asyncio.CancelledError:
            self.execute_cancelled = True
            raise
        finally:
            self.cancelling = True
        return "INSERT 0 0"


class FakePool:
    def __init__(self, conn: FakeConn, *, acquire_delay_s: float = 0.0) -> None:
        self.conn = conn
        self.acquire_delay_s = acquire_delay_s
        self.budgets: list[float | None] = []
        self._budget: float | None = None

    def acquire(self, *, timeout: float | None = None) -> Coroutine[Any, Any, FakeConn]:
        return self._acquire(timeout)

    async def _acquire(self, limit_s: float | None) -> FakeConn:
        await asyncio.wait_for(asyncio.sleep(self.acquire_delay_s), limit_s)
        self._budget = limit_s
        return self.conn

    async def release(self, conn: FakeConn) -> None:
        self.budgets.append(self._budget)
        await asyncio.shield(self._release(conn, self._budget))

    async def _release(self, conn: FakeConn, budget: float | None) -> None:
        try:
            if conn.cancelling:
                await asyncio.wait_for(asyncio.Event().wait(), budget)
        except (Exception, asyncio.CancelledError):
            conn.terminated = True
            raise


def fake_db(
    monkeypatch: pytest.MonkeyPatch, pool: FakePool, *, acquire_timeout_s: float
) -> Database:
    async def create_pool(*_args: object, **_kwargs: object) -> FakePool:
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    return Database("postgresql://fake", acquire_timeout_s=acquire_timeout_s)


async def test_acquire_timeout_becomes_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = fake_db(monkeypatch, FakePool(FakeConn(), acquire_delay_s=60.0), acquire_timeout_s=0.05)
    with pytest.raises(DatabaseUnavailableError, match=r"acquire timed out \(0.05s\)"):
        async with asyncio.timeout(2.0), db.connection():
            pass
    assert await asyncio.wait_for(db.ping(), 2.0) is False


async def test_timeout_in_body_is_not_reported_as_acquire_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """본문(쿼리)의 TimeoutError는 예전처럼 OSError 절에서 바뀌고, 메시지·원인이 그대로 남는다."""
    pool = FakePool(FakeConn())
    db = fake_db(monkeypatch, pool, acquire_timeout_s=0.5)
    query_timeout = TimeoutError("query")
    with pytest.raises(DatabaseUnavailableError, match=r"^query$") as info:
        async with db.connection():
            raise query_timeout
    assert info.value.__cause__ is query_timeout
    assert pool.budgets == [0.5]  # 반환 budget = acquire 제한


async def test_writer_flush_bounded_when_write_limit_lands_in_execute(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """V1 N1: 획득 지연 > (쓰기 제한 - 쿼리 제한)이면 쓰기 제한의 한 번뿐인 취소를 execute가 쓴다.

    그 뒤 shield된 반환 대기는 다시 취소되지 않는다. 반환 budget(= acquire 제한)이 없으면 flush가
    끝없이 멈췄다(V1 부록 0.8s 케이스). 이제는 쓰기 제한 + acquire 제한 안에 실패로 돌아온다.
    """
    query_s, write_s, acquire_s = 0.4, 0.6, 0.5
    conn = FakeConn()
    pool = FakePool(conn, acquire_delay_s=0.4)  # 0.4 > write_s - query_s(0.2)
    db = fake_db(monkeypatch, pool, acquire_timeout_s=acquire_s)
    writer = HistoryWriter(PgRowStore(db, query_timeout_s=query_s), write_timeout_s=write_s)
    writer.append([Row(i * 1_000_000_000, i, float(i), None, 1) for i in (1, 2, 3)])

    started = time.monotonic()
    assert await asyncio.wait_for(writer.flush(), 3.0) == 0
    elapsed = time.monotonic() - started

    assert conn.execute_cancelled  # 재현 조건: 쓰기 제한이 execute 도중에 걸렸다
    assert conn.terminated  # 반환 budget을 넘긴 연결은 버려진다
    assert pool.budgets == [acquire_s]
    assert elapsed < write_s + acquire_s + 0.3
    assert "timed out (write limit 0.6s)" in caplog.text  # 반환 실패가 시간 초과를 덮지 않는다
    status = writer.status()
    assert (status.buffer_rows, status.db_ok, status.written_total) == (3, False, 0)


async def test_stop_cancel_during_execute_is_not_swallowed_by_release_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`stop()`의 취소가 INSERT 도중에 걸리고 반환이 budget을 넘어도 취소는 그대로 전파된다.

    반환의 `TimeoutError`가 `CancelledError`를 덮으면 writer는 이를 일반 실패로 삼켜 flush loop가
    계속 돌고, `stop()`이 loop를 기다리며 끝나지 않는다. 그 회귀가 테스트를 멈추게 하지 않도록
    첫 execute만 멈추고 그 뒤로는 DB가 바로 연결 오류를 낸다(최종 flush도 바로 실패).
    """
    acquire_s = 0.2
    conn = FakeConn(hangs=1)
    db = fake_db(monkeypatch, FakePool(conn), acquire_timeout_s=acquire_s)
    writer = HistoryWriter(PgRowStore(db), flush_s=0.01)
    writer.append([Row(i * 1_000_000_000, i, float(i), None, 1) for i in (1, 2, 3)])
    writer.start()
    await asyncio.wait_for(conn.executing.wait(), 2.0)

    started = time.monotonic()
    await asyncio.wait_for(writer.stop(), 3.0)
    elapsed = time.monotonic() - started

    assert conn.execute_cancelled
    assert conn.terminated
    # loop 취소 뒤 반환 정리 ≤ acquire_s + 최종 flush의 반환 ≤ acquire_s
    # (가짜 연결은 terminate 뒤에도 취소 대기 상태로 남는다).
    # 무한 대기 회귀는 wait_for(3.0)가 잡는다.
    assert elapsed < 2 * acquire_s + 0.3
    assert writer.status().buffer_rows == 3
    assert "stopped with 3 unwritten" in caplog.text


class RaisingConn:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def execute(
        self, query: str, *args: object, timeout: float | None = None
    ) -> Coroutine[Any, Any, str]:
        return self._raise()

    async def _raise(self) -> str:
        raise self.exc


class PlainPool:
    def __init__(self, conn: RaisingConn) -> None:
        self.conn = conn

    def acquire(self, *, timeout: float | None = None) -> Coroutine[Any, Any, RaisingConn]:
        return self._acquire()

    async def _acquire(self) -> RaisingConn:
        return self.conn

    async def release(self, conn: RaisingConn) -> None:
        pass


def _store_raising(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> PgRowStore:
    async def create_pool(*_args: object, **_kwargs: object) -> PlainPool:
        return PlainPool(RaisingConn(exc))

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    return PgRowStore(Database("postgresql://fake"))


@pytest.mark.parametrize(
    "exc",
    [
        asyncpg.DataError("timestamp out of range"),
        asyncpg.NumericValueOutOfRangeError("value out of range"),
        asyncpg.UniqueViolationError("duplicate key"),
        asyncpg.DataError("invalid input for query argument $2: 2147483648"),
    ],
    ids=["server-data", "server-data-subclass", "integrity", "client-encode"],
)
async def test_row_store_reports_row_content_errors_as_rejected(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """R1 L9b: 행 내용 오류만 `RowsRejectedError`가 된다(writer가 반분 격리)."""
    store = _store_raising(monkeypatch, exc)
    with pytest.raises(RowsRejectedError) as info:
        await store.insert([Row(1, 1, 1.0, None, 1)])
    assert info.value.__cause__ is exc


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ConnectionResetError("db down"), DatabaseUnavailableError),
        (asyncpg.InterfaceError("connection is closed"), asyncpg.InterfaceError),
        (asyncpg.ClientConfigurationError("bad sslmode"), asyncpg.ClientConfigurationError),
        (asyncpg.InsufficientPrivilegeError("permission denied"), asyncpg.PostgresError),
    ],
    ids=["connection", "interface", "client-config", "server-other"],
)
async def test_row_store_keeps_other_errors(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, expected: type[Exception]
) -> None:
    """연결·권한 등 행 탓이 아닌 오류는 그대로(writer가 batch 전체를 되돌린다)."""
    store = _store_raising(monkeypatch, exc)
    with pytest.raises(expected) as info:
        await store.insert([Row(1, 1, 1.0, None, 1)])
    assert not isinstance(info.value, RowsRejectedError)


@pytest.mark.parametrize("body_fails", [False, True], ids=["after-body", "after-body-error"])
async def test_cancelled_release_wait_leaves_no_unretrieved_task_exception(
    monkeypatch: pytest.MonkeyPatch, body_fails: bool
) -> None:
    """V2-1: 반환을 기다리던 쪽이 취소되고 백그라운드 반환이 budget을 넘겨 실패해도 asyncio가
    `Task exception was never retrieved`를 남기지 않는다(반환 task의 예외를 회수한다).

    shield된 반환은 바깥 취소에 끊기지 않는다. Python 3.12 `shield`는 바깥이 취소되면 안쪽 task의
    완료 콜백을 떼어 내므로, 그 task가 나중에 실패하면 예외를 아무도 읽지 않는다.
    """
    loop = asyncio.get_running_loop()
    contexts: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        conn = FakeConn()
        conn.cancelling = True  # 쿼리가 끊긴 연결: 반환이 취소 완료를 budget까지 기다린다
        db = fake_db(monkeypatch, FakePool(conn), acquire_timeout_s=0.1)

        async def use() -> None:
            async with db.connection():
                if body_fails:
                    raise DatabaseUnavailableError("query timed out")

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(use(), 0.05)  # 반환 대기 도중 취소
        await asyncio.sleep(0.2)  # 백그라운드 반환이 budget(0.1s)을 넘겨 실패한다
        gc.collect()
        assert conn.terminated
        assert [c.get("message") for c in contexts] == []
    finally:
        loop.set_exception_handler(None)
