"""`/auth` 라우터와 SessionGuard: 쿠키·Bearer 인증, 401/403 경계, Set-Cookie 속성, 감사 기록,
비밀번호 변경 검증, DB 장애 503, OpenAPI 보안 스키마 이름(세션 vs Updater)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from pureiron_core.api.auth import SESSION_COOKIE, SessionGuard, build_auth_router
from pureiron_core.api.project import build_project_router
from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSink, AuditSource
from pureiron_core.audit.store import PgAuditStore
from pureiron_core.auth.ports import Principal, Role
from pureiron_core.platform.db import Database
from pureiron_core.project.ports import ProjectState

from .auth_fakes import AuthFixture, auth_fixture
from .conftest import FakeAudit

PASSWORD = "correct-horse-1"
ME = "/api/v1/auth/me"


@dataclass
class Api:
    client: TestClient
    fx: AuthFixture
    audit: FakeAudit
    app: FastAPI


def _api(*, cookie_secure: bool = False, sink: AuditSink | None = None) -> Api:
    fx = auth_fixture()
    audit = FakeAudit()
    guard = SessionGuard(fx.service)
    app = FastAPI()
    app.include_router(
        build_auth_router(
            auth=fx.service,
            audit=sink or audit,
            guard=guard,
            cookie_secure=cookie_secure,
            cookie_max_age_s=604_800,
        ),
        prefix="/api/v1",
    )
    probe = APIRouter()
    operator = Depends(guard.require_role(Role.OPERATOR))

    @probe.get("/probe/operator")
    async def operator_only(principal: Principal = operator) -> dict[str, str]:
        return {"user": principal.username}

    app.include_router(probe, prefix="/api/v1")
    return Api(TestClient(app), fx, audit, app)


async def _user(api: Api, username: str = "alice", role: Role = Role.OPERATOR) -> None:
    await api.fx.service.add_user(username, PASSWORD, role, display_name="Alice")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(api: Api, username: str = "alice", password: str = PASSWORD) -> str:
    response = api.client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


async def test_login_sets_strict_httponly_cookie_and_returns_same_token() -> None:
    api = _api()
    await _user(api)
    response = api.client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {
        "user_id": 1,
        "username": "alice",
        "role": "operator",
        "display_name": "Alice",
    }
    assert body["expires_at"] == "2026-10-18T09:00:00Z"
    cookie = response.headers["set-cookie"]
    attributes = {part.strip().split("=")[0].lower() for part in cookie.split(";")}
    assert cookie.startswith(f"{SESSION_COOKIE}={body['token']};")
    assert {"httponly", "samesite", "path", "max-age"} <= attributes
    assert "secure" not in attributes
    assert "samesite=strict" in cookie.lower()
    assert "path=/api" in cookie.lower()
    assert "max-age=604800" in cookie.lower()


async def test_secure_cookie_flag_follows_setting() -> None:
    api = _api(cookie_secure=True)
    await _user(api)
    response = api.client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": PASSWORD}
    )
    assert "; secure" in response.headers["set-cookie"].lower()


async def test_me_works_with_cookie_and_with_bearer() -> None:
    api = _api()
    await _user(api)
    token = _login(api)  # TestClient가 쿠키를 보관한다
    assert api.client.get(ME).json()["username"] == "alice"
    fresh = TestClient(api.app)
    assert fresh.get(ME).status_code == 401
    assert fresh.get(ME, headers=_bearer(token)).json()["role"] == "operator"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "x"}])
async def test_me_without_valid_session_is_401(headers: dict[str, str]) -> None:
    api = _api()
    response = api.client.get(ME, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_failed_login_is_401_and_audited_without_password() -> None:
    api = _api()
    await _user(api)
    response = api.client.post(
        "/api/v1/auth/login", json={"username": "alice", "password": "wrong-secret-9"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid username or password"
    unknown = api.client.post(
        "/api/v1/auth/login", json={"username": "mallory", "password": "wrong-secret-9"}
    )
    assert unknown.status_code == 401
    assert unknown.json() == response.json()
    assert api.audit.entries == [
        AuditEntry(
            action=AuditAction.AUTH_LOGIN_FAILED,
            source=AuditSource.API,
            username="alice",
            target="alice",
            detail={"reason": "bad_password"},
        ),
        AuditEntry(
            action=AuditAction.AUTH_LOGIN_FAILED,
            source=AuditSource.API,
            username="mallory",
            target="mallory",
            detail={"reason": "unknown_user"},
        ),
    ]
    assert "wrong-secret-9" not in repr(api.audit.entries)


async def test_login_success_is_audited() -> None:
    api = _api()
    await _user(api)
    _login(api)
    assert [(e.action, e.username) for e in api.audit.entries] == [
        (AuditAction.AUTH_LOGIN, "alice")
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"username": "alice"},
        {"username": "", "password": PASSWORD},
        {"username": "alice", "password": "x" * 129},
        {"username": "alice", "password": PASSWORD, "extra": 1},
    ],
)
async def test_login_body_validation_is_422(body: dict[str, object]) -> None:
    assert _api().client.post("/api/v1/auth/login", json=body).status_code == 422


async def test_logout_revokes_session_and_clears_cookie() -> None:
    api = _api()
    await _user(api)
    token = _login(api)
    response = api.client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "max-age=0" in cookie
    assert api.client.get(ME, headers=_bearer(token)).status_code == 401
    assert api.audit.entries[-1].action == AuditAction.AUTH_LOGOUT


async def test_logout_with_dead_or_missing_session_still_clears_cookie() -> None:
    """만료·폐기된 쿠키도 지울 수 있어야 한다(HttpOnly라 브라우저 JS는 못 지움). 감사는 없다."""
    api = _api()
    await _user(api)
    token = _login(api)
    assert api.client.post("/api/v1/auth/logout").status_code == 204
    entries = len(api.audit.entries)
    again = TestClient(api.app).post("/api/v1/auth/logout", headers=_bearer(token))
    assert again.status_code == 204
    assert "max-age=0" in again.headers["set-cookie"].lower()
    assert TestClient(api.app).post("/api/v1/auth/logout").status_code == 204
    assert len(api.audit.entries) == entries


async def test_rejected_session_reason_is_not_disclosed() -> None:
    api = _api()
    await _user(api)
    token = _login(api)
    await api.fx.service.set_enabled("alice", False)
    response = api.client.get(ME, headers=_bearer(token))
    assert (response.status_code, response.json()) == (401, {"detail": "not authenticated"})


async def test_malformed_username_is_not_stored_in_audit() -> None:
    """username 칸에 비밀번호를 넣는 실수가 감사 로그에 평문으로 남지 않게 한다."""
    api = _api()
    response = api.client.post(
        "/api/v1/auth/login", json={"username": "My Secret Pass!", "password": "whatever-1"}
    )
    assert response.status_code == 401
    assert api.audit.entries == [
        AuditEntry(
            action=AuditAction.AUTH_LOGIN_FAILED,
            source=AuditSource.API,
            detail={"reason": "unknown_user", "username_invalid": True},
        )
    ]


async def test_disabled_user_existing_token_is_401() -> None:
    api = _api()
    await _user(api)
    token = _login(api)
    assert api.client.get(ME, headers=_bearer(token)).status_code == 200
    await api.fx.service.set_enabled("alice", False)
    assert api.client.get(ME, headers=_bearer(token)).status_code == 401
    login = api.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert login.status_code == 401


@pytest.mark.parametrize(
    ("role", "status"),
    [(Role.VIEWER, 403), (Role.OPERATOR, 200), (Role.ENGINEER, 200), (Role.ADMIN, 200)],
)
async def test_require_role_compares_levels(role: Role, status: int) -> None:
    api = _api()
    await _user(api, role=role)
    response = api.client.get("/api/v1/probe/operator", headers=_bearer(_login(api)))
    assert response.status_code == status
    if status == 403:
        assert response.json()["detail"] == "requires role operator or higher"
    else:
        assert response.json() == {"user": "alice"}
    assert TestClient(api.app).get("/api/v1/probe/operator").status_code == 401


async def test_change_password_success_and_audit_has_no_password() -> None:
    api = _api()
    await _user(api)
    token = _login(api)
    response = api.client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "secretBBB2"},
    )
    assert response.status_code == 204
    assert api.client.get(ME, headers=_bearer(token)).status_code == 200  # 이 세션은 유지
    old = api.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert old.status_code == 401
    _login(api, password="secretBBB2")
    change = [e for e in api.audit.entries if e.action == AuditAction.AUTH_PASSWORD_CHANGE]
    assert change == [
        AuditEntry(
            action=AuditAction.AUTH_PASSWORD_CHANGE,
            source=AuditSource.API,
            username="alice",
            target="alice",
        )
    ]
    serialized = json.dumps([repr(e) for e in api.audit.entries])
    assert PASSWORD not in serialized
    assert "secretBBB2" not in serialized


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"current_password": "WRONGpass1", "new_password": "brandnew99"}, 400),
        ({"current_password": PASSWORD, "new_password": "short7!"}, 422),
        ({"current_password": PASSWORD, "new_password": "x" * 129}, 422),
        ({"current_password": "   ", "new_password": "newpass1234"}, 422),
        ({"current_password": PASSWORD, "new_password": PASSWORD}, 422),
        ({"current_password": PASSWORD, "new_password": "newpass4567", "extra": "x"}, 422),
    ],
)
async def test_change_password_rejections(body: dict[str, object], status: int) -> None:
    api = _api()
    await _user(api)
    _login(api)
    response = api.client.post("/api/v1/auth/password", json=body)
    assert response.status_code == status, response.text
    if status == 400:
        assert response.json()["detail"] == "current password is incorrect"
    _login(api)  # 원래 비밀번호 그대로


async def test_change_password_requires_auth() -> None:
    response = _api().client.post(
        "/api/v1/auth/password", json={"current_password": "x", "new_password": "y" * 10}
    )
    assert response.status_code == 401


async def test_store_outage_is_503() -> None:
    api = _api()
    await _user(api)
    token = _login(api)
    api.fx.users.available = False
    assert api.client.get(ME, headers=_bearer(token)).status_code == 503
    login = api.client.post("/api/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert login.status_code == 503


async def test_requests_succeed_when_audit_store_is_down() -> None:
    """감사 기록은 best-effort: site-db에 못 써도 로그인·비밀번호 변경은 성공한다(legacy 동일)."""
    unreachable = Database("postgresql://pureiron@127.0.0.1:1/none", connect_timeout_s=1)
    api = _api(sink=PgAuditStore(unreachable))
    await _user(api)
    try:
        _login(api)
        changed = api.client.post(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "secretBBB2"},
        )
        assert changed.status_code == 204
        _login(api, password="secretBBB2")
        assert api.client.post("/api/v1/auth/logout").status_code == 204
    finally:
        await unreachable.close()


def test_openapi_names_session_and_updater_schemes_apart() -> None:
    api = _api()
    project = build_project_router(store=_NoProject(), updater_token="t", audit=FakeAudit())
    api.app.include_router(project, prefix="/api/v1")
    spec = api.app.openapi()
    schemes = spec["components"]["securitySchemes"]
    assert schemes["SessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": SESSION_COOKIE,
        "description": schemes["SessionCookie"]["description"],
    }
    assert schemes["SessionBearer"]["scheme"] == "bearer"
    assert schemes["HTTPBearer"] == {
        "type": "http",
        "scheme": "bearer",
        "description": "Updater token",
    }
    me = spec["paths"]["/api/v1/auth/me"]["get"]["security"]
    assert me == [{"SessionCookie": []}, {"SessionBearer": []}]
    lock = spec["paths"]["/api/v1/project/edit-lock"]["post"]["security"]
    assert lock == [{"HTTPBearer": []}]
    assert "security" not in spec["paths"]["/api/v1/auth/login"]["post"]


class _NoProject:
    async def get(self) -> ProjectState:
        raise AssertionError("not called")

    async def acquire_edit_lock(self, owner: str, generation: int) -> int:
        raise AssertionError("not called")

    async def release_edit_lock(self, owner: str) -> None:
        raise AssertionError("not called")
