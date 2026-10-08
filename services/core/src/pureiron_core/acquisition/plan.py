"""DB 정의(`Definitions`) → `AcquisitionPlan`(collector.proto)과 TagStore 색인.

정의는 project-tools 정규형(스키마 기본값 생략)이라 기본값은 여기서 채운다(값은
`packages/contracts/project.schema.json`의 `default`와 같다). collector는 `port=0`인 장비를
거부하므로 포트가 비면 프로토콜 기본 포트(Modbus TCP 502)를 채운다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.project.ports import Definitions, DeviceDef, TagDef
from pureiron_core.tags.ports import TagKey

DEFAULT_POLL_MS = 1000
DEFAULT_TIMEOUT_MS = 1000
DEFAULT_UNIT_ID = 1
DEFAULT_ORDER = "big"
DEFAULT_PORTS: Mapping[str, int] = {"modbus_tcp": 502}


@dataclass(frozen=True, slots=True)
class BuiltPlan:
    plan: pb.AcquisitionPlan
    keys: tuple[TagKey, ...]
    problems: tuple[str, ...]


def _int(data: Mapping[str, object], key: str, default: int) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _str(data: Mapping[str, object], key: str, default: str = "") -> str:
    value = data.get(key)
    return value if isinstance(value, str) and value else default


def _device(device: DeviceDef) -> pb.Device:
    data = device.definition
    protocol = _str(data, "protocol")
    return pb.Device(
        device_id=device.device_id,
        protocol=protocol,
        host=_str(data, "host"),
        port=_int(data, "port", 0) or DEFAULT_PORTS.get(protocol, 0),
        unit_id=_int(data, "unit_id", DEFAULT_UNIT_ID),
        poll_ms=_int(data, "poll_ms", DEFAULT_POLL_MS),
        timeout_ms=_int(data, "timeout_ms", DEFAULT_TIMEOUT_MS),
    )


def _tag(tag: TagDef) -> pb.TagPoint | None:
    modbus = tag.definition.get("modbus")
    if not isinstance(modbus, dict):
        return None
    address = cast("dict[str, object]", modbus)  # 정의는 JSON 객체(키는 항상 문자열)
    return pb.TagPoint(
        tag_id=tag.tag_id,
        device_id=tag.device_id,
        modbus=pb.ModbusAddress(
            register_type=_str(address, "register_type"),
            address=_int(address, "address", 0),
            data_type=_str(address, "data_type"),
            byte_order=_str(address, "byte_order", DEFAULT_ORDER),
            word_order=_str(address, "word_order", DEFAULT_ORDER),
        ),
    )


def build_plan(definitions: Definitions) -> BuiltPlan:
    devices = [_device(device) for device in definitions.devices]
    known = {device.device_id for device in devices}
    problems: list[str] = []
    points: list[pb.TagPoint] = []
    keys: list[TagKey] = []
    for tag in definitions.tags:
        if tag.device_id not in known:
            problems.append(f"tag {tag.path!r}: device {tag.device_id!r} is not active")
            continue
        point = _tag(tag)
        if point is None:
            problems.append(f"tag {tag.path!r}: no supported address")
            continue
        points.append(point)
        keys.append(TagKey(tag.tag_id, tag.path, tag.device_id))
    plan = pb.AcquisitionPlan(
        plan_rev=definitions.rev, devices=devices, tags=points, default_poll_ms=DEFAULT_POLL_MS
    )
    return BuiltPlan(plan=plan, keys=tuple(keys), problems=tuple(problems))
