"""알람 저널 ↔ site-db: 쓰기·알람별 마지막 행 복원, ACK = 저널·감사 한 트랜잭션(감사 실패면 저널도
롤백), 조회 필터·최신순."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest

from pureiron_core.alarms.journal import JournalRow, PgAlarmStore
from pureiron_core.alarms.ports import AlarmUnavailableError, JournalQuery
from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSource
from pureiron_core.audit.store import PgAuditStore
from pureiron_core.platform.db import Connection, Database

from .conftest import SiteDb

pytestmark = [pytest.mark.integration, pytest.mark.fresh_db]

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


@pytest.fixture
async def db(site_db: SiteDb) -> AsyncGenerator[Database]:
    database = Database(site_db.dsn, password=site_db.password, max_size=2)
    try:
        yield database
    finally:
        await database.close()


def _row(alarm_id: str, to_state: str, seconds: float, username: str | None = None) -> JournalRow:
    return JournalRow(
        T0 + timedelta(seconds=seconds), alarm_id, f"plant/{alarm_id}", "X", to_state, 1.5, username
    )


async def _define(db: Database, *alarm_ids: str, retired: tuple[str, ...] = ()) -> None:
    async with db.connection() as conn:
        for alarm_id in alarm_ids:
            await conn.execute(
                "INSERT INTO alarms (alarm_id, tag_path, definition, retired_at)"
                " VALUES ($1, $2, '{}'::jsonb, CASE WHEN $3 THEN now() END)",
                alarm_id,
                f"plant/{alarm_id}",
                alarm_id in retired,
            )


class FailingAudit:
    """트랜잭션 안에서 실패하는 감사 기록(asyncpg 오류를 그대로 올린다)."""

    async def record_with(self, conn: Connection, entry: AuditEntry) -> None:
        await conn.execute("INSERT INTO audit_log_missing_table VALUES (1)")


async def test_append_and_last_state_per_alarm(db: Database) -> None:
    store = PgAlarmStore(db, PgAuditStore(db))
    await _define(db, "a", "b", "c", retired=("c",))
    await store.append(
        [
            _row("a", "ACTIVE_UNACKED", 0),
            _row("a", "CLEARED_UNACKED", 10),
            _row("b", "ACTIVE_UNACKED", 5),
            _row("c", "ACTIVE_ACKED", 1),
            _row("orphan", "ACTIVE_UNACKED", 1),  # 정의 행이 없는 알람은 복원 대상이 아니다
        ]
    )
    last = await store.last_states()
    assert {k: (v.to_state, v.time) for k, v in last.items()} == {
        "a": ("CLEARED_UNACKED", T0 + timedelta(seconds=10)),
        "b": ("ACTIVE_UNACKED", T0 + timedelta(seconds=5)),
        "c": ("ACTIVE_ACKED", T0 + timedelta(seconds=1)),  # retired 정의도 복원해 RETIRED 처리
    }
    assert (last["a"].value, last["a"].tag_path) == (1.5, "plant/a")


async def test_ack_writes_journal_and_audit_in_one_transaction(db: Database) -> None:
    store = PgAlarmStore(db, PgAuditStore(db))
    entry = AuditEntry(
        action=AuditAction.ALARM_ACK,
        source=AuditSource.API,
        username="kim",
        target="a",
        detail={"from_state": "ACTIVE_UNACKED", "to_state": "ACTIVE_ACKED"},
    )
    await store.append_acked([_row("a", "ACTIVE_ACKED", 3, "kim")], entry)
    async with db.connection() as conn:
        journal = await conn.fetch("SELECT to_state, username FROM alarm_journal")
        audit = await conn.fetch("SELECT action, username, target FROM audit_log")
    assert [tuple(r) for r in journal] == [("ACTIVE_ACKED", "kim")]
    assert [tuple(r) for r in audit] == [("alarm.ack", "kim", "a")]

    failing = PgAlarmStore(db, FailingAudit())
    with pytest.raises(AlarmUnavailableError):
        await failing.append_acked([_row("a", "NORMAL", 4, "kim")], entry)
    async with db.connection() as conn:
        assert await conn.fetchval("SELECT count(*) FROM alarm_journal") == 1  # 함께 롤백


async def test_query_filters_and_orders_newest_first(db: Database) -> None:
    store = PgAlarmStore(db, PgAuditStore(db))
    await store.append([_row("a", "S1", 0), _row("b", "S2", 1), _row("a", "S3", 2)])
    everything = await store.query(JournalQuery())
    assert [(r.alarm_id, r.to_state) for r in everything] == [("a", "S3"), ("b", "S2"), ("a", "S1")]
    only_a = await store.query(JournalQuery(alarm_id="a", start=T0 + timedelta(seconds=1)))
    assert [r.to_state for r in only_a] == ["S3"]
    window = await store.query(JournalQuery(end=T0 + timedelta(seconds=1), limit=5))
    assert [r.to_state for r in window] == ["S1"]


async def test_store_reports_unavailable_db() -> None:
    db = Database("postgresql://pureiron@127.0.0.1:1/none", connect_timeout_s=1)
    store = PgAlarmStore(db, PgAuditStore(db))
    try:
        with pytest.raises(AlarmUnavailableError):
            await store.last_states()
        with pytest.raises(AlarmUnavailableError):
            await store.append([_row("a", "S", 0)])
    finally:
        await db.close()
