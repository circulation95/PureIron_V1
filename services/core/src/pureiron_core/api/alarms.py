"""`/alarms`: 알람·저널 조회(viewer 이상), ACK·ACK all(operator 이상), plan M3b D7.

ACK는 저널 행·감사(`alarm.ack`·`alarm.ack_all`)를 한 트랜잭션으로 쓴 뒤에만 상태가 바뀐다. 이미
NORMAL·ACTIVE_ACKED인 알람의 ACK는 **409**다: 화면이 오래됐거나 다른 사용자가 먼저 ACK한 것이라
운전원이 알아야 하고, no-op 200이면 감사에 남지 않는 "확인" 클릭이 성공처럼 보인다. ACK all은
ACK할 것만 골라 하므로 0건이어도 200이다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import AwareDatetime, BaseModel, Field

from pureiron_core.alarms.ports import (
    DEFAULT_JOURNAL_ROWS,
    MAX_JOURNAL_ROWS,
    AlarmNotAckableError,
    AlarmNotFoundError,
    AlarmPort,
    AlarmUnavailableError,
    JournalQuery,
)
from pureiron_core.api.auth import SessionGuard
from pureiron_core.api.errors import site_db_unavailable
from pureiron_core.auth.ports import Principal, Role
from pureiron_core.realtime.protocol import AlarmOut

# project 스키마 `$defs/id`와 같은 규칙(경로 입력을 그대로 SQL 인자로 넘기기 전에 좁힌다).
_ALARM_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"

type _AlarmId = Annotated[str, Path(pattern=_ALARM_ID_PATTERN)]

_AUTH_ERRORS: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "not authenticated"},
    status.HTTP_403_FORBIDDEN: {"description": "requires a higher role"},
}


class AlarmList(BaseModel):
    alarms: list[AlarmOut] = Field(
        description="NORMAL이 아닌 알람(활성 + 해제됐지만 미확인): 우선순위 높은 것 → 최근 전이 순"
    )


class JournalEntryOut(BaseModel):
    id: int
    time: datetime
    alarm_id: str
    tag_path: str
    from_state: str
    to_state: str = Field(description="NORMAL·ACTIVE_UNACKED·ACTIVE_ACKED·CLEARED_UNACKED·RETIRED")
    value: float | None
    username: str | None = Field(description="ACK한 사용자. 조건 변화로 생긴 전이는 null")
    note: str | None


class JournalPage(BaseModel):
    entries: list[JournalEntryOut] = Field(description="최신순")


class AckAllOut(BaseModel):
    acknowledged: list[AlarmOut] = Field(description="이번에 ACK한 알람(ACK 뒤 상태)")


def build_alarms_router(*, alarms: AlarmPort, guard: SessionGuard) -> APIRouter:
    router = APIRouter(prefix="/alarms", tags=["alarms"])
    viewer = Depends(guard.require_role(Role.VIEWER))
    operator = Depends(guard.require_role(Role.OPERATOR))

    @router.get("", operation_id="list_alarms", dependencies=[viewer], responses=_AUTH_ERRORS)
    async def list_alarms() -> AlarmList:
        return AlarmList(alarms=[AlarmOut.of(a) for a in alarms.alarms()])

    @router.get(
        "/journal", operation_id="list_alarm_journal", dependencies=[viewer], responses=_AUTH_ERRORS
    )
    async def list_journal(
        alarm_id: Annotated[str | None, Query(pattern=_ALARM_ID_PATTERN)] = None,
        start: Annotated[AwareDatetime | None, Query(description="포함(>=)")] = None,
        end: Annotated[AwareDatetime | None, Query(description="제외(<)")] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_JOURNAL_ROWS)] = DEFAULT_JOURNAL_ROWS,
    ) -> JournalPage:
        try:
            records = await alarms.journal(
                JournalQuery(alarm_id=alarm_id, start=start, end=end, limit=limit)
            )
        except AlarmUnavailableError as exc:
            raise site_db_unavailable(exc) from exc
        return JournalPage(
            entries=[
                JournalEntryOut(
                    id=r.id,
                    time=r.time,
                    alarm_id=r.alarm_id,
                    tag_path=r.tag_path,
                    from_state=r.from_state,
                    to_state=r.to_state,
                    value=r.value,
                    username=r.username,
                    note=r.note,
                )
                for r in records
            ]
        )

    @router.post(
        "/ack-all",
        operation_id="ack_all_alarms",
        responses={
            **_AUTH_ERRORS,
            status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "site-db unavailable"},
        },
    )
    async def ack_all(principal: Principal = operator) -> AckAllOut:
        try:
            acked = await alarms.ack_all(principal.username)
        except AlarmUnavailableError as exc:
            raise site_db_unavailable(exc) from exc
        return AckAllOut(acknowledged=[AlarmOut.of(a) for a in acked])

    @router.post(
        "/{alarm_id}/ack",
        operation_id="ack_alarm",
        responses={
            **_AUTH_ERRORS,
            status.HTTP_404_NOT_FOUND: {"description": "unknown alarm"},
            status.HTTP_409_CONFLICT: {"description": "nothing to acknowledge (NORMAL·ACKED)"},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "site-db unavailable"},
        },
    )
    async def ack(alarm_id: _AlarmId, principal: Principal = operator) -> AlarmOut:
        try:
            return AlarmOut.of(await alarms.ack(alarm_id, principal.username))
        except AlarmNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except AlarmNotAckableError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except AlarmUnavailableError as exc:
            raise site_db_unavailable(exc) from exc

    return router
