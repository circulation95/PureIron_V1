"""`create_app(rt)`: 조립된 SiteRuntime을 HTTP 앱으로 노출한다(전역 조회 없음)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pureiron_core import __version__
from pureiron_core.api.alarms import build_alarms_router
from pureiron_core.api.audit import build_audit_router
from pureiron_core.api.auth import SessionGuard, build_auth_router
from pureiron_core.api.devices import build_devices_router
from pureiron_core.api.health import build_health_router
from pureiron_core.api.history import build_history_router
from pureiron_core.api.origin import OriginGuard
from pureiron_core.api.project import build_project_router
from pureiron_core.api.tags import build_tags_router
from pureiron_core.api.ws import build_ws_router, ws_schema_components
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
    app.add_middleware(OriginGuard)
    app.include_router(
        build_health_router(
            db=rt.db,
            schema=rt.schema,
            collector=rt.acquisition,
            historian=rt.historian,
            alarms=rt.alarms,
            realtime=rt.realtime,
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
            throttle=rt.login_throttle,
            cookie_secure=rt.settings.auth_cookie_secure,
            cookie_max_age_s=int(rt.settings.auth_session_absolute_s),
        ),
        prefix=API_PREFIX,
    )
    app.include_router(build_audit_router(audit=rt.audit, guard=guard), prefix=API_PREFIX)
    app.include_router(build_tags_router(tags=rt.tags, guard=guard), prefix=API_PREFIX)
    app.include_router(build_alarms_router(alarms=rt.alarms, guard=guard), prefix=API_PREFIX)
    app.include_router(build_devices_router(devices=rt.devices, guard=guard), prefix=API_PREFIX)
    app.include_router(build_history_router(history=rt.history, guard=guard), prefix=API_PREFIX)
    app.include_router(build_ws_router(auth=rt.auth, realtime=rt.realtime), prefix=API_PREFIX)
    _add_ws_schemas(app)
    return app


def _add_ws_schemas(app: FastAPI) -> None:
    """WS 메시지 모델을 OpenAPI components에 싣는다(WS 경로는 OpenAPI에 나오지 않는다, D10).

    `app.openapi()`는 만든 문서를 캐시해 돌려주므로 여기서 한 번 만들어 components를 더한다. REST
    응답과 같은 이름(AlarmOut 등)은 같은 스키마여야 한다 — 다르면 계약이 갈라지므로 실패시킨다.
    """
    components = app.openapi().setdefault("components", {}).setdefault("schemas", {})
    for name, schema in ws_schema_components().items():
        existing = components.setdefault(name, schema)
        if existing != schema:
            raise RuntimeError(f"OpenAPI schema {name!r} differs between REST and WS models")
