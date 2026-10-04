"""다른 모듈이 acquisition에 기대도 되는 표면(도메인 간 import는 ports만, PD-8)."""

from __future__ import annotations

from typing import Protocol


class CollectorSessionStatus(Protocol):
    @property
    def collector_connected(self) -> bool: ...
