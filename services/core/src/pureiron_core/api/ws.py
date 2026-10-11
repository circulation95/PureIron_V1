"""WS `/ws`: 실시간 태그·알람·장비(plan M3b D10). 인증 후 연결은 `RealtimeService.serve`가 맡는다.

인증: 업그레이드 요청의 세션 쿠키(`pureiron_session`, 브라우저 — SameSite=Strict라 다른 사이트의
페이지는 이 쿠키로 연결하지 못한다) 또는 쿠키가 없으면 `AUTH_TIMEOUT_S` 안에 오는 첫 메시지
`{"type":"auth","token"}`. 실패는 close 4401, site-db 장애로 검증하지 못하면 1013.
WS는 OpenAPI 경로에 나오지 않으므로 메시지 모델을 `ws_schema_components()`로 components에 싣는다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, cast

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


async def _first_message_token(socket: _Socket, timeout_s: float) -> str | None:
    try:
        async with asyncio.timeout(timeout_s):
            text = await socket.receive()
        message = WsClientMessage.model_validate_json(text).root
    except (TimeoutError, TransportClosedError, ValidationError):
        return None
    return message.token if isinstance(message, AuthMessage) else None


def build_ws_router(
    *, auth: AuthPort, realtime: RealtimeServer, auth_timeout_s: float = AUTH_TIMEOUT_S
) -> APIRouter:
    router = APIRouter()

    @router.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()  # 앱 close code(4401)를 보내려면 먼저 수락해야 한다
        socket = _Socket(websocket)
        token = websocket.cookies.get(SESSION_COOKIE) or await _first_message_token(
            socket, auth_timeout_s
        )
        if not token:
            await socket.close(CloseCode.UNAUTHORIZED)
            return
        try:
            principal = await auth.authenticate(token)
        except SessionInvalidError:
            await socket.close(CloseCode.UNAUTHORIZED)
            return
        except AuthUnavailableError as exc:
            log.warning("ws auth unavailable (site-db): %s", exc)
            await socket.close(CloseCode.TRY_AGAIN_LATER)
            return
        await realtime.serve(socket, principal, token)

    return router


def ws_schema_components() -> dict[str, dict[str, object]]:
    """WS 메시지 모델의 OpenAPI component 스키마(이름 → 스키마, REST와 같은 이름은 같은 스키마)."""
    _, schema = models_json_schema(
        [(WsServerMessage, "serialization"), (WsClientMessage, "validation")],
        ref_template="#/components/schemas/{model}",
    )
    return cast("dict[str, dict[str, object]]", schema.get("$defs", {}))
