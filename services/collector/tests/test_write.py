"""collector 쓰기(M3c-1 D7): encode = decode의 역, 값 쓰기 FC만, poll 사이 직렬 실행, 2중 방어."""

from __future__ import annotations

import asyncio
import math
import socket
import struct
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from typing import cast

import pytest
from grpc import aio
from hypothesis import given
from hypothesis import strategies as st
from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType as SimType
from pymodbus.simulator import SimData, SimDevice

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector._gen import collector_pb2_grpc as pb_grpc
from pureiron_collector.drivers.common import POLLER_STOPPED, DevicePoller, good
from pureiron_collector.drivers.modbus import (
    EncodeError,
    ModbusClient,
    ModbusDevice,
    decode,
    encode,
)
from pureiron_collector.health import DeviceHealth
from pureiron_collector.plan import DataType, DevicePlan, ModbusPoint, Order, RegisterType
from pureiron_collector.settings import CollectorSettings
from pureiron_collector.stream import Acquisition, Collector

OK, FAILED, REJECTED = pb.WriteResult.OK, pb.WriteResult.FAILED, pb.WriteResult.REJECTED
ORDERS: list[Order] = ["big", "little"]


def point(
    tag_id: int,
    address: int,
    data_type: DataType = "uint16",
    *,
    register_type: RegisterType = "holding",
    byte_order: Order = "big",
    word_order: Order = "big",
    writable: bool = True,
) -> ModbusPoint:
    return ModbusPoint(
        tag_id, register_type, address, data_type, byte_order, word_order, writable=writable
    )


def words(payload: bytes) -> list[int]:
    return [int.from_bytes(payload[i : i + 2], "big") for i in range(0, len(payload), 2)]


# --- encode ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "pt", "expected"),
    [
        (-1.0, point(1, 0, "int16"), [0xFFFF]),
        (65535.0, point(1, 0, "uint16"), [0xFFFF]),
        (1.0, point(1, 0, "uint16", byte_order="little"), [0x0100]),
        (1.0, point(1, 0, "bool"), [1]),
        (0.0, point(1, 0, "bool"), [0]),
        (-123456.0, point(1, 0, "int32"), words(struct.pack(">i", -123456))),
        (4000000000.0, point(1, 0, "uint32"), words(struct.pack(">I", 4000000000))),
        (12.5, point(1, 0, "float32"), words(struct.pack(">f", 12.5))),
        (12.5, point(1, 0, "float32", word_order="little"), words(struct.pack(">f", 12.5))[::-1]),
        (-0.1, point(1, 0, "float64"), words(struct.pack(">d", -0.1))),
        (1.0, point(1, 0, "uint16", register_type="coil"), True),
        (0.0, point(1, 0, "float32", register_type="coil"), False),
    ],
)
def test_encode_known_values(value: float, pt: ModbusPoint, expected: bool | list[int]) -> None:
    assert encode(value, pt) == expected


@pytest.mark.parametrize(
    ("value", "pt", "message"),
    [
        (math.nan, point(1, 0, "float32"), "not finite"),
        (math.inf, point(1, 0, "float64"), "not finite"),
        (32768.0, point(1, 0, "int16"), "outside int16 range"),
        (-1.0, point(1, 0, "uint16"), "outside uint16 range"),
        (2.0**32, point(1, 0, "uint32"), "outside uint32 range"),
        (1.5, point(1, 0, "int32"), "not an integer"),
        (1.0e39, point(1, 0, "float32"), "outside float32 range"),
        (2.0, point(1, 0, "bool"), "not 0 or 1"),
        (0.5, point(1, 0, "bool", register_type="coil"), "not 0 or 1"),
        (1.0, point(1, 0, register_type="input"), "read-only"),
        (1.0, point(1, 0, "bool", register_type="discrete"), "read-only"),
    ],
)
def test_encode_rejects_values_it_cannot_send_exactly(
    value: float, pt: ModbusPoint, message: str
) -> None:
    with pytest.raises(EncodeError, match=message):
        encode(value, pt)


