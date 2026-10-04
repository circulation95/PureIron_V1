from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pureiron_project.cli import main
from pureiron_project.project import RelPathError, load_project, normalize_relpath

ACME = Path(__file__).resolve().parents[1] / "fixtures" / "acme"


@pytest.fixture
def acme(tmp_path: Path) -> Path:
    target = tmp_path / "acme"
    shutil.copytree(ACME, target)
    return target


def _hash(root: Path) -> str:
    project = load_project(root)
    assert project.ok, project.problems
    return project.content_hash()


def test_fixture_is_valid_and_canonical(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(ACME)]) == 0
    assert main(["fmt", "--check", str(ACME)]) == 0
    assert main(["hash", str(ACME)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("sha256:")
    assert len(out[-1]) == len("sha256:") + 64


def test_hash_ignores_formatting_key_order_crlf_and_explicit_defaults(acme: Path) -> None:
    baseline = _hash(acme)
    device = acme / "devices" / "plc-1.yaml"
    device.write_bytes(
        b"protocol: modbus_tcp\r\nid: plc-1\r\nport: 502\r\nhost: '10.0.0.5'\r\n"
        b'credentials: "secret://plc-1"\r\nunit_id: 1\r\npoll_ms: 1000\r\n'
    )
    (acme / "README.md").write_text("not project content", encoding="utf-8")
    assert _hash(acme) == baseline
    assert main(["fmt", "--check", str(acme)]) == 1
    assert main(["fmt", str(acme)]) == 0
    assert device.read_bytes() == (ACME / "devices" / "plc-1.yaml").read_bytes()
    assert main(["fmt", "--check", str(acme)]) == 0


def test_hash_changes_with_content(acme: Path) -> None:
    baseline = _hash(acme)
    project_file = acme / "project.yaml"
    project_file.write_text(
        project_file.read_text(encoding="utf-8").replace("400", "401"), encoding="utf-8"
    )
    assert _hash(acme) != baseline


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("devices\\plc-1.yaml", "devices/plc-1.yaml"),
        ("devices/plc-1.yaml", "devices/plc-1.yaml"),
        (".\\tags\\.\\plc-1.yaml", "tags/plc-1.yaml"),
    ],
)
def test_windows_paths_normalise_to_posix(raw: str, expected: str) -> None:
    assert normalize_relpath(raw) == expected


@pytest.mark.parametrize(
    "raw", ["C:\\x\\project.yaml", "/etc/passwd", "\\\\server\\share\\a", "a\\..\\..\\b", ""]
)
def test_unsafe_paths_are_rejected(raw: str) -> None:
    with pytest.raises(RelPathError):
        normalize_relpath(raw)


def _problems(root: Path) -> list[str]:
    return load_project(root).problems


def test_schema_violation_is_reported(acme: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (acme / "devices" / "plc-1.yaml").write_text(
        "id: plc-1\nprotocol: modbus_tcp\nhost: h\nport: 502\ncredentials: hunter2\n",
        encoding="utf-8",
    )
    assert main(["validate", str(acme)]) == 1
    assert "devices/plc-1.yaml: $.credentials" in capsys.readouterr().err


def test_cross_reference_checks(acme: Path) -> None:
    (acme / "devices" / "plc-2.yaml").write_text(
        "host: h\nid: plc-x\nport: 502\nprotocol: modbus_tcp\n", encoding="utf-8"
    )
    (acme / "tags" / "ghost.yaml").write_text(
        "- device: plc-1\n  modbus:\n    address: 1\n    data_type: int16\n"
        "    register_type: input\n"
        "  path: line1/temp\n",
        encoding="utf-8",
    )
    (acme / "alarms" / "more.yaml").write_text(
        "- id: temp-high\n  setpoint: 1\n  tag: nowhere\n  type: above\n", encoding="utf-8"
    )
    problems = "\n".join(_problems(acme))
    assert "devices/plc-2.yaml: id 'plc-x' must match file name 'plc-2'" in problems
    assert "tags/ghost.yaml: no device 'ghost'" in problems
    assert "tags/ghost.yaml: tag 'line1/temp' device 'plc-1' must be 'ghost'" in problems
    assert "duplicate tag path 'line1/temp'" in problems
    assert "duplicate alarm id 'temp-high'" in problems
    assert "alarm 'temp-high' refers to unknown tag 'nowhere'" in problems


def test_layout_rules(acme: Path) -> None:
    (acme / "screens" / "main.json").write_text("{}", encoding="utf-8")
    (acme / "devices" / "notes.txt").write_text("x", encoding="utf-8")
    (acme / "tags" / "nested").mkdir()
    problems = "\n".join(_problems(acme))
    assert "screens/main.json: screens/ is reserved" in problems
    assert "devices/notes.txt: only .yaml files are allowed" in problems
    assert "tags/nested: nested directories are not allowed" in problems


def test_missing_project_file(tmp_path: Path) -> None:
    assert _problems(tmp_path) == ["project.yaml: missing"]


def test_strict_yaml_errors_surface_with_path(acme: Path) -> None:
    (acme / "alarms" / "line1.yaml").write_text("- &a {id: x}\n- *a\n", encoding="utf-8")
    assert any("alarms/line1.yaml" in p and "anchors" in p for p in _problems(acme))


def test_symlinks_are_rejected(acme: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text("id: plc-9\n", encoding="utf-8")
    link = acme / "devices" / "plc-9.yaml"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not permitted on this host")
    assert any("symbolic links are not allowed" in p for p in _problems(acme))
    assert main(["fmt", str(acme)]) == 1
    assert outside.read_text(encoding="utf-8") == "id: plc-9\n"
