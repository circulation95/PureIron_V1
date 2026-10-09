"""historian writer: 상한 drop-oldest, N행씩 flush, 실패 시 순서 보존 되돌림, 최종 flush."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import pytest

from pureiron_core.historian.writer import HistoryWriter, Row, columns


def rows(*ids: int) -> list[Row]:
    """tag_id = 번호, 시각 = 번호초(순서 확인용)."""
    return [Row(i * 1_000_000_000 + 1_234, i, float(i), None, 1) for i in ids]


def ids(batch: Sequence[Row]) -> list[int]:
    return [r.tag_id for r in batch]


class FakeStore:
    def __init__(self) -> None:
        self.batches: list[list[Row]] = []
        self.fail = False
        self.gate: asyncio.Event | None = None
        self.calls = 0

    async def insert(self, rows: Sequence[Row]) -> None:
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise ConnectionError("db down")
        self.batches.append(list(rows))

    @property
    def written(self) -> list[int]:
        return [r.tag_id for b in self.batches for r in b]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_columns_convert_ns_to_us() -> None:
    us, tag_ids, nums, flags, qualities = columns(
        [Row(1_700_000_000_123_456_789, 7, None, True, 2)]
    )
    assert (us, tag_ids, nums, flags, qualities) == (
        [1_700_000_000_123_456],
        [7],
        [None],
        [True],
        [2],
    )


def test_overflow_drops_oldest_and_counts(caplog: pytest.LogCaptureFixture) -> None:
    writer = HistoryWriter(FakeStore(), max_rows=5)
    writer.append(rows(1, 2, 3))
    writer.append(rows(4, 5, 6, 7))  # 2개 초과 → 1, 2 버림
    status = writer.status()
    assert (status.buffer_rows, status.dropped_total) == (5, 2)
    assert "dropped 2 oldest" in caplog.text
    writer.append(rows(8, 9, 10, 11, 12, 13))  # 한 chunk가 통째로·부분적으로 빠지는 경우
    assert (writer.status().buffer_rows, writer.status().dropped_total) == (5, 8)


async def test_overflow_keeps_newest_rows() -> None:
    store = FakeStore()
    writer = HistoryWriter(store, max_rows=4)
    writer.append(rows(1, 2, 3))
    writer.append(rows(4, 5, 6))
    await writer.flush()
    assert store.written == [3, 4, 5, 6]


async def test_flush_writes_in_chunks_until_empty() -> None:
    store = FakeStore()
    writer = HistoryWriter(store, flush_max_rows=3)
    writer.append(rows(1, 2))
    writer.append(rows(3, 4, 5, 6, 7))
    assert await writer.flush() == 7
    assert [ids(b) for b in store.batches] == [[1, 2, 3], [4, 5, 6], [7]]
    status = writer.status()
    assert (status.buffer_rows, status.written_total, status.db_ok) == (0, 7, True)
    assert await writer.flush() == 0
    assert store.calls == 3


async def test_failure_puts_rows_back_in_order_then_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="pureiron_core.historian.writer")
    store = FakeStore()
    writer = HistoryWriter(store, flush_max_rows=2)
    writer.append(rows(1, 2, 3))
    store.fail = True
    assert await writer.flush() == 0
    assert writer.status().db_ok is False
    assert writer.status().buffer_rows == 3
    assert "write failed" in caplog.text
    writer.append(rows(4))
    caplog.clear()
    assert await writer.flush() == 0  # 실패 연속 구간은 경고를 반복하지 않는다
    assert "write failed" not in caplog.text

    store.fail = False
    assert await writer.flush() == 4
    assert store.written == [1, 2, 3, 4]
    assert writer.status().db_ok is True
    assert "recovered" in caplog.text


async def test_failure_reapplies_cap_dropping_the_failed_oldest_rows() -> None:
    """쓰는 동안 새 행이 들어와 상한을 넘으면, 되돌린 (가장 오래된) 행부터 버린다."""
    store = FakeStore()
    store.fail = True
    store.gate = asyncio.Event()
    writer = HistoryWriter(store, max_rows=4, flush_max_rows=10)
    writer.append(rows(1, 2, 3))
    flushing = asyncio.create_task(writer.flush())
    await asyncio.sleep(0)
    assert writer.status().buffer_rows == 3  # 쓰는 중인 행도 버퍼로 보고한다
    writer.append(rows(4, 5, 6))
    store.gate.set()
    await flushing
    status = writer.status()
    assert (status.buffer_rows, status.dropped_total) == (4, 2)
    store.fail = False
    store.gate = None
    await writer.flush()
    assert store.written == [3, 4, 5, 6]


async def test_rows_appended_during_write_follow_in_order() -> None:
    store = FakeStore()
    store.gate = asyncio.Event()
    writer = HistoryWriter(store, flush_max_rows=10)
    writer.append(rows(1, 2))
    flushing = asyncio.create_task(writer.flush())
    await asyncio.sleep(0)
    writer.append(rows(3))
    store.gate.set()
    assert await flushing == 3  # 같은 주기에 이어서 쓴다
    assert [ids(b) for b in store.batches] == [[1, 2], [3]]


async def test_write_lag_is_age_of_oldest_buffered_row() -> None:
    clock = Clock()
    store = FakeStore()
    store.gate = asyncio.Event()
    writer = HistoryWriter(store, clock=clock)
    assert writer.status().write_lag_s is None
    writer.append(rows(1))
    clock.now += 2.5
    writer.append(rows(2))
    clock.now += 1.0
    assert writer.status().write_lag_s == pytest.approx(3.5)
    flushing = asyncio.create_task(writer.flush())
    await asyncio.sleep(0)
    assert writer.status().write_lag_s == pytest.approx(3.5)  # 쓰는 중인 행 기준
    store.gate.set()
    await flushing
    assert writer.status().write_lag_s is None


async def test_stop_does_final_flush() -> None:
    store = FakeStore()
    writer = HistoryWriter(store, flush_s=3600)
    writer.start()
    writer.append(rows(1, 2))
    await writer.stop()
    assert store.written == [1, 2]
    assert writer.status().buffer_rows == 0


async def test_stop_cancels_in_flight_write_then_final_flush_writes_it() -> None:
    """flush loop가 쓰는 도중 stop: 취소된 행은 버퍼로 되돌아와 마지막 flush가 다시 쓴다."""
    store = FakeStore()
    store.gate = asyncio.Event()
    writer = HistoryWriter(store, flush_s=0.01)
    writer.start()
    writer.append(rows(1, 2))
    for _ in range(100):
        if store.calls:
            break
        await asyncio.sleep(0.01)
    assert store.calls == 1  # loop의 insert가 gate에서 멈춰 있다
    asyncio.get_running_loop().call_later(0.05, store.gate.set)
    await writer.stop(timeout_s=2.0)
    assert store.written == [1, 2]
    assert (writer.status().buffer_rows, writer.status().written_total) == (0, 2)


async def test_flush_loop_writes_periodically() -> None:
    store = FakeStore()
    writer = HistoryWriter(store, flush_s=0.01)
    writer.start()
    writer.append(rows(1))
    for _ in range(100):
        if store.written:
            break
        await asyncio.sleep(0.01)
    await writer.stop()
    assert store.written == [1]


async def test_stop_gives_up_after_timeout_and_keeps_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore()
    store.gate = asyncio.Event()  # 응답 없는(half-open) DB
    writer = HistoryWriter(store)
    writer.append(rows(1, 2, 3))
    await writer.stop(timeout_s=0.05)
    assert writer.status().buffer_rows == 3  # 취소된 쓰기는 버퍼로 되돌아온다
    assert "stopped with 3 unwritten" in caplog.text


class HangingStore(FakeStore):
    """응답 없는(half-open) DB: 연결 획득이든 INSERT든 반환이든 끝나지 않는다."""

    def __init__(self) -> None:
        super().__init__()
        self.hang = True
        self.cancelled = 0

    async def insert(self, rows: Sequence[Row]) -> None:
        if self.hang:
            self.calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        await super().insert(rows)


async def test_hanging_write_times_out_keeps_rows_and_retries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R1 M1: 쓰기 전체에 시간 제한 — flush가 제한 안에 실패로 돌아오고 다음 주기에 다시 쓴다."""
    store = HangingStore()
    writer = HistoryWriter(store, write_timeout_s=0.05)
    writer.append(rows(1, 2, 3))
    assert await asyncio.wait_for(writer.flush(), 2.0) == 0
    assert store.cancelled == 1  # 멈춘 쓰기는 취소된다
    status = writer.status()
    assert (status.buffer_rows, status.db_ok, status.written_total) == (3, False, 0)
    assert "write failed" in caplog.text
    assert "timed out (write limit 0.05s)" in caplog.text

    store.hang = False
    assert await writer.flush() == 3
    assert store.written == [1, 2, 3]
    assert writer.status().db_ok is True


async def test_stop_with_failing_db_reports_unwritten_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore()
    store.fail = True
    writer = HistoryWriter(store)
    writer.append(rows(1))
    await writer.stop()
    assert "stopped with 1 unwritten" in caplog.text
