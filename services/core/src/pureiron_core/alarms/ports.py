"""다른 모듈이 alarms에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만, plan M3b D7·D8).

알람의 현재 상태는 core 알람 엔진(메모리)이 정본이고, 저널(`alarm_journal`)은 이력·감사다
(architecture SOT: AlarmJournal is audit/history not current state). 상태가 바뀔 때마다 버스에
`alarm.changed.<alarm_id>` 이벤트가 나가고 payload `alarm`에 바뀐 뒤의 `AlarmSnapshot`이 실린다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

ALARM_CHANGED_PREFIX = "alarm.changed."
DEFAULT_JOURNAL_ROWS = 100
MAX_JOURNAL_ROWS = 1000


class AlarmState(StrEnum):
    """4상태(D7) + 저널·방송 전용 `RETIRED`(정의에서 빠져 엔진에서 사라진 알람)."""

    NORMAL = "NORMAL"
    ACTIVE_UNACKED = "ACTIVE_UNACKED"
    ACTIVE_ACKED = "ACTIVE_ACKED"
    CLEARED_UNACKED = "CLEARED_UNACKED"
    RETIRED = "RETIRED"


class AlarmType(StrEnum):
    ABOVE = "above"
    BELOW = "below"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class AlarmSnapshot:
    """알람 하나의 정의 + 현재 상태.

    `value`·`value_ts_ns`는 마지막 전이를 만든 값과 그 샘플 시각이다(ACK는 값을 바꾸지 않는다,
    복원한 알람은 시각을 모른다). `changed_at`·`changed_by`는 마지막 전이 시각과 그 전이를 한
    사용자(조건 변화로 생긴 전이는 None). 한 번도 전이하지 않은 알람은 둘 다 None.
    """

    alarm_id: str
    tag_path: str
    kind: AlarmType
    setpoint: float
    deadband: float
    on_delay_s: float
    priority: Priority
    description: str | None
    state: AlarmState
    value: float | None
    value_ts_ns: int | None
    changed_at: datetime | None
    changed_by: str | None


@dataclass(frozen=True, slots=True)
class AlarmTransition:
    """상태 전이 하나 = 저널 한 행 + 이벤트 하나. `snapshot`은 전이 뒤 상태."""

    alarm_id: str
    tag_path: str
    from_state: AlarmState
    to_state: AlarmState
    value: float | None
    time: datetime
    username: str | None
    snapshot: AlarmSnapshot


@dataclass(frozen=True, slots=True)
class JournalQuery:
    """조회 조건. 시간 범위는 `[start, end)`, 결과는 최신순, `limit`은 1~MAX_JOURNAL_ROWS."""

    alarm_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    limit: int = DEFAULT_JOURNAL_ROWS


@dataclass(frozen=True, slots=True)
class JournalRecord:
    """저장된 저널 한 행. 상태는 문자열 그대로(이후 버전이 더한 값도 읽을 수 있게)."""

    id: int
    time: datetime
    alarm_id: str
    tag_path: str
    from_state: str
    to_state: str
    value: float | None
    username: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class AlarmStatus:
    """`/readyz.alarms`(준비 판정에는 쓰지 않음). 누적 값은 core 기동 후 기준.

    - `active_total` = ACTIVE_UNACKED + ACTIVE_ACKED.
    - `unacked_total` = ACTIVE_UNACKED + CLEARED_UNACKED.
    - `journal_queue_rows`: 아직 DB에 쓰지 못한 저널 행 수.
    - `dropped_total`: 큐 상한을 넘어 버린(가장 오래된) 저널 행 수.
    """

    active_total: int
    unacked_total: int
    journal_queue_rows: int
    dropped_total: int


class AlarmError(Exception):
    pass


class AlarmNotFoundError(AlarmError):
    def __init__(self, alarm_id: str) -> None:
        super().__init__(f"alarm {alarm_id!r} not found")
        self.alarm_id = alarm_id


class AlarmNotAckableError(AlarmError):
    """ACK할 것이 없는 상태(NORMAL·ACTIVE_ACKED) — 다른 사용자가 먼저 ACK했거나 화면이 오래됐다."""

    def __init__(self, alarm_id: str, state: AlarmState) -> None:
        super().__init__(f"alarm {alarm_id!r} is {state.value}; nothing to acknowledge")
        self.alarm_id = alarm_id
        self.state = state


class AlarmUnavailableError(AlarmError):
    """site-db 문제로 저널을 읽거나 ACK를 기록하지 못했다(API는 503)."""


class AlarmStatusSource(Protocol):
    def status(self) -> AlarmStatus: ...


class AlarmPort(AlarmStatusSource, Protocol):
    """HTTP 라우터가 알람을 다루는 표면."""

    def alarms(self) -> tuple[AlarmSnapshot, ...]:
        """NORMAL이 아닌 알람(활성 + 해제됐지만 미확인). 우선순위 높은 것·최근 전이 순."""
        ...

    async def journal(self, query: JournalQuery) -> tuple[JournalRecord, ...]: ...

    async def ack(self, alarm_id: str, username: str) -> AlarmSnapshot:
        """`AlarmNotFoundError`·`AlarmNotAckableError`·`AlarmUnavailableError`."""
        ...

    async def ack_all(self, username: str) -> tuple[AlarmSnapshot, ...]:
        """ACK할 수 있는 알람 전부를 한 트랜잭션으로 ACK하고 그 결과를 돌려준다(없으면 빈 값)."""
        ...
