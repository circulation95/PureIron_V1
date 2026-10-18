"""쓰기 선언(`write` 블록)이 스키마 모양과 주소·형식 교차 검사를 둘 다 거쳐야 통과하는지."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pureiron_project.canonical import Json, load
from pureiron_project.project import load_project
from pureiron_project.schema import Kind, omit_defaults, validate
from pureiron_project.writes import value_problem

ACME = Path(__file__).resolve().parents[1] / "fixtures" / "acme"

PERMISSIVE = "- device: plc-1\n  modbus: {register_type: coil, address: 11, data_type: bool}\n"
TEMP = (
    "- {path: line1/temp, device: plc-1,"
    " modbus: {register_type: holding, address: 0, data_type: float32}}\n"
)
COIL = "{register_type: coil, address: 2, data_type: bool}"


def _holding(address: int, data_type: str) -> str:
    return f"{{register_type: holding, address: {address}, data_type: {data_type}}}"


def _tag(path: str, modbus: str, write: str) -> str:
    return f"- path: {path}\n  device: plc-1\n  modbus: {modbus}\n  write: {write}\n"


@pytest.fixture
def acme(tmp_path: Path) -> Path:
    target = tmp_path / "acme"
    shutil.copytree(ACME, target)
    return target


def _problems(root: Path, tags: str) -> list[str]:
    (root / "tags" / "plc-1.yaml").write_text(
        PERMISSIVE + "  path: ok\n" + TEMP + tags, encoding="utf-8"
    )
    return load_project(root).problems


def test_fixture_declares_setpoint_and_command() -> None:
    project = load_project(ACME)
    assert project.ok, project.problems
    tags = next(f.data for f in project.files if f.relpath == "tags/plc-1.yaml")
    assert isinstance(tags, list)
    writes = {str(t["path"]): t["write"] for t in tags if isinstance(t, dict) and "write" in t}
    assert writes == {
        "line1/temp_sp": {"kind": "setpoint", "min": 20, "max": 90},
        "line1/start": {"kind": "command", "values": [True], "permissive": ["line1/remote_ok"]},
    }


@pytest.mark.parametrize(
    ("write", "message"),
    [
        ("{kind: setpoint, min: 1}", "'max' is a required property"),
        ("{kind: setpoint, min: 1, max: 2, values: [1]}", "should not be valid"),
        ("{kind: command, values: [1]}", "'permissive' is a required property"),
        ("{kind: command, values: [], permissive: [ok]}", "should be non-empty"),
        ("{kind: command, values: [1], permissive: []}", "should be non-empty"),
        ("{kind: command, min: 0, values: [1], permissive: [ok]}", "should not be valid"),
        ("{kind: setpoint, min: 1, max: 2, role: admin}", "is not one of"),
        ("{kind: toggle}", "is not one of"),
        ("{kind: setpoint, min: 1, max: 2, extra: 1}", "Additional properties"),
    ],
)
def test_schema_rejects_malformed_declarations(write: str, message: str) -> None:
    document = load(_tag("t", "{register_type: holding, address: 0, data_type: uint16}", write))
    errors = validate(Kind.TAG_LIST, document)
    assert any(message in error for error in errors), errors


def test_role_default_is_omitted_in_canonical_form() -> None:
    tags: Json = [
        {
            "path": "t",
            "device": "plc-1",
            "modbus": {"register_type": "holding", "address": 0, "data_type": "uint16"},
            "write": {"kind": "setpoint", "min": 0, "max": 9, "role": "operator"},
        }
    ]
    assert validate(Kind.TAG_LIST, tags) == []
    canonical = omit_defaults(Kind.TAG_LIST, tags)
    assert isinstance(canonical, list)
    first = canonical[0]
    assert isinstance(first, dict)
    assert first["write"] == {"kind": "setpoint", "min": 0, "max": 9}


@pytest.mark.parametrize(
    ("modbus", "write", "message"),
    [
        (
            "{register_type: input, address: 0, data_type: uint16}",
            "{kind: setpoint, min: 0, max: 1}",
            "register_type 'input' is read-only",
        ),
        (
            "{register_type: discrete, address: 0, data_type: bool}",
            "{kind: command, values: [true], permissive: [ok]}",
            "register_type 'discrete' is read-only",
        ),
        (
            "{register_type: coil, address: 0, data_type: bool}",
            "{kind: setpoint, min: 0, max: 1}",
            "setpoint needs a numeric data_type",
        ),
        (
            "{register_type: holding, address: 0, data_type: bool}",
            "{kind: setpoint, min: 0, max: 1}",
            "setpoint needs a numeric data_type",
        ),
        (
            "{register_type: holding, address: 0, data_type: int16}",
            "{kind: setpoint, min: -40000, max: 10}",
            "min: -40000 is not an integer in int16 range",
        ),
        (
            "{register_type: holding, address: 0, data_type: uint16}",
            "{kind: setpoint, min: 0, max: 1.5}",
            "max: 1.5 is not an integer in uint16 range",
        ),
        (
            "{register_type: holding, address: 0, data_type: float32}",
            "{kind: setpoint, min: 0, max: 1.0e39}",
            "outside float32 range",
        ),
        (
            "{register_type: holding, address: 0, data_type: float32}",
            "{kind: setpoint, min: 10, max: 1}",
            "min 10 is greater than max 1",
        ),
        (
            "{register_type: coil, address: 0, data_type: bool}",
            "{kind: command, values: [1], permissive: [ok]}",
            "values: 1 is not a boolean",
        ),
        (
            "{register_type: holding, address: 0, data_type: uint16}",
            "{kind: command, values: [true], permissive: [ok]}",
            "values: True is not a number",
        ),
        (
            "{register_type: holding, address: 0, data_type: uint16}",
            "{kind: command, values: [1, 70000], permissive: [ok]}",
            "values: 70000 is not an integer in uint16 range",
        ),
        (
            "{register_type: coil, address: 0, data_type: bool}",
            "{kind: command, values: [true], permissive: [nowhere]}",
            "permissive 'nowhere' is not a known tag",
        ),
        (
            "{register_type: coil, address: 0, data_type: bool}",
            "{kind: command, values: [true], permissive: [t]}",
            "permissive 't' is the tag itself",
        ),
        (
            "{register_type: coil, address: 0, data_type: bool}",
            "{kind: command, values: [true], permissive: [line1/temp]}",
            "permissive 'line1/temp' is not a bool tag",
        ),
    ],
)
def test_cross_checks_reject_unwritable_declarations(
    acme: Path, modbus: str, write: str, message: str
) -> None:
    problems = _problems(acme, _tag("t", modbus, write))
    assert [p for p in problems if message in p], problems
    assert all(p.startswith("tags/plc-1.yaml: tag ") for p in problems), problems


def test_valid_declarations_pass(acme: Path) -> None:
    tags = (
        _tag("sp", _holding(0, "int16"), "{kind: setpoint, min: -100, max: 100, role: engineer}")
        + _tag("cmd", _holding(1, "uint16"), "{kind: command, values: [0, 1], permissive: [ok]}")
        + _tag("go", COIL, "{kind: command, values: [true, false], permissive: [ok]}")
        + _tag("big", _holding(4, "float64"), "{kind: setpoint, min: -1.0e+300, max: 1.0e+300}")
        + _tag("gate", _holding(8, "float32"), "{kind: setpoint, min: 0, max: 1, permissive: [ok]}")
    )
    assert _problems(acme, tags) == []


@pytest.mark.parametrize(
    ("value", "data_type", "bit", "ok"),
    [
        (True, "bool", True, True),
        (1, "bool", True, False),
        (-32768, "int16", False, True),
        (32768, "int16", False, False),
        (4294967295, "uint32", False, True),
        (-1, "uint32", False, False),
        (2.0, "uint16", False, True),
        (2.5, "int32", False, False),
        (3.4e38, "float32", False, True),
        (3.5e38, "float32", False, False),
        (1.0e308, "float64", False, True),
        ("1", "float64", False, False),
    ],
)
def test_value_problem_by_type(value: Json, data_type: str, bit: bool, ok: bool) -> None:
    assert (value_problem(value, data_type, bit=bit) is None) is ok
