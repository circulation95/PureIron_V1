"""알람 정의 하나(`alarms/*.yaml` 항목 + 스키마 기본값)와 조건 판정(plan M3b D6·D7)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from pureiron_core.alarms.ports import AlarmType, Priority
from pureiron_core.project.ports import AlarmDef


@dataclass(frozen=True, slots=True)
class AlarmDefinition:
    """`alarms/*.yaml` 항목에 스키마 기본값을 채운 것(deadband 0, on_delay_s 0, priority medium)."""

    alarm_id: str
    tag_path: str
    kind: AlarmType
    setpoint: float
    deadband: float = 0.0
    on_delay_s: float = 0.0
    priority: Priority = Priority.MEDIUM
    description: str | None = None

    @classmethod
    def parse(cls, alarm: AlarmDef) -> AlarmDefinition:
        """project-tools가 검증한 정의를 읽는다. 어긋나면 ValueError(호출자가 그 알람만 뺀다)."""
        data = alarm.definition
        description = data.get("description")
        return cls(
            alarm_id=alarm.alarm_id,
            tag_path=alarm.tag_path,
            kind=AlarmType(str(data.get("type"))),
            setpoint=_number(data.get("setpoint"), "setpoint"),
            deadband=_number(data.get("deadband", 0), "deadband"),
            on_delay_s=_number(data.get("on_delay_s", 0), "on_delay_s"),
            priority=Priority(str(data.get("priority", Priority.MEDIUM.value))),
            description=description if isinstance(description, str) else None,
        )

    def active(self, value: float) -> bool:
        return value >= self.setpoint if self.kind is AlarmType.ABOVE else value <= self.setpoint

    def cleared(self, value: float) -> bool:
        if self.kind is AlarmType.ABOVE:
            return value < self.setpoint - self.deadband
        return value > self.setpoint + self.deadband

    def order_key(self) -> tuple[int, float, str]:
        """above 오름차순 → below 내림차순(같은 태그 안 평가 순서)."""
        if self.kind is AlarmType.ABOVE:
            return (0, self.setpoint, self.alarm_id)
        return (1, -self.setpoint, self.alarm_id)


def _number(raw: object, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
        raise ValueError(f"{name} must be a finite number, got {raw!r}")
    return float(raw)
