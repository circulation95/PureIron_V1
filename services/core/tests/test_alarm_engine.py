"""알람 상태 기계(D7): 경계·deadband·on_delay·4상태 전이표·ACK·평가 보류·평가 순서·정의 변경·복원.

legacy `backend/tests/test_alarm_engine.py` 이식분은 테스트 이름 옆 주석에 원래 이름을 적는다(K11).
"""

from __future__ import annotations

import math

import pytest

from pureiron_core.alarms.engine import AlarmEngine
from pureiron_core.alarms.ports import (
    AlarmNotAckableError,
    AlarmNotFoundError,
    AlarmState,
    AlarmTransition,
)
from pureiron_core.project.ports import AlarmDef
from pureiron_core.tags.ports import Quality
from pureiron_core.tags.store import TagStore

from .alarm_fakes import (
    TAG_PATH,
    TS0_NS,
    Clock,
    FakeScheduler,
    alarm_def,
    definitions,
    journal_record,
    set_value,
    tag_store,
)

S = AlarmState


class Harness:
    def __init__(self, tags: TagStore | None = None) -> None:
        self.tags = tags or tag_store()
        self.clock = Clock()
        self.scheduler = FakeScheduler()
        self.timer_transitions: list[AlarmTransition] = []
        self.engine = AlarmEngine(
            self.tags,
            schedule=self.scheduler,
            on_timer=self.timer_transitions.extend,
            clock=self.clock,
        )

    def value(
        self, value: float | bool | None, quality: Quality = Quality.GOOD
    ) -> list[tuple[str, AlarmState, AlarmState]]:
        """태그 값을 바꾸고 엔진에 알린다(TagStore 반영 → tag.changed 평가와 같은 순서)."""
        set_value(self.tags, value, quality=quality)
        transitions = self.engine.on_tag(TAG_PATH, value, quality, TS0_NS)
        return [(t.alarm_id, t.from_state, t.to_state) for t in transitions]

    def state(self, alarm_id: str = "tank-high") -> AlarmState:
        snapshot = self.engine.get(alarm_id)
        assert snapshot is not None
        return snapshot.state

    def ack(self, alarm_id: str = "tank-high", username: str = "op") -> AlarmTransition | None:
        return self.engine.apply_ack(self.engine.plan_ack(alarm_id, username))


def _harness() -> Harness:
    """above 8.0 알람 하나(`tank-high`)."""
    h = Harness()
    h.engine.set_definitions(definitions(alarm_def()))
    return h


def test_above_activates_at_setpoint_boundary() -> None:  # legacy test_normal_to_active_unacked
    h = _harness()
    assert h.value(7.99) == []
    assert h.value(8.0) == [("tank-high", S.NORMAL, S.ACTIVE_UNACKED)]
    snapshot = h.engine.get("tank-high")
    assert snapshot is not None
    assert (snapshot.value, snapshot.value_ts_ns, snapshot.changed_by) == (8.0, TS0_NS, None)


def test_ack_moves_active_unacked_to_active_acked() -> None:  # legacy ..._to_active_acked
    h = _harness()
    h.value(9.0)
    acked = h.ack(username="kim")
    assert acked is not None
    assert (acked.from_state, acked.to_state, acked.username) == (
        S.ACTIVE_UNACKED,
        S.ACTIVE_ACKED,
        "kim",
    )
    assert h.state() is S.ACTIVE_ACKED
    snapshot = h.engine.get("tank-high")
    assert snapshot is not None
    assert (snapshot.changed_by, snapshot.value) == ("kim", 9.0)  # ACK는 값을 바꾸지 않는다


def test_clear_of_unacked_goes_to_cleared_unacked() -> None:  # legacy ..._to_clear_unacked
    h = _harness()
    h.value(9.0)
    assert h.value(7.0) == [("tank-high", S.ACTIVE_UNACKED, S.CLEARED_UNACKED)]


def test_clear_of_acked_goes_straight_to_normal() -> None:
    """legacy test_active_acked_to_clear_acked_to_normal: V1엔 CLEAR_ACKED가 없다(재ACK 제거)."""
    h = _harness()
    h.value(9.0)
    h.ack()
    assert h.value(7.0) == [("tank-high", S.ACTIVE_ACKED, S.NORMAL)]


def test_ack_of_cleared_unacked_returns_to_normal() -> None:  # legacy test_clear_unacked_to_normal
    h = _harness()
    h.value(9.0)
    h.value(7.0)
    acked = h.ack()
    assert acked is not None
    assert (acked.from_state, acked.to_state) == (S.CLEARED_UNACKED, S.NORMAL)


