"""ingest sink: 샘플 → 이력 행(plan B9, M3a-3 D2·D3·D6·D7).

- fresh live 샘플(`offer_live`)은 태그별 deadband·max_interval을 거친다. 정책은 태그 `record` →
  프로젝트 `historian.default_*` → 스키마 기본값(0, 60s) 순으로 정한다.
- raw(`offer_raw`: replay·오래된 live·미래 시각 live)는 걸러내지 않고 모두 기록하며 live 상태를
  읽지도 바꾸지도 않는다(D2 — 이력은 받은 것을 다 남기고, 걸러내기는 현재값 경로만).
- NaN/Inf는 `value_num = NULL`(quality는 받은 값). CAGG avg/min/max가 NaN으로 오염되지 않게(D3).
  deadband 비교에서 비유한값끼리는 같은 값이고, 유한↔비유한 전환은 기록한다.
- batch `plan_rev`가 현재 rev보다 크면 버린다(D6): rev는 리셋 없이 단조 증가하므로 DB가 새로
  만들어져 tag_id가 1부터 다시 매겨졌다는 신호다. plan을 아직 모르면(기동 직후 DB 미준비) 판단할
  수 없으므로 live까지 raw로 받아 둔다(정책도 아직 없음) — collector는 Ack된 batch를 다시 보내지
  않으므로 버리면 DB가 돌아올 때까지의 이력이 사라진다. 그 사이 DB 재생성이 겹치면 잘못된 tag_id로
  기록될 수 있다(한계).
- `tag_history.tag_id`(int4) 범위 밖 tag_id는 어느 경로든 버린다 — 존재할 수 없는 id이고, 한 행이
  INSERT 전체를 매번 실패시켜 이력을 막지 않게 한다.
- live 시간 역행(마지막으로 본 시각 이하)은 기록하지 않는다. 단 마지막으로 본 시각이 지금보다
  `CLOCK_SLACK_NS`(2s) 넘게 미래면(벽시계가 뒤로 보정됨) 그 상태를 믿지 않고 리셋해 첫 샘플처럼
  기록한다 — TagStore와 같은 기준(현재값만 갱신되고 이력이 비는 불일치 방지, M3a-3 W1 진단).
- live 샘플의 tag_id가 현재 plan에 없으면 버린다(D7, TagStore와 같은 기준). raw는 옛 rev의
  tag_id(retired 포함)를 그대로 기록한다 — id는 재사용되지 않는다.

모든 메서드는 동기(await 없음)다 — ingest 직후 Ack가 나가므로 DB I/O를 하지 않는다.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, cast

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.historian.writer import Row
from pureiron_core.project.ports import Definitions
from pureiron_core.tags.ports import CLOCK_SLACK_NS, Quality

log = logging.getLogger(__name__)

# packages/contracts/project.schema.json `historian.default_*`의 default와 같다.
SCHEMA_DEFAULT_DEADBAND = 0.0
SCHEMA_DEFAULT_MAX_INTERVAL_S = 60.0
REJECT_WARN_INTERVAL_S = 60.0
# 스키마는 max_interval_s 상한이 없다 — ns 정수 변환이 넘치지 않게 자른다(1년).
MAX_INTERVAL_S = 365 * 86_400.0
MAX_TAG_ID = 2**31 - 1  # tag_history.tag_id int4


class RowSink(Protocol):
    def append(self, rows: Sequence[Row]) -> None: ...


@dataclass(frozen=True, slots=True)
class RecordPolicy:
    deadband: float
    max_interval_ns: int


class _NonFinite:
    """NaN/Inf 비교값(서로 같은 값으로 본다)."""


NON_FINITE: Final = _NonFinite()

type CompareValue = float | bool | _NonFinite | None


@dataclass(slots=True)
class _Last:
    """태그별 live 상태: 마지막으로 **기록된** 값·quality·시각과 마지막으로 본 시각."""

    value: CompareValue
    quality: int
    recorded_ns: int
    seen_ns: int


@dataclass(slots=True)
class RecorderStats:
    raw: int = 0
    live_recorded: int = 0
    live_skipped: int = 0
    rejected_rev: int = 0  # D6: batch plan_rev가 현재 rev보다 앞섬
    rejected_unknown: int = 0  # D7: 현재 plan에 없는 tag_id의 live 샘플 + int4 범위 밖 tag_id

    @property
    def rejected(self) -> int:
        return self.rejected_rev + self.rejected_unknown


def _number(data: Mapping[str, object], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return float(value)
    except OverflowError:  # float로 표현할 수 없는 큰 정수
        return math.inf


def _mapping(value: object) -> Mapping[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def policies(definitions: Definitions) -> dict[int, RecordPolicy]:
    """태그별 기록 정책(태그 `record` → 프로젝트 기본값 → 스키마 기본값)."""
    project = definitions.historian
    default_deadband = _number(project, "default_deadband")
    default_interval = _number(project, "default_max_interval_s")
    if default_deadband is None:
        default_deadband = SCHEMA_DEFAULT_DEADBAND
    if default_interval is None:
        default_interval = SCHEMA_DEFAULT_MAX_INTERVAL_S
    result: dict[int, RecordPolicy] = {}
    for tag in definitions.tags:
        record = _mapping(tag.definition.get("record"))
        deadband = _number(record, "deadband")
        interval = _number(record, "max_interval_s")
        interval_s = default_interval if interval is None else interval
        result[tag.tag_id] = RecordPolicy(
            deadband=max(default_deadband if deadband is None else deadband, 0.0),
            max_interval_ns=int(min(interval_s, MAX_INTERVAL_S) * 1e9),
        )
    return result


def _quality(value: int) -> int:
    try:
        return int(Quality(value))
    except ValueError:
        return int(Quality.BAD)  # QUALITY_UNSPECIFIED 등 모르는 값은 BAD(TagStore와 같은 기준)


def row(sample: pb.Sample) -> Row:
    kind = sample.WhichOneof("v")
    num: float | None = None
    flag: bool | None = None
    if kind == "num":
        num = sample.num if math.isfinite(sample.num) else None
    elif kind == "flag":
        flag = sample.flag
    return Row(sample.ts_unix_ns, sample.tag_id, num, flag, _quality(sample.quality))


def _compare_value(sample: pb.Sample) -> CompareValue:
    kind = sample.WhichOneof("v")
    if kind == "num":
        return sample.num if math.isfinite(sample.num) else NON_FINITE
    if kind == "flag":
        return sample.flag
    return None


def _changed(last: CompareValue, value: CompareValue, deadband: float) -> bool:
    # bool은 int의 하위형이라 수치 비교보다 먼저 본다(True ↔ 1.0을 같은 값으로 보지 않게).
    if isinstance(last, bool) or isinstance(value, bool):
        return type(last) is not type(value) or last != value
    if isinstance(last, float) and isinstance(value, float):
        return abs(value - last) > deadband
    return last is not value  # None·비유한·종류 전환(둘 다 같은 싱글턴이면 같은 값)


class Recorder:
    def __init__(self, sink: RowSink, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._sink = sink
        self._clock = clock
        self._rev: int | None = None
        self._policies: dict[int, RecordPolicy] = {}
        self._last: dict[int, _Last] = {}
        # 버림 경고 rate-limit은 사유별로 — 한 사유의 경고가 다른 사유를 가리지 않게.
        self._warned_at: dict[str, float] = {}
        self.stats = RecorderStats()

    @property
    def plan_rev(self) -> int | None:
        return self._rev

    def set_definitions(self, definitions: Definitions) -> None:
        """새 정의의 정책을 적용한다. plan에서 빠진 태그의 live 상태는 지운다.

        정책만 바뀐 태그는 상태를 유지한다(다음 샘플부터 새 정책). rev가 줄면(site-db 재생성 —
        같은 tag_id가 이제 다른 태그일 수 있다) live 상태를 모두 지운다.
        """
        if self._rev is not None and definitions.rev < self._rev:
            log.warning(
                "definitions rev went back %d -> %d (site-db recreated?): "
                "resetting historian live state of %d tag(s)",
                self._rev,
                definitions.rev,
                len(self._last),
            )
            self._last.clear()
        self._policies = policies(definitions)
        self._rev = definitions.rev
        for tag_id in [t for t in self._last if t not in self._policies]:
            del self._last[tag_id]

    def _rev_ahead(self, plan_rev: int, count: int) -> bool:
        if self._rev is None or plan_rev <= self._rev:
            return False
        self.stats.rejected_rev += count
        self._warn(
            "rev_ahead",
            count,
            f"batch plan_rev {plan_rev} is ahead of current rev {self._rev} "
            "(site-db recreated? tag ids may not match)",
        )
        return True

    def _warn(self, kind: str, count: int, reason: str) -> None:
        now = self._clock()
        last = self._warned_at.get(kind)
        if last is not None and now - last < REJECT_WARN_INTERVAL_S:
            return
        self._warned_at[kind] = now
        log.warning(
            "historian dropped %d sample(s): %s (total rejected=%d)",
            count,
            reason,
            self.stats.rejected,
        )

    def _invalid_ids(self, samples: Sequence[pb.Sample]) -> Sequence[pb.Sample]:
        valid = [s for s in samples if s.tag_id <= MAX_TAG_ID]
        if len(valid) == len(samples):
            return samples
        dropped = len(samples) - len(valid)
        self.stats.rejected_unknown += dropped
        self._warn(
            "tag_id_range", dropped, f"tag_id above {MAX_TAG_ID} (cannot exist in tag_history)"
        )
        return valid

    def offer_raw(self, samples: Sequence[pb.Sample], *, plan_rev: int) -> None:
        """replay·오래된 live·미래 시각 live: 걸러내지 않고 모두 기록한다(live 상태 불변)."""
        if not samples or self._rev_ahead(plan_rev, len(samples)):
            return
        samples = self._invalid_ids(samples)
        self.stats.raw += len(samples)
        if samples:
            self._sink.append([row(s) for s in samples])

    def offer_live(self, samples: Sequence[pb.Sample], *, plan_rev: int, now_ns: int) -> None:
        """fresh live: deadband·max_interval을 통과한 샘플만 기록한다.

        `now_ns`는 ingest가 fresh 판정에 쓴 벽시계(시계 역보정 판단용). plan을 아직 모르면(정책
        없음) raw로 받아 둔다(D6).
        """
        if self._rev is None:
            self.offer_raw(samples, plan_rev=plan_rev)
            return
        if not samples or self._rev_ahead(plan_rev, len(samples)):
            return
        rows: list[Row] = []
        unknown = 0
        for sample in samples:
            policy = self._policies.get(sample.tag_id)
            if policy is None:
                unknown += 1
            elif self._keep(sample, policy, now_ns):
                rows.append(row(sample))
        if unknown:
            self.stats.rejected_unknown += unknown
            self._warn("unknown_tag", unknown, "live tag_id not in the current plan")
        self.stats.live_recorded += len(rows)
        self.stats.live_skipped += len(samples) - unknown - len(rows)
        if rows:
            self._sink.append(rows)

    def _keep(self, sample: pb.Sample, policy: RecordPolicy, now_ns: int) -> bool:
        value = _compare_value(sample)
        quality = _quality(sample.quality)
        ts = sample.ts_unix_ns
        last = self._last.get(sample.tag_id)
        if last is None or last.seen_ns > now_ns + CLOCK_SLACK_NS:
            # 첫 샘플, 또는 벽시계가 뒤로 보정돼 마지막 시각을 믿을 수 없음 → 상태 리셋 후 기록.
            # (계속 비교하면 max_interval 강제 기록이 시계가 따라잡을 때까지 멈춘다.)
            self._last[sample.tag_id] = _Last(value, quality, ts, ts)
            return True
        if ts <= last.seen_ns:
            # 시간 역행은 TagStore처럼 버린다. 같은 시각(==)은 TagStore와 달리 버린다: 같은
            # (tag_id, time) 두 번째 행은 UNIQUE + ON CONFLICT DO NOTHING으로 어차피 저장되지 않으니
            # 받으면 deadband 상태만 DB와 어긋난다(현재값은 마지막 값이 이기는 덮어쓰기라 무해).
            return False
        last.seen_ns = ts
        if (
            last.quality == quality
            and ts - last.recorded_ns < policy.max_interval_ns
            and not _changed(last.value, value, policy.deadband)
        ):
            return False
        last.value, last.quality, last.recorded_ns = value, quality, ts
        return True
