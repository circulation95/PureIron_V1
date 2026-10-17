"""다른 모듈이 acquisition에 기대도 되는 표면(도메인 간 import는 ports만, PD-8).

장비 상태는 태그가 아니다(Device health is not a tag): collector `DeviceHealth`를 core
`DeviceMonitor`가 보관하고 `DeviceStatusSource`(`GET /devices`·WS `device_update`)로만 낸다(M3b D9).
상태가 바뀌면 버스에 `device.changed.<device_id>`가 나간다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

DEVICE_CHANGED_PREFIX = "device.changed."


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


class DeviceState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """장비 하나의 통신 상태(plan 장비 + 마지막 collector `DeviceHealth`).

    `state`는 core 판정(CONNECTED = health가 connected이고 무신호가 아님), `silent`는 core 무신호
    판정이다. 나머지는 `pb.DeviceHealth` 필드이며 health를 아직 못 받았으면(또는 세션이 모두
    끊겨 버렸으면) None이다. `tag_count`는 plan 기준 태그 수.
    """

    device_id: str
    protocol: str
    state: DeviceState
    silent: bool
    tag_count: int
    connected: bool | None = None
    status: str | None = None
    last_success_unix_ns: int | None = None
    last_error: str | None = None
    last_error_at_unix_ns: int | None = None
    consecutive_failures: int | None = None
    poll_latency_ms: float | None = None
    read_success_rate: float | None = None
    poll_total: int | None = None
    timeout_count: int | None = None
    bad_quality_tag_count: int | None = None
    reconnect_count: int | None = None


class DeviceStatusSource(Protocol):
    def devices(self) -> tuple[DeviceSnapshot, ...]:
        """plan의 장비 전부(device_id 순)."""
        ...

    def device(self, device_id: str) -> DeviceSnapshot | None: ...
