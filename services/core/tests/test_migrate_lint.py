from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pureiron_core.cli import main
from pureiron_core.platform.migrate import EXIT_LINT, EXIT_OK
from pureiron_core.platform.migrate_lint import lint_directory

from .conftest import write_migrations

BASELINE = {
    "0001_base.sql": "CREATE TABLE t (id int PRIMARY KEY, v text);\nDROP TABLE IF EXISTS legacy;",
}


def _lint_one(tmp_path: Path, sql: str, name: str = "0002_change.sql") -> list[str]:
    files = {**BASELINE, name: sql}
    directory = write_migrations(tmp_path / "m", files, {1: "0001_base.sql", 2: name})
    return [issue.message for issue in lint_directory(directory).issues]


def test_real_migrations_pass(migrations_dir: Path) -> None:
    report = lint_directory(migrations_dir)
    assert report.issues == []


def test_baseline_epoch_is_exempt(tmp_path: Path) -> None:
    directory = write_migrations(tmp_path, BASELINE, {1: "0001_base.sql"})
    assert lint_directory(directory).issues == []


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("DROP TABLE t;", "DROP is not additive"),
        ("ALTER TABLE t DROP COLUMN v;", "DROP is not additive"),
        ("ALTER TABLE t RENAME COLUMN v TO w;", "RENAME"),
        ("ALTER TABLE t ALTER COLUMN v TYPE integer USING v::integer;", "ALTER ... TYPE"),
        ("ALTER TABLE t ALTER COLUMN v SET DATA TYPE varchar(10);", "ALTER ... TYPE"),
        ("TRUNCATE t;", "TRUNCATE"),
        ("ALTER TABLE t ADD CONSTRAINT v_ck CHECK (v <> '');", "ADD CONSTRAINT"),
        ("ALTER TABLE t ADD UNIQUE (v);", "ADD CONSTRAINT"),
        ("ALTER TABLE t ADD FOREIGN KEY (id) REFERENCES u (id);", "ADD CONSTRAINT"),
        ("ALTER TABLE t ADD COLUMN w int REFERENCES u (id);", "column constraint"),
        ("ALTER TABLE t ADD COLUMN w int NOT NULL;", "NOT NULL without DEFAULT"),
        ("CREATE UNIQUE INDEX t_v ON t (v);", "CREATE UNIQUE INDEX"),
        ("CREATE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f();", "TRIGGER"),
        ("CREATE OR REPLACE TRIGGER trg AFTER UPDATE ON t EXECUTE FUNCTION f();", "TRIGGER"),
        ("CALL refresh_continuous_aggregate('t_1m', NULL, NULL);", "REFRESH_CONTINUOUS_AGGREGATE"),
        (
            "CREATE MATERIALIZED VIEW t_1h WITH (timescaledb.continuous) AS "
            "SELECT time_bucket('1 hour', ts) b, count(*) FROM h GROUP BY b;",
            "WITH NO DATA",
        ),
    ],
)
def test_rejected_statements(tmp_path: Path, sql: str, fragment: str) -> None:
    messages = _lint_one(tmp_path, sql)
    assert any(fragment in m for m in messages), messages


@pytest.mark.parametrize(
    "sql",
    [
        "DO $$ BEGIN EXECUTE 'DROP TABLE tags CASCADE'; END $$;",
        "UPDATE t SET v = 'x';",
        "DELETE FROM t;",
        "COMMIT;",
        "BEGIN;",
        "CREATE OR REPLACE VIEW tv AS SELECT 1;",
        "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;",
        "SELECT drop_chunks('h', INTERVAL '1 day');",
        "SELECT add_retention_policy('h', INTERVAL '30 days');",
        "SELECT remove_compression_policy('h');",
        "SELECT decompress_chunk(c) FROM show_chunks('h') c;",
        "ALTER TABLE t ALTER COLUMN v SET NOT NULL;",
        "ALTER TABLE t ALTER COLUMN v DROP DEFAULT, ALTER COLUMN v SET DEFAULT 'x';",
        "INSERT INTO t (id) VALUES (1);",
        "SELECT add_compression_policy('h', INTERVAL '2 days'), drop_chunks('h', INTERVAL '1d');",
        "CREATE TABLE junk AS SELECT drop_chunks('h', INTERVAL '1 day');",
        "INSERT INTO t (id) SELECT 1 FROM drop_chunks('h', INTERVAL '1d') ON CONFLICT DO NOTHING;",
        "SELECT set_chunk_time_interval('h', INTERVAL '7 days');",
        "SELECT add_compression_policy('h', INTERVAL '2 days') FROM t;",
    ],
)
def test_statements_outside_allowlist_are_rejected(tmp_path: Path, sql: str) -> None:
    assert _lint_one(tmp_path, sql), sql


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE u (id int PRIMARY KEY, code text UNIQUE);",
        "INSERT INTO t (id) VALUES (1) ON CONFLICT DO NOTHING;",
        "SELECT create_hypertable('u', by_range('ts'));",
        "SELECT add_compression_policy('u', INTERVAL '2 days', if_not_exists => TRUE);",
        "ALTER TABLE u SET (timescaledb.compress, timescaledb.compress_segmentby = 'id');",
        "CREATE TYPE mood AS ENUM ('ok');",
        "ALTER TABLE t ADD COLUMN w int;",
        "ALTER TABLE t ADD COLUMN IF NOT EXISTS w int NOT NULL DEFAULT 0;",
        "CREATE INDEX IF NOT EXISTS t_v ON t (v);",
        "ALTER TYPE mood ADD VALUE 'ok';",
        "COMMENT ON COLUMN t.v IS 'drop me, truncate me';",
        "-- DROP TABLE t; (comment only)\nCREATE TABLE w (id int);",
    ],
)
def test_additive_statements_pass(tmp_path: Path, sql: str) -> None:
    assert _lint_one(tmp_path, sql) == []


