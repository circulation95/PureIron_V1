"""다른 모듈이 audit에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만, plan M3b D12).

`AuditSink.record`는 best-effort다: 감사 기록 실패는 경고 로그만 남기고 호출자의 요청은
성공한다(운전 화면이 감사 장애로 멈추지 않게). 같은 트랜잭션에 묶어야 하는 기록(M3b-2 알람
ACK)은 구현의 `record_with(conn, entry)`를 쓴다. `detail`에는 비밀번호·토큰을 절대 넣지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

DEFAULT_QUERY_ROWS = 100
MAX_QUERY_ROWS = 1000


class AuditAction(StrEnum):
    AUTH_LOGIN = "auth.login"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGOUT = "auth.logout"
    AUTH_PASSWORD_CHANGE = "auth.password_change"
    USERS_ADD = "users.add"
    USERS_PASSWD = "users.passwd"
    USERS_DISABLE = "users.disable"
    USERS_ENABLE = "users.enable"
    PROJECT_EDIT_LOCK_ACQUIRE = "project.edit_lock_acquire"
    PROJECT_EDIT_LOCK_RELEASE = "project.edit_lock_release"


class AuditSource(StrEnum):
    API = "api"
    CLI = "cli"
    UPDATER = "updater"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """기록할 한 건. `username` = 행위자(로그인 사용자가 없으면 None), `target` = 대상."""

    action: AuditAction
    source: AuditSource
    username: str | None = None
    target: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict[str, object])


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """저장된 한 건. `action`·`source`는 문자열 그대로(이후 버전이 더한 값도 읽을 수 있게)."""

    id: int
    time: datetime
    username: str | None
    action: str
    target: str | None
    detail: Mapping[str, object]
    source: str


@dataclass(frozen=True, slots=True)
class AuditQuery:
    """조회 조건. 시간 범위는 `[start, end)`, 결과는 최신순, `limit`은 1~MAX_QUERY_ROWS."""

    username: str | None = None
    action: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    limit: int = DEFAULT_QUERY_ROWS


class AuditError(Exception):
    pass


class AuditUnavailableError(AuditError):
    """site-db 문제로 감사 기록을 읽거나 (트랜잭션 기록에서) 쓰지 못했다."""


class AuditSink(Protocol):
    async def record(self, entry: AuditEntry) -> None:
        """best-effort: 실패해도 예외를 내지 않는다(경고 로그)."""
        ...


class AuditLog(AuditSink, Protocol):
    async def query(self, query: AuditQuery) -> tuple[AuditRecord, ...]:
        """실패하면 `AuditUnavailableError`."""
        ...
