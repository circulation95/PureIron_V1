"""알람 상태 기계(plan M3b D7): 정의·태그 값 → 4상태 전이. 동기(await 없음)이고 전이를 돌려준다.

- 조건: above `value >= setpoint`, below `value <= setpoint`. 해제는 deadband를 적용한다:
  above `value < setpoint - deadband`, below `value > setpoint + deadband`(그 사이는 상태 유지 —
  채터링 방지).
- 전이: NORMAL·CLEARED_UNACKED --조건--> ACTIVE_UNACKED --ACK--> ACTIVE_ACKED --해제--> NORMAL,
  ACTIVE_UNACKED --해제--> CLEARED_UNACKED --ACK--> NORMAL. NORMAL·ACTIVE_ACKED는 ACK할 것이
  없다.
- `on_delay_s` > 0: 조건이 성립하면 타이머를 걸고(이미 걸려 있으면 그대로) 조건이 깨지면
  취소한다. 만료 시 TagStore 현재값을 다시 읽어 GOOD이고 조건이 여전할 때만 활성화한다(잠깐
  스친 값은 걸러진다).
- quality가 GOOD이 아니거나 값이 없거나 유한하지 않으면 평가를 보류한다(상태 유지) — 통신
  끊김이 "해제"로 보이지 않게. bool 태그는 0/1로 비교한다.
- 같은 태그의 알람은 above를 setpoint 오름차순, 이어서 below를 내림차순으로 평가한다(전이
  순서 결정적).
- 정의에서 빠진 알람은 RETIRED 전이를 하나 내고 제거한다. 기동 시 저널의 알람별 마지막 상태로
  시작한다(`restore` → 첫 `set_definitions`).

발행·저널 쓰기는 호출자(`service.py`)가 한다(Lock discipline과 같은 모양, D8). ACK는 DB
기록이 성공한 뒤에만 메모리에 반영해야 하므로 `plan_ack`(미리보기) → 기록 → `apply_ack` 두
단계로 나뉜다.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pureiron_core.alarms.definition import AlarmDefinition
from pureiron_core.alarms.ports import (
    AlarmNotAckableError,
    AlarmNotFoundError,
    AlarmSnapshot,
    AlarmState,
    AlarmTransition,
    AlarmType,
    JournalRecord,
    Priority,
)
from pureiron_core.project.ports import Definitions
from pureiron_core.tags.ports import Quality, TagStorePort, Value

log = logging.getLogger(__name__)

# ACK할 수 있는 상태 → ACK 뒤 상태.
_ACK_TARGET: dict[AlarmState, AlarmState] = {
    AlarmState.ACTIVE_UNACKED: AlarmState.ACTIVE_ACKED,
    AlarmState.CLEARED_UNACKED: AlarmState.NORMAL,
}
# 해제 조건이 성립했을 때 → 그 뒤 상태(ACK한 알람은 바로 NORMAL).
_CLEAR_TARGET: dict[AlarmState, AlarmState] = {
    AlarmState.ACTIVE_UNACKED: AlarmState.CLEARED_UNACKED,
    AlarmState.ACTIVE_ACKED: AlarmState.NORMAL,
}
_ACTIVATABLE = frozenset({AlarmState.NORMAL, AlarmState.CLEARED_UNACKED})
_PRIORITY_RANK = {Priority.CRITICAL: 0, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3}


class Timer(Protocol):
    """`asyncio.TimerHandle`의 쓰는 부분(테스트는 가짜 루프 타이머를 넣는다)."""

    def cancel(self) -> None: ...


type Scheduler = Callable[[float, Callable[[], None]], Timer]
type TimerSink = Callable[[list[AlarmTransition]], None]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _numeric(value: Value | None, quality: Quality) -> float | None:
    """평가할 수 있는 값(GOOD·유한 숫자)이면 float, 아니면 None(평가 보류)."""
    if quality is not Quality.GOOD or value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


@dataclass(slots=True)
class _Alarm:
    definition: AlarmDefinition
    tag_id: int | None
    state: AlarmState = AlarmState.NORMAL
    value: float | None = None
    value_ts_ns: int | None = None
    changed_at: datetime | None = None
    changed_by: str | None = None
    timer: Timer | None = None

    def snapshot(self) -> AlarmSnapshot:
        d = self.definition
        return AlarmSnapshot(
            alarm_id=d.alarm_id,
            tag_path=d.tag_path,
            kind=d.kind,
            setpoint=d.setpoint,
            deadband=d.deadband,
            on_delay_s=d.on_delay_s,
            priority=d.priority,
            description=d.description,
            state=self.state,
            value=self.value,
            value_ts_ns=self.value_ts_ns,
            changed_at=self.changed_at,
            changed_by=self.changed_by,
        )


class AlarmEngine:
    def __init__(
        self,
        tags: TagStorePort,
        *,
        schedule: Scheduler,
        on_timer: TimerSink,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._tags = tags
        self._schedule = schedule
        self._on_timer = on_timer
        self._clock = clock
        self._alarms: dict[str, _Alarm] = {}
        self._by_path: dict[str, list[_Alarm]] = {}
        self._restored: dict[str, JournalRecord] = {}

    # --- 수명주기 -------------------------------------------------------------------------
    def restore(self, last: Mapping[str, JournalRecord]) -> None:
        """저널의 알람별 마지막 행. 다음 `set_definitions`가 이 상태로 알람을 만든다."""
        self._restored = dict(last)

    def close(self) -> None:
        for alarm in self._alarms.values():
            self._cancel(alarm)

    # --- 정의 -----------------------------------------------------------------------------
    def set_definitions(self, definitions: Definitions) -> list[AlarmTransition]:
        tag_ids = {tag.path: tag.tag_id for tag in definitions.tags}
        parsed: dict[str, AlarmDefinition] = {}
        for raw in definitions.alarms:
            try:
                parsed[raw.alarm_id] = AlarmDefinition.parse(raw)
            except ValueError as exc:
                log.warning("alarm %s skipped (invalid definition): %s", raw.alarm_id, exc)
        transitions: list[AlarmTransition] = []
        for alarm_id in [a for a in self._alarms if a not in parsed]:
            transitions.append(self._retire(self._alarms.pop(alarm_id)))
        transitions.extend(self._retire_restored(parsed))
        touched: list[_Alarm] = []
        for alarm_id, definition in parsed.items():
            alarm = self._alarms.get(alarm_id)
            tag_id = tag_ids.get(definition.tag_path)
            if alarm is None:
                alarm = self._create(definition, tag_id)
                self._alarms[alarm_id] = alarm
            elif alarm.definition == definition and alarm.tag_id == tag_id:
                continue
            else:
                self._cancel(alarm)
                alarm.definition, alarm.tag_id = definition, tag_id
            touched.append(alarm)
        self._by_path = {}
        for alarm in sorted(self._alarms.values(), key=lambda a: a.definition.order_key()):
            self._by_path.setdefault(alarm.definition.tag_path, []).append(alarm)
        # 새·바뀐 알람은 지금 값으로 한 번 평가한다(값이 그대로인 태그는 tag.changed가 오지 않는다).
        for alarm in sorted(touched, key=lambda a: a.definition.order_key()):
            current = self._tags.get(alarm.tag_id) if alarm.tag_id is not None else None
            if current is not None:
                transitions.extend(
                    self._evaluate(alarm, current.value, current.quality, current.ts_ns)
                )
        return transitions

    def _create(self, definition: AlarmDefinition, tag_id: int | None) -> _Alarm:
        alarm = _Alarm(definition, tag_id)
        last = self._restored.pop(definition.alarm_id, None)
        state = _state_or_normal(last.to_state) if last is not None else AlarmState.NORMAL
        if last is not None and state is not AlarmState.RETIRED:
            alarm.state = state
            alarm.value, alarm.changed_at, alarm.changed_by = last.value, last.time, last.username
        return alarm

    def _retire_restored(self, parsed: Mapping[str, AlarmDefinition]) -> list[AlarmTransition]:
        """복원했지만 지금 정의에 없는 알람(core가 꺼진 동안 빠진 정의): 끝나지 않은 상태면
        RETIRED. 정의를 모르므로 이 전이 snapshot의 정의 필드(type·setpoint …)는 자리 채움이다."""
        transitions: list[AlarmTransition] = []
        for alarm_id in [a for a in self._restored if a not in parsed]:
            last = self._restored.pop(alarm_id)
            if last.to_state in (AlarmState.NORMAL.value, AlarmState.RETIRED.value):
                continue
            ghost = _Alarm(
                AlarmDefinition(alarm_id, last.tag_path, AlarmType.ABOVE, 0.0),
                None,
                state=_state_or_normal(last.to_state),
                value=last.value,
            )
            transitions.append(self._retire(ghost))
        return transitions

    def _retire(self, alarm: _Alarm) -> AlarmTransition:
        self._cancel(alarm)
        return self._transition(alarm, AlarmState.RETIRED, alarm.value, alarm.value_ts_ns)

    # --- 평가 -----------------------------------------------------------------------------
    def on_tag(
        self, path: str, value: Value | None, quality: Quality, ts_ns: int
    ) -> list[AlarmTransition]:
        transitions: list[AlarmTransition] = []
        for alarm in self._by_path.get(path, ()):
            transitions.extend(self._evaluate(alarm, value, quality, ts_ns))
        return transitions

    def _evaluate(
        self, alarm: _Alarm, value: Value | None, quality: Quality, ts_ns: int
    ) -> list[AlarmTransition]:
        number = _numeric(value, quality)
        if number is None:
            return []
        target = self._target(alarm, number)
        if target is None:
            return []
        return [self._transition(alarm, target, number, ts_ns)]

    def _target(self, alarm: _Alarm, number: float) -> AlarmState | None:
        """값 하나로 갈 상태(없으면 None). on_delay 타이머는 여기서 걸고 취소한다."""
        definition = alarm.definition
        if definition.active(number):
            if alarm.state not in _ACTIVATABLE:
                return None
            if definition.on_delay_s <= 0:
                return AlarmState.ACTIVE_UNACKED
            if alarm.timer is None:
                alarm.timer = self._schedule(definition.on_delay_s, lambda: self._expire(alarm))
            return None
        self._cancel(alarm)
        if definition.cleared(number):
            return _CLEAR_TARGET.get(alarm.state)
        return None

    def _expire(self, alarm: _Alarm) -> None:
        """on_delay 만료(타이머 콜백): 지금 값으로 다시 확인하고 전이가 있으면 sink로 넘긴다."""
        if alarm.timer is None or self._alarms.get(alarm.definition.alarm_id) is not alarm:
            return
        alarm.timer = None
        if alarm.state not in _ACTIVATABLE or alarm.tag_id is None:
            return
        current = self._tags.get(alarm.tag_id)
        if current is None:
            return
        number = _numeric(current.value, current.quality)
        if number is None or not alarm.definition.active(number):
            return
        self._on_timer([self._transition(alarm, AlarmState.ACTIVE_UNACKED, number, current.ts_ns)])

    def _cancel(self, alarm: _Alarm) -> None:
        if alarm.timer is not None:
            alarm.timer.cancel()
            alarm.timer = None

    def _transition(
        self,
        alarm: _Alarm,
        to_state: AlarmState,
        value: float | None,
        value_ts_ns: int | None,
        *,
        username: str | None = None,
        time: datetime | None = None,
    ) -> AlarmTransition:
        from_state = alarm.state
        alarm.state = to_state
        alarm.value, alarm.value_ts_ns = value, value_ts_ns
        alarm.changed_at = time or self._clock()
        alarm.changed_by = username
        return AlarmTransition(
            alarm_id=alarm.definition.alarm_id,
            tag_path=alarm.definition.tag_path,
            from_state=from_state,
            to_state=to_state,
            value=value,
            time=alarm.changed_at,
            username=username,
            snapshot=alarm.snapshot(),
        )

    # --- ACK ------------------------------------------------------------------------------
    def plan_ack(self, alarm_id: str, username: str) -> AlarmTransition:
        """ACK 전이를 미리 계산한다(상태는 바꾸지 않는다 — DB 기록이 성공한 뒤 `apply_ack`)."""
        alarm = self._alarms.get(alarm_id)
        if alarm is None:
            raise AlarmNotFoundError(alarm_id)
        target = _ACK_TARGET.get(alarm.state)
        if target is None:
            raise AlarmNotAckableError(alarm_id, alarm.state)
        return self._preview(alarm, target, username)

    def plan_ack_all(self, username: str) -> list[AlarmTransition]:
        return [
            self._preview(alarm, target, username)
            for alarm in self._alarms.values()
            if (target := _ACK_TARGET.get(alarm.state)) is not None
        ]

    def apply_ack(self, planned: AlarmTransition) -> AlarmTransition | None:
        """기록된 ACK를 지금 상태에 반영한다. 기록하는 사이 상태가 바뀌었으면(해제·재발생) 지금
        상태 기준의 ACK 전이를 돌려준다 — 호출자는 `planned`와 다르면 그 전이를 저널에 더한다.
        그 사이 ACK할 것이 없어졌거나 알람이 빠졌으면 None."""
        alarm = self._alarms.get(planned.alarm_id)
        target = _ACK_TARGET.get(alarm.state) if alarm is not None else None
        if alarm is None or target is None:
            return None
        return self._transition(
            alarm,
            target,
            alarm.value,
            alarm.value_ts_ns,
            username=planned.username,
            time=planned.time,
        )

    def _preview(self, alarm: _Alarm, target: AlarmState, username: str) -> AlarmTransition:
        now = self._clock()
        after = _Alarm(
            alarm.definition,
            alarm.tag_id,
            state=target,
            value=alarm.value,
            value_ts_ns=alarm.value_ts_ns,
            changed_at=now,
            changed_by=username,
        )
        return AlarmTransition(
            alarm_id=alarm.definition.alarm_id,
            tag_path=alarm.definition.tag_path,
            from_state=alarm.state,
            to_state=target,
            value=alarm.value,
            time=now,
            username=username,
            snapshot=after.snapshot(),
        )

    # --- 조회 -----------------------------------------------------------------------------
    def get(self, alarm_id: str) -> AlarmSnapshot | None:
        alarm = self._alarms.get(alarm_id)
        return alarm.snapshot() if alarm is not None else None

    def raised(self) -> tuple[AlarmSnapshot, ...]:
        """NORMAL이 아닌 알람: 우선순위 높은 것 → 최근 전이 순."""
        alarms = [a.snapshot() for a in self._alarms.values() if a.state is not AlarmState.NORMAL]
        alarms.sort(key=lambda a: a.changed_at.timestamp() if a.changed_at else 0.0, reverse=True)
        alarms.sort(key=lambda a: _PRIORITY_RANK[a.priority])
        return tuple(alarms)

    def counts(self) -> tuple[int, int]:
        """(활성 수, 미확인 수)."""
        states = [a.state for a in self._alarms.values()]
        active = sum(s in (AlarmState.ACTIVE_UNACKED, AlarmState.ACTIVE_ACKED) for s in states)
        unacked = sum(s in _ACK_TARGET for s in states)
        return active, unacked


def _state_or_normal(raw: str) -> AlarmState:
    try:
        return AlarmState(raw)
    except ValueError:
        return AlarmState.NORMAL
