"""fleet 표 읽기(foundation §6.4 `GET /fleet`)와 연결 판정(plan M4 C8).

연결 판정은 저장하지 않고 응답 때 계산한다: `last_seen`이 없으면 `never`, 중앙 시각 기준
`now - last_seen <= 3 * poll`이면 `online`, 넘으면 `stale`. stale 현장의 `current`는 마지막
보고값이고 그 시각은 `current.reported_at`이다(control-web이 "마지막 보고값(시각)"으로 표시).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import cast

import asyncpg

from pureiron_bundle.sitesign import Json
from pureiron_control.platform.clock import Clock
from pureiron_core.platform.db import Database

STALE_AFTER_POLLS = 3

type Record = asyncpg.Record

FLEET_SQL = """
SELECT s.site_id, s.last_heartbeat_seq, s.last_report_seq, s.last_seen, s.current,
       s.current_reported_at, s.current_source,
       s.updater_phase, s.disk_state,
       d.generation AS desired_gen, d.sha256 AS desired_sha256, d.size_bytes AS desired_size,
       d.product_version AS desired_product, d.project_version AS desired_project_version,
       d.published_at AS desired_published_at,
       a.report_seq AS attempt_seq, a.source AS attempt_source, a.payload AS attempt_payload,
       a.received_at AS attempt_received_at
  FROM sites s
  LEFT JOIN LATERAL (
        SELECT generation, sha256, size_bytes, product_version, project_version, published_at
          FROM bundles b WHERE b.site_id = s.site_id ORDER BY generation DESC LIMIT 1
       ) d ON true
  LEFT JOIN apply_attempts a ON a.id = s.last_attempt_id
 WHERE $1::text IS NULL OR s.site_id = $1
 ORDER BY s.site_id
"""


class ConnectionState(StrEnum):
    ONLINE = "online"
    STALE = "stale"
    NEVER = "never"


def connection_state(
    last_seen: datetime | None, now: datetime, *, poll_s: float
) -> ConnectionState:
    if last_seen is None:
        return ConnectionState.NEVER
    if now - last_seen <= timedelta(seconds=STALE_AFTER_POLLS * poll_s):
        return ConnectionState.ONLINE
    return ConnectionState.STALE


@dataclass(frozen=True, slots=True)
class Desired:
    generation: int
    sha256: str
    size_bytes: int
    product_version: str
    project_version: str
    published_at: datetime


@dataclass(frozen=True, slots=True)
class CurrentReport:
    product: str | None
    project_version: str | None
    gen: int | None
    reported_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class LastAttempt:
    report_seq: int
    source: str
    received_at: datetime
    payload: dict[str, Json]


@dataclass(frozen=True, slots=True)
class FleetRow:
    site_id: str
    desired: Desired | None
    current: CurrentReport | None
    last_seen: datetime | None
    connection: ConnectionState
    last_attempt: LastAttempt | None
    disk_state: str | None
    updater_phase: str | None
    last_heartbeat_seq: int
    last_report_seq: int

    @property
    def max_published_gen(self) -> int | None:
        return None if self.desired is None else self.desired.generation


def _json_object(text: object) -> dict[str, Json]:
    value: object = json.loads(text) if isinstance(text, str) else None
    return cast(dict[str, Json], value) if isinstance(value, dict) else {}


class PgFleetReader:
    def __init__(self, db: Database, *, clock: Clock, poll_s: float) -> None:
        self._db = db
        self._clock = clock
        self._poll_s = poll_s

    async def rows(self, site_id: str | None = None) -> list[FleetRow]:
        async with self._db.connection() as conn:
            records = await conn.fetch(FLEET_SQL, site_id)
        now = self._clock()
        return [self._row(r, now) for r in records]

    def _row(self, r: Record, now: datetime) -> FleetRow:
        desired = None
        if r["desired_gen"] is not None:
            desired = Desired(
                generation=r["desired_gen"],
                sha256=r["desired_sha256"],
                size_bytes=r["desired_size"],
                product_version=r["desired_product"],
                project_version=r["desired_project_version"],
                published_at=r["desired_published_at"],
            )
        current = None
        if r["current_reported_at"] is not None:
            reported = _json_object(r["current"])
            current = CurrentReport(
                product=cast(str | None, reported.get("product")),
                project_version=cast(str | None, reported.get("project_version")),
                gen=cast(int | None, reported.get("gen")),
                reported_at=r["current_reported_at"],
                source=r["current_source"],
            )
        attempt = None
        if r["attempt_seq"] is not None:
            attempt = LastAttempt(
                report_seq=r["attempt_seq"],
                source=r["attempt_source"],
                received_at=r["attempt_received_at"],
                payload=_json_object(r["attempt_payload"]),
            )
        return FleetRow(
            site_id=r["site_id"],
            desired=desired,
            current=current,
            last_seen=r["last_seen"],
            connection=connection_state(r["last_seen"], now, poll_s=self._poll_s),
            last_attempt=attempt,
            disk_state=r["disk_state"],
            updater_phase=r["updater_phase"],
            last_heartbeat_seq=r["last_heartbeat_seq"],
            last_report_seq=r["last_report_seq"],
        )
