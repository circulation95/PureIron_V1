"""dev 전용 Modbus TCP 시뮬레이터(compose profile `dev`).

holding·input register 0..N-1이 읽는 순간의 시각으로 사인파 값을 돌려준다(값이 계속 바뀐다).
통합 시험(M3a: 100태그 → 10s 내 GOOD, 2,000태그 부하)의 장비 대역이며,
현장 번들에는 들어가지 않는다.

pymodbus 3.15부터 `ModbusDeviceContext`·`ModbusSequentialDataBlock`은 deprecated(0번지 블록은
생성 단계에서 실패)이므로 `SimDevice`의 action 훅으로 응답 직전에 레지스터를 채운다.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

REGISTERS = int(os.environ.get("SIM_REGISTERS", "100"))
PORT = int(os.environ.get("SIM_PORT", "5020"))
READ_HOLDING = 3
READ_INPUT = 4
ALL_UNITS = 0  # SimDevice id 0 = 모든 unit id에 응답

log = logging.getLogger("simulator")


def value_at(t: float, address: int) -> int:
    return int(1000 + 500 * math.sin(t / 10 + address))


async def fill_registers(
    function_code: int,
    start_address: int,
    address: int,
    count: int,
    current_registers: list[int],
    set_values: list[int] | list[bool] | None,
) -> ExcCodes | None:
    if function_code in (READ_HOLDING, READ_INPUT):
        now = time.monotonic()
        for offset in range(count):
            current_registers[address - start_address + offset] = value_at(now, address + offset)
    return None


def device(registers: int) -> SimDevice:
    zeros = [SimData(0, values=[0] * registers, datatype=DataType.REGISTERS)]
    bits = [SimData(0, values=[False] * registers, datatype=DataType.BITS)]
    return SimDevice(ALL_UNITS, simdata=(bits, bits, zeros, zeros), action=fill_registers)


async def main() -> None:
    log.info("modbus simulator: %d holding/input registers on :%d", REGISTERS, PORT)
    server = ModbusTcpServer(device(REGISTERS), address=("0.0.0.0", PORT))
    await server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
