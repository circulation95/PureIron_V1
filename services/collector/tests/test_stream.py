from __future__ import annotations

import ast
import asyncio
import time
import tomllib
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
from grpc import aio

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector._gen import collector_pb2_grpc as pb_grpc
from pureiron_collector.drivers.common import DevicePoller, good
from pureiron_collector.health import DeviceHealth
from pureiron_collector.plan import DevicePlan, parse_plan
from pureiron_collector.settings import CollectorSettings, SettingsError
from pureiron_collector.stream import HEARTBEAT_SEQ, Acquisition, Backoff, Collector

COLLECTOR = Path(__file__).resolve().parents[1]
DB_MODULES = {"asyncpg", "psycopg", "psycopg2", "sqlalchemy", "sqlite3"}


class FakeCore(pb_grpc.AcquisitionServicer):
    """plan 하나와 쓰기 요청 하나를 보내고, collector가 보낸 메시지를 기록한다."""

    def __init__(self) -> None:
        self.received: list[pb.CollectorMsg] = []
        self.write_result = asyncio.Event()
        self.batches = asyncio.Event()

    async def Session(
        self,
        request_iterator: AsyncIterator[pb.CollectorMsg],
        context: aio.ServicerContext[pb.CollectorMsg, pb.CoreMsg],
    ) -> AsyncIterator[pb.CoreMsg]:
        async for message in request_iterator:
            self.received.append(message)
            kind = message.WhichOneof("msg")
            if kind == "hello":
                yield pb.CoreMsg(plan=pb.AcquisitionPlan(plan_rev=7, default_poll_ms=1000))
                yield pb.CoreMsg(write=pb.WriteRequest(request_id="w-1", tag_id=1, value=1.0))
            elif kind == "write_result":
                self.write_result.set()
            elif kind == "batch":
                yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))
                heartbeats = [m for m in self.received if m.WhichOneof("msg") == "batch"]
                if len(heartbeats) >= 2:
                    self.batches.set()


async def test_session_hello_plan_write_rejected_and_heartbeat() -> None:
    core = FakeCore()
    server = aio.server()
    pb_grpc.add_AcquisitionServicer_to_server(core, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        settings = CollectorSettings(
            core_target=f"127.0.0.1:{port}",
            heartbeat_s=0.05,
            reconnect_initial_s=0.05,
            reconnect_max_s=0.1,
        )
        collector = Collector(settings)
        stop = asyncio.Event()
        session = asyncio.create_task(collector.run_session(stop))
        await asyncio.wait_for(core.write_result.wait(), 5)
        await asyncio.wait_for(core.batches.wait(), 5)
        stop.set()
        assert await asyncio.wait_for(session, 5) is True
    finally:
        await server.stop(0)

    hello = core.received[0]
    assert hello.WhichOneof("msg") == "hello"
    assert hello.hello.collector_version
    assert hello.hello.last_plan_rev == 0
    assert collector.acquisition.plan_rev == 7

    results = [m.write_result for m in core.received if m.WhichOneof("msg") == "write_result"]
    assert [(r.request_id, r.status) for r in results] == [("w-1", pb.WriteResult.REJECTED)]
    # plan에 장비가 없으므로 batch는 빈 heartbeat뿐이다(seq 0, 링버퍼에 들어가지 않음).
    seqs = [m.batch.seq for m in core.received if m.WhichOneof("msg") == "batch"]
    assert seqs
    assert set(seqs) == {HEARTBEAT_SEQ}
    assert len(collector.acquisition.ring) == 0
    assert all(m.batch.plan_rev == 7 for m in core.received if m.WhichOneof("msg") == "batch")


def test_hello_reports_oldest_unacked_seq_from_ring() -> None:
    collector = Collector(CollectorSettings("127.0.0.1:1", 1.0, 0.01, 0.02))
    ring = collector.acquisition.ring
    sample = pb.Sample(tag_id=1, ts_unix_ns=1, num=1.0, quality=pb.GOOD)
    for _ in range(3):
        ring.append(0, [sample])
    assert collector.hello().hello.oldest_unacked_seq == 1
    ring.ack(2)
    assert collector.hello().hello.oldest_unacked_seq == 3
    ring.ack(1)  # 늦게 온 작은 Ack는 무시
    assert len(ring) == 1


def test_default_write_result_status_is_rejected() -> None:
    assert pb.WriteResult().status == pb.WriteResult.REJECTED


def test_backoff_doubles_to_cap_and_resets() -> None:
    backoff = Backoff(0.5, 4.0)
    assert [backoff.next_delay() for _ in range(6)] == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]
    backoff.reset()
    assert backoff.next_delay() == 0.5


