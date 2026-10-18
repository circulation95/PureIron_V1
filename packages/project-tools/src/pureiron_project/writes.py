"""태그 쓰기 선언(`write` 블록, ADR-013 1항)의 교차 검사 — 스키마가 못 보는 주소·형식과의 일치.

스키마(`$defs/tagWrite`)는 모양(kind별 필수 필드)만 본다. 여기서는 현장에 닿기 전에 막을 것을 본다:
쓰기 가능한 register_type(holding·coil)인지, setpoint가 숫자 형식인지, 범위·허용 값이 data_type으로
인코딩 가능한지, permissive가 존재하는 bool 태그이고 자기 자신이 아닌지.
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


def _declaration_problems(
    tag: Tag, write: Mapping[str, Json], tags: Mapping[str, Tag]
) -> list[str]:
    modbus = tag.get("modbus")
    if not isinstance(modbus, dict):
        return []
    register_type, data_type = modbus.get("register_type"), str(modbus.get("data_type"))
    if register_type not in WRITABLE_REGISTER_TYPES:
        return [f"register_type {register_type!r} is read-only (write needs holding or coil)"]
    bit = is_bool_tag(tag)
    problems: list[str] = []
    if write.get("kind") == "setpoint":
        if bit:
            problems.append("setpoint needs a numeric data_type (use kind: command for bits)")
        else:
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
    permissive = write.get("permissive")
    if isinstance(permissive, list):
        for path in permissive:
            other = tags.get(str(path))
            if path == tag.get("path"):
                problems.append(f"permissive {path!r} is the tag itself")
            elif other is None:
                problems.append(f"permissive {path!r} is not a known tag")
            elif not is_bool_tag(other):
                problems.append(f"permissive {path!r} is not a bool tag")
    return problems


def check_writes(tags: Iterable[tuple[str, Tag]]) -> list[str]:
    """(파일 상대 경로, 태그) 목록 → `<relpath>: tag <path> write: <문제>` 목록."""
    listed = list(tags)
    by_path = {str(tag.get("path")): tag for _, tag in listed}
    problems: list[str] = []
    for relpath, tag in listed:
        write = tag.get("write")
        if not isinstance(write, dict):
            continue
        problems.extend(
            f"{relpath}: tag {tag.get('path')!r} write: {problem}"
            for problem in _declaration_problems(tag, write, by_path)
        )
    return problems
