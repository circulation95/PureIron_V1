"""`pureiron-core migrate` 본체: 카탈로그, epoch envelope, 적용·검증(ADR-024, plan §6.6).

- 이력은 `schema_migrations(version, name, checksum, applied_at, release)`, 현재 스키마 epoch는
  `schema_meta('epoch')`에 둔다. 구 앱(N-1)이 새 스키마(N)를 자기 카탈로그 없이 판정할 수
  있어야 하기 때문이다.
- `pg_advisory_lock(hashtext('pureiron.migrate'))`로 직렬화, 파일 단위 트랜잭션, 첫 실패에서 중단
  (legacy `gateway.py:627-648`처럼 실패 구문을 건너뛰지 않는다).
- 첫 줄이 `-- pureiron:no-transaction`인 파일은 구문별 autocommit으로 실행한다(TimescaleDB CAGG 등).
  이런 파일의 구문은 재실행에 안전해야 한다(lint가 강제).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

import asyncpg
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from pureiron_core.platform.db import Connection, DatabaseUnavailableError
from pureiron_core.platform.sqltext import split_statements

MIGRATION_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
NO_TRANSACTION_MARKER = "-- pureiron:no-transaction"
EPOCHS_FILE = "epochs.yaml"
LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('pureiron.migrate'))"
UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('pureiron.migrate'))"
BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    integer     PRIMARY KEY,
    name       text        NOT NULL,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    release    text        NOT NULL
)
"""

log = logging.getLogger(__name__)

type ConnectionProvider = Callable[[], AbstractAsyncContextManager[Connection]]

EXIT_OK = 0
EXIT_LOCK = 2
EXIT_CHECKSUM = 3
EXIT_FAILED = 4
EXIT_LINT = 5


class MigrationError(Exception):
    exit_code = EXIT_FAILED


class CatalogError(MigrationError):
    pass


class LockTimeoutError(MigrationError):
    exit_code = EXIT_LOCK


class ChecksumMismatchError(MigrationError):
    exit_code = EXIT_CHECKSUM


class MigrationFailedError(MigrationError):
    pass


class SchemaUnavailableError(MigrationError):
    """스키마 epoch를 읽을 수 없다(DB 단절·손상된 schema_meta). readiness는 503으로 다룬다."""


def checksum_of(sql: str) -> str:
    """줄바꿈을 LF로 정규화한 sha256 — OS별 체크아웃 차이로 checksum이 흔들리지 않게 한다."""
    normalized = sql.replace("\r\n", "\n").encode("utf-8")
    return "sha256:" + hashlib.sha256(normalized).hexdigest()


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    filename: str
    sql: str
    checksum: str
    transactional: bool


@dataclass(frozen=True, slots=True)
class Catalog:
    migrations: tuple[Migration, ...]
    epochs: Mapping[int, int]
    """epoch → 그 epoch의 마지막 마이그레이션 version."""

    @property
    def app_expected(self) -> int:
        return max(self.epochs)

    @property
    def app_max_compatible(self) -> int:
        return self.app_expected + 1

    def last_version_of(self, epoch: int) -> int:
        try:
            return self.epochs[epoch]
        except KeyError:
            raise MigrationError(
                f"epoch {epoch} is not in {EPOCHS_FILE} (known: {sorted(self.epochs)})"
            ) from None

    def epoch_of(self, version: int) -> int:
        for epoch in sorted(self.epochs):
            if version <= self.epochs[epoch]:
                return epoch
        raise MigrationError(f"version {version:04d} is not covered by {EPOCHS_FILE}")

    def epoch_reached(self, applied: set[int]) -> int | None:
        """카탈로그 기준으로 모든 파일이 적용된 가장 높은 epoch."""
        reached: int | None = None
        for epoch in sorted(self.epochs):
            last = self.epochs[epoch]
            if all(m.version in applied for m in self.migrations if m.version <= last):
                reached = epoch
            else:
                break
        return reached


def parse_migration_name(filename: str) -> tuple[int, str] | None:
    match = MIGRATION_FILE.match(filename)
    if match is None:
        return None
    return int(match[1]), match[2]


class _YamlLoader(Protocol):
    def load(self, stream: str) -> object: ...


def load_epochs(path: Path) -> dict[int, str]:
    yaml = cast(_YamlLoader, YAML(typ="safe", pure=True))
    try:
        data: object = yaml.load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise CatalogError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CatalogError(f"{path}: top level must be a mapping with key 'epochs'")
    epochs_obj = cast("dict[object, object]", data).get("epochs")
    if not isinstance(epochs_obj, dict) or not epochs_obj:
        raise CatalogError(f"{path}: 'epochs' must be a non-empty mapping of epoch → file")
    epochs: dict[int, str] = {}
    for key, value in cast("dict[object, object]", epochs_obj).items():
        if not isinstance(key, int) or isinstance(key, bool) or not isinstance(value, str):
            raise CatalogError(f"{path}: entries must be `<int epoch>: <migration file>`")
        epochs[key] = value
    return epochs


