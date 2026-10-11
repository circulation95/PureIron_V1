"""`/auth`(본인 login·logout·me·password)와 세션 인증 의존성 `SessionGuard`(plan M3b D2·D3·D5).

세션 토큰은 로그인 응답의 HttpOnly·SameSite=Strict 쿠키(`pureiron_session`, 브라우저)와 응답
본문 `token`(비브라우저 → `Authorization: Bearer`) 두 가지로 받는다. 둘 다 오면 쿠키를 쓴다.
미인증은 401, 역할 부족은 403. 계정 생성·비활성화는 CLI 전용이라 여기에 없다(D3).
감사 기록은 best-effort다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pureiron_core.api.errors import site_db_unavailable
from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSink, AuditSource
from pureiron_core.auth.passwords import username_problem
from pureiron_core.auth.ports import (
    AuthPort,
    AuthUnavailableError,
    InvalidCredentialsError,
    InvalidPasswordError,
    Principal,
    Role,
    SessionInvalidError,
)

log = logging.getLogger(__name__)

SESSION_COOKIE = "pureiron_session"
# 쿠키는 API 경로에만 보낸다(웹 정적 파일 요청에는 실리지 않게). WS(`/api/v1/ws`)도 이 아래다.
SESSION_COOKIE_PATH = "/api"
# 입력 상한(그보다 긴 비밀번호는 정책상 존재할 수 없다 — passwords.PASSWORD_MAX_CHARS).
_USERNAME_MAX_CHARS = 64
_PASSWORD_MAX_CHARS = 128
_PASSWORD_MIN_CHARS = 8

_session_cookie = APIKeyCookie(
    name=SESSION_COOKIE,
    scheme_name="SessionCookie",
    description="로그인 응답이 굽는 세션 쿠키(HttpOnly·SameSite=Strict). 브라우저용",
    auto_error=False,
)
_session_bearer = HTTPBearer(
    scheme_name="SessionBearer",
    description="로그인 응답 본문의 `token`을 `Authorization: Bearer`로. 비브라우저(ai·스크립트)용",
    auto_error=False,
)

type RoleName = Literal["viewer", "operator", "engineer", "admin"]
_ROLE_NAMES: dict[Role, RoleName] = {
    Role.VIEWER: "viewer",
    Role.OPERATOR: "operator",
    Role.ENGINEER: "engineer",
    Role.ADMIN: "admin",
}
_UNAUTHORIZED: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "not authenticated"}
}


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=_USERNAME_MAX_CHARS)
    password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)


class MeOut(BaseModel):
    user_id: int
    username: str
    role: RoleName = Field(description="권한 수준: viewer < operator < engineer < admin")
    display_name: str | None


class LoginOut(BaseModel):
    token: str = Field(
        description="세션 토큰(쿠키와 같은 값). 비브라우저는 `Authorization: Bearer`로 보낸다"
    )
    expires_at: datetime = Field(description="절대 만료 시각(유휴 만료는 더 이를 수 있다)")
    user: MeOut


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=_PASSWORD_MAX_CHARS)
    new_password: str = Field(min_length=_PASSWORD_MIN_CHARS, max_length=_PASSWORD_MAX_CHARS)

    @field_validator("current_password")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("current_password must not be blank")
        return value


def _me(principal: Principal) -> MeOut:
    return MeOut(
        user_id=principal.user_id,
        username=principal.username,
        role=_ROLE_NAMES[principal.role],
        display_name=principal.display_name,
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


def _token(cookie: str | None, bearer: HTTPAuthorizationCredentials | None) -> str | None:
    return cookie or (bearer.credentials if bearer is not None else None)


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal: Principal
    token: str


class SessionGuard:
    """세션 토큰(쿠키 또는 Bearer)을 검증하는 FastAPI 의존성 묶음. 라우터 팩토리에 주입한다.

    사용: `viewer = Depends(guard.require_role(Role.VIEWER))` → `dependencies=[viewer]` 또는
    `principal: Principal = viewer`.
    """

    def __init__(self, auth: AuthPort) -> None:
        self._auth = auth

    async def context(
        self,
        cookie: Annotated[str | None, Depends(_session_cookie)],
        bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_session_bearer)],
    ) -> AuthContext:
        token = _token(cookie, bearer)
        if not token:
            raise _unauthorized("not authenticated")
        try:
            principal = await self._auth.authenticate(token)
        except SessionInvalidError as exc:
            # 이유(만료·폐기·비활성화)는 응답에 내지 않는다 — 토큰을 가진 쪽이 상태를 알지 못하게.
            log.debug("session rejected: %s", exc)
            raise _unauthorized("not authenticated") from exc
        except AuthUnavailableError as exc:
            raise site_db_unavailable(exc) from exc
        return AuthContext(principal=principal, token=token)

    def require_role(self, minimum: Role) -> Callable[..., Awaitable[Principal]]:
        signed_in = Depends(self.context)

        async def dependency(ctx: AuthContext = signed_in) -> Principal:
            if ctx.principal.role < minimum:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, detail=f"requires role {minimum.label} or higher"
                )
            return ctx.principal

        return dependency


def build_auth_router(
    *,
    auth: AuthPort,
    audit: AuditSink,
    guard: SessionGuard,
    cookie_secure: bool,
    cookie_max_age_s: int,
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])
    signed_in = Depends(guard.context)

    async def record(action: AuditAction, username: str, **detail: object) -> None:
        await audit.record(
            AuditEntry(
                action=action,
                source=AuditSource.API,
                username=username,
                target=username,
                detail=detail,
            )
        )

    @router.post("/login", operation_id="login", responses=_UNAUTHORIZED)
    async def login(body: LoginRequest, response: Response) -> LoginOut:
        try:
            result = await auth.login(body.username, body.password)
        except InvalidCredentialsError as exc:
            # 규칙에 맞지 않는 입력(예: username 칸에 넣은 비밀번호)은 감사에 남기지 않는다.
            if username_problem(body.username) is None:
                await record(AuditAction.AUTH_LOGIN_FAILED, body.username, reason=exc.reason)
            else:
                await audit.record(
                    AuditEntry(
                        action=AuditAction.AUTH_LOGIN_FAILED,
                        source=AuditSource.API,
                        detail={"reason": exc.reason, "username_invalid": True},
                    )
                )
            raise _unauthorized(str(exc)) from exc
        except AuthUnavailableError as exc:
            raise site_db_unavailable(exc) from exc
        response.set_cookie(
            SESSION_COOKIE,
            result.token,
            max_age=cookie_max_age_s,
            path=SESSION_COOKIE_PATH,
            secure=cookie_secure,
            httponly=True,
            samesite="strict",
        )
        await record(AuditAction.AUTH_LOGIN, result.principal.username)
        return LoginOut(
            token=result.token, expires_at=result.expires_at, user=_me(result.principal)
        )

    @router.post("/logout", operation_id="logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        cookie: Annotated[str | None, Depends(_session_cookie)],
        bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_session_bearer)],
    ) -> Response:
        """세션이 이미 끝났어도 204 + 쿠키 삭제(HttpOnly라 브라우저 JS는 죽은 쿠키를 못 지운다).

        감사는 유효한 세션을 끝냈을 때만 남긴다.
        """
        token = _token(cookie, bearer)
        if token:
            try:
                try:
                    principal: Principal | None = await auth.authenticate(token)
                except SessionInvalidError:
                    principal = None
                await auth.logout(token)
            except AuthUnavailableError as exc:
                raise site_db_unavailable(exc) from exc
            if principal is not None:
                await record(AuditAction.AUTH_LOGOUT, principal.username)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(
            SESSION_COOKIE,
            path=SESSION_COOKIE_PATH,
            secure=cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @router.get("/me", operation_id="get_me", responses=_UNAUTHORIZED)
    async def me(ctx: AuthContext = signed_in) -> MeOut:
        return _me(ctx.principal)

    @router.post(
        "/password",
        operation_id="change_password",
        status_code=status.HTTP_204_NO_CONTENT,
        responses={
            **_UNAUTHORIZED,
            status.HTTP_400_BAD_REQUEST: {"description": "current password is incorrect"},
        },
    )
    async def change_password(
        body: PasswordChangeRequest, ctx: AuthContext = signed_in
    ) -> Response:
        try:
            await auth.change_password(
                ctx.principal, ctx.token, body.current_password, body.new_password
            )
        except InvalidCredentialsError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="current password is incorrect"
            ) from exc
        except InvalidPasswordError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        except AuthUnavailableError as exc:
            raise site_db_unavailable(exc) from exc
        await record(AuditAction.AUTH_PASSWORD_CHANGE, ctx.principal.username)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
