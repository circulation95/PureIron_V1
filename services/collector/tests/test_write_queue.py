"""collector 쓰기 실행(M3c-1 D7): poll 사이 직렬, 단계별 결과, 기한, 큐 제한, 2중 방어, 세션."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace

import pytest
from grpc import aio

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector._gen import collector_pb2_grpc as pb_grpc
from pureiron_collector.drivers.common import (
    MAX_QUEUED_WRITES,
    POLLER_STOPPED,
    DevicePoller,
    EncodeError,
    ReadbackError,
    good,
)
from pureiron_collector.health import DeviceHealth
from pureiron_collector.plan import DevicePlan, ModbusPoint
from pureiron_collector.settings import CollectorSettings
from pureiron_collector.stream import Acquisition, Collector

OK, FAILED = pb.WriteResult.OK, pb.WriteResult.FAILED
REJECTED, INDETERMINATE = pb.WriteResult.REJECTED, pb.WriteResult.INDETERMINATE
LATER_S = 60.0  # 기한 검사와 무관한 테스트의 넉넉한 기한


def point(tag_id: int) -> ModbusPoint:
    return ModbusPoint(tag_id, "holding", tag_id, "uint16", "big", "big", writable=True)


class GateDriver:
    """poll·쓰기·재읽기를 각각 gate로 멈출 수 있는 드라이버. 순서를 events에 남긴다."""

    def __init__(self, plan: DevicePlan) -> None:
        self.plan = plan
        self.poll_gate, self.send_gate, self.readback_gate = (asyncio.Event() for _ in range(3))
        for gate in (self.poll_gate, self.send_gate, self.readback_gate):
            gate.set()
        self.events: list[str] = []
        self.send_error: Exception | None = None
        self.readback_error: Exception | None = None
        self.value = 0.0
        self.closed = 0

    async def poll(self) -> list[pb.Sample]:
        self.events.append("poll-start")
        await self.poll_gate.wait()
        self.events.append("poll-end")
        return [good(p.tag_id, 1.0, 1) for p in self.plan.points]

    async def write(self, point: ModbusPoint, value: float) -> pb.WriteResult:
        self.events.append("write")
        await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        self.value = value
        return pb.WriteResult(status=OK)

    async def read_back(self, point: ModbusPoint) -> float:
        self.events.append("readback")
        await self.readback_gate.wait()
        if self.readback_error is not None:
            raise self.readback_error
        return self.value

    async def close(self) -> None:
        self.closed += 1


PLAN = DevicePlan("plc", "modbus_tcp", "h", 502, 1, 20, 500, (point(1),))


async def _until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    # 반복 횟수가 아니라 시각으로 센다: Windows(시계 해상도 15.6ms)에서는 sleep(0.005)가 바로
    # 깨어나 횟수 기준 2s가 거의 0s가 된다.
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def _blocked_poller(driver: GateDriver) -> tuple[DevicePoller, asyncio.Task[None], int]:
    """연결된 뒤 poll 하나가 gate에 막힌 상태의 poller와 그 poll-start의 events 위치."""
    poller = DevicePoller(PLAN, driver, DeviceHealth("plc", "modbus_tcp"), lambda _: None)
    task = asyncio.create_task(poller.run())
    await _until(lambda: poller.health.connected)
    driver.poll_gate.clear()
    await _until(lambda: driver.events[-1] == "poll-start")
    return poller, task, len(driver.events) - 1


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _later() -> float:
    return time.monotonic() + LATER_S


async def _write_once(driver: GateDriver, value: float = 1.0) -> tuple[pb.WriteResult, int]:
    """poll이 막힌 동안 쓰기를 넣고 gate를 열어 (결과, 그때까지 연결을 닫은 횟수)를 받는다."""
    poller, task, _ = await _blocked_poller(driver)
    try:
        pending = asyncio.create_task(poller.write(point(1), value, deadline_s=_later()))
        driver.poll_gate.set()
        result = await asyncio.wait_for(pending, 2)
        return result, driver.closed  # poller 정지가 닫는 것은 세지 않는다
    finally:
        await _stop(task)


# --- 실행 시점 -----------------------------------------------------------------------
async def test_write_runs_only_between_polls() -> None:
    driver = GateDriver(PLAN)
    poller, task, blocked = await _blocked_poller(driver)
    try:
        pending = asyncio.create_task(poller.write(point(1), 5.0, deadline_s=_later()))
        await asyncio.sleep(0.05)
        assert driver.events[blocked:] == ["poll-start"]  # poll 도중에는 보내지 않는다
        assert not pending.done()
        driver.poll_gate.set()
        result = await asyncio.wait_for(pending, 2)
    finally:
        await _stop(task)
    assert (result.status, result.readback, result.error) == (OK, 5.0, "")
    assert result.readback_ts_unix_ns > 0
    assert driver.events[blocked : blocked + 4] == ["poll-start", "poll-end", "write", "readback"]


async def test_expired_write_is_rejected_and_never_sent() -> None:
    """HIGH-1: poll이 길어 기한이 지나면 보내지 않는다(core가 응답 없음 확정 뒤 PLC 도달 방지)."""
    driver = GateDriver(PLAN)
    poller, task, _ = await _blocked_poller(driver)
    try:
        deadline_s = time.monotonic() + 0.05
        pending = asyncio.create_task(poller.write(point(1), 1.0, deadline_s=deadline_s))
        await asyncio.sleep(0.1)
        driver.poll_gate.set()
        result = await asyncio.wait_for(pending, 2)
    finally:
        await _stop(task)
    assert (result.status, result.error) == (REJECTED, "expired before send")
    assert "write" not in driver.events


# --- 단계별 결과 (HIGH-2) -------------------------------------------------------------
async def test_disconnected_device_fails_immediately_without_queueing() -> None:
    driver = GateDriver(PLAN)
    poller = DevicePoller(PLAN, driver, DeviceHealth("plc", "modbus_tcp"), lambda _: None)
    result = await poller.write(point(1), 1.0, deadline_s=_later())
    assert (result.status, result.error) == (FAILED, "device not connected")
    assert driver.events == []


async def test_no_response_after_send_is_indeterminate_and_closes_connection() -> None:
    driver = GateDriver(PLAN)
    driver.send_error = TimeoutError("no response")
    result, closed = await _write_once(driver)
    assert result.status == INDETERMINATE
    assert result.error == "no confirmed response: no response"
    assert closed == 1
    assert driver.events.count("write") == 1  # 재시도 없음
    assert "readback" not in driver.events


async def test_unencodable_value_is_rejected_and_keeps_connection() -> None:
    driver = GateDriver(PLAN)
    driver.send_error = EncodeError("1.5 is not an integer (uint16)")
    result, closed = await _write_once(driver)
    assert (result.status, result.error) == (REJECTED, "1.5 is not an integer (uint16)")
    assert closed == 0


@pytest.mark.parametrize(
    ("error", "closes"),
    [
        (ReadbackError("device rejected readback: ILLEGAL ADDRESS"), False),
        (OSError("connection reset"), True),
    ],
)
async def test_readback_failure_keeps_ok_without_readback(error: Exception, closes: bool) -> None:
    driver = GateDriver(PLAN)
    driver.readback_error = error
    result, closed = await _write_once(driver, 7.0)
    assert result.status == OK  # 장비는 이미 정상 응답했다
    assert not result.HasField("readback")
    assert not result.HasField("readback_ts_unix_ns")
    assert result.error == f"readback failed: {error}"
    assert closed == (1 if closes else 0)


async def _cancel_poller_at(driver: GateDriver, gate: asyncio.Event, event: str) -> pb.WriteResult:
    poller, task, _ = await _blocked_poller(driver)
    gate.clear()
    pending = asyncio.create_task(poller.write(point(1), 1.0, deadline_s=_later()))
    driver.poll_gate.set()
    await _until(lambda: driver.events[-1] == event)
    await _stop(task)  # plan 교체·종료
    return await asyncio.wait_for(pending, 2)


async def test_poller_cancelled_while_sending_is_indeterminate() -> None:
    driver = GateDriver(PLAN)
    result = await _cancel_poller_at(driver, driver.send_gate, "write")
    assert result.status == INDETERMINATE
    assert "outcome unknown" in result.error


async def test_poller_cancelled_during_readback_is_ok_without_readback() -> None:
    driver = GateDriver(PLAN)
    result = await _cancel_poller_at(driver, driver.readback_gate, "readback")
    assert (result.status, result.error) == (OK, "readback interrupted")
    assert not result.HasField("readback")


# --- 큐 (MEDIUM-2·3) -----------------------------------------------------------------
async def test_abandoned_write_is_never_sent() -> None:
    driver = GateDriver(PLAN)
    poller, task, _ = await _blocked_poller(driver)
    try:
        pending = asyncio.create_task(poller.write(point(1), 9.0, deadline_s=_later()))
        await _until(lambda: poller.pending_writes == 1)  # 큐에 들어갔다
        pending.cancel()
        driver.poll_gate.set()
        await _until(lambda: driver.events.count("poll-end") >= 3)
    finally:
        await _stop(task)
    assert "write" not in driver.events


async def test_stopping_poller_rejects_queued_and_later_writes() -> None:
    driver = GateDriver(PLAN)
    poller, task, _ = await _blocked_poller(driver)
    pending = asyncio.create_task(poller.write(point(1), 1.0, deadline_s=_later()))
    await asyncio.sleep(0.01)
    await _stop(task)
    result = await asyncio.wait_for(pending, 2)
    late = await asyncio.wait_for(poller.write(point(2), 1.0, deadline_s=_later()), 1)
    assert (result.status, result.error) == (REJECTED, POLLER_STOPPED)
    assert (late.status, late.error) == (REJECTED, POLLER_STOPPED)
    assert "write" not in driver.events


async def test_queue_limit_and_one_write_per_tag() -> None:
    driver = GateDriver(PLAN)
    poller, task, _ = await _blocked_poller(driver)
    try:
        queued = [
            asyncio.create_task(poller.write(point(i), 1.0, deadline_s=_later()))
            for i in range(1, MAX_QUEUED_WRITES + 1)
        ]
        await asyncio.sleep(0.01)
        same_tag = await poller.write(point(1), 2.0, deadline_s=_later())
        overflow = await poller.write(point(99), 1.0, deadline_s=_later())
        driver.poll_gate.set()
        results = await asyncio.wait_for(asyncio.gather(*queued), 2)
    finally:
        await _stop(task)
    assert (same_tag.status, same_tag.error) == (REJECTED, "write in progress for tag 1")
    assert (overflow.status, overflow.error) == (REJECTED, "write queue full")
    assert [r.status for r in results] == [OK] * MAX_QUEUED_WRITES


# --- collector 2중 방어 ------------------------------------------------------------------
SETTINGS = CollectorSettings("127.0.0.1:1", 0.05, 0.01, 0.02, batch_interval_s=0.02)


def plan_msg(rev: int, *, writable: bool = True) -> pb.AcquisitionPlan:
    def tag(tag_id: int, *, writable: bool) -> pb.TagPoint:
        address = pb.ModbusAddress(register_type="holding", address=tag_id, data_type="uint16")
        return pb.TagPoint(tag_id=tag_id, device_id="plc", modbus=address, writable=writable)

    return pb.AcquisitionPlan(
        plan_rev=rev,
        default_poll_ms=20,
        devices=[pb.Device(device_id="plc", protocol="modbus_tcp", host="h", port=502)],
        tags=[tag(1, writable=writable), tag(2, writable=False)],
    )


class Drivers:
    """Acquisition이 만든 드라이버를 테스트가 붙잡는다."""

    def __init__(self) -> None:
        self.made: list[GateDriver] = []

    def __call__(self, plan: DevicePlan) -> GateDriver:
        driver = GateDriver(plan)
        self.made.append(driver)
        return driver


@pytest.fixture
async def acquisition() -> AsyncIterator[tuple[Acquisition, Drivers]]:
    drivers = Drivers()
    acq = Acquisition(SETTINGS, drivers)
    await acq.apply_plan(plan_msg(3))
    await _until(lambda: all(h.connected for h in acq.health()))
    try:
        yield acq, drivers
    finally:
        await acq.stop_pollers()


def request(request_id: str = "r", **fields: float) -> pb.WriteRequest:
    values: dict[str, float] = {"tag_id": 1, "value": 1.0, "plan_rev": 3, "timeout_ms": 1000}
    values.update(fields)
    return pb.WriteRequest(
        request_id=request_id,
        tag_id=int(values["tag_id"]),
        value=values["value"],
        plan_rev=int(values["plan_rev"]),
        timeout_ms=int(values["timeout_ms"]),
    )


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        (request(plan_rev=2), "plan_rev 2 does not match"),
        (request(plan_rev=0), "plan_rev 0 does not match"),
        (request(timeout_ms=0), "missing timeout"),
        (request(timeout_ms=60_001), "timeout_ms 60001 exceeds 60000"),
        (request(tag_id=9), "not in the applied"),
        (request(tag_id=2), "not writable"),
        (request(value=1.5), "not an integer"),
        (request(value=-1.0), "outside uint16"),
    ],
)
async def test_collector_rejects_before_sending(
    acquisition: tuple[Acquisition, Drivers], request_: pb.WriteRequest, message: str
) -> None:
    acq, drivers = acquisition
    result = await acq.write(request_)
    assert result is not None
    assert (result.request_id, result.status) == ("r", REJECTED)
    assert message in result.error
    assert "write" not in drivers.made[0].events


async def test_collector_sends_valid_write_once_per_request_id(
    acquisition: tuple[Acquisition, Drivers],
) -> None:
    acq, drivers = acquisition
    first = await asyncio.wait_for(acq.write(request("w-7", value=42.0)), 2)
    again = await asyncio.wait_for(acq.write(request("w-7", value=42.0)), 2)
    assert first is not None
    assert (first.request_id, first.status, first.readback) == ("w-7", OK, 42.0)
    assert again is None  # 중복은 응답하지 않는다 — 그 id의 응답은 원래 요청 것 1건
    assert drivers.made[0].events.count("write") == 1


async def test_empty_request_id_is_rejected(acquisition: tuple[Acquisition, Drivers]) -> None:
    acq, drivers = acquisition
    result = await acq.write(request(""))
    assert result is not None
    assert (result.status, result.error) == (REJECTED, "missing request_id")
    assert "write" not in drivers.made[0].events


async def test_plan_change_rejects_queued_write(acquisition: tuple[Acquisition, Drivers]) -> None:
    acq, drivers = acquisition
    driver = drivers.made[0]
    driver.poll_gate.clear()
    await _until(lambda: driver.events[-1] == "poll-start")
    pending = asyncio.create_task(acq.write(request()))
    await asyncio.sleep(0.01)
    await acq.apply_plan(plan_msg(4))
    result = await asyncio.wait_for(pending, 2)
    assert result is not None
    assert (result.status, result.error) == (REJECTED, POLLER_STOPPED)
    assert "write" not in driver.events


async def test_same_rev_plan_updates_writable_without_restarting_pollers(
    acquisition: tuple[Acquisition, Drivers],
) -> None:
    """core가 현장 스위치만 바꿔 다시 뜨면 같은 rev로 writable만 다른 plan이 온다(M3c-2)."""
    acq, drivers = acquisition
    before = dict(acq.pollers)
    await acq.apply_plan(plan_msg(3, writable=False))
    assert acq.pollers == before  # poll은 끊지 않는다
    rejected = await asyncio.wait_for(acq.write(request("off")), 2)
    assert rejected is not None
    assert (rejected.status, "not writable" in rejected.error) == (REJECTED, True)
    await acq.apply_plan(plan_msg(3, writable=True))
    accepted = await asyncio.wait_for(acq.write(request("on", value=7.0)), 2)
    assert accepted is not None
    assert (accepted.status, accepted.readback) == (OK, 7.0)
    assert drivers.made[0].events.count("write") == 1
    assert acq.pollers == before


async def test_write_to_dead_poller_is_rejected(acquisition: tuple[Acquisition, Drivers]) -> None:
    acq, _ = acquisition
    task = acq.pollers["plc"]
    await _stop(task)
    result = await asyncio.wait_for(acq.write(request()), 1)
    assert result is not None
    assert (result.status, result.error) == (REJECTED, "device plc is not polled")


# --- gRPC 세션 -------------------------------------------------------------------------
class WritingCore(pb_grpc.AcquisitionServicer):
    """plan을 보내고, `trigger`가 켜지면 같은 쓰기를 `copies`번 보내고 결과를 모두 기록한다."""

    def __init__(self, *, trigger_set: bool, copies: int = 1) -> None:
        self.trigger = asyncio.Event()
        if trigger_set:
            self.trigger.set()
        self.copies = copies
        self.sent = asyncio.Event()
        self.result: asyncio.Future[pb.WriteResult] = asyncio.get_running_loop().create_future()
        self.results: list[pb.WriteResult] = []

    async def Session(
        self,
        request_iterator: AsyncIterator[pb.CollectorMsg],
        context: aio.ServicerContext[pb.CollectorMsg, pb.CoreMsg],
    ) -> AsyncIterator[pb.CoreMsg]:
        async for message in request_iterator:
            kind = message.WhichOneof("msg")
            if kind == "hello":
                yield pb.CoreMsg(plan=plan_msg(4))
            elif kind == "write_result":
                self.results.append(message.write_result)
                if not self.result.done():
                    self.result.set_result(message.write_result)
            elif kind == "batch":
                yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))
            ready = kind == "health" and message.health.connected and self.trigger.is_set()
            if ready and not self.sent.is_set():
                self.sent.set()
                for _ in range(self.copies):
                    yield pb.CoreMsg(write=request("s-1", value=3.0, plan_rev=4))


async def _session(core: WritingCore) -> tuple[aio.Server, Collector, Drivers]:
    server = aio.server()
    pb_grpc.add_AcquisitionServicer_to_server(core, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    drivers = Drivers()
    settings = replace(SETTINGS, core_target=f"127.0.0.1:{port}", health_interval_s=0.05)
    return server, Collector(settings, driver_factory=drivers), drivers


async def test_session_answers_write_with_result_and_readback() -> None:
    core = WritingCore(trigger_set=True)
    server, collector, _ = await _session(core)
    stop = asyncio.Event()
    runner = asyncio.create_task(collector.run_forever(stop))
    try:
        result = await asyncio.wait_for(core.result, 5)
    finally:
        stop.set()
        await asyncio.wait_for(runner, 5)
        await server.stop(0)
    assert (result.request_id, result.status, result.readback) == ("s-1", OK, 3.0)


async def test_duplicate_request_id_gets_exactly_one_result() -> None:
    """N1: 첫 요청이 poll에 막혀 있는 동안 같은 id가 다시 와도 응답은 원래 요청의 OK 1건."""
    core = WritingCore(trigger_set=False, copies=2)
    server, collector, drivers = await _session(core)
    stop = asyncio.Event()
    session = asyncio.create_task(collector.run_session(stop))
    try:
        await _until(lambda: bool(drivers.made) and "poll-end" in drivers.made[0].events)
        driver = drivers.made[0]
        driver.poll_gate.clear()
        await _until(lambda: driver.events[-1] == "poll-start")
        core.trigger.set()
        await asyncio.wait_for(core.sent.wait(), 5)
        await _until(lambda: collector.acquisition.pending_writes() == 1)
        driver.poll_gate.set()
        await asyncio.wait_for(core.result, 5)
        await _until(lambda: driver.events.count("poll-end") >= 5)  # 늦은 두 번째 응답이 없는지
    finally:
        stop.set()
        await asyncio.wait_for(session, 5)
        await collector.acquisition.stop_pollers()
        await server.stop(0)
    assert [(r.request_id, r.status) for r in core.results] == [("s-1", OK)]
    assert driver.events.count("write") == 1


async def test_session_end_drops_queued_write() -> None:
    """세션이 끝나면 아직 보내지 않은 쓰기는 poller가 살아 있어도 보내지 않는다."""
    core = WritingCore(trigger_set=False)
    server, collector, drivers = await _session(core)
    stop = asyncio.Event()
    session = asyncio.create_task(collector.run_session(stop))
    try:
        await _until(lambda: bool(drivers.made) and "poll-end" in drivers.made[0].events)
        driver = drivers.made[0]
        driver.poll_gate.clear()
        await _until(lambda: driver.events[-1] == "poll-start")
        core.trigger.set()
        await asyncio.wait_for(core.sent.wait(), 5)
        await _until(lambda: collector.acquisition.pending_writes() == 1)  # 큐에 들어갔다
        stop.set()
        await asyncio.wait_for(session, 5)
        driver.poll_gate.set()  # poller는 계속 돈다 — 큐의 쓰기는 버려졌어야 한다
        await _until(lambda: driver.events.count("poll-end") >= 4)
    finally:
        await collector.acquisition.stop_pollers()
        await server.stop(0)
    assert "write" not in driver.events
    assert not core.result.done()