def _roundtrip(value: float, pt: ModbusPoint) -> float | bool:
    payload = encode(value, pt)
    return decode([payload] if isinstance(payload, bool) else payload, pt)


@given(
    data_type=st.sampled_from(["int16", "uint16", "int32", "uint32"]),
    raw=st.integers(0, 2**32 - 1),
    byte_order=st.sampled_from(ORDERS),
    word_order=st.sampled_from(ORDERS),
)
def test_integer_roundtrip(
    data_type: DataType, raw: int, byte_order: Order, word_order: Order
) -> None:
    bits = 16 if data_type.endswith("16") else 32
    low = -(2 ** (bits - 1)) if data_type.startswith("int") else 0
    value = float(low + raw % 2**bits)
    pt = point(1, 0, data_type, byte_order=byte_order, word_order=word_order)
    assert _roundtrip(value, pt) == value


@given(
    value=st.floats(allow_nan=False, allow_infinity=False, width=32),
    byte_order=st.sampled_from(ORDERS),
    word_order=st.sampled_from(ORDERS),
)
def test_float32_roundtrip(value: float, byte_order: Order, word_order: Order) -> None:
    pt = point(1, 0, "float32", byte_order=byte_order, word_order=word_order)
    assert _roundtrip(value, pt) == value


@given(
    value=st.floats(allow_nan=False, allow_infinity=False),
    byte_order=st.sampled_from(ORDERS),
    word_order=st.sampled_from(ORDERS),
)
def test_float64_roundtrip(value: float, byte_order: Order, word_order: Order) -> None:
    pt = point(1, 0, "float64", byte_order=byte_order, word_order=word_order)
    assert _roundtrip(value, pt) == value


@given(
    registers=st.lists(st.integers(0, 0xFFFF), min_size=2, max_size=2),
    order=st.sampled_from(ORDERS),
)
def test_decode_then_encode_returns_the_same_registers(registers: list[int], order: Order) -> None:
    pt = point(1, 0, "uint32", byte_order=order, word_order=order)
    assert encode(float(decode(registers, pt)), pt) == registers


def test_client_surface_has_only_value_write_functions() -> None:
    """PLC 로직 불변: 클라이언트 표면에 프로그램·진단·파일 FC가 들어오지 않게 고정한다."""
    members = {name for name in vars(ModbusClient) if not name.startswith("_")}
    assert members == {
        "connected",
        "connect",
        "close",
        "read_holding_registers",
        "read_input_registers",
        "read_coils",
        "read_discrete_inputs",
        "write_coil",  # FC5
        "write_register",  # FC6
        "write_registers",  # FC16
    }


def test_float32_rounds_to_nearest_representable() -> None:
    # R5: core는 같은 규칙(float32로 인코딩→디코딩)으로 만든 값과 readback을 비교해야 한다.
    assert _roundtrip(0.1, point(1, 0, "float32")) == struct.unpack(">f", struct.pack(">f", 0.1))[0]


# --- 실제 Modbus 서버에 쓰기 -----------------------------------------------------------------
IGNORED_ADDRESS = 40  # 서버가 쓰기를 받아들이지만 값을 바꾸지 않는 주소(readback 불일치)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Recorder:
    """서버 action 훅: 요청 FC를 기록하고, IGNORED_ADDRESS 쓰기는 현재 값으로 되돌린다."""

    def __init__(self) -> None:
        self.function_codes: list[int] = []

    async def hook(
        self,
        function_code: int,
        start_address: int,
        address: int,
        count: int,
        current_registers: list[int],
        set_values: list[int] | list[bool] | None,
    ) -> ExcCodes | None:
        self.function_codes.append(function_code)
        if function_code == 6 and address == IGNORED_ADDRESS and set_values:
            cast(list[int], set_values)[0] = current_registers[address - start_address]
        return None


