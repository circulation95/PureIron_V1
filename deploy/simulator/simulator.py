"""dev 전용 Modbus TCP 시뮬레이터(compose profile `dev`).

holding·input register 0..N-1이 읽는 순간의 시각으로 사인파 값을 돌려준다(값이 계속 바뀐다).
통합 시험(M3a: 100태그 → 10s 내 GOOD, 2,000태그 부하)의 장비 대역이며,
현장 번들에는 들어가지 않는다.

쓰기 시험용 메모리(M3c-1 D17, `SIM_MEMORY` = M칸): holding N..N+M-1과 coil 0..max(N,M)-1은
덮어쓰지 않는 메모리다(쓴 값이 그대로 읽힌다). 거부 시험용으로 holding N+M-1 쓰기는 예외 응답
(ILLEGAL DATA VALUE), N+M-2 쓰기는 정상 응답하지만 값을 바꾸지 않는다(readback 불일치).

pymodbus 3.15부터 `ModbusDeviceContext`·`ModbusSequentialDataBlock`은 deprecated(0번지 블록은
생성 단계에서 실패)이므로 `SimDevice`의 action 훅으로 응답 직전에 레지스터를 채운다. 3.15
`SimDevice`는 쓰기를 블록에 보존하고, action 훅은 쓰기 요청에도 `set_values`와 함께 불린다
(R1 탐침 — `deploy/tests/test_simulator.py`가 고정한다).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import cast

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimAction, SimData, SimDevice

REGISTERS = int(os.environ.get("SIM_REGISTERS", "100"))
MEMORY = int(os.environ.get("SIM_MEMORY", "100"))
PORT = int(os.environ.get("SIM_PORT", "5020"))
READ_HOLDING = 3
READ_INPUT = 4
WRITE_REGISTER = 6
WRITE_REGISTERS = 16
MIN_MEMORY = 2  # 거부 시험 주소 2칸(예외·무시)
ALL_UNITS = 0  # SimDevice id 0 = 모든 unit id에 응답

log = logging.getLogger("simulator")


def value_at(t: float, address: int) -> int:
    return int(1000 + 500 * math.sin(t / 10 + address))


def action(registers: int, memory: int) -> SimAction:
    """사인파 영역은 읽을 때 채우고, 메모리 영역 끝 두 칸은 거부 시험용으로 쓰기를 가로챈다."""
    reject_at = registers + memory - 1
    ignore_at = registers + memory - 2

    async def handle(
        function_code: int,
        start_address: int,
        address: int,
        count: int,
        current_registers: list[int],
        set_values: list[int] | list[bool] | None,
    ) -> ExcCodes | None:
        if function_code in (READ_HOLDING, READ_INPUT):
            now = time.monotonic()
            for addr in range(address, min(address + count, registers)):
                current_registers[addr - start_address] = value_at(now, addr)
        elif function_code in (WRITE_REGISTER, WRITE_REGISTERS) and set_values:
            if address <= reject_at < address + count:
                return ExcCodes.ILLEGAL_VALUE
            if address <= ignore_at < address + count:
                values = cast(list[int], set_values)  # FC6·FC16은 레지스터 값
                values[ignore_at - address] = current_registers[ignore_at - start_address]
        return None

    return handle


def device(registers: int, memory: int) -> SimDevice:
    if memory < MIN_MEMORY:
        raise ValueError(f"SIM_MEMORY must be at least {MIN_MEMORY}, got {memory}")
    holding = [SimData(0, values=[0] * (registers + memory), datatype=DataType.REGISTERS)]
    inputs = [SimData(0, values=[0] * registers, datatype=DataType.REGISTERS)]
    # coil은 전부 메모리다. 칸 수는 max(N, M) — 부하 측정(SIM_REGISTERS=2000)에서도 이전처럼 N칸.
    coils = [SimData(0, values=[False] * max(registers, memory), datatype=DataType.BITS)]
    discrete = [SimData(0, values=[False] * registers, datatype=DataType.BITS)]
    return SimDevice(
        ALL_UNITS, simdata=(coils, discrete, holding, inputs), action=action(registers, memory)
    )


async def main() -> None:
    log.info(
        "modbus simulator: %d sine holding/input registers, memory holding %d..%d and coil 0..%d,"
        " write-reject %d, write-ignore %d on :%d",
        REGISTERS,
        REGISTERS,
        REGISTERS + MEMORY - 1,
        max(REGISTERS, MEMORY) - 1,
        REGISTERS + MEMORY - 1,
        REGISTERS + MEMORY - 2,
        PORT,
    )
    server = ModbusTcpServer(device(REGISTERS, MEMORY), address=("0.0.0.0", PORT))
    await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
