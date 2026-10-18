"""dev 시뮬레이터(M3c-1 D17): 메모리 칸이 쓰기를 보존하고 거부 시험 주소가 정해진 대로 답하는지.

R1 탐침을 고정한다 — pymodbus 3.15 `SimDevice`가 쓰기를 블록에 남기고 action 훅이 쓰기에도
불린다는 가정이 깨지면(pymodbus 업그레이드) 여기서 먼저 실패한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncGenerator, AsyncIterator

import pytest
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from simulator import device, value_at

SINE = 4  # holding·input 0..3 = 사인파
MEMORY = 8  # holding 4..11, coil 0..7 = 메모리
REJECT_AT = SINE + MEMORY - 1  # 11: 예외 응답
IGNORE_AT = SINE + MEMORY - 2  # 10: 응답은 정상, 값은 그대로


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def _serve(registers: int, memory: int) -> AsyncGenerator[AsyncModbusTcpClient]:
    port = _free_port()
    server = ModbusTcpServer(device(registers, memory), address=("127.0.0.1", port))
    task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0.2)
    modbus = AsyncModbusTcpClient("127.0.0.1", port=port)
    await modbus.connect()
    try:
        yield modbus
    finally:
        modbus.close()
        await server.shutdown()
        task.cancel()


@pytest.fixture
async def client() -> AsyncIterator[AsyncModbusTcpClient]:
    async with _serve(SINE, MEMORY) as modbus:
        yield modbus


async def test_compose_default_layout() -> None:
    """compose 기본값(100·100): 16의 배수가 아닌 coil 칸 수도 끝까지 쓴다."""
    async with _serve(100, 100) as modbus:
        assert not (await modbus.write_coil(99, True)).isError()
        assert (await modbus.read_coils(99, count=1)).bits[0] is True
        assert not (await modbus.write_register(150, 42)).isError()
        assert (await modbus.read_holding_registers(150, count=1)).registers == [42]
        assert (await modbus.write_register(199, 1)).isError()
        assert (await modbus.read_holding_registers(200, count=1)).isError()  # 블록 밖


async def test_memory_registers_keep_written_values(client: AsyncModbusTcpClient) -> None:
    assert not (await client.write_register(SINE, 1234)).isError()
    assert not (await client.write_registers(SINE + 2, [7, 8])).isError()
    # 사인파 영역과 한 요청으로 묶어 읽어도 메모리 칸은 덮어쓰지 않는다.
    response = await client.read_holding_registers(SINE - 1, count=5)
    assert response.registers[1:] == [1234, 0, 7, 8]


async def test_sine_area_is_overwritten_on_read(client: AsyncModbusTcpClient) -> None:
    assert not (await client.write_register(0, 1)).isError()
    value = (await client.read_holding_registers(0, count=1)).registers[0]
    assert value != 1
    assert 500 <= value <= 1500  # value_at 범위
    assert value_at(0.0, 0) == 1000


async def test_memory_coils_keep_written_values(client: AsyncModbusTcpClient) -> None:
    assert not (await client.write_coil(3, True)).isError()
    assert not (await client.write_coil(MEMORY - 1, True)).isError()
    bits = (await client.read_coils(0, count=MEMORY)).bits[:MEMORY]
    assert bits == [False, False, False, True, False, False, False, True]
    assert not (await client.write_coil(3, False)).isError()
    assert (await client.read_coils(3, count=1)).bits[0] is False


async def test_reject_address_answers_with_exception(client: AsyncModbusTcpClient) -> None:
    response = await client.write_register(REJECT_AT, 5)
    assert response.isError()
    assert response.exception_code == ExcCodes.ILLEGAL_VALUE
    # 여러 칸 쓰기에 걸쳐도 거부하고, 아무 칸도 바꾸지 않는다.
    assert (await client.write_registers(REJECT_AT - 1, [1, 2])).isError()
    assert (await client.read_holding_registers(REJECT_AT - 1, count=2)).registers == [0, 0]


async def test_ignore_address_acks_but_keeps_old_value(client: AsyncModbusTcpClient) -> None:
    assert not (await client.write_register(IGNORE_AT, 99)).isError()
    assert (await client.read_holding_registers(IGNORE_AT, count=1)).registers == [0]
    # 묶음 쓰기의 다른 칸은 정상 반영된다.
    assert not (await client.write_registers(IGNORE_AT - 1, [5, 6])).isError()
    assert (await client.read_holding_registers(IGNORE_AT - 1, count=2)).registers == [5, 0]


async def test_coils_cover_the_sine_area_when_it_is_larger() -> None:
    """부하 측정(SIM_REGISTERS=2000 > SIM_MEMORY) 때도 coil은 N칸 — 이전 칸 수를 줄이지 않는다."""
    async with _serve(20, 4) as modbus:
        assert not (await modbus.write_coil(19, True)).isError()
        assert (await modbus.read_coils(19, count=1)).bits[0] is True


def test_memory_needs_room_for_both_reject_addresses() -> None:
    with pytest.raises(ValueError, match="SIM_MEMORY"):
        device(SINE, 1)
