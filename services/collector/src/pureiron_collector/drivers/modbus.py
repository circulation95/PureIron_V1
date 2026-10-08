"""Modbus TCP 드라이버(legacy `drivers/modbus_client.py` 묶음 읽기·디코드 이식).

legacy와 다른 점: 묶음 읽기에 프로토콜 상한(레지스터 125, 비트 2,000 —
Modbus Application Protocol v1.1b3 §6.1-6.4)을 둔다 — 2,000태그 연속 주소를 한 요청으로
묶으면 장비가 거부한다. float64(4워드) 지원.
"""

from __future__ import annotations

import logging
import struct
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusIOException
from pymodbus.pdu import ModbusPDU

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector.drivers.common import Value, bad, good
from pureiron_collector.plan import BIT_TYPES, DevicePlan, ModbusPoint, RegisterType, width

log = logging.getLogger(__name__)

MAX_REGISTERS = 125
MAX_BITS = 2000


@dataclass(slots=True)
class Block:
    register_type: RegisterType
    address: int
    count: int
    points: list[ModbusPoint] = field(default_factory=list[ModbusPoint])


def coalesce(points: Sequence[ModbusPoint]) -> list[Block]:
    """같은 register_type에서 겹치거나 맞닿은 주소를 상한 안에서 한 요청으로 묶는다."""
    blocks: list[Block] = []
    for register_type in ("holding", "input", "coil", "discrete"):
        limit = MAX_BITS if register_type in BIT_TYPES else MAX_REGISTERS
        current: Block | None = None
        for point in sorted(
            (p for p in points if p.register_type == register_type), key=lambda p: p.address
        ):
            start, end = point.address, point.address + width(point)
            if current is not None:
                current_end = current.address + current.count
                merged_end = max(current_end, end)
                if start <= current_end and merged_end - current.address <= limit:
                    current.count = merged_end - current.address
                    current.points.append(point)
                    continue
            current = Block(register_type, start, end - start, [point])
            blocks.append(current)
    return blocks


def decode(words: Sequence[int | bool], point: ModbusPoint) -> Value:
    if point.register_type in BIT_TYPES:
        return bool(words[0])
    if point.data_type == "bool":
        return int(words[0]) != 0
    ordered = [int(word) for word in words[: width(point)]]
    if len(ordered) < width(point):
        raise ValueError(f"short response for tag {point.tag_id}")
    if point.word_order == "little":
        ordered.reverse()
    payload = b"".join(word.to_bytes(2, point.byte_order) for word in ordered)
    match point.data_type:
        case "int16" | "int32":
            return float(int.from_bytes(payload, "big", signed=True))
        case "uint16" | "uint32":
            return float(int.from_bytes(payload, "big", signed=False))
        case "float32":
            return float(struct.unpack(">f", payload)[0])
        case "float64":
            return float(struct.unpack(">d", payload)[0])


class ModbusClient(Protocol):
    """테스트가 바꿔 끼울 수 있는 pymodbus 클라이언트 표면."""

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> bool: ...

    def close(self) -> None: ...

    def read_holding_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Awaitable[ModbusPDU]: ...

    def read_input_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Awaitable[ModbusPDU]: ...

    def read_coils(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Awaitable[ModbusPDU]: ...

    def read_discrete_inputs(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> Awaitable[ModbusPDU]: ...


type ClientFactory = Callable[[DevicePlan], ModbusClient]


def tcp_client(plan: DevicePlan) -> ModbusClient:
    # 재연결은 poll 루프가 맡는다(reconnect_delay=0: pymodbus 자체 재연결 끔).
    return AsyncModbusTcpClient(
        plan.host,
        port=plan.port,
        timeout=plan.timeout_ms / 1000,
        retries=0,
        reconnect_delay=0,
    )


class ModbusDevice:
    def __init__(self, plan: DevicePlan, client_factory: ClientFactory = tcp_client) -> None:
        self._plan = plan
        self._client_factory = client_factory
        self._client: ModbusClient | None = None
        self.blocks = coalesce(plan.points)

    async def _connected_client(self) -> ModbusClient:
        if self._client is not None and self._client.connected:
            return self._client
        await self.close()
        client = self._client_factory(self._plan)
        if not await client.connect():
            client.close()
            raise ConnectionError(f"cannot connect to {self._plan.host}:{self._plan.port}")
        self._client = client
        return client

    async def _read(self, client: ModbusClient, block: Block) -> ModbusPDU:
        unit = self._plan.unit_id
        match block.register_type:
            case "holding":
                return await client.read_holding_registers(
                    block.address, count=block.count, device_id=unit
                )
            case "input":
                return await client.read_input_registers(
                    block.address, count=block.count, device_id=unit
                )
            case "coil":
                return await client.read_coils(block.address, count=block.count, device_id=unit)
            case "discrete":
                return await client.read_discrete_inputs(
                    block.address, count=block.count, device_id=unit
                )

    async def poll(self) -> list[pb.Sample]:
        client = await self._connected_client()
        samples: list[pb.Sample] = []
        for block in self.blocks:
            try:
                response = await self._read(client, block)
            except ModbusIOException as exc:
                # 응답 없음은 timeout으로 올려 health.timeout_count에 잡히게 한다.
                if "no response" in str(exc).lower():
                    raise TimeoutError(str(exc)) from exc
                raise
            ts_ns = time.time_ns()
            if response.isError():
                # 장비가 응답은 했다(연결 정상) — 이 블록 태그만 BAD.
                log.warning(
                    "device %s: %s @%d x%d rejected: %s",
                    self._plan.device_id,
                    block.register_type,
                    block.address,
                    block.count,
                    response,
                )
                samples.extend(bad((point.tag_id for point in block.points), ts_ns))
                continue
            words: Sequence[int | bool] = (
                response.bits if block.register_type in BIT_TYPES else response.registers
            )
            for point in block.points:
                offset = point.address - block.address
                try:
                    value = decode(words[offset : offset + width(point)], point)
                except (IndexError, ValueError, struct.error):
                    samples.extend(bad((point.tag_id,), ts_ns))
                else:
                    samples.append(good(point.tag_id, value, ts_ns))
        return samples

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
