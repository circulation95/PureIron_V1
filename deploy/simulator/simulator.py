"""dev 전용 Modbus TCP 시뮬레이터(compose profile `dev`).

holding register 0..N-1에 사인파 값을 0.5초마다 쓴다. 통합 시험(M3a: 100태그 → 10s 내 GOOD)의
장비 대역이며, 현장 번들에는 들어가지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

REGISTERS = int(os.environ.get("SIM_REGISTERS", "100"))
PORT = int(os.environ.get("SIM_PORT", "5020"))
PERIOD_S = float(os.environ.get("SIM_PERIOD_S", "0.5"))
HOLDING_FUNCTION_CODE = 3

log = logging.getLogger("simulator")


def values_at(t: float) -> list[int]:
    return [int(1000 + 500 * math.sin(t / 10 + i)) for i in range(REGISTERS)]


async def update(context: ModbusServerContext) -> None:
    device = context[0]
    while True:
        device.setValues(HOLDING_FUNCTION_CODE, 0, values_at(time.monotonic()))
        await asyncio.sleep(PERIOD_S)


async def main() -> None:
    block = ModbusSequentialDataBlock(0, values_at(0.0))
    context = ModbusServerContext(devices=ModbusDeviceContext(hr=block), single=True)
    updater = asyncio.create_task(update(context))
    log.info("modbus simulator: %d holding registers on :%d", REGISTERS, PORT)
    try:
        await StartAsyncTcpServer(context=context, address=("0.0.0.0", PORT))
    finally:
        updater.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
