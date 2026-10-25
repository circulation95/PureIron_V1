"""legacy 파일 읽기와 보고 전용 영역(UDT·scripts·dashboard·auth·settings·그 밖)(M5-1, PD-12).

보고 전용 영역은 변환하지 않으므로, 읽지 못해도 실패하지 않고 파일 단위 항목으로 남긴다.
`auth/`·`settings/`·사용자별 dashboard는 키(사용자 이름)·값을 보고서에 옮기지 않고 개수만 쓴다.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from pureiron_project.canonical import Json
from pureiron_project.legacy_items import Notes

DOCUMENT_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
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


def _plain(value: object, where: str) -> Json:
    """legacy YAML 값 → JSON 데이터 모델. 날짜는 ISO 문자열(legacy pydantic이 문자열로 받던 값)."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(k): _plain(v, f"{where}.{k}") for k, v in mapping.items()}
    if isinstance(value, list):
        return [_plain(v, where) for v in cast(list[object], value)]
    raise ConvertError(f"{where}: unsupported YAML value {type(value).__name__}")


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} is not valid JSON")


def read_document(path: Path, rel: str) -> Json:
    """legacy JSON·YAML 파일. YAML은 legacy(PyYAML)와 같은 1.1 규칙으로 읽는다(`yes` → true 등)."""
    try:
        text = path.read_text(encoding="utf-8").removeprefix("﻿")
        if path.suffix == ".json":
            return cast(Json, json.loads(text, parse_constant=_reject_constant))
        yaml = cast(_Loader, YAML(typ="safe", pure=True))
        yaml.version = (1, 1)
        return _plain(yaml.load(text), rel)
    except (OSError, UnicodeDecodeError, ValueError, YAMLError) as exc:
        raise ConvertError(f"{rel}: cannot read ({exc})") from exc


def mappings(value: Json) -> Iterator[tuple[int, dict[str, Json]]]:
    """목록 안 mapping 항목(위치 포함). 목록이 아니면 없음."""
    for index, item in enumerate(value if isinstance(value, list) else []):
        if isinstance(item, dict):
            yield index, item


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
        for index, item in mappings(data):
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