@pytest.fixture
async def server() -> AsyncIterator[tuple[int, Recorder]]:
    recorder = Recorder()
    registers = [SimData(0, values=[0] * 300, datatype=SimType.REGISTERS)]
    bits = [SimData(0, values=[False] * 304, datatype=SimType.BITS)]
    device = SimDevice(1, simdata=(bits, bits, registers, registers), action=recorder.hook)
    port = free_port()
    modbus = ModbusTcpServer(device, address=("127.0.0.1", port))
    task = asyncio.create_task(modbus.serve_forever())
    await asyncio.sleep(0.2)
    try:
        yield port, recorder
    finally:
        await modbus.shutdown()
        task.cancel()


def device_plan(port: int, points: Sequence[ModbusPoint]) -> DevicePlan:
    return DevicePlan("plc-1", "modbus_tcp", "127.0.0.1", port, 1, 100, 500, tuple(points))


@pytest.mark.parametrize(
    ("pt", "value", "function_codes"),
    [
        (point(1, 10, "uint16"), 1234.0, {6, 3}),
        (point(1, 11, "int16", byte_order="little"), -2.0, {6, 3}),
        (point(1, 20, "float32", word_order="little"), 2.5, {16, 3}),
        (point(1, 30, "float64"), -1.0e300, {16, 3}),
        (point(1, 5, "bool", register_type="coil"), 1.0, {5, 1}),
        (point(1, 6, "bool", register_type="coil"), 0.0, {5, 1}),
    ],
)
async def test_write_uses_value_write_functions_and_reads_back(
    server: tuple[int, Recorder], pt: ModbusPoint, value: float, function_codes: set[int]
) -> None:
    port, recorder = server
    driver = ModbusDevice(device_plan(port, [pt]))
    try:
        await driver.poll()  # 연결
        recorder.function_codes.clear()
        result = await driver.write(pt, value)
    finally:
        await driver.close()
    assert (result.status, result.error) == (OK, "")
    assert result.readback == value
    assert result.readback_ts_unix_ns > 0
    assert set(recorder.function_codes) == function_codes


async def test_readback_reports_what_the_device_holds(server: tuple[int, Recorder]) -> None:
    port, _ = server
    pt = point(1, IGNORED_ADDRESS)
    driver = ModbusDevice(device_plan(port, [pt]))
    try:
        await driver.poll()
        result = await driver.write(pt, 77.0)
    finally:
        await driver.close()
    assert result.status == OK
    assert result.readback == 0.0  # 장비가 무시한 쓰기 — 판정(MISMATCH)은 core 몫


async def test_exception_response_is_failed(server: tuple[int, Recorder]) -> None:
    port, _ = server
    pt = point(1, 5000)  # 서버 블록 밖 → ILLEGAL ADDRESS
    driver = ModbusDevice(device_plan(port, [point(2, 0)]))
    try:
        await driver.poll()
        result = await driver.write(pt, 1.0)
    finally:
        await driver.close()
    assert result.status == FAILED
    assert "device rejected write" in result.error
    assert not result.HasField("readback")


async def test_write_never_opens_a_connection(server: tuple[int, Recorder]) -> None:
    port, recorder = server
    driver = ModbusDevice(device_plan(port, [point(1, 10)]))
    result = await driver.write(point(1, 10), 1.0)
    assert (result.status, result.error) == (FAILED, "device not connected")
    assert recorder.function_codes == []


# --- poller 쓰기 큐 -------------------------------------------------------------------
class GateDriver:
    """poll이 gate가 열릴 때까지 멈춘다. poll·write 순서를 events에 남긴다."""

    def __init__(self, plan: DevicePlan, *, fail: Exception | None = None) -> None:
        self.plan = plan
        self.gate = asyncio.Event()
        self.gate.set()
        self.events: list[str] = []
        self.written: list[float] = []
        self.closed = 0
        self._fail = fail

    async def poll(self) -> list[pb.Sample]:
        self.events.append("poll-start")
        await self.gate.wait()
        self.events.append("poll-end")
        return [good(p.tag_id, 1.0, 1) for p in self.plan.points]

    async def write(self, point: ModbusPoint, value: float) -> pb.WriteResult:
        self.events.append("write")
        if self._fail is not None:
            raise self._fail
        self.written.append(value)
        return pb.WriteResult(status=OK, readback=value, readback_ts_unix_ns=1)

    async def close(self) -> None:
        self.closed += 1


