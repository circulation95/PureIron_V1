"""알람 저널: 전이 행 상한 큐 → 주기 쓰기(`AlarmJournalWriter`)와 site-db 저장·조회·복원
(`PgAlarmStore`), plan M3b D7·D8.

- bus 핸들러는 I/O를 await 하지 않는다 → 전이 행은 `offer`로 메모리 큐에만 넣고 writer task가
  비운다.
- 큐 상한(기본 10,000행)을 넘으면 가장 오래된 행부터 버리고 `dropped_total`을 올린다(site-db 장애가
  길어도 메모리가 늘지 않게). 쓰기 실패는 행을 큐 앞에 되돌리고 다음 주기에 다시 쓴다.
- ACK는 큐를 거치지 않는다: 전이 행과 감사 기록을 한 트랜잭션으로 쓰고(`append_acked`), 성공한
  뒤에만 엔진에 반영한다.
- 행 시각은 엔진이 정한 전이 시각(core 벽시계)이다. 복원은 알람별 마지막 행(time·id 내림차순).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import asyncpg

from pureiron_core.alarms.ports import (
    MAX_JOURNAL_ROWS,
    AlarmTransition,
    AlarmUnavailableError,
    JournalQuery,
    JournalRecord,
)
from pureiron_core.audit.ports import AuditEntry, AuditWriter
from pureiron_core.platform.db import Database, DatabaseUnavailableError

log = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 10_000
DEFAULT_FLUSH_S = 1.0
DEFAULT_FLUSH_MAX_ROWS = 1_000
DEFAULT_STOP_TIMEOUT_S = 3.0
DROP_WARN_INTERVAL_S = 60.0
_WRITE_TIMEOUT_S = 5.0
_QUERY_TIMEOUT_S = 10.0

INSERT_SQL = """
INSERT INTO alarm_journal (time, alarm_id, tag_path, from_state, to_state, value, username, note)
SELECT * FROM unnest(
    $1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::text[], $6::float8[], $7::text[],
    $8::text[]
)
"""
# 알람 정의 행(retired 포함)마다 마지막 저널 한 행 — (alarm_id, time desc) 인덱스로 알람 수만큼만
# 읽는다(저널 전체를 훑지 않는다).
LAST_STATES_SQL = """
SELECT j.id, j.time, j.alarm_id, j.tag_path, j.from_state, j.to_state, j.value, j.username, j.note
  FROM alarms a
 CROSS JOIN LATERAL (
       SELECT * FROM alarm_journal
        WHERE alarm_id = a.alarm_id
        ORDER BY time DESC, id DESC
        LIMIT 1
 ) j
"""
# 조건이 없는 인자는 NULL로 넘겨 하나의 SQL로 둔다(동적 SQL 조립 없음).
SELECT_SQL = """
SELECT id, time, alarm_id, tag_path, from_state, to_state, value, username, note
  FROM alarm_journal
 WHERE ($1::text IS NULL OR alarm_id = $1)
   AND ($2::timestamptz IS NULL OR time >= $2)
   AND ($3::timestamptz IS NULL OR time < $3)
 ORDER BY time DESC, id DESC
 LIMIT $4
