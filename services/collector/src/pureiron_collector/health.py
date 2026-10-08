"""장비 1개의 통신 상태(plan §6.1 `DeviceHealth`, legacy `drivers/health.py` 규칙 이식).

장비 상태는 태그가 아니다 — core는 이 메시지를 장비 상태의 단일 소스로 쓴다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pureiron_collector._gen import collector_pb2 as pb


def _now_ns() -> int:
    return time.time_ns()


def is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


@dataclass(slots=True)
class DeviceHealth:
    device_id: str
    protocol: str
    tag_count: int = 0
    connected: bool = False
    last_success_ns: int | None = None
    last_error: str | None = None
    last_error_ns: int | None = None
    consecutive_failures: int = 0
    poll_latency_ms: float | None = None
    bad_quality_tag_count: int = 0
    poll_total: int = 0
    poll_ok: int = 0
    timeout_count: int = 0
    reconnect_count: int = 0

    @property
    def read_success_rate(self) -> float | None:
        """한 번도 poll 하지 않았으면 None(no_data) — 1.0으로 보이지 않게 한다."""
        return self.poll_ok / self.poll_total if self.poll_total else None

    @property
    def status(self) -> str:
        if self.connected:
            return "online"
        if self.consecutive_failures or self.last_error:
            return "error"
        return "offline"

    def record_success(self, latency_ms: float, bad_quality_tags: int) -> None:
        if not self.connected and self.consecutive_failures:
            self.reconnect_count += 1
        self.connected = True
        self.consecutive_failures = 0
        self.last_success_ns = _now_ns()
        self.poll_latency_ms = latency_ms
        self.bad_quality_tag_count = bad_quality_tags
        # 회복 뒤에 지난 오류가 계속 보이지 않게 지운다.
        self.last_error = None
        self.last_error_ns = None
        self.poll_ok += 1
        self.poll_total += 1

    def record_failure(self, exc: BaseException) -> None:
        self.connected = False
        self.last_error = str(exc) or type(exc).__name__
        self.last_error_ns = _now_ns()
        self.consecutive_failures += 1
        self.bad_quality_tag_count = self.tag_count
        self.poll_total += 1
        if is_timeout(exc):
            self.timeout_count += 1

    def to_proto(self) -> pb.DeviceHealth:
        message = pb.DeviceHealth(
            device_name=self.device_id,
            protocol=self.protocol,
            status=self.status,
            connected=self.connected,
            consecutive_failures=self.consecutive_failures,
            tag_count=self.tag_count,
            poll_total=self.poll_total,
            timeout_count=self.timeout_count,
            bad_quality_tag_count=self.bad_quality_tag_count,
            reconnect_count=self.reconnect_count,
        )
        if self.last_success_ns is not None:
            message.last_success_unix_ns = self.last_success_ns
        if self.last_error is not None:
            message.last_error = self.last_error
        if self.last_error_ns is not None:
            message.last_error_at_unix_ns = self.last_error_ns
        if self.poll_latency_ms is not None:
            message.poll_latency_ms = self.poll_latency_ms
        rate = self.read_success_rate
        if rate is not None:
            message.read_success_rate = rate
        return message
