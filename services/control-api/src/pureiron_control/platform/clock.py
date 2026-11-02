"""중앙 시각(UTC aware). 테스트가 통제할 수 있게 생성자 인자로 주입한다(coding-style 6절)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

type Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)
