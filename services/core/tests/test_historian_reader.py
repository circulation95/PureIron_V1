"""historian reader: 해상도 자동 선택 경계, 조회 구간 검증(DB 조회는 integration)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from pureiron_core.historian.reader import auto_resolution, read_raw
from pureiron_core.platform.db import Connection

T0 = datetime(2026, 10, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        (timedelta(minutes=1), "raw"),
        (timedelta(hours=1), "raw"),
        (timedelta(hours=1, seconds=1), "1m"),
        (timedelta(hours=24), "1m"),
        (timedelta(hours=24, seconds=1), "1h"),
        (timedelta(days=30), "1h"),
    ],
)
def test_auto_resolution(span: timedelta, expected: str) -> None:
    assert auto_resolution(T0, T0 + span) == expected


class NoConn:
    """검증이 쿼리보다 먼저인지 확인용(호출되면 실패)."""

    async def fetch(self, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError("query must not run")


@pytest.mark.parametrize(
    ("start", "end", "limit", "message"),
    [
        (T0.replace(tzinfo=None), T0 + timedelta(hours=1), 10, "timezone-aware"),
        (T0, T0, 10, "after start"),
        (T0, T0 - timedelta(seconds=1), 10, "after start"),
        (T0, T0 + timedelta(hours=1), 0, "limit"),
    ],
)
async def test_invalid_window_is_rejected_before_query(
    start: datetime, end: datetime, limit: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await read_raw(cast("Connection", NoConn()), [1], start, end, limit=limit)
