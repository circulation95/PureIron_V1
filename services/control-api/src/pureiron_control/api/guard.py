"""bearer token 인증·인가 의존성(plan M4 C5). 판정 자체는 `credentials.authorize`(순수 함수)다.

- token 없음·모름·폐기 → 401(`WWW-Authenticate: Bearer`).
- 알려진 token이지만 이 경로에 못 씀(다른 현장 token, 사람 token으로 현장 경로, 역할 부족) → 403.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pureiron_control.credentials import (
    AnyRole,
    ApiPrincipal,
    Decision,
    OwnSite,
    PgCredentialStore,
    Principal,
    Requirement,
    Role,
    authorize,
)
from pureiron_control.sites import site_id_pattern

_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ControlToken",
    description="control-api token — 현장 `pis_…`(자기 `{site}` 경로만), 사람·CI `pia_…`(역할)",
)

SitePath = Annotated[str, Path(pattern=site_id_pattern(), description="현장 id")]
Bearer = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def _enforce(decision: Decision) -> None:
    if decision is Decision.UNAUTHENTICATED:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if decision is Decision.FORBIDDEN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="token is not allowed on this path")


class Guard:
    def __init__(self, credentials: PgCredentialStore) -> None:
        self._credentials = credentials

    async def _check(
        self,
        bearer: HTTPAuthorizationCredentials | None,
        requirement: Requirement,
        site: str | None,
    ) -> Principal:
        principal = None if bearer is None else await self._credentials.resolve(bearer.credentials)
        _enforce(authorize(principal, requirement, path_site=site))
        return cast(Principal, principal)

    def own_site(self) -> Callable[..., Awaitable[str]]:
        """경로 `{site}`를 선언하고, 그 현장의 token일 때만 현장 id를 돌려준다."""

        async def dependency(site: SitePath, bearer: Bearer) -> str:
            await self._check(bearer, OwnSite(), site)
            return site

        return dependency

    def roles(self, *roles: Role) -> Callable[..., Awaitable[ApiPrincipal]]:
        requirement = AnyRole(frozenset(roles))

        async def dependency(bearer: Bearer) -> ApiPrincipal:
            return cast(ApiPrincipal, await self._check(bearer, requirement, None))

        return dependency
