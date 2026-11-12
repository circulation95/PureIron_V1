"""G2 pytest 세션 동안 Windows 최저 가용 메모리(M6-3a B-2) — 배경 스레드가 `SAMPLE_S`마다
`preflight.windows_mem_gb`를 재고, 세션이 끝나면 결과 디렉터리의 `MEMORY_FILE`에 남긴다(재시도
pytest가 또 돌면 두 세션을 합친다 — 최저값·표본 수). nightly `step_run`이 `summary.json`의
`windows_memory`에 싣는다.

표본 간격: 측정 한 번이 PowerShell 프로세스 하나(실측 약 0.4초·잠깐 수십 MB)라 20초면 CPU 약 2%
이하이고, 낮게 떨어지는 구간(HEAD 빌드·VM 시작·복원·번들 빌드)은 수십 초 이상 이어져 놓치지 않는다.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harness import preflight

MEMORY_FILE = "windows-memory.json"
SAMPLE_S = 20.0
JOIN_S = 90.0
"""멈출 때 마지막 표본(PowerShell 시간 제한 60초)을 기다리는 최대 시간."""


class MemoryWatch:
    """`start` → (배경에서 `sample`) → `stop`이 요약을 돌려준다. 단위 시험은 `sample`을 직접
    부른다."""

    def __init__(
        self,
        read: Callable[[], float | None] | None = None,
        *,
        interval_s: float = SAMPLE_S,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._interval = interval_s
        self._now = now
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = clock()
        self.samples = 0
        self.unread = 0
        self.min_gb: float | None = None
        self.min_at: str | None = None

    def sample(self) -> None:
        value = (self._read or preflight.windows_mem_gb)()
        with self._lock:
            if value is None:
                self.unread += 1
                return
            self.samples += 1
            if self.min_gb is None or value < self.min_gb:
                self.min_gb, self.min_at = value, self._now().isoformat(timespec="seconds")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self._interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="g2-memwatch", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, object]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(JOIN_S)
        with self._lock:
            return {
                "min_gb": self.min_gb,
                "min_at": self.min_at,
                "samples": self.samples,
                "unread": self.unread,
                "interval_s": self._interval,
                "seconds": round(self._clock() - self._started),
                "sessions": 1,
            }


def merge(old: dict[str, object] | None, new: dict[str, object]) -> dict[str, object]:
    """같은 run의 두 세션(첫 실행·infra 재시도)을 합친다: 최저값은 둘 중 낮은 것(그 시각과 함께),
    표본·못 읽은 수·초·세션 수는 더한다."""
    if not old:
        return dict(new)

    def number(data: dict[str, object], key: str) -> float:
        value = data.get(key)
        return float(value) if isinstance(value, int | float) else 0.0

    lows = [d for d in (old, new) if isinstance(d.get("min_gb"), int | float)]
    low = min(lows, key=lambda d: number(d, "min_gb")) if lows else new
    return {
        **new,
        "min_gb": low.get("min_gb"),
        "min_at": low.get("min_at"),
        **{
            key: int(number(old, key) + number(new, key))
            for key in ("samples", "unread", "seconds", "sessions")
        },
    }


def read(path: Path) -> dict[str, object] | None:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cast(dict[str, object], data) if isinstance(data, dict) else None


def save(path: Path, summary: dict[str, object]) -> dict[str, object]:
    merged = merge(read(path), summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return merged