async def test_run_forever_retries_until_stopped() -> None:
    settings = CollectorSettings("127.0.0.1:1", 1.0, 0.01, 0.02)
    attempts = 0

    class CountingCollector(Collector):
        async def run_session(self, stop: asyncio.Event) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts >= 3:
                stop.set()
            return False

    await asyncio.wait_for(CountingCollector(settings).run_forever(asyncio.Event()), 5)
    assert attempts == 3


def test_settings_validation() -> None:
    assert CollectorSettings.from_env({}).core_target == "127.0.0.1:50051"
    with pytest.raises(SettingsError):
        CollectorSettings.from_env({"PUREIRON_COLLECTOR_HEARTBEAT_S": "0"})


def test_collector_has_no_database_dependency() -> None:
    project = tomllib.loads((COLLECTOR / "pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(project["project"]["dependencies"]).lower()
    assert not any(name in deps for name in DB_MODULES)
    for path in (COLLECTOR / "src").rglob("*.py"):
        if "_gen" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not {n.split(".")[0] for n in names} & DB_MODULES, path


class FakeDriver:
    """plan의 태그마다 poll 횟수를 값으로 돌려주는 드라이버."""

    def __init__(self, plan: DevicePlan) -> None:
        self.plan = plan
        self.polls = 0

    async def poll(self) -> list[pb.Sample]:
        self.polls += 1
        return [good(p.tag_id, float(self.polls), time.time_ns()) for p in self.plan.points]

    async def close(self) -> None:
        return None


def device_plan_msg(rev: int, tags: int) -> pb.AcquisitionPlan:
    return pb.AcquisitionPlan(
        plan_rev=rev,
        default_poll_ms=20,
        devices=[pb.Device(device_id="plc", protocol="modbus_tcp", host="h", port=502)],
        tags=[
            pb.TagPoint(
                tag_id=i,
                device_id="plc",
                modbus=pb.ModbusAddress(register_type="holding", address=i, data_type="uint16"),
            )
            for i in range(1, tags + 1)
        ],
    )


FAST = CollectorSettings(
    "127.0.0.1:1", 0.05, 0.01, 0.02, batch_interval_s=0.02, health_interval_s=0.05
)


class PlanCore(pb_grpc.AcquisitionServicer):
    def __init__(self) -> None:
        self.batches: list[pb.ValueBatch] = []
        self.health: list[pb.DeviceHealth] = []
        self.ready = asyncio.Event()

    async def Session(
        self,
        request_iterator: AsyncIterator[pb.CollectorMsg],
        context: aio.ServicerContext[pb.CollectorMsg, pb.CoreMsg],
    ) -> AsyncIterator[pb.CoreMsg]:
        async for message in request_iterator:
            kind = message.WhichOneof("msg")
            if kind == "hello":
                yield pb.CoreMsg(plan=device_plan_msg(5, 3))
            elif kind == "health":
                self.health.append(message.health)
            elif kind == "batch" and message.batch.seq != HEARTBEAT_SEQ:
                self.batches.append(message.batch)
                yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))
                if len(self.batches) >= 3 and self.health:
                    self.ready.set()


