"""`GET /tags`: 태그 현재값 스냅샷(viewer 이상, plan M3b D10). 변화 스트림은 WS `tag_update`."""

from __future__ import annotations

from typing import Protocol

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from pureiron_core.api.auth import SessionGuard
from pureiron_core.auth.ports import Role
from pureiron_core.realtime.protocol import TagValueOut
from pureiron_core.tags.ports import TagValue


class TagSnapshotSource(Protocol):
    def snapshot(self) -> tuple[TagValue, ...]: ...


class TagList(BaseModel):
    tags: list[TagValueOut] = Field(description="현재 plan의 태그 전부(tag_id 순)")


def build_tags_router(*, tags: TagSnapshotSource, guard: SessionGuard) -> APIRouter:
    router = APIRouter(prefix="/tags", tags=["tags"])
    viewer = Depends(guard.require_role(Role.VIEWER))

    @router.get(
        "",
        operation_id="list_tags",
        dependencies=[viewer],
        responses={status.HTTP_401_UNAUTHORIZED: {"description": "not authenticated"}},
    )
    async def list_tags() -> TagList:
        return TagList(tags=[TagValueOut.of(tag) for tag in tags.snapshot()])

    return router
