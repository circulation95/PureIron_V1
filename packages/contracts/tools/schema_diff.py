"""JSON Schema 호환성 diff: base → head가 기존 문서를 깨는지 판정한다(contracts-diff, G1-3).

breaking(exit 1): 필드·$defs 제거, required 추가, type·enum 축소, const 추가·변경,
additionalProperties 닫기, 수치·길이·개수 제약 강화, pattern·format·multipleOf 추가·변경,
uniqueItems 추가, propertyNames 추가·강화, allOf 항목 추가, oneOf/anyOf 분기 감소·추가.
비호환이 아닌 변경(선택 필드 추가, 제약 완화, 설명 변경)은 통과(exit 0).

    python packages/contracts/tools/schema_diff.py <base.schema.json> <head.schema.json>
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

type Schema = Mapping[str, object]

_LOWER_BOUNDS = ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties")
_UPPER_BOUNDS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties")


def _as_schema(value: object) -> Schema | None:
    return cast(Schema, value) if isinstance(value, Mapping) else None


def _types(node: Schema) -> set[str] | None:
    raw = node.get("type")
    if raw is None:
        return None
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        types = {str(t) for t in cast(list[object], raw)}
        if "number" in types:
            types.add("integer")
        return types
    return None


def _number(node: Schema, key: str) -> float | None:
    value = node.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _string_list(node: Schema, key: str) -> list[str]:
    raw = node.get(key)
    return [str(v) for v in cast(list[object], raw)] if isinstance(raw, list) else []


def _schema_map(node: Schema, key: str) -> dict[str, Schema]:
    raw = _as_schema(node.get(key))
    if raw is None:
        return {}
    return {k: s for k, v in raw.items() if (s := _as_schema(v)) is not None}


def _compare_enum(base: Schema, head: Schema, path: str, out: list[str]) -> None:
    if "enum" not in head:
        return
    base_enum = base.get("enum")
    if not isinstance(base_enum, list):
        out.append(f"{path}: enum added")
        return
    head_values = {json.dumps(v, sort_keys=True) for v in cast(list[object], head["enum"])}
    removed = [
        v
        for v in (json.dumps(x, sort_keys=True) for x in cast(list[object], base_enum))
        if v not in head_values
    ]
    if removed:
        out.append(f"{path}: enum values removed {removed}")


def _compare_scalars(base: Schema, head: Schema, path: str, out: list[str]) -> None:
    base_types, head_types = _types(base), _types(head)
    if head_types is not None and (base_types is None or not base_types <= head_types):
        out.append(f"{path}: type narrowed {sorted(base_types or ['any'])} -> {sorted(head_types)}")

    _compare_enum(base, head, path, out)

    if "const" in head and base.get("const", object()) != head["const"]:
        out.append(f"{path}: const added or changed")

    for key in ("pattern", "format", "multipleOf"):
        if key in head and base.get(key) != head[key]:
            out.append(f"{path}: {key} added or changed")

    if head.get("uniqueItems") is True and base.get("uniqueItems") is not True:
        out.append(f"{path}: uniqueItems added")

    for key in _LOWER_BOUNDS:
        b, h = _number(base, key), _number(head, key)
        if h is not None and (b is None or h > b):
            out.append(f"{path}: {key} tightened {b} -> {h}")
    for key in _UPPER_BOUNDS:
        b, h = _number(base, key), _number(head, key)
        if h is not None and (b is None or h < b):
            out.append(f"{path}: {key} tightened {b} -> {h}")


def _compare_objects(base: Schema, head: Schema, path: str, out: list[str]) -> None:
    base_props, head_props = _schema_map(base, "properties"), _schema_map(head, "properties")
    for name in sorted(base_props.keys() - head_props.keys()):
        out.append(f"{path}.properties.{name}: removed")
    for name in sorted(base_props.keys() & head_props.keys()):
        compare(base_props[name], head_props[name], f"{path}.properties.{name}", out)

    added_required = sorted(
        set(_string_list(head, "required")) - set(_string_list(base, "required"))
    )
    if added_required:
        out.append(f"{path}: required added {added_required}")

    base_additional, head_additional = (
        base.get("additionalProperties"),
        head.get("additionalProperties"),
    )
    if head_additional is False and base_additional is not False:
        out.append(f"{path}: additionalProperties closed")
    base_add_schema, head_add_schema = _as_schema(base_additional), _as_schema(head_additional)
    if base_add_schema is not None and head_add_schema is not None:
        compare(base_add_schema, head_add_schema, f"{path}.additionalProperties", out)

    base_names, head_names = (
        _as_schema(base.get("propertyNames")),
        _as_schema(head.get("propertyNames")),
    )
    if head_names is not None:
        if base_names is None:
            out.append(f"{path}: propertyNames constraint added")
        else:
            compare(base_names, head_names, f"{path}.propertyNames", out)


def _schema_list(node: Schema, key: str) -> list[Schema]:
    raw = node.get(key)
    if not isinstance(raw, list):
        return []
    return [s for v in cast(list[object], raw) if (s := _as_schema(v)) is not None]


def compare(base: Schema, head: Schema, path: str = "$", out: list[str] | None = None) -> list[str]:
    """base 스키마로 유효했던 문서가 head에서 거부될 수 있는 변경 목록."""
    found: list[str] = [] if out is None else out
    _compare_scalars(base, head, path, found)
    _compare_objects(base, head, path, found)

    base_items, head_items = _as_schema(base.get("items")), _as_schema(head.get("items"))
    if base_items is not None and head_items is not None:
        compare(base_items, head_items, f"{path}.items", found)
    elif head_items is not None and base_items is None and "items" not in base:
        found.append(f"{path}: items constraint added")

    for key in ("oneOf", "anyOf"):
        base_list, head_list = _schema_list(base, key), _schema_list(head, key)
        if key in base and key in head:
            if len(head_list) < len(base_list):
                found.append(f"{path}.{key}: branches removed")
            for i, (b, h) in enumerate(zip(base_list, head_list, strict=False)):
                compare(b, h, f"{path}.{key}[{i}]", found)
        elif key in head:
            found.append(f"{path}: {key} constraint added")

    # allOf는 모든 항목을 만족해야 하므로 항목 추가 = 강화, 같은 위치 항목은 재귀 비교.
    base_all, head_all = _schema_list(base, "allOf"), _schema_list(head, "allOf")
    if len(head_all) > len(base_all):
        found.append(f"{path}.allOf: constraints added")
    for i, (b, h) in enumerate(zip(base_all, head_all, strict=False)):
        compare(b, h, f"{path}.allOf[{i}]", found)

    base_defs, head_defs = _schema_map(base, "$defs"), _schema_map(head, "$defs")
    for name in sorted(base_defs.keys() - head_defs.keys()):
        found.append(f"{path}.$defs.{name}: removed")
    for name in sorted(base_defs.keys() & head_defs.keys()):
        compare(base_defs[name], head_defs[name], f"{path}.$defs.{name}", found)

    if "$ref" in head and base.get("$ref") != head["$ref"]:
        found.append(f"{path}: $ref changed {base.get('$ref')} -> {head['$ref']}")
    return found


def _load(path: Path) -> Schema:
    data: object = json.loads(path.read_text(encoding="utf-8"))
    schema = _as_schema(data)
    if schema is None:
        raise SystemExit(f"schema_diff: {path} is not a JSON object")
    return schema


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    base_path, head_path = Path(argv[0]), Path(argv[1])
    breaking = compare(_load(base_path), _load(head_path))
    for item in breaking:
        print(f"::error file={head_path}::breaking schema change: {item}")
    if not breaking:
        print(f"schema_diff: {head_path.name} compatible with base")
    return 1 if breaking else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
