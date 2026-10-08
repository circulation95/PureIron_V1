"""다른 모듈이 tags에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만)."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

from pureiron_core.platform.bus import Event

TAG_CHANGED_PREFIX = "tag.changed."

type Value = float | bool


class Quality(IntEnum):
    """collector.proto `Quality`·`tag_history.quality`와 같은 값."""

    GOOD = 1
    BAD = 2
    UNCERTAIN = 3


@dataclass(frozen=True, slots=True)
class TagKey:
    tag_id: int
    path: str
    device_id: str


@dataclass(frozen=True, slots=True)
class Reading:
    """collector 샘플 하나(현재값 후보)."""

    tag_id: int
    value: Value | None
    quality: Quality
    ts_ns: int


@dataclass(frozen=True, slots=True)
class TagValue:
    tag_id: int
    path: str
    device_id: str
    value: Value | None
    quality: Quality
    ts_ns: int


@dataclass(frozen=True, slots=True)
class TagSummary:
    total: int
    good: int


def json_value(value: Value | None) -> float | bool | None:
    """JSON으로 내보낼 값. NaN/Inf는 GOOD 품질로도 올 수 있으므로 null로 바꾼다(JSON에 없는 값)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class TagStorePort(Protocol):
    """상태 변경(`commit*`)은 await 없는 동기 구간이고 발행할 이벤트를 돌려준다.

    호출자는 그 이벤트를 변경이 끝난 뒤(락 구간 밖) `EventBus.publish`로 내보낸다.
    """

    def reindex(self, keys: Iterable[TagKey]) -> None: ...

    def commit(self, readings: Sequence[Reading]) -> list[Event]: ...

    def commit_bad(self, tag_ids: Iterable[int], ts_ns: int) -> list[Event]: ...

    def get(self, tag_id: int) -> TagValue | None: ...

    def summary(self) -> TagSummary: ...
