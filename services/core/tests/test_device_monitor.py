"""장비 무신호 타이머 max(3*poll_ms, 5s)·health·세션 종료 → DISCONNECTED/BAD."""

from __future__ import annotations

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.acquisition.devices import DeviceMonitor, DeviceState
from pureiron_core.acquisition.plan import build_plan
from pureiron_core.project.ports import Definitions, DeviceDef
from pureiron_core.tags.ports import Quality, Reading
from pureiron_core.tags.store import TagStore

from .conftest import sample_definitions


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def ns(self) -> int:
        return int(self.now * 1e9)


def _monitor(defs: Definitions | None = None) -> tuple[DeviceMonitor, TagStore, Clock]:
    clock = Clock()
    tags = TagStore()
    monitor = DeviceMonitor(tags, silence_min_s=5.0, clock=clock, wall_ns=clock.ns)
    built = build_plan(defs or sample_definitions())
    tags.reindex(built.keys)
    monitor.reindex(built.plan)
    return monitor, tags, clock


def _health(connected: bool, device: str = "plc-1") -> pb.DeviceHealth:
    return pb.DeviceHealth(device_name=device, protocol="modbus_tcp", connected=connected)


def _good(tags: TagStore, clock: Clock) -> None:
    ts = clock.ns()
    tags.commit([Reading(1, 1.0, Quality.GOOD, ts), Reading(2, True, Quality.GOOD, ts)], ts)


def test_connected_needs_health_and_signal() -> None:
    monitor, tags, clock = _monitor()
    assert monitor.state("plc-1") is DeviceState.DISCONNECTED
    _good(tags, clock)
    assert monitor.seen([1, 2]) == []  # health 전에는 DISCONNECTED 유지(태그는 건드리지 않음)
    assert tags.summary().good == 2
    events = monitor.on_health(_health(True))
    assert [e.topic for e in events] == ["device.changed.plc-1"]
    assert events[0].payload["state"] == "connected"
    assert monitor.summary().connected == 1


def test_silence_after_5s_minimum_marks_bad_once() -> None:
    monitor, tags, clock = _monitor()  # poll 1000ms → 3s < 최소 5s
    monitor.on_health(_health(True))
    _good(tags, clock)
    monitor.seen([1, 2])
    clock.now += 5.0
    assert monitor.check() == []  # 경계: 5s를 넘어야 무신호
    clock.now += 0.1
    events = monitor.check()
    topics = [e.topic for e in events]
    assert topics[0] == "device.changed.plc-1"
    assert sorted(topics[1:]) == ["tag.changed.line1/running", "tag.changed.line1/temp"]
    assert monitor.state("plc-1") is DeviceState.DISCONNECTED
    assert tags.summary().good == 0
    clock.now += 10
    assert monitor.check() == []  # 전이는 한 번만

    # 샘플이 다시 오면 회복(health는 여전히 connected)
    _good(tags, clock)
    events = monitor.seen([1])
    assert [e.payload["state"] for e in events] == ["connected"]


def test_silence_uses_three_polls_when_longer() -> None:
    device = {"id": "plc-1", "protocol": "modbus_tcp", "host": "h", "port": 502, "poll_ms": 4000}
    defs = Definitions(rev=1, devices=(DeviceDef("plc-1", device),), tags=sample_definitions().tags)
    monitor, tags, clock = _monitor(defs)
    monitor.on_health(_health(True))
    _good(tags, clock)
    monitor.seen([1, 2])
    clock.now += 11.9
    assert monitor.check() == []
    clock.now += 0.2
    assert monitor.check() != []


def test_silence_marks_bad_even_without_health() -> None:
    """health를 아직 못 받아 이미 DISCONNECTED인 장비도 무신호면 태그는 BAD."""
    monitor, tags, clock = _monitor()
    _good(tags, clock)
    monitor.seen([1, 2])
    clock.now += 6
    events = monitor.check()
    assert len(events) == 2  # 상태 전이 없음, 태그 BAD 2건
    assert tags.summary().good == 0


def test_health_disconnected_is_immediate() -> None:
    monitor, tags, clock = _monitor()
    monitor.on_health(_health(True))
    _good(tags, clock)
    monitor.seen([1, 2])
    events = monitor.on_health(_health(False))
    assert events[0].payload == {"device_id": "plc-1", "state": "disconnected", "reason": "health"}
    assert tags.summary().good == 0
    assert monitor.on_health(_health(False)) == []  # 반복 health는 이벤트 없음
    assert monitor.on_health(_health(True, device="other")) == []  # plan에 없는 장비


def test_disconnect_all_drops_health() -> None:
    monitor, tags, clock = _monitor()
    monitor.on_health(_health(True))
    _good(tags, clock)
    monitor.seen([1, 2])
    events = monitor.disconnect_all()
    assert events[0].payload["reason"] == "session_closed"
    assert monitor.summary().connected == 0
    assert tags.summary().good == 0
    assert monitor.health("plc-1") is None
    # 재연결 뒤 새 샘플만으로는 CONNECTED가 되지 않는다(이전 세션의 health를 믿지 않음)
    clock.now += 1
    _good(tags, clock)
    assert monitor.seen([1]) == []
    assert monitor.state("plc-1") is DeviceState.DISCONNECTED


def test_reindex_keeps_silence() -> None:
    """무신호였던 장비가 plan 교체만으로 CONNECTED로 깜빡이지 않는다(회복은 실제 샘플로만)."""
    monitor, tags, clock = _monitor()
    monitor.on_health(_health(True))
    _good(tags, clock)
    monitor.seen([1, 2])
    clock.now += 6
    monitor.check()
    assert monitor.state("plc-1") is DeviceState.DISCONNECTED
    monitor.reindex(build_plan(sample_definitions(rev=4)).plan)
    assert monitor.on_health(_health(True)) == []
    assert monitor.state("plc-1") is DeviceState.DISCONNECTED
    _good(tags, clock)
    assert [e.payload["state"] for e in monitor.seen([1])] == ["connected"]
