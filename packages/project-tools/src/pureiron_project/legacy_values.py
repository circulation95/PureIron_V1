"""legacy 변환기 공용 값 규칙 — 보고서 항목·id 만들기·legacy pydantic lax 값 해석(M5-1, PD-12).

legacy 설정은 pydantic v2 lax 모드로 검증됐다: `"true"`·`1`·`"yes"`는 True, `"502"`는 502로 받았다.
같은 입력을 같은 값으로 읽어야 주소·순서가 legacy와 같아진다. 해석할 수 없는 값은 None을 돌려주고,
호출자가 보고서에 남긴다(추측하지 않는다).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from pureiron_project.canonical import MAX_SAFE_INTEGER, Json

ID_MAX = 63
DESCRIPTION_MAX = 500
_NOT_ID = re.compile(r"[^a-z0-9]+")
# pydantic v2 lax bool 문자열(대소문자 무시).
_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE = frozenset({"0", "off", "f", "false", "n", "no"})
_INTEGER = re.compile(r"[+-]?\d+")
_DECIMAL = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


@dataclass(frozen=True, slots=True)
class Entry:
    """보고서 `unsupported[]` 한 줄. field는 변환된 항목에서 옮기지 못한 필드 이름."""

    file: str
    item: str
    kind: str
    reason: str
    field: str | None = None

    def as_json(self) -> dict[str, Json]:
        out: dict[str, Json] = {
            "file": self.file,
            "item": self.item,
            "kind": self.kind,
            "reason": self.reason,
        }
        if self.field is not None:
            out["field"] = self.field
        return out


@dataclass(slots=True)
class Notes:
    unsupported: list[Entry] = field(default_factory=list[Entry])
    secrets: list[dict[str, Json]] = field(default_factory=list[dict[str, Json]])

    def add(self, file: str, item: str, kind: str, reason: str) -> None:
        self.unsupported.append(Entry(file, item, kind, reason))

    def dropped(self, file: str, item: str, key: str, reason: str, *, kind: str = "field") -> None:
        self.unsupported.append(Entry(file, item, kind, reason, key))


class Fields:
    """읽은 키를 기억해, 읽지 않은 키를 field 항목으로 보고할 수 있게 한다."""

    def __init__(self, data: Mapping[str, Json]) -> None:
        self._data = data
        self._used: set[str] = set()

    def get(self, key: str) -> Json:
        self._used.add(key)
        return self._data.get(key)

    def rest(self) -> list[str]:
        return sorted(k for k in self._data if k not in self._used)


def slug(text: str) -> str:
    """legacy 이름 → V1 id(`^[a-z0-9][a-z0-9-]{0,62}$`). 쓸 글자가 없으면 빈 문자열."""
    lowered = unicodedata.normalize("NFKC", text).lower()
    return _NOT_ID.sub("-", lowered).strip("-")[:ID_MAX].rstrip("-")


def new_id(name: str, prefix: str, taken: set[str]) -> str | None:
    """이름 → 쓰지 않은 id. 쓸 글자가 없는 이름(예: 한글만)은 `<prefix>-<n>`(입력 순서대로),
    slug가 이미 쓰인 이름은 None(호출자가 대체 id를 만들거나 보고)."""
    base = slug(name)
    if base:
        return None if base in taken else base
    n = 1
    while f"{prefix}-{n}" in taken:
        n += 1
    return f"{prefix}-{n}"


def as_bool(value: Json) -> bool | None:
    """pydantic v2 lax bool: bool, 0/1, `"true"`/`"false"`·`"yes"`/`"no"`·`"on"`/`"off"` 등."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value in (0, 1):
        return value == 1
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    return None


def as_int(value: Json, low: int, high: int) -> int | None:
    """pydantic v2 lax int(정수, 소수부 없는 float, 정수 문자열) 중 [low, high] 안의 값."""
    number: int | None = None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and _INTEGER.fullmatch(value.strip()):
        number = int(value.strip())
    return number if number is not None and low <= number <= high else None


def as_number(value: Json) -> float | int | None:
    """유한한 수(정수는 ±2^53 안 — 정규형 출력이 표현할 수 있는 범위), 숫자 문자열 포함."""
    if isinstance(value, str) and _DECIMAL.fullmatch(value.strip()):
        value = float(value.strip())
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, int) and abs(value) > MAX_SAFE_INTEGER:
        return None
    return value


def as_text(value: Json) -> str | None:
    return value if isinstance(value, str) else None


def put_text(
    doc: dict[str, Json], key: str, value: Json, limit: int, note: tuple[Notes, str, str, str]
) -> None:
    """빈 문자열은 생략, 길이 초과·문자열 아님은 field로 보고."""
    if value is None or value == "":
        return
    if isinstance(value, str) and len(value) <= limit:
        doc[key] = value
        return
    notes, file, item, legacy_key = note
    notes.dropped(file, item, legacy_key, f"not a string of at most {limit} characters")
