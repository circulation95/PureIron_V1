"""태그 쓰기 선언(`write` 블록, ADR-013 1항)의 교차 검사 — 스키마가 못 보는 주소·형식과의 일치.

스키마(`$defs/tagWrite`)는 모양(kind별 필수 필드)만 본다. 여기서는 현장에 닿기 전에 막을 것을 본다:
쓰기 가능한 register_type(holding·coil)인지, holding bool(레지스터 16비트 전체 덮어씀)이 아닌지,
setpoint가 숫자 형식인지, 범위·허용 값이 data_type으로 인코딩 가능한지, permissive가 존재하는 bool
태그이고 자기 자신도 쓰기 대상도 아닌지, 쓰기 태그끼리 주소 범위가 겹치지 않는지.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from pureiron_project.canonical import Json

WRITABLE_REGISTER_TYPES = frozenset({"holding", "coil"})
BIT_REGISTER_TYPES = frozenset({"coil", "discrete"})
INTEGER_RANGES: Mapping[str, tuple[int, int]] = {
    "int16": (-(2**15), 2**15 - 1),
    "uint16": (0, 2**16 - 1),
    "int32": (-(2**31), 2**31 - 1),
    "uint32": (0, 2**32 - 1),
}
FLOAT32_MAX = 3.4028234663852886e38
WORDS: Mapping[str, int] = {"int32": 2, "uint32": 2, "float32": 2, "float64": 4}  # 그 밖은 1워드

type Tag = Mapping[str, Json]


def is_bool_tag(tag: Tag) -> bool:
    modbus = tag.get("modbus")
    if not isinstance(modbus, dict):
        return False
    return modbus.get("register_type") in BIT_REGISTER_TYPES or modbus.get("data_type") == "bool"


def value_problem(value: Json, data_type: str, *, bit: bool) -> str | None:
    """`value`를 이 형식으로 장비에 쓸 수 없으면 이유, 쓸 수 있으면 None."""
    if bit:
        return None if isinstance(value, bool) else f"{value!r} is not a boolean"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"{value!r} is not a number"
    if data_type in INTEGER_RANGES:
        low, high = INTEGER_RANGES[data_type]
        if not float(value).is_integer() or not low <= value <= high:
            return f"{value!r} is not an integer in {data_type} range [{low}, {high}]"
        return None
    if data_type == "float32" and abs(value) > FLOAT32_MAX:
        return f"{value!r} is outside float32 range"
    return None if math.isfinite(value) else f"{value!r} is not finite"


def _value_problems(write: Mapping[str, Json], data_type: str, *, bit: bool) -> list[str]:
    problems: list[str] = []
    if write.get("kind") == "setpoint":
        if bit:
            return ["setpoint needs a numeric data_type (use kind: command for bits)"]
        low, high = write.get("min"), write.get("max")
        problems.extend(
            f"{name}: {problem}"
            for name, bound in (("min", low), ("max", high))
            if (problem := value_problem(bound, data_type, bit=False)) is not None
        )
        if isinstance(low, int | float) and isinstance(high, int | float) and low > high:
            problems.append(f"min {low!r} is greater than max {high!r}")
    values = write.get("values")
    if isinstance(values, list):
        problems.extend(
            f"values: {problem}"
            for value in values
            if (problem := value_problem(value, data_type, bit=bit)) is not None
        )
    return problems


def _permissive_problems(tag: Tag, write: Mapping[str, Json], tags: Mapping[str, Tag]) -> list[str]:
    permissive = write.get("permissive")
    if not isinstance(permissive, list):
        return []
    problems: list[str] = []
    for path in permissive:
        other = tags.get(str(path))
        if path == tag.get("path"):
            problems.append(f"permissive {path!r} is the tag itself")
        elif other is None:
            problems.append(f"permissive {path!r} is not a known tag")
        elif not is_bool_tag(other):
            problems.append(f"permissive {path!r} is not a bool tag")
        elif "write" in other:
            # PureIron이 자기 허가 조건을 켤 수 있으면 PLC 측 허가 조건이 아니다(ADR-013 1항).
            problems.append(f"permissive {path!r} is itself writable")
    return problems


def _declaration_problems(
    tag: Tag, write: Mapping[str, Json], tags: Mapping[str, Tag]
) -> list[str]:
    modbus = tag.get("modbus")
    if not isinstance(modbus, dict):
        return []
    register_type, data_type = modbus.get("register_type"), str(modbus.get("data_type"))
    if register_type not in WRITABLE_REGISTER_TYPES:
        return [f"register_type {register_type!r} is read-only (write needs holding or coil)"]
    if register_type == "holding" and data_type == "bool":
        return [
            "data_type bool on a holding register would overwrite all 16 bits"
            " (declare a coil, or a uint16 command)"
        ]
    return _value_problems(write, data_type, bit=is_bool_tag(tag)) + _permissive_problems(
        tag, write, tags
    )


def _span(tag: Tag) -> tuple[str, str, int, int] | None:
    """(장비, register_type, 첫 주소, 끝 주소) — 쓰기 태그 주소 겹침 검사용."""
    modbus = tag.get("modbus")
    if not isinstance(modbus, dict):
        return None
    address = modbus.get("address")
    if not isinstance(address, int) or isinstance(address, bool):
        return None
    register_type = str(modbus.get("register_type"))
    words = 1 if register_type in BIT_REGISTER_TYPES else WORDS.get(str(modbus.get("data_type")), 1)
    return str(tag.get("device")), register_type, address, address + words - 1


def _overlap_problems(writable: list[tuple[str, Tag]]) -> list[str]:
    """쓰기 태그끼리 주소 범위가 겹치면 한 쪽 쓰기가 다른 쪽 값을 바꾼다 — 둘 다 거부."""
    spans = [(relpath, tag, span) for relpath, tag in writable if (span := _span(tag)) is not None]
    problems: list[str] = []
    for relpath, tag, (device, register_type, first, last) in spans:
        problems.extend(
            f"{relpath}: tag {tag.get('path')!r} write: {register_type} {first}-{last}"
            f" overlaps writable tag {other.get('path')!r} ({o_first}-{o_last})"
            for _, other, (o_device, o_type, o_first, o_last) in spans
            if other is not tag
            and (o_device, o_type) == (device, register_type)
            and first <= o_last
            and o_first <= last
        )
    return problems


def check_writes(tags: Iterable[tuple[str, Tag]]) -> list[str]:
    """(파일 상대 경로, 태그) 목록 → `<relpath>: tag <path> write: <문제>` 목록."""
    listed = list(tags)
    by_path = {str(tag.get("path")): tag for _, tag in listed}
    writable: list[tuple[str, Tag]] = []
    problems: list[str] = []
    for relpath, tag in listed:
        write = tag.get("write")
        if not isinstance(write, dict):
            continue
        writable.append((relpath, tag))
        problems.extend(
            f"{relpath}: tag {tag.get('path')!r} write: {problem}"
            for problem in _declaration_problems(tag, write, by_path)
        )
    return problems + _overlap_problems(writable)
