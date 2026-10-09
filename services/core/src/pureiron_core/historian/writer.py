"""상한 메모리 버퍼 → `tag_history`(plan B9 N-buffer).

- `append`는 동기·논블로킹이다(ingest 직후 Ack가 나간다). 버퍼가 상한(기본 600,000행)을 넘으면
  **가장 오래된 행부터** 버리고 `dropped_total`을 센다. 쓰는 중(꺼낸) 행은 상한 밖이라 순간 최대는
  상한 + `flush_max_rows`(기본 620,000행)다.
- flush loop가 주기(기본 1s)마다 버퍼를 최대 `flush_max_rows`씩 꺼내
  `INSERT … ON CONFLICT (tag_id, time) DO NOTHING`으로 쓴다(replay 중복 흡수). 밀려 있으면 같은
  주기에 버퍼가 빌 때까지 계속 쓴다.
- 쓰기에 실패하면 꺼낸 행을 버퍼 앞에 순서대로 되돌리고 상한을 다시 적용한다 — DB 장애 동안은 이
  버퍼가 이력을 들고 있고(collector 링버퍼는 core 단절 구간만 담당), 넘치면 오래된 것부터 잃는다.
- 한 번의 쓰기(연결 획득·INSERT·연결 반환 대기)는 `write_timeout_s`(기본 쿼리 제한 10s + 여유 5s)
  안에 끝나지 않으면 취소하고 실패로 본다 — half-open DB 연결에서도 flush가 그 시간 넘게 멈추지
  않고, 행은 버퍼로 돌아가 다음 주기에 다시 쓴다. 취소된 연결의 정리(asyncpg는 반환을 shield한다)는
  백그라운드에서 계속될 수 있다.
- 문서화된 허용 손실: core 프로세스 crash 시 아직 쓰지 못한 버퍼(정상 시 ≤ 1 주기분, DB 장애 중이면
  상한 내 전체).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pureiron_core.platform.db import Database

log = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 600_000
DEFAULT_FLUSH_S = 1.0
DEFAULT_FLUSH_MAX_ROWS = 20_000
DEFAULT_STOP_TIMEOUT_S = 5.0
QUERY_TIMEOUT_S = 10.0
# flush 한 번의 쓰기 전체 제한: 쿼리 제한 + 연결 획득·반환 여유.
DEFAULT_WRITE_TIMEOUT_S = QUERY_TIMEOUT_S + 5.0
DROP_WARN_INTERVAL_S = 60.0

# 시각은 µs 정수로 넘겨 DB에서 timestamptz로 바꾼다(datetime 변환 비용·부동소수 반올림 없음).
INSERT_SQL = """
INSERT INTO tag_history (time, tag_id, value_num, value_bool, quality)
SELECT 'epoch'::timestamptz + r.us * INTERVAL '1 microsecond', r.tag_id, r.num, r.flag, r.quality
  FROM unnest($1::bigint[], $2::int4[], $3::float8[], $4::bool[], $5::int2[])
       AS r(us, tag_id, num, flag, quality)
