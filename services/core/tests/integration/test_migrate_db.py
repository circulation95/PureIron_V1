"""migrate CLI·baseline 스키마를 실제 TimescaleDB에서 검증한다(US-004, plan §6.6)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from datetime import timedelta
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
    assert "applied 8" in first.stdout
    second = _cli(env, "migrate", "up")
    assert second.returncode == 0, second.stderr
    assert "applied 0" in second.stdout

    status = _cli(env, "migrate", "status", "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout) == {
        "cur": 3,
        "app_expected": 3,
        "app_max_compatible": 4,
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


def _copy_with_extra(tmp_path: Path, files: dict[str, str], epoch_last: str | None) -> Path:
    """실제 카탈로그 사본에 파일을 더한다.

    `epoch_last`가 있으면 그 파일로 끝나는 새 epoch를 붙인다.
    """
    copy = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, copy)
    for name, sql in files.items():
        (copy / name).write_text(sql, encoding="utf-8")
    if epoch_last is not None:
        catalog = migrate.load_catalog(MIGRATIONS)
        epochs = (copy / "epochs.yaml").read_text(encoding="utf-8").rstrip("\n")
        (copy / "epochs.yaml").write_text(
            f"{epochs}\n  {catalog.app_expected + 1}: {epoch_last}\n", encoding="utf-8"
        )
    return copy


@pytest.mark.fresh_db
async def test_failing_file_stops_and_is_not_recorded(
    stack: Stack, fresh_database: str, tmp_path: Path
) -> None:
    catalog = migrate.load_catalog(MIGRATIONS)
    real = [m.version for m in catalog.migrations]
    bad, after = f"{real[-1] + 1:04d}_bad.sql", f"{real[-1] + 2:04d}_after.sql"
    copy = _copy_with_extra(
        tmp_path,
        {
            bad: "CREATE TABLE ok_table (x int);\nSELECT * FROM missing_table;\n",
            after: "CREATE TABLE never (x int);\n",
        },
        after,
    )
    result = _cli(stack.cli_env(fresh_database, copy), "migrate", "up")
    assert result.returncode == migrate.EXIT_FAILED
    assert bad in result.stderr

    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        versions = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
        assert [r["version"] for r in versions] == real
        assert await conn.fetchval("SELECT to_regclass('ok_table')") is None
        assert await conn.fetchval("SELECT to_regclass('never')") is None
        assert await migrate.read_epoch(conn) == catalog.app_expected
    finally:
        await conn.close()


@pytest.mark.fresh_db
async def test_up_to_epoch_and_envelope(stack: Stack, fresh_database: str, tmp_path: Path) -> None:
    catalog = migrate.load_catalog(MIGRATIONS)  # 현재 앱: envelope [E, E+1]
    current = catalog.app_expected
    extra = f"{catalog.migrations[-1].version + 1:04d}_extra.sql"
    copy = _copy_with_extra(tmp_path, {extra: "CREATE TABLE extra (x int);\n"}, extra)
    env = stack.cli_env(fresh_database, copy)
    assert _cli(env, "migrate", "up", "--to", "1").returncode == 0
    status = json.loads(_cli(env, "migrate", "status", "--json").stdout)
    epoch_one_last = catalog.last_version_of(1)
    assert status["cur"] == 1
    assert status["pending"] == [
        m.filename for m in catalog.migrations if m.version > epoch_one_last
    ] + [extra]
    assert _cli(env, "migrate", "up", "--to", str(current)).returncode == 0
    status = json.loads(_cli(env, "migrate", "status", "--json").stdout)
    assert (status["cur"], status["pending"]) == (current, [extra])

    assert _cli(env, "migrate", "up").returncode == 0
    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        assert await migrate.read_epoch(conn) == current + 1
        guard_status = await migrate.status(conn, catalog)
        assert guard_status.unknown_applied == [catalog.migrations[-1].version + 1]
        assert migrate.Envelope(current + 1, catalog.app_expected, catalog.app_max_compatible).ok
    finally:
        await conn.close()


SEED_TAGS = """
INSERT INTO devices (id, protocol) VALUES ('d1', 'modbus_tcp');
INSERT INTO tags (path, device_id) VALUES ('d1/a', 'd1'), ('d1/b', 'd1');
"""
SEED_1H = """
-- 값이 분마다 바뀐다(avg·min·max·last가 서로 다르게). 'd1/b'는 BAD 행(값 NULL)도 섞는다.
INSERT INTO tag_history (time, tag_id, value_num, quality)
SELECT ts, t.id,
       CASE WHEN t.path = 'd1/b' AND extract(minute FROM ts)::int % 7 = 0 THEN NULL
            ELSE (extract(epoch FROM ts)::bigint % 997)
                 * (CASE t.path WHEN 'd1/a' THEN 1 ELSE -1 END)
       END,
       CASE WHEN t.path = 'd1/b' AND extract(minute FROM ts)::int % 7 = 0 THEN 2 ELSE 1 END
  FROM tags t,
       generate_series(date_trunc('hour', now()) - $1::interval,
                       date_trunc('hour', now()) - $2::interval - interval '1 minute',
                       interval '1 minute') ts;
