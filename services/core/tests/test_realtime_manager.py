"""realtime(D8·D10): 태그 합치기·구독 즉시 현재값·구독 해제·방송·큐 초과 1013·NaN null, 연결 루프의
hello·배치·ping/pong 4408·세션 재검증 4401·정지 1001.

legacy `backend/tests/test_websocket.py` 이식분은 테스트 이름 옆 주석에 원래 이름을 적는다(K11).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import cast

import pytest

from pureiron_core.acquisition.ports import DeviceSnapshot, DeviceState
from pureiron_core.alarms.ports import AlarmSnapshot, AlarmState, AlarmType, Priority
from pureiron_core.auth.ports import Principal, Role
from pureiron_core.platform.bus import Event, EventBus
from pureiron_core.realtime.manager import CloseCode, ConnectionManager
from pureiron_core.realtime.service import RealtimeService, TransportClosedError
from pureiron_core.tags.ports import json_value
from pureiron_core.tags.store import TagStore

from .alarm_fakes import T0, TS0_NS, eventually, set_value, tag_store
from .auth_fakes import auth_fixture

PRINCIPAL = Principal(user_id=1, username="kim", role=Role.OPERATOR, display_name=None)
TAGS = {"a/one": 1, "a/two": 2}

type Json = dict[str, object]


@dataclass
class FakeDevices:
    snapshots: dict[str, DeviceSnapshot] = field(default_factory=dict[str, DeviceSnapshot])

    def devices(self) -> tuple[DeviceSnapshot, ...]:
        return tuple(self.snapshots[d] for d in sorted(self.snapshots))

    def device(self, device_id: str) -> DeviceSnapshot | None:
        return self.snapshots.get(device_id)


@dataclass
class FakeTransport:
    """WS 한 연결 대역: `inbox`에 넣은 텍스트를 받고(None = 상대가 닫음), 보낸 것을 모은다."""

    inbox: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue[str | None])
    sent: list[Json] = field(default_factory=list[Json])
    closed: int | None = None

    async def send(self, text: str) -> None:
        if self.closed is not None:
            raise TransportClosedError("closed")
        self.sent.append(json.loads(text))

    async def receive(self) -> str:
        text = await self.inbox.get()
        if text is None:
            raise TransportClosedError("peer closed")
        return text

    async def close(self, code: int) -> None:
        self.closed = code

    def types(self) -> list[object]:
        return [m["type"] for m in self.sent]

    def tag_updates(self) -> list[list[Json]]:
        return [_values(m) for m in self.sent if m["type"] == "tag_update"]


def _values(message: Json) -> list[Json]:
    return cast("list[Json]", message["values"])


def _tag_event(store: TagStore, tag_id: int) -> Event:
    tag = store.get(tag_id)
    assert tag is not None
    return Event(
        "tag.changed." + tag.path,
        {
            "tag_id": tag.tag_id,
            "path": tag.path,
            "device_id": tag.device_id,
            "value": json_value(tag.value),
            "quality": int(tag.quality),
            "ts_ns": tag.ts_ns,
        },
    )


def _alarm(state: AlarmState = AlarmState.ACTIVE_UNACKED) -> AlarmSnapshot:
    return AlarmSnapshot(
        alarm_id="tank-high",
        tag_path="a/one",
        kind=AlarmType.ABOVE,
        setpoint=8.0,
        deadband=0.0,
        on_delay_s=0.0,
        priority=Priority.HIGH,
        description=None,
        state=state,
        value=9.0,
        value_ts_ns=TS0_NS,
        changed_at=T0,
        changed_by=None,
    )


# --- manager ------------------------------------------------------------------------------


def _manager(queue_max: int = 1000) -> tuple[ConnectionManager, TagStore, FakeDevices]:
    tags = tag_store(TAGS)
    devices = FakeDevices()
    return ConnectionManager(tags, devices, queue_max=queue_max), tags, devices


async def test_updates_for_same_tag_are_coalesced_to_latest() -> None:
    """legacy test_batch_multiple_tag_changes_into_single_message + 태그별 최신값만."""
    manager, tags, _ = _manager()
    client = manager.open(PRINCIPAL, "t")
    manager.subscribe(client, [1, 2])
    manager.drain(client, flush_tags=True)  # 구독 직후 현재값
    for value in (1.0, 2.0, 3.0):
        set_value(tags, value, tag_id=1, ts_ns=TS0_NS + int(value))
        await manager.on_tag(_tag_event(tags, 1))
    set_value(tags, 7.0, tag_id=2)
    await manager.on_tag(_tag_event(tags, 2))
    assert manager.drain(client, flush_tags=False) == []  # 배치 주기 전에는 보내지 않는다
    [message] = manager.drain(client, flush_tags=True)
    assert message.type == "tag_update"
    assert [(v.tag_id, v.value) for v in message.values] == [(1, 3.0), (2, 7.0)]


async def test_subscribe_sends_current_value_immediately_only_for_new_tags() -> None:
    """legacy test_subscribe_sends_current_value_immediately."""
    manager, tags, _ = _manager()
    set_value(tags, 5.0, tag_id=1)
    client = manager.open(PRINCIPAL, "t")
    manager.subscribe(client, [1, 999])  # 모르는 tag_id는 값이 없을 뿐 구독은 남는다
    assert client.wake.is_set()
    [message] = manager.drain(client, flush_tags=False)
    assert message.type == "tag_update"
    assert [(v.tag_id, v.value, v.quality) for v in message.values] == [(1, 5.0, "GOOD")]
    manager.subscribe(client, [1])
    assert manager.drain(client, flush_tags=False) == []


async def test_unsubscribe_stops_updates() -> None:  # legacy test_unsubscribe_stops_updates
    manager, tags, _ = _manager()
    client = manager.open(PRINCIPAL, "t")
    manager.subscribe(client, [1])
    manager.drain(client, flush_tags=True)
    manager.unsubscribe(client, [1])
    set_value(tags, 4.0, tag_id=1)
    await manager.on_tag(_tag_event(tags, 1))
    assert manager.drain(client, flush_tags=True) == []


async def test_alarm_and_device_events_reach_every_client() -> None:
    """legacy test_multiple_clients_receive_same_update(방송 쪽)."""
    manager, tags, devices = _manager()
    first, second = manager.open(PRINCIPAL, "a"), manager.open(PRINCIPAL, "b")
    devices.snapshots["plc-1"] = DeviceSnapshot(
        "plc-1", "modbus_tcp", DeviceState.DISCONNECTED, silent=True, tag_count=2, connected=False
    )
    await manager.on_alarm(Event("alarm.changed.tank-high", {"alarm": _alarm()}))
    await manager.on_device(Event("device.changed.plc-1", {"device_id": "plc-1"}))
    for client in (first, second):
        alarm, device = manager.drain(client, flush_tags=False)
        assert alarm.type == "alarm_event"
        assert (alarm.alarm_id, alarm.state, alarm.priority) == (
            "tank-high",
            "ACTIVE_UNACKED",
            "high",
        )
        assert device.type == "device_update"
        assert (device.device_id, device.state, device.silent) == ("plc-1", "DISCONNECTED", True)
    assert tags.get(1) is not None


async def test_same_tag_update_reaches_every_subscriber() -> None:
    """legacy test_multiple_clients_receive_same_update(태그 쪽)."""
    manager, tags, _ = _manager()
    clients = [manager.open(PRINCIPAL, str(i)) for i in range(2)]
    for client in clients:
        manager.subscribe(client, [2])
        manager.drain(client, flush_tags=True)
    set_value(tags, 1.5, tag_id=2)
    await manager.on_tag(_tag_event(tags, 2))
    for client in clients:
        [message] = manager.drain(client, flush_tags=True)
        assert message.type == "tag_update"
        assert [v.value for v in message.values] == [1.5]


async def test_event_queue_overflow_closes_with_1013() -> None:
    manager, _, _ = _manager(queue_max=2)
    slow, fast = manager.open(PRINCIPAL, "slow"), manager.open(PRINCIPAL, "fast")
    for _ in range(2):
        await manager.on_alarm(Event("alarm.changed.x", {"alarm": _alarm()}))
    manager.drain(fast, flush_tags=False)
    await manager.on_alarm(Event("alarm.changed.x", {"alarm": _alarm()}))
    assert (slow.close_code, list(slow.events)) == (CloseCode.TRY_AGAIN_LATER, [])
    assert fast.close_code is None


async def test_tag_update_field_set_and_nan_as_null() -> None:
    """legacy test_tag_update_value_field_set_is_contract_stable + NaN/Inf → null."""
    manager, tags, _ = _manager()
    set_value(tags, float("nan"), tag_id=1)
    client = manager.open(PRINCIPAL, "t")
    manager.subscribe(client, [1, 2])
    [message] = manager.drain(client, flush_tags=False)
    payload = json.loads(message.model_dump_json())
    first, second = payload["values"]
    assert set(first) == {"tag_id", "path", "device_id", "value", "quality", "ts"}
    assert (first["value"], first["quality"], first["ts"]) == (None, "GOOD", "2026-10-03T04:00:00Z")
    assert (second["value"], second["quality"], second["ts"]) == (None, "BAD", None)


# --- 연결 루프 ----------------------------------------------------------------------------


@dataclass
class Live:
    bus: EventBus
    service: RealtimeService
    transport: FakeTransport
    task: asyncio.Task[None]
    tags: TagStore


@pytest.fixture
async def live(request: pytest.FixtureRequest) -> AsyncGenerator[Live]:
    options: dict[str, float] = getattr(request, "param", {})
    fx = auth_fixture()
    await fx.service.add_user("kim", "correct-horse-1", Role.OPERATOR)
    login = await fx.service.login("kim", "correct-horse-1")
    bus = EventBus()
    tags = tag_store(TAGS)
    service = RealtimeService(
        bus,
        tags,
        FakeDevices(),
        fx.service,
        batch_ms=int(options.get("batch_ms", 20)),
        ping_s=options.get("ping_s", 60.0),
        pong_timeout_s=options.get("pong_timeout_s", 120.0),
        revalidate_s=options.get("revalidate_s", 60.0),
    )
    await service.start()
    transport = FakeTransport()
    task = asyncio.create_task(service.serve(transport, login.principal, login.token))
    built = Live(bus, service, transport, task, tags)
    if options.get("logout"):
        await fx.service.logout(login.token)
    yield built
    await service.stop(timeout_s=1.0)
    await asyncio.gather(task, return_exceptions=True)


async def test_hello_then_batched_tag_updates(live: Live) -> None:
    """legacy test_ws_connection_status_on_connect(→ hello) + test_subscribe_and_receive_..."""
    await eventually(lambda: live.transport.types() == ["hello"])
    assert live.transport.sent[0] == {"type": "hello", "username": "kim", "role": "operator"}
    assert live.service.status().connections == 1
    await live.transport.inbox.put(json.dumps({"type": "subscribe", "tag_ids": [1]}))
    await eventually(lambda: len(live.transport.tag_updates()) == 1)  # 현재값(BAD, 값 없음)
    for value in (1.0, 2.0):
        set_value(live.tags, value, tag_id=1, ts_ns=TS0_NS + int(value))
        await live.bus.publish(_tag_event(live.tags, 1))
    await eventually(lambda: len(live.transport.tag_updates()) == 2)
    assert [v["value"] for v in live.transport.tag_updates()[1]] == [2.0]


async def test_alarm_event_is_sent_without_waiting_for_batch(live: Live) -> None:
    await eventually(lambda: live.transport.types() == ["hello"])
    await live.bus.publish(Event("alarm.changed.tank-high", {"alarm": _alarm()}))
    await eventually(lambda: "alarm_event" in live.transport.types(), timeout_s=0.5)


async def test_invalid_client_message_is_ignored(live: Live) -> None:
    await live.transport.inbox.put("not json")
    await live.transport.inbox.put(json.dumps({"type": "subscribe", "tag_ids": [0]}))
    await live.transport.inbox.put(json.dumps({"type": "pong"}))
    await asyncio.sleep(0.05)
    assert (live.transport.closed, live.task.done()) == (None, False)


@pytest.mark.parametrize("live", [{"ping_s": 0.02, "pong_timeout_s": 0.08}], indirect=True)
async def test_missing_pong_closes_with_4408(live: Live) -> None:
    await eventually(lambda: "ping" in live.transport.types())
    await eventually(lambda: live.transport.closed == CloseCode.PONG_TIMEOUT)
    assert live.service.status().connections == 0


@pytest.mark.parametrize("live", [{"ping_s": 0.02, "pong_timeout_s": 0.08}], indirect=True)
async def test_pong_keeps_connection_open(live: Live) -> None:
    for _ in range(10):
        await live.transport.inbox.put(json.dumps({"type": "pong"}))
        await asyncio.sleep(0.02)
    assert live.transport.closed is None
    assert live.transport.types().count("ping") >= 3


@pytest.mark.parametrize("live", [{"revalidate_s": 0.03, "logout": True}], indirect=True)
async def test_logged_out_session_is_closed_with_4401(live: Live) -> None:
    await eventually(lambda: live.transport.closed == CloseCode.UNAUTHORIZED)


async def test_peer_close_and_stop(live: Live) -> None:
    await eventually(lambda: live.service.status().connections == 1)
    await live.service.stop(timeout_s=1.0)
    await eventually(live.task.done)
    assert live.transport.closed == CloseCode.GOING_AWAY
