"""자격증명 확인과 인가 표(plan M4 C5): 현장 token은 자기 `{site}`만, 사람·CI token은 역할로.

- 현장 = `site_tokens`(SHA-256) + 요청 본문의 site key 서명(검증은 각 경로가 한다).
- 사람·CI = `api_tokens` 역할 `admin`·`ci`·`viewer`. CLI로만 발급하고 기본 계정은 없다.
- 판정은 순수 함수 `authorize` 하나 — 인증 표 단위 테스트가 이것을 그대로 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pureiron_control.platform.clock import Clock
from pureiron_control.platform.tokens import TokenKind, new_token, token_kind, token_sha256
from pureiron_core.platform.db import Database

RESOLVE_API_SQL = """
SELECT id, name, role FROM api_tokens WHERE token_sha256 = $1 AND revoked_at IS NULL
"""
RESOLVE_SITE_SQL = """
SELECT site_id FROM site_tokens WHERE token_sha256 = $1 AND revoked_at IS NULL
"""
INSERT_API_SQL = """
INSERT INTO api_tokens (name, role, token_sha256, created_at) VALUES ($1, $2, $3, $4)
RETURNING id
"""
REVOKE_API_SQL = """
UPDATE api_tokens SET revoked_at = $2 WHERE name = $1 AND revoked_at IS NULL RETURNING id
"""
LIST_API_SQL = """
SELECT id, name, role, created_at, revoked_at FROM api_tokens ORDER BY id
"""


class Role(StrEnum):
    ADMIN = "admin"
    CI = "ci"
    VIEWER = "viewer"


class CredentialError(Exception):
    pass


class TokenNameTakenError(CredentialError):
    pass


@dataclass(frozen=True, slots=True)
class ApiPrincipal:
    token_id: int
    name: str
    role: Role


@dataclass(frozen=True, slots=True)
class SitePrincipal:
    site_id: str


type Principal = ApiPrincipal | SitePrincipal


@dataclass(frozen=True, slots=True)
class OwnSite:
    """현장 token, 그리고 그 현장이 경로 `{site}`와 같아야 한다."""


@dataclass(frozen=True, slots=True)
class AnyRole:
    roles: frozenset[Role]


type Requirement = OwnSite | AnyRole


class Decision(StrEnum):
    ALLOW = "allow"
    UNAUTHENTICATED = "unauthenticated"
    """401: token 없음·모름·폐기됨."""
    FORBIDDEN = "forbidden"
    """403: 알려진 token이지만 이 경로에 쓸 수 없다(다른 현장·종류·역할)."""


def authorize(
    principal: Principal | None, requirement: Requirement, *, path_site: str | None = None
) -> Decision:
    if principal is None:
        return Decision.UNAUTHENTICATED
    match requirement:
        case OwnSite():
            ok = isinstance(principal, SitePrincipal) and principal.site_id == path_site
        case AnyRole(roles=roles):
            ok = isinstance(principal, ApiPrincipal) and principal.role in roles
    return Decision.ALLOW if ok else Decision.FORBIDDEN


@dataclass(frozen=True, slots=True)
class ApiToken:
    token_id: int
    name: str
    role: Role
    created_at: datetime
    revoked_at: datetime | None


class PgCredentialStore:
    def __init__(self, db: Database, *, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def resolve(self, token: str) -> Principal | None:
        kind = token_kind(token)
        if kind is None:
            return None
        digest = token_sha256(token)
        async with self._db.connection() as conn:
            if kind is TokenKind.SITE:
                site = await conn.fetchval(RESOLVE_SITE_SQL, digest)
                return SitePrincipal(site_id=site) if isinstance(site, str) else None
            row = await conn.fetchrow(RESOLVE_API_SQL, digest)
        if row is None:
            return None
        return ApiPrincipal(token_id=row["id"], name=row["name"], role=Role(row["role"]))

    async def create_api_token(self, name: str, role: Role) -> tuple[ApiToken, str]:
        """새 token과 그 원문(한 번만 보여 준다). 같은 이름의 유효 token이 있으면 거부."""
        token = new_token(TokenKind.API)
        now = self._clock()
        async with self._db.connection() as conn, conn.transaction():
            taken = await conn.fetchval(
                "SELECT 1 FROM api_tokens WHERE name = $1 AND revoked_at IS NULL", name
            )
            if taken is not None:
                raise TokenNameTakenError(
                    f"an active token named {name!r} exists (revoke it first)"
                )
            token_id = await conn.fetchval(
                INSERT_API_SQL, name, role.value, token_sha256(token), now
            )
        if not isinstance(token_id, int):
            raise CredentialError("api_tokens insert returned no id")
        return ApiToken(token_id, name, role, now, None), token

    async def revoke_api_token(self, name: str) -> bool:
        async with self._db.connection() as conn:
            return await conn.fetchval(REVOKE_API_SQL, name, self._clock()) is not None

    async def list_api_tokens(self) -> list[ApiToken]:
        async with self._db.connection() as conn:
            rows = await conn.fetch(LIST_API_SQL)
        return [
            ApiToken(r["id"], r["name"], Role(r["role"]), r["created_at"], r["revoked_at"])
            for r in rows
        ]
