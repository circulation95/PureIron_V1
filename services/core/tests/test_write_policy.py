"""writes `policy`: 선언 해석(어긋나면 그 태그만 빠짐), D9 검사 하나씩 실패(정확히 그 이름으로),
값 규칙, 시한 산출, 결과 판정표(D10, float32 반올림 비교)."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import replace

import pytest

from pureiron_core.acquisition.ports import WritePathState, WriteReply, WriteReplyStatus
from pureiron_core.auth.ports import Role
from pureiron_core.project.ports import TagDef
from pureiron_core.tags.ports import Quality, TagValue
from pureiron_core.writes import policy
from pureiron_core.writes.policy import WriteFacts
from pureiron_core.writes.ports import CheckName, WriteKind, WriteOutcome, WriteTarget

from .write_fakes import AUTO_MODE, COMMAND, REMOTE_OK, SETPOINT, TAG_IDS, write_definitions

KNOWN = frozenset(TAG_IDS)
SP = WriteTarget(
    tag_id=10,
    path=SETPOINT,
    device_id="plc-1",
    kind=WriteKind.SETPOINT,
    role=Role.OPERATOR,
    data_type="float32",
    bit=False,
    minimum=20.0,
    maximum=90.0,
    permissive=(REMOTE_OK,),
)
CMD = WriteTarget(
    tag_id=11,
    path=COMMAND,
    device_id="plc-1",
    kind=WriteKind.COMMAND,
    role=Role.OPERATOR,
    data_type="bool",
    bit=True,
    values=(True,),
    permissive=(REMOTE_OK, AUTO_MODE),
)


def _good(tag_id: int, value: float | bool) -> TagValue:
    return TagValue(tag_id, "x", "plc-1", value, Quality.GOOD, 1)


def _facts() -> WriteFacts:
    return WriteFacts(
        enabled=True,
        tag_path=SETPOINT,
        target=SP,
        role=Role.OPERATOR,
        value=42.5,
        current=_good(10, 25.0),
        path=WritePathState(1, 5, 5, device_connected=True, device_timeout_ms=1000),
        definitions_rev=5,
        permissive={REMOTE_OK: _good(12, True)},
        edit_lock_owner=None,
        edit_lock_error=None,
        in_flight=False,
        write_timeout_s=10.0,
    )


def _path(**changes: object) -> Callable[[WriteFacts], WriteFacts]:
    return lambda f: replace(f, path=replace(f.path, **changes))


FAILURES: dict[str, tuple[CheckName, Callable[[WriteFacts], WriteFacts]]] = {
    "switch off": (CheckName.WRITES_ENABLED, lambda f: replace(f, enabled=False)),
    "role": (
        CheckName.ROLE,
        lambda f: replace(f, target=replace(SP, role=Role.ENGINEER)),
    ),
    "above max": (CheckName.VALUE, lambda f: replace(f, value=90.5)),
    "bool to setpoint": (CheckName.VALUE, lambda f: replace(f, value=True)),
    "tag BAD": (
        CheckName.TAG_GOOD,
        lambda f: replace(f, current=TagValue(10, "x", "plc-1", 25.0, Quality.BAD, 1)),
    ),
    "tag unknown": (CheckName.TAG_GOOD, lambda f: replace(f, current=None)),
    "device": (CheckName.DEVICE_CONNECTED, _path(device_connected=False)),
    "no session": (CheckName.SINGLE_SESSION, _path(sessions=0)),
    "two sessions": (CheckName.SINGLE_SESSION, _path(sessions=2)),
    "batch on old rev": (CheckName.PLAN_REPORTED, _path(reported_plan_rev=4)),
    "no batch yet": (CheckName.PLAN_REPORTED, _path(reported_plan_rev=None)),
    "plan changing": (CheckName.PLAN_REPORTED, lambda f: replace(f, definitions_rev=6)),
    "long device timeout": (CheckName.SEND_WINDOW, _path(device_timeout_ms=3501)),
    "permissive false": (
        CheckName.PERMISSIVE,
        lambda f: replace(f, permissive={REMOTE_OK: _good(12, False)}),
    ),
    "permissive BAD": (
        CheckName.PERMISSIVE,
        lambda f: replace(
            f, permissive={REMOTE_OK: TagValue(12, "x", "plc-1", True, Quality.BAD, 1)}
        ),
    ),
    "permissive missing": (CheckName.PERMISSIVE, lambda f: replace(f, permissive={})),
    "edit lock": (CheckName.NO_EDIT_LOCK, lambda f: replace(f, edit_lock_owner="updater")),
    "edit lock unreadable": (
        CheckName.NO_EDIT_LOCK,
        lambda f: replace(f, edit_lock_error="site-db error: down"),
    ),
    "in flight": (CheckName.NOT_IN_FLIGHT, lambda f: replace(f, in_flight=True)),
}


def test_all_checks_pass_in_fixed_order() -> None:
    checks = policy.evaluate(_facts())
    assert [c.name for c in checks] == list(CheckName)
    assert all(c.ok and c.detail is None for c in checks)


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_each_check_fails_alone_under_its_own_name(case: str) -> None:
    name, change = FAILURES[case]
    checks = policy.evaluate(change(_facts()))
    failed = policy.failed(checks)
    assert [c.name for c in failed] == [name]
    assert failed[0].detail


def test_undeclared_tag_reports_only_switch_and_declaration() -> None:
    checks = policy.evaluate(replace(_facts(), target=None, tag_path="line1/temp"))
    assert [(c.name, c.ok) for c in checks] == [
        (CheckName.WRITES_ENABLED, True),
        (CheckName.DECLARED, False),
    ]
    assert checks[1].detail == "tag 'line1/temp' has no write declaration"


def test_targets_are_parsed_with_defaults() -> None:
    parsed = policy.parse_targets(write_definitions())
    assert parsed.problems == ()
    assert parsed.targets == {SETPOINT: replace(SP, permissive=()), COMMAND: CMD}
    assert parsed.tag_ids[REMOTE_OK] == 12


def _with_write(write: dict[str, object], modbus: dict[str, object] | None = None) -> TagDef:
    address = modbus or {"register_type": "holding", "address": 100, "data_type": "uint16"}
    return TagDef(99, "line1/x", "plc-1", {"path": "line1/x", "modbus": address, "write": write})


@pytest.mark.parametrize(
    ("write", "modbus", "message"),
    [
        ({"kind": "setpoint", "min": 0, "max": 1}, {"register_type": "input"}, "read-only"),
        (
            {"kind": "setpoint", "min": 0, "max": 1},
            {"register_type": "holding", "data_type": "bool"},
            "16 bits",
        ),
        ({"kind": "setpoint", "min": 5, "max": 1}, None, "greater than max"),
        ({"kind": "setpoint", "min": 0.5, "max": 1}, None, "not an integer"),
        ({"kind": "setpoint", "max": 1}, None, "not a number"),
        (
            {"kind": "setpoint", "min": 0, "max": 1},
            {"register_type": "coil", "data_type": "bool"},
            "numeric data_type",
        ),
        ({"kind": "command", "values": [1], "permissive": []}, None, "needs permissive"),
        ({"kind": "command", "permissive": [REMOTE_OK]}, None, "needs values"),
        ({"kind": "command", "values": [70000], "permissive": [REMOTE_OK]}, None, "uint16"),
        ({"kind": "command", "values": [1], "permissive": ["nope"]}, None, "not a known tag"),
        ({"kind": "command", "values": [1], "permissive": ["line1/x"]}, None, "is the tag"),
        ({"kind": "setpoint", "min": 0, "max": 1, "role": "admin"}, None, "role"),
        ({"kind": "toggle"}, None, "toggle"),
    ],
)
def test_bad_declaration_raises(
    write: dict[str, object], modbus: dict[str, object] | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        policy.parse_target(_with_write(write, modbus), KNOWN | {"line1/x"})


def test_bad_declaration_drops_only_that_tag() -> None:
    bad = TagDef(
        15,
        "line1/bad",
        "plc-1",
        {
            "path": "line1/bad",
            "modbus": {"register_type": "input", "address": 5, "data_type": "uint16"},
            "write": {"kind": "setpoint", "min": 0, "max": 1},
        },
    )
    parsed = policy.parse_targets(write_definitions(5, bad))
    assert set(parsed.targets) == {SETPOINT, COMMAND}
    assert len(parsed.problems) == 1
    assert "line1/bad" in parsed.problems[0]
    assert "read-only" in parsed.problems[0]


def test_engineer_role_and_integer_command() -> None:
    target = policy.parse_target(
        _with_write(
            {"kind": "command", "values": [1, 2], "permissive": [REMOTE_OK], "role": "engineer"}
        ),
        KNOWN | {"line1/x"},
    )
    assert (target.role, target.values, target.bit) == (Role.ENGINEER, (1, 2), False)
    assert policy.value_problem(target, 2) is None
    assert policy.value_problem(target, 2.0) is None
    assert policy.value_problem(target, 3) == "3 is not an allowed value [1, 2]"
    assert policy.value_problem(target, True) == "True is not a number"


def test_value_rules() -> None:
    assert policy.value_problem(SP, 20) is None
    assert policy.value_problem(SP, 90.0) is None
    assert policy.value_problem(SP, 19.99) is not None
    assert policy.value_problem(SP, float("nan")) == "nan is not finite"
    assert policy.value_problem(CMD, True) is None
    assert policy.value_problem(CMD, False) == "False is not an allowed value [True]"
    assert policy.value_problem(CMD, 1) == "1 is not a boolean"
    uint = replace(SP, data_type="uint16", minimum=0.0, maximum=100.0)
    assert policy.value_problem(uint, 7.0) is None
    assert policy.value_problem(uint, 7.5) is not None


@pytest.mark.parametrize(
    ("write_timeout_s", "device_timeout_ms", "expected"),
    [
        (10.0, 1000, 6000),  # 10s - 여유 2s - 2 * 1s
        (10.0, 3500, 1000),  # 최소 기한 그대로
        (10.0, 3501, None),  # 최소 기한보다 짧아진다
        (10.0, None, None),  # 장비가 plan에 없다
        (60.0, 0, 58000),
        (2.5, 0, None),
    ],
)
def test_send_timeout(
    write_timeout_s: float, device_timeout_ms: int | None, expected: int | None
) -> None:
    assert policy.send_timeout_ms(write_timeout_s, device_timeout_ms) == expected


FLOAT32_20_1 = 20.100000381469727  # struct.pack('>f', 20.1) → unpack (손으로 확인한 값)


def test_float32_rounding_matches_readback() -> None:
    assert struct.unpack(">f", struct.pack(">f", 20.1))[0] == FLOAT32_20_1
    assert policy.written_value(SP, 20.1) == FLOAT32_20_1
    assert policy.written_value(replace(SP, data_type="float64"), 20.1) == 20.1
    ok = WriteReply(WriteReplyStatus.OK, readback=FLOAT32_20_1)
    assert policy.classify(SP, 20.1, ok) == (WriteOutcome.CONFIRMED, None)
    # float64 형식이면 반올림하지 않는다 — float32 값은 불일치
    outcome, error = policy.classify(replace(SP, data_type="float64"), 20.1, ok)
    assert outcome is WriteOutcome.MISMATCH
    assert error == f"readback {FLOAT32_20_1!r} differs from written 20.1"


@pytest.mark.parametrize(
    ("target", "reply", "expected"),
    [
        (SP, WriteReply(WriteReplyStatus.OK, readback=50.0), WriteOutcome.CONFIRMED),
        (SP, WriteReply(WriteReplyStatus.OK, readback=25.0), WriteOutcome.MISMATCH),
        (SP, WriteReply(WriteReplyStatus.OK, "readback interrupted"), WriteOutcome.WRITTEN),
        (CMD, WriteReply(WriteReplyStatus.OK, readback=0.0), WriteOutcome.WRITTEN),
        (CMD, WriteReply(WriteReplyStatus.OK), WriteOutcome.WRITTEN),
        (SP, WriteReply(WriteReplyStatus.REJECTED, "queue full"), WriteOutcome.REJECTED),
        (SP, WriteReply(WriteReplyStatus.FAILED, "exception 3"), WriteOutcome.FAILED),
        (SP, WriteReply(WriteReplyStatus.INDETERMINATE, "timeout"), WriteOutcome.NO_RESPONSE),
        (CMD, WriteReply(WriteReplyStatus.NO_RESPONSE, "closed"), WriteOutcome.NO_RESPONSE),
    ],
)
def test_classification_table(
    target: WriteTarget, reply: WriteReply, expected: WriteOutcome
) -> None:
    value: float | bool = True if target.bit else 50.0
    assert policy.classify(target, value, reply)[0] is expected


def test_setpoint_ack_without_readback_is_written_with_reason() -> None:
    reply = WriteReply(WriteReplyStatus.OK, "readback failed: exception 2")
    assert policy.classify(SP, 50.0, reply) == (
        WriteOutcome.WRITTEN,
        "readback unavailable: readback failed: exception 2",
    )
