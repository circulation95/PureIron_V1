"""`create_app(rt)`: 조립된 ControlRuntime을 HTTP 앱(`/api/v1`)으로 노출한다(전역 조회 없음)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pureiron_control import __version__
from pureiron_control.api.bundles import build_bundles_router
from pureiron_control.api.drafts import build_drafts_router
from pureiron_control.api.errors import control_db_unavailable
from pureiron_control.api.fleet import build_fleet_router
from pureiron_control.api.guard import Guard
from pureiron_control.api.health import build_health_router
from pureiron_control.api.reports import build_reports_router
from pureiron_control.api.sites import build_sites_router
from pureiron_control.runtime import ControlRuntime
from pureiron_core.platform.db import DatabaseUnavailableError

API_PREFIX = "/api/v1"


def create_app(rt: ControlRuntime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await rt.start()
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="PureIron control-api", version=__version__, lifespan=lifespan)
    app.add_exception_handler(DatabaseUnavailableError, control_db_unavailable)
    guard = Guard(rt.credentials)
    app.include_router(build_health_router(db=rt.db, schema=rt.schema), prefix=API_PREFIX)
    app.include_router(build_sites_router(registry=rt.sites, guard=guard), prefix=API_PREFIX)
    app.include_router(
        build_reports_router(registry=rt.sites, reports=rt.reports, guard=guard),
        prefix=API_PREFIX,
    )
    app.include_router(
        build_bundles_router(store=rt.bundles, guard=guard, api_prefix=API_PREFIX),
        prefix=API_PREFIX,
    )
    app.include_router(
        build_drafts_router(registry=rt.sites, store=rt.drafts, guard=guard), prefix=API_PREFIX
    )
    app.include_router(build_fleet_router(fleet=rt.fleet, guard=guard), prefix=API_PREFIX)
    return app
