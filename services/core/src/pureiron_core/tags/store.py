"""TagStore: 태그 현재값(메모리). legacy `tags/store.py`의 락 규율(ADR-009)을 asyncio로 옮겼다.

core는 단일 이벤트 루프라 await 없는 동기 메서드 하나가 곧 원자 구간(= 락 구간)이다. 그래서
상태 변경은 `commit*` 동기 메서드에서 끝내고, `tag.changed.<path>` 이벤트는 돌려주기만 한다 —
발행(구독자 await, DB I/O)은 호출자가 변경이 끝난 뒤 한다. 구독자가 발행 중 store를 다시 읽거나
바꿔도 교착이 없고, 구독자는 이미 반영된 상태를 본다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pureiron_core.platform.bus import Event
from pureiron_core.tags.ports import (
    TAG_CHANGED_PREFIX,
    Quality,
    Reading,
    TagKey,
    TagSummary,
    TagValue,
    json_value,
)


def _event(tag: TagValue) -> Event:
    return Event(
        TAG_CHANGED_PREFIX + tag.path,
        {
            "tag_id": tag.tag_id,
            "path": tag.path,
            "device_id": tag.device_id,
            "value": json_value(tag.value),
            "quality": int(tag.quality),
            "ts_ns": tag.ts_ns,
        },
    )


class TagStore:
    def __init__(self) -> None:
        self._tags: dict[int, TagValue] = {}

    def reindex(self, keys: Iterable[TagKey]) -> None:
        """plan이 바뀌면 태그 집합을 맞춘다.

        남는 태그는 현재값을 유지하고 새 태그는 BAD(값 없음)로 시작한다.
        """
        tags: dict[int, TagValue] = {}
        for key in keys:
            old = self._tags.get(key.tag_id)
            if old is not None:
                tags[key.tag_id] = TagValue(
                    key.tag_id, key.path, key.device_id, old.value, old.quality, old.ts_ns
                )
            else:
                tags[key.tag_id] = TagValue(
                    key.tag_id, key.path, key.device_id, None, Quality.BAD, 0
                )
        self._tags = tags

    def commit(self, readings: Sequence[Reading]) -> list[Event]:
        """샘플을 반영한다. 모르는 태그·현재값보다 오래된 샘플은 버린다.

        값이나 품질이 바뀐 태그만 이벤트를 만든다.
        """
        events: list[Event] = []
        for reading in readings:
            old = self._tags.get(reading.tag_id)
            if old is None or reading.ts_ns < old.ts_ns:
                continue
            value = reading.value
            if value is None and reading.quality is not Quality.GOOD:
                value = old.value  # 값 없는 BAD는 마지막 값을 지우지 않는다(품질만 바뀜).
            new = TagValue(
                old.tag_id, old.path, old.device_id, value, reading.quality, reading.ts_ns
            )
            self._tags[reading.tag_id] = new
            if (new.value, new.quality) != (old.value, old.quality):
                events.append(_event(new))
        return events

    def commit_bad(self, tag_ids: Iterable[int], ts_ns: int) -> list[Event]:
        """태그를 BAD로 표시한다(마지막 값 유지). 이미 BAD인 태그는 이벤트 없음."""
        events: list[Event] = []
        for tag_id in tag_ids:
            old = self._tags.get(tag_id)
            if old is None or old.quality is Quality.BAD:
                continue
            new = TagValue(
                old.tag_id, old.path, old.device_id, old.value, Quality.BAD, max(ts_ns, old.ts_ns)
            )
            self._tags[tag_id] = new
            events.append(_event(new))
        return events

    def get(self, tag_id: int) -> TagValue | None:
        return self._tags.get(tag_id)

    def summary(self) -> TagSummary:
        good = sum(1 for tag in self._tags.values() if tag.quality is Quality.GOOD)
        return TagSummary(total=len(self._tags), good=good)
