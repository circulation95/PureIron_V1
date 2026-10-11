"""WS `/ws`: 실시간 태그·알람·장비(plan M3b D10). 인증 후 연결은 `RealtimeService.serve`가 맡는다.

인증: 업그레이드 요청의 세션 쿠키(`pureiron_session`, 브라우저 — SameSite=Strict라 다른 사이트의
페이지는 이 쿠키로 연결하지 못한다) 또는 쿠키가 없으면 `AUTH_TIMEOUT_S` 안에 오는 첫 메시지
`{"type":"auth","token"}`. 실패는 close 4401, site-db 장애로 검증하지 못하면 1013.
Origin: 헤더가 있고(브라우저) 그 host:port가 요청 `Host`와 다르면 4403 — 같은 PC의 다른 포트
웹앱은 same-site라 Strict 쿠키가 실린다(CSWSH). Origin이 없으면(비브라우저 클라이언트) 허용한다.
프록시는 브라우저의 Host를 그대로 넘겨야 한다(nginx `$http_host`, vite `changeOrigin` 끔).
자원: 프레임은 `WS_MAX_MESSAGE_BYTES`까지(넘으면 uvicorn이 1009로 닫는다, 설정은 `cli.http_config`),
인증을 마치지 않은 연결은 동시에 `MAX_PENDING_AUTH`개까지(넘으면 1013).
WS는 OpenAPI 경로에 나오지 않으므로 메시지 모델을 `ws_schema_components()`로 components에 싣는다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from pydantic.json_schema import models_json_schema
from starlette.websockets import WebSocketState

from pureiron_core.api.auth import SESSION_COOKIE
from pureiron_core.auth.ports import AuthPort, AuthUnavailableError, Principal, SessionInvalidError
from pureiron_core.realtime.manager import CloseCode
from pureiron_core.realtime.protocol import AuthMessage, WsClientMessage, WsServerMessage
from pureiron_core.realtime.service import Transport, TransportClosedError

log = logging.getLogger(__name__)

AUTH_TIMEOUT_S = 5.0
# 가장 큰 정상 메시지는 subscribe tag_ids 10,000개 ≈ 110 KB — 두 배 남짓 여유를 둔다.
WS_MAX_MESSAGE_BYTES = 256 * 1024
# 인증 전(쿠키 검증·첫 메시지 대기) 동시 연결 상한: 비인증 연결이 쓰는 메모리·task의 상한.
MAX_PENDING_AUTH = 32


class RealtimeServer(Protocol):
    async def serve(self, transport: Transport, principal: Principal, token: str) -> None: ...


class _Socket:
    """FastAPI WebSocket → `realtime.service.Transport`."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket

    async def send(self, text: str) -> None:
        try:
            await self._ws.send_text(text)
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            raise TransportClosedError(str(exc)) from exc

    async def receive(self) -> str:
        try:
            message = await self._ws.receive()
        except (RuntimeError, OSError) as exc:
            raise TransportClosedError(str(exc)) from exc
        if message["type"] == "websocket.disconnect":
            raise TransportClosedError("peer closed")
        text: object = message.get("text")
        return text if isinstance(text, str) else ""

    async def close(self, code: int) -> None:
        if self._ws.application_state is not WebSocketState.CONNECTED:
            return
        try:
            await self._ws.close(code)
        except (RuntimeError, OSError) as exc:
            raise TransportClosedError(str(exc)) from exc


def _same_origin(origin: str, host: str, *, secure: bool) -> bool:
    """Origin의 host:port가 요청 Host와 같은가. 포트가 없으면 각자 scheme의 기본 포트다(Origin은
    http/https, Host는 이 요청의 ws/wss)."""
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False  # "null"(sandbox·file) 등
    requested = urlsplit("//" + host)
    try:
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host_port = requested.port or (443 if secure else 80)
    except ValueError:
        return False
    return (parsed.hostname, origin_port) == (requested.hostname, host_port)


async def _first_message_token(socket: _Socket, timeout_s: float) -> str | None:
    try:
        async with asyncio.timeout(timeout_s):
            text = await socket.receive()
        message = WsClientMessage.model_validate_json(text).root
    except (TimeoutError, TransportClosedError, ValidationError):
        return None
    return message.token if isinstance(message, AuthMessage) else None


async def _authenticate(
    websocket: WebSocket, socket: _Socket, auth: AuthPort, timeout_s: float
) -> tuple[Principal, str] | None:
    """인증에 성공하면 (principal, token), 실패하면 close code를 보내고 None."""
    token = websocket.cookies.get(SESSION_COOKIE) or await _first_message_token(socket, timeout_s)
    if not token:
        await socket.close(CloseCode.UNAUTHORIZED)
        return None
    try:
        return await auth.authenticate(token), token
    except SessionInvalidError:
        await socket.close(CloseCode.UNAUTHORIZED)
    except AuthUnavailableError as exc:
        log.warning("ws auth unavailable (site-db): %s", exc)
        await socket.close(CloseCode.TRY_AGAIN_LATER)
    return None


def build_ws_router(
    *,
    auth: AuthPort,
    realtime: RealtimeServer,
    auth_timeout_s: float = AUTH_TIMEOUT_S,
    max_pending_auth: int = MAX_PENDING_AUTH,
) -> APIRouter:
    router = APIRouter()
    pending = 0  # 인증 중인 연결 수(이벤트 루프 하나에서만 바뀐다)

    @router.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        nonlocal pending
        await websocket.accept()  # 앱 close code(4401)를 보내려면 먼저 수락해야 한다
        socket = _Socket(websocket)
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host", "")
        secure = websocket.url.scheme == "wss"
        if origin is not None and not _same_origin(origin, host, secure=secure):
            log.warning("ws rejected: origin %r does not match host", origin)
            await socket.close(CloseCode.FORBIDDEN_ORIGIN)
            return
        if pending >= max_pending_auth:
            log.warning("ws rejected: %d connections already authenticating", pending)
            await socket.close(CloseCode.TRY_AGAIN_LATER)
            return
        pending += 1
        try:
            authenticated = await _authenticate(websocket, socket, auth, auth_timeout_s)
        finally:
            pending -= 1
        if authenticated is not None:
            await realtime.serve(socket, *authenticated)

    return router


def ws_schema_components() -> dict[str, dict[str, object]]:
    """WS 메시지 모델의 OpenAPI component 스키마(이름 → 스키마, REST와 같은 이름은 같은 스키마)."""
    _, schema = models_json_schema(
        [(WsServerMessage, "serialization"), (WsClientMessage, "validation")],
        ref_template="#/components/schemas/{model}",
    )
    return cast("dict[str, dict[str, object]]", schema.get("$defs", {}))
