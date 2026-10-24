"""Modbus 쓰기 드라이버(M3c-1 D7): encode = decode의 역, 값 쓰기 FC만, 실제 서버로 쓰기·재읽기."""

from __future__ import annotations

import asyncio
import math
import socket
import struct
from collections.abc import AsyncIterator, Sequence
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType as SimType
from pymodbus.simulator import SimData, SimDevice

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector.drivers.common import EncodeError, ReadbackError
from pureiron_collector.drivers.modbus import ModbusClient, ModbusDevice, decode, encode
from pureiron_collector.plan import DataType, DevicePlan, ModbusPoint, Order, RegisterType

OK, FAILED, INDETERMINATE = (
    pb.WriteResult.OK,
    pb.WriteResult.FAILED,
    pb.WriteResult.INDETERMINATE,
)
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
# 쓰기(FC6)에 이 예외 코드로 답하는 주소
EXCEPTION_ADDRESSES = {
    50: ExcCodes.ILLEGAL_VALUE,
    51: ExcCodes.DEVICE_BUSY,
    52: ExcCodes.DEVICE_FAILURE,
    53: ExcCodes.ACKNOWLEDGE,
    54: ExcCodes.GATEWAY_NO_RESPONSE,
}


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
        if function_code == 6 and set_values and address in EXCEPTION_ADDRESSES:
            return EXCEPTION_ADDRESSES[address]
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
        readback = await driver.read_back(pt)
    finally:
        await driver.close()
    assert (result.status, result.error) == (OK, "")
    assert not result.HasField("readback")  # 재읽기는 poller가 따로 한다
    assert readback == value
    assert set(recorder.function_codes) == function_codes


async def test_readback_reports_what_the_device_holds(server: tuple[int, Recorder]) -> None:
    port, _ = server
    pt = point(1, IGNORED_ADDRESS)
    driver = ModbusDevice(device_plan(port, [pt]))
    try:
        await driver.poll()
        result = await driver.write(pt, 77.0)
        readback = await driver.read_back(pt)
    finally:
        await driver.close()
    assert result.status == OK
    assert readback == 0.0  # 장비가 무시한 쓰기 — 판정(MISMATCH)은 core 몫


async def test_exception_responses_are_failed_write_and_readback_error(
    server: tuple[int, Recorder],
) -> None:
    port, _ = server
    pt = point(1, 5000)  # 서버 블록 밖 → ILLEGAL ADDRESS
    driver = ModbusDevice(device_plan(port, [point(2, 0)]))
    try:
        await driver.poll()
        result = await driver.write(pt, 1.0)
        with pytest.raises(ReadbackError, match="device rejected readback"):
            await driver.read_back(pt)
    finally:
        await driver.close()
    assert result.status == FAILED
    assert "device rejected write" in result.error


@pytest.mark.parametrize(
    ("address", "status"),
    [
        (50, FAILED),  # 03 ILLEGAL DATA VALUE — 요청 거부
        (51, FAILED),  # 06 BUSY — 실행 안 함
        (52, INDETERMINATE),  # 04 DEVICE FAILURE — 실행 도중 실패했을 수 있음
        (53, INDETERMINATE),  # 05 ACKNOWLEDGE — 처리 중
        (54, INDETERMINATE),  # 0B GATEWAY TARGET 무응답 — 장비까지 갔는지 모름
    ],
)
async def test_exception_codes_split_failed_and_indeterminate(
    server: tuple[int, Recorder], address: int, status: int
) -> None:
    port, _ = server
    driver = ModbusDevice(device_plan(port, [point(1, 0)]))
    try:
        await driver.poll()
        result = await driver.write(point(1, address), 1.0)
    finally:
        await driver.close()
    assert result.status == status
    assert "device rejected write" in result.error


async def test_write_never_opens_a_connection(server: tuple[int, Recorder]) -> None:
    port, recorder = server
    driver = ModbusDevice(device_plan(port, [point(1, 10)]))
    result = await driver.write(point(1, 10), 1.0)
    assert (result.status, result.error) == (FAILED, "device not connected")
    with pytest.raises(ReadbackError, match="not connected"):
        await driver.read_back(point(1, 10))
    assert recorder.function_codes == []


async def test_unencodable_value_raises_before_any_io(server: tuple[int, Recorder]) -> None:
    port, recorder = server
    driver = ModbusDevice(device_plan(port, [point(1, 10)]))
    try:
        await driver.poll()
        recorder.function_codes.clear()
        with pytest.raises(EncodeError):
            await driver.write(point(1, 10), 1.5)
    finally:
        await driver.close()
    assert recorder.function_codes == []
