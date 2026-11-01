"""heartbeat·ApplyAttempt 보고 반영(plan M4 C7): 온라인·폐쇄망 모두 `PgReportStore.record`로.

seq 규칙(`judge_seq`): 현장의 `report_seq` 카운터는 heartbeat·보고 공용이지만(R2 — 온라인·USB가
겹치지 않게) 반영 수위는 **종류별**이다(`SeqMarks`). 메시지는 자기 종류의 수위보다 클 때만 반영
(`applied`), 그 밖 → 이력만(`history`). 수위가 하나면 끊겼다 늦게 재전송된 보고(seq 11)가 그 사이
성공한 heartbeat(seq 12)에 가려 `last_attempt`에 영영 안 남는다 — 실패한 업데이트가 안 보인다.
둘 다 이력 행(`heartbeats`·`apply_attempts`, `source`·`outcome` 기록)을 남기고 호출자에게는
성공(200)이다. 반영될 때만 바뀌는 것:

- 공통: 그 종류의 수위(`sites.last_heartbeat_seq`·`last_report_seq`).
- heartbeat: `current`(현장이 보고한 product·project_version·gen)·`current_reported_at`
  (서명된 `at`)·`updater_phase`·`disk_state`.
- 보고: `last_attempt_id`. 결과가 `succeeded`면 `current`도 — gen = `to`, 버전은 그 gen의 게시 번들.
- `last_seen`(stale 판정의 기준, C8)은 반영 여부와 무관하게 **온라인** 메시지를 받은 중앙 시각이다 —
  token·서명이 확인된 요청은 그 현장이 지금 연결돼 있다는 증거다(seq가 밀린 재전송이라도). 폐쇄망
  반입은 `last_seen`을 바꾸지 않는다(중앙이 그 현장을 "본" 것이 아니다).

서명·현장 일치는 호출자(api)가 확인한 뒤 여기 온다. payload 형식 위반은 `ReportFormatError`(400) —
DB 열 범위를 넘는 정수(seq bigint, gen 계열 integer)와 jsonb가 받지 않는 NUL 문자도 여기서 막는다
(저장 단계 500이 되면 그 파일·큐 맨 앞 메시지를 영원히 보낼 수 없다).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pureiron_bundle.sitesign import Json
from pureiron_bundle.verify import AttemptResult
from pureiron_control.platform.clock import Clock
from pureiron_control.platform.seqmarks import MessageKind, SeqMarks
from pureiron_core.platform.db import Connection, Database

LOCK_SITE_SQL = """
SELECT last_heartbeat_seq, last_report_seq FROM sites WHERE site_id = $1 FOR UPDATE
"""
INSERT_IMPORT_SQL = """
INSERT INTO offline_imports (site_id, first_seq, last_seq, exported_at, file_sha256, imported_at,
                             imported_by)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING id
"""
COUNT_IMPORT_SQL = """
UPDATE offline_imports SET applied_total = $2, history_total = $3 WHERE id = $1
"""
INSERT_HEARTBEAT_SQL = """
INSERT INTO heartbeats (site_id, report_seq, source, outcome, payload, received_at)
VALUES ($1, $2, $3, $4, $5::jsonb, $6)
"""
INSERT_ATTEMPT_SQL = """
INSERT INTO apply_attempts (site_id, report_seq, source, outcome, generation, result, payload,
                            received_at, offline_import_id)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
RETURNING id
"""
APPLY_HEARTBEAT_SQL = """
UPDATE sites
   SET last_heartbeat_seq = $2, current = $3::jsonb, current_reported_at = $4, current_source = $5,
       updater_phase = $6, disk_state = $7
 WHERE site_id = $1
"""
APPLY_ATTEMPT_SQL = """
UPDATE sites SET last_report_seq = $2, last_attempt_id = $3 WHERE site_id = $1
"""
APPLY_CURRENT_SQL = """
UPDATE sites SET current = $2::jsonb, current_reported_at = $3, current_source = $4
 WHERE site_id = $1
