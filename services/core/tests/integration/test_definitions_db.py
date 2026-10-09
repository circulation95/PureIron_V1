"""apply-definitions: path 기준 upsert·retire, 같은 path는 같은 id, 변경 있을 때만 rev+1."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path

import asyncpg
import pytest

from pureiron_core.platform.db import Database
from pureiron_core.project.definitions import PgDefinitionsSource, apply_definitions
from pureiron_core.project.ports import DefinitionsUnavailableError
from pureiron_project.project import load_project

from .conftest import (
    JsonObject,
    SiteDb,
    Stack,
    core_cli,
    holding_tag,
    modbus_device,
    write_project,
)

pytestmark = [pytest.mark.integration, pytest.mark.fresh_db]


@pytest.fixture
async def conn(site_db: SiteDb) -> AsyncGenerator[asyncpg.Connection[asyncpg.Record]]:
    connection = await asyncpg.connect(site_db.dsn, password=site_db.password)
    try:
        yield connection
    finally:
        await connection.close()


def _tags(device_id: str, count: int) -> list[JsonObject]:
    return [holding_tag(f"{device_id}/r{i:03d}", device_id, i) for i in range(count)]


async def _apply(
    conn: asyncpg.Connection[asyncpg.Record],
    root: Path,
    devices: dict[str, JsonObject],
    tags: dict[str, list[JsonObject]],
) -> tuple[int, bool]:
    project = load_project(write_project(root, devices, tags))
    assert project.ok, project.problems
    result = await apply_definitions(conn, project)
    return result.rev, result.changed


async def _ids(conn: asyncpg.Connection[asyncpg.Record]) -> dict[str, tuple[int, bool]]:
    rows = await conn.fetch("SELECT path, id, retired_at IS NOT NULL AS retired FROM tags")
    return {str(r["path"]): (int(r["id"]), bool(r["retired"])) for r in rows}


async def test_apply_upsert_retire_and_revive_keeps_ids(
    conn: asyncpg.Connection[asyncpg.Record], site_db: SiteDb, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    plc = {"plc-1": modbus_device("plc-1", "10.0.0.5")}

    project = load_project(write_project(root, plc, {"plc-1": _tags("plc-1", 3)}))
    first = await apply_definitions(conn, project)
    assert (first.rev, first.changed, first.devices_added, first.tags_added) == (1, True, 1, 3)
    ids = await _ids(conn)
    definition = await conn.fetchval("SELECT definition FROM tags WHERE path = 'plc-1/r001'")
    assert json.loads(definition) == holding_tag("plc-1/r001", "plc-1", 1)

    # 같은 내용 재적용 → 변화 없음, rev 그대로
    assert await _apply(conn, root, plc, {"plc-1": _tags("plc-1", 3)}) == (1, False)

    # 태그 하나 삭제 → retired, rev+1
    assert await _apply(conn, root, plc, {"plc-1": _tags("plc-1", 2)}) == (2, True)
    assert (await _ids(conn))["plc-1/r002"] == (ids["plc-1/r002"][0], True)

    # 다시 추가 → 같은 id로 되살아남
    assert await _apply(conn, root, plc, {"plc-1": _tags("plc-1", 3)}) == (3, True)
    assert await _ids(conn) == ids

    # 장비 정의 변경 → rev+1, 새 태그는 새 id(기존 id 재사용 없음)
    moved = {"plc-1": modbus_device("plc-1", "10.0.0.6")}
    assert await _apply(conn, root, moved, {"plc-1": _tags("plc-1", 4)}) == (4, True)
    after = await _ids(conn)
    assert after["plc-1/r003"][0] > max(i for i, _ in ids.values())

    # 장비째 삭제 → 장비·태그 retire, 활성 정의 없음
    db = Database(site_db.dsn, password=site_db.password, max_size=1)
    try:
        source = PgDefinitionsSource(db)
        loaded = await source.load()
        assert loaded.rev == 4
        assert [d.device_id for d in loaded.devices] == ["plc-1"]
        assert loaded.devices[0].definition["host"] == "10.0.0.6"
        assert len(loaded.tags) == 4

        other = {"plc-2": modbus_device("plc-2", "10.0.0.7")}
        assert await _apply(conn, root, other, {"plc-2": _tags("plc-2", 1)}) == (5, True)
        retired = await conn.fetchval(
            "SELECT retired_at IS NOT NULL FROM devices WHERE id = 'plc-1'"
        )
        assert retired is True
        loaded = await source.load()
        assert [d.device_id for d in loaded.devices] == ["plc-2"]
        assert [t.path for t in loaded.tags] == ["plc-2/r000"]
        assert await source.rev() == 5
    finally:
        await db.close()


async def test_historian_settings_stored_with_rev(
    conn: asyncpg.Connection[asyncpg.Record], site_db: SiteDb, tmp_path: Path
) -> None:
    """project.yaml `historian`은 project_state.historian에 저장되고 바뀔 때만 rev+1(D1)."""
    root = tmp_path / "project"
    plc = {"plc-1": modbus_device("plc-1", "10.0.0.5")}
    tags = {"plc-1": _tags("plc-1", 1)}

    def tree(historian: JsonObject | None) -> Path:
        return write_project(root, plc, tags, historian=historian)

    first = await apply_definitions(conn, load_project(tree(None)))
    assert (first.rev, first.historian_changed) == (1, False)
    assert await conn.fetchval("SELECT historian FROM project_state") is None

    settings: JsonObject = {"default_deadband": 0.5, "default_max_interval_s": 30}
    second = await apply_definitions(conn, load_project(tree(settings)))
    assert (second.rev, second.changed, second.historian_changed) == (2, True, True)
    same = await apply_definitions(conn, load_project(tree(settings)))
    assert (same.rev, same.changed, same.historian_changed) == (2, False, False)
    # 스키마 기본값만 적은 설정은 정규형에서 빠진다 → 위 설정과 같은 값으로 저장된다.
    with_defaults: JsonObject = {**settings, "retention_days": 365}
    assert (await apply_definitions(conn, load_project(tree(with_defaults)))).changed is False

    db = Database(site_db.dsn, password=site_db.password, max_size=1)
    try:
        loaded = await PgDefinitionsSource(db).load()
        assert (loaded.rev, dict(loaded.historian)) == (2, settings)
        # 설정을 지우면(전부 기본값) NULL로 돌아가고 rev+1
        cleared = await apply_definitions(conn, load_project(tree({})))
        assert (cleared.rev, cleared.historian_changed) == (3, True)
        assert await conn.fetchval("SELECT historian FROM project_state") is None
        assert dict((await PgDefinitionsSource(db).load()).historian) == {}
    finally:
        await db.close()


async def test_rows_without_definition_are_not_planned(
    conn: asyncpg.Connection[asyncpg.Record], site_db: SiteDb
) -> None:
    """epoch 2 이전 방식으로 들어간 행(definition NULL, n-minus-1 seed 등)은 plan에서 빠진다."""
    await conn.execute("INSERT INTO devices (id, protocol) VALUES ('n1-dev', 'modbus_tcp')")
    await conn.execute("INSERT INTO tags (path, device_id) VALUES ('n1/tag', 'n1-dev')")
    db = Database(site_db.dsn, password=site_db.password, max_size=1)
    try:
        loaded = await PgDefinitionsSource(db).load()
        assert (loaded.rev, loaded.devices, loaded.tags) == (0, (), ())
    finally:
        await db.close()


async def test_source_reports_unavailable_db() -> None:
    db = Database("postgresql://pureiron@127.0.0.1:1/none", connect_timeout_s=1)
    try:
        with pytest.raises(DefinitionsUnavailableError):
            await PgDefinitionsSource(db).rev()
    finally:
        await db.close()


def test_cli_apply_definitions(stack: Stack, fresh_database: str, tmp_path: Path) -> None:
    env = stack.cli_env(fresh_database)
    assert core_cli(env, "migrate", "up").returncode == 0
    root = write_project(
        tmp_path / "project",
        {"plc-1": modbus_device("plc-1", "10.0.0.5")},
        {"plc-1": _tags("plc-1", 2)},
    )
    applied = core_cli(env, "apply-definitions", str(root), "--json")
    assert applied.returncode == 0, applied.stderr
    result = json.loads(applied.stdout)
    assert (result["rev"], result["tags_added"], result["changed"]) == (1, 2, True)
    again = core_cli(env, "apply-definitions", str(root))
    assert again.returncode == 0, again.stderr
    assert "rev=1 changed=False" in again.stdout

    (root / "tags" / "ghost.yaml").write_text("[]", encoding="utf-8")  # 장비 없는 태그 파일
    bad = core_cli(env, "apply-definitions", str(root))
    assert bad.returncode == 4
    assert "ghost" in bad.stderr