PLAN = DevicePlan("plc", "modbus_tcp", "h", 502, 1, 20, 500, (point(1, 0),))


async def _until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    for _ in range(int(timeout_s / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


async def _blocked_poller(driver: GateDriver) -> tuple[DevicePoller, asyncio.Task[None], int]:
    """연결된 뒤 poll 하나가 gate에 막힌 상태의 poller와 그 poll-start의 events 위치."""
    poller = DevicePoller(PLAN, driver, DeviceHealth("plc", "modbus_tcp"), lambda _: None)
    task = asyncio.create_task(poller.run())
    await _until(lambda: poller.health.connected)
    driver.gate.clear()
    await _until(lambda: driver.events[-1] == "poll-start")
    return poller, task, len(driver.events) - 1


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_write_runs_only_between_polls() -> None:
    driver = GateDriver(PLAN)
    poller, task, blocked = await _blocked_poller(driver)
    try:
        pending = asyncio.create_task(poller.write(PLAN.points[0], 5.0))
        await asyncio.sleep(0.05)
        assert driver.events[blocked:] == ["poll-start"]  # poll 도중에는 보내지 않는다
        assert not pending.done()
        driver.gate.set()
        result = await asyncio.wait_for(pending, 2)
    finally:
        await _stop(task)
    assert (result.status, result.readback) == (OK, 5.0)
    assert driver.events[blocked : blocked + 3] == ["poll-start", "poll-end", "write"]


async def test_disconnected_device_fails_immediately_without_queueing() -> None:
    driver = GateDriver(PLAN)
    poller = DevicePoller(PLAN, driver, DeviceHealth("plc", "modbus_tcp"), lambda _: None)
    result = await poller.write(PLAN.points[0], 1.0)
    assert (result.status, result.error) == (FAILED, "device not connected")
    assert driver.events == []


async def test_driver_error_is_failed_and_connection_is_closed() -> None:
    driver = GateDriver(PLAN, fail=TimeoutError("no response"))
    poller, task, _ = await _blocked_poller(driver)
    try:
        pending = asyncio.create_task(poller.write(PLAN.points[0], 1.0))
        driver.gate.set()
        result = await asyncio.wait_for(pending, 2)
    finally:
        await _stop(task)
    assert (result.status, result.error) == (FAILED, "no response")
    assert driver.closed >= 1
    assert driver.events.count("write") == 1  # 재시도 없음


async def test_abandoned_write_is_never_sent() -> None:
    driver = GateDriver(PLAN)
    poller, task, _ = await _blocked_poller(driver)
    try:
        pending = asyncio.create_task(poller.write(PLAN.points[0], 9.0))
        await asyncio.sleep(0.01)
        pending.cancel()
        driver.gate.set()
        await _until(lambda: driver.events.count("poll-end") >= 3)
    finally:
        await _stop(task)
    assert "write" not in driver.events


async def test_stopping_poller_rejects_queued_writes() -> None:
    driver = GateDriver(PLAN)
    poller, task, _ = await _blocked_poller(driver)
    pending = asyncio.create_task(poller.write(PLAN.points[0], 1.0))
    await asyncio.sleep(0.01)
    await _stop(task)
    result = await asyncio.wait_for(pending, 2)
    assert (result.status, result.error) == (REJECTED, POLLER_STOPPED)
    assert "write" not in driver.events


# --- collector 2중 방어 ------------------------------------------------------------------
SETTINGS = CollectorSettings("127.0.0.1:1", 0.05, 0.01, 0.02, batch_interval_s=0.02)


def plan_msg(rev: int) -> pb.AcquisitionPlan:
    def tag(tag_id: int, data_type: str, *, writable: bool) -> pb.TagPoint:
        address = pb.ModbusAddress(register_type="holding", address=tag_id, data_type=data_type)
        return pb.TagPoint(tag_id=tag_id, device_id="plc", modbus=address, writable=writable)

    return pb.AcquisitionPlan(
        plan_rev=rev,
        default_poll_ms=20,
        devices=[pb.Device(device_id="plc", protocol="modbus_tcp", host="h", port=502)],
        tags=[tag(1, "uint16", writable=True), tag(2, "uint16", writable=False)],
    )


@pytest.fixture
async def acquisition() -> AsyncIterator[Acquisition]:
    acq = Acquisition(SETTINGS, GateDriver)
    await acq.apply_plan(plan_msg(3))
    await _until(lambda: all(h.connected for h in acq.health()))
    try:
        yield acq
    finally:
        await acq.stop_pollers()


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        (pb.WriteRequest(request_id="r", tag_id=1, value=1.0, plan_rev=2), "plan_rev 2"),
        (pb.WriteRequest(request_id="r", tag_id=9, value=1.0, plan_rev=3), "not in the applied"),
        (pb.WriteRequest(request_id="r", tag_id=2, value=1.0, plan_rev=3), "not writable"),
        (pb.WriteRequest(request_id="r", tag_id=1, value=1.5, plan_rev=3), "not an integer"),
        (pb.WriteRequest(request_id="r", tag_id=1, value=-1.0, plan_rev=3), "outside uint16"),
    ],
)
async def test_collector_rejects_before_sending(
    acquisition: Acquisition, request_: pb.WriteRequest, message: str
) -> None:
    result = await acquisition.write(request_)
    assert (result.request_id, result.status) == ("r", REJECTED)
    assert message in result.error


