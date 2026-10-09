"""recorder: deadband·max_interval, raw 기록, NaN/Inf, plan_rev·미지 tag_id 버림(D2·D3·D6·D7)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import pytest

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.historian.recorder import Recorder, policies
from pureiron_core.historian.service import Historian
from pureiron_core.historian.writer import HistoryWriter, Row
from pureiron_core.project.ports import Definitions, TagDef

S = 1_000_000_000  # ns
T0 = 1_700_000_000 * S
NOW = T0 + 10_000 * S  # 테스트 샘플 시각(T0 + 수십 초)보다 뒤 — 평상시 벽시계
GOOD, BAD, UNCERTAIN = pb.GOOD, pb.BAD, pb.UNCERTAIN


class ListSink:
    def __init__(self) -> None:
        self.rows: list[Row] = []

    def append(self, rows: Sequence[Row]) -> None:
        self.rows.extend(rows)


def _tag(tag_id: int, record: Mapping[str, object] | None = None) -> TagDef:
    definition: dict[str, object] = {"path": f"t{tag_id}", "device": "plc-1"}
    if record is not None:
        definition["record"] = dict(record)
    return TagDef(tag_id, f"t{tag_id}", "plc-1", definition)


def _defs(
    *tags: TagDef, rev: int = 3, historian: Mapping[str, object] | None = None
) -> Definitions:
    return Definitions(rev=rev, tags=tags, historian=dict(historian or {}))


def num(tag_id: int, ts_s: float, value: float, quality: pb.Quality.ValueType = GOOD) -> pb.Sample:
    return pb.Sample(tag_id=tag_id, ts_unix_ns=T0 + int(ts_s * S), num=value, quality=quality)


def flag(tag_id: int, ts_s: float, value: bool, quality: pb.Quality.ValueType = GOOD) -> pb.Sample:
    return pb.Sample(tag_id=tag_id, ts_unix_ns=T0 + int(ts_s * S), flag=value, quality=quality)


def _recorder(
    *tags: TagDef, rev: int = 3, historian: Mapping[str, object] | None = None
) -> tuple[Recorder, ListSink]:
    sink = ListSink()
    recorder = Recorder(sink)
    recorder.set_definitions(_defs(*tags, rev=rev, historian=historian))
    return recorder, sink


def _live(recorder: Recorder, *samples: pb.Sample, plan_rev: int = 3, now_ns: int = NOW) -> None:
    for sample in samples:
        recorder.offer_live([sample], plan_rev=plan_rev, now_ns=now_ns)


def _times(sink: ListSink) -> list[float]:
    return [(r.ts_ns - T0) / S for r in sink.rows]


@pytest.mark.parametrize(
    ("value", "recorded"),
    [(10.0, False), (10.3, False), (10.5, False), (10.51, True), (9.49, True), (9.5, False)],
    ids=["same", "below", "exact-up", "above-up", "above-down", "exact-down"],
)
def test_deadband_boundaries(value: float, recorded: bool) -> None:
    recorder, sink = _recorder(_tag(1, {"deadband": 0.5}))
    _live(recorder, num(1, 0, 10.0), num(1, 1, value))
    assert len(sink.rows) == (2 if recorded else 1)


def test_deadband_is_measured_from_last_recorded_value() -> None:
    """작은 변화가 쌓여 deadband를 넘으면 기록한다(직전 샘플이 아니라 마지막 기록 기준)."""
    recorder, sink = _recorder(_tag(1, {"deadband": 0.5}))
    _live(recorder, num(1, 0, 10.0), num(1, 1, 10.3), num(1, 2, 10.6), num(1, 3, 10.9))
    assert [r.value_num for r in sink.rows] == [10.0, 10.6]


def test_zero_deadband_skips_repeats_records_changes() -> None:
    recorder, sink = _recorder(_tag(1))
    _live(recorder, num(1, 0, 5.0), num(1, 1, 5.0), num(1, 2, 5.0), num(1, 3, 5.000001))
    assert [r.value_num for r in sink.rows] == [5.0, 5.000001]
    assert (recorder.stats.live_recorded, recorder.stats.live_skipped) == (2, 2)


def test_bool_change_is_recorded() -> None:
    recorder, sink = _recorder(_tag(2, {"deadband": 100}))
    _live(recorder, flag(2, 0, False), flag(2, 1, False), flag(2, 2, True), flag(2, 3, True))
    assert [r.value_bool for r in sink.rows] == [False, True]
    assert all(r.value_num is None for r in sink.rows)


def test_bool_and_number_are_different_values() -> None:
    """True ↔ 1.0은 같은 값이 아니다(종류 전환은 기록)."""
    recorder, sink = _recorder(_tag(1, {"deadband": 5}))
    _live(recorder, num(1, 0, 1.0), flag(1, 1, True), num(1, 2, 1.0))
    assert len(sink.rows) == 3


def test_quality_change_is_recorded_regardless_of_deadband() -> None:
    recorder, sink = _recorder(_tag(1, {"deadband": 100}))
    _live(recorder, num(1, 0, 1.0), num(1, 1, 1.0, BAD), num(1, 2, 1.0, BAD), num(1, 3, 1.0))
    assert [r.quality for r in sink.rows] == [GOOD, BAD, GOOD]


def test_value_less_sample_and_back() -> None:
    """값 없는 샘플(BAD 등) ↔ 값 전환은 기록한다(NULL↔값)."""
    recorder, sink = _recorder(_tag(1, {"deadband": 100}))
    empty = pb.Sample(tag_id=1, ts_unix_ns=T0 + S, quality=GOOD)
    _live(recorder, num(1, 0, 1.0), empty, num(1, 2, 1.0))
    assert [(r.value_num, r.value_bool) for r in sink.rows] == [
        (1.0, None),
        (None, None),
        (1.0, None),
    ]


@pytest.mark.parametrize(("gap_s", "recorded"), [(9.999, False), (10.0, True), (10.5, True)])
def test_max_interval_boundary(gap_s: float, recorded: bool) -> None:
    recorder, sink = _recorder(_tag(1, {"deadband": 100, "max_interval_s": 10}))
    _live(recorder, num(1, 0, 1.0), num(1, gap_s, 1.0))
    assert len(sink.rows) == (2 if recorded else 1)


def test_max_interval_counts_from_last_recorded_sample() -> None:
    recorder, sink = _recorder(_tag(1, {"max_interval_s": 10}))
    _live(recorder, *(num(1, t, 1.0) for t in range(26)))
    assert _times(sink) == [0, 10, 20]


def test_policy_tag_record_overrides_project_default() -> None:
    defs = _defs(
        _tag(1, {"deadband": 2.0, "max_interval_s": 5}),
        _tag(2, {"deadband": 0}),
        _tag(3),
        historian={"default_deadband": 0.5, "default_max_interval_s": 30},
    )
    result = policies(defs)
    assert (result[1].deadband, result[1].max_interval_ns) == (2.0, 5 * S)
    # 태그가 명시한 0은 프로젝트 기본값보다 우선한다(정규형은 태그 record에 기본값이 없음).
    assert (result[2].deadband, result[2].max_interval_ns) == (0.0, 30 * S)
    assert (result[3].deadband, result[3].max_interval_ns) == (0.5, 30 * S)


def test_policy_schema_defaults_without_project_settings() -> None:
    result = policies(_defs(_tag(1)))
    assert (result[1].deadband, result[1].max_interval_ns) == (0.0, 60 * S)


def test_project_default_deadband_applies() -> None:
    recorder, sink = _recorder(_tag(1), historian={"default_deadband": 1.0})
    _live(recorder, num(1, 0, 1.0), num(1, 1, 1.9), num(1, 2, 2.1))
    assert [r.value_num for r in sink.rows] == [1.0, 2.1]


def test_raw_is_always_recorded_and_leaves_live_state_alone() -> None:
    """replay·stale·future는 deadband를 우회하고 live 상태를 읽지도 바꾸지도 않는다(D2)."""
    recorder, sink = _recorder(_tag(1, {"deadband": 100, "max_interval_s": 60}))
    _live(recorder, num(1, 10, 1.0))
    # 같은 값·같은 시각·과거 시각·미래 시각 모두 그대로 기록
    raw = [num(1, 10, 1.0), num(1, 5, 1.0), num(1, 11, 1.0), num(1, 500, 1.0, BAD)]
    recorder.offer_raw(raw, plan_rev=3)
    assert len(sink.rows) == 5
    assert recorder.stats.raw == 4
    # live 상태는 t=10 기록 그대로: 미래 raw(t=500)가 있어도 t=11 live는 역행이 아니고,
    # BAD raw로 quality 상태가 바뀌지도 않았으므로 같은 값 live는 생략된다.
    _live(recorder, num(1, 11, 1.0))
    assert len(sink.rows) == 5
    _live(recorder, num(1, 12, 1.0, BAD))
    assert len(sink.rows) == 6


def test_raw_records_tag_ids_outside_the_plan() -> None:
    """replay는 옛 rev의 tag_id(retired 포함)를 그대로 기록한다(D7은 live만)."""
    recorder, sink = _recorder(_tag(1), rev=5)
    recorder.offer_raw([num(99, 0, 1.0)], plan_rev=4)
    assert [r.tag_id for r in sink.rows] == [99]
    assert recorder.stats.rejected == 0


@pytest.mark.parametrize(
    ("value", "stored"), [(math.nan, None), (math.inf, None), (-math.inf, None), (1.5, 1.5)]
)
def test_non_finite_is_stored_as_null_with_quality(value: float, stored: float | None) -> None:
    recorder, sink = _recorder(_tag(1))
    _live(recorder, num(1, 0, value, UNCERTAIN))
    recorder.offer_raw([num(1, 1, value)], plan_rev=3)
    assert [(r.value_num, r.value_bool, r.quality) for r in sink.rows] == [
        (stored, None, UNCERTAIN),
        (stored, None, GOOD),
    ]


def test_non_finite_values_compare_equal_and_transitions_are_recorded() -> None:
    recorder, sink = _recorder(_tag(1, {"deadband": 100}))
    _live(
        recorder,
        num(1, 0, 1.0),
        num(1, 1, math.nan),  # 유한 → 비유한: 기록
        num(1, 2, math.inf),  # 비유한끼리 같은 값: 생략
        num(1, 3, math.nan),  # 생략
        num(1, 4, 1.0),  # 비유한 → 유한: 기록
    )
    assert _times(sink) == [0, 1, 4]
    assert sink.rows[1].value_num is None


def test_time_regression_in_live_is_skipped() -> None:
    recorder, sink = _recorder(_tag(1))
    _live(recorder, num(1, 10, 1.0), num(1, 10, 2.0), num(1, 9, 3.0), num(1, 11, 4.0))
    assert [r.value_num for r in sink.rows] == [1.0, 4.0]


def test_time_regression_uses_last_seen_not_last_recorded() -> None:
    """생략된 샘플(t=12)보다 이른 늦은 샘플(t=11)도 역행으로 본다."""
    recorder, sink = _recorder(_tag(1, {"deadband": 10}))
    _live(recorder, num(1, 10, 1.0), num(1, 12, 1.0), num(1, 11, 50.0))
    assert _times(sink) == [10]


def test_unknown_live_tag_is_rejected_and_counted() -> None:
    recorder, sink = _recorder(_tag(1))
    recorder.offer_live([num(1, 0, 1.0), num(7, 0, 1.0), num(8, 0, 1.0)], plan_rev=3, now_ns=NOW)
    assert [r.tag_id for r in sink.rows] == [1]
    assert (recorder.stats.rejected_unknown, recorder.stats.rejected) == (2, 2)


def test_unknown_quality_is_stored_as_bad() -> None:
    recorder, sink = _recorder(_tag(1))
    recorder.offer_raw([num(1, 0, 1.0, pb.QUALITY_UNSPECIFIED)], plan_rev=3)
    assert sink.rows[0].quality == BAD


@pytest.mark.parametrize("raw", [True, False], ids=["raw", "live"])
def test_batch_from_newer_plan_rev_is_rejected(raw: bool) -> None:
    """plan_rev가 현재보다 크면 DB 재생성 신호 — 이력에서 버리고 센다(D6)."""
    recorder, sink = _recorder(_tag(1), rev=3)
    samples = [num(1, 0, 1.0), num(1, 1, 2.0)]
    if raw:
        recorder.offer_raw(samples, plan_rev=4)
    else:
        recorder.offer_live(samples, plan_rev=4, now_ns=NOW)
    assert sink.rows == []
    assert (recorder.stats.rejected_rev, recorder.stats.rejected) == (2, 2)
    # 같은 rev·옛 rev는 받는다
    recorder.offer_raw([num(1, 2, 1.0)], plan_rev=2)
    recorder.offer_live([num(1, 3, 1.0)], plan_rev=3, now_ns=NOW)
    assert len(sink.rows) == 2


def test_samples_before_any_plan_are_kept_raw() -> None:
    """plan을 아직 모르면(DB 미준비) rev·정책을 판단할 수 없어 live까지 raw로 받아 둔다(D6).

    collector는 Ack된 batch를 다시 보내지 않으므로 버리면 DB 복구 전 이력이 사라진다.
    """
    sink = ListSink()
    recorder = Recorder(sink)
    recorder.offer_raw([num(1, 0, 1.0)], plan_rev=9)
    # live도 deadband 없이 모두
    recorder.offer_live([num(1, 1, 1.0), num(1, 2, 1.0)], plan_rev=9, now_ns=NOW)
    assert _times(sink) == [0, 1, 2]
    assert (recorder.stats.raw, recorder.stats.rejected) == (3, 0)
    # plan을 받은 뒤부터는 live 규칙(같은 값 반복 생략, 첫 샘플은 기록)
    recorder.set_definitions(_defs(_tag(1), rev=9))
    _live(recorder, num(1, 3, 1.0), num(1, 4, 1.0), plan_rev=9)
    assert _times(sink) == [0, 1, 2, 3]


@pytest.mark.parametrize("raw", [True, False], ids=["raw", "live"])
def test_tag_id_outside_int4_is_rejected(raw: bool) -> None:
    """int4 범위 밖 tag_id 한 행이 INSERT 전체를 매번 실패시키지 않게 미리 버린다."""
    recorder, sink = _recorder(_tag(1))
    samples = [num(1, 0, 1.0), num(2**31, 0, 1.0), num(2**32 - 1, 0, 1.0)]
    if raw:
        recorder.offer_raw(samples, plan_rev=3)
    else:
        recorder.offer_live(samples, plan_rev=3, now_ns=NOW)
    assert [r.tag_id for r in sink.rows] == [1]
    assert recorder.stats.rejected == 2


def test_huge_max_interval_is_clamped() -> None:
    """스키마에 상한이 없는 max_interval_s가 커도 정책 계산이 실패하지 않는다(plan 적용 보호)."""
    result = policies(
        _defs(_tag(1, {"max_interval_s": 10**400}), _tag(2, {"max_interval_s": 10**12}))
    )
    assert result[1].max_interval_ns == result[2].max_interval_ns == 365 * 86_400 * S


def test_plan_update_drops_state_of_removed_tags_keeps_others() -> None:
    recorder, sink = _recorder(_tag(1, {"deadband": 10}), _tag(2, {"deadband": 10}), rev=3)
    _live(recorder, num(1, 0, 1.0), num(2, 0, 1.0))
    # rev 4: 태그 2 제거, 태그 1은 정책만 바뀜(상태 유지 → 다음 샘플부터 새 정책)
    recorder.set_definitions(_defs(_tag(1, {"deadband": 0.1}), rev=4))
    assert recorder.plan_rev == 4
    _live(recorder, num(1, 1, 1.05), num(1, 2, 1.2), plan_rev=4)
    assert [(r.tag_id, r.value_num) for r in sink.rows][2:] == [(1, 1.2)]
    # 태그 2가 다시 plan에 들어오면 첫 샘플로 본다(이전 상태가 남아 있지 않음).
    recorder.set_definitions(_defs(_tag(1), _tag(2, {"deadband": 10}), rev=5))
    _live(recorder, num(2, 3, 1.0), plan_rev=5)
    assert sink.rows[-1].tag_id == 2


def test_rev_going_back_resets_live_state(caplog: pytest.LogCaptureFixture) -> None:
    """R1 L6: rev 감소(site-db 재생성)면 같은 tag_id라도 다른 태그 — 옛 deadband 상태를 지운다."""
    recorder, sink = _recorder(_tag(1, {"deadband": 0.5}), rev=5)  # 옛 DB의 tag 1 = 온도
    _live(recorder, num(1, 0, 50.0), plan_rev=5)
    recorder.set_definitions(_defs(_tag(1, {"deadband": 0.5}), rev=2))  # 새 DB의 tag 1 = 압력
    assert "definitions rev went back 5 -> 2" in caplog.text
    _live(recorder, num(1, 1, 50.0), plan_rev=2)  # 새 태그의 첫 샘플 — 값이 같아도 기록
    assert [(t, r.value_num) for t, r in zip(_times(sink), sink.rows, strict=True)] == [
        (0, 50.0),
        (1, 50.0),
    ]


def test_same_rev_definitions_keep_live_state(caplog: pytest.LogCaptureFixture) -> None:
    """V1 N6: 같은 rev로 정의를 다시 적용하면 live 상태를 유지한다(감소만 초기화)."""
    recorder, sink = _recorder(_tag(1, {"deadband": 0.5}), rev=5)
    _live(recorder, num(1, 0, 50.0), plan_rev=5)
    recorder.set_definitions(_defs(_tag(1, {"deadband": 0.5}), rev=5))
    assert "went back" not in caplog.text
    _live(recorder, num(1, 1, 50.0), plan_rev=5)  # deadband 안 — 상태가 남아 있으면 건너뛴다
    assert [(t, r.value_num) for t, r in zip(_times(sink), sink.rows, strict=True)] == [(0, 50.0)]


def test_historian_status_rejected_total_sums_both_paths() -> None:
    """`/readyz.historian.rejected_total` = D6(rev 앞섬) + D7(live 미지 tag_id)."""

    class NullStore:
        async def insert(self, rows: Sequence[Row]) -> None:
            pass

    writer = HistoryWriter(NullStore())
    recorder = Recorder(writer)
    recorder.set_definitions(_defs(_tag(1), rev=3))
    historian = Historian(recorder, writer)
    recorder.offer_raw([num(1, 0, 1.0)], plan_rev=4)
    assert historian.status().rejected_total == 1
    recorder.offer_live([num(5, 0, 1.0), num(6, 0, 1.0)], plan_rev=3, now_ns=NOW)
    status = historian.status()
    assert status.rejected_total == 3
    assert (recorder.stats.rejected_rev, recorder.stats.rejected_unknown) == (1, 2)
    assert status.buffer_rows == 0


def test_reject_warnings_are_rate_limited_per_reason(caplog: pytest.LogCaptureFixture) -> None:
    """R1 L8: 사유마다 60s에 한 번 — 다른 사유의 경고는 서로 가리지 않는다."""
    now = [0.0]
    recorder = Recorder(ListSink(), clock=lambda: now[0])
    recorder.set_definitions(_defs(_tag(1), rev=3))
    recorder.offer_raw([num(2**31, 0, 1.0)], plan_rev=3)  # int4 밖 tag_id
    now[0] = 10.0
    recorder.offer_raw([num(1, 0, 1.0)], plan_rev=4)  # rev 앞섬(D6)
    recorder.offer_live([num(9, 0, 1.0)], plan_rev=3, now_ns=NOW)  # plan에 없는 tag_id(D7)
    now[0] = 20.0
    recorder.offer_raw([num(1, 1, 1.0)], plan_rev=4)  # 같은 사유 60s 안 → 경고 없음
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 3
    assert "tag_id above" in messages[0]
    assert "plan_rev 4 is ahead" in messages[1]
    assert "not in the current definitions" in messages[2]
    now[0] = 70.0
    recorder.offer_raw([num(1, 2, 1.0)], plan_rev=4)  # 같은 사유도 60s 뒤엔 다시 경고
    assert len(caplog.records) == 4


def _at(ts_s: float) -> int:
    return T0 + int(ts_s * S)


def test_clock_stepped_back_resets_state_and_records() -> None:
    """마지막으로 본 시각이 지금보다 2s 넘게 미래(벽시계 역보정)면 과거 ts live도 기록한다.

    TagStore와 같은 기준 — 현재값만 갱신되고 이력이 비는 불일치를 막는다(W1 진단 §5).
    """
    recorder, sink = _recorder(_tag(1, {"deadband": 100, "max_interval_s": 10}))
    _live(recorder, num(1, 100, 1.0), now_ns=_at(100))
    # 벽시계가 100s → 50s로 뒤로 보정: 마지막 시각(100)이 now(50)+2s보다 미래
    _live(recorder, num(1, 50, 1.0), now_ns=_at(50))
    assert _times(sink) == [100, 50]
    # 상태가 새 시각으로 리셋됐다: 같은 값은 생략, max_interval은 50s 기준으로 다시 센다
    _live(recorder, num(1, 51, 1.0), now_ns=_at(51))
    _live(recorder, num(1, 60, 1.0), now_ns=_at(60))
    assert _times(sink) == [100, 50, 60]


@pytest.mark.parametrize(
    ("now_s", "recorded"),
    [(99.0, False), (98.0, False), (97.999, True)],
    ids=["within-slack", "exact-slack", "beyond-slack"],
)
def test_clock_slack_boundary(now_s: float, recorded: bool) -> None:
    """last_seen ≤ now + 2s면 역행으로 버리고, 넘으면 역보정으로 보고 기록한다(TagStore와 같음)."""
    recorder, sink = _recorder(_tag(1))
    _live(recorder, num(1, 100, 1.0), now_ns=_at(100))
    _live(recorder, num(1, 90, 2.0), now_ns=_at(now_s))
    assert len(sink.rows) == (2 if recorded else 1)


def test_same_timestamp_live_is_not_recorded_twice() -> None:
    """같은 시각 두 번째 샘플은 버린다(UNIQUE(tag_id,time)로 어차피 저장되지 않음)."""
    recorder, sink = _recorder(_tag(1))
    _live(recorder, num(1, 10, 1.0), num(1, 10, 5.0), num(1, 11, 5.0))
    assert [r.value_num for r in sink.rows] == [1.0, 5.0]
    assert _times(sink) == [10, 11]
