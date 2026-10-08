"""`Acquisition.Session` 세션 계약(M3a-1 PR #14 리뷰 확정).

core `_gen` stub을 가짜 collector로 쓴다.
① Hello 직후부터(plan·DB 준비 전 포함) heartbeat를 포함한 모든 batch에 Ack, plan은 정의가
읽히는 순간 ② 세션마다 같은 rev여도 plan ③ rev 변경 시 세션 중 plan push ④ replay는 이력만
⑤ 오래된 live 샘플은 이력만 ⑥ 세션이 모두 끊기면 전 태그 BAD·장비 DISCONNECTED.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest
from grpc import aio

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core._gen import collector_pb2_grpc as pb_grpc
from pureiron_core.acquisition.devices import DeviceMonitor, DeviceState
from pureiron_core.acquisition.ingest import DiscardHistory, Ingestor
from pureiron_core.acquisition.server import (
    SESSION_TOPIC,
    AcquisitionServer,
    AcquisitionService,
    PlanKeeper,
)
from pureiron_core.platform.bus import Event, EventBus
from pureiron_core.project.ports import Definitions, DefinitionsUnavailableError
from pureiron_core.tags.ports import Quality, TagKey
from pureiron_core.tags.store import TagStore

from .conftest import sample_definitions

READ_TIMEOUT_S = 5.0


@dataclass
class FakeSource:
    definitions: Definitions | None = None

    async def rev(self) -> int:
        return (await self.load()).rev

    async def load(self) -> Definitions:
        if self.definitions is None:
            raise DefinitionsUnavailableError("site-db down")
        return self.definitions


@dataclass
class Core:
    source: FakeSource
    tags: TagStore
    devices: DeviceMonitor
    history: DiscardHistory
    ingestor: Ingestor
    server: AcquisitionServer
    events: list[Event] = field(default_factory=list[Event])

    @property
    def target(self) -> str:
        return f"127.0.0.1:{self.server.bound_port}"


@asynccontextmanager
async def start_core(
    *, silence_min_s: float = 5.0, session_silence_s: float = 15.0
) -> AsyncGenerator[Core]:
    bus = EventBus()
    source = FakeSource()
    tags = TagStore()
    devices = DeviceMonitor(tags, silence_min_s=silence_min_s)
    history = DiscardHistory()
    ingestor = Ingestor(tags, devices, history, live_max_age_s=5.0)
    service = AcquisitionService(bus, tags, devices, ingestor, session_silence_s=session_silence_s)
    keeper = PlanKeeper(source, service.set_plan, refresh_s=0.05)
    server = AcquisitionServer(service, keeper, host="127.0.0.1", port=0, watchdog_interval_s=0.05)
    built = Core(source, tags, devices, history, ingestor, server)

    async def record(event: Event) -> None:
        built.events.append(event)

    bus.subscribe("*", record)
    await server.start()
    try:
        yield built
    finally:
        await server.stop(grace_s=0)


@pytest.fixture
async def core() -> AsyncGenerator[Core]:
    async with start_core() as built:
        yield built


class FakeCollector:
    def __init__(self, channel: aio.Channel) -> None:
        self._outgoing: asyncio.Queue[pb.CollectorMsg | None] = asyncio.Queue()
        self._call = pb_grpc.AcquisitionStub(channel).Session(self._requests())

    async def _requests(self) -> AsyncIterator[pb.CollectorMsg]:
        while (message := await self._outgoing.get()) is not None:
            yield message

    def send(self, message: pb.CollectorMsg) -> None:
        self._outgoing.put_nowait(message)

    def hello(self) -> None:
        self.send(pb.CollectorMsg(hello=pb.Hello(collector_version="test")))

    def batch(
        self, seq: int, samples: list[pb.Sample] | None = None, *, replay: bool = False
    ) -> None:
        self.send(
            pb.CollectorMsg(batch=pb.ValueBatch(seq=seq, replay=replay, samples=samples or []))
        )

    async def read(self) -> pb.CoreMsg:
        message = await asyncio.wait_for(self._call.read(), READ_TIMEOUT_S)
        assert isinstance(message, pb.CoreMsg)
        return message

    def cancel(self) -> None:
        """비정상 종료(프로세스 kill과 같은 효과: half-close 없이 RPC 취소)."""
        self._call.cancel()

    async def close(self) -> None:
        self._outgoing.put_nowait(None)
        assert await asyncio.wait_for(self._call.read(), READ_TIMEOUT_S) is aio.EOF


def _num(tag_id: int, value: float, *, age_s: float = 0.0) -> pb.Sample:
    ts = time.time_ns() - int(age_s * 1e9)
    return pb.Sample(tag_id=tag_id, ts_unix_ns=ts, num=value, quality=pb.GOOD)


def _flag(tag_id: int, value: bool) -> pb.Sample:
    return pb.Sample(tag_id=tag_id, ts_unix_ns=time.time_ns(), flag=value, quality=pb.GOOD)


async def _eventually(predicate: object, timeout_s: float = READ_TIMEOUT_S) -> None:
    assert callable(predicate)
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


async def test_ack_before_plan_while_definitions_unavailable(core: Core) -> None:
    async with aio.insecure_channel(core.target) as channel:
        collector = FakeCollector(channel)
        collector.hello()
        collector.batch(0)  # heartbeat — DB 준비 전에도 Ack(collector watchdog 방지)
        first = await collector.read()
        assert first.WhichOneof("msg") == "ack"
        assert first.ack.seq == 0
        assert core.server.summary().plan_rev is None

        core.source.definitions = sample_definitions(rev=3)
        plan = await collector.read()
        assert plan.WhichOneof("msg") == "plan"
        assert plan.plan.plan_rev == 3
        assert [d.port for d in plan.plan.devices] == [502]
        await collector.close()


async def test_every_session_gets_plan_and_rev_change_is_pushed(core: Core) -> None:
    core.source.definitions = sample_definitions(rev=3)
    await _eventually(lambda: core.server.summary().plan_rev == 3)
    async with aio.insecure_channel(core.target) as channel:
        for _ in range(2):  # 재연결해도 같은 rev plan을 다시 받는다
            collector = FakeCollector(channel)
            collector.hello()
            message = await collector.read()
            assert message.plan.plan_rev == 3
            await collector.close()

        collector = FakeCollector(channel)
        collector.hello()
        assert (await collector.read()).plan.plan_rev == 3
        core.source.definitions = sample_definitions(rev=4)
        pushed = await collector.read()
        assert pushed.WhichOneof("msg") == "plan"
        assert pushed.plan.plan_rev == 4
        await collector.close()


async def test_live_replay_and_stale_samples(core: Core) -> None:
    core.source.definitions = sample_definitions()
    await _eventually(lambda: core.server.summary().tags_total == 2)
    async with aio.insecure_channel(core.target) as channel:
        collector = FakeCollector(channel)
        collector.hello()
        assert (await collector.read()).WhichOneof("msg") == "plan"

        collector.batch(1, [_num(1, 20.0), _flag(2, True)])
        assert (await collector.read()).ack.seq == 1
        assert core.tags.summary().good == 2
        assert [e.topic for e in core.events if e.topic.startswith("tag.")] == [
            "tag.changed.line1/temp",
            "tag.changed.line1/running",
        ]

        # replay: 이력에만 — 현재값·이벤트 불변
        before = len(core.events)
        collector.batch(7, [_num(1, 99.0)], replay=True)
        assert (await collector.read()).ack.seq == 7
        temp = core.tags.get(1)
        assert temp is not None
        assert temp.value == 20.0
        assert core.history.replay == 1
        assert len(core.events) == before

        # 늦게 도착한 live(샘플 시각이 5s보다 오래됨): 이력에만
        collector.batch(2, [_num(1, 55.0, age_s=30)])
        assert (await collector.read()).ack.seq == 2
        temp = core.tags.get(1)
        assert temp is not None
        assert temp.value == 20.0
        assert core.ingestor.stats.stale == 1
        assert core.history.live == 3

        # 같은 seq가 다시 와도(다른 collector 프로세스) 중복 제거하지 않고 반영·Ack
        collector.batch(1, [_num(1, 21.0)])
        assert (await collector.read()).ack.seq == 1
        temp = core.tags.get(1)
        assert temp is not None
        assert temp.value == 21.0
        await collector.close()


async def test_session_end_marks_tags_bad_and_devices_disconnected(core: Core) -> None:
    core.source.definitions = sample_definitions()
    await _eventually(lambda: core.server.summary().tags_total == 2)
    sessions: list[object] = []

    async with aio.insecure_channel(core.target) as channel:
        collector = FakeCollector(channel)
        collector.hello()
        await collector.read()
        assert core.server.collector_connected
        collector.send(
            pb.CollectorMsg(
                health=pb.DeviceHealth(
                    device_name="plc-1", protocol="modbus_tcp", connected=True, tag_count=2
                )
            )
        )
        collector.batch(1, [_num(1, 20.0), _flag(2, True)])
        await collector.read()
        await _eventually(lambda: core.devices.state("plc-1") is DeviceState.CONNECTED)
        summary = core.server.summary()
        assert (summary.devices_connected, summary.tags_good, summary.good_ratio) == (1, 2, 1.0)

        sessions = [e.payload["sessions"] for e in core.events if e.topic == SESSION_TOPIC]
        await collector.close()

    await _eventually(lambda: not core.server.collector_connected)
    await _eventually(lambda: core.tags.summary().good == 0)
    assert core.devices.state("plc-1") is DeviceState.DISCONNECTED
    temp = core.tags.get(1)
    assert temp is not None
    assert (temp.value, temp.quality) == (20.0, Quality.BAD)
    assert [e.payload["sessions"] for e in core.events if e.topic == SESSION_TOPIC] == [
        *sessions,
        0,
    ]
    assert sessions == [1]


async def _connected(core: Core, collector: FakeCollector) -> None:
    """hello → plan → health(connected) → GOOD 샘플 → 장비 CONNECTED·태그 GOOD 2개."""
    collector.hello()
    assert (await collector.read()).WhichOneof("msg") == "plan"
    collector.send(
        pb.CollectorMsg(
            health=pb.DeviceHealth(device_name="plc-1", protocol="modbus_tcp", connected=True)
        )
    )
    collector.batch(1, [_num(1, 20.0), _flag(2, True)])
    assert (await collector.read()).ack.seq == 1
    await _eventually(lambda: core.devices.state("plc-1") is DeviceState.CONNECTED)
    assert core.tags.summary().good == 2


async def test_future_timestamp_goes_to_history_only(core: Core) -> None:
    """시계가 앞선 샘플이 현재값에 미래 시각을 박아 이후 값을 얼리지 않는다(리뷰 HIGH-1)."""
    core.source.definitions = sample_definitions()
    await _eventually(lambda: core.server.summary().tags_total == 2)
    async with aio.insecure_channel(core.target) as channel:
        collector = FakeCollector(channel)
        await _connected(core, collector)
        collector.batch(2, [_num(1, 99.0, age_s=-3600)])  # 1시간 미래
        assert (await collector.read()).ack.seq == 2
        assert core.ingestor.stats.future == 1
        collector.batch(3, [_num(1, 21.0)])
        assert (await collector.read()).ack.seq == 3
        temp = core.tags.get(1)
        assert temp is not None
        assert temp.value == 21.0
        await collector.close()


async def test_one_of_two_sessions_closing_keeps_tags(core: Core) -> None:
    core.source.definitions = sample_definitions()
    await _eventually(lambda: core.server.summary().tags_total == 2)
    async with aio.insecure_channel(core.target) as channel:
        first = FakeCollector(channel)
        await _connected(core, first)
        second = FakeCollector(channel)
        second.hello()
        await second.read()
        await second.close()
        await asyncio.sleep(0.1)
        assert core.server.collector_connected
        assert core.tags.summary().good == 2
        assert core.devices.state("plc-1") is DeviceState.CONNECTED
        await first.close()
    await _eventually(lambda: core.tags.summary().good == 0)


async def test_cancelled_session_is_cleaned_up(core: Core) -> None:
    core.source.definitions = sample_definitions()
    await _eventually(lambda: core.server.summary().tags_total == 2)
    async with aio.insecure_channel(core.target) as channel:
        collector = FakeCollector(channel)
        await _connected(core, collector)
        collector.cancel()
        await _eventually(lambda: not core.server.collector_connected)
        await _eventually(lambda: core.tags.summary().good == 0)
        assert core.devices.state("plc-1") is DeviceState.DISCONNECTED


async def test_server_watchdog_marks_silent_device_bad() -> None:
    """세션은 살아 있는데 장비 샘플만 끊기면 무신호 타이머가 BAD/DISCONNECTED로 둔다."""
    async with start_core(silence_min_s=0.3) as core:
        core.source.definitions = sample_definitions()
        await _eventually(lambda: core.server.summary().tags_total == 2)
        async with aio.insecure_channel(core.target) as channel:
            collector = FakeCollector(channel)
            await _connected(core, collector)
            collector.batch(0)  # heartbeat만 — 장비 신호가 아니다
            assert (await collector.read()).ack.seq == 0
            await _eventually(lambda: core.devices.state("plc-1") is DeviceState.DISCONNECTED)
            assert core.tags.summary().good == 0
            assert core.server.collector_connected
            await collector.close()


async def test_silent_session_is_closed_by_core() -> None:
    """RST 없이 끊긴(half-open) 세션: collector가 아무것도 안 보내면 core가 세션을 닫는다."""
    async with start_core(session_silence_s=0.3) as core:
        channel = aio.insecure_channel(core.target)
        try:
            collector = FakeCollector(channel)
            collector.hello()
            await _eventually(lambda: core.server.collector_connected)
            await _eventually(lambda: not core.server.collector_connected)
            collector.cancel()
        finally:
            await channel.close()


async def test_plan_keeper_retries_after_apply_error() -> None:
    source = FakeSource(sample_definitions(rev=5))
    applied: list[int] = []

    async def apply(plan: pb.AcquisitionPlan, keys: Sequence[TagKey]) -> None:
        if not applied:
            applied.append(-1)
            raise RuntimeError("boom")
        applied.append(plan.plan_rev)

    keeper = PlanKeeper(source, apply, refresh_s=0.01)
    task = asyncio.create_task(keeper.run())
    try:
        await _eventually(lambda: applied[-1:] == [5])
        await asyncio.sleep(0.05)
        assert applied == [-1, 5]  # 성공한 뒤에는 같은 rev를 다시 적용하지 않는다
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_clock_skew_drops_are_logged_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    """시계 차이로 현재값에서 빠지는 샘플은 원인 단서를 남긴다(간격 제한)."""
    now = [0.0]
    tags = TagStore()
    devices = DeviceMonitor(tags)
    ingestor = Ingestor(tags, devices, DiscardHistory(), clock=lambda: now[0])
    future = pb.ValueBatch(seq=1, samples=[_num(1, 1.0, age_s=-60)])
    with caplog.at_level(logging.WARNING, logger="pureiron_core.acquisition.ingest"):
        ingestor.ingest(future)
        ingestor.ingest(future)  # 60s 안 — 다시 찍지 않음
        now[0] = 61.0
        ingestor.ingest(pb.ValueBatch(seq=2, samples=[_num(1, 1.0, age_s=30)]))
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 2
    assert "clock skew" in warnings[0]
    assert (ingestor.stats.future, ingestor.stats.stale) == (2, 1)
