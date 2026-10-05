from __future__ import annotations

import asyncio
import socket
import struct
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

import pytest
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType as SimType
from pymodbus.simulator import SimData, SimDevice

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector.drivers.modbus import (
    MAX_BITS,
    MAX_REGISTERS,
    ModbusDevice,
    coalesce,
    decode,
    width,
)
from pureiron_collector.plan import DataType, DevicePlan, ModbusPoint, Order, RegisterType


def point(
    tag_id: int,
    address: int,
    data_type: DataType = "uint16",
    *,
    register_type: RegisterType = "holding",
    byte_order: Order = "big",
    word_order: Order = "big",
) -> ModbusPoint:
    return ModbusPoint(tag_id, register_type, address, data_type, byte_order, word_order)


def words(payload: bytes) -> list[int]:
    return [int.from_bytes(payload[i : i + 2], "big") for i in range(0, len(payload), 2)]


@pytest.mark.parametrize(
    ("registers", "pt", "expected"),
    [
        ([0xFFFF], point(1, 0, "int16"), -1.0),
        ([0xFFFF], point(1, 0, "uint16"), 65535.0),
        ([0x0100], point(1, 0, "uint16", byte_order="little"), 1.0),
        ([0x0002], point(1, 0, "bool"), True),
        ([0x0000], point(1, 0, "bool"), False),
        (words(struct.pack(">i", -123456)), point(1, 0, "int32"), -123456.0),
        (words(struct.pack(">I", 4000000000)), point(1, 0, "uint32"), 4000000000.0),
        (words(struct.pack(">f", 12.5)), point(1, 0, "float32"), 12.5),
        (words(struct.pack(">f", 12.5))[::-1], point(1, 0, "float32", word_order="little"), 12.5),
        (words(struct.pack(">d", -0.1)), point(1, 0, "float64"), -0.1),
        (words(struct.pack(">d", -0.1))[::-1], point(1, 0, "float64", word_order="little"), -0.1),
        ([True], point(1, 0, "uint16", register_type="coil"), True),
    ],
)
def test_decode(registers: Sequence[int | bool], pt: ModbusPoint, expected: float | bool) -> None:
    value = decode(registers, pt)
    assert value == expected
    assert type(value) is type(expected)


def test_width_by_type() -> None:
    assert [width(point(1, 0, t)) for t in ("bool", "int16", "int32", "float32", "float64")] == [
        1,
        1,
        2,
        2,
        4,
    ]
    assert width(point(1, 0, "float64", register_type="coil")) == 1


def test_coalesce_merges_adjacent_and_overlapping_per_register_type() -> None:
    points = [
        point(1, 10),
        point(2, 11, "float32"),  # 11-12
        point(3, 12),  # 겹침
        point(4, 20),  # 떨어짐 → 새 블록
        point(5, 10, register_type="input"),
    ]
    blocks = [
        (b.register_type, b.address, b.count, [p.tag_id for p in b.points])
        for b in coalesce(points)
    ]
    assert blocks == [
        ("holding", 10, 3, [1, 2, 3]),
        ("holding", 20, 1, [4]),
        ("input", 10, 1, [5]),
    ]


def test_coalesce_splits_at_protocol_limits() -> None:
    registers = coalesce([point(i, i) for i in range(2000)])
    assert all(block.count <= MAX_REGISTERS for block in registers)
    assert sum(len(block.points) for block in registers) == 2000
    assert len(registers) == -(-2000 // MAX_REGISTERS)

    # 상한 경계에 걸친 2워드 값은 쪼개지 않고 다음 블록으로 넘긴다.
    straddle = coalesce(
        [
            point(1, 0, "uint16"),
            *(point(i, i, "uint16") for i in range(1, 124)),
            point(999, 124, "float32"),
        ]
    )
    assert [(b.address, b.count) for b in straddle] == [(0, 124), (124, 2)]

    bits = coalesce([point(i, i, register_type="coil") for i in range(MAX_BITS + 1)])
    assert [b.count for b in bits] == [MAX_BITS, 1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sim_device() -> SimDevice:
    """holding 0..299, coil 0..299. 값은 테스트가 읽을 위치에 미리 심는다."""
    holding = [0] * 300
    holding[10:13] = [7, *words(struct.pack(">f", 2.5))]
    holding[200] = 42
    coils = [False] * 300
    coils[5] = True
    registers = [SimData(0, values=holding, datatype=SimType.REGISTERS)]
    bits = [SimData(0, values=coils, datatype=SimType.BITS)]
    return SimDevice(1, simdata=(bits, bits, registers, registers))


@pytest.fixture
async def modbus_port() -> AsyncIterator[int]:
    port = free_port()
    server = ModbusTcpServer(sim_device(), address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    try:
        yield port
    finally:
        await server.shutdown()
        task.cancel()


def device_plan(port: int, points: Sequence[ModbusPoint]) -> DevicePlan:
    return DevicePlan("plc-1", "modbus_tcp", "127.0.0.1", port, 1, 100, 500, tuple(points))


async def test_poll_reads_real_server(modbus_port: int) -> None:
    port = modbus_port
    driver = ModbusDevice(
        device_plan(
            port,
            [
                point(1, 10),
                point(2, 11, "float32"),
                point(3, 200),
                point(4, 5, register_type="coil"),
            ],
        )
    )
    try:
        samples = await driver.poll()
    finally:
        await driver.close()
    by_tag = {s.tag_id: s for s in samples}
    assert {tag: s.quality for tag, s in by_tag.items()} == {
        1: pb.GOOD,
        2: pb.GOOD,
        3: pb.GOOD,
        4: pb.GOOD,
    }
    assert (by_tag[1].num, by_tag[2].num, by_tag[3].num, by_tag[4].flag) == (7.0, 2.5, 42.0, True)
    assert all(s.ts_unix_ns > 0 for s in samples)


async def test_rejected_block_is_bad_but_device_stays_connected(modbus_port: int) -> None:
    port = modbus_port
    # 5000번지는 서버 블록(0..299) 밖 → 예외 응답(ILLEGAL ADDRESS).
    driver = ModbusDevice(device_plan(port, [point(1, 10), point(2, 5000)]))
    try:
        samples = await driver.poll()
    finally:
        await driver.close()
    assert {s.tag_id: s.quality for s in samples} == {1: pb.GOOD, 2: pb.BAD}


async def test_unreachable_device_raises() -> None:
    plan = replace(device_plan(free_port(), [point(1, 0)]), timeout_ms=200)
    driver = ModbusDevice(plan)
    with pytest.raises(ConnectionError):
        await driver.poll()
    await driver.close()


def test_decode_short_response_raises() -> None:
    with pytest.raises(ValueError, match="short response"):
        decode([1], point(1, 0, "int32"))