ON CONFLICT (tag_id, time) DO NOTHING
"""


@dataclass(frozen=True, slots=True)
class Row:
    """`tag_history` 한 행. 시각은 ns(DB에는 µs로 들어간다), 비유한 수치는 이미 None."""

    ts_ns: int
    tag_id: int
    value_num: float | None
    value_bool: bool | None
    quality: int


class RowStore(Protocol):
    async def insert(self, rows: Sequence[Row]) -> None:
        """한 번에 쓴다. 실패하면 예외(writer가 행을 버퍼에 되돌린다)."""
        ...


type Columns = tuple[list[int], list[int], list[float | None], list[bool | None], list[int]]


def columns(rows: Sequence[Row]) -> Columns:
    return (
        [r.ts_ns // 1000 for r in rows],
        [r.tag_id for r in rows],
        [r.value_num for r in rows],
        [r.value_bool for r in rows],
        [r.quality for r in rows],
    )


class PgRowStore:
    """`tag_history`에 쓴다. 쿼리 제한 시간은 INSERT 실행에만 걸린다.

    연결 획득·반환까지 포함한 전체 제한은 `HistoryWriter`(`write_timeout_s`)가 건다.
    """

    def __init__(self, db: Database, *, query_timeout_s: float = QUERY_TIMEOUT_S) -> None:
        self._db = db
        self._query_timeout_s = query_timeout_s

    async def insert(self, rows: Sequence[Row]) -> None:
        async with self._db.connection() as conn:
            await conn.execute(INSERT_SQL, *columns(rows), timeout=self._query_timeout_s)


@dataclass(slots=True)
class _Chunk:
    enqueued: float  # monotonic
    rows: list[Row]


@dataclass(frozen=True, slots=True)
class WriterStatus:
    buffer_rows: int
    dropped_total: int
    written_total: int
    write_lag_s: float | None
    db_ok: bool


class HistoryWriter:
    def __init__(
        self,
        store: RowStore,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        flush_s: float = DEFAULT_FLUSH_S,
        flush_max_rows: int = DEFAULT_FLUSH_MAX_ROWS,
        write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._max_rows = max_rows
        self._flush_s = flush_s
        self._flush_max_rows = flush_max_rows
        self._write_timeout_s = write_timeout_s
        self._clock = clock
        self._chunks: deque[_Chunk] = deque()
        self._rows = 0
        # 쓰는 중(꺼냈지만 아직 결과를 모르는) 행: 상태 보고에만 쓴다.
        self._inflight_rows = 0
        self._inflight_since: float | None = None
        self._dropped = 0
        self._written = 0
        self._db_ok = True
        self._drop_warned_at: float | None = None
        self._flush_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    # --- 버퍼 (동기) -----------------------------------------------------------------------
    def append(self, rows: Sequence[Row]) -> None:
        if not rows:
            return
        self._chunks.append(_Chunk(self._clock(), list(rows)))
        self._rows += len(rows)
        self._trim()

    def _trim(self) -> None:
        excess = self._rows - self._max_rows
        if excess <= 0:
            return
        dropped = excess
        while excess > 0:
            head = self._chunks[0]
            if len(head.rows) <= excess:
                excess -= len(head.rows)
                self._chunks.popleft()
            else:
                del head.rows[:excess]
                excess = 0
        self._rows -= dropped
        self._dropped += dropped
        now = self._clock()
        if self._drop_warned_at is None or now - self._drop_warned_at >= DROP_WARN_INTERVAL_S:
            self._drop_warned_at = now
            log.warning(
                "historian buffer full (%d rows): dropped %d oldest row(s) (total dropped=%d)",
                self._max_rows,
                dropped,
                self._dropped,
            )

    def _take(self) -> list[_Chunk]:
        taken: list[_Chunk] = []
        count = 0
        while self._chunks and count < self._flush_max_rows:
            head = self._chunks[0]
            room = self._flush_max_rows - count
            if len(head.rows) <= room:
                taken.append(self._chunks.popleft())
                count += len(head.rows)
            else:
                taken.append(_Chunk(head.enqueued, head.rows[:room]))
                del head.rows[:room]
                count += room
        self._rows -= count
        return taken

    def _put_back(self, taken: list[_Chunk]) -> None:
        """실패한 행을 순서대로 앞에 되돌린다. 넘치면 _trim이 이 (가장 오래된) 행부터 버린다."""
        self._chunks.extendleft(reversed(taken))
        self._rows += sum(len(c.rows) for c in taken)
        self._trim()

    def status(self) -> WriterStatus:
        oldest = self._inflight_since
        if self._chunks:
            head = self._chunks[0].enqueued
            oldest = head if oldest is None else min(oldest, head)
        return WriterStatus(
            buffer_rows=self._rows + self._inflight_rows,
            dropped_total=self._dropped,
            written_total=self._written,
            write_lag_s=None if oldest is None else self._clock() - oldest,
            db_ok=self._db_ok,
        )

    # --- flush (비동기) --------------------------------------------------------------------
    async def flush(self) -> int:
        """버퍼가 빌 때까지(또는 실패할 때까지) 쓴다. 이번에 쓴 행 수."""
        async with self._flush_lock:
            total = 0
            while self._chunks:
                taken = self._take()
                rows = [r for chunk in taken for r in chunk.rows]
                self._inflight_rows = len(rows)
                self._inflight_since = taken[0].enqueued
                try:
                    async with asyncio.timeout(self._write_timeout_s):
                        await self._store.insert(rows)
                except asyncio.CancelledError:
                    self._put_back(taken)
                    raise
                except Exception as exc:
                    self._put_back(taken)
                    if self._db_ok:
                        log.warning(
                            "historian write failed, keeping %d row(s) in memory: %s",
                            self._rows,
                            f"timed out (write limit {self._write_timeout_s:g}s)"
                            if isinstance(exc, TimeoutError)
                            else exc,
                        )
                    self._db_ok = False
                    return total
                finally:
                    self._inflight_rows = 0
                    self._inflight_since = None
                if not self._db_ok:
                    log.info("historian writes recovered (%d row(s) still buffered)", self._rows)
                self._db_ok = True
                self._written += len(rows)
                total += len(rows)
            return total

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_s)
            try:
                await self.flush()
            except Exception:
                log.exception("historian flush loop error")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="historian-flush")

    async def stop(self, timeout_s: float = DEFAULT_STOP_TIMEOUT_S) -> None:
        """flush loop를 멈추고 남은 버퍼를 제한 시간 안에서 마지막으로 쓴다.

        수집(acquisition)을 먼저 멈춘 뒤 호출한다 — 그 뒤로는 새 행이 들어오지 않는다. 다 쓰지
        못한 행은 허용 손실로 경고만 남긴다.
        """
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.flush(), timeout_s)
        if self._rows:
            log.warning("historian stopped with %d unwritten row(s)", self._rows)
