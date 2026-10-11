"""`ValueBatch` → 현재값(TagStore)·이력 sink (plan B9 ingest 의미론).

- `replay=true`(재연결 후 재전송분)는 이력에만 넘긴다 — 현재값·이벤트(알람·WS)는 건드리지
  않는다.
- live라도 샘플 시각이 `live_max_age_s`보다 오래됐으면 이력에만 넘긴다. collector watchdog이
  끊긴 세션을 알아채기 전(≤ 5s) 커널 버퍼에 남은 batch가 네트워크 복구 뒤 늦게 도착하는 경우의
  방어다. 시각이 `FUTURE_SLACK_S`보다 미래인 샘플(시계 오류)도 이력에만 넘긴다 — 현재값에 미래
  시각이 박히면 이후 정상 샘플이 모두 "역행"으로 버려진다.
- 이력 sink에는 replay·오래된 live·미래 live를 `offer_raw`(걸러내지 않는 원시 기록)로, fresh live만
  `offer_live`(deadband·max_interval 적용)로 넘긴다(M3a-3 D2: 이력은 받은 것을 다 남기고 걸러내기는
  현재값 경로만). 둘 다 batch의 `plan_rev`를 함께 넘긴다(D6: DB 재생성 감지).
- 중복 제거는 하지 않는다. seq는 collector 프로세스마다 1부터 다시 시작하고 링버퍼 drop으로 빈
  구간도 있다 — 중복은 historian의 `UNIQUE(tag_id, time)`가 흡수한다.
- replay batch는 원래 plan_rev를 유지한다(지금 rev와 달라도 tag_id로 기록 — id는 재사용되지
  않음).

상태 반영은 동기(await 없음)이고 발행할 이벤트를 돌려준다. 호출자는 Ack를 먼저 보내고
발행한다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.acquisition.devices import DeviceMonitor
from pureiron_core.platform.bus import Event
from pureiron_core.tags.ports import Quality, Reading, TagStorePort

log = logging.getLogger(__name__)

FUTURE_SLACK_S = 2.0
# 시계 차이로 버려지는 샘플 경고 간격(원인 단서 — 모든 태그가 BAD인데 이유가 안 보이는 것 방지).
SKEW_WARN_INTERVAL_S = 60.0


class HistorySink(Protocol):
    """메모리 버퍼에 넣기만 한다(블로킹·DB I/O 없음 — Ack가 이 뒤에 나간다)."""

    def offer_raw(self, samples: Sequence[pb.Sample], *, plan_rev: int) -> None:
        """replay·오래된 live·미래 시각 live — 걸러내지 않고 기록."""
        ...

    def offer_live(self, samples: Sequence[pb.Sample], *, plan_rev: int, now_ns: int) -> None:
        """fresh live — deadband·max_interval을 거쳐 기록. `now_ns` = fresh 판정에 쓴 벽시계."""
        ...


@dataclass(slots=True)
class IngestStats:
    batches: int = 0
    heartbeats: int = 0
    live: int = 0
    stale: int = 0
    future: int = 0
    replay: int = 0


def _quality(value: int) -> Quality:
    try:
        return Quality(value)
    except ValueError:
        return Quality.BAD  # QUALITY_UNSPECIFIED 등 모르는 값은 BAD(fail-safe)


def reading(sample: pb.Sample) -> Reading:
    kind = sample.WhichOneof("v")
    value: float | bool | None
    if kind == "num":
        value = sample.num
    elif kind == "flag":
        value = sample.flag
    else:
        value = None
    return Reading(sample.tag_id, value, _quality(sample.quality), sample.ts_unix_ns)


class Ingestor:
    def __init__(
        self,
        tags: TagStorePort,
        devices: DeviceMonitor,
        history: HistorySink,
        *,
        live_max_age_s: float = 5.0,
        wall_ns: Callable[[], int] = time.time_ns,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tags = tags
        self._devices = devices
        self._history = history
        self._live_max_age_ns = int(live_max_age_s * 1e9)
        self._wall_ns = wall_ns
        self._clock = clock
        self._warned_at: float | None = None
        self.stats = IngestStats()

    def _warn_skew(self, stale: int, future: int) -> None:
        now = self._clock()
        if self._warned_at is not None and now - self._warned_at < SKEW_WARN_INTERVAL_S:
            return
        self._warned_at = now
        log.warning(
            "dropping live samples from current values (history only): %d older than %.0fs, "
            "%d more than %.0fs in the future — collector/core clock skew or delayed batch? "
            "(totals stale=%d future=%d)",
            stale,
            self._live_max_age_ns / 1e9,
            future,
            FUTURE_SLACK_S,
            self.stats.stale,
            self.stats.future,
        )

    def ingest(self, batch: pb.ValueBatch) -> list[Event]:
        self.stats.batches += 1
        samples = list(batch.samples)
        if not samples:
            self.stats.heartbeats += 1
            return []
        if batch.replay:
            self._history.offer_raw(samples, plan_rev=batch.plan_rev)
            self.stats.replay += len(samples)
            return []
        now = self._wall_ns()
        oldest = now - self._live_max_age_ns
        newest = now + int(FUTURE_SLACK_S * 1e9)
        live: list[pb.Sample] = []
        late: list[pb.Sample] = []
        stale = future = 0
        for sample in samples:
            if sample.ts_unix_ns < oldest:
                stale += 1
                late.append(sample)
            elif sample.ts_unix_ns > newest:
                future += 1
                late.append(sample)
            else:
                live.append(sample)
        if late:
            self._history.offer_raw(late, plan_rev=batch.plan_rev)
        if live:
            self._history.offer_live(live, plan_rev=batch.plan_rev, now_ns=now)
        fresh = [reading(sample) for sample in live]
        self.stats.stale += stale
        self.stats.future += future
        self.stats.live += len(fresh)
        if stale or future:
            self._warn_skew(stale, future)
        commit = self._tags.commit(fresh, now)
        # 무신호 타이머는 실제로 반영된 샘플만 되돌린다(버려진 샘플로 "살아 있음"이 되지 않게).
        return [*commit.events, *self._devices.seen(commit.accepted)]
