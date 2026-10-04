"""프로젝트 JSON Schema 검증·기본값 생략.

스키마 정본은 `packages/contracts/project.schema.json`이고, 패키지 안 `schema/project.schema.json`은
`scripts/g1.sh contracts-gen`이 복사한 생성물이다(contracts-drift가 동기 여부를 검사).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any, Protocol, cast

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from pureiron_project.canonical import Json

type Schema = Mapping[str, object]


class Kind(StrEnum):
    """프로젝트 파일 종류 → 스키마 위치."""

    PROJECT = ""
    DEVICE = "#/$defs/device"
    TAG_LIST = "#/$defs/tagList"
    ALARM_LIST = "#/$defs/alarmList"


class _Validator(Protocol):
    def iter_errors(self, instance: object) -> Iterator[_ValidationError]: ...


class _ValidationError(Protocol):
    @property
    def message(self) -> str: ...

    @property
    def json_path(self) -> str: ...


@cache
def project_schema() -> Schema:
    text = (
        resources.files("pureiron_project")
        .joinpath("schema/project.schema.json")
        .read_text("utf-8")
    )
    return cast(Schema, json.loads(text))


@cache
def _validator(kind: Kind) -> _Validator:
    schema = project_schema()
    schema_id = str(schema["$id"])
    registry = Registry[Any]().with_resource(schema_id, Resource.from_contents(schema))
    target: Mapping[str, object] = {"$ref": schema_id + kind.value} if kind.value else schema
    return cast(_Validator, Draft202012Validator(target, registry=registry))


def validate(kind: Kind, document: Json) -> list[str]:
    """스키마 위반 메시지(`<json path>: <message>`), 정렬."""
    return sorted(f"{e.json_path}: {e.message}" for e in _validator(kind).iter_errors(document))


def _resolve(node: Schema) -> Schema:
    ref = node.get("$ref")
    while isinstance(ref, str) and ref.startswith("#/"):
        target: Schema = project_schema()
        for part in ref[2:].split("/"):
            target = cast(Schema, target[part])
        node = target
        ref = node.get("$ref")
    return node


def _node_for(kind: Kind) -> Schema:
    return _resolve({"$ref": kind.value}) if kind.value else project_schema()


def _same(a: object, b: object) -> bool:
    """기본값 비교. True == 1 같은 bool/숫자 혼동은 같은 값으로 보지 않는다."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    return a == b


def _omit(value: Json, node: Schema) -> Json:
    node = _resolve(node)
    if isinstance(value, dict):
        props = cast(Mapping[str, Schema], node.get("properties", {}))
        extra = node.get("additionalProperties")
        out: dict[str, Json] = {}
        for key, item in value.items():
            child = props.get(key)
            if child is None and isinstance(extra, Mapping):
                child = cast(Schema, extra)
            if child is not None:
                resolved = _resolve(child)
                if "default" in resolved and _same(item, resolved["default"]):
                    continue
                out[key] = _omit(item, resolved)
            else:
                out[key] = item
        return out
    if isinstance(value, list):
        items = node.get("items")
        if isinstance(items, Mapping):
            return [_omit(item, cast(Schema, items)) for item in value]
    return value


def omit_defaults(kind: Kind, document: Json) -> Json:
    """스키마 `default`와 같은 값의 필드를 지운다(정규형: 기본값 생략)."""
    return _omit(document, _node_for(kind))