def test_cleared_unacked_reactivates_when_condition_returns() -> None:
    h = _harness()
    h.value(9.0)
    h.value(7.0)
    assert h.value(8.5) == [("tank-high", S.CLEARED_UNACKED, S.ACTIVE_UNACKED)]


@pytest.mark.parametrize(
    ("clear_value", "expected"),
    [(7.6, S.ACTIVE_UNACKED), (7.5, S.ACTIVE_UNACKED), (7.49, S.CLEARED_UNACKED)],
)
def test_deadband_applies_to_clear_only(clear_value: float, expected: AlarmState) -> None:
    """legacy test_deadband_applied_on_clear_only: 활성은 8.0 그대로, 해제는 < 8.0 - 0.5."""
    h = Harness()
    h.engine.set_definitions(definitions(alarm_def(deadband=0.5)))
    h.value(8.0)
    h.value(clear_value)
    assert h.state() is expected


def test_below_alarm_boundaries_and_deadband() -> None:
    h = Harness()
    h.engine.set_definitions(
        definitions(alarm_def("tank-low", kind="below", setpoint=2.0, deadband=1.0))
    )
    assert h.value(2.01) == []
    assert h.value(2.0) == [("tank-low", S.NORMAL, S.ACTIVE_UNACKED)]
    assert h.value(3.0) == []  # 해제는 > 2.0 + 1.0
    assert h.value(3.01) == [("tank-low", S.ACTIVE_UNACKED, S.CLEARED_UNACKED)]


def test_on_delay_cancelled_when_condition_clears() -> (
    None
):  # legacy ..._resets_if_condition_clears
    h = Harness()
    h.engine.set_definitions(definitions(alarm_def(on_delay_s=5)))
    assert h.value(9.0) == []
    [timer] = h.scheduler.pending
    assert timer.delay_s == 5.0
    h.value(9.5)  # 재진입: 타이머를 다시 걸지 않는다
    assert len(h.scheduler.timers) == 1
    h.value(7.9)
    assert h.scheduler.pending == []
    h.scheduler.fire()
    assert (h.state(), h.timer_transitions) == (S.NORMAL, [])


def test_on_delay_activates_with_current_value_on_expiry() -> None:  # legacy ..._condition_persists
    h = Harness()
    h.engine.set_definitions(definitions(alarm_def(on_delay_s=5)))
    h.value(9.0)
    set_value(h.tags, 9.7, ts_ns=TS0_NS + 5_000_000_000)  # 만료 전 마지막 값(이벤트 없이)
    h.scheduler.fire()
    [t] = h.timer_transitions
    assert (t.from_state, t.to_state, t.value) == (S.NORMAL, S.ACTIVE_UNACKED, 9.7)
    assert t.snapshot.value_ts_ns == TS0_NS + 5_000_000_000


@pytest.mark.parametrize(
    ("value", "quality"),
    [(9.0, Quality.BAD), (7.0, Quality.GOOD), (None, Quality.GOOD)],
    ids=["bad", "below-setpoint", "no-value"],
)
def test_on_delay_expiry_rechecks_current_value(value: float | None, quality: Quality) -> None:
    h = Harness()
    h.engine.set_definitions(definitions(alarm_def(on_delay_s=5)))
    h.value(9.0)
    set_value(h.tags, value, quality=quality, ts_ns=TS0_NS + 1)
    h.scheduler.fire()
    assert (h.state(), h.timer_transitions) == (S.NORMAL, [])


def test_levels_evaluate_above_ascending_then_below_descending() -> None:
    """legacy test_multiple_levels_h_and_hh_can_be_active_h_first + below 순서."""
    h = Harness()
    h.engine.set_definitions(
        definitions(
            alarm_def("hh", setpoint=9.5),
            alarm_def("h", setpoint=8.0),
            alarm_def("l", kind="below", setpoint=2.0),
            alarm_def("ll", kind="below", setpoint=1.0),
        )
    )
    assert [a for a, _, _ in h.value(10.0)] == ["h", "hh"]
    assert [a for a, _, _ in h.value(0.5)] == ["h", "hh", "l", "ll"]


def test_no_retrigger_after_ack_while_condition_holds() -> (
    None
):  # legacy test_retrigger_after_ack...
    h = _harness()
    h.value(8.6)
    h.ack()
    assert h.value(8.7) == []
    assert h.state() is S.ACTIVE_ACKED


