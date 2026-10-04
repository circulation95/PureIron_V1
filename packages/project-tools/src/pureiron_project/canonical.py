"""프로젝트 파일 정규화(ADR-025, plan §6.3).

- 로더: YAML 1.2 strict — 중복 키, anchor/alias, 명시적 tag, 문자열이 아닌 키,
  JSON 데이터 모델 밖의 값(timestamp·NaN·Infinity·2^53 초과 정수 등)을 거부한다.
- 정규형: 문자열 UTF-8 NFC, 숫자는 RFC 8785(JCS) 표현, 키 정렬, 2칸 block YAML, LF, 끝 줄바꿈 1개.
  plain scalar로 다시 읽어도 같은 문자열일 때만 따옴표를 생략하고, 아니면 JSON 이중따옴표를 쓴다.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterator, Mapping
from typing import Protocol, cast

import rfc8785
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None

MAX_SAFE_INTEGER = 2**53 - 1
_PLAIN_FIRST = re.compile(r"^[^\s\-?:,\[\]{}#&*!|>'\"%@`]")
_NONPRINT = r"\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\u2028\u2029\ufeff\ufffe\uffff"
_PLAIN_FORBIDDEN = re.compile(rf"[\t\n\r{_NONPRINT}]|: |:$| #|^~$")
# YAML 비출력 문자(1.2 printable 밖)·줄바꿈으로 읽히는 문자·BOM은 따옴표 안에서 \u 이스케이프한다.
_ESCAPE_IN_QUOTES = re.compile(rf"[{_NONPRINT}]")
# YAML 1.2에서는 문자열이지만 YAML 1.1 도구가 bool/null로 읽는 값은 따옴표를 붙인다(상호운용).
_YAML11_AMBIGUOUS = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~"}


class CanonicalError(ValueError):
    pass


class _Event(Protocol):
    @property
    def anchor(self) -> str | None: ...

    @property
    def tag(self) -> str | None: ...

    @property
    def implicit(self) -> object: ...


class _Yaml(Protocol):
    def load(self, stream: str) -> object: ...

    def parse(self, stream: str) -> Iterator[object]: ...


def _yaml() -> _Yaml:
    return cast(_Yaml, YAML(typ="safe", pure=True))


def _check_events(text: str, source: str) -> None:
    for event in _yaml().parse(text):
        name = type(event).__name__
        if name == "AliasEvent":
            raise CanonicalError(f"{source}: YAML aliases are not allowed")
        typed = cast(_Event, event)
        if getattr(event, "anchor", None) is not None:
            raise CanonicalError(f"{source}: YAML anchors are not allowed")
        tag = getattr(event, "tag", None)
        if name in {"ScalarEvent", "MappingStartEvent", "SequenceStartEvent"} and tag is not None:
            implicit = typed.implicit
            explicit = not (
                implicit is True
                or (isinstance(implicit, tuple) and any(cast(tuple[bool, ...], implicit)))
            )
            if explicit or not str(tag).startswith("tag:yaml.org,2002:"):
                raise CanonicalError(f"{source}: explicit YAML tags are not allowed ({tag})")


def _to_json(value: object, source: str, path: str) -> Json:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise CanonicalError(f"{source}:{path}: integer outside ±2^53 cannot be represented")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError(f"{source}:{path}: NaN/Infinity are not allowed")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        out: dict[str, Json] = {}
        for key, item in cast(Mapping[object, object], value).items():
            if not isinstance(key, str):
                raise CanonicalError(f"{source}:{path}: mapping keys must be strings, got {key!r}")
            nkey = unicodedata.normalize("NFC", key)
            if nkey in out:
                raise CanonicalError(
                    f"{source}:{path}: duplicate key after NFC normalisation: {nkey!r}"
                )
            out[nkey] = _to_json(item, source, f"{path}.{nkey}")
        return out
    if isinstance(value, list):
        return [
            _to_json(item, source, f"{path}[{i}]")
            for i, item in enumerate(cast(list[object], value))
        ]
    raise CanonicalError(
        f"{source}:{path}: unsupported YAML value {type(value).__name__} ({value!r})"
    )


def load(text: str, source: str = "<input>") -> Json:
    """YAML 1.2 strict 로드 → JSON 데이터 모델(문자열 NFC)."""
    text = text.removeprefix("﻿").replace("\r\n", "\n")
    try:
        _check_events(text, source)
        data = _yaml().load(text)
    except YAMLError as exc:
        raise CanonicalError(f"{source}: {exc}") from exc
    return _to_json(data, source, "$")


def _number(value: int | float) -> str:
    """JCS 표현. 단 2^53을 넘는 정수값 float은 YAML이 정수로 읽지 않도록 float 표기(repr)를 쓴다."""
    if isinstance(value, float) and value.is_integer() and abs(value) > MAX_SAFE_INTEGER:
        return repr(value)
    return rfc8785.dumps(value).decode("ascii")


def _is_plain(text: str) -> bool:
    if not text or text != text.strip() or "\n" in text:
        return False
    if not _PLAIN_FIRST.match(text) or _PLAIN_FORBIDDEN.search(text):
        return False
    if text.lower() in _YAML11_AMBIGUOUS:
        return False
    try:
        return _yaml().load(text) == text
    except YAMLError:
        return False


def _scalar(value: Json) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return _number(value)
    if isinstance(value, str):
        if _is_plain(value):
            return value
        quoted = json.dumps(value, ensure_ascii=False)
        return _ESCAPE_IN_QUOTES.sub(lambda m: f"\\u{ord(m[0]):04x}", quoted)
    raise CanonicalError(f"not a scalar: {value!r}")


def _lines(value: Json, indent: int) -> list[str]:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return [pad + "{}"]
        lines: list[str] = []
        for key in sorted(value):
            item = value[key]
            head = f"{pad}{_scalar(key)}:"
            if isinstance(item, dict | list) and item:
                lines.append(head)
                lines.extend(_lines(item, indent + 2))
            else:
                lines.append(f"{head} {_inline(item)}")
        return lines
    if isinstance(value, list):
        if not value:
            return [pad + "[]"]
        lines = []
        for item in value:
            if isinstance(item, dict | list) and item:
                nested = _lines(item, indent + 2)
                lines.append(f"{pad}- {nested[0].lstrip()}")
                lines.extend(nested[1:])
            else:
                lines.append(f"{pad}- {_inline(item)}")
        return lines
    return [pad + _scalar(value)]


def _inline(value: Json) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return _scalar(value)


def dump(value: Json) -> str:
    """정규형 YAML 텍스트(LF, 끝 줄바꿈). 입력도 먼저 같은 규칙(NFC·JSON 데이터 모델)으로 정규화."""
    return "\n".join(_lines(_to_json(value, "<dump>", "$"), 0)) + "\n"


def jcs(value: Json) -> bytes:
    """RFC 8785 JSON Canonicalization Scheme 바이트."""
    return rfc8785.dumps(value)


def as_json_roundtrip(value: Json) -> Json:
    """JCS가 같은 값으로 보는 형태(예: 1.0 → 1)로 맞춘다 — 테스트·비교용."""
    return cast(Json, json.loads(jcs(value)))
