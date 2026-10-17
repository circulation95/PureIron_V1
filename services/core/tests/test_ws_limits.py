"""WS 자원 상한(리뷰 M-2): 실제 uvicorn(`cli.http_config`)에서 프레임 상한을 넘으면 1009, 인증 전
동시 연결 상한을 넘으면 1013. TestClient는 uvicorn 프로토콜 계층을 거치지 않아 서버를 띄운다."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator

import pytest
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from pureiron_core.api.ws import WS_MAX_MESSAGE_BYTES, build_ws_router
from pureiron_core.cli import http_config
from pureiron_core.platform.bus import EventBus
from pureiron_core.realtime.service import RealtimeService

from .alarm_fakes import eventually, tag_store
from .auth_fakes import auth_fixture
from .test_realtime_manager import FakeDevices

MAX_PENDING = 2


@pytest.fixture
async def url() -> AsyncGenerator[str]:
    fx = auth_fixture()
    realtime = RealtimeService(EventBus(), tag_store(), FakeDevices(), fx.service)
    app = FastAPI()
    app.include_router(
        build_ws_router(
            auth=fx.service, realtime=realtime, auth_timeout_s=5.0, max_pending_auth=MAX_PENDING
        ),
        prefix="/api/v1",
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    server = uvicorn.Server(http_config(app, host="127.0.0.1", port=port))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    await eventually(lambda: server.started)
    yield f"ws://127.0.0.1:{port}/api/v1/ws"
    server.should_exit = True
    await task
    sock.close()


async def _close_code(ws: ClientConnection) -> int | None:
    with pytest.raises(ConnectionClosed) as excinfo:
        async with asyncio.timeout(2.0):
            await ws.recv()
    return excinfo.value.rcvd.code if excinfo.value.rcvd is not None else None


async def test_frame_over_limit_closes_with_1009_before_auth(url: str) -> None:
    async with connect(url, max_size=None) as ws:
        token = "x" * WS_MAX_MESSAGE_BYTES
        await ws.send(json.dumps({"type": "auth", "token": token}))
        assert await _close_code(ws) == 1009


async def test_frame_within_limit_is_read(url: str) -> None:
    async with connect(url) as ws:
        await ws.send(json.dumps({"type": "auth", "token": "x" * (WS_MAX_MESSAGE_BYTES // 2)}))
        assert await _close_code(ws) == 4401  # 읽고 검증까지 갔다(토큰이 틀림)


async def test_connections_over_pending_auth_limit_close_with_1013(url: str) -> None:
    waiting = [await connect(url) for _ in range(MAX_PENDING)]
    await asyncio.sleep(0.1)  # 두 연결이 첫 메시지를 기다리는 상태
    async with connect(url) as rejected:
        assert await _close_code(rejected) == 1013
    for ws in waiting:
        await ws.close()
    await asyncio.sleep(0.1)  # 인증 대기에서 빠지면 다시 받는다
    async with connect(url) as ws:
        await ws.send(json.dumps({"type": "auth", "token": "wrong"}))
        assert await _close_code(ws) == 4401
