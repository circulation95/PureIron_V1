"""이력 조회: raw(`tag_history`) / 1m(`tag_history_1m`) / 1h(`tag_history_1h`), plan M3b D11.

1m·1h는 실시간 집계 CAGG(0009, materialized_only=false)라 refresh 전 최근 구간도 포함된다(D4).
구간은 [start, end)이고 결과는 태그별 series(시간순)다.

- 행 상한은 **태그별**이다(`limit_per_tag`) — 한 태그의 행이 많아도 다른 태그를 굶기지 않는다.
  상한을 넘긴 태그는 앞쪽 `limit_per_tag`행만 주고 `truncated=True`로 표시한다(L10).
- 1m·1h의 첫 bucket은 `time_bucket` 경계로 맞춘다: start가 bucket 중간이어도 start를 품은 bucket을
  포함한다(CAGG와 같은 `time_bucket(interval, time)` 식, L10).
- 구간 길이 상한(`MAX_SPAN`): raw 1h · 1m 7d · 1h 400d. 넘는 요청은 `ValueError`(API는 422).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import asyncpg

from pureiron_core.historian.ports import HistoryUnavailableError
from pureiron_core.platform.db import Connection, Database, DatabaseUnavailableError

type Resolution = Literal["raw", "1m", "1h"]
type AggResolution = Literal["1m", "1h"]

RAW_MAX_SPAN = timedelta(hours=1)
MINUTE_MAX_SPAN = timedelta(hours=24)
MAX_SPAN: dict[Resolution, timedelta] = {
    "raw": timedelta(hours=1),
    "1m": timedelta(days=7),
    "1h": timedelta(days=400),
}
DEFAULT_LIMIT_PER_TAG = 20_000
QUERY_TIMEOUT_S = 10.0

# 태그마다 상한 + 1행을 읽어 잘림 여부를 안다(LATERAL = 태그별 인덱스 범위 조회).
_RAW_SQL = """
SELECT t.tag_id, h.time, h.value_num, h.value_bool, h.quality
  FROM unnest($1::int4[]) AS t(tag_id)
 CROSS JOIN LATERAL (
       SELECT time, value_num, value_bool, quality
         FROM tag_history
        WHERE tag_id = t.tag_id AND time >= $2 AND time < $3
        ORDER BY time
        LIMIT $4
 ) h
 ORDER BY t.tag_id, h.time
"""


def _agg_sql(view: str, interval: str) -> str:
    # 뷰 이름·간격은 아래 고정 표에서만 온다(사용자 입력을 SQL에 넣지 않는다).
    return f"""
SELECT t.tag_id, a.bucket, a.avg, a.min, a.max, a.last, a.samples
  FROM unnest($1::int4[]) AS t(tag_id)
 CROSS JOIN LATERAL (
       SELECT bucket, avg, min, max, last, samples
         FROM {view}
        WHERE tag_id = t.tag_id
          AND bucket >= time_bucket(INTERVAL '{interval}', $2::timestamptz) AND bucket < $3
        ORDER BY bucket
        LIMIT $4
 ) a
 ORDER BY t.tag_id, a.bucket
"""


_AGG_SQL: dict[AggResolution, str] = {
    "1m": _agg_sql("tag_history_1m", "1 minute"),
    "1h": _agg_sql("tag_history_1h", "1 hour"),
}


@dataclass(frozen=True, slots=True)
class RawPoint:
    tag_id: int
    time: datetime
    value_num: float | None
    value_bool: bool | None
    quality: int


@dataclass(frozen=True, slots=True)
class AggPoint:
    """`samples` = 행 수(bool·NULL 행 포함) — avg의 분모와 다르다(0006 주석)."""

    tag_id: int
    bucket: datetime
    avg: float | None
    min: float | None
    max: float | None
    last: float | None
    samples: int


@dataclass(frozen=True, slots=True)
class Series[P]:
    tag_id: int
    points: tuple[P, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """요청한 tag_id 순서대로 series 하나씩(행이 없는 태그도 빈 series)."""

    resolution: Resolution
    raw: tuple[Series[RawPoint], ...] = ()
    aggregate: tuple[Series[AggPoint], ...] = ()


def auto_resolution(start: datetime, end: datetime) -> Resolution:
    """구간 길이로 해상도를 고른다: ≤ 1h raw, ≤ 24h 1m, 그 이상 1h."""
    span = end - start
    if span <= RAW_MAX_SPAN:
        return "raw"
    if span <= MINUTE_MAX_SPAN:
        return "1m"
    return "1h"


def check_window(start: datetime, end: datetime, resolution: Resolution, limit: int) -> None:
    """조회 전에 거른다(ValueError → API 422)."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > MAX_SPAN[resolution]:
        raise ValueError(f"span exceeds {MAX_SPAN[resolution]} for resolution {resolution}")
    if limit <= 0:
        raise ValueError("limit must be positive")


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _series[P](
    tag_ids: Sequence[int], by_tag: dict[int, list[P]], limit: int
) -> tuple[Series[P], ...]:
    """상한 + 1행까지 읽은 태그별 행 → series(넘친 태그는 상한까지 + truncated)."""
    return tuple(
        Series(tag_id, tuple(by_tag.get(tag_id, [])[:limit]), len(by_tag.get(tag_id, [])) > limit)
        for tag_id in tag_ids
    )


