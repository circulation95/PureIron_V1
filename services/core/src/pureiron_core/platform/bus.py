"""core 내부 이벤트 pub/sub(불변 규칙: 이벤트 pub/sub은 core 내부에서만).

토픽은 점으로 구분하고 구독은 fnmatch 패턴(`tag.changed.*`)을 쓴다. 핸들러 예외는 기록만 하고
다른 구독자 전달을 막지 않는다. TagStore 락 안에서 publish 하지 않는다(Lock discipline).
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Event:
    topic: str
    payload: Mapping[str, object] = field(default_factory=dict[str, object])


type Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscriptions: list[tuple[str, Handler]] = []

    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        entry = (pattern, handler)
        self._subscriptions.append(entry)

        def unsubscribe() -> None:
            if entry in self._subscriptions:
                self._subscriptions.remove(entry)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        for pattern, handler in list(self._subscriptions):
            if not fnmatch.fnmatchcase(event.topic, pattern):
                continue
            try:
                await handler(event)
            except Exception:
                log.exception("event handler failed for %s", event.topic)
