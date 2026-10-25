"""`/project`(적용본 조회), `/project/edit-lock`(Updater 전용 편집 잠금, plan §6.5), 현장 운영 값
PATCH(`/project/alarms/{id}`·`/project/tags/{path}`, M5-2 D8·P1~P4).

편집 잠금은 Updater 토큰(`PUREIRON_UPDATER_TOKEN_FILE`)으로만 호출할 수 있다. 토큰이 설정되지 않은
현장에서는 잠금 API가 503으로 닫혀 있다(기본 거부). 잠금 획득·해제는 감사 기록(source=updater)을
남긴다(best-effort, plan M3b D12).

PATCH는 admin 세션만(Origin 규칙은 `OriginGuard`가 비안전 메서드 전체에 건다). 본문은 허용 필드만
담은 부분 객체다 — 허용 밖·알 수 없는 필드·빈 본문·`setpoint: null`은 422, 선택 필드 `null`은 그
필드 제거. 잠금 중 423 `E_EDIT_LOCKED`, 대상 없음·retire 404, site-db 문제 503. 화면은 없다(No
config UI) — 정책·트랜잭션은 `project/patch.py`.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema

from pureiron_core.api.auth import SessionGuard
from pureiron_core.api.errors import site_db_unavailable
from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSink, AuditSource
from pureiron_core.auth.ports import Principal, Role
from pureiron_core.platform.db import DatabaseUnavailableError
from pureiron_core.project.ports import (
    DefinitionNotFoundError,
    EditLockConflictError,
    EditLockedError,
    PatchInvalidError,
    PatchResult,
    ProjectPatchPort,
    ProjectStateError,
    ProjectStatePort,
)

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


# project 스키마 `$defs/id`·`$defs/tagPath`와 같은 규칙(경로 입력을 SQL 인자로 넘기기 전에 좁힌다).
_ALARM_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
_TAG_PATH_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}$"
E_EDIT_LOCKED = "E_EDIT_LOCKED"

type _AlarmId = Annotated[str, Path(pattern=_ALARM_ID_PATTERN)]
type _TagPath = Annotated[str, Path(pattern=_TAG_PATH_PATTERN)]


class _PatchBody(BaseModel):
    """허용 필드만(`extra=forbid`), JSON 타입 그대로(`strict` — "1"·true를 숫자로 바꾸지 않는다)."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def _not_empty(self) -> _PatchBody:
        if not self.model_fields_set:
            raise ValueError("patch body is empty")
        return self

    def changes(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_unset=True)


class AlarmPatch(_PatchBody):
    setpoint: float | SkipJsonSchema[None] = Field(
        default=None, description="필수 필드 — null 불가"
    )
    deadband: float | None = Field(default=None, ge=0, description="null = 제거(기본 0)")
    on_delay_s: float | None = Field(default=None, ge=0, description="null = 제거(기본 0)")
    priority: Literal["low", "medium", "high", "critical"] | None = Field(
        default=None, description="null = 제거(기본 medium)"
    )

    @field_validator("setpoint")
    @classmethod
    def _setpoint_not_null(cls, value: float | None) -> float | None:
        if value is None:
            raise ValueError("setpoint cannot be null")
        return value


class RecordPatch(_PatchBody):
    deadband: float | None = Field(default=None, ge=0, description="null = 제거")
    max_interval_s: int | None = Field(default=None, ge=1, description="null = 제거")


class TagPatch(_PatchBody):
    description: str | None = Field(default=None, max_length=500, description="null = 제거")
    eu: str | None = Field(default=None, max_length=32, description="null = 제거")
    record: RecordPatch | None = Field(default=None, description="null = record 전체 제거")


class PatchOut(BaseModel):
    site_rev: int = Field(description="PATCH 뒤 site_rev(no-op이면 그대로)")
    definition: dict[str, object] = Field(description="저장된 정규형 정의(스키마 기본값 생략)")


_PATCH_ERRORS: dict[int | str, dict[str, object]] = {
    status.HTTP_401_UNAUTHORIZED: {"description": "not authenticated"},
    status.HTTP_403_FORBIDDEN: {"description": "requires admin"},
    status.HTTP_404_NOT_FOUND: {"description": "unknown or retired definition"},
    status.HTTP_423_LOCKED: {"description": "E_EDIT_LOCKED: Updater holds the edit lock"},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "site-db unavailable"},
}


async def _patched(call: Callable[[], Awaitable[PatchResult]]) -> PatchOut:
    try:
        result = await call()
    except EditLockedError as exc:
        raise HTTPException(status.HTTP_423_LOCKED, detail=E_EDIT_LOCKED) from exc
    except DefinitionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except PatchInvalidError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except (DatabaseUnavailableError, ProjectStateError) as exc:
        raise site_db_unavailable(exc) from exc
    return PatchOut(site_rev=result.site_rev, definition=dict(result.definition))


def build_project_router(
    *,
    store: ProjectStatePort,
    updater_token: str | None,
    audit: AuditSink,
    patcher: ProjectPatchPort,
    guard: SessionGuard,
) -> APIRouter:
    router = APIRouter(prefix="/project", tags=["project"])
    admin = Depends(guard.require_role(Role.ADMIN))

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

    @router.patch("/alarms/{alarm_id}", operation_id="patch_alarm", responses=_PATCH_ERRORS)
    async def patch_alarm(
        alarm_id: _AlarmId, body: AlarmPatch, principal: Principal = admin
    ) -> PatchOut:
        changes: Mapping[str, object] = body.changes()
        return await _patched(
            lambda: patcher.patch_alarm(alarm_id, changes, username=principal.username)
        )

    @router.patch("/tags/{tag_path:path}", operation_id="patch_tag", responses=_PATCH_ERRORS)
    async def patch_tag(
        tag_path: _TagPath, body: TagPatch, principal: Principal = admin
    ) -> PatchOut:
        changes: Mapping[str, object] = body.changes()
        return await _patched(
            lambda: patcher.patch_tag(tag_path, changes, username=principal.username)
        )

    return router
