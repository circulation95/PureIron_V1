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
from pureiron_collector.drivers.common import DevicePoller, good, retry_delay
from pureiron_collector.drivers.modbus import ModbusDevice
from pureiron_collector.health import DeviceHealth
from pureiron_collector.plan import DevicePlan, ModbusPoint, parse_plan
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

    # plan_rev가 빠진(0) 쓰기는 적용 중인 plan(7)과 달라 보내지 않는다.
    results = [m.write_result for m in core.received if m.WhichOneof("msg") == "write_result"]
    assert [(r.request_id, r.status) for r in results] == [("w-1", pb.WriteResult.REJECTED)]
    assert "plan_rev 0" in results[0].error
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
    with pytest.raises(SettingsError, match="at least twice"):
        CollectorSettings.from_env({"PUREIRON_COLLECTOR_HEARTBEAT_S": "3"})


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

    async def write(self, point: ModbusPoint, value: float) -> pb.WriteResult:
        return pb.WriteResult(status=pb.WriteResult.OK, readback=value, readback_ts_unix_ns=1)

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
            elif kind == "batch":
                if message.batch.seq != HEARTBEAT_SEQ:
                    self.batches.append(message.batch)
                if len(self.batches) == 3 and message.batch.seq != HEARTBEAT_SEQ:
                    # 세션 도중 plan 변경 — Ack보다 먼저 보내 미-Ack batch가 링에 남게 한다.
                    yield pb.CoreMsg(plan=device_plan_msg(6, 2))
                yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))
                if len(self.batches) >= 6 and self.health:
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
    # 세션 도중 plan이 바뀌어도 이미 보낸 batch를 replay로 다시 보내지 않는다(seq 단조 증가).
    assert not any(b.replay for b in core.batches)
    assert [b.seq for b in core.batches] == list(range(1, len(core.batches) + 1))
    assert core.batches[-1].plan_rev == 6
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


def _drain(queue: asyncio.Queue[pb.CollectorMsg]) -> list[tuple[int, bool, list[int]]]:
    """큐의 batch → (seq, replay, 샘플 시각들)."""
    sent = [queue.get_nowait().batch for _ in range(queue.qsize())]
    return [(b.seq, b.replay, [s.ts_unix_ns for s in b.samples]) for b in sent]


def _at(*ts: int) -> list[pb.Sample]:
    return [pb.Sample(tag_id=1, ts_unix_ns=t, num=float(t), quality=pb.GOOD) for t in ts]


async def test_reconnect_edge_samples_go_once_as_replay_or_live_never_both() -> None:
    """V1 N4: 재연결 경계의 샘플은 정확히 한 번, 링에 있던 것은 replay로만 간다.

    통합 테스트(compose ④)는 창 끝 EDGE 2s 안의 샘플이 재연결 뒤 live로 다시 오는 회귀를 나이로
    구별하지 못한다 — 여기서 seq·replay·샘플 시각으로 고정한다. 단절 전 보냈지만 Ack 못 받은
    batch(seq 2)도 replay로 다시 가고(core가 UNIQUE로 흡수), 단절 중 링에 들어간 batch는 live로
    가지 않으며, 재연결 순간 아직 batch로 묶이지 않은 샘플(t=7)은 새 seq의 live로 한 번만 간다.
    """
    acquisition = Acquisition(replace(FAST, batch_max_samples=2), FakeDriver)
    await acquisition.apply_plan(pb.AcquisitionPlan(plan_rev=1))  # 장비 없음 → 샘플은 손으로 넣는다
    first: asyncio.Queue[pb.CollectorMsg] = asyncio.Queue()
    acquisition.attach(first)
    acquisition.accept(_at(1, 2))
    acquisition.flush()
    acquisition.ring.ack(1)
    acquisition.accept(_at(3))
    acquisition.flush()  # 보냈지만 Ack 전에 끊긴다

    acquisition.detach()
    acquisition.accept(_at(4, 5, 6))
    acquisition.flush()  # 단절 중: 링에만
    acquisition.accept(_at(7))  # 창 끝: 재연결 순간 아직 버퍼에 있다

    second: asyncio.Queue[pb.CollectorMsg] = asyncio.Queue()
    await acquisition.apply_plan(pb.AcquisitionPlan(plan_rev=1))  # 같은 rev 재연결 → 그대로
    acquisition.attach(second)
    acquisition.attach(second)  # 세션 도중 plan이 다시 와도 replay를 반복하지 않는다
    acquisition.accept(_at(8))
    acquisition.flush()

    assert _drain(first) == [(1, False, [1, 2]), (2, False, [3])]
    sent = _drain(second)
    assert sent == [(2, True, [3]), (3, True, [4, 5]), (4, True, [6]), (5, False, [7, 8])]
    live = [t for _, replay, ts in sent if not replay for t in ts]
    replayed = [t for _, replay, ts in sent if replay for t in ts]
    assert not set(live) & set(replayed)


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
    assert await poller.poll_once() is False
    assert await poller.poll_once() is False
    # BAD는 끊긴 순간 한 번만(매 주기 반복하지 않음)
    assert [(s.tag_id, s.quality) for s in sent] == [(1, pb.BAD), (2, pb.BAD)]
    health = poller.health.to_proto()
    assert (health.status, health.connected, health.consecutive_failures) == ("error", False, 2)
    assert health.read_success_rate == 0.0
    assert health.last_error == "no route"


