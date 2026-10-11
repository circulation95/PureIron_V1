"""`GET /audit`: engineer 이상만, 필터·limit 전달과 경계(1~1000), 시간대 없는 시각 거부, 503."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pureiron_core.api.audit import build_audit_router
from pureiron_core.api.auth import SessionGuard
from pureiron_core.audit.ports import AuditQuery, AuditRecord, AuditUnavailableError
from pureiron_core.auth.ports import Role

from .auth_fakes import AuthFixture, auth_fixture
from .conftest import FakeAudit

PASSWORD = "correct-horse-1"
URL = "/api/v1/audit"
RECORDS = (
    AuditRecord(
        id=2,
        time=datetime(2026, 10, 11, 9, 5, tzinfo=UTC),
        username="alice",
        action="auth.logout",
        target="alice",
        detail={},
        source="api",
    ),
    AuditRecord(
        id=1,
        time=datetime(2026, 10, 11, 9, 0, tzinfo=UTC),
        username=None,
        action="users.add",
        target="alice",
        detail={"role": "engineer"},
        source="cli",
    ),
)


class DownAudit(FakeAudit):
    async def query(self, query: AuditQuery) -> tuple[AuditRecord, ...]:
        raise AuditUnavailableError("site-db error: down")


@dataclass
class Api:
    client: TestClient
    fx: AuthFixture


def _api(audit: FakeAudit) -> Api:
    fx = auth_fixture()
    app = FastAPI()
    app.include_router(
        build_audit_router(audit=audit, guard=SessionGuard(fx.service)), prefix="/api/v1"
    )
    return Api(TestClient(app), fx)


async def _headers(api: Api, role: Role) -> dict[str, str]:
    username = f"user-{role.label}"
    await api.fx.service.add_user(username, PASSWORD, role)
    token = (await api.fx.service.login(username, PASSWORD)).token
    return {"Authorization": f"Bearer {token}"}


async def test_engineer_reads_entries_in_given_order_with_default_limit() -> None:
    audit = FakeAudit(results=RECORDS)
    api = _api(audit)
    response = api.client.get(URL, headers=await _headers(api, Role.ENGINEER))
    assert response.status_code == 200
    assert response.json() == {
        "entries": [
            {
                "id": 2,
                "time": "2026-10-11T09:05:00Z",
                "username": "alice",
                "action": "auth.logout",
                "target": "alice",
                "detail": {},
                "source": "api",
            },
            {
                "id": 1,
                "time": "2026-10-11T09:00:00Z",
                "username": None,
                "action": "users.add",
                "target": "alice",
                "detail": {"role": "engineer"},
                "source": "cli",
            },
        ]
    }
    assert audit.queries == [AuditQuery(limit=100)]


async def test_filters_are_passed_through() -> None:
    audit = FakeAudit()
    api = _api(audit)
    response = api.client.get(
        URL,
        headers=await _headers(api, Role.ADMIN),
        params={
            "username": "alice",
            "action": "auth.login_failed",
            "start": "2026-10-11T00:00:00+09:00",
            "end": "2026-10-12T00:00:00Z",
            "limit": "1000",
        },
    )
    assert response.status_code == 200
    assert audit.queries == [
        AuditQuery(
            username="alice",
            action="auth.login_failed",
            start=datetime(2026, 10, 10, 15, 0, tzinfo=UTC),
            end=datetime(2026, 10, 12, 0, 0, tzinfo=UTC),
            limit=1000,
        )
    ]


@pytest.mark.parametrize(
    "params",
    [{"limit": "0"}, {"limit": "1001"}, {"start": "2026-10-11T00:00:00"}, {"username": "u" * 65}],
)
async def test_invalid_query_is_422(params: dict[str, str]) -> None:
    audit = FakeAudit()
    api = _api(audit)
    response = api.client.get(URL, headers=await _headers(api, Role.ENGINEER), params=params)
    assert response.status_code == 422
    assert audit.queries == []


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
async def test_below_engineer_is_403(role: Role) -> None:
    audit = FakeAudit()
    api = _api(audit)
    response = api.client.get(URL, headers=await _headers(api, role))
    assert response.status_code == 403
    assert audit.queries == []


def test_anonymous_is_401() -> None:
    assert _api(FakeAudit()).client.get(URL).status_code == 401


async def test_store_outage_is_503() -> None:
    api = _api(DownAudit())
    response = api.client.get(URL, headers=await _headers(api, Role.ENGINEER))
    assert response.status_code == 503
