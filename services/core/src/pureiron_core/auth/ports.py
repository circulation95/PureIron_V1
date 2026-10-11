"""다른 모듈이 auth에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만, plan M3b D2·D5).

세션 토큰은 불투명 문자열이고, 검증하면 `Principal`(그 시점 사용자 스냅샷)이 나온다. 역할은 수준
비교(`principal.role >= Role.OPERATOR`)로만 판단한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Protocol


class Role(IntEnum):
    VIEWER = 0
    OPERATOR = 1
    ENGINEER = 2
    ADMIN = 3

    @property
    def label(self) -> str:
        """API·CLI 표기(`viewer` …)."""
        return self.name.lower()

    @classmethod
    def from_label(cls, label: str) -> Role:
        try:
            return cls[label.upper()]
        except KeyError:
            raise ValueError(f"unknown role {label!r}") from None


@dataclass(frozen=True, slots=True)
class Principal:
    user_id: int
    username: str
    role: Role
    display_name: str | None


@dataclass(frozen=True, slots=True)
class User:
    """계정 목록·CLI 출력용(비밀번호 해시 없음)."""

    user_id: int
    username: str
    role: Role
    display_name: str | None
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LoginResult:
    token: str
    principal: Principal
    expires_at: datetime


class AuthError(Exception):
    pass


class AuthUnavailableError(AuthError):
    """site-db 문제로 사용자·세션을 읽거나 쓰지 못했다(API는 503)."""


class InvalidCredentialsError(AuthError):
    """로그인 실패. 응답은 이유와 무관하게 같다 — `reason`은 감사 기록용이다."""

    def __init__(self, reason: str) -> None:
        super().__init__("invalid username or password")
        self.reason = reason


class SessionInvalidError(AuthError):
    """토큰이 없거나·모르거나·만료·폐기됐거나 사용자가 비활성화됐다(API는 401)."""


class InvalidAccountError(AuthError):
    """계정 입력값(username·display_name)이 규칙에 맞지 않는다."""


class InvalidPasswordError(AuthError):
    pass


class UserExistsError(AuthError):
    def __init__(self, username: str) -> None:
        super().__init__(f"user {username!r} already exists")
        self.username = username


class UserNotFoundError(AuthError):
    def __init__(self, username: str) -> None:
        super().__init__(f"user {username!r} not found")
        self.username = username


class AuthPort(Protocol):
    """HTTP 라우터와 (M3b-2) realtime이 세션을 다루는 표면."""

    async def login(self, username: str, password: str) -> LoginResult: ...

    async def authenticate(self, token: str) -> Principal:
        """유효한 세션이면 사용자 스냅샷을, 아니면 `SessionInvalidError`."""
        ...

    async def logout(self, token: str) -> None: ...

    async def change_password(
        self, principal: Principal, token: str, current: str, new: str
    ) -> None:
        """현재 비밀번호가 틀리면 `InvalidCredentialsError`.

        성공하면 이 세션만 남기고 그 사용자의 나머지 세션을 폐기한다.
        """
        ...
