"""WS `/ws` 인증(D10): 쿠키 또는 첫 메시지 `auth`(제한 시간) → hello, 실패는 close 4401, site-db
장애는 1013, 브라우저 Origin이 Host와 다르면 4403. 연결 뒤 구독하면 현재값이 곧바로 온다. WS
메시지 모델이 OpenAPI components에 실린다."""

from __future__ import annotations

import json
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pureiron_core.api.ws import build_ws_router, ws_schema_components
from pureiron_core.auth.ports import Role
from pureiron_core.platform.bus import EventBus
from pureiron_core.realtime.service import RealtimeService

from .alarm_fakes import set_value, tag_store
from .auth_fakes import AuthFixture, auth_fixture
from .test_realtime_manager import FakeDevices

PASSWORD = "correct-horse-1"
URL = "/api/v1/ws"


async def _app(fx: AuthFixture) -> tuple[TestClient, str]:
    tags = tag_store({"plant/level": 1})
    set_value(tags, 4.5, tag_id=1)
    realtime = RealtimeService(EventBus(), tags, FakeDevices(), fx.service, batch_ms=20)
    app = FastAPI()
    app.include_router(
        build_ws_router(auth=fx.service, realtime=realtime, auth_timeout_s=0.2), prefix="/api/v1"
    )
    await fx.service.add_user("kim", PASSWORD, Role.VIEWER)
    token = (await fx.service.login("kim", PASSWORD)).token
    return TestClient(app), token


async def test_cookie_session_gets_hello_and_current_values_on_subscribe() -> None:
    client, token = await _app(auth_fixture())
    client.cookies.set("pureiron_session", token)
    with client.websocket_connect(URL) as ws:
        assert ws.receive_json() == {"type": "hello", "username": "kim", "role": "viewer"}
        ws.send_text(json.dumps({"type": "subscribe", "tag_ids": [1]}))
        update = ws.receive_json()
        assert update["type"] == "tag_update"
        assert [(v["tag_id"], v["value"], v["quality"]) for v in update["values"]] == [
            (1, 4.5, "GOOD")
        ]


async def test_first_message_auth_without_cookie() -> None:
    client, token = await _app(auth_fixture())
    with client.websocket_connect(URL) as ws:
        ws.send_text(json.dumps({"type": "auth", "token": token}))
        assert ws.receive_json()["type"] == "hello"


@pytest.mark.parametrize("first", [None, {"type": "auth", "token": "wrong"}, {"type": "pong"}])
async def test_auth_failures_close_with_4401(first: dict[str, str] | None) -> None:
    client, _ = await _app(auth_fixture())
    with client.websocket_connect(URL) as ws:
        if first is not None:
            ws.send_text(json.dumps(first))
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()  # 첫 메시지가 없으면 제한 시간(0.2s) 뒤 닫힌다
    assert excinfo.value.code == 4401


@pytest.mark.parametrize(
    ("origin", "accepted"),
    [
        ("http://testserver", True),  # TestClient의 Host: testserver
        ("http://testserver:80", True),
        ("HTTP://TestServer", True),
        ("https://testserver", False),  # 기본 포트 443 ≠ Host의 80
        ("http://testserver:8080", False),  # 같은 호스트의 다른 포트 웹앱(same-site)
        ("http://evil.example", False),
        ("null", False),
    ],
)
async def test_browser_origin_must_match_host(origin: str, accepted: bool) -> None:
    """리뷰 L-A2: Origin이 있으면 host:port가 Host와 같아야 한다(없으면 허용 — 위 테스트들)."""
    client, token = await _app(auth_fixture())
    client.cookies.set("pureiron_session", token)
    with client.websocket_connect(URL, headers={"origin": origin}) as ws:
        if accepted:
            assert ws.receive_json()["type"] == "hello"
            return
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4403


async def test_site_db_down_closes_with_1013() -> None:
    fx = auth_fixture()
    client, token = await _app(fx)
    fx.users.available = False
    client.cookies.set("pureiron_session", token)
    with client.websocket_connect(URL) as ws, pytest.raises(WebSocketDisconnect) as excinfo:
        ws.receive_json()
    assert excinfo.value.code == 1013


def test_ws_message_models_are_openapi_components() -> None:
    components = ws_schema_components()
    assert {"WsServerMessage", "WsClientMessage", "AlarmEventMessage", "TagUpdateMessage"} <= set(
        components
    )
    discriminator = cast(
        "dict[str, dict[str, str]]", components["WsServerMessage"]["discriminator"]
    )
    assert sorted(discriminator["mapping"]) == [
        "alarm_event",
        "device_update",
        "hello",
        "ping",
        "tag_update",
    ]
