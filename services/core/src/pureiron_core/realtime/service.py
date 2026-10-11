"""WS 연결 하나의 수명(hello → 수신·송신 루프 → close)과 realtime 수명주기(plan M3b D10).

- 송신 루프: 알람·장비 이벤트는 깨어나는 즉시, 태그는 `batch_s`마다 합쳐서(구독 직후 현재값은
  곧바로) 보낸다. `ping_s`마다 ping, `pong_timeout_s` 동안 pong이 없으면 4408로 닫는다.
- `revalidate_s`마다 세션을 다시 검증한다: 로그아웃·만료·사용자 비활성화면 4401. site-db 장애로
  검증 못 하면 연결은 유지한다(graceful degradation — 다음 주기에 다시 본다).
- 보내기는 `SEND_TIMEOUT_S` 안에 끝나야 한다(TCP가 막힌 클라이언트를 붙잡지 않게).
- 정지: 모든 연결을 1001로 닫고 연결 루프가 끝나기를 제한 시간까지 기다린다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Protocol

from pydantic import ValidationError

from pureiron_core.acquisition.ports import DEVICE_CHANGED_PREFIX, DeviceStatusSource
from pureiron_core.alarms.ports import ALARM_CHANGED_PREFIX
from pureiron_core.auth.ports import AuthPort, AuthUnavailableError, Principal, SessionInvalidError
from pureiron_core.platform.bus import EventBus
from pureiron_core.realtime.manager import DEFAULT_QUEUE_MAX, Client, CloseCode, ConnectionManager
from pureiron_core.realtime.ports import RealtimeStatus
from pureiron_core.realtime.protocol import (
    HelloMessage,
    PingMessage,
    PongMessage,
    ServerMessage,
    SubscribeMessage,
    UnsubscribeMessage,
    WsClientMessage,
    encode,
)
from pureiron_core.tags.ports import TAG_CHANGED_PREFIX, TagStorePort

log = logging.getLogger(__name__)

DEFAULT_BATCH_MS = 250
DEFAULT_PING_S = 20.0
DEFAULT_PONG_TIMEOUT_S = 60.0
DEFAULT_REVALIDATE_S = 60.0
DEFAULT_STOP_TIMEOUT_S = 2.0
SEND_TIMEOUT_S = 10.0
CLOSE_TIMEOUT_S = 2.0


class TransportClosedError(Exception):
    """상대가 연결을 닫았거나 소켓을 더 쓸 수 없다."""


class Transport(Protocol):
    """WS 한 연결(I/O 경계 — `api/ws.py`가 FastAPI WebSocket으로, 테스트는 가짜로 만족한다)."""

    async def send(self, text: str) -> None:
        """실패하면 `TransportClosedError`."""
        ...

    async def receive(self) -> str:
        """텍스트 프레임 하나. 닫혔으면 `TransportClosedError`, 텍스트가 아니면 빈 문자열."""
        ...

    async def close(self, code: int) -> None:
        """best-effort(이미 닫혔으면 아무것도 하지 않는다)."""
        ...


class RealtimeService:
    def __init__(
        self,
        bus: EventBus,
        tags: TagStorePort,
        devices: DeviceStatusSource,
        auth: AuthPort,
        *,
        batch_ms: int = DEFAULT_BATCH_MS,
        ping_s: float = DEFAULT_PING_S,
        pong_timeout_s: float = DEFAULT_PONG_TIMEOUT_S,
        revalidate_s: float = DEFAULT_REVALIDATE_S,
        queue_max: int = DEFAULT_QUEUE_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._auth = auth
        self._batch_s = batch_ms / 1000
        self._ping_s = ping_s
        self._pong_timeout_s = pong_timeout_s
        self._revalidate_s = revalidate_s
        self._clock = clock
        self.manager = ConnectionManager(tags, devices, queue_max=queue_max, clock=clock)
        self._serving: set[asyncio.Task[object]] = set()
        self._unsubscribe: list[Callable[[], None]] = []

    async def start(self) -> None:
        self._unsubscribe = [
            self._bus.subscribe(TAG_CHANGED_PREFIX + "*", self.manager.on_tag),
            self._bus.subscribe(ALARM_CHANGED_PREFIX + "*", self.manager.on_alarm),
            self._bus.subscribe(DEVICE_CHANGED_PREFIX + "*", self.manager.on_device),
        ]

    async def stop(self, timeout_s: float = DEFAULT_STOP_TIMEOUT_S) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        self.manager.shutdown()
        if self._serving:
            _done, pending = await asyncio.wait(set(self._serving), timeout=timeout_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def status(self) -> RealtimeStatus:
        return RealtimeStatus(connections=self.manager.connections)

    # --- 연결 하나 ------------------------------------------------------------------------
    async def serve(self, transport: Transport, principal: Principal, token: str) -> None:
        """인증된 연결 하나를 끝날 때까지 돌린다(close까지 이 함수가 한다)."""
        current = asyncio.current_task()
        if current is not None:
            self._serving.add(current)
        client = self.manager.open(principal, token)
        receiver = asyncio.create_task(self._receive(transport, client))
        try:
            await self._send(transport, HelloMessage.of(principal))
            await self._loop(transport, client)
        except TransportClosedError:
            client.close(CloseCode.NORMAL)
        finally:
            self.manager.close(client)
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            with contextlib.suppress(TimeoutError, TransportClosedError):
                async with asyncio.timeout(CLOSE_TIMEOUT_S):
                    await transport.close(int(client.close_code or CloseCode.NORMAL))
            if current is not None:
                self._serving.discard(current)

    async def _loop(self, transport: Transport, client: Client) -> None:
        now = self._clock()
        next_flush, next_ping, next_check = (
            now + self._batch_s,
            now + self._ping_s,
            now + self._revalidate_s,
        )
        while client.close_code is None:
            wait_s = max(0.0, min(next_flush, next_ping, next_check) - self._clock())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(client.wake.wait(), wait_s)
            client.wake.clear()
            if client.close_code is not None:
                break
            now = self._clock()
            flush = now >= next_flush
            if flush:
                next_flush = now + self._batch_s
            for message in self.manager.drain(client, flush_tags=flush):
                await self._send(transport, message)
            if now >= next_ping:
                next_ping = now + self._ping_s
                if self.manager.pong_overdue(client, self._pong_timeout_s):
                    client.close(CloseCode.PONG_TIMEOUT)
                    break
                await self._send(transport, PingMessage(type="ping"))
            if now >= next_check:
                next_check = now + self._revalidate_s
                await self._revalidate(client)

    async def _revalidate(self, client: Client) -> None:
        try:
            await self._auth.authenticate(client.token)
        except SessionInvalidError:
            log.info("ws session of %s no longer valid; closing", client.principal.username)
            client.close(CloseCode.UNAUTHORIZED)
        except AuthUnavailableError as exc:
            log.warning("ws session revalidation skipped (site-db unavailable): %s", exc)

    async def _send(self, transport: Transport, message: ServerMessage) -> None:
        try:
            async with asyncio.timeout(SEND_TIMEOUT_S):
                await transport.send(encode(message))
        except TimeoutError as exc:
            raise TransportClosedError(f"send timed out ({SEND_TIMEOUT_S:g}s)") from exc

    async def _receive(self, transport: Transport, client: Client) -> None:
        try:
            while client.close_code is None:
                text = await transport.receive()
                try:
                    message = WsClientMessage.model_validate_json(text).root
                except ValidationError:
                    log.debug("ignoring invalid ws message from %s", client.principal.username)
                    continue
                if isinstance(message, SubscribeMessage):
                    self.manager.subscribe(client, message.tag_ids)
                elif isinstance(message, UnsubscribeMessage):
                    self.manager.unsubscribe(client, message.tag_ids)
                elif isinstance(message, PongMessage):
                    self.manager.pong(client)
        except TransportClosedError:
            client.close(CloseCode.NORMAL)
