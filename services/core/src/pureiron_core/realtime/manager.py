"""WS 연결 상태(구독·보낼 것)를 메모리에서 관리한다(plan M3b D8·D10). 동기(await 없음)만 있다.

- bus 핸들러(`on_tag`·`on_alarm`·`on_device`)는 연결별 메모리에 넣기만 한다(I/O await 금지 — D8).
  태그는 연결별 `pending`에 태그별 최신값만 남기고(합치기) 배치 주기에 한 번 보낸다. 알람·장비
  이벤트는 인증된 모든 연결의 이벤트 큐에 넣고 보내는 쪽을 깨운다.
- 이벤트 큐가 상한(기본 1,000)을 넘으면 그 연결을 1013(try again later)으로 닫는다 — 느린
  클라이언트 하나가 메모리를 키우거나 다른 연결·ingest를 늦추지 않게.
- 실제 소켓 쓰기는 `service.py`가 `drain` 결과로 한다.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum

from pureiron_core.acquisition.ports import DeviceStatusSource
from pureiron_core.alarms.ports import AlarmSnapshot
from pureiron_core.auth.ports import Principal
from pureiron_core.platform.bus import Event
from pureiron_core.realtime.protocol import (
    MAX_TAG_IDS,
    AlarmEventMessage,
    AlarmOut,
    DeviceOut,
    DeviceUpdateMessage,
    ServerMessage,
    TagUpdateMessage,
    TagValueOut,
)
from pureiron_core.tags.ports import Quality, TagStorePort, TagValue

DEFAULT_QUEUE_MAX = 1_000


class CloseCode(IntEnum):
    """WS close code. 4xxx는 앱 정의."""

    NORMAL = 1000
    GOING_AWAY = 1001  # core 정지
    TRY_AGAIN_LATER = 1013  # 이벤트 큐 초과·site-db 장애로 인증 불가·인증 대기 연결 초과
    UNAUTHORIZED = 4401  # 인증 실패·세션 만료/폐기
    FORBIDDEN_ORIGIN = 4403  # 브라우저 Origin이 요청 Host와 다름(CSWSH 방지)
    PONG_TIMEOUT = 4408  # pong이 pong_timeout_s 동안 없음


@dataclass(eq=False, slots=True)
class Client:
    """연결 하나의 상태. 소유자는 `ConnectionManager`다: 상태를 바꾸는 일(구독·보낼 것·close·wake)은
    manager 메서드로만 하고, `service.py`는 `close_code`·`wake.wait()`를 읽기만 한다."""

    principal: Principal
    token: str
    last_pong_s: float
    subscribed: set[int] = field(default_factory=set[int])
    pending: dict[int, TagValue] = field(default_factory=dict[int, TagValue])
    events: deque[ServerMessage] = field(default_factory=deque[ServerMessage])
    # 구독 직후 현재값처럼 배치 주기를 기다리지 않고 보낼 태그 값이 있다.
    flush_now: bool = False
    close_code: CloseCode | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)


def _tag_value(data: Mapping[str, object]) -> TagValue | None:
    """`tag.changed.*` payload → TagValue(형식이 어긋나면 None)."""
    tag_id, path, device_id = data.get("tag_id"), data.get("path"), data.get("device_id")
    value, quality, ts_ns = data.get("value"), data.get("quality"), data.get("ts_ns")
    if not (isinstance(tag_id, int) and isinstance(path, str) and isinstance(device_id, str)):
        return None
    if not (isinstance(quality, int) and isinstance(ts_ns, int)):
        return None
    if value is not None and not isinstance(value, float | bool | int):
        return None
    try:
        return TagValue(tag_id, path, device_id, value, Quality(quality), ts_ns)
    except ValueError:
        return None


class ConnectionManager:
    def __init__(
        self,
        tags: TagStorePort,
        devices: DeviceStatusSource,
        *,
        queue_max: int = DEFAULT_QUEUE_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tags = tags
        self._devices = devices
        self._queue_max = queue_max
        self._clock = clock
        self._clients: set[Client] = set()

    @property
    def connections(self) -> int:
        return len(self._clients)

    def open(self, principal: Principal, token: str) -> Client:
        client = Client(principal, token, last_pong_s=self._clock())
        self._clients.add(client)
        return client

    def release(self, client: Client) -> None:
        """연결이 끝났다(목록에서 뺀다)."""
        self._clients.discard(client)

    def close(self, client: Client, code: CloseCode) -> None:
        """닫기로 표시한다: 처음 code를 남기고 보낼 것을 버린 뒤 송신 루프를 깨운다."""
        if client.close_code is None:
            client.close_code = code
        client.events.clear()
        client.pending.clear()
        client.wake.set()

    def woken(self, client: Client) -> None:
        """송신 루프가 깨어난 것을 받았다(다음 wake를 기다린다)."""
        client.wake.clear()

    def shutdown(self) -> None:
        for client in self._clients:
            self.close(client, CloseCode.GOING_AWAY)

    # --- 클라이언트 요청 ------------------------------------------------------------------
    def subscribe(self, client: Client, tag_ids: Iterable[int]) -> None:
        """구독을 더하고 새로 구독한 태그의 현재값을 곧바로 보낼 것으로 둔다."""
        added = [t for t in dict.fromkeys(tag_ids) if t not in client.subscribed]
        added = added[: max(0, MAX_TAG_IDS - len(client.subscribed))]
        client.subscribed.update(added)
        for tag_id in added:
            current = self._tags.get(tag_id)
            if current is not None:
                client.pending[tag_id] = current
                client.flush_now = True
        if client.flush_now:
            client.wake.set()

    def unsubscribe(self, client: Client, tag_ids: Iterable[int]) -> None:
        for tag_id in tag_ids:
            client.subscribed.discard(tag_id)
            client.pending.pop(tag_id, None)

    def pong(self, client: Client) -> None:
        client.last_pong_s = self._clock()

    # --- bus 핸들러(I/O 없음) -------------------------------------------------------------
    async def on_tag(self, event: Event) -> None:
        tag = _tag_value(event.payload)
        if tag is None:
            return
        for client in self._clients:
            if tag.tag_id in client.subscribed and client.close_code is None:
                client.pending[tag.tag_id] = tag

    async def on_alarm(self, event: Event) -> None:
        alarm = event.payload.get("alarm")
        if isinstance(alarm, AlarmSnapshot):
            out = AlarmOut.of(alarm)
            self._broadcast(AlarmEventMessage(type="alarm_event", **out.model_dump()))

    async def on_device(self, event: Event) -> None:
        device_id = event.payload.get("device_id")
        snapshot = self._devices.device(device_id) if isinstance(device_id, str) else None
        if snapshot is not None:
            out = DeviceOut.of(snapshot)
            self._broadcast(DeviceUpdateMessage(type="device_update", **out.model_dump()))

    def _broadcast(self, message: ServerMessage) -> None:
        for client in self._clients:
            if client.close_code is not None:
                continue
            if len(client.events) >= self._queue_max:
                self.close(client, CloseCode.TRY_AGAIN_LATER)
                continue
            client.events.append(message)
            client.wake.set()

    # --- 보낼 것 꺼내기 -------------------------------------------------------------------
    def drain(self, client: Client, *, flush_tags: bool) -> list[ServerMessage]:
        """보낼 메시지: 쌓인 알람·장비 이벤트(순서대로) + 태그 배치(주기이거나 구독 직후)."""
        messages: list[ServerMessage] = list(client.events)
        client.events.clear()
        if (flush_tags or client.flush_now) and client.pending:
            values = [TagValueOut.of(client.pending[t]) for t in sorted(client.pending)]
            client.pending.clear()
            messages.append(TagUpdateMessage(type="tag_update", values=values))
        client.flush_now = False
        return messages

    def pong_overdue(self, client: Client, timeout_s: float) -> bool:
        return self._clock() - client.last_pong_s > timeout_s
