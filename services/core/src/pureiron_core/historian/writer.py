"""상한 메모리 버퍼 → `tag_history`(plan B9 N-buffer).

- `append`는 동기·논블로킹이다(ingest 직후 Ack가 나간다). 버퍼가 상한(기본 600,000행)을 넘으면
  **가장 오래된 행부터** 버리고 `dropped_total`을 센다. 쓰는 중(꺼낸) 행은 상한 밖이라 순간 최대는
  상한 + `flush_max_rows`(기본 620,000행)다.
- flush loop가 주기(기본 1s)마다 버퍼를 최대 `flush_max_rows`씩 꺼내
  `INSERT … ON CONFLICT (tag_id, time) DO NOTHING`으로 쓴다(replay 중복 흡수). 밀려 있으면 같은
  주기에 버퍼가 빌 때까지 계속 쓴다.
- 쓰기에 실패하면 꺼낸 행을 버퍼 앞에 순서대로 되돌리고 상한을 다시 적용한다 — DB 장애 동안은 이
  버퍼가 이력을 들고 있고(collector 링버퍼는 core 단절 구간만 담당), 넘치면 오래된 것부터 잃는다.
- 단 행 내용 때문에 거부된 쓰기(`RowsRejectedError` — 같은 행을 다시 써도 또 실패)는 되돌리지 않고
  batch를 반으로 나눠 앞쪽부터 다시 쓴다. 1행까지 줄어도 거부되면 그 행만 버리고 `poisoned_total`을
  센다(R1 L9b: 한 행이 버퍼 전체를 매 주기 막지 않게). 독성 행 k개가 섞인 n행 batch는 INSERT를
  약 2k·log2(n)번 더 한다. 알려진 원인(int4 밖 tag_id·NaN·모르는 quality)은 recorder가 미리 막는다.
- 한 번의 쓰기(연결 획득·INSERT·연결 반환 대기)는 `write_timeout_s`(기본 쿼리 제한 10s + 여유 5s)
  가 지나면 취소하고 실패로 본다 — 행은 버퍼로 돌아가 다음 주기에 다시 쓴다. 이 취소는 한 번뿐이라
  INSERT 도중에 걸리면 이어지는 연결 반환(asyncpg는 shield한다)은 끊지 못한다. 그 반환은
  `Database`의 획득·반환 제한(기본 5s, 넘으면 연결을 terminate)으로 끝나므로 half-open DB
  연결에서도 flush 한 번은 최대 `write_timeout_s` + 그 제한(기본 20s) 안에 돌아온다.
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

import asyncpg

from pureiron_core.platform.db import Database

log = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 600_000
DEFAULT_FLUSH_S = 1.0
DEFAULT_FLUSH_MAX_ROWS = 20_000
DEFAULT_STOP_TIMEOUT_S = 5.0
QUERY_TIMEOUT_S = 10.0
# flush 한 번의 쓰기 제한: 쿼리 제한 + 연결 획득 여유. 이 제한이 INSERT 도중에 걸린 뒤의 연결 반환은
# `Database`의 획득·반환 제한이 따로 끝낸다.
DEFAULT_WRITE_TIMEOUT_S = QUERY_TIMEOUT_S + 5.0
DROP_WARN_INTERVAL_S = 60.0
POISON_WARN_INTERVAL_S = 60.0

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


class RowsRejectedError(Exception):
    """DB가 행 내용 때문에 쓰기를 거부했다 — 같은 행을 다시 써도 또 실패한다(writer가 반분 격리)."""


class RowStore(Protocol):
    async def insert(self, rows: Sequence[Row]) -> None:
        """한 번에 쓴다. 행 내용 오류면 `RowsRejectedError`, 그 밖의 실패(연결·시간 초과)는 다른
        예외(writer가 행을 버퍼에 되돌린다)."""
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

    연결 획득·반환 제한은 `Database`가, 한 번의 쓰기 전체 제한은 `HistoryWriter`
    (`write_timeout_s`)가 건다.
    """

    def __init__(self, db: Database, *, query_timeout_s: float = QUERY_TIMEOUT_S) -> None:
        self._db = db
        self._query_timeout_s = query_timeout_s

    async def insert(self, rows: Sequence[Row]) -> None:
        try:
            async with self._db.connection() as conn:
                await conn.execute(INSERT_SQL, *columns(rows), timeout=self._query_timeout_s)
        except (asyncpg.DataError, asyncpg.IntegrityConstraintViolationError) as exc:
            # 서버가 행 값을 거부(SQLSTATE 22·23: 범위 밖 시각 등).
            raise RowsRejectedError(str(exc)) from exc
        except asyncpg.InterfaceError as exc:
            # 클라이언트 쪽 인자 인코딩 실패(asyncpg 내부 `exceptions._base.DataError`, 공개 이름이
            # 없어 ValueError 겸함으로 구별한다)도 행 내용 오류다. 나머지 InterfaceError는 그대로.
            if isinstance(exc, ValueError):
                raise RowsRejectedError(str(exc)) from exc
            raise


