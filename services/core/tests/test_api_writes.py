"""`/writes` 라우터: 401/403(viewer는 조회만), 입력 검증 422, prepare 200·409(검사 목록), confirm
200·404·409·410·503, 응답 모양."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pureiron_core.api.auth import SessionGuard
from pureiron_core.api.writes import build_writes_router
from pureiron_core.auth.ports import Principal, Role
from pureiron_core.tags.ports import Quality, TagValue, Value
from pureiron_core.writes.ports import (
    CheckName,
    PreparedWrite,
    WriteCheck,
    WriteExpiredError,
    WriteKind,
    WriteNotFoundError,
    WriteOutcome,
    WriteRejectedError,
    WriteReport,
    WritesStatus,
    WriteTarget,
    WriteUnavailableError,
)

from .auth_fakes import AuthFixture, auth_fixture
from .write_fakes import T0, TS0_NS

PASSWORD = "correct-horse-1"
WRITE_ID = "a" * 32
SETPOINT = WriteTarget(
    tag_id=10,
    path="line1/temp_sp",
    device_id="plc-1",
    kind=WriteKind.SETPOINT,
    role=Role.OPERATOR,
    data_type="float32",
    bit=False,
    minimum=20.0,
    maximum=90.0,
)
COMMAND = WriteTarget(
    tag_id=11,
    path="line1/start",
    device_id="plc-1",
    kind=WriteKind.COMMAND,
    role=Role.ENGINEER,
    data_type="bool",
    bit=True,
    values=(True,),
    permissive=("line1/remote_ok",),
)
CURRENT = TagValue(10, "line1/temp_sp", "plc-1", 25.0, Quality.GOOD, TS0_NS)
PASSED = (WriteCheck(CheckName.WRITES_ENABLED, True), WriteCheck(CheckName.DECLARED, True))
FAILED = (
    WriteCheck(CheckName.WRITES_ENABLED, True),
    WriteCheck(CheckName.DECLARED, True),
    WriteCheck(CheckName.PERMISSIVE, False, "permissive not GOOD/true: line1/remote_ok"),
)


@dataclass
class FakeWrites:
    """`WritesPort` 가짜: 받은 호출을 남기고 정해 둔 오류 또는 결과를 낸다."""

    error: Exception | None = None
    prepared: list[tuple[str, str, Value, str]] = field(
        default_factory=list[tuple[str, str, Value, str]]
    )
    confirmed: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    @property
    def enabled(self) -> bool:
        return True

    @property
    def confirm_ttl_s(self) -> float:
        return 60.0

    def targets(self) -> tuple[WriteTarget, ...]:
        return (COMMAND, SETPOINT)

    def status(self) -> WritesStatus:
        return WritesStatus(True, 0, {}, 0)

    async def prepare(
        self, principal: Principal, tag_path: str, value: Value, reason: str
    ) -> PreparedWrite:
        self.prepared.append((principal.username, tag_path, value, reason))
        if self.error is not None:
            raise self.error
        return PreparedWrite(WRITE_ID, SETPOINT, value, CURRENT, PASSED, T0)

    async def confirm(self, principal: Principal, write_id: str) -> WriteReport:
        self.confirmed.append((principal.username, write_id))
        if self.error is not None:
            raise self.error
        return WriteReport(
            write_id=write_id,
            target=SETPOINT,
            value=25.5,
            before=CURRENT,
            outcome=WriteOutcome.CONFIRMED,
            readback=25.5,
            readback_ts_ns=TS0_NS,
            error=None,
        )


@dataclass
class Api:
    client: TestClient
    fx: AuthFixture
    writes: FakeWrites


def _api(writes: FakeWrites | None = None) -> Api:
    fx = auth_fixture()
    fake = writes or FakeWrites()
    app = FastAPI()
    app.include_router(
        build_writes_router(writes=fake, guard=SessionGuard(fx.service)), prefix="/api/v1"
    )
    return Api(TestClient(app), fx, fake)


async def _headers(api: Api, role: Role) -> dict[str, str]:
    username = f"user-{role.label}"
    await api.fx.service.add_user(username, PASSWORD, role)
    token = (await api.fx.service.login(username, PASSWORD)).token
    return {"Authorization": f"Bearer {token}"}


BODY = {"tag_path": "line1/temp_sp", "value": 25.5, "reason": "온도 조정"}


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/api/v1/writes/targets"),
        ("POST", "/api/v1/writes"),
        ("POST", f"/api/v1/writes/{WRITE_ID}/confirm"),
    ],
)
def test_requires_session(method: str, url: str) -> None:
    response = _api().client.request(method, url, json=BODY)
    assert response.status_code == 401


async def test_viewer_reads_targets_but_cannot_write() -> None:
    api = _api()
    viewer = await _headers(api, Role.VIEWER)
    targets = api.client.get("/api/v1/writes/targets", headers=viewer)
    assert targets.status_code == 200
    assert targets.json() == {
        "enabled": True,
        "confirm_ttl_s": 60.0,
        "targets": [
            {
                "tag_id": 11,
                "path": "line1/start",
                "device_id": "plc-1",
                "kind": "command",
                "role": "engineer",
                "data_type": "bool",
                "min": None,
                "max": None,
                "values": [True],
                "permissive": ["line1/remote_ok"],
            },
            {
                "tag_id": 10,
                "path": "line1/temp_sp",
                "device_id": "plc-1",
                "kind": "setpoint",
                "role": "operator",
                "data_type": "float32",
                "min": 20.0,
                "max": 90.0,
                "values": [],
                "permissive": [],
            },
        ],
    }
    assert api.client.post("/api/v1/writes", json=BODY, headers=viewer).status_code == 403
    confirm = api.client.post(f"/api/v1/writes/{WRITE_ID}/confirm", headers=viewer)
    assert confirm.status_code == 403
    assert (api.writes.prepared, api.writes.confirmed) == ([], [])


async def test_prepare_returns_write_id_current_and_checks() -> None:
    api = _api()
    operator = await _headers(api, Role.OPERATOR)
    response = api.client.post("/api/v1/writes", json=BODY, headers=operator)
    assert response.status_code == 200
    assert response.json() == {
        "write_id": WRITE_ID,
        "tag_id": 10,
        "tag_path": "line1/temp_sp",
        "kind": "setpoint",
        "value": 25.5,
        "current": {
            "tag_id": 10,
            "path": "line1/temp_sp",
            "device_id": "plc-1",
            "value": 25.0,
            "quality": "GOOD",
            "ts": "2026-10-03T04:00:00Z",
        },
        "checks": [
            {"name": "writes_enabled", "ok": True, "detail": None},
            {"name": "declared", "ok": True, "detail": None},
        ],
        "expires_at": "2026-10-24T09:00:00Z",
    }
    assert api.writes.prepared == [("user-operator", "line1/temp_sp", 25.5, "온도 조정")]


async def test_value_types_pass_through() -> None:
    api = _api()
    operator = await _headers(api, Role.OPERATOR)
    for value, expected in ((True, True), (25, 25.0), (False, False)):
        body = {**BODY, "value": value}
        assert api.client.post("/api/v1/writes", json=body, headers=operator).status_code == 200
        received = api.writes.prepared[-1][2]
        assert (received, type(received)) == (expected, type(expected))


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "value": "1"},  # 문자열은 bool·숫자로 바꾸지 않는다
        {**BODY, "value": None},
        {**BODY, "reason": ""},
        {**BODY, "reason": "   "},
        {**BODY, "reason": "x" * 201},
        {"tag_path": "line1/temp_sp", "value": 1},
        {**BODY, "tag_path": "../etc"},
        {**BODY, "extra": 1},
    ],
)
async def test_prepare_validates_input(body: dict[str, object]) -> None:
    api = _api()
    operator = await _headers(api, Role.OPERATOR)
    assert api.client.post("/api/v1/writes", json=body, headers=operator).status_code == 422
    assert api.writes.prepared == []


async def test_prepare_rejects_non_finite_value() -> None:
    api = _api()
    operator = await _headers(api, Role.OPERATOR)
    response = api.client.post(
        "/api/v1/writes",
        content='{"tag_path": "line1/temp_sp", "value": NaN, "reason": "r"}',
        headers={**operator, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "value must be a finite number"}
    assert api.writes.prepared == []


async def test_prepare_rejected_is_409_with_checks() -> None:
    api = _api(FakeWrites(error=WriteRejectedError(FAILED)))
    operator = await _headers(api, Role.OPERATOR)
    response = api.client.post("/api/v1/writes", json=BODY, headers=operator)
    assert response.status_code == 409
    assert response.json() == {
        "detail": "write rejected by checks",
        "write_id": None,
        "checks": [
            {"name": "writes_enabled", "ok": True, "detail": None},
            {"name": "declared", "ok": True, "detail": None},
            {
                "name": "permissive",
                "ok": False,
                "detail": "permissive not GOOD/true: line1/remote_ok",
            },
        ],
    }


async def test_confirm_returns_outcome() -> None:
    api = _api()
    operator = await _headers(api, Role.OPERATOR)
    response = api.client.post(f"/api/v1/writes/{WRITE_ID}/confirm", headers=operator)
    assert response.status_code == 200
    body = response.json()
    assert (body["outcome"], body["readback"], body["error"]) == ("CONFIRMED", 25.5, None)
    assert body["readback_ts"] == "2026-10-03T04:00:00Z"
    assert body["before"]["value"] == 25.0
    assert (body["write_id"], body["tag_path"], body["value"]) == (WRITE_ID, "line1/temp_sp", 25.5)
    assert api.writes.confirmed == [("user-operator", WRITE_ID)]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (WriteNotFoundError(WRITE_ID), 404),
        (WriteExpiredError(WRITE_ID), 410),
        (WriteUnavailableError("down"), 503),
    ],
)
async def test_confirm_errors(error: Exception, status: int) -> None:
    api = _api(FakeWrites(error=error))
    operator = await _headers(api, Role.OPERATOR)
    response = api.client.post(f"/api/v1/writes/{WRITE_ID}/confirm", headers=operator)
    assert response.status_code == status
    if status == 503:
        assert response.json() == {"detail": "audit unavailable; write not sent"}


async def test_confirm_rejected_carries_write_id() -> None:
    api = _api(FakeWrites(error=WriteRejectedError(FAILED, WRITE_ID)))
    operator = await _headers(api, Role.OPERATOR)
    response = api.client.post(f"/api/v1/writes/{WRITE_ID}/confirm", headers=operator)
    assert response.status_code == 409
    assert response.json()["write_id"] == WRITE_ID


async def test_confirm_rejects_malformed_write_id() -> None:
    api = _api()
    operator = await _headers(api, Role.OPERATOR)
    assert api.client.post("/api/v1/writes/NOT-AN-ID/confirm", headers=operator).status_code == 422
    assert api.writes.confirmed == []
