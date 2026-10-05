"""migrate CLI·baseline 스키마를 실제 TimescaleDB에서 검증한다(US-004, plan §6.6)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import asyncpg
import pytest

from pureiron_core.platform import migrate

from .conftest import MIGRATIONS, SiteDb, Stack

pytestmark = pytest.mark.integration

CAGG_COUNT_SQL = (
    "SELECT count(*) FROM timescaledb_information.continuous_aggregates WHERE view_name = $1"
)
# CAGG refresh policy의 jobs.hypertable_name은 CAGG(view) 이름이다.
REFRESH_POLICY_COUNT_SQL = (
    "SELECT count(*) FROM timescaledb_information.jobs "
    "WHERE proc_name = 'policy_refresh_continuous_aggregate' AND hypertable_name = $1"
)


def _cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("pureiron-core")
    assert exe is not None
    return subprocess.run(
        [exe, *args], env=env, capture_output=True, text=True, encoding="utf-8", check=False
    )


@pytest.mark.fresh_db
def test_up_is_idempotent_and_status_reports_epoch(stack: Stack, fresh_database: str) -> None:
    env = stack.cli_env(fresh_database)
    first = _cli(env, "migrate", "up")
    assert first.returncode == 0, first.stderr
    assert "applied 7" in first.stdout
    second = _cli(env, "migrate", "up")
    assert second.returncode == 0, second.stderr
    assert "applied 0" in second.stdout

    status = _cli(env, "migrate", "status", "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout) == {
        "cur": 2,
        "app_expected": 2,
        "app_max_compatible": 3,
        "pending": [],
        "checksum_ok": True,
        "mismatched": [],
        "unknown_applied": [],
    }
    assert _cli(env, "migrate", "verify").returncode == 0


async def test_baseline_timescale_objects(site_db: SiteDb) -> None:
    conn = await asyncpg.connect(site_db.dsn, password=site_db.password)
    try:
        interval = await conn.fetchval(
            "SELECT time_interval FROM timescaledb_information.dimensions "
            "WHERE hypertable_name = 'tag_history' AND column_name = 'time'"
        )
        assert str(interval) == "1 day, 0:00:00"
        assert await conn.fetchval(
            "SELECT compression_enabled FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'tag_history'"
        )
        compress_after = await conn.fetchval(
            "SELECT config->>'compress_after' FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_compression' AND hypertable_name = 'tag_history'"
        )
        assert compress_after == "2 days"
        unique_cols = await conn.fetchval(
            """
            SELECT array_agg(a.attname ORDER BY a.attname)
              FROM pg_constraint c
              JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
             WHERE c.conrelid = 'tag_history'::regclass AND c.contype = 'u'
            """
        )
        assert unique_cols == ["tag_id", "time"]
        # 개수는 CAGG별로 센다 — 전역 개수 단정은 다음 epoch가 CAGG·policy를 더하면 N-1 실행에서
        # 깨진다(r0 core-test가 r1 스키마에서 실제로 그랬다).
        for view in ("tag_history_1m", "tag_history_1h"):
            assert await conn.fetchval(CAGG_COUNT_SQL, view) == 1, view
            assert await conn.fetchval(REFRESH_POLICY_COUNT_SQL, view) == 1, view
        assert await conn.fetchval("SELECT count(*) FROM project_state") == 1
        epoch = await migrate.read_epoch(conn)
        assert epoch is not None
        assert epoch >= 2
    finally:
        await conn.close()


@pytest.mark.fresh_db
async def test_cagg_is_created_without_data(site_db: SiteDb) -> None:
    """1분·1시간 CAGG 모두 WITH NO DATA: 채우기는 refresh policy 몫.

    새 DB에서만 성립(n-minus-1 DB는 시드·refresh됨).
    """
    conn = await asyncpg.connect(site_db.dsn, password=site_db.password)
    try:
        for view in ("tag_history_1m", "tag_history_1h"):
            assert await conn.fetchval(f"SELECT count(*) FROM {view}") == 0, view
    finally:
        await conn.close()


@pytest.mark.fresh_db
def test_checksum_tamper_exits_3(stack: Stack, fresh_database: str, tmp_path: Path) -> None:
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, copy)
    assert _cli(stack.cli_env(fresh_database, copy), "migrate", "up").returncode == 0
    target = copy / "0002_tags_devices.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    env = stack.cli_env(fresh_database, copy)
    verify = _cli(env, "migrate", "verify")
    assert verify.returncode == migrate.EXIT_CHECKSUM, verify.stderr
    assert "0002_tags_devices.sql" in verify.stderr
    assert _cli(env, "migrate", "up").returncode == migrate.EXIT_CHECKSUM
    status = json.loads(_cli(env, "migrate", "status", "--json").stdout)
    assert status["checksum_ok"] is False


@pytest.mark.fresh_db
async def test_lock_held_exits_2(stack: Stack, fresh_database: str) -> None:
    holder = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        assert await holder.fetchval(migrate.LOCK_SQL) is True
        result = _cli(stack.cli_env(fresh_database), "migrate", "up", "--lock-timeout", "1s")
        assert result.returncode == migrate.EXIT_LOCK, result.stderr
    finally:
        await holder.close()


@pytest.mark.fresh_db
async def test_failing_file_stops_and_is_not_recorded(
    stack: Stack, fresh_database: str, tmp_path: Path
) -> None:
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, copy)
    (copy / "0008_bad.sql").write_text(
        "CREATE TABLE ok_table (x int);\nSELECT * FROM missing_table;\n"
    )
    (copy / "0009_after.sql").write_text("CREATE TABLE never (x int);\n")
    (copy / "epochs.yaml").write_text(
        "epochs:\n  1: 0005_project_state.sql\n  2: 0007_cagg_1h_policy.sql\n  3: 0009_after.sql\n",
        encoding="utf-8",
    )
    result = _cli(stack.cli_env(fresh_database, copy), "migrate", "up")
    assert result.returncode == migrate.EXIT_FAILED
    assert "0008_bad.sql" in result.stderr

    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        versions = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
        assert [r["version"] for r in versions] == [1, 2, 3, 4, 5, 6, 7]
        assert await conn.fetchval("SELECT to_regclass('ok_table')") is None
        assert await conn.fetchval("SELECT to_regclass('never')") is None
        assert await migrate.read_epoch(conn) == 2
    finally:
        await conn.close()


@pytest.mark.fresh_db
async def test_up_to_epoch_and_envelope(stack: Stack, fresh_database: str, tmp_path: Path) -> None:
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, copy)
    (copy / "0008_extra.sql").write_text("CREATE TABLE extra (x int);\n", encoding="utf-8")
    (copy / "epochs.yaml").write_text(
        "epochs:\n  1: 0005_project_state.sql\n  2: 0007_cagg_1h_policy.sql\n  3: 0008_extra.sql\n",
        encoding="utf-8",
    )
    env = stack.cli_env(fresh_database, copy)
    assert _cli(env, "migrate", "up", "--to", "1").returncode == 0
    status = json.loads(_cli(env, "migrate", "status", "--json").stdout)
    assert (status["cur"], status["pending"]) == (
        1,
        ["0006_cagg_1h.sql", "0007_cagg_1h_policy.sql", "0008_extra.sql"],
    )
    assert _cli(env, "migrate", "up", "--to", "2").returncode == 0
    status = json.loads(_cli(env, "migrate", "status", "--json").stdout)
    assert (status["cur"], status["pending"]) == (2, ["0008_extra.sql"])

    assert _cli(env, "migrate", "up").returncode == 0
    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        assert await migrate.read_epoch(conn) == 3
        old_app = migrate.load_catalog(MIGRATIONS)  # 현재(epoch 2) 앱: envelope [2, 3]
        guard_status = await migrate.status(conn, old_app)
        assert guard_status.unknown_applied == [8]
        assert migrate.Envelope(3, old_app.app_expected, old_app.app_max_compatible).ok
    finally:
        await conn.close()


@pytest.mark.fresh_db
async def test_epoch2_on_compressed_epoch1_db(stack: Stack, fresh_database: str) -> None:
    """r0→r1 경로: 압축 chunk가 있는 epoch 1 DB에 epoch 2(1시간 CAGG)를 올리고 집계까지 확인한다."""
    env = stack.cli_env(fresh_database)
    assert _cli(env, "migrate", "up", "--to", "1").returncode == 0
    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        await conn.execute(
            """
            INSERT INTO devices (id, protocol) VALUES ('d1', 'modbus_tcp');
            INSERT INTO tags (path, device_id) VALUES ('d1/t', 'd1');
            INSERT INTO tag_history (time, tag_id, value_num, quality)
            SELECT ts, (SELECT id FROM tags WHERE path = 'd1/t'), 1.0, 1
              FROM generate_series(
                  date_trunc('hour', now()) - interval '4 days',
                  date_trunc('hour', now()) - interval '3 days' - interval '1 minute',
                  interval '1 minute'
              ) ts;
            """
        )
        compressed = await conn.fetch(
            "SELECT compress_chunk(c) "
            "FROM show_chunks('tag_history', older_than => interval '2 days') c"
        )
        assert compressed, "fixture must leave at least one compressed chunk"
    finally:
        await conn.close()

    up = _cli(env, "migrate", "up")
    assert up.returncode == 0, up.stderr
    assert "applied 2" in up.stdout

    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        assert await migrate.read_epoch(conn) == 2
        await conn.execute("CALL refresh_continuous_aggregate('tag_history_1h', NULL, NULL)")
        assert await conn.fetchval("SELECT count(*) FROM tag_history_1h") == 24
        assert await conn.fetchval("SELECT sum(samples) FROM tag_history_1h") == 24 * 60
        assert await conn.fetchval("SELECT count(*) FROM tag_history") == 24 * 60
    finally:
        await conn.close()
