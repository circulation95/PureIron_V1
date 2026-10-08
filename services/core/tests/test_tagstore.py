"""TagStore: 현재값 반영·역행 무시·BAD 표시, 이벤트는 상태 반영 뒤 락 구간 밖에서 발행."""

from __future__ import annotations

import math

from pureiron_core.platform.bus import Event, EventBus
from pureiron_core.tags.ports import Quality, Reading, TagKey, json_value
from pureiron_core.tags.store import TagStore

NOW = 10**15  # 테스트 샘플 시각(100~10_000ns)보다 충분히 뒤
KEYS = (TagKey(1, "line1/temp", "plc-1"), TagKey(2, "line1/running", "plc-1"))


def _store() -> TagStore:
    store = TagStore()
    store.reindex(KEYS)
    return store


def test_new_tags_start_bad_without_value() -> None:
    store = _store()
    assert (store.summary().total, store.summary().good) == (2, 0)
    tag = store.get(1)
    assert tag is not None
    assert (tag.value, tag.quality, tag.path) == (None, Quality.BAD, "line1/temp")


def test_commit_updates_values_and_emits_only_changes() -> None:
    store = _store()
    events = store.commit(
        [Reading(1, 21.5, Quality.GOOD, 100), Reading(2, True, Quality.GOOD, 100)], NOW
    ).events
    assert [e.topic for e in events] == ["tag.changed.line1/temp", "tag.changed.line1/running"]
    assert events[0].payload["value"] == 21.5
    assert events[0].payload["quality"] == 1
    assert store.summary().good == 2

    # 같은 값·품질 → 시각만 갱신, 이벤트 없음
    assert store.commit([Reading(1, 21.5, Quality.GOOD, 200)], NOW).events == []
    tag = store.get(1)
    assert tag is not None
    assert tag.ts_ns == 200


def test_older_samples_and_unknown_tags_are_ignored() -> None:
    store = _store()
    store.commit([Reading(1, 10.0, Quality.GOOD, 500)], NOW)
    late = [Reading(1, 99.0, Quality.GOOD, 400), Reading(77, 1.0, Quality.GOOD, 600)]
    assert store.commit(late, NOW).events == []
    assert store.commit(late, NOW).accepted == []
    tag = store.get(1)
    assert tag is not None
    assert tag.value == 10.0
    assert store.get(77) is None


def test_bad_keeps_last_value() -> None:
    store = _store()
    store.commit([Reading(1, 10.0, Quality.GOOD, 100)], NOW)
    events = store.commit([Reading(1, None, Quality.BAD, 200)], NOW).events
    assert [e.payload["quality"] for e in events] == [2]
    tag = store.get(1)
    assert tag is not None
    assert (tag.value, tag.quality) == (10.0, Quality.BAD)


def test_commit_bad_marks_once() -> None:
    store = _store()
    store.commit([Reading(1, 10.0, Quality.GOOD, 100), Reading(2, True, Quality.GOOD, 100)], NOW)
    events = store.commit_bad([1, 2, 99], 300)
    assert len(events) == 2
    assert store.summary().good == 0
    assert store.commit_bad([1, 2], 400) == []  # 이미 BAD
    # BAD 표시 시각보다 오래된 지연 샘플은 GOOD으로 되돌리지 않는다
    assert store.commit([Reading(1, 11.0, Quality.GOOD, 250)], NOW).events == []
    assert store.commit([Reading(1, 11.0, Quality.GOOD, 500)], NOW).events != []


def test_reindex_keeps_values_of_remaining_tags() -> None:
    store = _store()
    store.commit([Reading(1, 10.0, Quality.GOOD, 100)], NOW)
    store.reindex([TagKey(1, "line1/temp-renamed", "plc-1"), TagKey(3, "line1/new", "plc-1")])
    kept = store.get(1)
    assert kept is not None
    assert (kept.value, kept.quality, kept.path) == (10.0, Quality.GOOD, "line1/temp-renamed")
    assert store.get(2) is None
    new = store.get(3)
    assert new is not None
    assert new.quality is Quality.BAD


async def test_subscriber_sees_committed_state_and_can_reenter_store() -> None:
    """Lock discipline: 발행은 상태 반영이 끝난 뒤.

    구독자가 store를 다시 읽거나 바꿔도 교착이 없고, 이미 반영된 상태를 본다.
    """
    store = _store()
    bus = EventBus()
    seen: list[tuple[object, Quality | None]] = []

    async def handler(event: Event) -> None:
        tag = store.get(1)
        seen.append((event.payload["value"], tag.quality if tag else None))
        store.commit_bad([2], 10_000)  # 재진입

    bus.subscribe("tag.changed.line1/temp", handler)
    for event in store.commit([Reading(1, 5.0, Quality.GOOD, 100)], NOW).events:
        await bus.publish(event)
    assert seen == [(5.0, Quality.GOOD)]
    running = store.get(2)
    assert running is not None
    assert running.quality is Quality.BAD


def test_json_value_maps_non_finite_to_null() -> None:
    assert json_value(math.nan) is None
    assert json_value(math.inf) is None
    assert json_value(-math.inf) is None
    assert json_value(1.5) == 1.5
    assert json_value(True) is True
    assert json_value(None) is None
    store = _store()
    (event,) = store.commit([Reading(1, math.nan, Quality.GOOD, 100)], NOW).events
    assert event.payload["value"] is None


def test_backward_clock_jump_does_not_freeze_value() -> None:
    """현재값 시각이 지금보다 미래(벽시계가 뒤로 보정됨)면 역행 검사를 건너뛴다(리뷰 HIGH-1)."""
    store = _store()
    hour = 3600 * 10**9
    store.commit([Reading(1, 1.0, Quality.GOOD, NOW + hour)], NOW + hour)  # 시계가 1시간 앞섰을 때
    commit = store.commit([Reading(1, 2.0, Quality.GOOD, NOW)], NOW)  # 보정 뒤 정상 샘플
    assert commit.accepted == [1]
    tag = store.get(1)
    assert tag is not None
    assert (tag.value, tag.ts_ns) == (2.0, NOW)


def test_nan_repeated_is_not_a_change() -> None:
    store = _store()
    assert len(store.commit([Reading(1, math.nan, Quality.GOOD, 100)], NOW).events) == 1
    repeat = store.commit([Reading(1, math.nan, Quality.GOOD, 200)], NOW)
    assert (repeat.events, repeat.accepted) == ([], [1])
    # 타입이 바뀌는 것(1.0 → True)은 변경
    assert store.commit([Reading(1, 1.0, Quality.GOOD, 300)], NOW).events != []
    assert store.commit([Reading(1, True, Quality.GOOD, 400)], NOW).events != []
