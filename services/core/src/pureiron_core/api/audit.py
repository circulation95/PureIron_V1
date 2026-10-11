"""`GET /audit`: 감사 기록 조회(engineer 이상, 최신순, plan M3b D12)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime, BaseModel, Field

from pureiron_core.api.auth import SessionGuard
from pureiron_core.audit.ports import (
    DEFAULT_QUERY_ROWS,
    MAX_QUERY_ROWS,
    AuditLog,
    AuditQuery,
    AuditUnavailableError,
)
from pureiron_core.auth.ports import Role


class AuditEntryOut(BaseModel):
    id: int
    time: datetime
    username: str | None = Field(description="행위자. Updater·CLI처럼 로그인 사용자가 없으면 null")
    action: str = Field(description="예: auth.login, auth.login_failed, users.add")
    target: str | None
    detail: dict[str, object]
    source: str = Field(description="api · cli · updater")


class AuditPage(BaseModel):
    entries: list[AuditEntryOut] = Field(description="최신순")


def build_audit_router(*, audit: AuditLog, guard: SessionGuard) -> APIRouter:
    router = APIRouter(prefix="/audit", tags=["audit"])
    engineer = Depends(guard.require_role(Role.ENGINEER))

    @router.get(
        "",
        operation_id="list_audit",
        dependencies=[engineer],
        responses={
            status.HTTP_401_UNAUTHORIZED: {"description": "not authenticated"},
            status.HTTP_403_FORBIDDEN: {"description": "requires engineer or higher"},
        },
    )
    async def list_audit(
        username: Annotated[str | None, Query(max_length=64)] = None,
        action: Annotated[str | None, Query(max_length=64)] = None,
        start: Annotated[AwareDatetime | None, Query(description="포함(>=)")] = None,
        end: Annotated[AwareDatetime | None, Query(description="제외(<)")] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_QUERY_ROWS)] = DEFAULT_QUERY_ROWS,
    ) -> AuditPage:
        try:
            records = await audit.query(
                AuditQuery(username=username, action=action, start=start, end=end, limit=limit)
            )
        except AuditUnavailableError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        return AuditPage(
            entries=[
                AuditEntryOut(
                    id=r.id,
                    time=r.time,
                    username=r.username,
                    action=r.action,
                    target=r.target,
                    detail=dict(r.detail),
                    source=r.source,
                )
                for r in records
            ]
        )

    return router
