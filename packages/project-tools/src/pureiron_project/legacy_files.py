"""legacy 파일 읽기와 보고 전용 영역(UDT·scripts·dashboard·auth·settings·그 밖)(M5-1, PD-12).

보고 전용 영역은 변환하지 않으므로, 읽지 못해도 실패하지 않고 파일 단위 항목으로 남긴다.
`auth/`·`settings/`·사용자별 dashboard는 키(사용자 이름)·값을 보고서에 옮기지 않고 개수만 쓴다.
legacy 파일은 신뢰할 수 없는 입력이다: YAML alias를 펼친 값이 `MAX_NODES`를 넘거나 중첩이 너무
깊으면 읽지 못한 파일로 다루고, 오류 메시지에는 파일 내용(비밀값일 수 있는 값)을 넣지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import MarkedYAMLError, YAMLError

from pureiron_project.canonical import Json
from pureiron_project.legacy_values import Notes

DOCUMENT_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
# alias를 펼친 뒤의 값 개수 상한 — 10개씩 6단 alias(1 KB)가 10^6개가 된다.
MAX_NODES = 1_000_000
# 디렉터리 단위로 통째 미지원인 legacy 영역: 첫 경로 조각 → (kind, 이유).
_WHOLE_FILE_AREAS = {
    "scripts": ("script", "scripts are not part of the V1 project (No plugin system)"),
    "auth": ("users", "user accounts are operational data; credentials and PII are never copied"),
    "settings": ("settings", "per-user settings are not part of the V1 project"),
}
_ITEM_KEYS = ("name", "path", "type_id", "id")


class _Loader(Protocol):
    version: tuple[int, int] | None

    def load(self, stream: str) -> object: ...


class ConvertError(ValueError):
    """변환할 수 없는 입력·출력 위치. CLI는 exit 1."""


class _Budget:
    def __init__(self, where: str) -> None:
        self.where = where
        self.left = MAX_NODES

    def spend(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise ConvertError(f"{self.where}: more than {MAX_NODES} values (YAML aliases?)")


def _plain(value: object, where: str, budget: _Budget) -> Json:
    """legacy YAML 값 → JSON 데이터 모델. 날짜는 ISO 문자열(legacy pydantic이 문자열로 받던 값)."""
    budget.spend()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        out: dict[str, Json] = {}
        for k, v in cast(Mapping[object, object], value).items():
            key = str(k)
            if key in out:
                raise ConvertError(f"{where}: keys {key!r} collide as text")
            out[key] = _plain(v, f"{where}.{key}", budget)
        return out
    if isinstance(value, list):
        return [_plain(v, where, budget) for v in cast(list[object], value)]
    raise ConvertError(f"{where}: unsupported YAML value {type(value).__name__}")


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} is not valid JSON")


def parse_json(text: str) -> Json:
    """JSON 데이터 모델 밖의 값(NaN·Infinity)은 ValueError."""
    return cast(Json, json.loads(text, parse_constant=_reject_constant))


def _yaml_problem(exc: YAMLError) -> str:
    """줄·열과 문제 종류만. ruamel 기본 메시지는 원문 줄(비밀값일 수 있음)을 그대로 보여 준다."""
    problem = "invalid YAML"
    if isinstance(exc, DuplicateKeyError):
        problem = "duplicate key"
    elif isinstance(exc, MarkedYAMLError):
        detail = cast(object, getattr(exc, "problem", None))
        if isinstance(detail, str) and detail:
            problem = detail
    mark = cast(object, getattr(exc, "problem_mark", None))
    line, column = getattr(mark, "line", None), getattr(mark, "column", None)
    if isinstance(line, int) and isinstance(column, int):
        return f"YAML {problem} at line {line + 1} column {column + 1}"
    return f"YAML {problem}"


def read_document(path: Path, rel: str) -> Json:
    """legacy JSON·YAML 파일. YAML은 legacy(PyYAML)와 같은 1.1 규칙으로 읽는다(`yes` → true 등)."""
    try:
        text = path.read_text(encoding="utf-8").removeprefix("﻿")
        if path.suffix == ".json":
            return parse_json(text)
        yaml = cast(_Loader, YAML(typ="safe", pure=True))
        yaml.version = (1, 1)
        return _plain(yaml.load(text), rel, _Budget(rel))
    except ConvertError:
        raise
    except YAMLError as exc:
        raise ConvertError(f"{rel}: cannot read ({_yaml_problem(exc)})") from None
    except RecursionError:
        raise ConvertError(f"{rel}: cannot read (nested too deeply)") from None
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ConvertError(f"{rel}: cannot read ({exc})") from exc


@dataclass(frozen=True, slots=True)
class ListReport:
    """`mappings`가 목록이 아닌 값·mapping이 아닌 항목을 남길 곳. item = `<prefix>[<n>]`."""

    notes: Notes
    file: str
    kind: str
    prefix: str = ""


def mappings(
    value: Json, report: ListReport | None = None
) -> Iterator[tuple[int, dict[str, Json]]]:
    """목록 안 mapping 항목(위치 포함). report가 있으면 건너뛴 것을 보고한다(legacy는 그 파일을
    거부했다)."""
    if not isinstance(value, list):
        if report is not None and value is not None:
            report.notes.add(report.file, report.prefix or "$", report.kind, "not a list")
        return
    for index, item in enumerate(value):
        if isinstance(item, dict):
            yield index, item
        elif report is not None:
            where = f"{report.prefix}[{index}]"
            report.notes.add(report.file, where, report.kind, "entry is not a mapping")


def item_name(item: Mapping[str, Json], *keys: str, fallback: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _try_read(root: Path, rel: str) -> Json | None:
    if Path(rel).suffix not in DOCUMENT_SUFFIXES:
        return None
    try:
        return read_document(root / rel, rel)
    except ConvertError:
        return None


def _count(data: Json | None) -> str:
    if not isinstance(data, dict | list):
        return "unreadable"
    return "1 entry" if len(data) == 1 else f"{len(data)} entries"


def _udt(root: Path, rel: str, notes: Notes) -> None:
    """UDT 정의(`types.yaml`·`types/*.json`)와 인스턴스(`instances.*`) — 항목마다 보고."""
    kind = "udt_type" if rel.startswith(("udt/types", "udt/type")) else "udt_instance"
    data = _try_read(root, rel)
    if isinstance(data, dict) and len(data) == 1 and isinstance(next(iter(data.values())), list):
        data = next(iter(data.values()))
    reason = "UDTs are not part of the V1 project (expand them into device tags)"
    if isinstance(data, list):
        for index, item in mappings(data, ListReport(notes, rel, kind)):
            notes.add(rel, item_name(item, *_ITEM_KEYS, fallback=f"[{index}]"), kind, reason)
    elif isinstance(data, dict):
        notes.add(rel, item_name(data, *_ITEM_KEYS, fallback="$"), kind, reason)
    else:
        notes.add(rel, "$", kind, reason)


def _dashboard(root: Path, rel: str, notes: Notes) -> None:
    """공정 dashboard는 이름(공정 키)마다, 사용자별 dashboard는 사용자 이름 없이 개수만."""
    data = _try_read(root, rel)
    if Path(rel).name.startswith("user") or not isinstance(data, dict):
        notes.add(
            rel,
            "$",
            "dashboard",
            f"dashboards are not part of the V1 project ({_count(data)}; names not copied)",
        )
        return
    for key in sorted(data):
        notes.add(rel, key, "dashboard", "dashboards are not part of the V1 project")


def report_only(root: Path, rel: str, notes: Notes) -> None:
    """변환 대상이 아닌 파일 하나를 보고서에 남긴다."""
    area = rel.split("/", 1)[0]
    if area == "udt":
        _udt(root, rel, notes)
    elif area == "dashboard":
        _dashboard(root, rel, notes)
    elif area in _WHOLE_FILE_AREAS:
        kind, reason = _WHOLE_FILE_AREAS[area]
        detail = f" ({_count(_try_read(root, rel))})" if area != "scripts" else ""
        notes.add(rel, "$", kind, reason + detail)
    else:
        notes.add(rel, "$", "file", "unrecognised legacy file")
