"""heartbeat·보고·폐쇄망 반입(plan M4 C7): 서명 확인 → `PgReportStore.record`(공용 함수).

- `POST /sites/{site}/heartbeat`·`/reports`: 현장 token(자기 `{site}`) + 본문 서명(`site-heartbeat`·
  `site-report`, 등록된 site 키). payload `site_id`가 경로와 다르면 403, 서명 불일치 400.
- `POST /offline-imports`(admin): Updater `report export` 파일(`site-reports` 봉투) 그대로.
- seq가 마지막 반영 seq 이하인 메시지는 이력으로만 남고 응답은 200(`outcome: history`)이다.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from pureiron_bundle.sitesign import Json, SignedDocumentError, SitePurpose, open_document
from pureiron_control.api.body import envelope_body, read_body
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
    last_seq: int = Field(description="이 현장의 마지막 반영 report_seq")


class OfflineImported(BaseModel):
    import_id: int
    site_id: str
    applied_total: int
    history_total: int
    last_seq: int


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


async def _open_signed(
    data: bytes, site: str, purpose: SitePurpose, registry: PgSiteRegistry
) -> dict[str, Json]:
    """경로 현장의 등록 키로 봉투를 연다: 다른 현장 payload 403, 미등록 404, 서명·형식 400."""
    if claimed_site(data) not in (None, site):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="payload.site_id is another site")
    public = await registry.public_key(site)
    if public is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"site {site!r} is not registered")
    try:
        payload = open_document(data, purpose, public)
    except SignedDocumentError as exc:
        raise _bad_request(exc) from exc
    if payload.get("site_id") != site:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="payload.site_id missing")
    return payload


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
        responses={400: {}, 403: {}},
    )
    async def heartbeat(request: Request, site: str = own_site) -> Recorded:
        data = await read_body(request, limit_bytes=MESSAGE_LIMIT_BYTES)
        payload = await _open_signed(data, site, SitePurpose.HEARTBEAT, registry)
        try:
            result = await reports.record(site, [parse_heartbeat(payload)], source=Source.ONLINE)
        except ReportFormatError as exc:
            raise _bad_request(exc) from exc
        return Recorded(outcome=result.outcomes[0], last_seq=result.last_seq)

    @router.post(
        "/sites/{site}/reports",
        operation_id="post_report",
        openapi_extra=envelope_body(SitePurpose.REPORT.value),
        responses={400: {}, 403: {}},
    )
    async def report(request: Request, site: str = own_site) -> Recorded:
        data = await read_body(request, limit_bytes=MESSAGE_LIMIT_BYTES)
        payload = await _open_signed(data, site, SitePurpose.REPORT, registry)
        try:
            result = await reports.record(site, [parse_report(payload)], source=Source.ONLINE)
        except ReportFormatError as exc:
            raise _bad_request(exc) from exc
        return Recorded(outcome=result.outcomes[0], last_seq=result.last_seq)

    @router.post(
        "/offline-imports",
        operation_id="import_offline_reports",
        status_code=status.HTTP_201_CREATED,
        openapi_extra=envelope_body(SitePurpose.REPORTS.value),
        responses={400: {}, 404: {}},
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
            last_seq=result.last_seq,
        )

    return router
