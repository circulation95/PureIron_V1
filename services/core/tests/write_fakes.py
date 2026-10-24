"""쓰기 테스트용 정의·가짜 전송로·편집 잠금·감사(`WriteChannel`·`ProjectStatePort`·`StrictAuditSink`
Protocol을 직접 만족한다)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from pureiron_core.acquisition.ports import (
    WriteCommand,
    WritePathState,
    WriteReply,
    WriteReplyStatus,
)
from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditUnavailableError
from pureiron_core.auth.ports import Principal, Role
from pureiron_core.project.ports import (
    Definitions,
    DeviceDef,
    ProjectState,
    ProjectStateError,
    TagDef,
)
from pureiron_core.tags.ports import Quality, Reading, TagKey
from pureiron_core.tags.store import TagStore
from pureiron_core.writes.service import WritesService

REV = 5
TS0_NS = 1_791_000_000_000_000_000
T0 = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)

SETPOINT = "line1/temp_sp"
COMMAND = "line1/start"
REMOTE_OK = "line1/remote_ok"
AUTO_MODE = "line1/auto_mode"
PLAIN = "line1/temp"
TAG_IDS = {SETPOINT: 10, COMMAND: 11, REMOTE_OK: 12, AUTO_MODE: 13, PLAIN: 14}


def _tag(path: str, modbus: dict[str, object], write: dict[str, object] | None = None) -> TagDef:
    definition: dict[str, object] = {"path": path, "device": "plc-1", "modbus": modbus}
    if write is not None:
        definition["write"] = write
    return TagDef(TAG_IDS[path], path, "plc-1", definition)


def write_definitions(rev: int = REV, *extra: TagDef) -> Definitions:
    """setpoint(float32 holding 100, 20..90) + command(coil 10, [true], 허가 조건 2개) + 읽기
    전용 태그."""
    coil: dict[str, object] = {"register_type": "coil", "data_type": "bool"}
    return Definitions(
        rev=rev,
        devices=(DeviceDef("plc-1", {"id": "plc-1", "protocol": "modbus_tcp", "host": "h"}),),
        tags=(
            _tag(
                SETPOINT,
                {"register_type": "holding", "address": 100, "data_type": "float32"},
                {"kind": "setpoint", "min": 20, "max": 90},
            ),
            _tag(
                COMMAND,
                {**coil, "address": 10},
                {"kind": "command", "values": [True], "permissive": [REMOTE_OK, AUTO_MODE]},
            ),
            _tag(REMOTE_OK, {**coil, "address": 20}),
            _tag(AUTO_MODE, {**coil, "address": 21}),
            _tag(PLAIN, {"register_type": "holding", "address": 0, "data_type": "float32"}),
            *extra,
        ),
    )


def write_tags() -> TagStore:
    """정의의 태그 전부 GOOD: setpoint 25.0, 허가 조건 둘 다 참, command false."""
    store = TagStore()
    store.reindex(TagKey(tag_id, path, "plc-1") for path, tag_id in TAG_IDS.items())
    set_tag(store, SETPOINT, 25.0)
    set_tag(store, COMMAND, False)
    set_tag(store, REMOTE_OK, True)
    set_tag(store, AUTO_MODE, True)
    set_tag(store, PLAIN, 1.0)
    return store


def set_tag(
    store: TagStore, path: str, value: float | bool | None, quality: Quality = Quality.GOOD
) -> None:
    current = store.get(TAG_IDS[path])
    # 시간 역행으로 버려지지 않게 앞 샘플보다 1ns씩 늦춘다.
    ts_ns = max(TS0_NS, current.ts_ns + 1) if current is not None else TS0_NS
    store.commit([Reading(TAG_IDS[path], value, quality, ts_ns)], ts_ns)


def principal(username: str = "op", role: Role = Role.OPERATOR) -> Principal:
    return Principal(user_id=1, username=username, role=role, display_name=None)


def ready_path(**changes: object) -> WritePathState:
    base = WritePathState(
        sessions=1,
        plan_rev=REV,
        reported_plan_rev=REV,
        device_connected=True,
        device_timeout_ms=1000,
    )
    return replace(base, **changes)


@dataclass
class FakeChannel:
    """`WriteChannel` 가짜: 정해 둔 경로 상태와 응답. `gate`가 있으면 열릴 때까지 응답을 미룬다."""

    path: WritePathState = field(default_factory=ready_path)
    reply: WriteReply = field(
        default_factory=lambda: WriteReply(WriteReplyStatus.OK, readback=25.5, readback_ts_ns=1)
    )
    sent: list[tuple[WriteCommand, float]] = field(default_factory=list[tuple[WriteCommand, float]])
    gate: asyncio.Event | None = None

    def write_path(self, device_id: str) -> WritePathState:
        return self.path

    async def send(self, command: WriteCommand, *, wait_s: float) -> WriteReply:
        self.sent.append((command, wait_s))
        if self.gate is not None:
            await self.gate.wait()
        return self.reply


@dataclass
class FakeProject:
    """`ProjectStatePort` 가짜: 편집 잠금 소유자, `available=False`면 site-db 오류."""

    owner: str | None = None
    available: bool = True

    async def get(self) -> ProjectState:
        if not self.available:
            raise ProjectStateError("site-db error: down")
        return ProjectState(None, None, None, 0, 0, self.owner, None, None)

    async def acquire_edit_lock(self, owner: str, generation: int) -> int:
        self.owner = owner
        return 0

    async def release_edit_lock(self, owner: str) -> None:
        self.owner = None


@dataclass
class FakeStrictAudit:
    """`StrictAuditSink` 가짜: `failing`에 든 action(또는 전부)은 실패, `during`은 기록 중 호출."""

    entries: list[AuditEntry] = field(default_factory=list[AuditEntry])
    attempts: list[AuditAction] = field(default_factory=list[AuditAction])
    failing: set[AuditAction] = field(default_factory=set[AuditAction])
    down: bool = False
    during: Callable[[AuditEntry], Awaitable[None]] | None = None

    async def record_strict(self, entry: AuditEntry) -> None:
        self.attempts.append(entry.action)
        # PgAuditStore와 같이 직렬화하되 NaN/Inf는 거부한다 — jsonb가 받지 않는 값이 테스트에서
        # 바로 드러나게.
        json.dumps(dict(entry.detail), sort_keys=True, default=str, allow_nan=False)
        if self.during is not None:
            await self.during(entry)
        if self.down or entry.action in self.failing:
            raise AuditUnavailableError("site-db error: down")
        self.entries.append(entry)

    def rows(self) -> list[AuditEntry]:
        """기동 행(`writes.enabled_at_boot`)을 뺀 기록."""
        return [e for e in self.entries if e.action is not AuditAction.WRITES_ENABLED_AT_BOOT]

    def actions(self) -> list[tuple[str, object]]:
        return [(e.action.value, e.detail.get("outcome")) for e in self.rows()]


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class WriteFixture:
    service: WritesService
    channel: FakeChannel
    project: FakeProject
    audit: FakeStrictAudit
    tags: TagStore
    clock: Clock


def write_fixture(
    *, enabled: bool = True, confirm_ttl_s: float = 60.0, write_timeout_s: float = 10.0
) -> WriteFixture:
    channel, project, audit, tags, clock = (
        FakeChannel(),
        FakeProject(),
        FakeStrictAudit(),
        write_tags(),
        Clock(),
    )
    ids = iter(f"{n:032x}" for n in range(1, 10_000))
    service = WritesService(
        channel,
        tags,
        project,
        audit,
        enabled=enabled,
        confirm_ttl_s=confirm_ttl_s,
        write_timeout_s=write_timeout_s,
        clock=clock,
        wall=lambda: T0,
        new_id=lambda: next(ids),
    )
    service.set_definitions(write_definitions())
    return WriteFixture(service, channel, project, audit, tags, clock)
