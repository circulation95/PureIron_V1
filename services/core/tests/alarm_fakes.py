"""알람 테스트용 가짜 루프 타이머·시계·저널 저장소(`Timer`·`AlarmStore` Protocol을 직접 만족)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pureiron_core.alarms.journal import JournalRow
from pureiron_core.alarms.ports import AlarmUnavailableError, JournalQuery, JournalRecord
from pureiron_core.audit.ports import AuditEntry
from pureiron_core.project.ports import AlarmDef, Definitions, TagDef
from pureiron_core.tags.ports import Quality, Reading, TagKey
from pureiron_core.tags.store import TagStore

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
TS0_NS = 1_791_000_000_000_000_000


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeTimer:
    delay_s: float
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


@dataclass
class FakeScheduler:
    """`loop.call_later` 대역: 건 타이머를 모으고 `fire()`로 만료시킨다(취소된 것은 건너뛴다)."""

    timers: list[FakeTimer] = field(default_factory=list[FakeTimer])

    def __call__(self, delay_s: float, callback: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(delay_s, callback)
        self.timers.append(timer)
        return timer

    @property
    def pending(self) -> list[FakeTimer]:
        return [t for t in self.timers if not t.cancelled]

    def fire(self) -> None:
        due, self.timers = self.pending, []
        for timer in due:
            timer.callback()


@dataclass
class FakeAlarmStore:
    rows: list[JournalRow] = field(default_factory=list[JournalRow])
    acked: list[tuple[list[JournalRow], AuditEntry]] = field(
        default_factory=list[tuple[list[JournalRow], AuditEntry]]
    )
    last: dict[str, JournalRecord] = field(default_factory=dict[str, JournalRecord])
    queries: list[JournalQuery] = field(default_factory=list[JournalQuery])
    available: bool = True
    # ACK 트랜잭션 도중 부를 함수(기록하는 사이 상태가 바뀌는 경합 재현용).
    during_ack: Callable[[], Awaitable[None]] | None = None
    # INSERT 순서(= DB id 순서)의 모든 저널 행. ACK 행은 트랜잭션 시작에 INSERT된다.
    journal: list[JournalRow] = field(default_factory=list[JournalRow])

    def _check(self) -> None:
        if not self.available:
            raise AlarmUnavailableError("site-db error: down")

    async def append(self, rows: Sequence[JournalRow]) -> None:
        self._check()
        self.rows.extend(rows)
        self.journal.extend(rows)

    async def append_acked(self, rows: Sequence[JournalRow], entry: AuditEntry) -> None:
        self._check()
        self.journal.extend(rows)
        if self.during_ack is not None:
            await self.during_ack()
        self.acked.append((list(rows), entry))

    def last_row(self, alarm_id: str) -> JournalRow:
        """재기동 복원이 고를 행(`LAST_STATES_SQL`과 같은 `ORDER BY time DESC, id DESC`)."""
        ordered = [(r.time, i, r) for i, r in enumerate(self.journal) if r.alarm_id == alarm_id]
        return max(ordered, key=lambda item: (item[0], item[1]))[2]

    async def last_states(self) -> dict[str, JournalRecord]:
        self._check()
        return dict(self.last)

    async def query(self, query: JournalQuery) -> tuple[JournalRecord, ...]:
        self._check()
        self.queries.append(query)
        return ()


TAG_PATH = "plant/tank1/level"
TAG_ID = 7


def alarm_def(
    alarm_id: str = "tank-high",
    *,
    tag: str = TAG_PATH,
    kind: str = "above",
    setpoint: float = 8.0,
    **extra: object,
) -> AlarmDef:
    return AlarmDef(
        alarm_id, tag, {"id": alarm_id, "tag": tag, "type": kind, "setpoint": setpoint, **extra}
    )


def definitions(
    *alarms: AlarmDef, rev: int = 1, tag_ids: dict[str, int] | None = None
) -> Definitions:
    tags = tag_ids if tag_ids is not None else {TAG_PATH: TAG_ID}
    return Definitions(
        rev=rev,
        tags=tuple(TagDef(tag_id, path, "plc-1", {}) for path, tag_id in tags.items()),
        alarms=alarms,
    )


def tag_store(tag_ids: dict[str, int] | None = None) -> TagStore:
    store = TagStore()
    tags = tag_ids if tag_ids is not None else {TAG_PATH: TAG_ID}
    store.reindex(TagKey(tag_id, path, "plc-1") for path, tag_id in tags.items())
    return store


def set_value(
    store: TagStore,
    value: float | bool | None,
    *,
    tag_id: int = TAG_ID,
    quality: Quality = Quality.GOOD,
    ts_ns: int = TS0_NS,
) -> None:
    store.commit([Reading(tag_id, value, quality, ts_ns)], ts_ns)


def journal_record(
    alarm_id: str, to_state: str, *, value: float | None = 9.0, username: str | None = None
) -> JournalRecord:
    return JournalRecord(
        id=1,
        time=T0 - timedelta(hours=1),
        alarm_id=alarm_id,
        tag_path=TAG_PATH,
        from_state="NORMAL",
        to_state=to_state,
        value=value,
        username=username,
        note=None,
    )


async def eventually(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.005)
