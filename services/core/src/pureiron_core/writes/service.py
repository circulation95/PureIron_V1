"""사람 확인 쓰기의 수명주기: 선언 적용 → prepare(검사) → confirm(재검사·감사 의도 행·전송·
판정·결과 행), plan M3c D8~D12. 조립은 `runtime.py`가 한다.

- 준비 표(`_pending`): write_id → 준비한 사용자·대상·값·사유·만료. confirm은 동기 구간에서 꺼내므로
  (원자적 pop) 같은 id의 두 번째 confirm은 404다 — 이중 클릭에 두 번 보내지 않는다. 다른 사용자의
  confirm은 꺼내지 않고 404, 만료는 꺼내고 410. 사용자마다 최대 `MAX_PENDING_PER_USER`개(넘으면 가장
  오래된 것을 버린다), 만료된 것은 prepare 때 치운다.
- 진행 중 표(`_in_flight`, tag_id): confirm이 검사 전에(동기 구간에서) 태그를 잡고 끝날 때
  놓는다 — 같은 태그의 두 confirm이 edit-lock 조회(await) 사이에 둘 다 통과하지 못하게.
- 감사(D11): 의도 행 `tag.write`(outcome SENDING)가 커밋돼야 보낸다(실패 →
  `WriteUnavailableError`, 안 보냄). 결과 행(같은 write_id)·거부 행 `tag.write_rejected`는
  best-effort(실패는 경고 + 카운터). 스위치가 켜진 채 기동하면 `writes.enabled_at_boot` 1행을
  남기고, 그 행이 남기 전에는 보내지 않는다.
- 검사는 edit-lock 조회 뒤 동기 구간에서 하고, 의도 행을 쓰는 동안 바뀌었을 수 있어 보내기
  직전에 동기로 한 번 더 한다(그때 실패하면 보내지 않고 결과 행 REJECTED). 재시도는 하지 않는다
  (ADR-013 5항).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pureiron_core.acquisition.ports import (
    WriteChannel,
    WriteCommand,
    WriteReply,
    WriteReplyStatus,
)
from pureiron_core.audit.ports import (
    AuditAction,
    AuditEntry,
    AuditSource,
    AuditUnavailableError,
    StrictAuditSink,
)
from pureiron_core.auth.ports import Principal
from pureiron_core.platform.db import DatabaseUnavailableError
from pureiron_core.project.ports import Definitions, ProjectStateError, ProjectStatePort
from pureiron_core.tags.ports import TagStorePort, TagValue, Value, json_value
from pureiron_core.writes import policy
from pureiron_core.writes.ports import (
    PreparedWrite,
    WriteCheck,
    WriteExpiredError,
    WriteNotFoundError,
    WriteOutcome,
    WriteRejectedError,
    WriteReport,
    WritesStatus,
    WriteTarget,
    WriteUnavailableError,
)

log = logging.getLogger(__name__)

DEFAULT_CONFIRM_TTL_S = 60.0
DEFAULT_WRITE_TIMEOUT_S = 10.0
MAX_PENDING_PER_USER = 8
EDIT_LOCK_TIMEOUT_S = 3.0
ORIGIN_HUMAN = "human"
SENDING = "SENDING"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class _Pending:
    write_id: str
    username: str
    tag_path: str
    value: Value
    reason: str
    expires_mono: float
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _EditLock:
    owner: str | None
    error: str | None


def _value_detail(value: TagValue | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"value": json_value(value.value), "quality": value.quality.name, "ts_ns": value.ts_ns}


def _check_detail(checks: tuple[WriteCheck, ...]) -> list[dict[str, object]]:
    return [{"name": c.name.value, "ok": c.ok, "detail": c.detail} for c in checks]


class WritesService:
    def __init__(
        self,
        channel: WriteChannel,
        tags: TagStorePort,
        project: ProjectStatePort,
        audit: StrictAuditSink,
        *,
        enabled: bool,
        confirm_ttl_s: float = DEFAULT_CONFIRM_TTL_S,
        write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], str] = _new_id,
    ) -> None:
        self._channel = channel
        self._tags = tags
        self._project = project
        self._audit = audit
        self._enabled = enabled
        self._confirm_ttl_s = confirm_ttl_s
        self._write_timeout_s = write_timeout_s
        self._clock = clock
        self._wall = wall
        self._new_id = new_id
        self._targets: Mapping[str, WriteTarget] = {}
        self._tag_ids: Mapping[str, int] = {}
        self._rev: int | None = None
        self._pending: dict[str, _Pending] = {}
        self._in_flight: set[int] = set()
        self._outcomes = dict.fromkeys(WriteOutcome, 0)
        self._audit_failures = 0
        self._boot_recorded = not enabled
        self._boot_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    # --- 수명주기 -------------------------------------------------------------------------
    async def start(self) -> None:
        """스위치가 켜져 있으면 기동 감사 행을 남긴다(task — site-db가 없어도 기동을 막지
        않는다)."""
        if self._enabled:
            log.warning("PLC writes are ENABLED on this site (PUREIRON_WRITES_ENABLED)")
            self._spawn(self._record_boot_quietly())

    async def stop(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, coro: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _record_boot_quietly(self) -> None:
        try:
            await self._ensure_boot_recorded()
        except WriteUnavailableError as exc:
            log.warning(
                "writes.enabled_at_boot not recorded yet (will retry before a write): %s", exc
            )

    async def _ensure_boot_recorded(self) -> None:
        async with self._boot_lock:
            if self._boot_recorded:
                return
            entry = AuditEntry(
                action=AuditAction.WRITES_ENABLED_AT_BOOT,
                source=AuditSource.API,
                detail={
                    "confirm_ttl_s": self._confirm_ttl_s,
                    "write_timeout_s": self._write_timeout_s,
                },
            )
            await self._record_strict(entry)
            self._boot_recorded = True

    # --- 정의 -----------------------------------------------------------------------------
    def writable_tag_ids(self, definitions: Definitions) -> frozenset[int]:
        """plan `TagPoint.writable`(PlanKeeper `writable`). 스위치가 꺼지면 빈 집합."""
        if not self._enabled:
            return frozenset()
        return frozenset(t.tag_id for t in policy.parse_targets(definitions).targets.values())

    def set_definitions(self, definitions: Definitions) -> None:
        """PlanKeeper `on_definitions`(동기). 어긋난 선언은 그 태그만 쓰기 불가 + 경고."""
        parsed = policy.parse_targets(definitions)
        for problem in parsed.problems:
            log.warning("definitions rev %d: %s", definitions.rev, problem)
        self._targets = parsed.targets
        self._tag_ids = parsed.tag_ids
        self._rev = definitions.rev

    # --- 조회 -----------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def confirm_ttl_s(self) -> float:
        return self._confirm_ttl_s

    def targets(self) -> tuple[WriteTarget, ...]:
        return tuple(self._targets[path] for path in sorted(self._targets))

    def status(self) -> WritesStatus:
        return WritesStatus(
            enabled=self._enabled,
            in_flight=len(self._in_flight),
            outcomes=dict(self._outcomes),
            audit_failures_total=self._audit_failures,
        )

    # --- 검사 -----------------------------------------------------------------------------
    async def _edit_lock(self) -> _EditLock:
        try:
            async with asyncio.timeout(EDIT_LOCK_TIMEOUT_S):
                state = await self._project.get()
        except (ProjectStateError, DatabaseUnavailableError, TimeoutError) as exc:
            return _EditLock(None, str(exc) or type(exc).__name__)
        return _EditLock(state.edit_lock_owner, None)

    def _evaluate(
        self,
        principal: Principal,
        tag_path: str,
        value: Value,
        lock: _EditLock,
        *,
        in_flight: bool,
    ) -> tuple[tuple[WriteCheck, ...], WriteTarget | None, TagValue | None]:
        """동기 구간(await 없음): TagStore·수집 스냅샷으로 검사한다."""
        target = self._targets.get(tag_path)
        current = self._tags.get(target.tag_id) if target is not None else None
        permissive: dict[str, TagValue | None] = {}
        for path in target.permissive if target is not None else ():
            tag_id = self._tag_ids.get(path)
            permissive[path] = self._tags.get(tag_id) if tag_id is not None else None
        facts = policy.WriteFacts(
            enabled=self._enabled,
            tag_path=tag_path,
            target=target,
            role=principal.role,
            value=value,
            current=current,
            path=self._channel.write_path(target.device_id if target is not None else ""),
            definitions_rev=self._rev,
            permissive=permissive,
            edit_lock_owner=lock.owner,
            edit_lock_error=lock.error,
            in_flight=in_flight,
            write_timeout_s=self._write_timeout_s,
        )
        return policy.evaluate(facts), target, current

    # --- prepare --------------------------------------------------------------------------
    async def prepare(
        self, principal: Principal, tag_path: str, value: Value, reason: str
    ) -> PreparedWrite:
        lock = await self._edit_lock()
        target = self._targets.get(tag_path)
        busy = target is not None and target.tag_id in self._in_flight
        checks, target, current = self._evaluate(principal, tag_path, value, lock, in_flight=busy)
        if target is None or policy.failed(checks):
            await self._record_rejected(
                principal,
                tag_path,
                value,
                reason=reason,
                checks=checks,
                write_id=None,
                stage="prepare",
            )
            raise WriteRejectedError(checks)
        self._sweep()
        mine = [p.write_id for p in self._pending.values() if p.username == principal.username]
        for write_id in mine[: max(0, len(mine) - MAX_PENDING_PER_USER + 1)]:
            del self._pending[write_id]
        expires_at = self._wall() + timedelta(seconds=self._confirm_ttl_s)
        pending = _Pending(
            write_id=self._new_id(),
            username=principal.username,
            tag_path=tag_path,
            value=value,
            reason=reason,
            expires_mono=self._clock() + self._confirm_ttl_s,
            expires_at=expires_at,
        )
        self._pending[pending.write_id] = pending
        return PreparedWrite(pending.write_id, target, value, current, checks, expires_at)

    def _sweep(self) -> None:
        now = self._clock()
        for write_id in [w for w, p in self._pending.items() if p.expires_mono <= now]:
            del self._pending[write_id]

    def _take(self, principal: Principal, write_id: str) -> _Pending:
        """동기: 꺼내서 지운다(1회용). 다른 사용자 것은 건드리지 않는다."""
        pending = self._pending.get(write_id)
        if pending is None or pending.username != principal.username:
            raise WriteNotFoundError(write_id)
        del self._pending[write_id]
        if self._clock() >= pending.expires_mono:
            raise WriteExpiredError(write_id)
        return pending

    # --- confirm --------------------------------------------------------------------------
    async def confirm(self, principal: Principal, write_id: str) -> WriteReport:
        pending = self._take(principal, write_id)
        target = self._targets.get(pending.tag_path)
        reserved = target is not None and target.tag_id not in self._in_flight
        if target is not None and reserved:
            self._in_flight.add(target.tag_id)
        try:
            return await self._confirm(principal, pending, in_flight=not reserved)
        finally:
            if target is not None and reserved:
                self._in_flight.discard(target.tag_id)

    async def _confirm(
        self, principal: Principal, pending: _Pending, *, in_flight: bool
    ) -> WriteReport:
        lock = await self._edit_lock()
        checks, target, before = self._evaluate(
            principal, pending.tag_path, pending.value, lock, in_flight=in_flight
        )
        if target is None or policy.failed(checks):
            self._outcomes[WriteOutcome.REJECTED] += 1
            await self._record_rejected(
                principal,
                pending.tag_path,
                pending.value,
                reason=pending.reason,
                checks=checks,
                write_id=pending.write_id,
                stage="confirm",
            )
            raise WriteRejectedError(checks, pending.write_id)
        request_id = self._new_id()
        detail: dict[str, object] = {
            "write_id": pending.write_id,
            "request_id": request_id,
            "tag_path": target.path,
            "tag_id": target.tag_id,
            "kind": target.kind.value,
            "before": _value_detail(before),
            "target": pending.value,
            "reason": pending.reason,
            "origin": ORIGIN_HUMAN,
            "approved_by": principal.username,
        }
        await self._ensure_boot_recorded()
        await self._record_strict(self._entry(principal, target, {**detail, "outcome": SENDING}))
        reply = await self._send(principal, pending, target, lock, request_id)
        outcome, error = policy.classify(target, pending.value, reply)
        self._outcomes[outcome] += 1
        result: dict[str, object] = {**detail, "outcome": outcome.value}
        if reply.readback is not None:
            result["readback"] = reply.readback
            result["readback_ts_ns"] = reply.readback_ts_ns
        if error is not None:
            result["error"] = error
        await self._record_quietly(self._entry(principal, target, result))
        log.info("write %s to %s: %s", pending.write_id, target.path, outcome.value)
        return WriteReport(
            write_id=pending.write_id,
            target=target,
            value=pending.value,
            before=before,
            outcome=outcome,
            readback=reply.readback,
            readback_ts_ns=reply.readback_ts_ns,
            error=error,
        )

    async def _send(
        self,
        principal: Principal,
        pending: _Pending,
        target: WriteTarget,
        lock: _EditLock,
        request_id: str,
    ) -> WriteReply:
        # 의도 행을 쓰는 동안 허가 조건·품질·세션이 바뀌었을 수 있다 — 보내기 직전에 동기로 다시
        # 본다(`send`는 첫 await 전에 큐에 넣으므로 이 검사와 전송 사이에 다른 코드가 끼지 않는다).
        checks, _, _ = self._evaluate(
            principal, pending.tag_path, pending.value, lock, in_flight=False
        )
        if blocked := policy.failed(checks):
            names = ", ".join(c.name.value for c in blocked)
            return WriteReply(WriteReplyStatus.REJECTED, f"not sent: checks changed: {names}")
        timeout_ms = policy.send_timeout_ms(
            self._write_timeout_s, self._channel.write_path(target.device_id).device_timeout_ms
        )
        if timeout_ms is None or self._rev is None:
            return WriteReply(WriteReplyStatus.REJECTED, "not sent: no send window")
        command = WriteCommand(
            request_id=request_id,
            tag_id=target.tag_id,
            value=float(pending.value),
            plan_rev=self._rev,
            timeout_ms=timeout_ms,
        )
        return await self._channel.send(command, wait_s=self._write_timeout_s)

    # --- 감사 -----------------------------------------------------------------------------
    def _entry(
        self, principal: Principal, target: WriteTarget, detail: dict[str, object]
    ) -> AuditEntry:
        return AuditEntry(
            action=AuditAction.TAG_WRITE,
            source=AuditSource.API,
            username=principal.username,
            target=target.path,
            detail=detail,
        )

    async def _record_strict(self, entry: AuditEntry) -> None:
        try:
            await self._audit.record_strict(entry)
        except AuditUnavailableError as exc:
            self._audit_failures += 1
            raise WriteUnavailableError(f"audit record failed; write not sent: {exc}") from exc

    async def _record_quietly(self, entry: AuditEntry) -> None:
        """best-effort(결과·거부 행): 실패는 경고 + 카운터(이미 보냈거나 보내지 않은 뒤다)."""
        try:
            await self._audit.record_strict(entry)
        except AuditUnavailableError as exc:
            self._audit_failures += 1
            log.warning("audit %s for %s dropped: %s", entry.action, entry.target, exc)

    async def _record_rejected(
        self,
        principal: Principal,
        tag_path: str,
        value: Value,
        *,
        reason: str,
        checks: tuple[WriteCheck, ...],
        write_id: str | None,
        stage: str,
    ) -> None:
        target = self._targets.get(tag_path)
        await self._record_quietly(
            AuditEntry(
                action=AuditAction.TAG_WRITE_REJECTED,
                source=AuditSource.API,
                username=principal.username,
                target=tag_path,
                detail={
                    "write_id": write_id,
                    "stage": stage,
                    "tag_path": tag_path,
                    "tag_id": target.tag_id if target is not None else None,
                    "target": value,
                    "reason": reason,
                    "outcome": WriteOutcome.REJECTED.value,
                    "failed": [c.name.value for c in policy.failed(checks)],
                    "checks": _check_detail(checks),
                    "origin": ORIGIN_HUMAN,
                },
            )
        )
