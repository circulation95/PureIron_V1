"""연결 판정(plan M4 C8): last_seen <= 3*poll → online, 넘으면 stale, 없으면 never(저장 안 함)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pureiron_control.fleet import ConnectionState, connection_state

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("age_s", "poll_s", "expected"),
    [
        (0, 60.0, ConnectionState.ONLINE),
        (179, 60.0, ConnectionState.ONLINE),
        (180, 60.0, ConnectionState.ONLINE),  # 경계 포함(<= 3*poll)
        (181, 60.0, ConnectionState.STALE),
        (3600, 60.0, ConnectionState.STALE),
        (31, 10.0, ConnectionState.STALE),  # poll 주기는 settings
        (30, 10.0, ConnectionState.ONLINE),
    ],
)
def test_connection_from_last_seen(age_s: int, poll_s: float, expected: ConnectionState) -> None:
    last_seen = NOW - timedelta(seconds=age_s)
    assert connection_state(last_seen, NOW, poll_s=poll_s) is expected


def test_never_seen_site_is_never() -> None:
    assert connection_state(None, NOW, poll_s=60.0) is ConnectionState.NEVER
