"""`PATCH /project/alarms/{id}`·`/project/tags/{path}`(M5-2 D8·P1·P4): admin만(401/403), 본문 검사
422(허용 밖·알 수 없는 필드·빈 본문·`setpoint: null`·타입), 오류 매핑(423 `E_EDIT_LOCKED`·404·503),
Origin 위반 403, `null`은 그대로 전달(= 제거)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pureiron_core.api.auth import SESSION_COOKIE, SessionGuard
from pureiron_core.api.origin import OriginGuard
from pureiron_core.api.project import build_project_router
from pureiron_core.auth.ports import Role
from pureiron_core.platform.db import DatabaseUnavailableError
from pureiron_core.project.ports import (
    DefinitionNotFoundError,
    EditLockedError,
    PatchInvalidError,
    ProjectState,
    ProjectStateError,
)

from .auth_fakes import AuthFixture, auth_fixture
from .conftest import FakeAudit, FakePatcher

PASSWORD = "correct-horse-1"
ALARM_URL = "/api/v1/project/alarms/tank-high"
TAG_URL = "/api/v1/project/tags/plant/line-1/level"


class _NoStore:
    async def get(self) -> ProjectState:
        raise AssertionError("not called")

    async def acquire_edit_lock(self, owner: str, generation: int) -> int:
        raise AssertionError("not called")

    async def release_edit_lock(self, owner: str) -> None:
        raise AssertionError("not called")


@dataclass
class Api:
    client: TestClient
    fx: AuthFixture
    patcher: FakePatcher


def _api(patcher: FakePatcher | None = None) -> Api:
    fx = auth_fixture()
    fake = patcher or FakePatcher()
    app = FastAPI()
    app.add_middleware(OriginGuard)
    app.include_router(
        build_project_router(
            store=_NoStore(),
            updater_token=None,
            audit=FakeAudit(),
            patcher=fake,
            guard=SessionGuard(fx.service),
        ),
        prefix="/api/v1",
    )
    return Api(TestClient(app), fx, fake)


async def _token(api: Api, role: Role) -> str:
    username = f"user-{role.label}"
    await api.fx.service.add_user(username, PASSWORD, role)
    return (await api.fx.service.login(username, PASSWORD)).token


async def _admin(api: Api) -> dict[str, str]:
    return {"Authorization": f"Bearer {await _token(api, Role.ADMIN)}"}


def test_requires_session() -> None:
    api = _api()
    assert api.client.patch(ALARM_URL, json={"setpoint": 1}).status_code == 401
    assert api.client.patch(TAG_URL, json={"eu": "m"}).status_code == 401
    assert api.patcher.calls == []


@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR, Role.ENGINEER])
async def test_below_admin_is_403(role: Role) -> None:
    api = _api()
    headers = {"Authorization": f"Bearer {await _token(api, role)}"}
    assert api.client.patch(ALARM_URL, json={"setpoint": 1}, headers=headers).status_code == 403
    assert api.client.patch(TAG_URL, json={"eu": "m"}, headers=headers).status_code == 403
    assert api.patcher.calls == []


async def test_admin_patches_alarm_and_tag_with_actor() -> None:
    api = _api()
    headers = await _admin(api)
    alarm = api.client.patch(
        ALARM_URL, json={"setpoint": 85.5, "priority": "high", "deadband": None}, headers=headers
    )
    assert alarm.status_code == 200
    assert alarm.json() == {
        "site_rev": 8,
        "definition": {"id": "tank-high", "setpoint": 85.5, "priority": "high", "deadband": None},
    }
    tag = api.client.patch(
        TAG_URL, json={"eu": None, "record": {"deadband": 0.25}}, headers=headers
    )
    assert tag.status_code == 200
    assert api.patcher.calls == [
        (
            "alarm",
            "tank-high",
            {"setpoint": 85.5, "priority": "high", "deadband": None},
            "user-admin",
        ),
        ("tag", "plant/line-1/level", {"eu": None, "record": {"deadband": 0.25}}, "user-admin"),
    ]


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (ALARM_URL, {"enabled": False}),
        (ALARM_URL, {"type": "below"}),
        (ALARM_URL, {"tag": "plant/x"}),
        (ALARM_URL, {"id": "x"}),
        (ALARM_URL, {}),
        (ALARM_URL, {"setpoint": None}),
        (ALARM_URL, {"setpoint": "80"}),
        (ALARM_URL, {"setpoint": True}),
        (ALARM_URL, {"deadband": -1}),
        (ALARM_URL, {"priority": "urgent"}),
        (TAG_URL, {"modbus": {"address": 1}}),
        (TAG_URL, {"device": "plc-2"}),
        (TAG_URL, {"write": {"min": 0}}),
        (TAG_URL, {"path": "a/b"}),
        (TAG_URL, {}),
        (TAG_URL, {"record": {}}),
        (TAG_URL, {"record": {"mode": "x"}}),
        (TAG_URL, {"record": {"max_interval_s": 1.5}}),
        (TAG_URL, {"eu": "x" * 33}),
    ],
)
async def test_invalid_body_is_422_before_patcher(url: str, body: dict[str, object]) -> None:
    api = _api()
    response = api.client.patch(url, json=body, headers=await _admin(api))
    assert response.status_code == 422, response.text
    assert api.patcher.calls == []


async def test_invalid_path_is_422() -> None:
    api = _api()
    headers = await _admin(api)
    alarm = api.client.patch("/api/v1/project/alarms/Bad_ID", json={"setpoint": 1}, headers=headers)
    tag = api.client.patch("/api/v1/project/tags/-bad", json={"eu": "m"}, headers=headers)
    assert (alarm.status_code, tag.status_code) == (422, 422)
    assert api.patcher.calls == []


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (EditLockedError("held by 'updater'"), 423, "E_EDIT_LOCKED"),
        (
            DefinitionNotFoundError("alarm 'tank-high' not found"),
            404,
            "alarm 'tank-high' not found",
        ),
        (PatchInvalidError("$.setpoint: bad"), 422, "$.setpoint: bad"),
        (ProjectStateError("site-db error: boom"), 503, "site-db unavailable"),
        (DatabaseUnavailableError("down"), 503, "site-db unavailable"),
    ],
)
async def test_patcher_errors_map_to_status(error: Exception, status: int, detail: str) -> None:
    api = _api(FakePatcher(error=error))
    response = api.client.patch(ALARM_URL, json={"setpoint": 1}, headers=await _admin(api))
    assert (response.status_code, response.json()) == (status, {"detail": detail})


async def test_cookie_session_needs_same_origin() -> None:
    """Origin 규칙(M3c D14): 쿠키 세션의 PATCH는 Origin = Host여야 한다."""
    api = _api()
    token = await _token(api, Role.ADMIN)
    api.client.cookies.set(SESSION_COOKIE, token)
    missing = api.client.patch(ALARM_URL, json={"setpoint": 1})
    foreign = api.client.patch(
        ALARM_URL, json={"setpoint": 1}, headers={"Origin": "http://evil.example"}
    )
    assert (missing.status_code, missing.json()) == (403, {"detail": "E_ORIGIN"})
    assert (foreign.status_code, foreign.json()) == (403, {"detail": "E_ORIGIN"})
    assert api.patcher.calls == []
    same = api.client.patch(
        ALARM_URL, json={"setpoint": 1}, headers={"Origin": "http://testserver"}
    )
    assert same.status_code == 200