@pytest.mark.parametrize(
    ("value", "quality"),
    [
        (1.0, Quality.BAD),
        (1.0, Quality.UNCERTAIN),
        (None, Quality.GOOD),
        (float("nan"), Quality.GOOD),
    ],
    ids=["bad", "uncertain", "none", "nan"],
)
def test_evaluation_holds_without_good_finite_value(value: float | None, quality: Quality) -> None:
    """통신 끊김(BAD)·값 없음은 '해제'가 아니다 — 활성 알람은 그대로, NORMAL도 활성화되지 않는다."""
    h = _harness()
    h.value(9.0)
    assert h.value(value, quality) == []
    assert h.state() is S.ACTIVE_UNACKED
    calm = _harness()
    high = value if value is None or math.isnan(value) else 99.0
    assert calm.value(high, quality) == []
    assert calm.state() is S.NORMAL


def test_bool_tag_compares_as_zero_or_one() -> None:
    h = Harness()
    h.engine.set_definitions(definitions(alarm_def("running", setpoint=1)))
    assert h.value(False) == []
    assert h.value(True) == [("running", S.NORMAL, S.ACTIVE_UNACKED)]


@pytest.mark.parametrize("state_after", ["normal", "acked"])
def test_ack_without_anything_to_acknowledge_is_rejected(state_after: str) -> None:
    h = _harness()
    if state_after == "acked":
        h.value(9.0)
        h.ack()
    with pytest.raises(AlarmNotAckableError) as excinfo:
        h.engine.plan_ack("tank-high", "op")
    assert excinfo.value.state is (S.ACTIVE_ACKED if state_after == "acked" else S.NORMAL)
    with pytest.raises(AlarmNotFoundError):
        h.engine.plan_ack("ghost", "op")


def test_plan_ack_does_not_change_state_until_applied() -> None:
    h = _harness()
    h.value(9.0)
    planned = h.engine.plan_ack("tank-high", "op")
    assert planned.transition.snapshot.state is S.ACTIVE_ACKED
    assert h.state() is S.ACTIVE_UNACKED
    h.clock.advance(1.0)
    applied = h.engine.apply_ack(planned)
    assert applied is not None
    assert (applied.time, h.state()) == (planned.transition.time, S.ACTIVE_ACKED)


def test_apply_ack_follows_clear_while_recording_at_a_later_time() -> None:
    """ACK를 기록하는 사이 해제됐으면 지금 상태 기준(CLEARED_UNACKED → NORMAL)으로 반영하고,
    그 전이 시각은 해제 시각보다 앞서지 않는다(리뷰 M-1: 재기동 시 CLEARED_UNACKED 복원 방지)."""
    h = _harness()
    h.value(9.0)
    planned = h.engine.plan_ack("tank-high", "op")
    h.clock.advance(0.05)
    h.value(7.0)
    cleared_at = h.clock.now
    h.clock.advance(0.05)
    applied = h.engine.apply_ack(planned)
    assert applied is not None
    assert (applied.from_state, applied.to_state, applied.username) == (
        S.CLEARED_UNACKED,
        S.NORMAL,
        "op",
    )
    assert planned.transition.time < cleared_at < applied.time == h.clock.now
    h.engine.set_definitions(definitions())
    assert h.engine.apply_ack(planned) is None  # 그 사이 알람이 빠졌다


def test_apply_ack_skips_new_occurrence_raised_while_recording() -> None:
    """기록하는 사이 해제 → 재발생했으면 ACK를 반영하지 않는다 — 운전원이 보지 못한 발생이다
    (리뷰 L-A1). 상태는 ACTIVE_UNACKED로 남아 다시 ACK해야 한다."""
    h = _harness()
    h.value(9.0)
    planned = h.engine.plan_ack("tank-high", "op")
    h.value(7.0)
    h.value(9.5)
    assert h.engine.apply_ack(planned) is None
    assert h.state() is S.ACTIVE_UNACKED
    assert h.ack() is not None  # 새 계획으로는 ACK된다
    assert h.state() is S.ACTIVE_ACKED


def test_plan_ack_all_covers_only_ackable_alarms() -> None:
    h = Harness()
    h.engine.set_definitions(
        definitions(
            alarm_def("a", setpoint=1), alarm_def("b", setpoint=5), alarm_def("c", setpoint=50)
        )
    )
    h.value(9.0)  # a·b 활성
    h.ack("a")
    planned = h.engine.plan_ack_all("op")
    assert [(p.transition.alarm_id, p.transition.to_state) for p in planned] == [
        ("b", S.ACTIVE_ACKED)
    ]


