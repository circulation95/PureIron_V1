"""`/alarms`·`/tags`·`/devices` 라우터: viewer 조회·operator ACK(401/403), ACK 404·409·503, 저널
필터 전달·경계, 응답 모양."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pureiron_core.acquisition.ports import DeviceSnapshot, DeviceState
from pureiron_core.alarms.ports import (
    AlarmNotAckableError,
    AlarmNotFoundError,
    AlarmSnapshot,
    AlarmState,
    AlarmStatus,
    AlarmType,
    AlarmUnavailableError,
    JournalQuery,
    JournalRecord,
    Priority,
)
from pureiron_core.api.alarms import build_alarms_router
from pureiron_core.api.auth import SessionGuard
from pureiron_core.api.devices import build_devices_router
from pureiron_core.api.tags import build_tags_router
from pureiron_core.auth.ports import Role

from .alarm_fakes import T0, TS0_NS, set_value, tag_store
from .auth_fakes import AuthFixture, auth_fixture

PASSWORD = "correct-horse-1"


def _alarm(state: AlarmState = AlarmState.ACTIVE_UNACKED, by: str | None = None) -> AlarmSnapshot:
    return AlarmSnapshot(
        alarm_id="tank-high",
        tag_path="plant/level",
        kind=AlarmType.ABOVE,
        setpoint=8.0,
        deadband=0.5,
        on_delay_s=0.0,
        priority=Priority.CRITICAL,
        description="탱크 수위 높음",
        state=state,
        value=9.0,
        value_ts_ns=TS0_NS,
        changed_at=T0,
        changed_by=by,
    )


@dataclass
class FakeAlarms:
    """`AlarmPort` 가짜: 조회는 정해 둔 값, ACK는 정해 둔 오류 또는 ACKED."""

    error: Exception | None = None
    acked: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    queries: list[JournalQuery] = field(default_factory=list[JournalQuery])

    def status(self) -> AlarmStatus:
        return AlarmStatus(1, 1, 0, 0)

    def alarms(self) -> tuple[AlarmSnapshot, ...]:
        return (_alarm(),)

    async def journal(self, query: JournalQuery) -> tuple[JournalRecord, ...]:
        if self.error is not None:
            raise self.error
        self.queries.append(query)
        return (
            JournalRecord(
                5, T0, "tank-high", "plant/level", "NORMAL", "ACTIVE_UNACKED", 9.0, None, None
            ),
        )

    async def ack(self, alarm_id: str, username: str) -> AlarmSnapshot:
        if self.error is not None:
            raise self.error
        self.acked.append((alarm_id, username))
        return _alarm(AlarmState.ACTIVE_ACKED, username)

    async def ack_all(self, username: str) -> tuple[AlarmSnapshot, ...]:
        if self.error is not None:
            raise self.error
        self.acked.append(("*", username))
        return (_alarm(AlarmState.ACTIVE_ACKED, username),)


@dataclass
class FakeDevices:
    def devices(self) -> tuple[DeviceSnapshot, ...]:
        return (
            DeviceSnapshot(
                "plc-1",
                "modbus_tcp",
                DeviceState.CONNECTED,
                silent=False,
                tag_count=3,
                connected=True,
                last_success_unix_ns=TS0_NS,
                read_success_rate=0.98,
            ),
        )

    def device(self, device_id: str) -> DeviceSnapshot | None:
        return None


@dataclass
class Api:
    client: TestClient
    fx: AuthFixture
    alarms: FakeAlarms


def _api(alarms: FakeAlarms | None = None) -> Api:
    fx = auth_fixture()
    guard = SessionGuard(fx.service)
    fake = alarms or FakeAlarms()
    tags = tag_store({"plant/level": 1})
    set_value(tags, 9.0, tag_id=1)
    app = FastAPI()
    app.include_router(build_alarms_router(alarms=fake, guard=guard), prefix="/api/v1")
    app.include_router(build_tags_router(tags=tags, guard=guard), prefix="/api/v1")
    app.include_router(build_devices_router(devices=FakeDevices(), guard=guard), prefix="/api/v1")
    return Api(TestClient(app), fx, fake)


async def _headers(api: Api, role: Role) -> dict[str, str]:
    username = f"user-{role.label}"
    await api.fx.service.add_user(username, PASSWORD, role)
    token = (await api.fx.service.login(username, PASSWORD)).token
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", "/api/v1/alarms"),
        ("GET", "/api/v1/alarms/journal"),
        ("POST", "/api/v1/alarms/tank-high/ack"),
        ("POST", "/api/v1/alarms/ack-all"),
        ("GET", "/api/v1/tags"),
        ("GET", "/api/v1/devices"),
    ],
)
def test_requires_session(method: str, url: str) -> None:
    assert _api().client.request(method, url).status_code == 401


async def test_viewer_reads_but_cannot_ack() -> None:
    api = _api()
    viewer = await _headers(api, Role.VIEWER)
    listed = api.client.get("/api/v1/alarms", headers=viewer)
    assert listed.status_code == 200
    [alarm] = listed.json()["alarms"]
    assert alarm == {
        "alarm_id": "tank-high",
        "tag_path": "plant/level",
        "alarm_type": "above",
        "setpoint": 8.0,
        "deadband": 0.5,
        "on_delay_s": 0.0,
        "priority": "critical",
        "description": "탱크 수위 높음",
        "state": "ACTIVE_UNACKED",
        "value": 9.0,
        "value_ts": "2026-10-03T04:00:00Z",
        "changed_at": "2026-10-11T09:00:00Z",
        "changed_by": None,
    }
    assert api.client.post("/api/v1/alarms/tank-high/ack", headers=viewer).status_code == 403
    assert api.client.post("/api/v1/alarms/ack-all", headers=viewer).status_code == 403
    assert api.alarms.acked == []


async def test_operator_acks_one_and_all() -> None:
    api = _api()
    operator = await _headers(api, Role.OPERATOR)
    one = api.client.post("/api/v1/alarms/tank-high/ack", headers=operator)
    assert one.status_code == 200
    assert (one.json()["state"], one.json()["changed_by"]) == ("ACTIVE_ACKED", "user-operator")
    every = api.client.post("/api/v1/alarms/ack-all", headers=operator)
    assert [a["alarm_id"] for a in every.json()["acknowledged"]] == ["tank-high"]
    assert api.alarms.acked == [("tank-high", "user-operator"), ("*", "user-operator")]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AlarmNotFoundError("tank-high"), 404),
        (AlarmNotAckableError("tank-high", AlarmState.ACTIVE_ACKED), 409),
        (AlarmUnavailableError("site-db error: down"), 503),
    ],
)
async def test_ack_errors_map_to_status(error: Exception, status: int) -> None:
    api = _api(FakeAlarms(error=error))
    operator = await _headers(api, Role.OPERATOR)
    response = api.client.post("/api/v1/alarms/tank-high/ack", headers=operator)
    assert response.status_code == status
    if status == 503:
        assert response.json()["detail"] == "site-db unavailable"  # DB 오류 원문은 내지 않는다


async def test_ack_rejects_malformed_alarm_id() -> None:
    api = _api()
    operator = await _headers(api, Role.OPERATOR)
    assert api.client.post("/api/v1/alarms/Bad_Id/ack", headers=operator).status_code == 422


async def test_journal_passes_filters_and_bounds_limit() -> None:
    api = _api()
    viewer = await _headers(api, Role.VIEWER)
    url = "/api/v1/alarms/journal"
    response = api.client.get(
        url,
        params={"alarm_id": "tank-high", "start": "2026-10-11T00:00:00Z", "limit": 10},
        headers=viewer,
    )
    assert response.status_code == 200
    [entry] = response.json()["entries"]
    assert (entry["to_state"], entry["username"]) == ("ACTIVE_UNACKED", None)
    [query] = api.alarms.queries
    assert (query.alarm_id, query.start, query.end, query.limit) == (
        "tank-high",
        T0.replace(hour=0),
        None,
        10,
    )
    assert api.client.get(url, params={"limit": 1001}, headers=viewer).status_code == 422
    assert (
        api.client.get(url, params={"start": "2026-10-11T00:00"}, headers=viewer).status_code == 422
    )


async def test_tags_and_devices_snapshots() -> None:
    api = _api()
    viewer = await _headers(api, Role.VIEWER)
    [tag] = api.client.get("/api/v1/tags", headers=viewer).json()["tags"]
    assert tag == {
        "tag_id": 1,
        "path": "plant/level",
        "device_id": "plc-1",
        "value": 9.0,
        "quality": "GOOD",
        "ts": "2026-10-03T04:00:00Z",
    }
    [device] = api.client.get("/api/v1/devices", headers=viewer).json()["devices"]
    assert (device["state"], device["connected"], device["read_success_rate"]) == (
        "CONNECTED",
        True,
        0.98,
    )
    assert (device["last_success_at"], device["last_error_at"], device["poll_total"]) == (
        "2026-10-03T04:00:00Z",
        None,
        None,
    )
