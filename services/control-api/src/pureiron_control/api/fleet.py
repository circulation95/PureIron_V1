"""`GET /fleet`(admin·viewer): 현장별 desired·current·last_seen·connection·마지막 ApplyAttempt.

`?site=<id>`는 한 현장만 — 미등록이면 404(fleet `check-gen`이 "미등록 현장만 gen 1"을 이것으로
판정한다, foundation M4). `connection`은 응답 때 계산한다(C8, `fleet.connection_state`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from pureiron_bundle.sitesign import Json
from pureiron_control.api.guard import Guard
from pureiron_control.credentials import Role
from pureiron_control.fleet import ConnectionState, FleetRow, LastAttempt, PgFleetReader
from pureiron_control.sites import site_id_pattern


class DesiredInfo(BaseModel):
    generation: int
    sha256: str
    size: int
    product_version: str
    project_version: str
    published_at: datetime


class CurrentInfo(BaseModel):
    product: str | None
    project_version: str | None
    gen: int | None
    reported_at: datetime = Field(description="현장이 이 값을 보고한 시각(서명된 at·ended_at)")
    source: str = Field(description="online | offline(폐쇄망 반입)")


class AttemptInfo(BaseModel):
    report_seq: int
    source: str = Field(description="online | offline(폐쇄망 반입)")
    received_at: datetime
    generation: int | None
    from_: int | None = Field(serialization_alias="from")
    to: int | None
    phase: str | None
    result: str | None
    code: str | None
    started_at: str | None
    ended_at: str | None


class FleetSite(BaseModel):
    site: str
    desired: DesiredInfo | None
    max_published_gen: int | None = Field(description="게시된 가장 큰 gen(없으면 null)")
    current: CurrentInfo | None = Field(
        description="마지막 반영 보고값 — connection이 stale이면 reported_at 시각의 값"
    )
    last_seen: datetime | None = Field(description="마지막 반영된 온라인 메시지 수신 시각(중앙)")
    connection: ConnectionState
    last_attempt: AttemptInfo | None
    disk_state: str | None
    updater_phase: str | None
    last_seq: int


def _text(payload: dict[str, Json], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _int(payload: dict[str, Json], key: str) -> int | None:
    value = payload.get(key)
    return value if type(value) is int else None


def _attempt(attempt: LastAttempt) -> AttemptInfo:
    p = attempt.payload
    return AttemptInfo(
        report_seq=attempt.report_seq,
        source=attempt.source,
        received_at=attempt.received_at,
        generation=_int(p, "generation"),
        from_=_int(p, "from"),
        to=_int(p, "to"),
        phase=_text(p, "phase"),
        result=_text(p, "result"),
        code=_text(p, "code"),
        started_at=_text(p, "started_at"),
        ended_at=_text(p, "ended_at"),
    )


def _fleet_site(row: FleetRow) -> FleetSite:
    desired, current = row.desired, row.current
    return FleetSite(
        site=row.site_id,
        desired=None
        if desired is None
        else DesiredInfo(
            generation=desired.generation,
            sha256=desired.sha256,
            size=desired.size_bytes,
            product_version=desired.product_version,
            project_version=desired.project_version,
            published_at=desired.published_at,
        ),
        max_published_gen=row.max_published_gen,
        current=None
        if current is None
        else CurrentInfo(
            product=current.product,
            project_version=current.project_version,
            gen=current.gen,
            reported_at=current.reported_at,
            source=current.source,
        ),
        last_seen=row.last_seen,
        connection=row.connection,
        last_attempt=None if row.last_attempt is None else _attempt(row.last_attempt),
        disk_state=row.disk_state,
        updater_phase=row.updater_phase,
        last_seq=row.last_seq,
    )


def build_fleet_router(*, fleet: PgFleetReader, guard: Guard) -> APIRouter:
    router = APIRouter(tags=["fleet"])
    reader = Depends(guard.roles(Role.ADMIN, Role.VIEWER))

    @router.get(
        "/fleet",
        operation_id="get_fleet",
        dependencies=[reader],
        responses={404: {"description": "미등록 현장"}},
    )
    async def get_fleet(
        site: Annotated[str | None, Query(pattern=site_id_pattern())] = None,
    ) -> list[FleetSite]:
        rows = await fleet.rows(site)
        if site is not None and not rows:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"site {site!r} is not registered"
            )
        return [_fleet_site(row) for row in rows]

    return router