async def test_collector_sends_valid_write_and_keeps_request_id(acquisition: Acquisition) -> None:
    request = pb.WriteRequest(request_id="w-7", tag_id=1, value=42.0, plan_rev=3)
    result = await asyncio.wait_for(acquisition.write(request), 2)
    assert (result.request_id, result.status, result.readback) == ("w-7", OK, 42.0)


class WritingCore(pb_grpc.AcquisitionServicer):
    """plan 뒤 쓰기 하나를 보내고 결과를 기다린다."""

    def __init__(self) -> None:
        self.result: asyncio.Future[pb.WriteResult] = asyncio.get_running_loop().create_future()

    async def Session(
        self,
        request_iterator: AsyncIterator[pb.CollectorMsg],
        context: aio.ServicerContext[pb.CollectorMsg, pb.CoreMsg],
    ) -> AsyncIterator[pb.CoreMsg]:
        async for message in request_iterator:
            kind = message.WhichOneof("msg")
            if kind == "hello":
                yield pb.CoreMsg(plan=plan_msg(4))
            elif kind == "health" and message.health.connected and not self.result.done():
                request = pb.WriteRequest(request_id="s-1", tag_id=1, value=3.0, plan_rev=4)
                yield pb.CoreMsg(write=request)
            elif kind == "write_result" and not self.result.done():
                self.result.set_result(message.write_result)
            elif kind == "batch":
                yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))


async def test_session_answers_write_with_result_and_readback() -> None:
    core = WritingCore()
    server = aio.server()
    pb_grpc.add_AcquisitionServicer_to_server(core, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    settings = replace(SETTINGS, core_target=f"127.0.0.1:{port}", health_interval_s=0.05)
    collector = Collector(settings, driver_factory=GateDriver)
    stop = asyncio.Event()
    runner = asyncio.create_task(collector.run_forever(stop))
    try:
        result = await asyncio.wait_for(core.result, 5)
    finally:
        stop.set()
        await asyncio.wait_for(runner, 5)
        await server.stop(0)
    assert (result.request_id, result.status, result.readback) == ("s-1", OK, 3.0)
