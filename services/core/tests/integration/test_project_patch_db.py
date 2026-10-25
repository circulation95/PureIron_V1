"""현장 PATCH 트랜잭션(M5-2 P2·P3)과 `project export --draft`(P5)를 실제 site-db에서.

PATCH 한 번 = definition + `site_rev`·`definitions_rev` += 1 + 감사 `project.patch`(같은 트랜잭션).
같은 값은 no-op, 잠금 중 `EditLockedError`, 없음·retire는 `DefinitionNotFoundError`, 감사 기록
실패는 전부 롤백. `--draft`는 PATCH 값이 실린 유효 트리 + `draft-meta.json`을 쓰고, 같은 상태를
두 번 export 하면 바이트가 같고, 적용된 트리가 없으면 실패한다.
"""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg
import pytest

from pureiron_core.audit.store import PgAuditStore
from pureiron_core.platform.db import Database
from pureiron_core.project.export import DRAFT_META_FILE, TREE_FILE, tree_tar
from pureiron_core.project.patch import PgProjectPatcher
from pureiron_core.project.ports import (
    DefinitionNotFoundError,
    EditLockedError,
    PatchResult,
    ProjectStateError,
)
from pureiron_project.canonical import dump
from pureiron_project.project import load_project

from .conftest import (
    SiteDb,
    Stack,
    above_alarm,
    core_cli,
    holding_tag,
    modbus_device,
    write_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.fresh_db]

type Conn = asyncpg.Connection[asyncpg.Record]
type Env = dict[str, str]

EXIT_FAILED = 4


@pytest.fixture
def env(stack: Stack, fresh_database: str) -> Env:
    return stack.cli_env(fresh_database)


@pytest.fixture
async def conn(site_db: SiteDb) -> AsyncGenerator[Conn]:
    connection = await asyncpg.connect(site_db.dsn, password=site_db.password)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
async def patcher(site_db: SiteDb) -> AsyncGenerator[PgProjectPatcher]:
    db = Database(site_db.dsn, password=site_db.password)
    try:
        yield PgProjectPatcher(db, PgAuditStore(db))
    finally:
        await db.close()


def _apply_v1(env: Env, tmp: Path) -> str:
    """알람 2개·태그 2개(r000에 eu·record) 트리를 `project apply --tar`로 적용, content_hash."""
    root = write_project(
        tmp / "v1",
        {"plc-1": modbus_device("plc-1", "10.0.0.5")},
        {
            "plc-1": [
                {**holding_tag("plc-1/r000", "plc-1", 0), "eu": "m", "record": {"deadband": 0.5}},
                holding_tag("plc-1/r001", "plc-1", 1),
            ]
        },
        alarms=[
            above_alarm("a-high", "plc-1/r000", 80, priority="high"),
            {"id": "a-low", "tag": "plc-1/r001", "type": "below", "setpoint": 5},
        ],
    )
    project = load_project(root)
    assert project.ok, project.problems
    tar = tmp / "v1.tar"
    tar.write_bytes(tree_tar({f.relpath: dump(f.data).encode() for f in project.files}))
    content_hash = project.content_hash()
    applied = core_cli(
        env,
        *("project", "apply", "--tar", str(tar), "--project-id", "it", "--version", "v1"),
        *("--content-hash", content_hash),
    )
    assert applied.returncode == 0, applied.stdout + applied.stderr
    return content_hash


async def _revs(conn: Conn) -> tuple[int, int, int]:
    row = await conn.fetchrow(
        "SELECT site_rev, base_site_rev, definitions_rev FROM project_state WHERE id"
    )
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2])


async def _definition(conn: Conn, table: str, key: str, ident: str) -> dict[str, object]:
    raw = await conn.fetchval(f"SELECT definition::text FROM {table} WHERE {key} = $1", ident)
    parsed: dict[str, object] = json.loads(str(raw))
    return parsed


async def _audit(conn: Conn) -> list[tuple[str, str, str, dict[str, object]]]:
    rows = await conn.fetch(
        "SELECT username, action, target, detail::text FROM audit_log"
        " WHERE action = 'project.patch' ORDER BY id"
    )
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in rows]


async def test_patch_writes_definition_revs_and_audit_together(
    conn: Conn, env: Env, patcher: PgProjectPatcher, tmp_path: Path
) -> None:
    _apply_v1(env, tmp_path)
    site_rev, base, defs_rev = await _revs(conn)
    assert (site_rev, base) == (0, 0)

    result = await patcher.patch_alarm(
        "a-high", {"setpoint": 85.5, "priority": None}, username="admin"
    )
    expected = {"id": "a-high", "tag": "plc-1/r000", "type": "above", "setpoint": 85.5}
    assert result == PatchResult(site_rev=1, definition=expected, changed=True)
    assert await _definition(conn, "alarms", "alarm_id", "a-high") == expected
    assert await _revs(conn) == (1, 0, defs_rev + 1)

    tag = await patcher.patch_tag(
        "plc-1/r000",
        {"eu": "cm", "record": {"deadband": None, "max_interval_s": 30}},
        username="admin",
    )
    assert tag.site_rev == 2
    assert await _definition(conn, "tags", "path", "plc-1/r000") == {
        "path": "plc-1/r000",
        "device": "plc-1",
        "modbus": {"register_type": "holding", "address": 0, "data_type": "uint16"},
        "eu": "cm",
        "record": {"max_interval_s": 30},
    }
    assert await _revs(conn) == (2, 0, defs_rev + 2)
    assert await _audit(conn) == [
        (
            "admin",
            "project.patch",
            "alarm:a-high",
            {
                "kind": "alarm",
                "id": "a-high",
                "before": {"priority": "high", "setpoint": 80},
                "after": {"priority": None, "setpoint": 85.5},
                "site_rev": 1,
            },
        ),
        (
            "admin",
            "project.patch",
            "tag:plc-1/r000",
            {
                "kind": "tag",
                "path": "plc-1/r000",
                "before": {"eu": "m", "record.deadband": 0.5, "record.max_interval_s": None},
                "after": {"eu": "cm", "record.deadband": None, "record.max_interval_s": 30},
                "site_rev": 2,
            },
        ),
    ]


