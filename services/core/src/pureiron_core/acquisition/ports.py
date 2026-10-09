"""다른 모듈이 acquisition에 기대도 되는 표면(도메인 간 import는 ports만, PD-8)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class CollectorSessionStatus(Protocol):
    @property
    def collector_connected(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class IngestCounts:
    """ingest 누적 샘플 수(core 기동 후). live만 현재값에 반영, 나머지는 이력 전용."""

    live: int = 0
    stale: int = 0
    future: int = 0
    replay: int = 0
    # 받은 ValueBatch 수(heartbeat 포함)와 그중 heartbeat(빈 batch) 수 — 부하 측정의 batch/s.
    batches: int = 0
    heartbeats: int = 0


@dataclass(frozen=True, slots=True)
class AcquisitionSummary:
    """수집 상태 집계(`/readyz`, Updater `checking`의 GOOD 비율). 태그 값은 담지 않는다."""

    plan_rev: int | None
    devices_total: int
    devices_connected: int
    tags_total: int
    tags_good: int
    ingest: IngestCounts = IngestCounts()

    @property
    def good_ratio(self) -> float | None:
        return self.tags_good / self.tags_total if self.tags_total else None


class AcquisitionStatus(CollectorSessionStatus, Protocol):
    def summary(self) -> AcquisitionSummary: ...