"""
BUNDLE_VERSIONS_SQL = """
SELECT product_version, project_version FROM bundles WHERE site_id = $1 AND generation = $2
"""
# 시각은 잠금을 잡기 전에 잰다 — 동시 요청이 먼저 쓴 더 늦은 시각을 되돌리지 않는다.
TOUCH_SQL = """
UPDATE sites SET last_seen = GREATEST(coalesce(last_seen, $2), $2) WHERE site_id = $1
"""
SEQ_MAX = 2**63 - 1
"""`report_seq`·`first_seq`·`last_seq` 상한(bigint)."""
GEN_MAX = 2**31 - 1
"""generation 계열(`generation`·`to`·`from`·`current.gen`) 상한(integer — `bundles.generation`)."""


class Source(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"


class SeqOutcome(StrEnum):
    APPLIED = "applied"
    HISTORY = "history"


def judge_seq(marks: SeqMarks, kind: MessageKind, report_seq: int) -> SeqOutcome:
    return SeqOutcome.APPLIED if report_seq > marks.of(kind) else SeqOutcome.HISTORY


class ReportError(Exception):
    pass


class ReportFormatError(ReportError):
    pass


class ReportSiteNotFoundError(ReportError):
    pass


@dataclass(frozen=True, slots=True)
class Current:
    product: str | None
    project_version: str | None
    gen: int | None

    def as_json(self) -> dict[str, Json]:
        return {"product": self.product, "project_version": self.project_version, "gen": self.gen}


@dataclass(frozen=True, slots=True)
class Heartbeat:
    report_seq: int
    current: Current | None
    updater_phase: str | None
    disk_state: str | None
    at: datetime
    payload: Mapping[str, Json]


@dataclass(frozen=True, slots=True)
class AttemptReport:
    report_seq: int
    generation: int | None
    to: int | None
    result: str
    ended_at: datetime
    payload: Mapping[str, Json]


type Message = Heartbeat | AttemptReport


def kind_of(message: Message) -> MessageKind:
    return MessageKind.HEARTBEAT if isinstance(message, Heartbeat) else MessageKind.REPORT


@dataclass(frozen=True, slots=True)
class ReportBundle:
    """폐쇄망 `site-reports` 봉투 payload(Updater `report export`)."""

    site_id: str
    key_fingerprint: str
    exported_at: str
    first_seq: int
    last_seq: int
    reports: tuple[AttemptReport, ...]


@dataclass(frozen=True, slots=True)
class OfflineImport:
    bundle: ReportBundle
    file_sha256: str
    imported_by: int | None


@dataclass(frozen=True, slots=True)
class RecordResult:
    outcomes: tuple[SeqOutcome, ...]
    marks: SeqMarks
    import_id: int | None = None

    @property
    def applied_total(self) -> int:
        return self.outcomes.count(SeqOutcome.APPLIED)

    @property
    def history_total(self) -> int:
        return self.outcomes.count(SeqOutcome.HISTORY)


def _seq(payload: Mapping[str, Json], key: str = "report_seq") -> int:
    value = payload.get(key)
    if type(value) is not int or not 1 <= value <= SEQ_MAX:
        raise ReportFormatError(f"payload.{key} must be an integer in 1..{SEQ_MAX}")
    return value


def _optional_gen(payload: Mapping[str, Json], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= GEN_MAX:
        raise ReportFormatError(f"payload.{key} must be an integer in 0..{GEN_MAX} or null")
    return value


def _reject_nul(value: Json, where: str = "payload") -> None:
    """jsonb는 NUL 문자(JSON `\\u0000`)를 받지 않는다 — 저장 전에 400으로 거부한다(키·값·중첩)."""
    if isinstance(value, str):
        if "\x00" in value:
            raise ReportFormatError(f"{where} must not contain NUL (\\u0000)")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_nul(key, f"{where} key")
            _reject_nul(item, f"{where}.{key}")
    elif isinstance(value, list):
        for item in value:
            _reject_nul(item, f"{where}[]")


def _optional_text(payload: Mapping[str, Json], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReportFormatError(f"payload.{key} must be a string or null")
    return value


def _timestamp(payload: Mapping[str, Json], key: str) -> datetime:
    value = payload.get(key)
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ReportFormatError(f"payload.{key} must be an ISO 8601 timestamp with offset")
    return parsed


def parse_heartbeat(payload: Mapping[str, Json]) -> Heartbeat:
    _reject_nul(dict(payload))
    raw_current = payload.get("current")
    current: Current | None = None
    if isinstance(raw_current, dict):
        current = Current(
            product=_optional_text(raw_current, "product"),
            project_version=_optional_text(raw_current, "project_version"),
            gen=_optional_gen(raw_current, "gen"),
        )
    elif raw_current is not None:
        raise ReportFormatError("payload.current must be an object or null")
    return Heartbeat(
        report_seq=_seq(payload),
        current=current,
        updater_phase=_optional_text(payload, "updater_phase"),
        disk_state=_optional_text(payload, "disk_state"),
        at=_timestamp(payload, "at"),
        payload=payload,
    )


def parse_report(payload: Mapping[str, Json]) -> AttemptReport:
    _reject_nul(dict(payload))
    _optional_gen(payload, "from")  # 열에는 없지만 fleet `last_attempt.from`으로 낸다
    result = payload.get("result")
    if not isinstance(result, str) or not result:
        raise ReportFormatError("payload.result must be a non-empty string")
    return AttemptReport(
        report_seq=_seq(payload),
        generation=_optional_gen(payload, "generation"),
        to=_optional_gen(payload, "to"),
        result=result,
        ended_at=_timestamp(payload, "ended_at"),
        payload=payload,
    )


def parse_report_bundle(payload: Mapping[str, Json]) -> ReportBundle:
    _reject_nul(dict(payload))
    site_id, fingerprint = payload.get("site_id"), payload.get("key_fingerprint")
    exported_at, reports = payload.get("exported_at"), payload.get("reports")
    if not (isinstance(site_id, str) and isinstance(fingerprint, str)):
        raise ReportFormatError("payload.site_id and payload.key_fingerprint must be strings")
    if not isinstance(exported_at, str) or not isinstance(reports, list) or not reports:
        raise ReportFormatError("payload.exported_at must be a string, reports a non-empty list")
    first, last = _seq(payload, "first_seq"), _seq(payload, "last_seq")
    parsed: list[AttemptReport] = []
    for item in reports:
        if not isinstance(item, dict):
            raise ReportFormatError("payload.reports must hold objects")
        parsed.append(parse_report(item))
    # report export는 first_seq부터 빈틈없이 매긴다 — 다르면 손으로 고친 파일이다(서명이 맞아도).
    if [r.report_seq for r in parsed] != list(range(first, last + 1)):
        raise ReportFormatError(
            "payload.reports must carry report_seq first_seq..last_seq in order"
        )
    return ReportBundle(site_id, fingerprint, exported_at, first, last, tuple(parsed))


def _dumps(payload: Mapping[str, Json]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class _Batch:
    """한 `record` 호출(한 트랜잭션)의 공통 문맥."""

    conn: Connection
    site_id: str
    source: Source
    received_at: datetime
    import_id: int | None

    async def store(self, message: Message, outcome: SeqOutcome) -> None:
        if isinstance(message, Heartbeat):
            await self._heartbeat(message, outcome)
        else:
            await self._attempt(message, outcome)

    async def _heartbeat(self, message: Heartbeat, outcome: SeqOutcome) -> None:
        await self.conn.execute(
            INSERT_HEARTBEAT_SQL,
            self.site_id,
            message.report_seq,
            self.source.value,
            outcome.value,
            _dumps(message.payload),
            self.received_at,
        )
        if outcome is not SeqOutcome.APPLIED:
            return
        current = None if message.current is None else _dumps(message.current.as_json())
        await self.conn.execute(
            APPLY_HEARTBEAT_SQL,
            self.site_id,
            message.report_seq,
            current,
            message.at,
            self.source.value,
            message.updater_phase,
            message.disk_state,
        )

    async def _attempt(self, message: AttemptReport, outcome: SeqOutcome) -> None:
        attempt_id = await self.conn.fetchval(
            INSERT_ATTEMPT_SQL,
            self.site_id,
            message.report_seq,
            self.source.value,
            outcome.value,
            message.generation,
            message.result,
            _dumps(message.payload),
            self.received_at,
            self.import_id,
        )
        if outcome is not SeqOutcome.APPLIED:
            return
        await self.conn.execute(APPLY_ATTEMPT_SQL, self.site_id, message.report_seq, attempt_id)
        if message.result != AttemptResult.SUCCEEDED.value or message.to is None:
            return
        versions = await self.conn.fetchrow(BUNDLE_VERSIONS_SQL, self.site_id, message.to)
        current = Current(
            product=None if versions is None else versions["product_version"],
            project_version=None if versions is None else versions["project_version"],
            gen=message.to,
        )
        await self.conn.execute(
            APPLY_CURRENT_SQL,
            self.site_id,
            _dumps(current.as_json()),
            message.ended_at,
            self.source.value,
        )


class PgReportStore:
    def __init__(self, db: Database, *, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def record(
        self,
        site_id: str,
        messages: Sequence[Message],
        *,
        source: Source,
        offline: OfflineImport | None = None,
    ) -> RecordResult:
        """메시지를 seq 순서로 seq 규칙에 넣는다(한 트랜잭션, 현장 행 잠금)."""
        received_at = self._clock()
        ordered = sorted(messages, key=lambda m: m.report_seq)
        async with self._db.connection() as conn, conn.transaction():
            row = await conn.fetchrow(LOCK_SITE_SQL, site_id)
            if row is None:
                raise ReportSiteNotFoundError(f"site {site_id!r} is not registered")
            marks = SeqMarks(heartbeat=row["last_heartbeat_seq"], report=row["last_report_seq"])
            import_id = await _insert_import(conn, site_id, offline, received_at)
            batch = _Batch(conn, site_id, source, received_at, import_id)
            outcomes: list[SeqOutcome] = []
            for message in ordered:
                kind = kind_of(message)
                outcome = judge_seq(marks, kind, message.report_seq)
                await batch.store(message, outcome)
                outcomes.append(outcome)
                if outcome is SeqOutcome.APPLIED:
                    marks = marks.advanced(kind, message.report_seq)
            if source is Source.ONLINE:
                await conn.execute(TOUCH_SQL, site_id, received_at)
            result = RecordResult(tuple(outcomes), marks, import_id)
            if import_id is not None:
                await conn.execute(
                    COUNT_IMPORT_SQL, import_id, result.applied_total, result.history_total
                )
        return result


async def _insert_import(
    conn: Connection, site_id: str, offline: OfflineImport | None, now: datetime
) -> int | None:
    if offline is None:
        return None
    bundle = offline.bundle
    import_id = await conn.fetchval(
        INSERT_IMPORT_SQL,
        site_id,
        bundle.first_seq,
        bundle.last_seq,
        bundle.exported_at,
        offline.file_sha256,
        now,
        offline.imported_by,
    )
    if not isinstance(import_id, int):
        raise ReportError("offline_imports insert returned no id")
    return import_id
