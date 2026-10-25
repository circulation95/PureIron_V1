"""legacy 변환기: 지원·미지원 분류, 비밀값 비복사, 결정성, 종료 코드 0/1/2, demo-coffee 변환."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from pureiron_project.cli import main
from pureiron_project.project import load_project

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINI = FIXTURES / "legacy-mini"
MINI_CONVERTED = FIXTURES / "legacy-mini-converted"
DEMO_COFFEE = Path(__file__).resolve().parents[3] / "projects" / "demo-coffee"
PROJECT_JSON = '{"schema_version": 1, "project_id": "tiny", "name": "Tiny"}'
MODBUS_CONNECTION = """connections:
  - name: PLC
    host: 10.0.0.9
    tags:
      - tag_path: "[Modbus]Line/level"
        register_type: holding
        address: 7
"""
# 출력·보고서 어디에도 나오면 안 되는 fixture 값(비밀값·사용자 이름·연락처).
SECRET_VALUES = ("fixture-secret-value", "kim@example.com", "operator.kim", "010-0000-0000", "$2b$")


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _convert(source: Path, tmp_path: Path, name: str = "out") -> tuple[int, Path, dict[str, Any]]:
    out, report = tmp_path / name, tmp_path / f"{name}.json"
    code = main(["convert-legacy", str(source), "--out", str(out), "--report", str(report)])
    data: dict[str, Any] = json.loads(report.read_text("utf-8")) if report.exists() else {}
    return code, out, data


def _entries(report: dict[str, Any]) -> list[tuple[str, str, str, str | None]]:
    return [(e["file"], e["item"], e["kind"], e.get("field")) for e in report["unsupported"]]


def _legacy(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "legacy"
    for rel, text in {"project.json": PROJECT_JSON, **files}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def test_mini_converts_supported_items_to_the_expected_canonical_tree(tmp_path: Path) -> None:
    code, out, report = _convert(MINI, tmp_path)
    assert code == 2
    assert _tree(out) == _tree(MINI_CONVERTED)
    assert main(["validate", str(out)]) == 0
    assert main(["fmt", "--check", str(out)]) == 0
    assert report["content_hash"] == load_project(MINI_CONVERTED).content_hash()
    assert report["summary"]["devices"] == 1
    assert report["summary"]["tags"] == 3
    assert report["summary"]["alarms"] == 2


def test_mini_reports_every_unsupported_item(tmp_path: Path) -> None:
    _, _, report = _convert(MINI, tmp_path)
    assert _entries(report) == [
        ("README.txt", "$", "file", None),
        ("alarms/line-a.yaml", "LineA Flow Low", "field", "priority"),
        ("alarms/line-a.yaml", "LineA Flow Low", "shelve", "shelve_duration_s"),
        ("alarms/line-a.yaml", "LineA Run Equal", "alarm", None),
        ("alarms/line-a.yaml", "Memory High", "alarm", None),
        ("auth/users.json", "$", "users", None),
        ("connections/plc.yaml", "Serial PLC", "modbus_rtu_connection", None),
        ("connections/plc.yaml", "[Modbus]Serial/level", "modbus_tag", None),
        ("dashboard/process_dashboards.json", "LINE_A", "dashboard", None),
        ("dashboard/user_dashboards.json", "$", "dashboard", None),
        ("project.json", "features.coffee_demo", "feature", None),
        ("scripts/hello.py", "$", "script", None),
        ("settings/user_settings.json", "$", "settings", None),
        ("tags/tags.json", "[Memory]LineA/setpoint", "memory_tag", None),
        ("tags/tags.json", "[Expressions]LineA/avg", "expression_tag", None),
        ("tags/tags.json", "[S7]LineA/s7", "device_tag", None),
        ("tags/tags.json", "[Line-A PLC]LineA/old", "excluded_tag", None),
        ("udt/types.yaml", "VALVE", "udt_type", None),
    ]
    assert report["summary"]["unsupported"] == 18


def test_secret_values_and_user_data_are_never_copied(tmp_path: Path) -> None:
    _, out, report = _convert(MINI, tmp_path)
    blobs = [*_tree(out).values(), (tmp_path / "out.json").read_bytes()]
    for value in SECRET_VALUES:
        assert not any(value.encode() in blob for blob in blobs), value
    assert report["secrets"] == [
        {
            "field": "password",
            "file": "connections/plc.yaml",
            "item": "Line-A PLC",
            "ref": "secret://line-a-plc",
        }
    ]


@pytest.mark.parametrize(
    ("rel", "text", "expected"),
    [
        (
            "connections/ua.yaml",
            'name: UA\nendpoint_url: "opc.tcp://h:4840"\npassword: pw\n'
            'tags:\n  - tag_path: "[OPC-UA]a"\n',
            [("UA", "opcua_connection"), ("[OPC-UA]a", "opcua_tag")],
        ),
        ("connections/odd.yaml", "foo: 1\n", [("$", "file")]),
        (
            "tags/devices.json",
            '[{"name": "UA2", "type": "OPC UA", "hostname": "h", "port": 4840, "config": {}}]',
            [("UA2", "opcua_connection")],
        ),
        (
            "tags/more.yaml",
            'tags:\n  - path: "[Q]q"\n    tag_type: Query\n'
            '  - path: "[PLC]x"\n    tag_type: Device\n    device_name: Other\n'
            "    address_config: {protocol: modbus, address: 1}\n",
            [("[Q]q", "query_tag"), ("[PLC]x", "modbus_tag")],
        ),
        ("tags/legacy.json", '[{"path": "[UNKNOWN]X", "metadata": {}}]', [("[UNKNOWN]X", "tag")]),
        (
            "udt/instances.yaml",
            'udt_instances:\n  - udt_type: VALVE\n    path: "[Modbus]V1"\n',
            [("[Modbus]V1", "udt_instance")],
        ),
        ("udt/types/PUMP.json", '{"name": "PUMP"}', [("PUMP", "udt_type")]),
        ("alarms/a.yml", "alarms: []\n", [("$", "file")]),
        ("tags/sample/x.csv", "a,b\n", [("$", "file")]),
    ],
)
def test_classification_table(
    tmp_path: Path, rel: str, text: str, expected: list[tuple[str, str]]
) -> None:
    root = _legacy(tmp_path, {"connections/plc.yaml": MODBUS_CONNECTION, rel: text})
    code, _, report = _convert(root, tmp_path)
    assert code == 2
    assert [(item, kind) for _, item, kind, _ in _entries(report)] == expected
    assert {e["file"] for e in report["unsupported"]} == {rel}


def test_store_tag_addresses_follow_the_connection_addressing_rule(tmp_path: Path) -> None:
    connection = (
        "connections:\n  - name: PLC\n    host: h\n    zero_based_addressing: true\n"
        "    reverse_word_order: true\n"
    )
    tag = {
        "path": "[PLC]a",
        "tag_config": {
            "tag_type": "Device",
            "device_name": "PLC",
            "address_config": {"protocol": "modbus", "address": 41, "data_type": "float32"},
        },
    }
    root = _legacy(
        tmp_path, {"connections/plc.yaml": connection, "tags/tags.json": json.dumps([tag])}
    )
    code, out, _ = _convert(root, tmp_path)
    assert code == 0
    assert (out / "tags" / "plc.yaml").read_text("utf-8") == (
        "- device: plc\n  modbus:\n    address: 41\n    data_type: float32\n"
        "    register_type: holding\n    word_order: little\n  path: a\n"
    )


def test_flat_yaml_device_tag_and_name_without_ascii_get_ids(tmp_path: Path) -> None:
    connection = 'connections:\n  - name: "보일러"\n    host: h\n'
    tags = (
        'tags:\n  - path: "[보일러]boiler/temp"\n    tag_type: Device\n'
        "    device_name: 보일러\n    scan_rate_ms: 200\n"
        "    address_config: {protocol: modbus, address: 5, data_type: int16}\n"
    )
    alarms = (
        'alarms:\n  - name: 온도 높음\n    tag_path: "[보일러]boiler/temp"\n'
        "    alarm_type: AboveSetpoint\n    setpoint: 90\n"
    )
    files = {
        "connections/plc.yaml": connection,
        "tags/boiler.yaml": tags,
        "alarms/boiler.yaml": alarms,
        ".git/config": "[core]\n",
    }
    code, out, report = _convert(_legacy(tmp_path, files), tmp_path)
    assert code == 2
    assert _entries(report) == [
        ("tags/boiler.yaml", "[보일러]boiler/temp", "field", "scan_rate_ms")
    ]
    assert (out / "devices" / "device-1.yaml").exists()
    assert (out / "tags" / "device-1.yaml").read_text("utf-8") == (
        "- device: device-1\n  modbus:\n    address: 4\n    data_type: int16\n"
        "    register_type: holding\n  path: boiler/temp\n"
    )
    assert (out / "alarms" / "boiler.yaml").read_text("utf-8") == (
        "- id: alarm-1\n  setpoint: 90\n  tag: boiler/temp\n  type: above\n"
    )


def test_supported_only_project_exits_0_with_empty_unsupported(tmp_path: Path) -> None:
    root = _legacy(tmp_path, {"connections/plc.yaml": MODBUS_CONNECTION, ".gitkeep": ""})
    code, out, report = _convert(root, tmp_path)
    assert code == 0
    assert report["unsupported"] == []
    assert report["sources"] == ["connections/plc.yaml", "project.json"]
    assert (out / "tags" / "plc.yaml").read_text("utf-8") == (
        "- device: plc\n  modbus:\n    address: 7\n    data_type: uint16\n"
        "    register_type: holding\n  path: Line/level\n"
    )


@pytest.mark.parametrize(
    "files",
    [
        {"project.json": "{not json"},
        {"project.json": '{"project_id": "Bad ID", "name": "x"}'},
        {"project.json": '{"project_id": "ok", "name": ""}'},
        {"connections/plc.yaml": "connections: [\n"},
        {"tags/tags.json": "[NaN]"},
        {"alarms/a.yaml": "alarms:\n  - a: 1\n  - a: 1\n    a: 2\n"},
    ],
)
def test_unreadable_or_invalid_input_exits_1_without_output(
    tmp_path: Path, files: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, report = _convert(_legacy(tmp_path, files), tmp_path)
    assert code == 1
    assert not out.exists()
    assert report == {}
    assert capsys.readouterr().err.startswith("error: ")


def test_missing_input_or_non_empty_output_exits_1(tmp_path: Path) -> None:
    code, _, _ = _convert(tmp_path / "absent", tmp_path)
    assert code == 1
    (tmp_path / "busy").mkdir()
    (tmp_path / "busy" / "keep.txt").write_text("x", encoding="utf-8")
    code, _, _ = _convert(_legacy(tmp_path, {"project.json": PROJECT_JSON}), tmp_path, "busy")
    assert code == 1
    assert (tmp_path / "busy" / "keep.txt").read_text("utf-8") == "x"


def test_missing_project_json_exits_1(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    root.mkdir()
    (root / "connections").mkdir()
    code, _, _ = _convert(root, tmp_path)
    assert code == 1


@pytest.fixture(scope="module")
def demo_runs(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[int, Path, dict[str, Any]]]:
    assert DEMO_COFFEE.is_dir(), DEMO_COFFEE
    base = tmp_path_factory.mktemp("demo")
    return [_convert(DEMO_COFFEE, base, f"run{i}") for i in (1, 2)]


def test_demo_coffee_converts_validates_and_reports_unsupported(
    demo_runs: list[tuple[int, Path, dict[str, Any]]],
) -> None:
    code, out, report = demo_runs[0]
    assert code == 2
    assert load_project(out).ok
    assert main(["fmt", "--check", str(out)]) == 0
    files = {e["file"] for e in report["unsupported"]}
    for rel in (
        "connections/opcua-example.yaml",
        "scripts/example-scripts.py",
        "dashboard/process_dashboards.json",
        "dashboard/user_dashboards.json",
        "auth/users.json",
        "settings/user_settings.json",
        "udt/types.yaml",
        "udt/instances.yaml",
        "tags/gbrb_tags.json",
        "tags/tags.json",
    ):
        assert rel in files, rel
    assert sum(e["file"] == "tags/gbrb_tags.json" for e in report["unsupported"]) == 2383
    assert sum(e["file"] == "tags/tags.json" for e in report["unsupported"]) == 38
    assert report["summary"]["devices"] == 1
    assert report["summary"]["tags"] == 4


def test_demo_coffee_every_file_is_read_or_reported(
    demo_runs: list[tuple[int, Path, dict[str, Any]]],
) -> None:
    _, _, report = demo_runs[0]
    every = {
        p.relative_to(DEMO_COFFEE).as_posix()
        for p in DEMO_COFFEE.rglob("*")
        if p.is_file() and p.name != ".gitkeep"
    }
    accounted = set(report["sources"]) | {e["file"] for e in report["unsupported"]}
    assert accounted == every
    assert report["summary"]["files"] == len(every)


def test_conversion_is_deterministic(
    demo_runs: list[tuple[int, Path, dict[str, Any]]], tmp_path: Path
) -> None:
    (_, out1, _), (_, out2, _) = demo_runs
    assert _tree(out1) == _tree(out2)
    assert (out1.parent / "run1.json").read_bytes() == (out2.parent / "run2.json").read_bytes()
    copy = tmp_path / "mini-copy"
    shutil.copytree(MINI, copy)
    _convert(MINI, tmp_path, "a")
    _convert(copy, tmp_path, "b")
    assert _tree(tmp_path / "a") == _tree(tmp_path / "b")
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
