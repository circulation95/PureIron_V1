"""현장 드래프트 보관(plan M5-2 P6·P7, §6.4): 현장이 올린 `draft.tar` → 벤더가 받아 absorb.

검증은 현장·absorb와 같은 `sitesign.open_draft`(등록된 site 키 서명 + payload `site_id` =
경로 현장 + `project_tar_sha256` 일치) — 서명 불일치는 409(rekey 대기일 수 있음), 나머지는 400.
프로젝트 내용 검증(content_hash·스키마)은 absorb가 한다 — 중앙은 보관만. 파일은
`<data>/drafts/<site>/<sha256 hex>.tar`(같은 파일을 다시 올리면 같은 이름), DB에 메타 한 줄.

- 저장 전 검사(M4-0 리뷰 R-3): `site_rev`·`base_site_rev`는 0..2^63-1(bigint 열), `content_hash`·
  `exported_at`에 NUL·짝 없는 서로게이트가 없어야 한다 — 저장 단계 500 대신 400.
- 파일은 INSERT와 같은 트랜잭션 안에서(현장 행 잠금 아래) 쓰고 커밋한다: 파일 쓰기가 실패하면 행도
  롤백되고(507), 이번 요청이 새로 만든 파일은 실패 시 지운다 — 행 없는 고아 파일을 남기지 않는다.
- `latest` = 가장 높은 `site_rev`(같으면 나중 행) — 늦게 재전송된 낮은 리비전 드래프트가 새
  드래프트를 가리지 않는다(M4-1 E9, L-5).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import cast

from pureiron_bundle.sitesign import Json, SignatureMismatchError, SignedDocumentError, open_draft
from pureiron_control.platform.clock import Clock
from pureiron_control.platform.files import write_new
from pureiron_control.platform.seqmarks import SEQ_MAX
from pureiron_control.platform.storable import UNSTORABLE_HINT, storable
from pureiron_core.platform.db import Database

DRAFTS_DIR = "drafts"
LOCK_SITE_SQL = "SELECT 1 FROM sites WHERE site_id = $1 FOR UPDATE"
INSERT_SQL = """
INSERT INTO drafts (site_id, site_rev, base_site_rev, content_hash, exported_at, sha256,
                    size_bytes, received_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
RETURNING id
"""
LATEST_SQL = """
SELECT id, site_id, site_rev, base_site_rev, content_hash, exported_at, sha256, size_bytes,
       received_at
  FROM drafts WHERE site_id = $1 ORDER BY site_rev DESC, id DESC LIMIT 1
"""


class DraftError(Exception):
    pass


class DraftSignatureError(DraftError):
    """형식·현장·tree 해시 중 하나가 틀렸다(400)."""


class DraftKeyMismatchError(DraftError):
    """등록된 site 키로 서명이 확인되지 않는다(409 — 재등록 뒤 rekey 대기일 수 있음, 리뷰 M1)."""


class DraftSiteNotFoundError(DraftError):
    pass


class DraftStorageError(DraftError):
    """데이터 디렉터리에 드래프트 파일을 쓰지 못했다(507). `OSError`의 하위가 아니다 — DB 연결
    계층이 control-db 장애(503)로 바꾸지 않게."""


def check_payload(payload: dict[str, Json]) -> None:
    """`open_draft`가 타입(정수 >= 0·문자열)을 확인한 payload의 저장 가능 여부(R-3, 400)."""
    for key in ("site_rev", "base_site_rev"):
        if cast(int, payload[key]) > SEQ_MAX:
            raise DraftSignatureError(f"draft: payload.{key} must be an integer in 0..{SEQ_MAX}")
    for key in ("content_hash", "exported_at"):
        if not storable(cast(str, payload[key])):
            raise DraftSignatureError(f"draft: payload.{key} {UNSTORABLE_HINT}")


def open_site_draft(data: bytes, public_key: bytes, site_id: str) -> dict[str, Json]:
    """`open_draft` 오류 분류: 서명 불일치 → `DraftKeyMismatchError`(409), 그 밖 →
    `DraftSignatureError`(400)."""
    try:
        payload, _tree = open_draft(data, public_key, site_id=site_id)
    except SignatureMismatchError as exc:
        raise DraftKeyMismatchError(f"draft: {exc}") from exc
    except SignedDocumentError as exc:
        raise DraftSignatureError(f"draft: {exc}") from exc
    check_payload(payload)  # open_draft가 아래 필드의 타입(정수 >= 0·문자열)을 확인했다
    return payload


def draft_exists(target: Path) -> bool:
    """같은 내용 파일이 이미 있는가. 디렉터리 권한 오류도 저장 오류다(507, 리뷰 L5) — DB 연결 본문
    안의 `OSError`는 503이 된다."""
    try:
        return target.exists()
    except OSError as exc:
        raise DraftStorageError(f"cannot check draft file {target}: {exc}") from exc


def place_draft(target: Path, data: bytes) -> None:
    try:
        write_new(target, data)
    except OSError as exc:
        raise DraftStorageError(f"cannot write draft file {target}: {exc}") from exc


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
        payload = await asyncio.to_thread(open_site_draft, data, public_key, site_id)
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
        target = self.path(record)
        created = False
        try:
            async with self._db.connection() as conn, conn.transaction():
                if await conn.fetchval(LOCK_SITE_SQL, site_id) is None:
                    raise DraftSiteNotFoundError(f"site {site_id!r} is not registered")
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
                # 같은 내용의 이전 업로드가 둔 파일이면 그대로 둔다(이름 = 내용 해시).
                created = not await asyncio.to_thread(draft_exists, target)
                await asyncio.to_thread(place_draft, target, data)
        except BaseException:
            if created:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(target.unlink)
            raise
        return replace(record, id=draft_id)

    async def latest(self, site_id: str) -> DraftRecord | None:
        async with self._db.connection() as conn:
            row = await conn.fetchrow(LATEST_SQL, site_id)
        return None if row is None else DraftRecord(**dict(row.items()))
