"""writes `service`: prepare→confirm 감사 2행(SENDING·결과, 같은 write_id), TTL 만료 410, 다른
사용자 404, 이중 confirm 1회만 전송, 같은 태그 진행 중 거부, 의도 행 실패 → 안 보냄, 보내기 직전
재검사, 거부 감사(prepare·confirm), 스위치 꺼짐, 기동 감사 행, 정의 오류 태그만 제외."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import replace

import pytest

from pureiron_core.acquisition.ports import WriteCommand, WriteReply, WriteReplyStatus
from pureiron_core.audit.ports import AuditAction, AuditEntry
from pureiron_core.auth.ports import Role
from pureiron_core.project.ports import TagDef
from pureiron_core.tags.ports import Quality
from pureiron_core.writes.ports import (
    CheckName,
    WriteExpiredError,
    WriteNotFoundError,
    WriteOutcome,
    WriteRejectedError,
    WriteUnavailableError,
)

from .alarm_fakes import eventually
from .write_fakes import (
    COMMAND,
    REMOTE_OK,
    REV,
    SETPOINT,
    T0,
    WriteFixture,
    principal,
    set_tag,
    write_definitions,
    write_fixture,
)

OP = principal()


def _failed(error: WriteRejectedError) -> list[CheckName]:
    return [c.name for c in error.checks if not c.ok]


async def test_setpoint_confirmed_with_two_audit_rows() -> None:
    fx = write_fixture()
    prepared = await fx.service.prepare(OP, SETPOINT, 25.5, "온도 조정")
    assert prepared.target.tag_id == 10
    assert prepared.current is not None
    assert prepared.current.value == 25.0
    assert prepared.expires_at == T0.replace(minute=1)
    assert fx.channel.sent == []  # prepare는 아무것도 보내지 않는다
    assert fx.audit.rows() == []

    report = await fx.service.confirm(OP, prepared.write_id)
    assert (report.outcome, report.readback, report.error) == (WriteOutcome.CONFIRMED, 25.5, None)
    [(command, wait_s)] = fx.channel.sent
    assert command == WriteCommand(
        request_id=command.request_id, tag_id=10, value=25.5, plan_rev=REV, timeout_ms=6000
    )
    assert command.request_id != prepared.write_id
    assert wait_s == 10.0
    assert fx.audit.actions() == [("tag.write", "SENDING"), ("tag.write", "CONFIRMED")]
    sending, result = fx.audit.rows()
    assert (sending.username, sending.target) == ("op", SETPOINT)
    assert sending.detail["write_id"] == result.detail["write_id"] == prepared.write_id
    assert sending.detail["request_id"] == command.request_id
    assert sending.detail["before"] == {
        "value": 25.0,
        "quality": "GOOD",
        "ts_ns": 1_791_000_000_000_000_000,
    }
    for key, value in {
        "tag_path": SETPOINT,
        "tag_id": 10,
        "kind": "setpoint",
        "target": 25.5,
        "reason": "온도 조정",
        "origin": "human",
        "approved_by": "op",
    }.items():
        assert sending.detail[key] == value, key
    assert "readback" not in sending.detail
    assert (result.detail["readback"], result.detail["readback_ts_ns"]) == (25.5, 1)
    status = fx.service.status()
    assert (status.enabled, status.in_flight, status.audit_failures_total) == (True, 0, 0)
    assert status.outcomes[WriteOutcome.CONFIRMED] == 1


async def test_command_written_and_bool_sent_as_one() -> None:
    fx = write_fixture()
    fx.channel.reply = WriteReply(WriteReplyStatus.OK, readback=0.0)  # PLC가 명령 비트를 지움
    prepared = await fx.service.prepare(OP, COMMAND, True, "기동")
    report = await fx.service.confirm(OP, prepared.write_id)
    assert report.outcome is WriteOutcome.WRITTEN
    assert fx.channel.sent[0][0].value == 1.0
    assert fx.audit.entries[-1].detail["readback"] == 0.0


async def test_expired_prepare_is_410_and_consumed() -> None:
    fx = write_fixture(confirm_ttl_s=60.0)
    prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    fx.clock.now += 60.0
    with pytest.raises(WriteExpiredError):
        await fx.service.confirm(OP, prepared.write_id)
    with pytest.raises(WriteNotFoundError):
        await fx.service.confirm(OP, prepared.write_id)
    assert fx.channel.sent == []


async def test_other_user_cannot_confirm_and_does_not_consume() -> None:
    fx = write_fixture()
    prepared = await fx.service.prepare(OP, SETPOINT, 25.5, "r")
    with pytest.raises(WriteNotFoundError):
        await fx.service.confirm(principal("other", Role.ADMIN), prepared.write_id)
    assert (await fx.service.confirm(OP, prepared.write_id)).outcome is WriteOutcome.CONFIRMED
    with pytest.raises(WriteNotFoundError):
        await fx.service.confirm(OP, "f" * 32)


async def test_double_confirm_sends_once() -> None:
    fx = write_fixture()
    fx.channel.reply = WriteReply(WriteReplyStatus.OK, readback=30.0)
    prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    results = await asyncio.gather(
        fx.service.confirm(OP, prepared.write_id),
        fx.service.confirm(OP, prepared.write_id),
        return_exceptions=True,
    )
    assert sum(isinstance(r, WriteNotFoundError) for r in results) == 1
    assert len(fx.channel.sent) == 1


async def test_same_tag_in_flight_rejects_second_confirm_and_prepare() -> None:
    fx = write_fixture()
    fx.channel.gate = asyncio.Event()
    fx.channel.reply = WriteReply(WriteReplyStatus.OK, readback=30.0)
    first = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    second = await fx.service.prepare(principal("op2"), SETPOINT, 40.0, "r")
    running = asyncio.create_task(fx.service.confirm(OP, first.write_id))
    await eventually(lambda: bool(fx.channel.sent))
    assert fx.service.status().in_flight == 1
    with pytest.raises(WriteRejectedError) as rejected:
        await fx.service.confirm(principal("op2"), second.write_id)
    assert _failed(rejected.value) == [CheckName.NOT_IN_FLIGHT]
    with pytest.raises(WriteRejectedError) as again:
        await fx.service.prepare(OP, SETPOINT, 31.0, "r")
    assert _failed(again.value) == [CheckName.NOT_IN_FLIGHT]
    # 다른 태그는 막지 않는다
    assert (await fx.service.prepare(OP, COMMAND, True, "r")).target.path == COMMAND
    fx.channel.gate.set()
    assert (await running).outcome is WriteOutcome.CONFIRMED
    assert fx.service.status().in_flight == 0
    assert len(fx.channel.sent) == 1
    rejected_rows = [e for e in fx.audit.entries if e.action is AuditAction.TAG_WRITE_REJECTED]
    assert [e.detail["stage"] for e in rejected_rows] == ["confirm", "prepare"]


async def test_intent_audit_failure_sends_nothing() -> None:
    fx = write_fixture()
    prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    fx.audit.down = True
    with pytest.raises(WriteUnavailableError):
        await fx.service.confirm(OP, prepared.write_id)
    assert fx.channel.sent == []
    assert fx.service.status().audit_failures_total == 1
    assert fx.service.status().in_flight == 0


async def test_result_row_loss_is_counted_after_send() -> None:
    fx = write_fixture()
    fx.channel.reply = WriteReply(WriteReplyStatus.OK, readback=30.0)
    prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    rows: list[str] = []

    async def drop_result(entry: AuditEntry) -> None:
        if entry.action is AuditAction.TAG_WRITE:
            outcome = str(entry.detail.get("outcome"))
            rows.append(outcome)
            fx.audit.down = outcome != "SENDING"

    fx.audit.during = drop_result
    report = await fx.service.confirm(OP, prepared.write_id)
    assert report.outcome is WriteOutcome.CONFIRMED
    assert rows == ["SENDING", "CONFIRMED"]
    assert fx.audit.actions() == [("tag.write", "SENDING")]
    assert fx.service.status().audit_failures_total == 1


async def test_checks_changed_while_recording_intent_are_not_sent() -> None:
    fx = write_fixture()
    prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    fx.service.set_definitions(
        replace(
            write_definitions(),
            tags=tuple(
                TagDef(t.tag_id, t.path, t.device_id, _with_permissive(t))
                for t in write_definitions().tags
            ),
        )
    )

    async def drop_permissive(entry: AuditEntry) -> None:
        if entry.detail.get("outcome") == "SENDING":
            set_tag(fx.tags, REMOTE_OK, False)

    fx.audit.during = drop_permissive
    report = await fx.service.confirm(OP, prepared.write_id)
    assert report.outcome is WriteOutcome.REJECTED
    assert report.error == "not sent: checks changed: permissive"
    assert fx.channel.sent == []
    assert fx.audit.actions() == [("tag.write", "SENDING"), ("tag.write", "REJECTED")]


async def test_edit_lock_taken_while_recording_intent_is_not_sent() -> None:
    fx = write_fixture()
    prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")

    async def start_update(entry: AuditEntry) -> None:
        if entry.detail.get("outcome") == "SENDING":
            fx.project.owner = "updater"  # 의도 행을 쓰는 동안 업데이트가 시작됐다

    fx.audit.during = start_update
    report = await fx.service.confirm(OP, prepared.write_id)
    assert report.outcome is WriteOutcome.REJECTED
    assert report.error == "not sent: checks changed: no_edit_lock"
    assert fx.channel.sent == []
    assert fx.audit.actions() == [("tag.write", "SENDING"), ("tag.write", "REJECTED")]


def _with_permissive(tag: TagDef) -> dict[str, object]:
    definition = dict(tag.definition)
    if tag.path == SETPOINT:
        definition["write"] = {"kind": "setpoint", "min": 20, "max": 90, "permissive": [REMOTE_OK]}
    return definition


async def test_prepare_rejection_is_audited_with_checks() -> None:
    fx = write_fixture()
    with pytest.raises(WriteRejectedError) as rejected:
        await fx.service.prepare(OP, SETPOINT, 95.0, "too hot")
    assert _failed(rejected.value) == [CheckName.VALUE]
    [row] = fx.audit.rows()
    assert row.action is AuditAction.TAG_WRITE_REJECTED
    assert (row.username, row.target) == ("op", SETPOINT)
    for key, value in {
        "stage": "prepare",
        "write_id": None,
        "tag_id": 10,
        "target": 95.0,
        "reason": "too hot",
        "outcome": "REJECTED",
        "failed": ["value"],
        "origin": "human",
    }.items():
        assert row.detail[key] == value, key
    assert fx.service.status().outcomes[WriteOutcome.REJECTED] == 0  # prepare 거부는 세지 않는다


async def test_confirm_rechecks_everything() -> None:
    fx = write_fixture()
    prepared = await fx.service.prepare(OP, COMMAND, True, "r")
    set_tag(fx.tags, REMOTE_OK, True, Quality.BAD)
    fx.project.owner = "updater"
    with pytest.raises(WriteRejectedError) as rejected:
        await fx.service.confirm(OP, prepared.write_id)
    assert _failed(rejected.value) == [CheckName.PERMISSIVE, CheckName.NO_EDIT_LOCK]
    assert rejected.value.write_id == prepared.write_id
    assert fx.channel.sent == []
    assert fx.service.status().outcomes[WriteOutcome.REJECTED] == 1
    assert fx.audit.entries[-1].detail["stage"] == "confirm"


async def test_edit_lock_unreadable_fails_closed() -> None:
    fx = write_fixture()
    fx.project.available = False
    with pytest.raises(WriteRejectedError) as rejected:
        await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    assert _failed(rejected.value) == [CheckName.NO_EDIT_LOCK]


async def test_engineer_tag_needs_engineer() -> None:
    fx = write_fixture()
    engineer_only = TagDef(
        15,
        "line1/recipe",
        "plc-1",
        {
            "path": "line1/recipe",
            "modbus": {"register_type": "holding", "address": 110, "data_type": "uint16"},
            "write": {"kind": "setpoint", "min": 0, "max": 9, "role": "engineer"},
        },
    )
    fx.service.set_definitions(write_definitions(REV, engineer_only))
    with pytest.raises(WriteRejectedError) as rejected:
        await fx.service.prepare(OP, "line1/recipe", 3, "r")
    assert CheckName.ROLE in _failed(rejected.value)
    with pytest.raises(WriteRejectedError) as engineer:
        await fx.service.prepare(principal("eng", Role.ENGINEER), "line1/recipe", 3, "r")
    assert CheckName.ROLE not in _failed(engineer.value)  # 남은 실패는 TagStore에 없는 태그(품질)


async def test_no_response_and_failed_outcomes_are_reported() -> None:
    fx = write_fixture()
    for reply, expected in (
        (
            WriteReply(WriteReplyStatus.NO_RESPONSE, "collector session closed"),
            WriteOutcome.NO_RESPONSE,
        ),
        (WriteReply(WriteReplyStatus.INDETERMINATE, "timeout"), WriteOutcome.NO_RESPONSE),
        (WriteReply(WriteReplyStatus.FAILED, "exception 3"), WriteOutcome.FAILED),
        (WriteReply(WriteReplyStatus.OK, readback=99.0), WriteOutcome.MISMATCH),
    ):
        fx.channel.reply = reply
        prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
        report = await fx.service.confirm(OP, prepared.write_id)
        assert report.outcome is expected
        assert fx.audit.entries[-1].detail["outcome"] == expected.value
    outcomes = fx.service.status().outcomes
    assert (outcomes[WriteOutcome.NO_RESPONSE], outcomes[WriteOutcome.FAILED]) == (2, 1)


async def test_non_finite_readback_is_mismatch_and_result_row_is_kept() -> None:
    # 레지스터가 NaN/Inf로 남아도 결과 행은 jsonb에 들어가야 한다(fake 감사는 allow_nan=False).
    fx = write_fixture()
    for raw in (math.nan, math.inf):
        fx.channel.reply = WriteReply(WriteReplyStatus.OK, readback=raw, readback_ts_ns=7)
        prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
        report = await fx.service.confirm(OP, prepared.write_id)
        assert report.outcome is WriteOutcome.MISMATCH
        result = fx.audit.rows()[-1].detail
        assert result["outcome"] == "MISMATCH"
        assert (result["readback"], result["readback_raw"]) == (None, repr(raw))
        assert result["readback_ts_ns"] == 7
    assert fx.service.status().audit_failures_total == 0
    assert fx.audit.actions() == [("tag.write", "SENDING"), ("tag.write", "MISMATCH")] * 2


async def test_switch_off_rejects_and_plan_is_read_only() -> None:
    fx = write_fixture(enabled=False)
    assert fx.service.writable_tag_ids(write_definitions()) == frozenset()
    assert [t.path for t in fx.service.targets()] == [COMMAND, SETPOINT]  # 선언은 보인다
    with pytest.raises(WriteRejectedError) as rejected:
        await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    assert _failed(rejected.value) == [CheckName.WRITES_ENABLED]
    await fx.service.start()
    assert [e.action for e in fx.audit.entries] == [AuditAction.TAG_WRITE_REJECTED]  # 기동 행 없음
    assert fx.service.status().enabled is False


async def test_writable_tag_ids_follow_declarations_when_enabled() -> None:
    fx = write_fixture()
    assert fx.service.writable_tag_ids(write_definitions()) == frozenset({10, 11})


async def _boot_attempted(fx: WriteFixture) -> None:
    await eventually(lambda: AuditAction.WRITES_ENABLED_AT_BOOT in fx.audit.attempts)


async def test_boot_audit_row_is_required_before_first_write() -> None:
    fx = write_fixture()
    fx.audit.failing = {AuditAction.WRITES_ENABLED_AT_BOOT}
    await fx.service.start()
    await _boot_attempted(fx)
    prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    with pytest.raises(WriteUnavailableError):
        await fx.service.confirm(OP, prepared.write_id)
    assert fx.channel.sent == []
    fx.audit.failing = set()
    prepared = await fx.service.prepare(OP, SETPOINT, 30.0, "r")
    fx.channel.reply = WriteReply(WriteReplyStatus.OK, readback=30.0)
    assert (await fx.service.confirm(OP, prepared.write_id)).outcome is WriteOutcome.CONFIRMED
    assert [e.action.value for e in fx.audit.entries] == [
        "writes.enabled_at_boot",
        "tag.write",
        "tag.write",
    ]
    await fx.service.stop()


async def test_boot_audit_recorded_once() -> None:
    fx = write_fixture()
    await fx.service.start()
    await _boot_attempted(fx)
    prepared = await fx.service.prepare(OP, SETPOINT, 25.5, "r")
    await fx.service.confirm(OP, prepared.write_id)
    await fx.service.stop()
    boots = [e for e in fx.audit.entries if e.action is AuditAction.WRITES_ENABLED_AT_BOOT]
    assert len(boots) == 1
    assert boots[0].detail == {"confirm_ttl_s": 60.0, "write_timeout_s": 10.0}
    assert fx.audit.entries[0].action is AuditAction.WRITES_ENABLED_AT_BOOT


async def test_pending_per_user_is_capped_oldest_first() -> None:
    fx = write_fixture()
    ids = [(await fx.service.prepare(OP, SETPOINT, 30.0, "r")).write_id for _ in range(9)]
    with pytest.raises(WriteNotFoundError):
        await fx.service.confirm(OP, ids[0])
    fx.channel.reply = WriteReply(WriteReplyStatus.OK, readback=30.0)
    assert (await fx.service.confirm(OP, ids[-1])).outcome is WriteOutcome.CONFIRMED


def test_bad_declaration_logs_and_drops_only_that_tag(caplog: pytest.LogCaptureFixture) -> None:
    fx = write_fixture()
    bad = TagDef(
        15,
        "line1/bad",
        "plc-1",
        {
            "path": "line1/bad",
            "modbus": {"register_type": "discrete", "address": 1, "data_type": "bool"},
            "write": {"kind": "command", "values": [True], "permissive": [REMOTE_OK]},
        },
    )
    with caplog.at_level(logging.WARNING, logger="pureiron_core.writes.service"):
        fx.service.set_definitions(write_definitions(REV, bad))
    assert [t.path for t in fx.service.targets()] == [COMMAND, SETPOINT]
    assert "line1/bad" in caplog.text
    assert 15 not in fx.service.writable_tag_ids(write_definitions(REV, bad))
