"""`create_app(rt)`: 조립된 SiteRuntime을 HTTP 앱으로 노출한다(전역 조회 없음)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pureiron_core import __version__
from pureiron_core.api.audit import build_audit_router
from pureiron_core.api.auth import SessionGuard, build_auth_router
from pureiron_core.api.health import build_health_router
from pureiron_core.api.project import build_project_router
from pureiron_core.runtime import SiteRuntime

API_PREFIX = "/api/v1"


def create_app(rt: SiteRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await rt.start()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="PureIron core", version=__version__, lifespan=lifespan)
    app.include_router(
        build_health_router(
            db=rt.db, schema=rt.schema, collector=rt.acquisition, historian=rt.historian
        ),
        prefix=API_PREFIX,
    )
    app.include_router(
        build_project_router(store=rt.project, updater_token=rt.updater_token, audit=rt.audit),
        prefix=API_PREFIX,
    )
    guard = SessionGuard(rt.auth)
    app.include_router(
        build_auth_router(
            auth=rt.auth,
            audit=rt.audit,
            guard=guard,
            cookie_secure=rt.settings.auth_cookie_secure,
            cookie_max_age_s=int(rt.settings.auth_session_absolute_s),
        ),
        prefix=API_PREFIX,
    )
    app.include_router(build_audit_router(audit=rt.audit, guard=guard), prefix=API_PREFIX)
    return app
