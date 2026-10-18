"""로그인 실패 제한(plan M3c D16): 같은 username이 `window_s` 안에 `max_failures`번 실패하면 그 뒤
`block_s` 동안 로그인을 받지 않는다(api가 429 + `Retry-After`로 답한다).

- 메모리에만 둔다(단일 core, 재기동하면 초기화). 새 의존성 없음.
- 영구 잠금은 하지 않는다: 누가 운전원 이름으로 틀린 비밀번호를 계속 넣어도 그 계정은
  `block_s`마다 다시 열린다(계정 DoS 방지). 성공하면 그 username의 실패 기록을 지운다.
- 결과를 기다리는 시도도 실패처럼 센다(진행 중 + 실패 ≥ 상한이면 거부) — 동시에 보낸 추측
  여러 개가 검사를 함께 통과하지 못하게. 결과를 끝내 알리지 못한 시도(연결 끊김 등)는
  `window_s`가 지나면 저절로 빠진다.
- 추적하는 username은 `max_tracked`개까지다. 넘으면 기록이 빈 항목을 먼저 지우고, 그래도
  가득하면 가장 먼저 추적한 항목을 버린다(아무 이름이나 뿌려 core 메모리를 키우지 못하게).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

DEFAULT_MAX_FAILURES = 5
DEFAULT_WINDOW_S = 600.0
DEFAULT_BLOCK_S = 600.0
DEFAULT_MAX_TRACKED = 10_000


@dataclass(slots=True)
class _Attempts:
    failures: deque[float] = field(default_factory=deque[float])  # 실패 시각
    pending: deque[float] = field(default_factory=deque[float])  # 결과를 기다리는 시도의 시작 시각
    blocked_until: float | None = None

    def expire(self, now: float, window_s: float) -> None:
        for times in (self.failures, self.pending):
            while times and times[0] <= now - window_s:
                times.popleft()
        if self.blocked_until is not None and now >= self.blocked_until:
            self.blocked_until = None

    @property
    def idle(self) -> bool:
        return not self.failures and not self.pending and self.blocked_until is None


class LoginThrottle:
    """username별 로그인 실패 제한. 메서드는 동기라 이벤트 루프 안에서 끊기지 않는다.

    사용: `begin` → (`auth.login` 결과에 따라) `succeeded` | `failed` | `abandoned`.
    """

    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_s: float = DEFAULT_WINDOW_S,
        block_s: float = DEFAULT_BLOCK_S,
        max_tracked: int = DEFAULT_MAX_TRACKED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self.block_s = block_s
        self._max_tracked = max_tracked
        self._clock = clock
        self._entries: dict[str, _Attempts] = {}

    def begin(self, username: str) -> float | None:
        """시도를 시작한다. 받지 않으면 다시 시도할 수 있을 때까지 남은 초를 돌려준다."""
        now = self._clock()
        entry = self._entries.get(username)
        if entry is None:
            entry = self._track(username, now)
        else:
            entry.expire(now, self.window_s)
            if entry.blocked_until is not None:
                return entry.blocked_until - now
            if len(entry.failures) + len(entry.pending) >= self.max_failures:
                return self.block_s  # 진행 중인 시도가 상한을 채웠다(동시 요청)
        entry.pending.append(now)
        return None

    def failed(self, username: str) -> bool:
        """실패를 기록한다. 이 실패로 제한이 시작되면 True(호출자가 감사를 남긴다)."""
        now = self._clock()
        entry = self._finish(username, now)
        if entry is None or entry.blocked_until is not None:
            return False
        entry.failures.append(now)
        if len(entry.failures) < self.max_failures:
            return False
        entry.failures.clear()
        entry.blocked_until = now + self.block_s
        return True

    def succeeded(self, username: str) -> None:
        entry = self._finish(username, self._clock())
        if entry is not None:
            entry.failures.clear()
            if entry.idle:
                del self._entries[username]

    def abandoned(self, username: str) -> None:
        """결과를 알 수 없는 시도(site-db 장애)는 실패로 세지 않는다."""
        entry = self._finish(username, self._clock())
        if entry is not None and entry.idle:
            del self._entries[username]

    def _finish(self, username: str, now: float) -> _Attempts | None:
        entry = self._entries.get(username)
        if entry is None:
            return None  # 추적 상한으로 버려졌다
        entry.expire(now, self.window_s)
        if entry.pending:
            entry.pending.popleft()
        return entry

    def _track(self, username: str, now: float) -> _Attempts:
        if len(self._entries) >= self._max_tracked:
            for name, entry in list(self._entries.items()):
                entry.expire(now, self.window_s)
                if entry.idle:
                    del self._entries[name]
            if len(self._entries) >= self._max_tracked:
                del self._entries[next(iter(self._entries))]
        entry = _Attempts()
        self._entries[username] = entry
        return entry
