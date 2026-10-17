"""알람 저널 writer(D8): 상한 drop-oldest, 쓰기 실패 되돌림·재시도, 주기 쓰기, 정지 시 최종 쓰기."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest

from pureiron_core.alarms.journal import AlarmJournalWriter, JournalRow
from pureiron_core.alarms.ports import AlarmUnavailableError, JournalQuery, JournalRecord
from pureiron_core.audit.ports import AuditEntry

from .alarm_fakes import T0, FakeAlarmStore


def _rows(*alarm_ids: str) -> list[JournalRow]:
    return [
        JournalRow(T0, alarm_id, "plant/x", "NORMAL", "ACTIVE_UNACKED", 1.0, None)
        for alarm_id in alarm_ids
    ]


def _ids(rows: Sequence[JournalRow]) -> list[str]:
    return [r.alarm_id for r in rows]


async def test_queue_drops_oldest_beyond_limit() -> None:
    store = FakeAlarmStore()
    writer = AlarmJournalWriter(store, max_rows=3)
    writer.offer(_rows("a", "b"))
    writer.offer(_rows("c", "d", "e"))
    status = writer.status()
    assert (status.queue_rows, status.dropped_total) == (3, 2)
    await writer.flush()
    assert _ids(store.rows) == ["c", "d", "e"]


async def test_failed_write_is_requeued_in_order_and_retried() -> None:
    store = FakeAlarmStore(available=False)
    writer = AlarmJournalWriter(store, max_rows=10, flush_max_rows=2)
    writer.offer(_rows("a", "b", "c"))
    assert await writer.flush() == 0
    assert (writer.status().queue_rows, writer.status().db_ok) == (3, False)
    writer.offer(_rows("d"))
    store.available = True
    assert await writer.flush() == 2
    assert await writer.flush() == 2
    assert _ids(store.rows) == ["a", "b", "c", "d"]
    assert (writer.status().written_total, writer.status().db_ok) == (4, True)


@dataclass
class _FailingWhileWriting:
    """쓰는 도중 새 행 c·d가 들어오고 그 쓰기는 실패하는 저장소(`AlarmStore`의 append만 쓴다)."""

    writer: AlarmJournalWriter | None = None
    fail: bool = True
    rows: list[JournalRow] = field(default_factory=list[JournalRow])

    async def append(self, rows: Sequence[JournalRow]) -> None:
        if self.fail:
            assert self.writer is not None
            self.writer.offer(_rows("c", "d"))
            raise AlarmUnavailableError("down")
        self.rows.extend(rows)

    async def append_acked(self, rows: Sequence[JournalRow], entry: AuditEntry) -> None:
        raise AssertionError("not used")

    async def last_states(self) -> dict[str, JournalRecord]:
        raise AssertionError("not used")

    async def query(self, query: JournalQuery) -> tuple[JournalRecord, ...]:
        raise AssertionError("not used")


async def test_requeue_respects_limit_by_dropping_oldest() -> None:
    store = _FailingWhileWriting()
    writer = AlarmJournalWriter(store, max_rows=3, flush_max_rows=2)
    store.writer = writer
    writer.offer(_rows("a", "b"))
    assert await writer.flush() == 0
    # 되돌린 a·b가 가장 오래된 행이라 c·d 앞에 서고, 상한 3을 넘는 a가 버려진다
    assert (writer.status().queue_rows, writer.status().dropped_total) == (3, 1)
    store.fail = False
    await writer.flush()
    await writer.flush()
    assert _ids(store.rows) == ["b", "c", "d"]


async def test_loop_flushes_periodically_and_stop_writes_the_rest() -> None:
    store = FakeAlarmStore()
    writer = AlarmJournalWriter(store, flush_s=0.01)
    writer.start()
    writer.offer(_rows("a"))
    for _ in range(100):
        if store.rows:
            break
        await asyncio.sleep(0.01)
    assert _ids(store.rows) == ["a"]
    writer.offer(_rows("b", "c"))
    await writer.stop(timeout_s=1.0)
    assert _ids(store.rows) == ["a", "b", "c"]
    assert writer.status().queue_rows == 0


async def test_stop_gives_up_after_timeout_when_db_is_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeAlarmStore(available=False)
    writer = AlarmJournalWriter(store, flush_s=10.0)
    writer.start()
    writer.offer(_rows("a"))
    await writer.stop(timeout_s=0.05)
    assert writer.status().queue_rows == 1
    assert "not written" in caplog.text
