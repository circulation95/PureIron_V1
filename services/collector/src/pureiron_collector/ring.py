"""보낸 `ValueBatch`를 core Ack까지 보관하는 메모리 링버퍼(plan §M3a ingest 의미론 B9).

- core는 batch를 in-memory ingest한 직후 그 seq까지 Ack한다 → Ack된 batch는 바로 버린다.
- 링버퍼가 맡는 것은 **core 다운·스트림 단절 구간**뿐이다(DB 장애는 core 상한 버퍼 담당).
- 재연결하면 남은 batch를 `replay=true`로 다시 보낸다 — core는 historian에만 기록한다.
- 보관 창(기본 60s)을 넘긴 batch는 버린다. 이것이 문서화된 허용 손실이다.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pureiron_collector._gen import collector_pb2 as pb

log = logging.getLogger(__name__)

DEFAULT_WINDOW_S = 60.0


@dataclass(frozen=True, slots=True)
class Entry:
    created: float
    batch: pb.ValueBatch


class RingBuffer:
    def __init__(
        self, window_s: float = DEFAULT_WINDOW_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.window_s = window_s
        self._clock = clock
        self._entries: deque[Entry] = deque()
        self._next_seq = 1
        self.dropped_batches = 0
        self.dropped_samples = 0
        self._dropping_since: int | None = None  # 이번 단절 구간 시작 시점의 dropped_samples

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def oldest_unacked_seq(self) -> int:
        self._evict(self._clock())
        return self._entries[0].batch.seq if self._entries else 0

    def append(self, plan_rev: int, samples: Sequence[pb.Sample]) -> pb.ValueBatch:
        now = self._clock()
        self._evict(now)
        batch = pb.ValueBatch(plan_rev=plan_rev, seq=self._next_seq, samples=samples)
        self._next_seq += 1
        self._entries.append(Entry(now, batch))
        return batch

    def ack(self, seq: int) -> None:
        """seq 이하를 모두 해제한다(Ack는 누적). 이미 해제된 seq·heartbeat(0)의 Ack는 무시된다."""
        released = False
        while self._entries and self._entries[0].batch.seq <= seq:
            self._entries.popleft()
            released = True
        if released and self._dropping_since is not None:
            log.warning(
                "core acked again; %d sample(s) were dropped while unacked",
                self.dropped_samples - self._dropping_since,
            )
            self._dropping_since = None

    def pending(self) -> list[pb.ValueBatch]:
        """재연결 직후 다시 보낼 batch들(오래된 순, `replay=true` 사본)."""
        self._evict(self._clock())
        replays: list[pb.ValueBatch] = []
        for entry in self._entries:
            replay = pb.ValueBatch()
            replay.CopyFrom(entry.batch)
            replay.replay = True
            replays.append(replay)
        return replays

    def _evict(self, now: float) -> None:
        """보관 창을 넘긴 batch를 앞에서부터 버린다(deque는 오래된 순 → 창 안을 만나면 멈춤).

        로그는 단절 구간마다 처음 한 번과 Ack 회복 시 요약 한 번만 남긴다(100ms마다 찍지 않음).
        """
        while self._entries and now - self._entries[0].created > self.window_s:
            entry = self._entries.popleft()
            if self._dropping_since is None:
                self._dropping_since = self.dropped_samples
                log.warning(
                    "ring window %.0fs exceeded without Ack: dropping oldest batches (from seq %d)",
                    self.window_s,
                    entry.batch.seq,
                )
            self.dropped_batches += 1
            self.dropped_samples += len(entry.batch.samples)