def load_catalog(directory: Path) -> Catalog:
    if not directory.is_dir():
        raise CatalogError(f"migrations directory not found: {directory}")
    migrations: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        parsed = parse_migration_name(path.name)
        if parsed is None:
            raise CatalogError(f"{path.name}: file name must match NNNN_lower_snake.sql")
        version, name = parsed
        if version in seen:
            raise CatalogError(
                f"duplicate migration version {version:04d}: {seen[version]}, {path.name}"
            )
        seen[version] = path.name
        sql = path.read_text(encoding="utf-8")
        first_line = sql.replace("\r\n", "\n").split("\n", 1)[0].strip()
        migrations.append(
            Migration(
                version=version,
                name=name,
                filename=path.name,
                sql=sql,
                checksum=checksum_of(sql),
                transactional=first_line != NO_TRANSACTION_MARKER,
            )
        )
    if not migrations:
        raise CatalogError(f"no migrations in {directory}")
    by_file = {m.filename: m.version for m in migrations}
    raw_epochs = load_epochs(directory / EPOCHS_FILE)
    epochs: dict[int, int] = {}
    previous_last = 0
    for expected_epoch, epoch in enumerate(sorted(raw_epochs), start=1):
        if epoch != expected_epoch:
            raise CatalogError(
                f"{EPOCHS_FILE}: epochs must be contiguous from 1 (got {sorted(raw_epochs)})"
            )
        filename = raw_epochs[epoch]
        if filename not in by_file:
            raise CatalogError(f"{EPOCHS_FILE}: epoch {epoch} refers to missing file {filename}")
        last = by_file[filename]
        if last <= previous_last:
            raise CatalogError(f"{EPOCHS_FILE}: epoch {epoch} must end after epoch {epoch - 1}")
        epochs[epoch] = last
        previous_last = last
    newest = max(by_file.values())
    if previous_last != newest:
        raise CatalogError(
            f"{EPOCHS_FILE}: migration {newest:04d} is not covered by any epoch "
            "(a release that adds migrations must add an epoch)"
        )
    return Catalog(migrations=tuple(migrations), epochs=epochs)


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str


@dataclass(frozen=True, slots=True)
class Status:
    cur: int | None
    app_expected: int
    app_max_compatible: int
    pending: list[str]
    checksum_ok: bool
    mismatched: list[str] = field(default_factory=list[str])
    unknown_applied: list[int] = field(default_factory=list[int])

    def as_json(self) -> dict[str, object]:
        return {
            "cur": self.cur,
            "app_expected": self.app_expected,
            "app_max_compatible": self.app_max_compatible,
            "pending": self.pending,
            "checksum_ok": self.checksum_ok,
            "mismatched": self.mismatched,
            "unknown_applied": self.unknown_applied,
        }


async def _table_exists(conn: Connection, table: str) -> bool:
    return await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table) is True


async def read_applied(conn: Connection) -> dict[int, AppliedMigration]:
    if not await _table_exists(conn, "schema_migrations"):
        return {}
    rows = await conn.fetch(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    )
    applied: dict[int, AppliedMigration] = {}
    for row in rows:
        version, name, checksum = row["version"], row["name"], row["checksum"]
        if (
            not isinstance(version, int)
            or not isinstance(name, str)
            or not isinstance(checksum, str)
        ):
            raise MigrationError("schema_migrations row has unexpected types")
        applied[version] = AppliedMigration(version=version, name=name, checksum=checksum)
    return applied


async def read_epoch(conn: Connection) -> int | None:
    """DB에 기록된 현재 스키마 epoch. 아직 baseline 전이면 None."""
    if not await _table_exists(conn, "schema_meta"):
        return None
    value = await conn.fetchval("SELECT value FROM schema_meta WHERE key = 'epoch'")
    if value is None:
        return None
    if not isinstance(value, str) or not value.isdigit():
        raise MigrationError(f"schema_meta epoch is not an integer: {value!r}")
    return int(value)


def _compare(
    catalog: Catalog, applied: Mapping[int, AppliedMigration]
) -> tuple[list[str], list[int]]:
    known = {m.version: m for m in catalog.migrations}
    mismatched = [
        known[v].filename
        for v, row in applied.items()
        if v in known and known[v].checksum != row.checksum
    ]
    unknown = sorted(v for v in applied if v not in known)
    return mismatched, unknown


