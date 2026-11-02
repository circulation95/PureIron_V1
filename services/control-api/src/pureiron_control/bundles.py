"""번들 게시·DesiredState(PD-15, foundation §6.4): `PUT /bundles/{site}/{gen}` → 디스크 + DB 한 줄.

- gen은 현장별로 늘기만 한다: 최신 게시 gen 이하 업로드는 `GenerationConflictError`(409).
  같은 gen 동시 업로드는 현장 행 잠금으로 줄을 세우고 뒤에 온 쪽이 409다.
- 파일은 `<data>/bundles/<site>/g<gen>.pib`. 업로드를 임시 파일로 받으며 sha256·크기를 재고,
  `manifest.json`을 읽어 `site_id`·`generation`이 경로와 같은지 본다(서명 확인은 현장 Updater 몫 —
  여기서 읽은 버전은 fleet 표시용이다). 이름 바꾸기는 DB 행과 같은 트랜잭션 안에서 커밋 직전에 한다:
  커밋이 실패해도 남는 것은 아직 게시되지 않은 gen의 파일뿐이고 다음 업로드가 덮어쓴다.
- 데이터 디렉터리 쓰기 실패(`OSError` — 디스크 가득·권한)는 `BundleStorageError`(507)다. 이동은
  DB 연결 본문 안에서 일어나므로 `OSError`를 그대로 두면 연결 계층이 control-db 장애(503)로 바꾼다 —
  운영자가 DB를 의심하고 게시 도구가 일시 장애로 재시도하게 된다.
- DesiredState = 그 현장의 최신 게시 번들(revert도 gen+1 게시라 늘 최댓값이다).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pureiron_bundle.errors import BundleRejectedError
from pureiron_bundle.manifest import MANIFEST_FILE, Manifest, parse_manifest
from pureiron_bundle.pib import open_pib
from pureiron_control.platform.clock import Clock
from pureiron_control.platform.files import SpooledFile, discard, place, spool
from pureiron_core.platform.db import Database

BUNDLES_DIR = "bundles"
LOCK_SITE_SQL = "SELECT 1 FROM sites WHERE site_id = $1 FOR UPDATE"
LATEST_GEN_SQL = "SELECT max(generation) FROM bundles WHERE site_id = $1"
INSERT_SQL = """
INSERT INTO bundles (site_id, generation, sha256, size_bytes, product_version, project_id,
                     project_version, fleet_commit, published_at, published_by)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""
_COLUMNS = """
site_id, generation, sha256, size_bytes, product_version, project_id, project_version,
fleet_commit, published_at
"""
DESIRED_SQL = f"""
SELECT {_COLUMNS} FROM bundles WHERE site_id = $1 ORDER BY generation DESC LIMIT 1
"""
GET_SQL = f"SELECT {_COLUMNS} FROM bundles WHERE site_id = $1 AND generation = $2"


class BundleError(Exception):
    pass


class BundleSiteNotFoundError(BundleError):
    pass


class GenerationConflictError(BundleError):
    def __init__(self, generation: int, latest: int) -> None:
        super().__init__(f"generation {generation} <= latest published generation {latest}")
        self.latest = latest


class BundleFormatError(BundleError):
    """`.pib`가 아니거나 manifest가 경로의 site·gen과 다르다(400)."""


class BundleStorageError(BundleError):
    """데이터 디렉터리에 번들 파일을 쓰지 못했다(507). `OSError`의 하위가 아니다 — DB 연결 계층이
    control-db 장애로 바꾸지 않게."""


@contextmanager
def _storage(action: str) -> Generator[None]:
    try:
        yield
    except OSError as exc:
        raise BundleStorageError(f"{action}: {exc}") from exc


def place_bundle(spooled: SpooledFile, target: Path) -> None:
    """임시 파일을 게시 이름으로 옮긴다. 파일 시스템 오류는 `BundleStorageError`."""
    with _storage(f"cannot place bundle file {target}"):
        place(spooled, target)


@dataclass(frozen=True, slots=True)
class PublishedBundle:
    site_id: str
    generation: int
    sha256: str
    size_bytes: int
    product_version: str
    project_id: str
    project_version: str
    fleet_commit: str
    published_at: datetime


