"""이력 조회: raw(`tag_history`) / 1m(`tag_history_1m`) / 1h(`tag_history_1h`).

1m·1h는 실시간 집계 CAGG(0009, materialized_only=false)라 refresh 전 최근 구간도 포함된다(D4).
HTTP 노출(인증·Trend 화면)은 M3b(D8) — 여기서는 조회 함수만 둔다. 구간은 [start, end).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import asyncpg

from pureiron_core.platform.db import Connection

type Resolution = Literal["raw", "1m", "1h"]
type AggResolution = Literal["1m", "1h"]

RAW_MAX_SPAN = timedelta(hours=1)
MINUTE_MAX_SPAN = timedelta(hours=24)
DEFAULT_LIMIT = 100_000
QUERY_TIMEOUT_S = 10.0

_RAW_SQL = """
SELECT tag_id, time, value_num, value_bool, quality
  FROM tag_history
 WHERE tag_id = ANY($1::int4[]) AND time >= $2 AND time < $3
 ORDER BY tag_id, time
 LIMIT $4
"""

_AGG_COLUMNS = "tag_id, bucket, avg, min, max, last, samples"
_AGG_WHERE = "WHERE tag_id = ANY($1::int4[]) AND bucket >= $2 AND bucket < $3"
_AGG_TAIL = "ORDER BY tag_id, bucket LIMIT $4"
# 뷰 이름은 고정 표에서만 온다(사용자 입력을 SQL에 넣지 않는다).
_AGG_SQL: dict[AggResolution, str] = {
    "1m": f"SELECT {_AGG_COLUMNS} FROM tag_history_1m {_AGG_WHERE} {_AGG_TAIL}",
    "1h": f"SELECT {_AGG_COLUMNS} FROM tag_history_1h {_AGG_WHERE} {_AGG_TAIL}",
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


def auto_resolution(start: datetime, end: datetime) -> Resolution:
    """구간 길이로 해상도를 고른다: ≤ 1h raw, ≤ 24h 1m, 그 이상 1h."""
    span = end - start
    if span <= RAW_MAX_SPAN:
        return "raw"
    if span <= MINUTE_MAX_SPAN:
        return "1m"
    return "1h"


def _check_window(start: datetime, end: datetime, limit: int) -> None:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start/end must be timezone-aware")
    if end <= start:
        raise ValueError("end must be after start")
    if limit <= 0:
        raise ValueError("limit must be positive")


def _opt_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


async def read_raw(
    conn: Connection,
    tag_ids: Sequence[int],
    start: datetime,
    end: datetime,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[RawPoint]:
    _check_window(start, end, limit)
    rows = await conn.fetch(_RAW_SQL, list(tag_ids), start, end, limit, timeout=QUERY_TIMEOUT_S)
    return [_raw(r) for r in rows]


async def read_aggregate(
    conn: Connection,
    tag_ids: Sequence[int],
    start: datetime,
    end: datetime,
    resolution: AggResolution,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[AggPoint]:
    _check_window(start, end, limit)
    rows = await conn.fetch(
        _AGG_SQL[resolution], list(tag_ids), start, end, limit, timeout=QUERY_TIMEOUT_S
    )
    return [_agg(r) for r in rows]


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
