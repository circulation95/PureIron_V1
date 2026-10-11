"""알람 수명주기: 저널 복원 → 정의 적용 → `tag.changed.*` 평가 → 저널 큐·발행, ACK 트랜잭션
(plan M3b D7·D8). 조립은 `runtime.py`가 한다.

- 기동: 저널의 알람별 마지막 상태를 읽을 때까지(site-db가 없으면 주기 재시도) 정의를 보류하고
  태그 변화를 평가하지 않는다 — 복원 전에 평가하면 미확인 알람이 새 알람으로 다시 생긴다.
- `tag.changed.*` 핸들러는 엔진을 동기로 평가하고 저널 행을 큐에 넣은 뒤 `alarm.changed.<id>`
  이벤트를 **돌려준다**(버스가 이어서 발행 — 핸들러는 I/O를 await 하지 않는다).
- 핸들러 밖에서 생기는 전이(on_delay 타이머 만료·정의 변경)는 이 서비스가 소유한 짧은 task로
  발행한다(stop에서 거둔다).
- ACK: 엔진이 전이를 미리 계산 → 저널 행·감사 기록을 한 트랜잭션으로 → 성공한 뒤에만 엔진에
  반영·발행. ACK끼리는 직렬화한다(미리 계산한 전이가 서로 엇갈리지 않게).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from datetime import datetime

from pureiron_core.alarms.engine import AlarmEngine, Scheduler, Timer, utc_now
from pureiron_core.alarms.journal import (
    DEFAULT_FLUSH_S,
    DEFAULT_MAX_ROWS,
    DEFAULT_STOP_TIMEOUT_S,
    AlarmJournalWriter,
    AlarmStore,
    JournalRow,
)
from pureiron_core.alarms.ports import (
    ALARM_CHANGED_PREFIX,
    AlarmSnapshot,
    AlarmStatus,
    AlarmTransition,
    AlarmUnavailableError,
    JournalQuery,
    JournalRecord,
)
from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSource
from pureiron_core.platform.bus import Event, EventBus
from pureiron_core.project.ports import Definitions
from pureiron_core.tags.ports import TAG_CHANGED_PREFIX, Quality, TagStorePort, Value

log = logging.getLogger(__name__)

DEFAULT_RESTORE_RETRY_S = 5.0


def _call_later(delay_s: float, callback: Callable[[], None]) -> Timer:
    return asyncio.get_running_loop().call_later(delay_s, callback)


def _tag_payload(event: Event) -> tuple[str, Value | None, Quality, int] | None:
    payload = event.payload
    path, value, quality, ts_ns = (
        payload.get("path"),
        payload.get("value"),
        payload.get("quality"),
        payload.get("ts_ns"),
    )
    if not isinstance(path, str) or not isinstance(quality, int) or not isinstance(ts_ns, int):
        return None
    if value is not None and not isinstance(value, float | bool | int):
        return None
    try:
        return path, value, Quality(quality), ts_ns
    except ValueError:
        return None


def _event(transition: AlarmTransition) -> Event:
    return Event(ALARM_CHANGED_PREFIX + transition.alarm_id, {"alarm": transition.snapshot})


class AlarmService:
    def __init__(
        self,
        bus: EventBus,
        tags: TagStorePort,
        store: AlarmStore,
        *,
        journal_max_rows: int = DEFAULT_MAX_ROWS,
        journal_flush_s: float = DEFAULT_FLUSH_S,
        restore_retry_s: float = DEFAULT_RESTORE_RETRY_S,
        schedule: Scheduler = _call_later,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._bus = bus
        self._store = store
        self._restore_retry_s = restore_retry_s
        self._engine = AlarmEngine(tags, schedule=schedule, on_timer=self._on_timer, clock=clock)
        self._writer = AlarmJournalWriter(store, max_rows=journal_max_rows, flush_s=journal_flush_s)
        self._restored = False
        self._pending: Definitions | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._unsubscribe: Callable[[], None] | None = None
        self._ack_lock = asyncio.Lock()

    # --- 수명주기 -------------------------------------------------------------------------
    async def start(self) -> None:
        self._unsubscribe = self._bus.subscribe(TAG_CHANGED_PREFIX + "*", self._on_tag)
        self._writer.start()
        self._spawn(self._restore())

    async def stop(self, timeout_s: float = DEFAULT_STOP_TIMEOUT_S) -> None:
        """수집이 멈춘 뒤 부른다: 구독 해제 → 남은 task·타이머 정리 → 저널 최종 쓰기(제한 시간)."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._engine.close()
        await self._writer.stop(timeout_s)

    @property
    def restored(self) -> bool:
        return self._restored

    def _spawn(self, coro: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _restore(self) -> None:
        warned = False
        while True:
            try:
                last = await self._store.last_states()
                break
            except AlarmUnavailableError as exc:
                if not warned:
                    log.warning("cannot restore alarm states (will retry): %s", exc)
                warned = True
            await asyncio.sleep(self._restore_retry_s)
        self._engine.restore(last)
        self._restored = True
        log.info("alarm states restored from journal (%d alarm(s))", len(last))
        if self._pending is not None:
            definitions, self._pending = self._pending, None
            await self._publish(self._emit(self._engine.set_definitions(definitions)))

    # --- 입력 -----------------------------------------------------------------------------
    def set_definitions(self, definitions: Definitions) -> None:
        """PlanKeeper `on_definitions`(동기). 복원 전이면 보류했다가 복원 직후 적용한다."""
        if not self._restored:
            self._pending = definitions
            return
        events = self._emit(self._engine.set_definitions(definitions))
        if events:
            self._spawn(self._publish(events))

    async def _on_tag(self, event: Event) -> list[Event]:
        if not self._restored:
            return []
        parsed = _tag_payload(event)
        if parsed is None:
            log.debug("ignoring malformed tag event %s", event.topic)
            return []
        return self._emit(self._engine.on_tag(*parsed))

    def _on_timer(self, transitions: list[AlarmTransition]) -> None:
        events = self._emit(transitions)
        if events:
            self._spawn(self._publish(events))

    def _emit(self, transitions: Sequence[AlarmTransition]) -> list[Event]:
        """저널 큐에 넣고(await 없음) 발행할 이벤트를 만든다."""
        self._writer.offer([JournalRow.of(t) for t in transitions])
        return [_event(t) for t in transitions]

    async def _publish(self, events: Sequence[Event]) -> None:
        for event in events:
            await self._bus.publish(event)

    # --- ACK ------------------------------------------------------------------------------
    async def ack(self, alarm_id: str, username: str) -> AlarmSnapshot:
        self._require_restored()
        async with self._ack_lock:
            planned = self._engine.plan_ack(alarm_id, username)
            entry = AuditEntry(
                action=AuditAction.ALARM_ACK,
                source=AuditSource.API,
                username=username,
                target=alarm_id,
                detail={
                    "tag_path": planned.tag_path,
                    "from_state": planned.from_state.value,
                    "to_state": planned.to_state.value,
                },
            )
            await self._store.append_acked([JournalRow.of(planned)], entry)
            events = self._settle([planned])
        await self._publish(events)
        return self._engine.get(alarm_id) or planned.snapshot

    async def ack_all(self, username: str) -> tuple[AlarmSnapshot, ...]:
        self._require_restored()
        async with self._ack_lock:
            planned = self._engine.plan_ack_all(username)
            if not planned:
                return ()
            entry = AuditEntry(
                action=AuditAction.ALARM_ACK_ALL,
                source=AuditSource.API,
                username=username,
                detail={"alarm_ids": [t.alarm_id for t in planned], "count": len(planned)},
            )
            await self._store.append_acked([JournalRow.of(t) for t in planned], entry)
            events = self._settle(planned)
        await self._publish(events)
        return tuple(self._engine.get(t.alarm_id) or t.snapshot for t in planned)

    def _settle(self, planned: Sequence[AlarmTransition]) -> list[Event]:
        """기록된 ACK를 엔진에 반영한다. 기록하는 사이 상태가 바뀌어 실제 전이가 다르면 그
        전이를 저널 큐에 더한다(기록된 ACK 행은 사용자의 확인 행위로 남는다)."""
        events: list[Event] = []
        for transition in planned:
            actual = self._engine.apply_ack(transition)
            if actual is None:
                continue
            if (actual.from_state, actual.to_state) != (transition.from_state, transition.to_state):
                events.extend(self._emit([actual]))
            else:
                events.append(_event(actual))
        return events

    def _require_restored(self) -> None:
        if not self._restored:
            raise AlarmUnavailableError("alarm states not restored yet (site-db unavailable?)")

    # --- 조회 -----------------------------------------------------------------------------
    def alarms(self) -> tuple[AlarmSnapshot, ...]:
        return self._engine.raised()

    async def journal(self, query: JournalQuery) -> tuple[JournalRecord, ...]:
        return await self._store.query(query)

    def status(self) -> AlarmStatus:
        active, unacked = self._engine.counts()
        writer = self._writer.status()
        return AlarmStatus(
            active_total=active,
            unacked_total=unacked,
            journal_queue_rows=writer.queue_rows,
            dropped_total=writer.dropped_total,
        )