async def test_pipeline_streams_good_samples_health_and_ring_drains_on_ack() -> None:
    core = PlanCore()
    server = aio.server()
    pb_grpc.add_AcquisitionServicer_to_server(core, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    collector = Collector(replace(FAST, core_target=f"127.0.0.1:{port}"), driver_factory=FakeDriver)
    stop = asyncio.Event()
    runner = asyncio.create_task(collector.run_forever(stop))
    ring = collector.acquisition.ring

    async def first_three_acked() -> None:
        await core.ready.wait()
        third = core.batches[2].seq
        for _ in range(500):
            if not 0 < ring.oldest_unacked_seq <= third:
                return
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(first_three_acked(), 5)
    finally:
        stop.set()
        await asyncio.wait_for(runner, 5)
        await server.stop(0)

    first = core.batches[0]
    assert (first.plan_rev, first.replay) == (5, False)
    assert {s.tag_id for s in first.samples} <= {1, 2, 3}
    assert all(s.quality == pb.GOOD for b in core.batches for s in b.samples)
    assert [b.seq for b in core.batches] == list(range(1, len(core.batches) + 1))
    assert core.health[-1].device_name == "plc"
    assert core.health[-1].connected
    assert ring.oldest_unacked_seq == 0 or ring.oldest_unacked_seq > core.batches[2].seq


async def test_batches_made_while_core_is_down_replay_before_live() -> None:
    acquisition = Acquisition(replace(FAST, batch_max_samples=2), FakeDriver)
    await acquisition.apply_plan(device_plan_msg(1, 3))
    try:
        acquisition.accept([pb.Sample(tag_id=1, ts_unix_ns=1, num=1.0, quality=pb.GOOD)] * 5)
        acquisition.flush()  # 세션 없음 → 링버퍼에만(500값 상한 대신 2로 잘림: 2+2+1)
        assert len(acquisition.ring) == 3

        outgoing: asyncio.Queue[pb.CollectorMsg] = asyncio.Queue()
        acquisition.attach(outgoing)
        acquisition.accept([pb.Sample(tag_id=2, ts_unix_ns=2, num=2.0, quality=pb.GOOD)])
        acquisition.flush()
    finally:
        await acquisition.stop_pollers()

    sent = [outgoing.get_nowait().batch for _ in range(outgoing.qsize())]
    assert [(b.seq, b.replay) for b in sent] == [(1, True), (2, True), (3, True), (4, False)]


async def test_same_plan_rev_on_reconnect_keeps_pollers() -> None:
    acquisition = Acquisition(FAST, FakeDriver)
    await acquisition.apply_plan(device_plan_msg(1, 1))
    try:
        before = dict(acquisition.pollers)
        await acquisition.apply_plan(device_plan_msg(1, 1))
        assert acquisition.pollers == before
        await acquisition.apply_plan(device_plan_msg(2, 2))
        assert acquisition.pollers.keys() == before.keys()
        assert acquisition.pollers != before
        assert acquisition.plan_rev == 2
    finally:
        await acquisition.stop_pollers()


async def test_failed_device_reports_bad_samples_and_error_health() -> None:
    class DeadDriver(FakeDriver):
        async def poll(self) -> list[pb.Sample]:
            raise ConnectionError("no route")

    sent: list[pb.Sample] = []
    plan = parse_plan(device_plan_msg(1, 2)).devices[0]
    poller = DevicePoller(plan, DeadDriver(plan), DeviceHealth("plc", "modbus_tcp"), sent.extend)
    await poller.poll_once()
    await poller.poll_once()
    assert [(s.tag_id, s.quality) for s in sent] == [(1, pb.BAD), (2, pb.BAD)] * 2
    health = poller.health.to_proto()
    assert (health.status, health.connected, health.consecutive_failures) == ("error", False, 2)
    assert health.read_success_rate == 0.0
    assert health.last_error == "no route"
