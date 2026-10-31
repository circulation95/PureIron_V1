"""드래프트 회수의 필드 병합 계획(M5-3a K2, ADR-025) — 순수 함수, 파일·서명을 다루지 않는다.

드래프트의 알람(`id`)·태그(`path`)에서 현장 PATCH 허용 필드만 꺼내 repo의 같은 항목에 덮는다.
드래프트에 없는 선택 필드 = 기본값(repo에서 제거). 그 밖의 필드·repo에만 있는 항목은 그대로 둔다.

허용 필드는 core `project/patch.py`의 `ALARM_FIELDS`·`TAG_FIELDS`와 같다(core 단위 테스트가 고정).
`record`는 스키마상 닫힌 객체이고 하위 필드(`deadband`·`max_interval_s`)가 모두 PATCH 대상이라
객체 통째로 옮긴다(core 단위 테스트가 스키마 `record` 속성까지 고정).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pureiron_project.canonical import Json

ALARM_FIELDS = frozenset({"setpoint", "deadband", "on_delay_s", "priority"})
TAG_FIELDS = frozenset({"description", "eu", "record"})


@dataclass(frozen=True, slots=True)
class _ItemKind:
    name: str
    directory: str
    key: str
    fields: frozenset[str]


_ALARM = _ItemKind("alarm", "alarms", "id", ALARM_FIELDS)
_TAG = _ItemKind("tag", "tags", "path", TAG_FIELDS)
_ITEM_KINDS = (_ALARM, _TAG)


@dataclass(frozen=True, slots=True)
class FieldChange:
    relpath: str
    item: str
    """`alarm temp-high`·`tag line1/temp`."""
    field: str
    before: Json
    """None = 필드 없음(스키마 기본값)."""
    after: Json


@dataclass(frozen=True, slots=True)
class MergePlan:
    files: Mapping[str, Json]
    """바뀐 파일만(상대 경로 → 병합된 데이터)."""
    changes: tuple[FieldChange, ...]
    missing: tuple[str, ...]
    """드래프트에 있지만 repo에 없는 항목(`alarm x`·`tag a/b`) — 있으면 충돌."""


def _items(data: Json) -> list[dict[str, Json]]:
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _draft_index(draft: Mapping[str, Json], kind: _ItemKind) -> dict[str, dict[str, Json]]:
    """드래프트 항목 식별자 → PATCH 허용 필드만 남긴 값."""
    index: dict[str, dict[str, Json]] = {}
    for rel, data in draft.items():
        if rel.split("/", 1)[0] != kind.directory:
            continue
        for item in _items(data):
            ident = item.get(kind.key)
            if isinstance(ident, str):
                index[ident] = {f: v for f, v in item.items() if f in kind.fields}
    return index


def _merge_item(
    rel: str, kind: _ItemKind, item: dict[str, Json], site: Mapping[str, Json]
) -> tuple[dict[str, Json], list[FieldChange]]:
    merged = {f: v for f, v in item.items() if f not in kind.fields} | dict(site)
    label = f"{kind.name} {item[kind.key]}"
    changes = [
        FieldChange(rel, label, field, item.get(field), merged.get(field))
        for field in sorted(kind.fields)
        if item.get(field) != merged.get(field)
    ]
    return merged, changes


def plan_merge(repo: Mapping[str, Json], draft: Mapping[str, Json]) -> MergePlan:
    """repo·드래프트 트리(상대 경로 → 정규 데이터, 기본값 생략) → 필드 병합 계획.

    repo에서 앞서 나간 변경(새 항목, PATCH 대상이 아닌 필드)은 보존한다. 같은 필드를 repo도
    바꿨다면 현장 값이 이긴다(공통 base가 없어 구분할 수 없다 — 바뀐 필드 전·후를 출력해
    PR 리뷰에서 사람이 판단, 계획 R1).
    """
    files: dict[str, Json] = {}
    changes: list[FieldChange] = []
    found: set[tuple[str, str]] = set()
    indexes = {kind.name: _draft_index(draft, kind) for kind in _ITEM_KINDS}
    for rel in sorted(repo, key=lambda r: r.encode("utf-8")):
        kind = next((k for k in _ITEM_KINDS if rel.split("/", 1)[0] == k.directory), None)
        data = repo[rel]
        if kind is None or not isinstance(data, list):
            continue
        index = indexes[kind.name]
        merged_list: list[Json] = []
        file_changes: list[FieldChange] = []
        for raw in data:
            ident = raw.get(kind.key) if isinstance(raw, dict) else None
            if not isinstance(raw, dict) or not isinstance(ident, str) or ident not in index:
                merged_list.append(raw)
                continue
            found.add((kind.name, ident))
            merged, item_changes = _merge_item(rel, kind, raw, index[ident])
            merged_list.append(merged)
            file_changes.extend(item_changes)
        if file_changes:
            files[rel] = merged_list
            changes.extend(file_changes)
    missing = tuple(
        f"{kind.name} {ident}"
        for kind in _ITEM_KINDS
        for ident in sorted(indexes[kind.name])
        if (kind.name, ident) not in found
    )
    return MergePlan(files=files, changes=tuple(changes), missing=missing)