@dataclass(slots=True)
class _Chunk:
    enqueued: float  # monotonic
    rows: list[Row]


@dataclass(frozen=True, slots=True)
class WriterStatus:
    buffer_rows: int
    dropped_total: int
    written_total: int
    poisoned_total: int
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
        # 쓰는 중(꺼냈지만 아직 결과를 모르는) 행. 꺼낸 batch의 앞에서부터 끝난(기록·격리) 만큼
        # 줄어든다 — 실패하면 끝나지 않은 뒤쪽만 되돌린다.
        self._inflight_rows = 0
        self._inflight_since: float | None = None
        self._dropped = 0
        self._written = 0
        self._poisoned = 0
        self._db_ok = True
        self._drop_warned_at: float | None = None
        self._poison_warned_at: float | None = None
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

    def _put_back(self, taken: list[_Chunk], done: int) -> None:
        """앞 `done`행(기록·격리 끝)을 뺀 나머지를 순서대로 앞에 되돌린다.

        넘치면 _trim이 이 (가장 오래된) 행부터 버린다.
        """
        rest: list[_Chunk] = []
        for chunk in taken:
            if done >= len(chunk.rows):
                done -= len(chunk.rows)
                continue
            rest.append(_Chunk(chunk.enqueued, chunk.rows[done:]) if done else chunk)
            done = 0
        self._chunks.extendleft(reversed(rest))
        self._rows += sum(len(c.rows) for c in rest)
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
            poisoned_total=self._poisoned,
            write_lag_s=None if oldest is None else self._clock() - oldest,
            db_ok=self._db_ok,
        )

    # --- flush (비동기) --------------------------------------------------------------------
    async def flush(self) -> int:
        """버퍼가 빌 때까지(또는 실패할 때까지) 쓴다. 이번에 쓴 행 수."""
        async with self._flush_lock:
            written_before = self._written
            while self._chunks:
                taken = self._take()
                rows = [r for chunk in taken for r in chunk.rows]
                self._inflight_rows = len(rows)
                self._inflight_since = taken[0].enqueued
                try:
                    await self._write(rows)
                except asyncio.CancelledError:
                    self._put_back(taken, len(rows) - self._inflight_rows)
                    raise
                except Exception as exc:
                    self._put_back(taken, len(rows) - self._inflight_rows)
                    if self._db_ok:
                        log.warning(
                            "historian write failed, keeping %d row(s) in memory: %s",
                            self._rows,
                            f"timed out (write limit {self._write_timeout_s:g}s)"
                            if isinstance(exc, TimeoutError)
                            else exc,
                        )
                    self._db_ok = False
                    break
                finally:
                    self._inflight_rows = 0
                    self._inflight_since = None
            return self._written - written_before

    async def _write(self, rows: list[Row]) -> None:
        """`rows`를 앞에서부터 쓴다. 행 내용 오류면 반으로 나눠 다시 쓰고 1행이면 버린다.

        구간을 왼쪽부터 처리하므로 끝난(기록·격리) 행은 항상 batch의 앞부분이다 — 다른 실패로
        멈추면 `_inflight_rows`가 남은 뒤쪽 행 수다. 쓰기 제한은 INSERT 한 번마다 건다.
        """
        pending = [(0, len(rows))]  # 끝이 다음에 쓸 (가장 왼쪽) 구간
        while pending:
            start, end = pending.pop()
            try:
                async with asyncio.timeout(self._write_timeout_s):
                    await self._store.insert(rows[start:end])
            except RowsRejectedError as exc:
                if end - start > 1:
                    mid = (start + end) // 2
                    pending += [(mid, end), (start, mid)]
                    continue
                self._poison(rows[start], exc)
            else:
                if not self._db_ok:
                    log.info("historian writes recovered (%d row(s) still buffered)", self._rows)
                self._db_ok = True
                self._written += end - start
            self._inflight_rows -= end - start

    def _poison(self, row: Row, exc: RowsRejectedError) -> None:
        self._poisoned += 1
        now = self._clock()
        if self._poison_warned_at is None or now - self._poison_warned_at >= POISON_WARN_INTERVAL_S:
            self._poison_warned_at = now
            log.warning(
                "historian dropped a row rejected by site-db (tag_id=%d, ts_ns=%d): %s "
                "(total poisoned=%d)",
                row.tag_id,
                row.ts_ns,
                exc,
                self._poisoned,
            )

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
        못한 행은 허용 손실로 경고만 남긴다. half-open DB에서는 진행 중 쓰기의 취소와 최종 flush의
        `timeout_s` 뒤에 각각 연결 반환 정리(`Database` 획득·반환 제한, 기본 5s)가 더해질 수 있다.
        """
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.flush(), timeout_s)
        if self._rows:
            log.warning("historian stopped with %d unwritten row(s)", self._rows)