def _read_manifest(path: Path) -> Manifest:
    with open_pib(path) as reader:
        return parse_manifest(reader.read_metadata()[MANIFEST_FILE])


class BundleStore:
    def __init__(self, db: Database, data_dir: Path, *, clock: Clock, max_bytes: int) -> None:
        self._db = db
        self._root = data_dir / BUNDLES_DIR
        self._clock = clock
        self._max_bytes = max_bytes

    def path(self, site_id: str, generation: int) -> Path:
        return self._root / site_id / f"g{generation}.pib"

    async def _latest(self, site_id: str) -> int:
        """게시 전 빠른 확인(번들을 다 받기 전에 409·404를 낸다). 판정은 `publish` 트랜잭션."""
        async with self._db.connection() as conn:
            if await conn.fetchval("SELECT 1 FROM sites WHERE site_id = $1", site_id) is None:
                raise BundleSiteNotFoundError(f"site {site_id!r} is not registered")
            latest = await conn.fetchval(LATEST_GEN_SQL, site_id)
        return latest if isinstance(latest, int) else 0

    async def publish(
        self,
        site_id: str,
        generation: int,
        chunks: AsyncIterator[bytes],
        *,
        published_by: int | None,
    ) -> PublishedBundle:
        latest = await self._latest(site_id)
        if generation <= latest:
            raise GenerationConflictError(generation, latest)
        with _storage("cannot spool bundle upload"):
            spooled = await spool(chunks, self._root / site_id, limit_bytes=self._max_bytes)
        try:
            manifest = await self._checked_manifest(spooled, site_id, generation)
            return await self._commit(spooled, manifest, published_by)
        finally:
            discard(spooled)

    async def _checked_manifest(
        self, spooled: SpooledFile, site_id: str, generation: int
    ) -> Manifest:
        try:
            manifest = await asyncio.to_thread(_read_manifest, spooled.path)
        except BundleRejectedError as exc:
            raise BundleFormatError(f"not a site bundle: {exc}") from exc
        if (manifest.site_id, manifest.generation) != (site_id, generation):
            raise BundleFormatError(
                f"bundle manifest is for site {manifest.site_id!r} generation "
                f"{manifest.generation}, not {site_id!r} generation {generation}"
            )
        return manifest

    async def _commit(
        self, spooled: SpooledFile, manifest: Manifest, published_by: int | None
    ) -> PublishedBundle:
        bundle = PublishedBundle(
            site_id=manifest.site_id,
            generation=manifest.generation,
            sha256=spooled.sha256,
            size_bytes=spooled.size_bytes,
            product_version=manifest.product_release.version,
            project_id=manifest.project.project_id,
            project_version=manifest.project.version,
            fleet_commit=manifest.fleet_commit,
            published_at=self._clock(),
        )
        async with self._db.connection() as conn, conn.transaction():
            if await conn.fetchval(LOCK_SITE_SQL, bundle.site_id) is None:
                raise BundleSiteNotFoundError(f"site {bundle.site_id!r} is not registered")
            latest = await conn.fetchval(LATEST_GEN_SQL, bundle.site_id)
            if isinstance(latest, int) and bundle.generation <= latest:
                raise GenerationConflictError(bundle.generation, latest)
            await conn.execute(
                INSERT_SQL,
                bundle.site_id,
                bundle.generation,
                bundle.sha256,
                bundle.size_bytes,
                bundle.product_version,
                bundle.project_id,
                bundle.project_version,
                bundle.fleet_commit,
                bundle.published_at,
                published_by,
            )
            target = self.path(bundle.site_id, bundle.generation)
            await asyncio.to_thread(place_bundle, spooled, target)
        return bundle

    async def desired(self, site_id: str) -> PublishedBundle | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(DESIRED_SQL, site_id)
        return None if row is None else PublishedBundle(**dict(row.items()))

    async def get(self, site_id: str, generation: int) -> PublishedBundle | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(GET_SQL, site_id, generation)
        return None if row is None else PublishedBundle(**dict(row.items()))
