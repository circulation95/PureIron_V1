"""heartbeat·보고·폐쇄망 반입(plan M4 C7): 서명 확인 → `PgReportStore.record`(공용 함수).

- `POST /sites/{site}/heartbeat`·`/reports`: 현장 token(자기 `{site}`) + 본문 서명(`site-heartbeat`·
  `site-report`, 등록된 site 키). payload `site_id`가 경로와 다르면 403, 봉투·payload 형식 위반
  400, 등록 키로 서명이 확인되지 않으면 409(`KEY_MISMATCH` — 현장이 키를 바꾸고 벤더 rekey를
  기다리는 중일 수 있다. 현장은 400과 달리 버리지 않고 다시 보낸다, M4-1 리뷰 M1).
- `POST /offline-imports`(admin): Updater `report export` 파일(`site-reports` 봉투) 그대로.
- seq가 그 종류(heartbeat·보고)의 마지막 반영 seq 이하인 메시지는 이력으로만 남고 응답은
  200(`outcome: history`)이다. 폐쇄망 반입 묶음은 보고 종류다.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from pureiron_bundle.sitesign import (
    Json,
    SignatureMismatchError,
    SignedDocumentError,
    SitePurpose,
    open_document,
)
from pureiron_control.api.body import envelope_body, read_body
from pureiron_control.api.errors import KEY_MISMATCH_RESPONSE, key_mismatch
from pureiron_control.api.guard import Guard
from pureiron_control.credentials import ApiPrincipal, Role
from pureiron_control.platform.envelopes import claimed_site
from pureiron_control.reports import (
    OfflineImport,
    PgReportStore,
    ReportFormatError,
    ReportSiteNotFoundError,
    SeqOutcome,
    Source,
    parse_heartbeat,
    parse_report,
    parse_report_bundle,
)
from pureiron_control.sites import PgSiteRegistry

MESSAGE_LIMIT_BYTES = 1024 * 1024
OFFLINE_IMPORT_LIMIT_BYTES = 32 * 1024 * 1024


class Recorded(BaseModel):
    outcome: SeqOutcome = Field(
        description="applied = 반영(current·last_attempt 갱신), history = seq ≤ last_seq라 이력만"
    )
    last_seq: int = Field(
        description="이 메시지 종류(heartbeat 또는 보고)의 마지막 반영 report_seq — 수위는 종류별"
    )


class OfflineImported(BaseModel):
    import_id: int
    site_id: str
    applied_total: int
    history_total: int
    last_seq: int = Field(description="보고 종류의 마지막 반영 report_seq")


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


def open_registered(data: bytes, site: str, purpose: SitePurpose, public: bytes) -> dict[str, Json]:
    """등록 키로 봉투를 연다: 서명 불일치 409, 봉투·payload 형식 위반 400."""
    try:
        payload = open_document(data, purpose, public)
    except SignatureMismatchError as exc:
        raise key_mismatch() from exc
    except SignedDocumentError as exc:
        raise _bad_request(exc) from exc
    if payload.get("site_id") != site:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="payload.site_id missing")
    return payload


async def _open_signed(
    data: bytes, site: str, purpose: SitePurpose, registry: PgSiteRegistry
) -> dict[str, Json]:
    """경로 현장의 등록 키로 봉투를 연다: 다른 현장 payload 403, 미등록 404, 그 밖은
    `open_registered`."""
    if claimed_site(data) not in (None, site):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="payload.site_id is another site")
    public = await registry.public_key(site)
    if public is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"site {site!r} is not registered")
    return open_registered(data, site, purpose, public)


def build_reports_router(
    *, registry: PgSiteRegistry, reports: PgReportStore, guard: Guard
) -> APIRouter:
    router = APIRouter(tags=["reports"])
    own_site = Depends(guard.own_site())
    admin = Depends(guard.roles(Role.ADMIN))

    @router.post(
        "/sites/{site}/heartbeat",
        operation_id="post_heartbeat",
        openapi_extra=envelope_body(SitePurpose.HEARTBEAT.value),
        responses={400: {}, 403: {}, **KEY_MISMATCH_RESPONSE},
    )
    async def heartbeat(request: Request, site: str = own_site) -> Recorded:
        data = await read_body(request, limit_bytes=MESSAGE_LIMIT_BYTES)
        payload = await _open_signed(data, site, SitePurpose.HEARTBEAT, registry)
        try:
            result = await reports.record(site, [parse_heartbeat(payload)], source=Source.ONLINE)
        except ReportFormatError as exc:
            raise _bad_request(exc) from exc
        return Recorded(outcome=result.outcomes[0], last_seq=result.marks.heartbeat)

    @router.post(
        "/sites/{site}/reports",
        operation_id="post_report",
        openapi_extra=envelope_body(SitePurpose.REPORT.value),
        responses={400: {}, 403: {}, **KEY_MISMATCH_RESPONSE},
    )
    async def report(request: Request, site: str = own_site) -> Recorded:
        data = await read_body(request, limit_bytes=MESSAGE_LIMIT_BYTES)
        payload = await _open_signed(data, site, SitePurpose.REPORT, registry)
        try:
            result = await reports.record(site, [parse_report(payload)], source=Source.ONLINE)
        except ReportFormatError as exc:
            raise _bad_request(exc) from exc
        return Recorded(outcome=result.outcomes[0], last_seq=result.marks.report)

    @router.post(
        "/offline-imports",
        operation_id="import_offline_reports",
        status_code=status.HTTP_201_CREATED,
        openapi_extra=envelope_body(SitePurpose.REPORTS.value),
        responses={400: {}, 404: {}, **KEY_MISMATCH_RESPONSE},
    )
    async def offline_import(request: Request, principal: ApiPrincipal = admin) -> OfflineImported:
        data = await read_body(request, limit_bytes=OFFLINE_IMPORT_LIMIT_BYTES)
        site = claimed_site(data)
        if site is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="payload.site_id missing")
        payload = await _open_signed(data, site, SitePurpose.REPORTS, registry)
        try:
            bundle = parse_report_bundle(payload)
            imported = OfflineImport(
                bundle, "sha256:" + hashlib.sha256(data).hexdigest(), principal.token_id
            )
            result = await reports.record(
                site, bundle.reports, source=Source.OFFLINE, offline=imported
            )
        except ReportFormatError as exc:
            raise _bad_request(exc) from exc
        except ReportSiteNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        if result.import_id is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="import not stored")
        return OfflineImported(
            import_id=result.import_id,
            site_id=site,
            applied_total=result.applied_total,
            history_total=result.history_total,
            last_seq=result.marks.report,
        )

    return router
