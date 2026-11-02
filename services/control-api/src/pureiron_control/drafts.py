"""현장 드래프트 보관(plan M5-2 P6·P7, §6.4): 현장이 올린 `draft.tar` → 벤더가 받아 absorb.

검증은 현장·absorb와 같은 `sitesign.open_draft`(등록된 site 키 서명 + payload `site_id` =
경로 현장 + `project_tar_sha256` 일치). 프로젝트 내용 검증(content_hash·스키마)은 absorb가
한다 — 중앙은 보관만. 파일은 `<data>/drafts/<site>/<sha256 hex>.tar`(같은 파일을 다시 올리면
같은 이름), DB에 메타 한 줄.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast

from pureiron_bundle.sitesign import SignedDocumentError, open_draft
from pureiron_control.platform.clock import Clock
from pureiron_control.platform.files import write_new
from pureiron_core.platform.db import Database

DRAFTS_DIR = "drafts"
INSERT_SQL = """
INSERT INTO drafts (site_id, site_rev, base_site_rev, content_hash, exported_at, sha256,
                    size_bytes, received_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING id
"""
LATEST_SQL = """
SELECT id, site_id, site_rev, base_site_rev, content_hash, exported_at, sha256, size_bytes,
       received_at
  FROM drafts WHERE site_id = $1 ORDER BY id DESC LIMIT 1
"""


class DraftError(Exception):
    pass


class DraftSignatureError(DraftError):
    """서명·형식·현장·tree 해시 중 하나가 틀렸다(400)."""


@dataclass(frozen=True, slots=True)
class DraftRecord:
    id: int
    site_id: str
    site_rev: int
    base_site_rev: int
    content_hash: str
    exported_at: str
    sha256: str
    size_bytes: int
    received_at: datetime


class DraftStore:
    def __init__(self, db: Database, data_dir: Path, *, clock: Clock) -> None:
        self._db = db
        self._root = data_dir / DRAFTS_DIR
        self._clock = clock

    def path(self, record: DraftRecord) -> Path:
        return self._root / record.site_id / f"{record.sha256.removeprefix('sha256:')}.tar"

    async def save(self, site_id: str, data: bytes, public_key: bytes) -> DraftRecord:
        try:
            payload, _tree = await asyncio.to_thread(open_draft, data, public_key, site_id=site_id)
        except SignedDocumentError as exc:
            raise DraftSignatureError(f"draft: {exc}") from exc
        # open_draft가 아래 필드의 타입(정수 ≥ 0·문자열)을 확인했다.
        record = DraftRecord(
            id=0,
            site_id=site_id,
            site_rev=cast(int, payload["site_rev"]),
            base_site_rev=cast(int, payload["base_site_rev"]),
            content_hash=cast(str, payload["content_hash"]),
            exported_at=cast(str, payload["exported_at"]),
            sha256="sha256:" + hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            received_at=self._clock(),
        )
        await asyncio.to_thread(write_new, self.path(record), data)
        async with self._db.connection() as conn:
            draft_id = await conn.fetchval(
                INSERT_SQL,
                record.site_id,
                record.site_rev,
                record.base_site_rev,
                record.content_hash,
                record.exported_at,
                record.sha256,
                record.size_bytes,
                record.received_at,
            )
        if not isinstance(draft_id, int):
            raise DraftError("drafts insert returned no id")
        return replace(record, id=draft_id)

    async def latest(self, site_id: str) -> DraftRecord | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(LATEST_SQL, site_id)
        return None if row is None else DraftRecord(**dict(row.items()))
