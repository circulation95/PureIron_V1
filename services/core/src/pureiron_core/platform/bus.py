"""core 내부 이벤트 pub/sub(불변 규칙: 이벤트 pub/sub은 core 내부에서만).

토픽은 점으로 구분하고 구독은 fnmatch 패턴(`tag.changed.*`)을 쓴다. 핸들러 예외는 기록만 하고
다른 구독자 전달을 막지 않는다. TagStore 락 안에서 publish 하지 않는다(Lock discipline).

핸들러 규칙(plan M3b D8): 핸들러는 발행자(ingest 루프)를 그대로 붙잡으므로 I/O를 await 하지
않는다 — 메모리 갱신·큐 적재만 한다. 핸들러가 새 이벤트를 내야 하면(알람 전이) 직접 publish
하지 않고 **돌려준다**. 버스는 지금 이벤트를 모든 구독자에게 전한 뒤 돌려받은 이벤트를 순서대로
이어서 전한다(너비 우선).
"""

from __future__ import annotations

import fnmatch
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Event:
    topic: str
    payload: Mapping[str, object] = field(default_factory=dict[str, object])


type Handler = Callable[[Event], Awaitable[Sequence[Event] | None]]


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
        pending = deque([event])
        while pending:
            current = pending.popleft()
            for pattern, handler in list(self._subscriptions):
                if not fnmatch.fnmatchcase(current.topic, pattern):
                    continue
                try:
                    follow_up = await handler(current)
                except Exception:
                    log.exception("event handler failed for %s", current.topic)
                    continue
                if follow_up:
                    pending.extend(follow_up)