class SilentThenAckingCore(pb_grpc.AcquisitionServicer):
    """첫 세션은 plan만 보내고 침묵(RST 없는 단절 흉내), 두 번째 세션부터 Ack 한다."""

    def __init__(self) -> None:
        self.sessions: list[list[pb.ValueBatch]] = []
        self.replayed = asyncio.Event()

    async def Session(
        self,
        request_iterator: AsyncIterator[pb.CollectorMsg],
        context: aio.ServicerContext[pb.CollectorMsg, pb.CoreMsg],
    ) -> AsyncIterator[pb.CoreMsg]:
        batches: list[pb.ValueBatch] = []
        self.sessions.append(batches)
        silent = len(self.sessions) == 1
        async for message in request_iterator:
            kind = message.WhichOneof("msg")
            if kind == "hello":
                yield pb.CoreMsg(plan=device_plan_msg(1, 2))
            elif kind == "batch" and message.batch.seq != HEARTBEAT_SEQ:
                batches.append(message.batch)
                if not silent:
                    yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))
                if any(b.replay for b in batches) and any(not b.replay for b in batches):
                    self.replayed.set()
            elif kind == "batch" and not silent:
                yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))


async def test_silent_core_triggers_reconnect_and_replay() -> None:
    core = SilentThenAckingCore()
    server = aio.server()
    pb_grpc.add_AcquisitionServicer_to_server(core, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    settings = replace(FAST, core_target=f"127.0.0.1:{port}", core_silence_s=0.3)
    collector = Collector(settings, driver_factory=FakeDriver)
    stop = asyncio.Event()
    runner = asyncio.create_task(collector.run_forever(stop))
    try:
        await asyncio.wait_for(core.replayed.wait(), 5)
    finally:
        stop.set()
        await asyncio.wait_for(runner, 5)
        await server.stop(0)

    first, second = core.sessions[0], core.sessions[1]
    assert first
    replays = [b for b in second if b.replay]
    # 두 번째 세션은 replay가 먼저, 그 뒤 live
    assert second[: len(replays)] == replays
    assert {b.seq for b in first} <= {b.seq for b in replays}


async def _swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read()  # 연결은 받고 Modbus 응답은 하지 않음
    writer.close()


def _poller_for(port: int, timeout_ms: int) -> DevicePoller:
    device = parse_plan(device_plan_msg(1, 1)).devices[0]
    plan = replace(device, host="127.0.0.1", port=port, timeout_ms=timeout_ms)
    sink: list[pb.Sample] = []
    return DevicePoller(plan, ModbusDevice(plan), DeviceHealth("plc", "modbus_tcp"), sink.extend)


async def test_cancel_while_device_never_answers_stops_poller() -> None:
    server = await asyncio.start_server(_swallow, "127.0.0.1", 0)
    poller = _poller_for(server.sockets[0].getsockname()[1], timeout_ms=5000)
    task = asyncio.create_task(poller.run())
    try:
        await asyncio.sleep(0.5)  # execute()가 응답을 기다리는 중
        task.cancel()
        # wait_for는 시간 초과 시 다시 cancel하고 끝까지 기다리므로(버그면 멈춤) wait로 본다.
        done, _ = await asyncio.wait({task}, timeout=2)
        assert task in done, "poller swallowed cancellation"
        assert task.cancelled()
    finally:
        server.close()
        await server.wait_closed()


async def test_no_response_counts_as_timeout() -> None:
    server = await asyncio.start_server(_swallow, "127.0.0.1", 0)
    poller = _poller_for(server.sockets[0].getsockname()[1], timeout_ms=200)
    try:
        assert await poller.poll_once() is False
    finally:
        server.close()
        await server.wait_closed()
    assert poller.health.timeout_count == 1


def test_retry_delay_backs_off_to_cap() -> None:
    assert [retry_delay(1.0, n) for n in (1, 2, 3, 6, 10)] == [1.0, 2.0, 4.0, 30.0, 30.0]
    assert retry_delay(0.1, 1) == 0.5  # 빠른 poll이라도 재연결은 최소 0.5s 간격
    assert retry_delay(1.0, 10_000) == 30.0  # 장시간 정지(수천 번 실패)에도 overflow 없음
