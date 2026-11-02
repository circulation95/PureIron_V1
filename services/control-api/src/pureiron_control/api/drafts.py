"""`POST /sites/{site}/drafts`(현장 token + site-draft 서명)·`GET …/drafts/latest`(admin).

본문은 `pureiron-site project export`가 만든 `draft.tar` 그대로다(M5-2). 벤더는 내려받은 파일을
`pureiron-project absorb`에 넣는다 — 서명 검증은 여기서 한 번, absorb에서 다시 한 번 한다.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from pureiron_bundle.sitesign import DRAFT_LIMIT_BYTES
from pureiron_control.api.body import binary_body, read_body
from pureiron_control.api.guard import Guard, SitePath
from pureiron_control.credentials import Role
from pureiron_control.drafts import DraftRecord, DraftSignatureError, DraftStore
from pureiron_control.platform.envelopes import claimed_draft_site
from pureiron_control.sites import PgSiteRegistry

DRAFT_MEDIA_TYPE = "application/x-tar"


class DraftOut(BaseModel):
    id: int
    site_id: str
    site_rev: int
    base_site_rev: int
    content_hash: str
    exported_at: str
    sha256: str
    size: int
    received_at: datetime


def _draft_out(record: DraftRecord) -> DraftOut:
    return DraftOut(
        id=record.id,
        site_id=record.site_id,
        site_rev=record.site_rev,
        base_site_rev=record.base_site_rev,
        content_hash=record.content_hash,
        exported_at=record.exported_at,
        sha256=record.sha256,
        size=record.size_bytes,
        received_at=record.received_at,
    )


def build_drafts_router(*, registry: PgSiteRegistry, store: DraftStore, guard: Guard) -> APIRouter:
    router = APIRouter(tags=["drafts"])
    own_site = Depends(guard.own_site())
    admin = Depends(guard.roles(Role.ADMIN))

    @router.post(
        "/sites/{site}/drafts",
        operation_id="upload_draft",
        status_code=status.HTTP_201_CREATED,
        openapi_extra=binary_body(DRAFT_MEDIA_TYPE, "현장 드래프트 `draft.tar`(site-draft 봉투)"),
        responses={400: {}, 403: {}, 413: {}},
    )
    async def upload(request: Request, site: str = own_site) -> DraftOut:
        data = await read_body(request, limit_bytes=DRAFT_LIMIT_BYTES)
        if claimed_draft_site(data) not in (None, site):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="draft is for another site")
        public = await registry.public_key(site)
        if public is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"site {site!r} is not registered"
            )
        try:
            record = await store.save(site, data, public)
        except DraftSignatureError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return _draft_out(record)

    @router.get(
        "/sites/{site}/drafts/latest",
        operation_id="get_latest_draft",
        dependencies=[admin],
        response_class=FileResponse,
        responses={200: {"content": {DRAFT_MEDIA_TYPE: {}}}, 404: {}},
    )
    async def latest(site: SitePath) -> FileResponse:
        record = await store.latest(site)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no draft uploaded")
        return FileResponse(
            store.path(record),
            media_type=DRAFT_MEDIA_TYPE,
            filename=f"{site}-draft-{record.id}.tar",
            headers={"X-PureIron-Draft-Site-Rev": str(record.site_rev)},
        )

    return router
