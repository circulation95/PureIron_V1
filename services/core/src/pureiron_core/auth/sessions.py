"""`sessions` 테이블 저장소(asyncpg raw SQL, plan M3b D2). 토큰 원문은 받지 않고 sha256만 다룬다.

시각은 모두 호출자(`AuthService`의 주입 clock)가 정한다 — 만료 판정과 저장 값이 같은 시계를 쓰게.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pureiron_core.auth._db import auth_connection
from pureiron_core.auth.ports import Principal, Role
from pureiron_core.platform.db import Database

# 매 요청의 세션 검증 경로라 짧게 둔다(half-open DB 연결에서 요청이 멈추지 않게).
_QUERY_TIMEOUT_S = 5.0

LOOKUP_SQL = """
SELECT s.user_id, s.last_seen_at, s.expires_at, s.revoked_at,
       u.username, u.display_name, u.role, u.enabled
  FROM sessions s JOIN users u USING (user_id)
 WHERE s.token_sha256 = $1
"""
# 로그인할 때 그 사용자의 끝난(절대 만료·폐기) 세션 행을 지운다(테이블이 로그인마다 자라지 않게).
PURGE_SQL = """
DELETE FROM sessions WHERE user_id = $1 AND (expires_at <= $2 OR revoked_at IS NOT NULL)
"""
INSERT_SQL = """
INSERT INTO sessions (token_sha256, user_id, created_at, last_seen_at, expires_at)
VALUES ($1, $2, $3, $3, $4)
"""
TOUCH_SQL = "UPDATE sessions SET last_seen_at = $2 WHERE token_sha256 = $1 AND revoked_at IS NULL"
REVOKE_SQL = "UPDATE sessions SET revoked_at = $2 WHERE token_sha256 = $1 AND revoked_at IS NULL"
REVOKE_USER_SQL = """
UPDATE sessions SET revoked_at = $2
 WHERE user_id = $1 AND revoked_at IS NULL AND token_sha256 IS DISTINCT FROM $3
"""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """세션 행 + 그 시점 사용자 상태(한 번의 JOIN으로 읽어 비활성화가 즉시 반영된다)."""

    user_id: int
    username: str
    display_name: str | None
    role: Role
    enabled: bool
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None

    def principal(self) -> Principal:
        return Principal(self.user_id, self.username, self.role, self.display_name)


class PgSessionStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self, token_sha256: bytes, user_id: int, *, now: datetime, expires_at: datetime
    ) -> None:
        async with auth_connection(self._db) as conn, conn.transaction():
            await conn.execute(PURGE_SQL, user_id, now, timeout=_QUERY_TIMEOUT_S)
            await conn.execute(
                INSERT_SQL, token_sha256, user_id, now, expires_at, timeout=_QUERY_TIMEOUT_S
            )

    async def lookup(self, token_sha256: bytes) -> SessionRecord | None:
        async with auth_connection(self._db) as conn:
            row = await conn.fetchrow(LOOKUP_SQL, token_sha256, timeout=_QUERY_TIMEOUT_S)
        if row is None:
            return None
        return SessionRecord(
            user_id=int(row["user_id"]),
            username=str(row["username"]),
            display_name=row["display_name"],
            role=Role(row["role"]),
            enabled=bool(row["enabled"]),
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
        )

    async def touch(self, token_sha256: bytes, at: datetime) -> None:
        async with auth_connection(self._db) as conn:
            await conn.execute(TOUCH_SQL, token_sha256, at, timeout=_QUERY_TIMEOUT_S)

    async def revoke(self, token_sha256: bytes, at: datetime) -> None:
        async with auth_connection(self._db) as conn:
            await conn.execute(REVOKE_SQL, token_sha256, at, timeout=_QUERY_TIMEOUT_S)

    async def revoke_user(self, user_id: int, at: datetime, *, keep: bytes | None = None) -> None:
        """사용자의 열린 세션을 모두 폐기한다(`keep` 세션은 남긴다 — 본인 비밀번호 변경)."""
        async with auth_connection(self._db) as conn:
            await conn.execute(REVOKE_USER_SQL, user_id, at, keep, timeout=_QUERY_TIMEOUT_S)
