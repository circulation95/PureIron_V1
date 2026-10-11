"""auth·audit 저장소와 `pureiron-core users` CLI를 실제 TimescaleDB에서 검증한다(M3b-1).

- 세션 행에는 토큰 sha256만 남고, 로그아웃·비활성화가 즉시 반영되며, last_seen 갱신·끝난 세션 정리가
  DB에 실제로 쓰인다.
- 감사 조회는 최신순·`[start, end)`·필터·limit, 트랜잭션 기록은 롤백되면 남지 않는다.
- CLI 계정 명령은 감사 행(source=cli, 행위자 없음)을 남기고 비밀번호를 어디에도 남기지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditQuery, AuditSource
from pureiron_core.audit.store import PgAuditStore
from pureiron_core.auth.ports import (
    InvalidCredentialsError,
    Role,
    SessionInvalidError,
    UserExistsError,
    UserNotFoundError,
)
from pureiron_core.auth.service import AuthService
from pureiron_core.auth.sessions import PgSessionStore
from pureiron_core.auth.users import PgUserStore
from pureiron_core.platform.db import Database

from .conftest import SiteDb, Stack, core_cli

pytestmark = pytest.mark.integration

PASSWORD = "first-pass-1"
T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class _RollbackError(Exception):
    pass


async def _record_then_roll_back(conn: asyncpg.Connection, store: PgAuditStore) -> None:
    async with conn.transaction():
        await store.record_with(
            conn, AuditEntry(action=AuditAction.AUTH_LOGIN, source=AuditSource.API)
        )
        raise _RollbackError


@pytest.fixture
async def db(site_db: SiteDb) -> AsyncGenerator[Database]:
    database = Database(site_db.dsn, password=site_db.password, max_size=2)
    try:
        yield database
    finally:
        await database.close()


async def _fetch(site_db: SiteDb, sql: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(site_db.dsn, password=site_db.password)
    try:
        return await conn.fetch(sql)
    finally:
        await conn.close()


@pytest.mark.fresh_db
async def test_sessions_store_only_digest_and_revoke_immediately(
    site_db: SiteDb, db: Database
) -> None:
    clock = Clock()
    service = AuthService(PgUserStore(db), PgSessionStore(db), clock=clock)
    user = await service.add_user("alice", PASSWORD, Role.ENGINEER, display_name="Alice")
    assert (user.username, user.role, user.display_name, user.enabled) == (
        "alice",
        Role.ENGINEER,
        "Alice",
        True,
    )
    with pytest.raises(UserExistsError):
        await service.add_user("alice", PASSWORD, Role.VIEWER)
    with pytest.raises(InvalidCredentialsError):
        await service.login("alice", "wrong-pass-1")

    login = await service.login("alice", PASSWORD)
    assert (await service.authenticate(login.token)).role is Role.ENGINEER
    rows = await _fetch(site_db, "SELECT token_sha256, last_seen_at, expires_at FROM sessions")
    assert [bytes(r["token_sha256"]) for r in rows] == [
        hashlib.sha256(login.token.encode()).digest()
    ]
    assert (rows[0]["last_seen_at"], rows[0]["expires_at"]) == (T0, T0 + timedelta(days=7))
    stored_hash = (await _fetch(site_db, "SELECT password_hash FROM users"))[0]["password_hash"]
    assert stored_hash.startswith("scrypt$16384$8$1$")
    assert PASSWORD not in stored_hash

    clock.now = T0 + timedelta(seconds=61)
    await service.authenticate(login.token)
    rows = await _fetch(site_db, "SELECT last_seen_at FROM sessions")
    assert rows[0]["last_seen_at"] == T0 + timedelta(seconds=61)

    await service.logout(login.token)
    with pytest.raises(SessionInvalidError, match="revoked"):
        await service.authenticate(login.token)

    second = await service.login("alice", PASSWORD)
    await service.set_enabled("alice", False)
    with pytest.raises(SessionInvalidError):
        await service.authenticate(second.token)
    with pytest.raises(UserNotFoundError):
        await service.set_enabled("nobody", False)


@pytest.mark.fresh_db
async def test_login_purges_finished_sessions_of_that_user(site_db: SiteDb, db: Database) -> None:
    clock = Clock()
    service = AuthService(PgUserStore(db), PgSessionStore(db), absolute_s=3600, clock=clock)
    await service.add_user("alice", PASSWORD, Role.VIEWER)
    await service.add_user("bob", PASSWORD, Role.VIEWER)
    expired = await service.login("alice", PASSWORD)
    revoked = await service.login("alice", PASSWORD)
    await service.login("bob", PASSWORD)
    await service.logout(revoked.token)
    clock.now = T0 + timedelta(hours=2)
    fresh = await service.login("alice", PASSWORD)
    rows = await _fetch(
        site_db,
        "SELECT u.username, s.token_sha256 FROM sessions s JOIN users u USING (user_id) "
        "ORDER BY u.username",
    )
    alice = [bytes(r["token_sha256"]) for r in rows if r["username"] == "alice"]
    assert alice == [hashlib.sha256(fresh.token.encode()).digest()]
    assert [r["username"] for r in rows].count("bob") == 1  # 다른 사용자 행은 건드리지 않는다
    assert expired.token != fresh.token


@pytest.mark.fresh_db
async def test_audit_query_filters_order_and_limit(site_db: SiteDb, db: Database) -> None:
    store = PgAuditStore(db)
    conn = await asyncpg.connect(site_db.dsn, password=site_db.password)
    try:
        await conn.execute(
            """
            INSERT INTO audit_log (time, username, action, target, detail, source) VALUES
              ('2026-10-11T09:00:00Z', 'alice', 'auth.login', 'alice', '{}', 'api'),
              ('2026-10-11T10:00:00Z', 'bob', 'auth.login_failed', 'bob',
               '{"reason": "bad_password"}', 'api'),
              ('2026-10-11T11:00:00Z', 'alice', 'auth.logout', 'alice', '{}', 'api'),
              ('2026-10-11T12:00:00Z', NULL, 'users.add', 'carol', '{"role": "viewer"}', 'cli')
            """
        )
        # 트랜잭션 기록은 롤백되면 남지 않는다(M3b-2 ACK가 기대는 성질).
        with pytest.raises(_RollbackError):
            await _record_then_roll_back(conn, store)
    finally:
        await conn.close()

    everything = await store.query(AuditQuery())
    assert [r.action for r in everything] == [
        "users.add",
        "auth.logout",
        "auth.login_failed",
        "auth.login",
    ]
    assert everything[0].username is None
    assert everything[0].detail == {"role": "viewer"}
    assert [r.action for r in await store.query(AuditQuery(username="alice"))] == [
        "auth.logout",
        "auth.login",
    ]
    failed = await store.query(AuditQuery(action="auth.login_failed"))
    assert [(r.username, r.detail) for r in failed] == [("bob", {"reason": "bad_password"})]
    window = await store.query(
        AuditQuery(
            start=datetime(2026, 10, 11, 10, tzinfo=UTC), end=datetime(2026, 10, 11, 12, tzinfo=UTC)
        )
    )
    assert [r.action for r in window] == ["auth.logout", "auth.login_failed"]
    assert [r.action for r in await store.query(AuditQuery(limit=1))] == ["users.add"]

    await store.record(
        AuditEntry(
            action=AuditAction.PROJECT_EDIT_LOCK_ACQUIRE,
            source=AuditSource.UPDATER,
            target="updater",
            detail={"generation": 3, "nested": {"ok": True}},
        )
    )
    latest = (await store.query(AuditQuery(limit=1)))[0]
    assert (latest.action, latest.source, latest.username, latest.target) == (
        "project.edit_lock_acquire",
        "updater",
        None,
        "updater",
    )
    assert latest.detail == {"generation": 3, "nested": {"ok": True}}


async def test_audit_record_failure_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    unreachable = Database("postgresql://pureiron@127.0.0.1:1/none", connect_timeout_s=1)
    try:
        with caplog.at_level(logging.WARNING):
            await PgAuditStore(unreachable).record(
                AuditEntry(action=AuditAction.AUTH_LOGIN, source=AuditSource.API, username="a")
            )
    finally:
        await unreachable.close()
    assert "audit record dropped" in caplog.text


@pytest.mark.fresh_db
async def test_users_cli_round_trip_records_audit_without_passwords(
    stack: Stack, fresh_database: str, tmp_path: Path
) -> None:
    env = stack.cli_env(fresh_database)
    assert core_cli(env, "migrate", "up").returncode == 0
    password_file = tmp_path / "pw"
    password_file.write_text(f"{PASSWORD}\n", encoding="utf-8")

    add = core_cli(
        env,
        "users",
        "add",
        "op1",
        "--role",
        "operator",
        "--display-name",
        "Op One",
        "--password-file",
        str(password_file),
        "--json",
    )
    assert add.returncode == 0, add.stderr
    added = json.loads(add.stdout)
    assert {k: v for k, v in added.items() if k != "created_at"} == {
        "username": "op1",
        "role": "operator",
        "display_name": "Op One",
        "enabled": True,
    }
    duplicate = core_cli(
        env, "users", "add", "op1", "--role", "viewer", "--password-file", str(password_file)
    )
    assert (duplicate.returncode, duplicate.stderr.strip()) == (
        4,
        "error: user 'op1' already exists",
    )

    passwd = core_cli(env, "users", "passwd", "op1", stdin="second-pass-2\n")
    assert passwd.returncode == 0, passwd.stderr
    assert passwd.stdout.strip() == "users passwd: op1 role=operator enabled=true"
    assert core_cli(env, "users", "disable", "op1").returncode == 0
    assert core_cli(env, "users", "enable", "op1").returncode == 0
    missing = core_cli(env, "users", "disable", "ghost")
    assert (missing.returncode, missing.stderr.strip()) == (4, "error: user 'ghost' not found")
    listed = json.loads(core_cli(env, "users", "list", "--json").stdout)
    assert [(u["username"], u["role"], u["enabled"]) for u in listed] == [("op1", "operator", True)]
    table = core_cli(env, "users", "list").stdout
    assert table == "op1\toperator\tenabled\tOp One\n"

    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        rows = await conn.fetch(
            "SELECT username, action, target, detail::text AS detail, source FROM audit_log "
            "ORDER BY id"
        )
        password_hash = await conn.fetchval("SELECT password_hash FROM users")
    finally:
        await conn.close()
    assert [(r["username"], r["action"], r["target"], r["source"]) for r in rows] == [
        (None, "users.add", "op1", "cli"),
        (None, "users.passwd", "op1", "cli"),
        (None, "users.disable", "op1", "cli"),
        (None, "users.enable", "op1", "cli"),
    ]
    assert json.loads(rows[0]["detail"]) == {"role": "operator"}
    everything = " ".join(str(dict(r)) for r in rows)
    assert PASSWORD not in everything
    assert "second-pass-2" not in everything
    assert "second-pass-2" not in password_hash
