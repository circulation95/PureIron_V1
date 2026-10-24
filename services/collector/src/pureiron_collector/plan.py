"""core가 보낸 `AcquisitionPlan`을 장비별 poll 단위(`DevicePlan`)로 바꾼다.

plan은 core가 project 스키마로 이미 검증한 값이지만, collector는 proto 문자열을 다시 확인한다
(버전이 다른 core가 모르는 값을 보내도 그 장비·태그만 빼고 나머지는 계속 수집한다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from pureiron_collector._gen import collector_pb2 as pb

type RegisterType = Literal["holding", "input", "coil", "discrete"]
type DataType = Literal["bool", "int16", "uint16", "int32", "uint32", "float32", "float64"]
type Order = Literal["big", "little"]

REGISTER_TYPES: frozenset[str] = frozenset({"holding", "input", "coil", "discrete"})
DATA_TYPES: frozenset[str] = frozenset(
    {"bool", "int16", "uint16", "int32", "uint32", "float32", "float64"}
)
ORDERS: frozenset[str] = frozenset({"big", "little"})
SUPPORTED_PROTOCOLS: frozenset[str] = frozenset({"modbus_tcp"})
DEFAULT_TIMEOUT_MS = 1000
DEFAULT_POLL_MS = 1000


@dataclass(frozen=True, slots=True)
class ModbusPoint:
    tag_id: int
    register_type: RegisterType
    address: int
    data_type: DataType
    byte_order: Order
    word_order: Order
    # core가 이 태그에 WriteRequest를 보낼 수 있다(TagPoint.writable). 아니면 collector가 REJECTED.
    writable: bool = False


BIT_TYPES: frozenset[str] = frozenset({"coil", "discrete"})
_WORDS = {"int32": 2, "uint32": 2, "float32": 2, "float64": 4}
ADDRESS_SPACE = 0x10000


def width(point: ModbusPoint) -> int:
    """태그 하나가 차지하는 레지스터(또는 비트) 수."""
    if point.register_type in BIT_TYPES:
        return 1
    return _WORDS.get(point.data_type, 1)


@dataclass(frozen=True, slots=True)
class DevicePlan:
    device_id: str
    protocol: str
    host: str
    port: int
    unit_id: int
    poll_ms: int
    timeout_ms: int
    points: tuple[ModbusPoint, ...]


@dataclass(frozen=True, slots=True)
class ParsedPlan:
    plan_rev: int
    devices: tuple[DevicePlan, ...]
    problems: tuple[str, ...]


def _order(value: str) -> Order | None:
    if not value:
        return "big"
    return cast(Order, value) if value in ORDERS else None


def _point(tag: pb.TagPoint) -> ModbusPoint | str:
    if tag.WhichOneof("address") != "modbus":
        return f"tag {tag.tag_id}: no supported address"
    address = tag.modbus
    if address.register_type not in REGISTER_TYPES:
        return f"tag {tag.tag_id}: unknown register_type {address.register_type!r}"
    if address.data_type not in DATA_TYPES:
        return f"tag {tag.tag_id}: unknown data_type {address.data_type!r}"
    byte_order = _order(address.byte_order)
    word_order = _order(address.word_order)
    if byte_order is None or word_order is None:
        return f"tag {tag.tag_id}: unknown byte/word order"
    point = ModbusPoint(
        tag_id=tag.tag_id,
        register_type=cast(RegisterType, address.register_type),
        address=address.address,
        data_type=cast(DataType, address.data_type),
        byte_order=byte_order,
        word_order=word_order,
        writable=tag.writable,
    )
    if point.address + width(point) > ADDRESS_SPACE:
        return f"tag {tag.tag_id}: address {point.address} out of range"
    return point


def _device_problem(device: pb.Device, seen: set[str]) -> str | None:
    if device.device_id in seen:
        return f"device {device.device_id}: duplicate device_id"
    if device.protocol not in SUPPORTED_PROTOCOLS:
        return f"device {device.device_id}: unsupported protocol {device.protocol!r}"
    if not device.host or not 0 < device.port <= 0xFFFF:
        return f"device {device.device_id}: invalid host/port {device.host!r}:{device.port}"
    if device.unit_id > 0xFF:
        return f"device {device.device_id}: unit_id {device.unit_id} out of range"
    return None


def parse_plan(plan: pb.AcquisitionPlan) -> ParsedPlan:
    problems: list[str] = []
    points: dict[str, list[ModbusPoint]] = {}
    for tag in plan.tags:
        parsed = _point(tag)
        if isinstance(parsed, str):
            problems.append(parsed)
        else:
            points.setdefault(tag.device_id, []).append(parsed)

    devices: list[DevicePlan] = []
    known: set[str] = set()
    for device in plan.devices:
        problem = _device_problem(device, known)
        known.add(device.device_id)
        if problem is not None:
            problems.append(problem)
            continue
        devices.append(
            DevicePlan(
                device_id=device.device_id,
                protocol=device.protocol,
                host=device.host,
                port=device.port,
                unit_id=device.unit_id,
                poll_ms=device.poll_ms or plan.default_poll_ms or DEFAULT_POLL_MS,
                timeout_ms=device.timeout_ms or DEFAULT_TIMEOUT_MS,
                points=tuple(points.get(device.device_id, ())),
            )
        )
    problems.extend(
        f"device {device_id}: {len(orphans)} tag(s) reference an unknown device"
        for device_id, orphans in points.items()
        if device_id not in known
    )
    return ParsedPlan(plan_rev=plan.plan_rev, devices=tuple(devices), problems=tuple(problems))
