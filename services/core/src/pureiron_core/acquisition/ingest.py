"""`ValueBatch` → 현재값(TagStore)·이력 sink (plan B9 ingest 의미론).

- `replay=true`(재연결 후 재전송분)는 이력에만 넘긴다 — 현재값·이벤트(알람·WS)는 건드리지
  않는다.
- live라도 샘플 시각이 `live_max_age_s`보다 오래됐으면 이력에만 넘긴다. collector watchdog이
  끊긴 세션을 알아채기 전(≤ 5s) 커널 버퍼에 남은 batch가 네트워크 복구 뒤 늦게 도착하는 경우의
  방어다.
- 중복 제거는 하지 않는다. seq는 collector 프로세스마다 1부터 다시 시작하고 링버퍼 drop으로 빈
  구간도 있다 — 중복은 historian의 `UNIQUE(tag_id, time)`가 흡수한다.
- replay batch는 원래 plan_rev를 유지한다(지금 rev와 달라도 tag_id로 기록 — id는 재사용되지
  않음).

상태 반영은 동기(await 없음)이고 발행할 이벤트를 돌려준다. 호출자는 Ack를 먼저 보내고
발행한다.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.acquisition.devices import DeviceMonitor
from pureiron_core.platform.bus import Event
from pureiron_core.tags.ports import Quality, Reading, TagStorePort


class HistorySink(Protocol):
    def offer(self, samples: Sequence[pb.Sample], *, replay: bool) -> None:
        """메모리 버퍼에 넣기만 한다(블로킹·DB I/O 없음 — Ack가 이 뒤에 나간다)."""
        ...


@dataclass(slots=True)
class DiscardHistory:
    """historian(M3a-3) 전까지의 sink: 개수만 센다."""

    live: int = 0
    replay: int = 0

    def offer(self, samples: Sequence[pb.Sample], *, replay: bool) -> None:
        if replay:
            self.replay += len(samples)
        else:
            self.live += len(samples)


@dataclass(slots=True)
class IngestStats:
    batches: int = 0
    heartbeats: int = 0
    live: int = 0
    stale: int = 0
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
    ) -> None:
        self._tags = tags
        self._devices = devices
        self._history = history
        self._live_max_age_ns = int(live_max_age_s * 1e9)
        self._wall_ns = wall_ns
        self.stats = IngestStats()

    def ingest(self, batch: pb.ValueBatch) -> list[Event]:
        self.stats.batches += 1
        samples = list(batch.samples)
        if not samples:
            self.stats.heartbeats += 1
            return []
        self._history.offer(samples, replay=batch.replay)
        if batch.replay:
            self.stats.replay += len(samples)
            return []
        cutoff = self._wall_ns() - self._live_max_age_ns
        fresh = [reading(s) for s in samples if s.ts_unix_ns >= cutoff]
        self.stats.stale += len(samples) - len(fresh)
        self.stats.live += len(fresh)
        events = self._tags.commit(fresh)
        events.extend(self._devices.seen(r.tag_id for r in fresh))
        return events