"""
RAW_1H = """
SELECT tag_id, time_bucket(INTERVAL '1 hour', time) AS bucket, avg(value_num) AS avg,
       min(value_num) AS min, max(value_num) AS max, last(value_num, time) AS last,
       count(*) AS samples
  FROM tag_history WHERE time >= date_trunc('hour', now()) - $1::interval
 GROUP BY 1, 2
"""
MISMATCH_1H = f"""
SELECT count(*) FROM ({RAW_1H}) r
  FULL JOIN (SELECT * FROM tag_history_1h
              WHERE bucket >= date_trunc('hour', now()) - $1::interval) c
    USING (tag_id, bucket)
 WHERE c.bucket IS NULL OR r.bucket IS NULL
    OR r.samples IS DISTINCT FROM c.samples
    OR r.min IS DISTINCT FROM c.min OR r.max IS DISTINCT FROM c.max
    OR r.last IS DISTINCT FROM c.last
    OR abs(coalesce(r.avg, 0) - coalesce(c.avg, 0)) > 1e-9
    OR (r.avg IS NULL) <> (c.avg IS NULL)
"""
POLICY_JOB_1H = (
    "SELECT job_id FROM timescaledb_information.jobs "
    "WHERE proc_name = 'policy_refresh_continuous_aggregate' AND hypertable_name = 'tag_history_1h'"
)


async def _policy_ran(conn: asyncpg.Connection, job_id: int, timeout_s: float = 90) -> None:
    """refresh policy가 한 번 성공할 때까지 기다린다. 스케줄러는 policy를 만들자마자 돌리므로 대개
    그 실행을 기다리게 되고, 아직 시작 전이면 같은 job을 직접 실행한다(동시 실행이면 계속 대기)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        stats = await conn.fetchrow(
            "SELECT total_successes, total_failures "
            "FROM timescaledb_information.job_stats WHERE job_id = $1",
            job_id,
        )
        if stats is not None and stats["total_failures"]:
            raise AssertionError(f"refresh policy job {job_id} failed in the scheduler")
        if stats is not None and stats["total_successes"]:
            return
        try:
            await conn.execute(f"CALL run_job({job_id})")
        except asyncpg.LockNotAvailableError:
            await asyncio.sleep(0.5)
        else:
            return
    raise AssertionError(f"refresh policy job {job_id} did not succeed in {timeout_s}s")


