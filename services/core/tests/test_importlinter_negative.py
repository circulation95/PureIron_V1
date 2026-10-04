"""import-linter 계약이 실제로 위반을 잡는지(음성 fixture) 확인한다(PD-8, G1-4)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]
SRC = CORE / "src" / "pureiron_core"
CONFIG = CORE / ".importlinter"
FIXTURES = Path(__file__).resolve().parent / "importlinter_fixtures"
NO_CONTRACT = "(none)"


def _fixture_meta(path: Path) -> tuple[str, set[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    target = lines[0].removeprefix("# target:").strip()
    breaks = {part.strip() for part in lines[1].removeprefix("# breaks:").split(";")}
    return target, breaks - {NO_CONTRACT}


def _lint_imports(pythonpath: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONUTF8": "1"}
    lint = shutil.which("lint-imports")
    assert lint is not None, "lint-imports not on PATH (run via `uv run`)"
    return subprocess.run(
        [lint, "--config", str(CONFIG), "--no-cache"],
        cwd=CORE,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _contract_names() -> list[str]:
    return re.findall(r"^name = (.+)$", CONFIG.read_text(encoding="ascii"), flags=re.MULTILINE)


@pytest.mark.parametrize("fixture", sorted(FIXTURES.glob("*.fixture")), ids=lambda p: p.stem)
def test_fixture_breaks_exactly_its_contract(fixture: Path, tmp_path: Path) -> None:
    target, breaks = _fixture_meta(fixture)
    copy = tmp_path / "pureiron_core"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(fixture, copy / target)

    result = _lint_imports(tmp_path)
    output = result.stdout + result.stderr
    for name in _contract_names():
        verdict = re.search(rf"^{re.escape(name)} (KEPT|BROKEN)", output, flags=re.MULTILINE)
        expected = "BROKEN" if name in breaks else "KEPT"
        assert verdict is not None, output
        assert verdict[1] == expected, f"{name}: expected {expected}\n{output}"
    assert breaks <= set(_contract_names()), f"unknown contract in fixture: {breaks}"
    assert (result.returncode != 0) == bool(breaks), output


def test_contracts_hold_on_real_tree() -> None:
    result = _lint_imports(SRC.parent)
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_subpackage_is_classified() -> None:
    """새 하위 패키지가 계약 밖으로 새지 않게: entry 모듈 외 패키지는 (a)·(b)에 있어야 한다."""
    config = CONFIG.read_text(encoding="ascii")
    entry_modules = {"runtime", "app", "cli", "settings", "_gen"}
    packages = sorted(p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").exists())
    missing = [
        name
        for name in packages
        if name not in entry_modules and config.count(f"pureiron_core.{name}") < 2
    ]
    assert not missing, f"unclassified pureiron_core subpackages: {missing}"
