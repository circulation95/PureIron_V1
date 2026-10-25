"""다른 모듈이 audit에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만, plan M3b D12).

`AuditSink.record`는 best-effort다: 감사 기록 실패는 경고 로그만 남기고 호출자의 요청은
성공한다(운전 화면이 감사 장애로 멈추지 않게). 같은 트랜잭션에 묶어야 하는 기록(M3b-2 알람
ACK)은 `AuditWriter.record_with(conn, entry)`를 쓴다 — 실패는 asyncpg 예외 그대로 올라가 호출자
트랜잭션이 롤백된다(감사 없이 상태만 바뀌지 않게). 기록이 돼야만 동작을 하는 쪽(M3c 쓰기 의도 행 —
커밋돼야 PLC로 보낸다)은 `StrictAuditSink.record_strict`를 쓴다(실패 = `AuditUnavailableError`).
`detail`에는 비밀번호·토큰을 절대 넣지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pureiron_core.platform.db import Connection

DEFAULT_QUERY_ROWS = 100
MAX_QUERY_ROWS = 1000


class AuditAction(StrEnum):
    AUTH_LOGIN = "auth.login"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGIN_THROTTLED = "auth.login_throttled"
    AUTH_LOGOUT = "auth.logout"
    AUTH_PASSWORD_CHANGE = "auth.password_change"
    USERS_ADD = "users.add"
    USERS_PASSWD = "users.passwd"
    USERS_DISABLE = "users.disable"
    USERS_ENABLE = "users.enable"
    PROJECT_EDIT_LOCK_ACQUIRE = "project.edit_lock_acquire"
    PROJECT_EDIT_LOCK_RELEASE = "project.edit_lock_release"
    PROJECT_PATCH = "project.patch"
    ALARM_ACK = "alarm.ack"
    ALARM_ACK_ALL = "alarm.ack_all"
    TAG_WRITE = "tag.write"
    TAG_WRITE_REJECTED = "tag.write_rejected"
    WRITES_ENABLED_AT_BOOT = "writes.enabled_at_boot"


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
    """site-db 문제로 감사 기록을 읽거나(`AuditLog.query`) 쓰지 못했다(`record_strict`).

    `AuditWriter.record_with`는 이 예외로 바꾸지 않는다 — asyncpg 예외를 그대로 올린다.
    """


class AuditSink(Protocol):
    async def record(self, entry: AuditEntry) -> None:
        """best-effort: 실패해도 예외를 내지 않는다(경고 로그)."""
        ...


class AuditLog(AuditSink, Protocol):
    async def query(self, query: AuditQuery) -> tuple[AuditRecord, ...]:
        """실패하면 `AuditUnavailableError`."""
        ...


class StrictAuditSink(Protocol):
    async def record_strict(self, entry: AuditEntry) -> None:
        """기록이 커밋된 뒤 돌아온다. 실패하면 `AuditUnavailableError` — 호출자는 그 동작을
        하지 않는다. 시간 초과로 실패하면 행이 실제로는 남았을 수도 있다(호출자는 "기록 안 됨"으로
        보고 멈춘다).
        """
        ...


class AuditWriter(Protocol):
    async def record_with(self, conn: Connection, entry: AuditEntry) -> None:
        """호출자 트랜잭션(`conn`) 안에서 기록한다.

        실패는 asyncpg 예외 그대로 올린다(변환·삼킴 없음) — 호출자 트랜잭션이 롤백되어 감사 없는
        상태 변경이 남지 않는다.
        """
        ...