async def test_same_value_patch_is_noop(
    conn: Conn, env: Env, patcher: PgProjectPatcher, tmp_path: Path
) -> None:
    _apply_v1(env, tmp_path)
    revs = await _revs(conn)
    before = await _definition(conn, "alarms", "alarm_id", "a-high")
    result = await patcher.patch_alarm(
        "a-high", {"setpoint": 80.0, "priority": "high", "deadband": 0}, username="admin"
    )
    assert (result.changed, result.site_rev) == (False, 0)
    assert await _revs(conn) == revs
    assert await _definition(conn, "alarms", "alarm_id", "a-high") == before
    assert await _audit(conn) == []


async def test_locked_missing_or_retired_target_changes_nothing(
    conn: Conn, env: Env, patcher: PgProjectPatcher, tmp_path: Path
) -> None:
    _apply_v1(env, tmp_path)
    await conn.execute("UPDATE alarms SET retired_at = now() WHERE alarm_id = 'a-low'")
    revs = await _revs(conn)
    with pytest.raises(DefinitionNotFoundError):
        await patcher.patch_alarm("a-low", {"setpoint": 1}, username="admin")
    with pytest.raises(DefinitionNotFoundError):
        await patcher.patch_alarm("nope", {"setpoint": 1}, username="admin")
    with pytest.raises(DefinitionNotFoundError):
        await patcher.patch_tag("plc-1/r999", {"eu": "m"}, username="admin")

    await conn.execute("UPDATE project_state SET edit_lock_owner = 'updater', edit_lock_gen = 3")
    with pytest.raises(EditLockedError):
        await patcher.patch_alarm("a-high", {"setpoint": 99}, username="admin")
    assert (await _definition(conn, "alarms", "alarm_id", "a-high"))["setpoint"] == 80
    assert await _revs(conn) == revs
    assert await _audit(conn) == []


async def test_audit_failure_rolls_back_definition_and_revs(
    conn: Conn, env: Env, patcher: PgProjectPatcher, tmp_path: Path
) -> None:
    _apply_v1(env, tmp_path)
    revs = await _revs(conn)
    await conn.execute(
        "CREATE FUNCTION it_fail() RETURNS trigger LANGUAGE plpgsql AS"
        " $$BEGIN RAISE EXCEPTION 'injected audit failure'; END$$"
    )
    await conn.execute(
        "CREATE TRIGGER it_fail BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION it_fail()"
    )
    with pytest.raises(ProjectStateError, match="injected audit failure"):
        await patcher.patch_alarm("a-high", {"setpoint": 99}, username="admin")
    assert (await _definition(conn, "alarms", "alarm_id", "a-high"))["setpoint"] == 80
    assert await _revs(conn) == revs


def _draft(env: Env, out: Path) -> tuple[dict[str, object], bytes]:
    result = core_cli(env, "project", "export", "--draft", "--out", str(out))
    assert result.returncode == 0, result.stdout + result.stderr
    meta: dict[str, object] = json.loads((out / DRAFT_META_FILE).read_text(encoding="utf-8"))
    return meta, (out / TREE_FILE).read_bytes()


def _member(data: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        handle = tar.extractfile(name)
        assert handle is not None
        return handle.read().decode()


async def test_draft_export_carries_patch_and_is_deterministic(
    conn: Conn, env: Env, patcher: PgProjectPatcher, tmp_path: Path
) -> None:
    empty = core_cli(env, "project", "export", "--draft", "--out", str(tmp_path / "none"))
    assert empty.returncode == EXIT_FAILED, empty.stdout
    assert "project_files is empty" in empty.stderr

    applied_hash = _apply_v1(env, tmp_path)
    meta0, _ = _draft(env, tmp_path / "d0")
    assert meta0 == {"site_rev": 0, "base_site_rev": 0, "content_hash": applied_hash}

    await patcher.patch_alarm("a-high", {"setpoint": 85.5}, username="admin")
    await patcher.patch_tag("plc-1/r001", {"eu": "bar"}, username="admin")
    meta, tar = _draft(env, tmp_path / "d1")
    assert (meta["site_rev"], meta["base_site_rev"]) == (2, 0)
    assert meta["content_hash"] != applied_hash
    assert "setpoint: 85.5" in _member(tar, "alarms/alarms.yaml")
    assert "eu: bar" in _member(tar, "tags/plc-1.yaml")

    again_meta, again_tar = _draft(env, tmp_path / "d2")
    assert (again_meta, again_tar) == (meta, tar)
    assert (tmp_path / "d1" / DRAFT_META_FILE).read_bytes() == (
        tmp_path / "d2" / DRAFT_META_FILE
    ).read_bytes()
