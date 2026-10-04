"""`/healthz`(프로세스 생존)·`/readyz`(DB + 스키마 epoch envelope + collector 세션), plan §6.5."""

from __future__ import annotations

from typing import Literal, Protocol

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from pureiron_core import __version__
from pureiron_core.acquisition.ports import CollectorSessionStatus
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


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    version: str
    checks: ReadinessChecks


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
    *, db: DatabaseProbe, schema: SchemaProbe, collector: CollectorSessionStatus
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
        return Readiness(
            status="ready" if ready else "not_ready", version=__version__, checks=checks
        )

    return router
