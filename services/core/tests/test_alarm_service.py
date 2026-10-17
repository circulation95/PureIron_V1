"""알람 서비스(D7·D8): 복원 전 보류, tag.changed → 저널 큐·이벤트 반환, ACK = 저널·감사 한
트랜잭션 후 반영·발행, 타이머·정의 변경 이벤트 발행, 정지 시 저널 최종 쓰기."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import pytest

from pureiron_core.alarms.ports import (
    AlarmNotAckableError,
    AlarmSnapshot,
    AlarmState,
    AlarmUnavailableError,
)
from pureiron_core.alarms.service import AlarmService
from pureiron_core.audit.ports import AuditAction, AuditSource
from pureiron_core.platform.bus import Event, EventBus
from pureiron_core.tags.ports import Quality
from pureiron_core.tags.store import TagStore

from .alarm_fakes import (
    TAG_ID,
    TAG_PATH,
    TS0_NS,
    Clock,
    FakeAlarmStore,
    FakeScheduler,
    alarm_def,
    definitions,
    eventually,
    journal_record,
    set_value,
    tag_store,
)

S = AlarmState


@dataclass
class Env:
    bus: EventBus
    tags: TagStore
    store: FakeAlarmStore
    scheduler: FakeScheduler
    clock: Clock
    service: AlarmService
    published: list[AlarmSnapshot] = field(default_factory=list[AlarmSnapshot])

    async def tag(self, value: float | None, quality: Quality = Quality.GOOD) -> None:
        """ingest와 같은 순서: TagStore 반영 → tag.changed 발행."""
        set_value(self.tags, value, quality=quality)
        await self.bus.publish(
            Event(
                "tag.changed." + TAG_PATH,
                {
                    "tag_id": TAG_ID,
                    "path": TAG_PATH,
                    "device_id": "plc-1",
                    "value": value,
                    "quality": int(quality),
                    "ts_ns": TS0_NS,
                },
            )
        )

    def state(self, alarm_id: str = "tank-high") -> AlarmState | None:
        found = [a for a in self.service.alarms() if a.alarm_id == alarm_id]
        return found[0].state if found else None


@pytest.fixture
async def env() -> AsyncGenerator[Env]:
    bus = EventBus()
    tags = tag_store()
    store = FakeAlarmStore()
    scheduler = FakeScheduler()
    clock = Clock()
    service = AlarmService(
        bus,
        tags,
        store,
        journal_flush_s=60.0,
        restore_retry_s=0.01,
        schedule=scheduler,
        clock=clock,
    )
    built = Env(bus, tags, store, scheduler, clock, service)

    async def collect(event: Event) -> None:
        alarm = event.payload.get("alarm")
        assert isinstance(alarm, AlarmSnapshot)
        built.published.append(alarm)

    bus.subscribe("alarm.changed.*", collect)
    yield built
    await service.stop(timeout_s=1.0)


async def _ready(env: Env) -> None:
    env.service.set_definitions(definitions(alarm_def()))
    await env.service.start()
    await eventually(lambda: env.service.restored)


async def test_definitions_wait_for_restore_then_apply(env: Env) -> None:
    env.store.available = False
    set_value(env.tags, 9.0)
    env.service.set_definitions(definitions(alarm_def()))
    await env.service.start()
    await asyncio.sleep(0.05)
    assert (env.service.restored, env.service.alarms()) == (False, ())
    await env.tag(9.5)  # 복원 전 태그 변화는 평가하지 않는다
    with pytest.raises(AlarmUnavailableError):
        await env.service.ack("tank-high", "op")
    env.store.available = True
    await eventually(lambda: env.service.restored)
    await eventually(lambda: env.state() is S.ACTIVE_UNACKED)
    assert [(a.alarm_id, a.state) for a in env.published] == [("tank-high", S.ACTIVE_UNACKED)]


async def test_restored_unacked_alarm_is_not_raised_again(env: Env) -> None:
    env.store.last = {"tank-high": journal_record("tank-high", "ACTIVE_UNACKED")}
    await _ready(env)
    await env.tag(9.0)
    assert (env.state(), env.published) == (S.ACTIVE_UNACKED, [])


async def test_tag_change_queues_journal_and_publishes_after_handler(env: Env) -> None:
    await _ready(env)
    await env.tag(9.0)
    assert [(a.alarm_id, a.state, a.value) for a in env.published] == [
        ("tank-high", S.ACTIVE_UNACKED, 9.0)
    ]
    status = env.service.status()
    assert (status.active_total, status.unacked_total, status.journal_queue_rows) == (1, 1, 1)
    assert env.store.rows == []  # 핸들러는 DB에 쓰지 않는다 — writer가 비운다
    await env.service.stop(timeout_s=1.0)
    [row] = env.store.rows
    assert (row.alarm_id, row.from_state, row.to_state, row.value, row.username) == (
        "tank-high",
        "NORMAL",
        "ACTIVE_UNACKED",
        9.0,
        None,
    )


async def test_ack_records_journal_and_audit_together_then_applies(env: Env) -> None:
    await _ready(env)
    await env.tag(9.0)
    env.published.clear()
    acked = await env.service.ack("tank-high", "kim")
    assert (acked.state, acked.changed_by) == (S.ACTIVE_ACKED, "kim")
    [(rows, entry)] = env.store.acked
    assert [(r.from_state, r.to_state, r.username) for r in rows] == [
        ("ACTIVE_UNACKED", "ACTIVE_ACKED", "kim")
    ]
    assert (entry.action, entry.source, entry.username, entry.target) == (
        AuditAction.ALARM_ACK,
        AuditSource.API,
        "kim",
        "tank-high",
    )
    assert dict(entry.detail) == {
        "tag_path": TAG_PATH,
        "from_state": "ACTIVE_UNACKED",
        "to_state": "ACTIVE_ACKED",
    }
    assert [(a.state, a.changed_by) for a in env.published] == [(S.ACTIVE_ACKED, "kim")]
    with pytest.raises(AlarmNotAckableError):
        await env.service.ack("tank-high", "kim")  # 이미 ACKED → 409 근거


async def test_ack_failure_leaves_state_and_publishes_nothing(env: Env) -> None:
    await _ready(env)
    await env.tag(9.0)
    env.published.clear()
    env.store.available = False
    with pytest.raises(AlarmUnavailableError):
        await env.service.ack("tank-high", "kim")
    assert (env.state(), env.published, env.store.acked) == (S.ACTIVE_UNACKED, [], [])


async def test_ack_follows_clear_that_happened_while_recording(env: Env) -> None:
    """리뷰 M-1: 해제 행 뒤에 오는 NORMAL 행이 시각으로도 마지막이다(재기동 복원 = 메모리)."""
    await _ready(env)
    await env.tag(9.0)

    async def clear_now() -> None:
        env.clock.advance(0.05)
        await env.tag(1.0)  # 트랜잭션 도중 해제(tag.changed 핸들러는 ACK 직렬화와 무관하게 돈다)
        env.clock.advance(0.05)

    env.clock.advance(1.0)
    env.store.during_ack = clear_now
    acked = await env.service.ack("tank-high", "kim")
    assert acked.state is S.NORMAL  # CLEARED_UNACKED에 ACK → NORMAL
    await env.service.stop(timeout_s=1.0)
    by_time = sorted(env.store.journal, key=lambda r: r.time)  # 시각이 모두 다르다
    assert [(r.from_state, r.to_state, r.username) for r in by_time] == [
        ("NORMAL", "ACTIVE_UNACKED", None),
        ("ACTIVE_UNACKED", "ACTIVE_ACKED", "kim"),  # 기록된 확인 행위
        ("ACTIVE_UNACKED", "CLEARED_UNACKED", None),
        ("CLEARED_UNACKED", "NORMAL", "kim"),
    ]
    last = env.store.last_row("tank-high")
    assert (last.to_state, last.time) == ("NORMAL", acked.changed_at)


async def test_ack_is_not_applied_to_occurrence_raised_while_recording(env: Env) -> None:
    """리뷰 L-A1: 기록하는 사이 해제 → 재발생하면 그 ACK는 반영하지 않는다. 응답은 지금 상태
    (ACTIVE_UNACKED)라 운전원이 다시 ACK하고, 저널의 마지막 행도 ACTIVE_UNACKED다."""
    await _ready(env)
    await env.tag(9.0)

    async def clear_and_raise() -> None:
        env.clock.advance(0.05)
        await env.tag(7.0)
        env.clock.advance(0.05)
        await env.tag(9.5)

    env.clock.advance(1.0)
    env.store.during_ack = clear_and_raise
    env.published.clear()
    acked = await env.service.ack("tank-high", "kim")
    assert (acked.state, acked.value, acked.changed_by) == (S.ACTIVE_UNACKED, 9.5, None)
    assert [a.state for a in env.published] == [S.CLEARED_UNACKED, S.ACTIVE_UNACKED]
    env.store.during_ack = None
    await env.service.stop(timeout_s=1.0)
    last = env.store.last_row("tank-high")
    assert (last.from_state, last.to_state, last.value) == (
        "CLEARED_UNACKED",
        "ACTIVE_UNACKED",
        9.5,
    )
    assert env.state() is S.ACTIVE_UNACKED


async def test_ack_all_is_one_transaction_with_one_audit_entry(env: Env) -> None:
    await env.service.start()
    await eventually(lambda: env.service.restored)
    env.service.set_definitions(
        definitions(
            alarm_def("a", setpoint=1), alarm_def("b", setpoint=5), alarm_def("c", setpoint=50)
        )
    )
    await env.tag(9.0)
    acked = await env.service.ack_all("lee")
    assert sorted((a.alarm_id, a.state) for a in acked) == [
        ("a", S.ACTIVE_ACKED),
        ("b", S.ACTIVE_ACKED),
    ]
    [(rows, entry)] = env.store.acked
    assert sorted(r.alarm_id for r in rows) == ["a", "b"]
    assert (entry.action, entry.target, entry.detail["count"]) == (
        AuditAction.ALARM_ACK_ALL,
        None,
        2,
    )
    assert await env.service.ack_all("lee") == ()
    assert len(env.store.acked) == 1  # ACK할 것이 없으면 기록하지 않는다


async def test_on_delay_expiry_is_published_by_the_service(env: Env) -> None:
    env.service.set_definitions(definitions(alarm_def(on_delay_s=3)))
    await env.service.start()
    await eventually(lambda: env.service.restored)
    await env.tag(9.0)
    assert env.published == []
    env.scheduler.fire()
    await eventually(lambda: len(env.published) == 1)
    assert env.published[0].state is S.ACTIVE_UNACKED


async def test_removed_definition_publishes_retired(env: Env) -> None:
    await _ready(env)
    await env.tag(9.0)
    env.service.set_definitions(definitions())
    await eventually(lambda: env.published[-1].state is S.RETIRED)
    assert env.service.alarms() == ()
