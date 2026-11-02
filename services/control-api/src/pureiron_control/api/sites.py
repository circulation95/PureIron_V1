"""`POST /sites`·`/sites/{site}/rekey`·`/sites/{site}/token`(admin): 현장 등록·재등록·token 재발급.

본문은 Updater `enroll`이 쓴 `enrollment.json`(`site-enrollment` 봉투) 그대로다. token 원문은 이
응답에만 나간다 — 현장 `state/secrets/control_api_token`에 넣는다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from pureiron_control.api.body import envelope_body, read_body
from pureiron_control.api.guard import Guard, SitePath
from pureiron_control.credentials import Role
from pureiron_control.platform.envelopes import claimed_site
from pureiron_control.sites import (
    Enrollment,
    EnrollmentError,
    PgSiteRegistry,
    SiteExistsError,
    SiteNotFoundError,
    parse_enrollment,
)

ENROLLMENT_LIMIT_BYTES = 64 * 1024


class SiteRegistered(BaseModel):
    site_id: str
    fingerprint: str = Field(description="site 공개키 지문(sha256(raw 32B) hex)")
    next_seq: int = Field(description="현장이 다음에 쓸 report_seq")
    token: str = Field(description="현장 온라인 token(이 응답에만 나온다)")


class SiteRekeyed(BaseModel):
    site_id: str
    fingerprint: str
    next_seq: int = Field(
        description="현장이 다음에 쓸 report_seq — `pureiron-site enroll --next-seq`로 반영"
    )


class SiteToken(BaseModel):
    site_id: str
    token: str = Field(description="새 현장 token(구 token은 즉시 무효, 이 응답에만 나온다)")


def build_sites_router(*, registry: PgSiteRegistry, guard: Guard) -> APIRouter:
    router = APIRouter(tags=["sites"])
    admin = Depends(guard.roles(Role.ADMIN))

    def parse(data: bytes) -> Enrollment:
        try:
            return parse_enrollment(data)
        except EnrollmentError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post(
        "/sites",
        operation_id="register_site",
        status_code=status.HTTP_201_CREATED,
        dependencies=[admin],
        openapi_extra=envelope_body("site-enrollment"),
        responses={400: {}, 409: {"description": "이미 등록된 현장(rekey를 쓴다)"}},
    )
    async def register(request: Request) -> SiteRegistered:
        enrollment = parse(await read_body(request, limit_bytes=ENROLLMENT_LIMIT_BYTES))
        try:
            done, token = await registry.register(enrollment)
        except SiteExistsError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return SiteRegistered(
            site_id=done.site_id, fingerprint=done.fingerprint, next_seq=done.next_seq, token=token
        )

    @router.post(
        "/sites/{site}/rekey",
        operation_id="rekey_site",
        dependencies=[admin],
        openapi_extra=envelope_body("site-enrollment"),
        responses={400: {}, 403: {}, 404: {}},
    )
    async def rekey(site: SitePath, request: Request) -> SiteRekeyed:
        data = await read_body(request, limit_bytes=ENROLLMENT_LIMIT_BYTES)
        if claimed_site(data) not in (None, site):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="enrollment is for another site")
        try:
            done = await registry.rekey(site, parse(data))
        except EnrollmentError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except SiteNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return SiteRekeyed(site_id=site, fingerprint=done.fingerprint, next_seq=done.next_seq)

    @router.post(
        "/sites/{site}/token",
        operation_id="reissue_site_token",
        dependencies=[admin],
        responses={404: {}},
    )
    async def reissue(site: SitePath) -> SiteToken:
        try:
            token = await registry.reissue_token(site)
        except SiteNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        return SiteToken(site_id=site, token=token)

    return router
