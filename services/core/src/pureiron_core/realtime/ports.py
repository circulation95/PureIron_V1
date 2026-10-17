"""다른 모듈이 realtime에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만, plan M3b D10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RealtimeStatus:
    """`/readyz.realtime`(준비 판정에는 쓰지 않음): 지금 열린 인증된 WS 연결 수."""

    connections: int


class RealtimeStatusSource(Protocol):
    def status(self) -> RealtimeStatus: ...
