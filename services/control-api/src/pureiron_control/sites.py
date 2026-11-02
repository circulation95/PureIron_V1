"""현장 등록·재등록·토큰 재발급(PD-3, foundation §6.4): enrollment 봉투 → site 키 + 온라인 token.

- enrollment 파일은 Updater `enroll`이 쓴 `site-enrollment` 봉투(자기 키 서명 = 개인키 보유 증명).
- 등록은 종류별 수위 둘과 `current_seq`를 `next_report_seq - 1`로 시작한다. rekey는 구 키를
  폐기하고(구 키로 서명된 보고는 이후 400) 다음 seq `max(두 수위 중 큰 값 + 1, 봉투
  next_report_seq)`를 돌려준다(`seq_pending`이면 봉투 값은 무시, 카운터는 공용 — `current_seq`는
  두 수위 중 큰 값을 넘지 않는다) — 현장은 이 값을 `enroll --next-seq`로 반영한다.
- 동시 등록은 `INSERT … ON CONFLICT DO NOTHING`으로 판정한다: 늦은 쪽은 409(`SiteExistsError`).
- token 원문은 발급 응답에만 나간다(DB에는 SHA-256).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import cast

from pureiron_bundle import keys
from pureiron_bundle.manifest import SchemaName, contract_schema
from pureiron_bundle.sitesign import SignedDocumentError, SitePurpose, open_document
from pureiron_control.platform.clock import Clock
from pureiron_control.platform.seqmarks import SeqMarks
from pureiron_control.platform.tokens import TokenKind, new_token, token_sha256
from pureiron_core.platform.db import Connection, Database

INSERT_SITE_SQL = """
INSERT INTO sites (site_id, registered_at, last_heartbeat_seq, last_report_seq, current_seq)
VALUES ($1, $2, $3, $3, $3)
ON CONFLICT (site_id) DO NOTHING
RETURNING site_id
"""
INSERT_KEY_SQL = """
INSERT INTO site_keys (site_id, public_key, fingerprint, created_at) VALUES ($1, $2, $3, $4)
"""
REVOKE_KEYS_SQL = "UPDATE site_keys SET revoked_at = $2 WHERE site_id = $1 AND revoked_at IS NULL"
INSERT_TOKEN_SQL = """
INSERT INTO site_tokens (site_id, token_sha256, created_at) VALUES ($1, $2, $3)
"""
REVOKE_TOKENS_SQL = """
UPDATE site_tokens SET revoked_at = $2 WHERE site_id = $1 AND revoked_at IS NULL
"""
ACTIVE_KEY_SQL = "SELECT public_key FROM site_keys WHERE site_id = $1 AND revoked_at IS NULL"
LOCK_SITE_SQL = """
SELECT last_heartbeat_seq, last_report_seq FROM sites WHERE site_id = $1 FOR UPDATE
"""


class SiteError(Exception):
    pass


class EnrollmentError(SiteError):
    """enrollment 봉투의 형식·서명이 틀렸다(400)."""


class SiteExistsError(SiteError):
    pass


class SiteNotFoundError(SiteError):
    pass


@cache
def site_id_pattern() -> str:
    """현장 id 정규식(fleet-site 계약 `$defs.id` — 경로 `{site}` 검증과 같은 값)."""
    defs = cast(dict[str, dict[str, str]], contract_schema(SchemaName.FLEET_SITE)["$defs"])
    return defs["id"]["pattern"]


@dataclass(frozen=True, slots=True)
class Enrollment:
    site_id: str
    public_key: bytes
    fingerprint: str
    next_report_seq: int
    seq_pending: bool


@dataclass(frozen=True, slots=True)
class Registration:
    site_id: str
    fingerprint: str
    next_seq: int


def parse_enrollment(data: bytes) -> Enrollment:
    try:
        payload = open_document(data, SitePurpose.ENROLLMENT)
    except SignedDocumentError as exc:
        raise EnrollmentError(f"enrollment: {exc}") from exc
    site_id, fingerprint = payload.get("site_id"), payload.get("fingerprint")
    next_seq, pending = payload.get("next_report_seq"), payload.get("seq_pending", False)
    if not isinstance(site_id, str) or not re.fullmatch(site_id_pattern(), site_id):
        raise EnrollmentError(f"enrollment: invalid site_id {site_id!r}")
    if type(next_seq) is not int or next_seq < 1:
        raise EnrollmentError("enrollment: next_report_seq must be an integer >= 1")
    if not isinstance(pending, bool):
        raise EnrollmentError("enrollment: seq_pending must be a boolean")
    # open_document가 public_key(hex 32B)를 확인했다. 지문은 그 키에서 다시 계산해 맞춰 본다.
    public = bytes.fromhex(cast(str, payload["public_key"]))
    if fingerprint != keys.fingerprint(public):
        raise EnrollmentError("enrollment: fingerprint does not match public_key")
    return Enrollment(site_id, public, keys.fingerprint(public), next_seq, pending)


class PgSiteRegistry:
    def __init__(self, db: Database, *, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def _add_key(self, conn: Connection, enrollment: Enrollment) -> None:
        await conn.execute(
            INSERT_KEY_SQL,
            enrollment.site_id,
            enrollment.public_key.hex(),
            enrollment.fingerprint,
            self._clock(),
        )

    async def _issue_token(self, conn: Connection, site_id: str) -> str:
        token = new_token(TokenKind.SITE)
        await conn.execute(REVOKE_TOKENS_SQL, site_id, self._clock())
        await conn.execute(INSERT_TOKEN_SQL, site_id, token_sha256(token), self._clock())
        return token

    async def register(self, enrollment: Enrollment) -> tuple[Registration, str]:
        """등록과 새 현장 token 원문(rekey는 token을 바꾸지 않는다 — `reissue_token`)."""
        site_id = enrollment.site_id
        # 상태를 잃은 현장(seq_pending)의 첫 등록도 seq 1부터다 — 중앙에 이전 기록이 없다.
        next_seq = 1 if enrollment.seq_pending else enrollment.next_report_seq
        async with self._db.connection() as conn, conn.transaction():
            # 확인 후 INSERT는 동시 등록에서 PK 위반 500이 된다 — 충돌은 INSERT 자체로 판정한다.
            inserted = await conn.fetchval(INSERT_SITE_SQL, site_id, self._clock(), next_seq - 1)
            if inserted is None:
                raise SiteExistsError(f"site {site_id!r} is already registered (use rekey)")
            await self._add_key(conn, enrollment)
            token = await self._issue_token(conn, site_id)
        return Registration(site_id, enrollment.fingerprint, next_seq), token

    async def rekey(self, site_id: str, enrollment: Enrollment) -> Registration:
        if enrollment.site_id != site_id:
            raise EnrollmentError(f"enrollment is for site {enrollment.site_id!r}, not {site_id!r}")
        async with self._db.connection() as conn, conn.transaction():
            row = await conn.fetchrow(LOCK_SITE_SQL, site_id)
            if row is None:
                raise SiteNotFoundError(f"site {site_id!r} is not registered")
            marks = SeqMarks(heartbeat=row["last_heartbeat_seq"], report=row["last_report_seq"])
            offered = 0 if enrollment.seq_pending else enrollment.next_report_seq
            next_seq = max(marks.next_seq, offered)
            await conn.execute(REVOKE_KEYS_SQL, site_id, self._clock())
            # 수위는 그대로 둔다: 다음 seq는 현장이 `--next-seq`로 맞추고, 그보다 큰 seq만
            # 반영된다는 규칙(C7)이 이미 순서를 지킨다.
            await self._add_key(conn, enrollment)
        return Registration(site_id, enrollment.fingerprint, next_seq)

    async def reissue_token(self, site_id: str) -> str:
        async with self._db.connection() as conn, conn.transaction():
            if await conn.fetchval(LOCK_SITE_SQL, site_id) is None:
                raise SiteNotFoundError(f"site {site_id!r} is not registered")
            return await self._issue_token(conn, site_id)

    async def public_key(self, site_id: str) -> bytes | None:
        """등록된 현장의 유효 공개키. 미등록이면 None."""
        async with self._db.connection() as conn:
            value = await conn.fetchval(ACTIVE_KEY_SQL, site_id)
        return bytes.fromhex(value) if isinstance(value, str) else None
