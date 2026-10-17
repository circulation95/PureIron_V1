"""장비 통신 상태(CONNECTED/DISCONNECTED)와 무신호 타이머.

장비 상태는 태그가 아니다 — collector `DeviceHealth`(device_name = device_id)가 단일
소스이고, core는 여기에 무신호 판정만 더한다. collector는 통신이 끊긴 순간 한 번만 BAD를
보내므로(core 단절 중 생긴 BAD는 replay로만 옴) BAD/DISCONNECTED 유지는 core가 판정한다:

- live 샘플이 `max(3 * poll_ms, 5s)` 동안 없으면 → DISCONNECTED, 그 장비 태그 BAD.
- `DeviceHealth.connected=False` → 즉시 DISCONNECTED, 태그 BAD.
- 세션이 모두 끊기면(`disconnect_all`) → 전 장비 DISCONNECTED, 전 태그 BAD,
  마지막 health는 버린다.
- CONNECTED는 마지막 health가 connected이고 무신호가 아닐 때만.

상태 변경은 동기 메서드에서 끝내고 발행할 이벤트를 돌려준다(TagStore와 같은 규율).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Literal

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.acquisition.ports import DEVICE_CHANGED_PREFIX, DeviceSnapshot, DeviceState
from pureiron_core.platform.bus import Event
from pureiron_core.tags.ports import TagStorePort

SILENCE_POLLS = 3


@dataclass(slots=True)
class _Device:
    device_id: str
    protocol: str
    silence_s: float
    tag_ids: tuple[int, ...]
    last_signal: float
    state: DeviceState = DeviceState.DISCONNECTED
    health: pb.DeviceHealth | None = None
    silent: bool = False


@dataclass(frozen=True, slots=True)
class DeviceSummary:
    total: int
    connected: int


class DeviceMonitor:
    def __init__(
        self,
        tags: TagStorePort,
        *,
        silence_min_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        wall_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._tags = tags
        self._silence_min_s = silence_min_s
        self._clock = clock
        self._wall_ns = wall_ns
        self._devices: dict[str, _Device] = {}
        self._tag_device: dict[int, str] = {}

    # --- plan ----------------------------------------------------------------------------
    def reindex(self, plan: pb.AcquisitionPlan) -> None:
        """plan의 장비 집합으로 맞춘다.

        남는 장비는 상태·health·무신호 여부를 유지하고 타이머는 지금부터 다시 센다(무신호였던
        장비가 plan 교체만으로 CONNECTED로 깜빡이지 않게 — 회복은 실제 샘플로만).
        """
        tag_ids: dict[str, list[int]] = {}
        for tag in plan.tags:
            tag_ids.setdefault(tag.device_id, []).append(tag.tag_id)
        now = self._clock()
        devices: dict[str, _Device] = {}
        for device in plan.devices:
            poll_ms = device.poll_ms or plan.default_poll_ms
            silence_s = max(SILENCE_POLLS * poll_ms / 1000, self._silence_min_s)
            old = self._devices.get(device.device_id)
            devices[device.device_id] = _Device(
                device_id=device.device_id,
                protocol=device.protocol,
                silence_s=silence_s,
                tag_ids=tuple(tag_ids.get(device.device_id, ())),
                last_signal=now,
                state=old.state if old else DeviceState.DISCONNECTED,
                health=old.health if old else None,
                silent=old.silent if old else False,
            )
        self._devices = devices
        self._tag_device = {
            tag_id: device_id for device_id, device in devices.items() for tag_id in device.tag_ids
        }

    # --- 입력 ----------------------------------------------------------------------------
    def seen(self, tag_ids: Iterable[int]) -> list[Event]:
        """live 샘플을 받은 태그들 → 그 장비의 무신호 타이머를 되돌린다."""
        now = self._clock()
        touched: set[str] = set()
        for tag_id in tag_ids:
            device_id = self._tag_device.get(tag_id)
            if device_id is not None:
                touched.add(device_id)
        events: list[Event] = []
        for device_id in touched:
            device = self._devices[device_id]
            device.last_signal = now
            device.silent = False
            events.extend(self._evaluate(device, "signal"))
        return events

    def on_health(self, health: pb.DeviceHealth) -> list[Event]:
        device = self._devices.get(health.device_name)
        if device is None:
            return []  # 이전 plan의 장비(plan 교체 직후) — 무시
        device.health = health
        return self._evaluate(device, "health", mark_bad=not health.connected)

    def check(self) -> list[Event]:
        """무신호 판정(주기 호출). 태그가 없는 장비는 샘플이 없으므로 health만 본다."""
        now = self._clock()
        events: list[Event] = []
        for device in self._devices.values():
            if device.tag_ids and not device.silent and now - device.last_signal > device.silence_s:
                device.silent = True
                events.extend(self._evaluate(device, "silence", mark_bad=True))
        return events

    def disconnect_all(self) -> list[Event]:
        """collector 세션이 하나도 없다 — 모든 장비를 끊긴 것으로 본다."""
        events: list[Event] = []
        for device in self._devices.values():
            device.health = None
            events.extend(self._evaluate(device, "session_closed", mark_bad=True))
        return events

    # --- 판정 ----------------------------------------------------------------------------
    def _evaluate(self, device: _Device, reason: str, *, mark_bad: bool = False) -> list[Event]:
        """상태를 다시 판정한다. `mark_bad`면 상태 전이와 무관하게 그 장비 태그를 BAD로 둔다
        (health를 아직 못 받아 이미 DISCONNECTED인 장비도 무신호면 태그는 BAD여야 한다)."""
        connected = device.health is not None and device.health.connected and not device.silent
        state = DeviceState.CONNECTED if connected else DeviceState.DISCONNECTED
        events: list[Event] = []
        if state is not device.state:
            device.state = state
            events.append(
                Event(
                    DEVICE_CHANGED_PREFIX + device.device_id,
                    {"device_id": device.device_id, "state": state.value, "reason": reason},
                )
            )
        if mark_bad:
            events.extend(self._tags.commit_bad(device.tag_ids, self._wall_ns()))
        return events

    # --- 조회 ----------------------------------------------------------------------------
    def state(self, device_id: str) -> DeviceState | None:
        device = self._devices.get(device_id)
        return device.state if device else None

    def health(self, device_id: str) -> pb.DeviceHealth | None:
        device = self._devices.get(device_id)
        return device.health if device else None

    def devices(self) -> tuple[DeviceSnapshot, ...]:
        return tuple(_snapshot(self._devices[d]) for d in sorted(self._devices))

    def device(self, device_id: str) -> DeviceSnapshot | None:
        device = self._devices.get(device_id)
        return _snapshot(device) if device is not None else None

    def summary(self) -> DeviceSummary:
        connected = sum(1 for d in self._devices.values() if d.state is DeviceState.CONNECTED)
        return DeviceSummary(total=len(self._devices), connected=connected)


def _snapshot(device: _Device) -> DeviceSnapshot:
    base = DeviceSnapshot(
        device_id=device.device_id,
        protocol=device.protocol,
        state=device.state,
        silent=device.silent,
        tag_count=len(device.tag_ids),
    )
    health = device.health
    if health is None:
        return base
    return replace(
        base,
        connected=health.connected,
        status=health.status,
        last_success_unix_ns=_opt(health, "last_success_unix_ns"),
        last_error=health.last_error if health.HasField("last_error") else None,
        last_error_at_unix_ns=_opt(health, "last_error_at_unix_ns"),
        consecutive_failures=health.consecutive_failures,
        poll_latency_ms=health.poll_latency_ms if health.HasField("poll_latency_ms") else None,
        read_success_rate=(
            health.read_success_rate if health.HasField("read_success_rate") else None
        ),
        poll_total=health.poll_total,
        timeout_count=health.timeout_count,
        bad_quality_tag_count=health.bad_quality_tag_count,
        reconnect_count=health.reconnect_count,
    )


def _opt(
    health: pb.DeviceHealth, field: Literal["last_success_unix_ns", "last_error_at_unix_ns"]
) -> int | None:
    """proto3 optional 시각(값이 없으면 None — 0과 구별)."""
    return getattr(health, field) if health.HasField(field) else None
