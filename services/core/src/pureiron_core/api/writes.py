"""`/writes`: 쓰기 대상 조회(viewer+), 2단계 쓰기 prepare·confirm(operator+), plan M3c D8·D13.

- `GET /writes/targets` — 현장 스위치 상태와 선언된 쓰기 태그·규칙(스위치가 꺼져도 선언은 보인다).
- `POST /writes` — 검사만 하고 아무것도 보내지 않는다. 통과하면 `write_id`·만료를 돌려주고, 하나라도
  실패하면 409 + 검사 목록(감사 `tag.write_rejected`). 선언 role(engineer)은 검사 `role`로 본다.
- `POST /writes/{write_id}/confirm` — prepare한 본인만, TTL 안, 1회용(두 번째는 404). 다시 검사(409)
  → 감사 의도 행을 못 쓰면 503(안 보냄) → 보내고 결과를 200으로 낸다. 결과는 `outcome`이며
  `NO_RESPONSE`는 "결과 모름, 현장 확인"이다(써졌을 수 있다 — 자동으로 다시 보내지 않는다).
이력은 `GET /audit?action=tag.write`(engineer+). WS 방송은 하지 않는다(값 변화는 `tag_update`).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, field_validator

from pureiron_core.api.auth import SessionGuard
from pureiron_core.auth.ports import Principal, Role
from pureiron_core.realtime.protocol import TagValueOut
from pureiron_core.writes.ports import (
    CheckName,
    WriteCheck,
    WriteExpiredError,
    WriteNotFoundError,
    WriteOutcome,
    WriteRejectedError,
    WritesPort,
    WriteTarget,
    WriteUnavailableError,
)

# project 스키마 `$defs/tagPath`와 같은 규칙.
_TAG_PATH_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}$"
_WRITE_ID_PATTERN = r"^[0-9a-f]{32}$"
REASON_MAX_CHARS = 200
WRITE_REJECTED = "write rejected by checks"
AUDIT_UNAVAILABLE = "audit unavailable; write not sent"

type _WriteId = Annotated[str, Path(pattern=_WRITE_ID_PATTERN)]
# NaN·Inf는 모델에서 거르지 않고 라우터가 422로 끝낸다 — pydantic 검증 오류는 입력값을 응답에 싣는데
# NaN은 JSON으로 낼 수 없어 422 대신 500이 된다.
type WriteValue = StrictBool | StrictFloat

_AUTH_ERRORS: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "not authenticated"},
    status.HTTP_403_FORBIDDEN: {"description": "requires a higher role"},
}


class WriteTargetOut(BaseModel):
    tag_id: int
    path: str
    device_id: str
    kind: Literal["setpoint", "command"]
    role: Literal["operator", "engineer"] = Field(description="실행에 필요한 최소 역할")
    data_type: str = Field(description="Modbus data_type(bool이면 값은 true/false)")
    min: float | None = Field(description="setpoint 허용 범위 하한(command는 null)")
    max: float | None = Field(description="setpoint 허용 범위 상한(command는 null)")
    values: list[StrictBool | float] = Field(description="command 허용 값(setpoint는 빈 목록)")
    permissive: list[str] = Field(description="허가 조건 태그 path — 전부 GOOD·참이어야 보낸다")

    @classmethod
    def of(cls, target: WriteTarget) -> WriteTargetOut:
        return cls(
            tag_id=target.tag_id,
            path=target.path,
            device_id=target.device_id,
            kind=target.kind.value,
            role="engineer" if target.role >= Role.ENGINEER else "operator",
            data_type=target.data_type,
            min=target.minimum,
            max=target.maximum,
            values=list(target.values),
            permissive=list(target.permissive),
        )


class WriteTargetsOut(BaseModel):
    enabled: bool = Field(description="현장 스위치 PUREIRON_WRITES_ENABLED(꺼지면 모든 쓰기 거부)")
    confirm_ttl_s: float = Field(description="prepare 뒤 confirm까지 허용 시간")
    targets: list[WriteTargetOut] = Field(description="선언된 쓰기 태그(path 순)")


class WriteRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_path: str = Field(pattern=_TAG_PATH_PATTERN)
    value: WriteValue = Field(description="목표값. bit 형식(coil·bool)은 true/false, 그 밖은 숫자")
    reason: str = Field(
        min_length=1, max_length=REASON_MAX_CHARS, description="사유(감사에 남는다)"
    )

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class WriteCheckOut(BaseModel):
    name: CheckName
    ok: bool
    detail: str | None = Field(description="실패 이유(통과하면 null)")

    @classmethod
    def of(cls, check: WriteCheck) -> WriteCheckOut:
        return cls(name=check.name, ok=check.ok, detail=check.detail)


class WritePreparedOut(BaseModel):
    write_id: str = Field(description="confirm에 쓰는 1회용 id")
    tag_id: int
    tag_path: str
    kind: Literal["setpoint", "command"]
    value: StrictBool | float = Field(description="목표값")
    current: TagValueOut | None = Field(description="지금 현재값(태그가 plan에 없으면 null)")
    checks: list[WriteCheckOut] = Field(description="실행 전 검사(모두 통과)")
    expires_at: datetime = Field(description="이 시각까지 같은 사용자가 confirm해야 한다")


class WriteRejectedOut(BaseModel):
    detail: str
    write_id: str | None = Field(description="confirm에서 거부됐으면 그 id(다시 쓸 수 없다)")
    checks: list[WriteCheckOut] = Field(description="검사 전부(실패한 것은 ok=false·detail)")


class WriteResultOut(BaseModel):
    write_id: str
    tag_id: int
    tag_path: str
    kind: Literal["setpoint", "command"]
    value: StrictBool | float = Field(description="목표값")
    before: TagValueOut | None = Field(description="보내기 직전 현재값")
    outcome: WriteOutcome = Field(
        description="CONFIRMED(설정값 readback 일치)·WRITTEN(장비 ack — 명령, 또는 readback을 못 "
        "읽은 설정값)·MISMATCH(readback 불일치)·FAILED(확실히 안 써짐)·REJECTED(보내지 않음)·"
        "NO_RESPONSE(결과 모름 — 써졌을 수 있다, 현장 확인. 자동 재시도 없음)"
    )
    readback: float | None = Field(description="장비를 다시 읽은 값(bool은 0/1)")
    readback_ts: datetime | None
    error: str | None


def _ts(ns: int | None) -> datetime | None:
    return datetime.fromtimestamp(ns / 1e9, UTC) if ns else None


def _rejected(error: WriteRejectedError) -> JSONResponse:
    body = WriteRejectedOut(
        detail=WRITE_REJECTED,
        write_id=error.write_id,
        checks=[WriteCheckOut.of(check) for check in error.checks],
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status.HTTP_409_CONFLICT)


_REJECTED: dict[int | str, dict[str, object]] = {
    status.HTTP_409_CONFLICT: {"model": WriteRejectedOut, "description": "실행 전 검사 실패"}
}


def build_writes_router(*, writes: WritesPort, guard: SessionGuard) -> APIRouter:
    router = APIRouter(prefix="/writes", tags=["writes"])
    viewer = Depends(guard.require_role(Role.VIEWER))
    operator = Depends(guard.require_role(Role.OPERATOR))

    @router.get(
        "/targets", operation_id="list_write_targets", dependencies=[viewer], responses=_AUTH_ERRORS
    )
    async def list_targets() -> WriteTargetsOut:
        return WriteTargetsOut(
            enabled=writes.enabled,
            confirm_ttl_s=writes.confirm_ttl_s,
            targets=[WriteTargetOut.of(t) for t in writes.targets()],
        )

    @router.post(
        "",
        operation_id="prepare_write",
        response_model=WritePreparedOut,
        responses={**_AUTH_ERRORS, **_REJECTED},
    )
    async def prepare(
        body: WriteRequestIn, principal: Principal = operator
    ) -> WritePreparedOut | JSONResponse:
        if isinstance(body.value, float) and not math.isfinite(body.value):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, detail="value must be a finite number"
            )
        try:
            prepared = await writes.prepare(principal, body.tag_path, body.value, body.reason)
        except WriteRejectedError as exc:
            return _rejected(exc)
        target = prepared.target
        return WritePreparedOut(
            write_id=prepared.write_id,
            tag_id=target.tag_id,
            tag_path=target.path,
            kind=target.kind.value,
            value=prepared.value,
            current=TagValueOut.of(prepared.current) if prepared.current else None,
            checks=[WriteCheckOut.of(check) for check in prepared.checks],
            expires_at=prepared.expires_at,
        )

    @router.post(
        "/{write_id}/confirm",
        operation_id="confirm_write",
        response_model=WriteResultOut,
        responses={
            **_AUTH_ERRORS,
            **_REJECTED,
            status.HTTP_404_NOT_FOUND: {
                "description": "준비된 쓰기 없음(이미 확인됨·다른 사용자가 준비함·모르는 id)"
            },
            status.HTTP_410_GONE: {"description": "확인 시간 초과 — 다시 prepare"},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "감사 기록 실패 — 보내지 않음"},
        },
    )
    async def confirm(
        write_id: _WriteId, principal: Principal = operator
    ) -> WriteResultOut | JSONResponse:
        try:
            report = await writes.confirm(principal, write_id)
        except WriteRejectedError as exc:
            return _rejected(exc)
        except WriteNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except WriteExpiredError as exc:
            raise HTTPException(status.HTTP_410_GONE, detail=str(exc)) from exc
        except WriteUnavailableError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=AUDIT_UNAVAILABLE
            ) from exc
        target = report.target
        return WriteResultOut(
            write_id=report.write_id,
            tag_id=target.tag_id,
            tag_path=target.path,
            kind=target.kind.value,
            value=report.value,
            before=TagValueOut.of(report.before) if report.before else None,
            outcome=report.outcome,
            readback=report.readback,
            readback_ts=_ts(report.readback_ts_ns),
            error=report.error,
        )

    return router
