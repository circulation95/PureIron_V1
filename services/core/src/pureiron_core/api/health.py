"""`/healthz`(프로세스 생존)·`/readyz`(DB + 스키마 epoch envelope + collector 세션), plan §6.5.

`/readyz`는 수집 집계(`acquisition`: 장비 연결 수·태그 GOOD 비율)도 함께 낸다 — Updater `checking`
단계의 GOOD 비율 기준(baseline - 2%p)이 이 값을 쓴다.
"""

from __future__ import annotations

from typing import Literal, Protocol

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from pureiron_core import __version__
from pureiron_core.acquisition.ports import AcquisitionStatus
from pureiron_core.platform.migrate import Envelope, SchemaUnavailableError


class DatabaseProbe(Protocol):
    async def ping(self) -> bool: ...


class SchemaProbe(Protocol):
    @property
    def app_expected(self) -> int: ...

    @property
    def app_max_compatible(self) -> int: ...

    async def envelope(self) -> Envelope: ...


class Health(BaseModel):
    status: Literal["ok"]
    version: str


class Check(BaseModel):
    ok: bool
    detail: str | None = None


class SchemaEnvelopeCheck(Check):
    cur: int | None
    app_expected: int
    app_max_compatible: int


class ReadinessChecks(BaseModel):
    db: Check
    schema_envelope: SchemaEnvelopeCheck
    collector: Check


class Acquisition(BaseModel):
    """수집 상태 집계(준비 판정에는 쓰지 않음 — 장비가 꺼진 현장도 ready). 태그 값은 담지 않는다."""

    plan_rev: int | None
    devices_total: int
    devices_connected: int
    tags_total: int
    tags_good: int
    good_ratio: float | None


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    checks: ReadinessChecks
    acquisition: Acquisition


async def _schema_check(db_ok: bool, schema: SchemaProbe) -> SchemaEnvelopeCheck:
    if not db_ok:
        return SchemaEnvelopeCheck(
            ok=False,
            detail="site-db unavailable",
            cur=None,
            app_expected=schema.app_expected,
            app_max_compatible=schema.app_max_compatible,
        )
    try:
        envelope = await schema.envelope()
    except SchemaUnavailableError as exc:
        return SchemaEnvelopeCheck(
            ok=False,
            detail=f"schema epoch unreadable: {exc}",
            cur=None,
            app_expected=schema.app_expected,
            app_max_compatible=schema.app_max_compatible,
        )
    detail = None
    if envelope.cur is None:
        detail = "schema not migrated (run `pureiron-core migrate up`)"
    elif not envelope.ok:
        detail = (
            f"schema epoch {envelope.cur} outside app envelope "
            f"[{envelope.app_expected}, {envelope.app_max_compatible}]"
        )
    return SchemaEnvelopeCheck(
        ok=envelope.ok,
        detail=detail,
        cur=envelope.cur,
        app_expected=envelope.app_expected,
        app_max_compatible=envelope.app_max_compatible,
    )


def build_health_router(
    *, db: DatabaseProbe, schema: SchemaProbe, collector: AcquisitionStatus
) -> APIRouter:
    router = APIRouter(tags=["ops"])

    @router.get("/healthz", operation_id="healthz")
    async def healthz() -> Health:
        return Health(status="ok", version=__version__)

    @router.get(
        "/readyz",
        operation_id="readyz",
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Readiness}},
    )
    async def readyz(response: Response) -> Readiness:
        db_ok = await db.ping()
        checks = ReadinessChecks(
            db=Check(ok=db_ok, detail=None if db_ok else "site-db unavailable"),
            schema_envelope=await _schema_check(db_ok, schema),
            collector=Check(
                ok=collector.collector_connected,
                detail=None if collector.collector_connected else "no collector session",
            ),
        )
        ready = checks.db.ok and checks.schema_envelope.ok and checks.collector.ok
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        summary = collector.summary()
        acquisition = Acquisition(
            plan_rev=summary.plan_rev,
            devices_total=summary.devices_total,
            devices_connected=summary.devices_connected,
            tags_total=summary.tags_total,
            tags_good=summary.tags_good,
            good_ratio=summary.good_ratio,
        )
        return Readiness(
            status="ready" if ready else "not_ready",
            version=__version__,
            checks=checks,
            acquisition=acquisition,
        )

    return router