async def read_raw(
    conn: Connection,
    tag_ids: Sequence[int],
    start: datetime,
    end: datetime,
    *,
    limit_per_tag: int = DEFAULT_LIMIT_PER_TAG,
) -> tuple[Series[RawPoint], ...]:
    check_window(start, end, "raw", limit_per_tag)
    ids = list(dict.fromkeys(tag_ids))
    rows = await conn.fetch(_RAW_SQL, ids, start, end, limit_per_tag + 1, timeout=QUERY_TIMEOUT_S)
    by_tag: dict[int, list[RawPoint]] = {}
    for row in rows:
        point = _raw(row)
        by_tag.setdefault(point.tag_id, []).append(point)
    return _series(ids, by_tag, limit_per_tag)


async def read_aggregate(
    conn: Connection,
    tag_ids: Sequence[int],
    start: datetime,
    end: datetime,
    resolution: AggResolution,
    *,
    limit_per_tag: int = DEFAULT_LIMIT_PER_TAG,
) -> tuple[Series[AggPoint], ...]:
    check_window(start, end, resolution, limit_per_tag)
    ids = list(dict.fromkeys(tag_ids))
    rows = await conn.fetch(
        _AGG_SQL[resolution], ids, start, end, limit_per_tag + 1, timeout=QUERY_TIMEOUT_S
    )
    by_tag: dict[int, list[AggPoint]] = {}
    for row in rows:
        point = _agg(row)
        by_tag.setdefault(point.tag_id, []).append(point)
    return _series(ids, by_tag, limit_per_tag)


def _raw(r: asyncpg.Record) -> RawPoint:
    flag: object = r["value_bool"]
    return RawPoint(
        tag_id=int(r["tag_id"]),
        time=r["time"],
        value_num=_opt_float(r["value_num"]),
        value_bool=flag if isinstance(flag, bool) else None,
        quality=int(r["quality"]),
    )


def _agg(r: asyncpg.Record) -> AggPoint:
    return AggPoint(
        tag_id=int(r["tag_id"]),
        bucket=r["bucket"],
        avg=_opt_float(r["avg"]),
        min=_opt_float(r["min"]),
        max=_opt_float(r["max"]),
        last=_opt_float(r["last"]),
        samples=int(r["samples"]),
    )


class PgHistoryReader:
    """HTTP 이력 조회(`GET /history`). site-db 문제는 `HistoryUnavailableError`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def read(
        self,
        tag_ids: Sequence[int],
        start: datetime,
        end: datetime,
        resolution: Resolution,
        *,
        limit_per_tag: int,
    ) -> HistoryResult:
        check_window(start, end, resolution, limit_per_tag)
        try:
            async with self._db.connection() as conn:
                if resolution == "raw":
                    raw = await read_raw(conn, tag_ids, start, end, limit_per_tag=limit_per_tag)
                    return HistoryResult(resolution, raw=raw)
                aggregate = await read_aggregate(
                    conn, tag_ids, start, end, resolution, limit_per_tag=limit_per_tag
                )
                return HistoryResult(resolution, aggregate=aggregate)
        except (DatabaseUnavailableError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            raise HistoryUnavailableError(f"site-db error: {exc}") from exc