def test_no_transaction_file_rules(tmp_path: Path) -> None:
    two = "-- pureiron:no-transaction\nCREATE TABLE IF NOT EXISTS a (x int);\nSELECT 1;"
    messages = _lint_one(tmp_path / "two", two)
    assert any("exactly one statement" in m for m in messages)
    assert any("re-runnable" in m for m in messages)
    single = "-- pureiron:no-transaction\nCREATE INDEX IF NOT EXISTS t_v ON t (v);"
    assert _lint_one(tmp_path / "ok", single) == []


def test_contract_drop_allowed_one_epoch_after_deprecation(tmp_path: Path) -> None:
    """deprecated in D → D+1 스키마부터 제거 가능(D-1 앱 envelope 상한이 D이므로)."""
    sql = "-- contract: drops t.v deprecated in epoch 2\nALTER TABLE t DROP COLUMN v;"
    too_early = _lint_one(tmp_path / "early", sql)
    assert any("allowed from epoch 3" in m for m in too_early)

    files = {**BASELINE, "0002_mid.sql": "CREATE TABLE mid (x int);", "0003_drop.sql": sql}
    directory = write_migrations(
        tmp_path / "late", files, {1: "0001_base.sql", 2: "0002_mid.sql", 3: "0003_drop.sql"}
    )
    assert lint_directory(directory).issues == []


def test_contract_comment_must_name_dropped_object(tmp_path: Path) -> None:
    sql = "-- contract: drops t.w deprecated in epoch 1\nALTER TABLE t DROP COLUMN v;"
    assert any("DROP is not additive" in m for m in _lint_one(tmp_path, sql))


def test_names_and_duplicates(tmp_path: Path) -> None:
    files = {**BASELINE, "0001_dup.sql": "SELECT 1;", "2_bad.sql": "SELECT 1;"}
    directory = write_migrations(tmp_path, files, {1: "0001_base.sql"})
    messages = [i.message for i in lint_directory(directory).issues]
    assert any("duplicate migration version 0001" in m for m in messages)
    assert any("NNNN_lower_snake" in m for m in messages)


def test_uncovered_file_is_reported(tmp_path: Path) -> None:
    files = {**BASELINE, "0002_new.sql": "SELECT 1;"}
    directory = write_migrations(tmp_path, files, {1: "0001_base.sql"})
    messages = [i.message for i in lint_directory(directory).issues]
    assert any("not covered by any epoch" in m for m in messages)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_base_ref_protects_released_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    directory = write_migrations(repo / "migrations", BASELINE, {1: "0001_base.sql"})
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")

    assert lint_directory(directory, base_ref="master").issues == []

    (directory / "0001_base.sql").write_text("CREATE TABLE t (id bigint);", encoding="utf-8")
    write_migrations(directory, {"0002_new.sql": "SELECT 1;"}, {1: "0002_new.sql"})
    messages = [i.message for i in lint_directory(directory, base_ref="master").issues]
    assert any("released migration modified" in m for m in messages)
    assert any("released epoch 1" in m for m in messages)


def _repo_with_baseline(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    directory = write_migrations(repo / "migrations", BASELINE, {1: "0001_base.sql"})
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    return repo, directory


def test_unknown_base_ref_is_an_error(tmp_path: Path) -> None:
    _, directory = _repo_with_baseline(tmp_path)
    messages = [i.message for i in lint_directory(directory, base_ref="origin/nope").issues]
    assert any("not a known commit" in m for m in messages)


def test_new_file_numbered_below_released_version_is_rejected(tmp_path: Path) -> None:
    _, directory = _repo_with_baseline(tmp_path)
    (directory / "0001_base.sql").rename(directory / "0002_base.sql")
    write_migrations(directory, {"0001_sneaky.sql": "SELECT 1;"}, {1: "0002_base.sql"})
    messages = [i.message for i in lint_directory(directory, base_ref="master").issues]
    assert any("at or below released" in m for m in messages)


def test_base_ref_without_directory_is_bootstrap_notice(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    (repo / "README").write_text("x", encoding="utf-8")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    directory = write_migrations(repo / "migrations", BASELINE, {1: "0001_base.sql"})
    report = lint_directory(directory, base_ref="master")
    assert report.issues == []
    assert any("skipped" in n for n in report.notices)


def test_cli_exit_codes(tmp_path: Path, migrations_dir: Path) -> None:
    assert main(["migrate", "lint", str(migrations_dir)]) == EXIT_OK
    bad = write_migrations(
        tmp_path, {**BASELINE, "0002_x.sql": "DROP TABLE t;"}, {1: "0001_base.sql", 2: "0002_x.sql"}
    )
    assert main(["migrate", "lint", str(bad)]) == EXIT_LINT
