"""legacy 변환기 경계·적대적 입력(M5-1 리뷰 r1): 조용히 버리지 않음, lax 값, 실패 시 쓴 것 없음."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from pureiron_project.cli import main
from pureiron_project.convert_legacy import convert_legacy, write_tree
from pureiron_project.legacy_files import ConvertError
from pureiron_project.legacy_values import as_bool, as_int, as_number

PROJECT_JSON = '{"schema_version": 1, "project_id": "tiny", "name": "Tiny"}'
CONNECTION = "connections:\n  - name: PLC\n    host: h\n"


def _legacy(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "legacy"
    for rel, text in {"project.json": PROJECT_JSON, **files}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _convert(tmp_path: Path, files: dict[str, str]) -> tuple[int, Path, dict[str, Any]]:
    out, report = tmp_path / "out", tmp_path / "out.json"
    root = _legacy(tmp_path, files)
    code = main(["convert-legacy", str(root), "--out", str(out), "--report", str(report)])
    data: dict[str, Any] = json.loads(report.read_text("utf-8")) if report.exists() else {}
    return code, out, data


def _entries(report: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
    return [(e["file"], e["item"], e["kind"], e.get("field")) for e in report["unsupported"]]


def _reasons(report: dict[str, Any]) -> list[str]:
    return [e["reason"] for e in report["unsupported"]]


def _store(path: str, address: dict[str, Any], **config: Any) -> dict[str, Any]:
    """장비 PLC의 TagStore Modbus 태그(JSON 저장본 모양). 기본 read_only true."""
    tag_config = {
        "tag_type": "Device",
        "device_name": "PLC",
        "address_config": {"protocol": "modbus", **address},
        "read_only": True,
        **config,
    }
    return {"path": path, "tag_config": tag_config}


def _tags_json(*tags: dict[str, Any]) -> str:
    return json.dumps(list(tags))


# --- H1: 장비 행 config가 JSON 문자열 -------------------------------------------------------

_ROW_CONFIG = {
    "unit_id": 7,
    "zero_based_addressing": True,
    "reverse_word_order": True,
    "password": "pw-secret",
}


@pytest.mark.parametrize("config", [_ROW_CONFIG, json.dumps(_ROW_CONFIG)], ids=["dict", "string"])
def test_modbus_device_row_config_object_or_json_string_converts(
    tmp_path: Path, config: object
) -> None:
    row = {"name": "PLC", "type": "Modbus TCP", "hostname": "10.0.0.1", "port": 502}
    files = {
        "tags/devices.json": json.dumps([{**row, "config": config}]),
        "tags/tags.json": _tags_json(_store("[PLC]a", {"address": 10, "data_type": "float32"})),
    }
    code, out, report = _convert(tmp_path, files)
    assert code == 0, report["unsupported"]
    assert (out / "devices" / "plc.yaml").read_text("utf-8") == (
        "credentials: secret://plc\nhost: 10.0.0.1\nid: plc\nport: 502\n"
        "protocol: modbus_tcp\nunit_id: 7\n"
    )
    assert (out / "tags" / "plc.yaml").read_text("utf-8") == (
        "- device: plc\n  modbus:\n    address: 10\n    data_type: float32\n"
        "    register_type: holding\n    word_order: little\n  path: a\n"
    )
    assert report["secrets"] == [
        {"field": "password", "file": "tags/devices.json", "item": "PLC", "ref": "secret://plc"}
    ]
    blobs = [p.read_bytes() for p in out.rglob("*") if p.is_file()]
    assert not any(b"pw-secret" in blob for blob in [*blobs, (tmp_path / "out.json").read_bytes()])


@pytest.mark.parametrize(
    ("row_extra", "reason"),
    [
        ({}, "config is NULL (legacy did not start this device)"),
        ({"config": None}, "config is NULL (legacy did not start this device)"),
        ({"config": "{not json"}, "config is not valid JSON"),
        ({"config": "[1]"}, "config is not a JSON object"),
        ({"config": 5}, "config is not a JSON object"),
    ],
)
def test_modbus_device_row_with_unusable_config_is_reported(
    tmp_path: Path, row_extra: dict[str, Any], reason: str
) -> None:
    row = {"name": "PLC", "type": "Modbus TCP", "hostname": "h", "port": 502, **row_extra}
    code, _, report = _convert(tmp_path, {"tags/devices.json": json.dumps([row])})
    assert code == 2
    assert _entries(report) == [("tags/devices.json", "PLC", "modbus_connection", None)]
    assert _reasons(report) == [reason]


# --- H2: zero_based_addressing·reverse_word_order lax bool ----------------------------------


@pytest.mark.parametrize(
    ("flag", "address"),
    [("true", 10), (1, 10), ("yes", 10), ("On", 10), ("false", 9), (0, 9), ("no", 9)],
)
def test_addressing_flag_is_read_as_legacy_pydantic_bool(
    tmp_path: Path, flag: object, address: int
) -> None:
    config = {"zero_based_addressing": flag}
    row = {"name": "PLC", "type": "Modbus TCP", "hostname": "h", "port": 502, "config": config}
    files = {
        "tags/devices.json": json.dumps([row]),
        "tags/tags.json": _tags_json(_store("[PLC]a", {"address": 10})),
    }
    code, out, _ = _convert(tmp_path, files)
    assert code == 0
    assert f"address: {address}\n" in (out / "tags" / "plc.yaml").read_text("utf-8")


@pytest.mark.parametrize("flag", ["maybe", 2, None, [True]])
def test_addressing_flag_that_is_not_a_bool_rejects_the_connection(
    tmp_path: Path, flag: object
) -> None:
    connection = {"connections": [{"name": "PLC", "host": "h", "reverse_word_order": flag}]}
    code, _, report = _convert(tmp_path, {"connections/plc.json": json.dumps(connection)})
    assert code == 2
    assert _entries(report) == [("connections/plc.json", "PLC", "modbus_connection", None)]
    assert _reasons(report) == ["reverse_word_order is not a boolean"]


# --- H3·M1: 목록 아닌 값·mapping 아닌 항목·읽지 않는 최상위 키 ---------------------------------


def test_non_mapping_entries_are_reported_not_dropped(tmp_path: Path) -> None:
    files = {
        "connections/plc.yaml": (
            "connections:\n  - name: PLC\n    host: h\n    tags: [just-a-string, 42]\n  - PLC2\n"
        ),
        "alarms/a.yaml": "alarms: [null, High]\n",
        "tags/tags.json": '["[PLC]x", 5]',
        "udt/types.yaml": "types: [VALVE]\n",
    }
    code, _, report = _convert(tmp_path, files)
    assert code == 2
    assert _entries(report) == [
        ("alarms/a.yaml", "alarms[0]", "alarm", None),
        ("alarms/a.yaml", "alarms[1]", "alarm", None),
        ("connections/plc.yaml", "PLC.tags[0]", "modbus_tag", None),
        ("connections/plc.yaml", "PLC.tags[1]", "modbus_tag", None),
        ("connections/plc.yaml", "connections[1]", "connection", None),
        ("tags/tags.json", "[0]", "tag", None),
        ("tags/tags.json", "[1]", "tag", None),
        ("udt/types.yaml", "[0]", "udt_type", None),
    ]
    assert set(_reasons(report)) == {"entry is not a mapping"}


def test_inline_tags_that_are_not_a_list_are_reported(tmp_path: Path) -> None:
    files = {"connections/plc.yaml": CONNECTION + "    tags: {a: 1}\n"}
    code, _, report = _convert(tmp_path, files)
    assert code == 2
    assert _entries(report) == [("connections/plc.yaml", "PLC.tags", "modbus_tag", None)]
    assert _reasons(report) == ["not a list"]


def test_unread_top_level_keys_are_reported(tmp_path: Path) -> None:
    files = {
        "connections/plc.yaml": CONNECTION + "extra_connections:\n  - name: PLC9\n",
        "tags/more.yaml": "tags: []\nscan_groups: [fast]\n",
        "alarms/a.yaml": "alarms: []\nmore_alarms: [x]\n",
    }
    code, _, report = _convert(tmp_path, files)
    assert code == 2
    assert _entries(report) == [
        ("alarms/a.yaml", "more_alarms", "file", None),
        ("connections/plc.yaml", "extra_connections", "file", None),
        ("tags/more.yaml", "scan_groups", "file", None),
    ]


# --- M2: NaN·Infinity·2^53 초과, 실패하면 쓴 것 없음 -------------------------------------------


@pytest.mark.parametrize("timeout", [".nan", ".inf", "-.inf", "1.0e300", "0.01"])
def test_timeout_out_of_range_is_reported_as_field(tmp_path: Path, timeout: str) -> None:
    code, out, report = _convert(
        tmp_path, {"connections/plc.yaml": CONNECTION + f"    timeout_s: {timeout}\n"}
    )
    assert code == 2
    assert _entries(report) == [("connections/plc.yaml", "PLC", "field", "timeout_s")]
    assert "timeout_ms" not in (out / "devices" / "plc.yaml").read_text("utf-8")


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("setpoint: .inf", ("alarm", None)),
        ("setpoint: .nan", ("alarm", None)),
        ("setpoint: 1152921504606846976", ("alarm", None)),
        ("setpoint: 90\n    deadband: .nan", ("field", "deadband")),
        ("setpoint: 90\n    on_delay_s: -1", ("field", "on_delay_s")),
    ],
)
def test_alarm_numbers_outside_the_json_model_are_reported(
    tmp_path: Path, option: str, expected: tuple[str, str | None]
) -> None:
    connection = (
        CONNECTION + '    tags:\n      - tag_path: "[PLC]a"\n        register_type: holding\n'
    )
    alarms = (
        'alarms:\n  - name: High\n    tag_path: "[PLC]a"\n'
        f"    alarm_type: AboveSetpoint\n    {option}\n"
    )
    files = {"connections/plc.yaml": connection, "alarms/a.yaml": alarms}
    code, out, report = _convert(tmp_path, files)
    assert code == 2
    assert [(kind, key) for _, _, kind, key in _entries(report)] == [expected]
    assert main(["validate", str(out)]) == 0


def test_write_tree_writes_nothing_when_the_output_is_invalid(tmp_path: Path) -> None:
    root = _legacy(tmp_path, {"connections/plc.yaml": CONNECTION})
    for bad in ("x" * 300, math.nan):
        conv = convert_legacy(root)
        conv.devices[0].doc["host"] = bad
        out = tmp_path / f"out-{type(bad).__name__}"
        with pytest.raises(ConvertError, match="converted output is invalid"):
            write_tree(conv, out)
        assert not out.exists()


# --- M3·L6·L7: 신뢰할 수 없는 legacy 파일 --------------------------------------------------


def _alias_bomb(levels: int) -> str:
    lines = ["l0: &l0 [x, x, x, x, x, x, x, x, x, x]"]
    for i in range(1, levels + 1):
        lines.append(f"l{i}: &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]")
    return "\n".join(lines) + "\n"


def test_yaml_alias_expansion_is_capped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _convert(tmp_path, {"tags/bomb.yaml": _alias_bomb(6)})
    assert code == 1
    assert not out.exists()
    assert "more than 1000000 values" in capsys.readouterr().err


@pytest.mark.parametrize(
    "rel", ["udt/types.yaml", "udt/deep.json"], ids=["alias-bomb", "deep-nesting"]
)
def test_unreadable_report_only_file_is_reported_not_fatal(tmp_path: Path, rel: str) -> None:
    text = _alias_bomb(6) if rel.endswith(".yaml") else "[" * 100_000 + "]" * 100_000
    code, _, report = _convert(tmp_path, {rel: text})
    assert code == 2
    assert [(f, item) for f, item, _, _ in _entries(report)] == [(rel, "$")]


def test_deeply_nested_converted_file_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _convert(tmp_path, {"tags/deep.json": "[" * 100_000 + "]" * 100_000})
    assert code == 1
    assert "nested too deeply" in capsys.readouterr().err


@pytest.mark.parametrize(
    "text",
    [
        'connections:\n  - name: PLC\n    password: "hunter2-secret\n',
        "connections:\n  - name: PLC\n    password: a\n    password: hunter2-secret\n",
    ],
    ids=["unclosed-quote", "duplicate-key"],
)
def test_yaml_errors_do_not_echo_file_content(
    tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _convert(tmp_path, {"connections/plc.yaml": text})
    err = capsys.readouterr().err
    assert code == 1
    assert "line " in err
    assert "hunter2-secret" not in err


def test_keys_that_collide_as_text_exit_1(tmp_path: Path) -> None:
    code, _, _ = _convert(tmp_path, {"connections/plc.yaml": CONNECTION + '    1: a\n    "1": b\n'})
    assert code == 1


# --- M4: legacy YAML 태그 추론·평면 형식 ----------------------------------------------------


def test_yaml_tags_follow_legacy_inference_and_flat_format(tmp_path: Path) -> None:
    tags = (
        "tags:\n"
        '  - path: "[Modbus]a"\n    device_name: PLC\n    read_only: true\n'
        "    address_config: {protocol: modbus, address: 5}\n"
        '  - path: "[Modbus]b"\n    tag_type: Device\n    protocol: modbus\n'
        '    opc_server: PLC\n    opc_item_path: "7"\n    read_only: true\n'
        '  - path: "[PLC]c"\n    device_name: PLC\n'
        "    address_config: {protocol: modbus, address: 5}\n"
        '  - path: "[OPC]d"\n    opc_server: PLC\n    opc_item_path: "ns=2;s=x"\n'
        '  - path: "[Modbus]e"\n    connection: PLC\n    protocol: modbus\n'
        "    read_only: true\n    tag_config: {tag_type: Device}\n"
    )
    code, out, report = _convert(
        tmp_path, {"connections/plc.yaml": CONNECTION, "tags/t.yaml": tags}
    )
    assert code == 2
    assert _entries(report) == [
        ("tags/t.yaml", "[PLC]c", "memory_tag", None),
        ("tags/t.yaml", "[OPC]d", "opcua_tag", None),
        ("tags/t.yaml", "[Modbus]e", "field", "tag_config"),
    ]
    assert (out / "tags" / "plc.yaml").read_text("utf-8") == (
        "- device: plc\n  modbus:\n    address: 4\n    data_type: uint16\n"
        "    register_type: holding\n  path: a\n"
        "- device: plc\n  modbus:\n    address: 6\n    data_type: uint16\n"
        "    register_type: holding\n  path: b\n"
        "- device: plc\n  modbus:\n    address: 0\n    data_type: uint16\n"
        "    register_type: holding\n  path: e\n"
    )


# --- M5: 같은 알람 이름을 다른 태그에 ------------------------------------------------------


def test_same_alarm_name_on_different_tags_gets_a_tag_qualified_id(tmp_path: Path) -> None:
    connection = CONNECTION + (
        '    tags:\n      - tag_path: "[PLC]a"\n        register_type: holding\n'
        '      - tag_path: "[PLC]b"\n        register_type: holding\n        address: 1\n'
    )

    def high(tag: str, setpoint: str = "90") -> str:
        return (
            f'  - name: High\n    tag_path: "[PLC]{tag}"\n'
            f"    alarm_type: AboveSetpoint\n    setpoint: {setpoint}\n"
        )

    alarms = "alarms:\n" + high("a") + high("b") + high("a") + high("a") + high("b", "abc")
    code, out, report = _convert(
        tmp_path, {"connections/plc.yaml": connection, "alarms/a.yaml": alarms}
    )
    assert code == 2
    assert [a["to"] for a in report["converted"] if a["kind"] == "alarm"] == [
        "high",
        "b-high",
        "a-high",
    ]
    assert _reasons(report) == ["duplicate alarm id 'high'", "setpoint is not a finite number"]
    assert main(["validate", str(out)]) == 0


# --- M6·M7: 쓰기 권한·수집 정책·알람 옵션·경로 ------------------------------------------------


@pytest.mark.parametrize(
    ("read_only", "reported"),
    [({}, True), ({"read_only": False}, True), ({"read_only": "true"}, False)],
)
def test_store_tag_write_permission_default_is_reported(
    tmp_path: Path, read_only: dict[str, Any], reported: bool
) -> None:
    tag = _store("[PLC]a", {"address": 1})
    del tag["tag_config"]["read_only"]
    tag["tag_config"].update(read_only)
    files = {"connections/plc.yaml": CONNECTION, "tags/tags.json": _tags_json(tag)}
    code, _, report = _convert(tmp_path, files)
    assert _entries(report) == (
        [("tags/tags.json", "[PLC]a", "field", "read_only")] if reported else []
    )
    assert code == (2 if reported else 0)


def test_collection_policy_follows_legacy_scope(tmp_path: Path) -> None:
    connection = CONNECTION + (
        '    tags:\n      - tag_path: "[PLC]x"\n        register_type: holding\n'
        '      - tag_path: "[PLC]y"\n        register_type: holding\n        address: 1\n'
    )
    tags = _tags_json(
        _store("[PLC]z", {"address": 3}),
        {**_store("[PLC]w", {"address": 4}), "source_kind": "driver_derived"},
        _store("[PLC]old", {"address": 5}),
    )
    files = {
        "connections/plc.yaml": connection,
        "tags/tags.json": tags,
        "tags/approved_paths.json": '["[PLC]x"]',
        "tags/excluded_paths.json": '["[PLC]old"]',
    }
    code, _, report = _convert(tmp_path, files)
    assert code == 2
    assert _entries(report) == [
        ("connections/plc.yaml", "[PLC]y", "excluded_tag", None),
        ("tags/tags.json", "[PLC]w", "field", "history"),
        ("tags/tags.json", "[PLC]old", "field", "history"),
    ]
    assert [c["to"] for c in report["converted"] if c["kind"] == "tag"] == ["x", "z", "w", "old"]


def test_approved_paths_that_is_not_a_list_means_no_allowlist(tmp_path: Path) -> None:
    files = {
        "connections/plc.yaml": CONNECTION + '    tags:\n      - tag_path: "[PLC]x"\n'
        "        register_type: holding\n",
        "tags/approved_paths.json": '{"a": 1}',
    }
    code, _, report = _convert(tmp_path, files)
    assert code == 2
    assert _entries(report) == [("tags/approved_paths.json", "$", "file", None)]
    assert report["summary"]["tags"] == 1


def test_alarm_enabled_flag_and_options_are_reported(tmp_path: Path) -> None:
    connection = (
        CONNECTION + '    tags:\n      - tag_path: "[PLC]a"\n        register_type: holding\n'
    )

    def above(name: str, extra: str) -> str:
        return (
            f'  - name: {name}\n    tag_path: "[PLC]a"\n'
            f"    alarm_type: AboveSetpoint\n    setpoint: 1\n{extra}"
        )

    alarms = "alarms:\n" + "".join(
        [
            above("Disabled", '    enabled: "false"\n'),
            above("Unclear", "    enabled: maybe\n"),
            above(
                "Options",
                '    enabled: "yes"\n    off_delay_s: 5\n    notification_channels: [mail]\n',
            ),
        ]
    )
    code, _, report = _convert(
        tmp_path, {"connections/plc.yaml": connection, "alarms/a.yaml": alarms}
    )
    assert code == 2
    assert _entries(report) == [
        ("alarms/a.yaml", "Disabled", "alarm", None),
        ("alarms/a.yaml", "Unclear", "alarm", None),
        ("alarms/a.yaml", "Options", "field", "notification_channels"),
        ("alarms/a.yaml", "Options", "field", "off_delay_s"),
    ]
    assert _reasons(report)[:2] == [
        "disabled alarm; V1 project alarms have no enabled flag",
        "enabled is not a boolean; V1 project alarms have no enabled flag",
    ]


def test_inline_tag_paths_and_addressing(tmp_path: Path) -> None:
    connection = (
        "connections:\n  - name: PLC\n    host: h\n    reverse_word_order: true\n    tags:\n"
        '      - {tag_path: "[PLC]보일러/온도", register_type: holding}\n'
        '      - {tag_path: "[PLC]bad path", register_type: holding}\n'
        '      - {tag_path: "[A]x", register_type: holding, data_type: float32}\n'
        '      - {tag_path: "[B]x", register_type: holding, address: 1}\n'
        '      - {tag_path: "[PLC]f", register_type: holding, data_type: float64}\n'
    )
    code, out, report = _convert(tmp_path, {"connections/plc.yaml": connection})
    assert code == 2
    assert _entries(report) == [
        ("connections/plc.yaml", "[PLC]보일러/온도", "non_ascii_tag_path", None),
        ("connections/plc.yaml", "[PLC]bad path", "modbus_tag", None),
        ("connections/plc.yaml", "[B]x", "modbus_tag", None),
        ("connections/plc.yaml", "[PLC]f", "modbus_tag", None),
    ]
    assert report["summary"]["unsupported_by_kind"]["non_ascii_tag_path"] == 1
    # 연결 안 태그에는 reverse_word_order가 적용되지 않는다(legacy `_configured_mappings`).
    assert (out / "tags" / "plc.yaml").read_text("utf-8") == (
        "- device: plc\n  modbus:\n    address: 0\n    data_type: float32\n"
        "    register_type: holding\n  path: x\n"
    )


def test_store_tag_without_protocol_and_highest_one_based_address(tmp_path: Path) -> None:
    no_protocol = _store("[PLC]u", {})
    del no_protocol["tag_config"]["address_config"]["protocol"]
    tags = _tags_json(no_protocol, _store("[PLC]top", {"address": 65536}))
    files = {"connections/plc.yaml": CONNECTION, "tags/tags.json": tags}
    code, out, report = _convert(tmp_path, files)
    assert code == 2
    assert _entries(report) == [("tags/tags.json", "[PLC]u", "opcua_tag", None)]
    assert "address: 65535\n" in (out / "tags" / "plc.yaml").read_text("utf-8")


@pytest.mark.parametrize(
    ("extra", "entry"),
    [
        ('    host: "' + "h" * 254 + '"\n', ("connections/plc.yaml", "PLC", "modbus_connection")),
        ('    port: "502"\n    unit_id: "3"\n', None),
    ],
    ids=["host-too-long", "numeric-strings"],
)
def test_connection_values_are_checked_before_writing(
    tmp_path: Path, extra: str, entry: tuple[str, str, str] | None
) -> None:
    connection = "connections:\n  - name: PLC\n" + extra
    code, out, report = _convert(tmp_path, {"connections/plc.yaml": connection})
    assert [(f, i, k) for f, i, k, _ in _entries(report)] == ([entry] if entry else [])
    if entry is None:
        assert code == 0
        assert (out / "devices" / "plc.yaml").read_text("utf-8") == (
            "host: localhost\nid: plc\nport: 502\nprotocol: modbus_tcp\nunit_id: 3\n"
        )


def test_secret_keys_follow_the_legacy_pattern_with_camel_case(tmp_path: Path) -> None:
    connection = CONNECTION + "    apiKey: k-value\n    location: plant-7\n    retries: 3\n"
    code, out, report = _convert(tmp_path, {"connections/plc.yaml": connection})
    assert code == 2
    assert [s["field"] for s in report["secrets"]] == ["apiKey", "location"]
    assert _entries(report) == [("connections/plc.yaml", "PLC", "field", "retries")]
    text = (out / "devices" / "plc.yaml").read_text("utf-8")
    assert "k-value" not in text
    assert "plant-7" not in text


# --- 종료 코드 2의 두 의미 ----------------------------------------------------------------


def test_usage_error_is_exit_2_with_empty_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["convert-legacy"])
    captured = capsys.readouterr()
    assert exc.value.code == 2
    assert captured.out == ""
    assert "usage:" in captured.err


# --- lax 값 해석(legacy pydantic v2) --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        (1.0, True),
        ("TRUE", True),
        ("off", False),
        ("t", True),
        ("n", False),
        (2, None),
        ("maybe", None),
        (None, None),
        (math.nan, None),
    ],
)
def test_as_bool(value: Any, expected: bool | None) -> None:
    assert as_bool(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        (5.0, 5),
        (" 7 ", 7),
        ("-1", None),
        (5.5, None),
        (True, None),
        ("x", None),
        (11, None),
    ],
)
def test_as_int(value: Any, expected: int | None) -> None:
    assert as_int(value, 0, 10) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.5, 1.5),
        ("2.5", 2.5),
        (2**53 - 1, 2**53 - 1),
        (2**53, None),
        (math.inf, None),
        ("nan", None),
        (False, None),
    ],
)
def test_as_number(value: Any, expected: float | None) -> None:
    assert as_number(value) == expected
