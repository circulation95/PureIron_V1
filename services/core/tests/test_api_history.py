"""`GET /history`(D11): 인증, tag_id int4·개수(1~20)·중복 제거, timezone 필수, 해상도별 span 상한
422, auto 해석, truncated·series 모양, 503."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pureiron_core.api.auth import SessionGuard
from pureiron_core.api.history import build_history_router
from pureiron_core.auth.ports import Role
from pureiron_core.historian.ports import HistoryUnavailableError
from pureiron_core.historian.reader import AggPoint, HistoryResult, RawPoint, Resolution, Series

from .auth_fakes import auth_fixture

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
URL = "/api/v1/history"

type Param = str | int | list[int]


@dataclass
class Call:
    tag_ids: list[int]
    start: datetime
    end: datetime
    resolution: Resolution
    limit_per_tag: int


@dataclass
class FakeHistory:
    calls: list[Call] = field(default_factory=list[Call])
    available: bool = True

    async def read(
        self,
        tag_ids: Sequence[int],
        start: datetime,
        end: datetime,
        resolution: Resolution,
        *,
        limit_per_tag: int,
    ) -> HistoryResult:
        if not self.available:
            raise HistoryUnavailableError("site-db error: down")
        self.calls.append(Call(list(tag_ids), start, end, resolution, limit_per_tag))
        if resolution == "raw":
            point = RawPoint(tag_ids[0], T0, 1.5, None, 1)
            flag = RawPoint(tag_ids[0], T0, None, True, 2)
            return HistoryResult("raw", raw=(Series(tag_ids[0], (point, flag), truncated=True),))
        bucket = AggPoint(tag_ids[0], T0, 2.0, 1.0, 3.0, 3.0, 60)
        return HistoryResult(resolution, aggregate=(Series(tag_ids[0], (bucket,), False),))


async def _client(history: FakeHistory) -> tuple[TestClient, dict[str, str]]:
    fx = auth_fixture()
    app = FastAPI()
    app.include_router(
        build_history_router(history=history, guard=SessionGuard(fx.service)), prefix="/api/v1"
    )
    await fx.service.add_user("viewer", "correct-horse-1", Role.VIEWER)
    token = (await fx.service.login("viewer", "correct-horse-1")).token
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def _params(**extra: Param) -> dict[str, Param]:
    return {"tag_id": [3], "start": "2026-10-11T09:00:00Z", "end": "2026-10-11T09:30:00Z", **extra}


async def test_requires_session() -> None:
    client, _ = await _client(FakeHistory())
    assert client.get(URL, params=_params()).status_code == 401


async def test_raw_series_with_truncated_and_auto_resolution() -> None:
    history = FakeHistory()
    client, headers = await _client(history)
    response = client.get(URL, params=_params(tag_id=[3, 4, 3], limit_rows=50), headers=headers)
    assert response.status_code == 200
    assert response.json() == {
        "resolution": "raw",
        "series": [
            {
                "tag_id": 3,
                "truncated": True,
                "raw": [
                    {"time": "2026-10-11T09:00:00Z", "value": 1.5, "quality": "GOOD"},
                    {"time": "2026-10-11T09:00:00Z", "value": True, "quality": "BAD"},
                ],
                "buckets": [],
            }
        ],
    }
    [call] = history.calls
    assert (call.tag_ids, call.resolution, call.limit_per_tag) == ([3, 4], "raw", 50)


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        ("2026-10-11T10:00:00Z", "raw"),
        ("2026-10-12T09:00:00Z", "1m"),
        ("2026-10-20T09:00:00Z", "1h"),
    ],
)
async def test_auto_picks_resolution_by_span(end: str, expected: str) -> None:
    history = FakeHistory()
    client, headers = await _client(history)
    response = client.get(URL, params=_params(end=end), headers=headers)
    assert response.json()["resolution"] == expected
    if expected != "raw":
        [series] = response.json()["series"]
        assert series["buckets"] == [
            {
                "time": "2026-10-11T09:00:00Z",
                "avg": 2.0,
                "min": 1.0,
                "max": 3.0,
                "last": 3.0,
                "samples": 60,
            }
        ]


@pytest.mark.parametrize(
    "params",
    [
        _params(tag_id=[]),
        _params(tag_id=list(range(1, 22))),
        _params(tag_id=[0]),
        _params(tag_id=[2**31]),
        _params(start="2026-10-11T09:00:00"),
        _params(end="2026-10-11T08:00:00Z"),
        _params(resolution="raw", end="2026-10-11T10:00:01Z"),
        _params(resolution="1m", end="2026-10-18T09:00:01Z"),
        _params(resolution="1h", end="2027-11-15T09:00:01Z"),
        _params(resolution="auto", end="2027-11-15T09:00:01Z"),
        _params(resolution="5m"),
        _params(limit_rows=0),
        _params(limit_rows=20_001),
    ],
    ids=[
        "no-tags",
        "21-tags",
        "tag-zero",
        "tag-over-int4",
        "naive-start",
        "end-before-start",
        "raw-over-1h",
        "1m-over-7d",
        "1h-over-400d",
        "auto-over-400d",
        "unknown-resolution",
        "limit-zero",
        "limit-over",
    ],
)
async def test_invalid_requests_are_422(params: dict[str, Param]) -> None:
    history = FakeHistory()
    client, headers = await _client(history)
    assert client.get(URL, params=params, headers=headers).status_code == 422
    assert history.calls == []


async def test_site_db_down_is_503() -> None:
    client, headers = await _client(FakeHistory(available=False))
    response = client.get(URL, params=_params(), headers=headers)
    assert (response.status_code, response.json()["detail"]) == (503, "site-db unavailable")