def test_new_definition_evaluates_current_value_immediately() -> None:
    """값이 그대로인 태그는 tag.changed가 오지 않는다 — 정의를 받는 순간 지금 값으로 평가한다."""
    h = Harness()
    set_value(h.tags, 9.0)
    [t] = h.engine.set_definitions(definitions(alarm_def()))
    assert (t.from_state, t.to_state, t.value) == (S.NORMAL, S.ACTIVE_UNACKED, 9.0)
    # setpoint를 올리면 다시 평가해 해제된다(같은 알람, 상태 유지 후 전이)
    [cleared] = h.engine.set_definitions(definitions(alarm_def(setpoint=20.0)))
    assert (cleared.from_state, cleared.to_state) == (S.ACTIVE_UNACKED, S.CLEARED_UNACKED)
    assert h.engine.set_definitions(definitions(alarm_def(setpoint=20.0), rev=2)) == []


def test_removed_definition_retires_with_one_transition_and_cancels_timer() -> None:
    h = Harness()
    h.engine.set_definitions(definitions(alarm_def(), alarm_def("slow", setpoint=5, on_delay_s=3)))
    h.value(9.0)
    [retired_a, retired_b] = h.engine.set_definitions(definitions())
    assert {(t.alarm_id, t.from_state, t.to_state) for t in (retired_a, retired_b)} == {
        ("tank-high", S.ACTIVE_UNACKED, S.RETIRED),
        ("slow", S.NORMAL, S.RETIRED),
    }
    assert h.scheduler.pending == []
    assert (h.engine.get("tank-high"), h.engine.raised()) == (None, ())


def test_invalid_definition_is_skipped() -> None:
    h = Harness()
    bad = alarm_def("bad")
    broken = AlarmDef(bad.alarm_id, bad.tag_path, {**bad.definition, "setpoint": "high"})
    assert h.engine.set_definitions(definitions(broken, alarm_def())) == []
    assert (h.engine.get("bad"), h.engine.get("tank-high") is not None) == (None, True)


def test_restore_starts_alarms_in_last_journal_state() -> None:
    h = Harness()
    set_value(h.tags, 9.0)
    h.engine.restore(
        {
            "tank-high": journal_record("tank-high", "ACTIVE_ACKED", username="kim"),
            "gone": journal_record("gone", "ACTIVE_UNACKED"),
            "gone-quiet": journal_record("gone-quiet", "NORMAL"),
        }
    )
    transitions = h.engine.set_definitions(definitions(alarm_def()))
    # 복원한 ACK 알람은 조건이 여전하니 그대로, 정의에서 빠진 미해결 알람만 RETIRED
    assert [(t.alarm_id, t.from_state, t.to_state) for t in transitions] == [
        ("gone", S.ACTIVE_UNACKED, S.RETIRED)
    ]
    snapshot = h.engine.get("tank-high")
    assert snapshot is not None
    assert (snapshot.state, snapshot.changed_by, snapshot.value) == (S.ACTIVE_ACKED, "kim", 9.0)


def test_restored_unacked_alarm_does_not_reraise_and_clears_normally() -> None:
    h = Harness()
    h.engine.restore({"tank-high": journal_record("tank-high", "ACTIVE_UNACKED")})
    assert h.engine.set_definitions(definitions(alarm_def())) == []  # 태그 아직 BAD → 보류
    assert h.value(9.0) == []  # 이미 ACTIVE_UNACKED — 새 알람으로 다시 생기지 않는다
    assert h.value(1.0) == [("tank-high", S.ACTIVE_UNACKED, S.CLEARED_UNACKED)]


def test_retired_last_row_restores_as_normal() -> None:
    h = Harness()
    h.engine.restore({"tank-high": journal_record("tank-high", "RETIRED")})
    h.engine.set_definitions(definitions(alarm_def()))
    assert h.state() is S.NORMAL


def test_raised_orders_by_priority_then_recent_and_counts() -> None:
    h = Harness()
    h.engine.set_definitions(
        definitions(
            alarm_def("low-prio", setpoint=1, priority="low"),
            alarm_def("crit", setpoint=5, priority="critical"),
            alarm_def("mid", setpoint=50),
        )
    )
    h.value(9.0)
    h.clock.advance(1)
    h.ack("low-prio")
    assert [a.alarm_id for a in h.engine.raised()] == ["crit", "low-prio"]
    assert h.engine.counts() == (2, 1)
    h.value(0.0)
    assert h.engine.counts() == (0, 1)  # crit: CLEARED_UNACKED, low-prio: NORMAL
