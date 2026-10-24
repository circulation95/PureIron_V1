"""다른 모듈이 writes에 기대도 되는 표면(PD-8: 도메인 간 import는 ports만, plan M3c D8~D13).

쓰기는 사람 확인 2단계다: `prepare`(검사만, 아무것도 보내지 않음) → `confirm`(같은 사용자,
TTL 안, 1회용 — 다시 검사한 뒤 감사 의도 행이 커밋돼야 보낸다). 결과(`WriteOutcome`) 중 장비가
받았다고 읽어도 되는 것은 `CONFIRMED`(설정값 readback 일치)와 `WRITTEN`(장비 ack — 명령 기준,
설정값이면 readback을 못 읽음)뿐이다. `NO_RESPONSE`는 "결과 모름, 현장 확인"이다(써졌을 수
있다 — 자동 재시도 없음, ADR-013 5·7항).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pureiron_core.auth.ports import Principal, Role
from pureiron_core.tags.ports import TagValue, Value


class WriteKind(StrEnum):
    SETPOINT = "setpoint"
    COMMAND = "command"


class WriteOutcome(StrEnum):
    """쓰기 결과(D10). 감사 `detail.outcome`·API 응답 값."""

    CONFIRMED = "CONFIRMED"  # 설정값: readback이 쓴 값(선언 형식으로 반올림)과 같다
    WRITTEN = "WRITTEN"  # 장비가 받았다(ack). 명령은 이것이 성공, 설정값이면 readback을 못 읽음
    MISMATCH = "MISMATCH"  # 설정값: 장비는 받았지만 readback이 다르다
    FAILED = "FAILED"  # 확실히 안 써짐(장비 미연결·요청 거부형 예외 응답)
    REJECTED = "REJECTED"  # 보내지 않음(검사 실패·collector 거부)
    NO_RESPONSE = "NO_RESPONSE"  # 결과 모름(시한 초과·세션 종료·collector INDETERMINATE)


class CheckName(StrEnum):
    """실행 전 검사(D9) 항목. 이름은 "통과하려면 참이어야 하는 조건"이다."""

    WRITES_ENABLED = "writes_enabled"  # 현장 스위치 PUREIRON_WRITES_ENABLED
    DECLARED = "declared"  # project 정의에 쓰기 선언이 있는 태그
    ROLE = "role"  # 사용자 역할 ≥ 선언 role
    VALUE = "value"  # 형식·범위(setpoint)·허용 값(command)
    TAG_GOOD = "tag_good"  # 대상 태그 품질 GOOD
    DEVICE_CONNECTED = "device_connected"  # 장비 CONNECTED(core 판정)
    SINGLE_SESSION = "single_collector_session"  # collector 세션이 정확히 하나
    PLAN_REPORTED = "plan_reported"  # collector가 현재 plan_rev로 batch를 보고함(plan 적용 끝)
    SEND_WINDOW = "send_window"  # 장비 timeout_ms가 쓰기 시한 안에 들어감
    PERMISSIVE = "permissive"  # 허가 조건 태그 전부 GOOD·참
    NO_EDIT_LOCK = "no_edit_lock"  # 편집 잠금 없음(site-db를 못 읽으면 실패)
    NOT_IN_FLIGHT = "not_in_flight"  # 같은 태그에 진행 중인 쓰기 없음


@dataclass(frozen=True, slots=True)
class WriteCheck:
    name: CheckName
    ok: bool
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class WriteTarget:
    """쓰기 선언 하나(태그 `write` 블록 + 주소 형식, 기본값 채움).

    `bit`은 coil·discrete 또는 `data_type: bool`(값은 bool). setpoint는 `minimum`·`maximum`,
    command는 `values`(허용 값)와 `permissive`(1개 이상)가 있다. setpoint의 `permissive`는 선택이다.
    """

    tag_id: int
    path: str
    device_id: str
    kind: WriteKind
    role: Role
    data_type: str
    bit: bool
    minimum: float | None = None
    maximum: float | None = None
    values: tuple[Value, ...] = ()
    permissive: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedWrite:
    """`prepare` 결과: 검사를 모두 통과했고 `expires_at`까지 같은 사용자가 확인할 수 있다."""

    write_id: str
    target: WriteTarget
    value: Value
    current: TagValue | None
    checks: tuple[WriteCheck, ...]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WriteReport:
    """`confirm` 결과. `before`는 보내기 직전 현재값, readback은 collector가 다시 읽은 값."""

    write_id: str
    target: WriteTarget
    value: Value
    before: TagValue | None
    outcome: WriteOutcome
    readback: float | None
    readback_ts_ns: int | None
    error: str | None


@dataclass(frozen=True, slots=True)
class WritesStatus:
    """`/readyz.writes`(준비 판정에는 쓰지 않음). 누적 값은 core 기동 후 기준.

    `outcomes`는 confirm 결과 수(confirm 때 검사 거부 = REJECTED 포함, prepare 거부는 제외).
    `audit_failures_total`은 기록하지 못한 감사 행 수(의도 행 실패 = 보내지 않음, 결과·거부 행
    실패 = best-effort 손실).
    """

    enabled: bool
    in_flight: int
    outcomes: Mapping[WriteOutcome, int]
    audit_failures_total: int


class WriteError(Exception):
    pass


class WriteNotFoundError(WriteError):
    """그 id의 준비된 쓰기가 없다(이미 확인됨·다른 사용자가 준비함·모르는 id)."""

    def __init__(self, write_id: str) -> None:
        super().__init__(f"no pending write {write_id!r}")
        self.write_id = write_id


class WriteExpiredError(WriteError):
    def __init__(self, write_id: str) -> None:
        super().__init__(f"pending write {write_id!r} expired; prepare again")
        self.write_id = write_id


class WriteRejectedError(WriteError):
    """실행 전 검사 실패 — 아무것도 보내지 않았다."""

    def __init__(self, checks: tuple[WriteCheck, ...], write_id: str | None = None) -> None:
        failed = ", ".join(check.name.value for check in checks if not check.ok)
        super().__init__(f"write rejected by checks: {failed}")
        self.checks = checks
        self.write_id = write_id


class WriteUnavailableError(WriteError):
    """감사 의도 행을 기록하지 못했다(site-db) — 보내지 않았다(D11 fail-closed, API 503)."""


class WritesStatusSource(Protocol):
    def status(self) -> WritesStatus: ...


class WritesPort(WritesStatusSource, Protocol):
    """HTTP 라우터가 쓰기를 다루는 표면."""

    @property
    def enabled(self) -> bool: ...

    @property
    def confirm_ttl_s(self) -> float: ...

    def targets(self) -> tuple[WriteTarget, ...]:
        """선언된 쓰기 태그 전부(path 순). 스위치가 꺼져 있어도 선언은 보여 준다."""
        ...

    async def prepare(
        self, principal: Principal, tag_path: str, value: Value, reason: str
    ) -> PreparedWrite:
        """`WriteRejectedError`(검사 실패 — 감사 `tag.write_rejected`)."""
        ...

    async def confirm(self, principal: Principal, write_id: str) -> WriteReport:
        """`WriteNotFoundError`·`WriteExpiredError`·`WriteRejectedError`·`WriteUnavailableError`.

        보낸 쓰기의 결과는 예외가 아니라 `WriteReport.outcome`이다.
        """
        ...