"""


@dataclass(frozen=True, slots=True)
class JournalRow:
    time: datetime
    alarm_id: str
    tag_path: str
    from_state: str
    to_state: str
    value: float | None
    username: str | None
    note: str | None = None

    @classmethod
    def of(cls, transition: AlarmTransition) -> JournalRow:
        return cls(
            time=transition.time,
            alarm_id=transition.alarm_id,
            tag_path=transition.tag_path,
            from_state=transition.from_state.value,
            to_state=transition.to_state.value,
            value=transition.value,
            username=transition.username,
        )


class AlarmStore(Protocol):
    """저널 저장소(I/O 경계 — 테스트는 가짜). site-db 문제는 모두 `AlarmUnavailableError`."""

    async def append(self, rows: Sequence[JournalRow]) -> None: ...

    async def append_acked(self, rows: Sequence[JournalRow], entry: AuditEntry) -> None:
        """전이 행과 감사 기록을 한 트랜잭션으로 쓴다(둘 다 쓰이거나 둘 다 안 쓰인다)."""
        ...

    async def last_states(self) -> dict[str, JournalRecord]: ...

    async def query(self, query: JournalQuery) -> tuple[JournalRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class JournalWriterStatus:
    queue_rows: int
    dropped_total: int
    written_total: int
    db_ok: bool


class AlarmJournalWriter:
    def __init__(
        self,
        store: AlarmStore,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        flush_s: float = DEFAULT_FLUSH_S,
        flush_max_rows: int = DEFAULT_FLUSH_MAX_ROWS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._max_rows = max_rows
        self._flush_s = flush_s
        self._flush_max_rows = flush_max_rows
        self._clock = clock
        self._queue: deque[JournalRow] = deque()
        self._dropped_total = 0
        self._written_total = 0
        self._db_ok = True
        self._warned_at: float | None = None
        self._last_error: str | None = None
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def offer(self, rows: Sequence[JournalRow]) -> None:
        """큐에 넣기만 한다(await 없음 — bus 핸들러에서 부른다)."""
        self._queue.extend(rows)
        self._trim()

    def _trim(self) -> None:
        excess = len(self._queue) - self._max_rows
        if excess <= 0:
            return
        for _ in range(excess):
            self._queue.popleft()
        self._dropped_total += excess
        now = self._clock()
        if self._warned_at is None or now - self._warned_at >= DROP_WARN_INTERVAL_S:
            self._warned_at = now
            log.warning(
                "alarm journal queue full (%d rows): dropped %d oldest (total %d)",
                self._max_rows,
                excess,
                self._dropped_total,
            )

    async def flush(self) -> int:
        """큐 앞에서 최대 `flush_max_rows`행을 쓴다. 쓴 행 수(실패하면 0, 행은 큐 앞으로 되돌림)."""
        if not self._queue:
            return 0
        batch = [self._queue.popleft() for _ in range(min(len(self._queue), self._flush_max_rows))]
        try:
            await self._store.append(batch)
        except AlarmUnavailableError as exc:
            self._requeue(batch)
            if self._db_ok:
                log.warning("alarm journal write failed (will retry): %s", exc)
            self._db_ok = False
            return 0
        except BaseException:
            self._requeue(batch)  # 취소(정지 시한 초과)도 행을 잃지 않게 되돌린 뒤 다시 던진다
            raise
        if not self._db_ok:
            log.info("alarm journal writes recovered")
        self._db_ok = True
        self._written_total += len(batch)
        return len(batch)

    def _requeue(self, batch: list[JournalRow]) -> None:
        self._queue.extendleft(reversed(batch))
        self._trim()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), self._flush_s)
            try:
                while await self.flush() == self._flush_max_rows:
                    pass  # 밀린 행은 주기를 기다리지 않고 이어서 쓴다
                self._last_error = None
            except Exception as exc:
                # 같은 오류가 주기마다 반복되므로 바뀔 때만 traceback을 남긴다.
                if repr(exc) != self._last_error:
                    log.exception("alarm journal flush failed")
                self._last_error = repr(exc)

    def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="alarm-journal")

    async def stop(self, timeout_s: float = DEFAULT_STOP_TIMEOUT_S) -> None:
        """루프를 끝내고(진행 중인 쓰기는 마치게) 남은 행을 제한 시간 안에 쓴다."""
        self._stopping.set()
        try:
            async with asyncio.timeout(timeout_s):
                if self._task is not None:
                    await self._task
                while self._queue and await self.flush():
                    pass
        except TimeoutError:
            log.warning("alarm journal stop timed out; %d row(s) not written", len(self._queue))
        finally:
            if self._task is not None and not self._task.done():
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._queue:
            log.warning("alarm journal: %d row(s) not written at stop", len(self._queue))

    def status(self) -> JournalWriterStatus:
        return JournalWriterStatus(
            queue_rows=len(self._queue),
            dropped_total=self._dropped_total,
            written_total=self._written_total,
            db_ok=self._db_ok,
        )


def _columns(rows: Sequence[JournalRow]) -> tuple[list[object], ...]:
    return (
        [r.time for r in rows],
        [r.alarm_id for r in rows],
        [r.tag_path for r in rows],
        [r.from_state for r in rows],
        [r.to_state for r in rows],
        [r.value for r in rows],
        [r.username for r in rows],
        [r.note for r in rows],
    )


def _record(row: asyncpg.Record) -> JournalRecord:
    value: object = row["value"]
    return JournalRecord(
        id=int(row["id"]),
        time=row["time"],
        alarm_id=str(row["alarm_id"]),
        tag_path=str(row["tag_path"]),
        from_state=str(row["from_state"]),
        to_state=str(row["to_state"]),
        value=float(value) if isinstance(value, int | float) else None,
        username=row["username"],
        note=row["note"],
    )


# site-db 문제로 보는 예외(연결 상태 오류 InterfaceError 포함). 그 밖의 예외는 버그라 그대로 올린다.
_DB_ERRORS = (DatabaseUnavailableError, asyncpg.PostgresError, asyncpg.InterfaceError)


class PgAlarmStore:
    def __init__(self, db: Database, audit: AuditWriter) -> None:
        self._db = db
        self._audit = audit

    async def append(self, rows: Sequence[JournalRow]) -> None:
        try:
            async with self._db.connection() as conn:
                await conn.execute(INSERT_SQL, *_columns(rows), timeout=_WRITE_TIMEOUT_S)
        except _DB_ERRORS as exc:
            raise AlarmUnavailableError(f"site-db error: {exc}") from exc

    async def append_acked(self, rows: Sequence[JournalRow], entry: AuditEntry) -> None:
        try:
            async with self._db.connection() as conn, conn.transaction():
                await conn.execute(INSERT_SQL, *_columns(rows), timeout=_WRITE_TIMEOUT_S)
                await self._audit.record_with(conn, entry)
        except _DB_ERRORS as exc:
            raise AlarmUnavailableError(f"site-db error: {exc}") from exc

    async def last_states(self) -> dict[str, JournalRecord]:
        try:
            async with self._db.connection() as conn:
                rows = await conn.fetch(LAST_STATES_SQL, timeout=_QUERY_TIMEOUT_S)
        except _DB_ERRORS as exc:
            raise AlarmUnavailableError(f"site-db error: {exc}") from exc
        return {record.alarm_id: record for record in map(_record, rows)}

    async def query(self, query: JournalQuery) -> tuple[JournalRecord, ...]:
        limit = max(1, min(query.limit, MAX_JOURNAL_ROWS))
        try:
            async with self._db.connection() as conn:
                rows = await conn.fetch(
                    SELECT_SQL,
                    query.alarm_id,
                    query.start,
                    query.end,
                    limit,
                    timeout=_QUERY_TIMEOUT_S,
                )
        except _DB_ERRORS as exc:
            raise AlarmUnavailableError(f"site-db error: {exc}") from exc
        return tuple(map(_record, rows))
