"""`/healthz`(프로세스 생존)·`/readyz`(control-db + 스키마 epoch envelope).

부팅 때 마이그레이션을 실행하지 않으므로(불변 규칙) 스키마가 앱 envelope 밖이면 readyz가 503으로
알린다 — 운영자가 `pureiron-control migrate up`을 실행한다.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from pureiron_control import __version__
from pureiron_core.platform.db import Database
from pureiron_core.platform.migrate import SchemaGuard, SchemaUnavailableError


class Health(BaseModel):
    status: Literal["ok"]
    version: str


class SchemaCheck(BaseModel):
    ok: bool
    cur: int | None
    app_expected: int
    app_max_compatible: int
    detail: str | None = None


class Readiness(BaseModel):
    ready: bool
    db: bool
    schema_envelope: SchemaCheck


def build_health_router(*, db: Database, schema: SchemaGuard) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/healthz", operation_id="control_healthz")
    async def healthz() -> Health:
        return Health(status="ok", version=__version__)

    @router.get("/readyz", operation_id="control_readyz", responses={503: {"model": Readiness}})
    async def readyz(response: Response) -> Readiness:
        db_ok = await db.ping()
        check = SchemaCheck(
            ok=False,
            cur=None,
            app_expected=schema.app_expected,
            app_max_compatible=schema.app_max_compatible,
        )
        if db_ok:
            try:
                envelope = await schema.envelope()
                check = check.model_copy(update={"ok": envelope.ok, "cur": envelope.cur})
            except SchemaUnavailableError as exc:
                check = check.model_copy(update={"detail": str(exc)})
        ready = db_ok and check.ok
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Readiness(ready=ready, db=db_ok, schema_envelope=check)

    return router
