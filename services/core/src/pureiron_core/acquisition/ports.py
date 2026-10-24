"""다른 모듈이 acquisition에 기대도 되는 표면(도메인 간 import는 ports만, PD-8).

장비 상태는 태그가 아니다(Device health is not a tag): collector `DeviceHealth`를 core
`DeviceMonitor`가 보관하고 `DeviceStatusSource`(`GET /devices`·WS `device_update`)로만 낸다(M3b D9).
상태가 바뀌면 버스에 `device.changed.<device_id>`가 나간다.

쓰기(plan M3c D12): `WriteChannel`은 collector 세션으로 `WriteRequest`를 보내고 `WriteResult`를
기다리는 전송로일 뿐이다 — 가드레일(선언·스위치·검사·감사·판정)은 `writes` 도메인이 한다.
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


# --- 쓰기 전송로(plan M3c D12) -------------------------------------------------------------


class WriteReplyStatus(StrEnum):
    """`WriteChannel.send` 결과. 앞 넷은 collector `WriteResult.Status` 그대로다.

    - `REJECTED`: 장비에 보내지 않았다(collector 거부, 또는 core가 보낼 세션이 없어 보내지 않음).
    - `INDETERMINATE`: collector가 보낸 뒤 결과를 모른다(모르는 status 값도 이것).
    - `NO_RESPONSE`: core가 응답을 받지 못했다(대기 시한 초과·세션 종료).
    """

    REJECTED = "REJECTED"
    OK = "OK"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"
    NO_RESPONSE = "NO_RESPONSE"


@dataclass(frozen=True, slots=True)
class WriteCommand:
    """collector에 보낼 쓰기 하나(`pb.WriteRequest`). bool은 `value` 0/1이다.

    `request_id`는 쓰기마다 새 값이어야 한다 — collector는 최근 id가 다시 오면 응답하지 않는다.
    `timeout_ms`(1..60000)는 collector가 처리한 순간부터 장비로 보내기 시작할 수 있는 기한이다.
    """

    request_id: str
    tag_id: int
    value: float
    plan_rev: int
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class WriteReply:
    status: WriteReplyStatus
    error: str = ""
    # OK일 때 collector가 같은 주소를 다시 읽은 값(bool은 0/1). 재읽기 실패면 None + error.
    readback: float | None = None
    readback_ts_ns: int | None = None


@dataclass(frozen=True, slots=True)
class WritePathState:
    """쓰기 실행 전 검사(D9)용 수집 쪽 사실 — 한 순간의 동기 스냅샷.

    - `sessions`: 열린 collector 세션 수.
    - `plan_rev`: core가 적용한 plan rev(아직 없으면 None).
    - `reported_plan_rev`: 세션이 정확히 하나일 때 그 세션이 마지막으로 보낸 live batch(heartbeat
      포함)의 plan_rev. collector는 plan 적용을 끝낸 뒤에야 새 rev로 batch를 보낸다(M3c-1 N3).
    - `device_connected`: 장비가 core 판정으로 CONNECTED인가.
    - `device_timeout_ms`: 현재 plan의 그 장비 `timeout_ms`(장비가 plan에 없으면 None).
    """

    sessions: int
    plan_rev: int | None
    reported_plan_rev: int | None
    device_connected: bool
    device_timeout_ms: int | None


class WriteChannel(Protocol):
    def write_path(self, device_id: str) -> WritePathState: ...

    async def send(self, command: WriteCommand, *, wait_s: float) -> WriteReply:
        """세션이 정확히 하나일 때만 보낸다(아니면 보내지 않고 REJECTED). `wait_s` 안에 결과가
        없거나 그 세션이 끝나면 NO_RESPONSE. 예외를 내지 않는다."""
        ...