async def status(conn: Connection, catalog: Catalog) -> Status:
    applied = await read_applied(conn)
    mismatched, unknown = _compare(catalog, applied)
    pending = [m.filename for m in catalog.migrations if m.version not in applied]
    return Status(
        cur=await read_epoch(conn),
        app_expected=catalog.app_expected,
        app_max_compatible=catalog.app_max_compatible,
        pending=pending,
        checksum_ok=not mismatched,
        mismatched=mismatched,
        unknown_applied=unknown,
    )


async def verify(conn: Connection, catalog: Catalog) -> None:
    """적용된 파일이 카탈로그와 같은지. 카탈로그에 없는 더 새 버전(N+1 앱이 적용)은 허용한다."""
    mismatched, _unknown = _compare(catalog, await read_applied(conn))
    if mismatched:
        raise ChecksumMismatchError(
            f"applied migrations changed since apply: {', '.join(mismatched)}"
        )


async def _acquire_lock(conn: Connection, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        if await conn.fetchval(LOCK_SQL) is True:
            return
        if time.monotonic() >= deadline:
            raise LockTimeoutError(f"could not acquire migrate lock within {timeout_s:g}s")
        await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


async def _record(
    conn: Connection, catalog: Catalog, migration: Migration, release: str, applied: set[int]
) -> None:
    await conn.execute(
        "INSERT INTO schema_migrations (version, name, checksum, release) VALUES ($1, $2, $3, $4)",
        migration.version,
        migration.name,
        migration.checksum,
        release,
    )
    applied.add(migration.version)
    reached = catalog.epoch_reached(applied)
    if reached is not None and await _table_exists(conn, "schema_meta"):
        await conn.execute(
            """
            INSERT INTO schema_meta (key, value) VALUES ('epoch', $1)
            ON CONFLICT (key) DO UPDATE
               SET value = GREATEST(schema_meta.value::integer, EXCLUDED.value::integer)::text,
                   updated_at = now()
            """,
            str(reached),
        )


async def _apply(
    conn: Connection, catalog: Catalog, migration: Migration, release: str, applied: set[int]
) -> None:
    try:
        if migration.transactional:
            async with conn.transaction():
                await conn.execute(migration.sql)
                await _record(conn, catalog, migration, release, applied)
        else:
            for statement in split_statements(migration.sql):
                await conn.execute(statement.sql)
            async with conn.transaction():
                await _record(conn, catalog, migration, release, applied)
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationFailedError(f"{migration.filename}: {exc}") from exc


async def up(
    conn: Connection,
    catalog: Catalog,
    *,
    release: str,
    to_epoch: int | None = None,
    lock_timeout_s: float = 30.0,
) -> list[str]:
    """`to_epoch`(기본: 앱 epoch)까지 미적용 파일을 순서대로 적용하고 그 파일 이름을 돌려준다."""
    target_epoch = catalog.app_expected if to_epoch is None else to_epoch
    target_version = catalog.last_version_of(target_epoch)
    await _acquire_lock(conn, lock_timeout_s)
    try:
        await conn.execute(BOOTSTRAP_SQL)
        current = await read_applied(conn)
        mismatched, _unknown = _compare(catalog, current)
        if mismatched:
            raise ChecksumMismatchError(
                f"applied migrations changed since apply: {', '.join(mismatched)}"
            )
        applied = set(current)
        done: list[str] = []
        for migration in catalog.migrations:
            if migration.version > target_version or migration.version in applied:
                continue
            await _apply(conn, catalog, migration, release, applied)
            done.append(migration.filename)
        return done
    finally:
        # unlock 실패(연결 끊김 등)가 원래 예외를 덮지 않게 한다. 세션이 끝나면 lock도 풀린다.
        try:
            await conn.fetchval(UNLOCK_SQL)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
            log.warning("could not release migrate lock explicitly: %s", exc)


@dataclass(frozen=True, slots=True)
class Envelope:
    cur: int | None
    app_expected: int
    app_max_compatible: int

    @property
    def ok(self) -> bool:
        return self.cur is not None and self.app_expected <= self.cur <= self.app_max_compatible


class SchemaGuard:
    """앱 카탈로그의 envelope와 DB의 현재 epoch를 비교한다(`/readyz`, Updater 판정 기준)."""

    def __init__(self, catalog: Catalog, connection: ConnectionProvider) -> None:
        self._catalog = catalog
        self._connection = connection

    @property
    def app_expected(self) -> int:
        return self._catalog.app_expected

    @property
    def app_max_compatible(self) -> int:
        return self._catalog.app_max_compatible

    async def envelope(self) -> Envelope:
        try:
            async with self._connection() as conn:
                cur = await read_epoch(conn)
        except SchemaUnavailableError:
            raise
        except (MigrationError, DatabaseUnavailableError, asyncpg.PostgresError, OSError) as exc:
            raise SchemaUnavailableError(str(exc)) from exc
        return Envelope(
            cur=cur, app_expected=self.app_expected, app_max_compatible=self.app_max_compatible
        )
