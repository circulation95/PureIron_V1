"""`/project`(적용본 조회)와 `/project/edit-lock`(Updater 전용 편집 잠금), plan §6.5.

편집 잠금은 Updater 토큰(`PUREIRON_UPDATER_TOKEN_FILE`)으로만 호출할 수 있다. 토큰이 설정되지 않은
현장에서는 잠금 API가 503으로 닫혀 있다(기본 거부). 잠금 획득·해제는 감사 기록(source=updater)을
남긴다(best-effort, plan M3b D12).
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from pureiron_core.api.errors import site_db_unavailable
from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSink, AuditSource
from pureiron_core.platform.db import DatabaseUnavailableError
from pureiron_core.project.ports import EditLockConflictError, ProjectStateError, ProjectStatePort

# OpenAPI 스키마 이름은 기본값 `HTTPBearer`(= Updater token) 그대로 둔다 — 이름을 바꾸면
# oasdiff가 api-security-removed(breaking)로 거부한다. 사용자 세션은 `SessionCookie`·
# `SessionBearer`(api/auth.py).
_bearer = HTTPBearer(auto_error=False, description="Updater token")


class ProjectStateOut(BaseModel):
    project_id: str | None
    version: str | None
    content_hash: str | None
    base_site_rev: int
    site_rev: int
    edit_locked: bool
    edit_lock_owner: str | None
    edit_lock_gen: int | None
    applied_at: datetime | None


class EditLockRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)


class EditLockOut(BaseModel):
    site_rev: int


def build_project_router(
    *, store: ProjectStatePort, updater_token: str | None, audit: AuditSink
) -> APIRouter:
    router = APIRouter(prefix="/project", tags=["project"])

    async def record(action: AuditAction, owner: str, **detail: object) -> None:
        await audit.record(
            AuditEntry(action=action, source=AuditSource.UPDATER, target=owner, detail=detail)
        )

    def require_updater(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        if updater_token is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail="updater token not configured"
            )
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode(), updater_token.encode()
        ):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="invalid updater token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @router.get("", operation_id="get_project")
    async def get_project() -> ProjectStateOut:
        try:
            state = await store.get()
        except (DatabaseUnavailableError, ProjectStateError) as exc:
            raise site_db_unavailable(exc) from exc
        return ProjectStateOut(
            project_id=state.project_id,
            version=state.version,
            content_hash=state.content_hash,
            base_site_rev=state.base_site_rev,
            site_rev=state.site_rev,
            edit_locked=state.edit_lock_owner is not None,
            edit_lock_owner=state.edit_lock_owner,
            edit_lock_gen=state.edit_lock_gen,
            applied_at=state.applied_at,
        )

    @router.post(
        "/edit-lock",
        operation_id="acquire_edit_lock",
        dependencies=[Depends(require_updater)],
        responses={status.HTTP_409_CONFLICT: {"description": "held by another owner"}},
    )
    async def acquire_edit_lock(body: EditLockRequest) -> EditLockOut:
        try:
            site_rev = await store.acquire_edit_lock(body.owner, body.generation)
        except EditLockConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (DatabaseUnavailableError, ProjectStateError) as exc:
            raise site_db_unavailable(exc) from exc
        await record(
            AuditAction.PROJECT_EDIT_LOCK_ACQUIRE,
            body.owner,
            generation=body.generation,
            site_rev=site_rev,
        )
        return EditLockOut(site_rev=site_rev)

    @router.delete(
        "/edit-lock",
        operation_id="release_edit_lock",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(require_updater)],
        responses={status.HTTP_409_CONFLICT: {"description": "held by another owner"}},
    )
    async def release_edit_lock(
        owner: Annotated[str, Query(min_length=1, max_length=128)],
    ) -> Response:
        try:
            await store.release_edit_lock(owner)
        except EditLockConflictError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (DatabaseUnavailableError, ProjectStateError) as exc:
            raise site_db_unavailable(exc) from exc
        await record(AuditAction.PROJECT_EDIT_LOCK_RELEASE, owner)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
