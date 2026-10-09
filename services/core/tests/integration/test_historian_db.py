"""historian ↔ site-db: 중복 흡수, µs 시각, NaN → NULL, reader raw/1m/1h(실시간 집계 D4)."""

from __future__ import annotations

import math
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core.historian.reader import read_aggregate, read_raw
from pureiron_core.historian.recorder import Recorder
from pureiron_core.historian.writer import HistoryWriter, PgRowStore
from pureiron_core.platform.db import Database
from pureiron_core.project.ports import Definitions, TagDef

from .conftest import SiteDb

pytestmark = [pytest.mark.integration, pytest.mark.fresh_db]

S = 1_000_000_000


@pytest.fixture
async def db(site_db: SiteDb) -> AsyncGenerator[Database]:
    database = Database(site_db.dsn, password=site_db.password, max_size=2)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
async def conn(site_db: SiteDb) -> AsyncGenerator[asyncpg.Connection[asyncpg.Record]]:
    connection = await asyncpg.connect(site_db.dsn, password=site_db.password)
    try:
        yield connection
    finally:
        await connection.close()


def _ns(at: datetime) -> int:
    return int(at.timestamp()) * S + at.microsecond * 1000


def _num(
    tag_id: int, at: datetime, value: float, quality: pb.Quality.ValueType = pb.GOOD
) -> pb.Sample:
    return pb.Sample(tag_id=tag_id, ts_unix_ns=_ns(at), num=value, quality=quality)


async def test_duplicates_absorbed_us_precision_and_nan_is_null(
    db: Database, conn: asyncpg.Connection[asyncpg.Record]
) -> None:
    writer = HistoryWriter(PgRowStore(db))
    recorder = Recorder(writer)
    recorder.set_definitions(
        Definitions(rev=1, tags=(TagDef(1, "a/num", "plc", {}), TagDef(2, "a/flag", "plc", {})))
    )
    base = datetime.now(UTC).replace(microsecond=123456) - timedelta(minutes=5)
    samples = [
        _num(1, base, 1.5),
        _num(1, base + timedelta(seconds=1), math.nan),
        pb.Sample(tag_id=2, ts_unix_ns=_ns(base), flag=True, quality=pb.GOOD),
        pb.Sample(tag_id=1, ts_unix_ns=_ns(base + timedelta(seconds=2)), quality=pb.BAD),
    ]
    recorder.offer_live(samples, plan_rev=1)
    assert await writer.flush() == 4
    # 같은 샘플이 replay로 다시 와도(Ack 전 끊김) 행은 늘지 않는다 — 같은 flush 안 중복도 흡수.
    recorder.offer_raw([*samples, *samples], plan_rev=1)
    assert await writer.flush() == 8
    assert writer.status().written_total == 12
    assert await conn.fetchval("SELECT count(*) FROM tag_history") == 4

    points = await read_raw(conn, [1, 2], base - timedelta(seconds=1), base + timedelta(seconds=5))
    assert [(p.tag_id, p.value_num, p.value_bool, p.quality) for p in points] == [
        (1, 1.5, None, pb.GOOD),
        (1, None, None, pb.GOOD),
        (1, None, None, pb.BAD),
        (2, None, True, pb.GOOD),
    ]
    assert points[0].time == base  # µs 정밀도 그대로
    assert await conn.fetchval("SELECT count(*) FROM tag_history WHERE value_num = 'NaN'") == 0


async def test_aggregates_include_recent_rows_without_refresh(
    db: Database, conn: asyncpg.Connection[asyncpg.Record]
) -> None:
    """0009 실시간 집계: refresh policy가 돌기 전에도 최근 구간이 1m·1h에 보인다(D4)."""
    assert await conn.fetchval(
        "SELECT bool_and(NOT materialized_only) FROM timescaledb_information.continuous_aggregates"
        " WHERE view_name IN ('tag_history_1m', 'tag_history_1h')"
    )
    writer = HistoryWriter(PgRowStore(db))
    recorder = Recorder(writer)
    minute = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)
    rows = [
        _num(1, minute + timedelta(seconds=s), v)
        for s, v in [(0, 1.0), (10, 3.0), (20, math.inf), (30, 5.0)]
    ]
    recorder.offer_raw(rows, plan_rev=0)
    await writer.flush()

    [m] = await read_aggregate(conn, [1], minute, minute + timedelta(minutes=1), "1m")
    assert (m.bucket, m.avg, m.min, m.max, m.last, m.samples) == (minute, 3.0, 1.0, 5.0, 5.0, 4)
    hour = minute.replace(minute=0)
    [h] = await read_aggregate(conn, [1], hour, hour + timedelta(hours=1), "1h")
    assert (h.bucket, h.avg, h.min, h.max, h.samples) == (hour, 3.0, 1.0, 5.0, 4)