@pytest.mark.fresh_db
async def test_epoch2_on_compressed_epoch1_db(stack: Stack, fresh_database: str) -> None:
    """r0→r1: 압축 chunk가 있는 epoch 1 DB에 epoch 2를 올리고 **refresh policy job**이 채운 1시간
    CAGG를 원본 직접 집계와 bucket마다 대조한다. policy 범위(3일)보다 오래된 이력은 채우지 않는다 —
    업그레이드 이전 이력 backfill은 M3a 계약(data-contracts)."""
    env = stack.cli_env(fresh_database)
    assert _cli(env, "migrate", "up", "--to", "1").returncode == 0
    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        await conn.execute(SEED_TAGS)
        await conn.execute(SEED_1H.replace("$1", "'60 hours'").replace("$2", "'3 hours'"))
        await conn.execute(SEED_1H.replace("$1", "'6 days'").replace("$2", "'5 days'"))
        # 끝난 chunk는 모두 압축(현재 chunk만 남음) — policy 범위 안에도 압축 chunk가 들어오게.
        compressed = await conn.fetch(
            "SELECT compress_chunk(c) FROM show_chunks('tag_history', older_than => now()) c"
        )
        assert compressed, "fixture must leave compressed chunks"
    finally:
        await conn.close()

    up = _cli(env, "migrate", "up")
    assert up.returncode == 0, up.stderr

    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        assert await migrate.read_epoch(conn) == migrate.load_catalog(MIGRATIONS).app_expected
        recent_compressed = await conn.fetchval(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'tag_history' AND is_compressed "
            "AND range_end > now() - interval '3 days'"
        )
        assert recent_compressed, "policy window must cover a compressed chunk"
        await _policy_ran(conn, int(await conn.fetchval(POLICY_JOB_1H)))
        assert await conn.fetchval(MISMATCH_1H, timedelta(hours=60)) == 0
        assert await conn.fetchval("SELECT count(*) FROM tag_history_1h") == 2 * 57
        old = await conn.fetchval(
            "SELECT count(*) FROM tag_history_1h WHERE bucket < now() - interval '4 days'"
        )
        assert old == 0, "policy must not backfill beyond its window (backfill is M3a)"
    finally:
        await conn.close()


@pytest.mark.fresh_db
@pytest.mark.parametrize("recorded", [False, True], ids=["ran-not-recorded", "0006-recorded"])
async def test_epoch2_resumes_after_crash_between_files(
    stack: Stack, fresh_database: str, recorded: bool
) -> None:
    """epoch 2의 두 파일 사이에서 끊긴 상태에서 `up` → 수렴.

    - `ran-not-recorded`: 0006 구문은 실행됐지만 기록 전에 죽음 → 재실행은 IF NOT EXISTS로 지나간다.
    - `0006-recorded`: 0006까지 기록되고 0007 전에 죽음 → epoch는 1에 머문다(r0 앱 ready).
    """
    catalog = migrate.load_catalog(MIGRATIONS)
    cagg = next(m for m in catalog.migrations if m.filename == "0006_cagg_1h.sql")
    env = stack.cli_env(fresh_database)
    assert _cli(env, "migrate", "up", "--to", "1").returncode == 0
    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        await conn.execute(cagg.sql)
        if recorded:
            await conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, release) "
                "VALUES ($1, $2, $3, 'test')",
                cagg.version,
                cagg.name,
                cagg.checksum,
            )
        status = json.loads(_cli(env, "migrate", "status", "--json").stdout)
        pending = ["0007_cagg_1h_policy.sql"]
        assert (status["cur"], status["pending"]) == (
            1,
            pending if recorded else ["0006_cagg_1h.sql", *pending],
        )
    finally:
        await conn.close()

    up = _cli(env, "migrate", "up", "--to", "2")
    assert up.returncode == 0, up.stderr
    assert f"applied {1 if recorded else 2}" in up.stdout
    conn = await asyncpg.connect(stack.dsn(fresh_database), password=stack.password)
    try:
        assert await migrate.read_epoch(conn) == 2
        assert await conn.fetchval(CAGG_COUNT_SQL, "tag_history_1h") == 1
        assert await conn.fetchval(REFRESH_POLICY_COUNT_SQL, "tag_history_1h") == 1
    finally:
        await conn.close()
