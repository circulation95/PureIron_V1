"""`GET /history`: 태그 이력(viewer 이상, plan M3b D11).

`tag_id`를 반복해 최대 20개, 구간 `[start, end)`(timezone 필수), `resolution=auto|raw|1m|1h`,
`limit_rows`(태그별 상한). auto는 ≤ 1h raw · ≤ 24h 1m · 그 이상 1h. 해상도별 구간 상한(raw 1h ·
1m 7d · 1h 400d)을 넘으면 422. 응답은 실제로 쓴 `resolution`과 태그별 series(+ `truncated`)다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime, BaseModel, Field

from pureiron_core.api.auth import SessionGuard
from pureiron_core.api.errors import site_db_unavailable
from pureiron_core.auth.ports import Role
from pureiron_core.historian.ports import HistoryUnavailableError
from pureiron_core.historian.reader import (
    DEFAULT_LIMIT_PER_TAG,
    HistoryResult,
    Resolution,
    auto_resolution,
    check_window,
)
from pureiron_core.realtime.protocol import TAG_ID_MAX
from pureiron_core.tags.ports import Quality

MAX_TAGS = 20


class HistorySource(Protocol):
    async def read(
        self,
        tag_ids: Sequence[int],
        start: datetime,
        end: datetime,
        resolution: Resolution,
        *,
        limit_per_tag: int,
    ) -> HistoryResult: ...


class RawPointOut(BaseModel):
    time: datetime
    value: float | bool | None = Field(description="숫자 또는 bool. NaN·값 없는 BAD는 null")
    quality: Literal["GOOD", "BAD", "UNCERTAIN"]


class BucketOut(BaseModel):
    time: datetime = Field(description="bucket 시작 시각")
    avg: float | None
    min: float | None
    max: float | None
    last: float | None
    samples: int = Field(description="bucket 안 행 수(bool·NULL 행 포함 — avg 분모와 다르다)")


class HistorySeries(BaseModel):
    tag_id: int
    truncated: bool = Field(description="limit_rows를 넘어 앞쪽 limit_rows개만 담았다")
    raw: list[RawPointOut] = Field(description="resolution=raw일 때만 채운다")
    buckets: list[BucketOut] = Field(description="resolution=1m·1h일 때만 채운다")


class HistoryOut(BaseModel):
    resolution: Literal["raw", "1m", "1h"] = Field(description="실제로 쓴 해상도(auto 해석 결과)")
    series: list[HistorySeries] = Field(description="요청한 tag_id 순서(중복 제거)")


def _quality(raw: int) -> Literal["GOOD", "BAD", "UNCERTAIN"]:
    try:
        return Quality(raw).name
    except ValueError:
        return "BAD"


def _out(result: HistoryResult) -> HistoryOut:
    series = [
        HistorySeries(
            tag_id=s.tag_id,
            truncated=s.truncated,
            raw=[
                RawPointOut(
                    time=p.time,
                    value=p.value_bool if p.value_bool is not None else p.value_num,
                    quality=_quality(p.quality),
                )
                for p in s.points
            ],
            buckets=[],
        )
        for s in result.raw
    ] + [
        HistorySeries(
            tag_id=s.tag_id,
            truncated=s.truncated,
            raw=[],
            buckets=[
                BucketOut(
                    time=p.bucket, avg=p.avg, min=p.min, max=p.max, last=p.last, samples=p.samples
                )
                for p in s.points
            ],
        )
        for s in result.aggregate
    ]
    return HistoryOut(resolution=result.resolution, series=series)


def build_history_router(*, history: HistorySource, guard: SessionGuard) -> APIRouter:
    router = APIRouter(prefix="/history", tags=["history"])
    viewer = Depends(guard.require_role(Role.VIEWER))

    @router.get(
        "",
        operation_id="get_history",
        dependencies=[viewer],
        responses={
            status.HTTP_401_UNAUTHORIZED: {"description": "not authenticated"},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "site-db unavailable"},
        },
    )
    async def get_history(
        tag_id: Annotated[
            list[Annotated[int, Field(ge=1, le=TAG_ID_MAX)]],
            Query(min_length=1, max_length=MAX_TAGS, description="반복 가능(최대 20개)"),
        ],
        start: Annotated[AwareDatetime, Query(description="포함(>=)")],
        end: Annotated[AwareDatetime, Query(description="제외(<)")],
        resolution: Annotated[Literal["auto", "raw", "1m", "1h"], Query()] = "auto",
        limit_rows: Annotated[
            int, Query(ge=1, le=DEFAULT_LIMIT_PER_TAG, description="태그별 행 상한")
        ] = DEFAULT_LIMIT_PER_TAG,
    ) -> HistoryOut:
        chosen: Resolution = auto_resolution(start, end) if resolution == "auto" else resolution
        try:
            check_window(start, end, chosen, limit_rows)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        ids = list(dict.fromkeys(tag_id))
        try:
            result = await history.read(ids, start, end, chosen, limit_per_tag=limit_rows)
        except HistoryUnavailableError as exc:
            raise site_db_unavailable(exc) from exc
        return _out(result)

    return router
