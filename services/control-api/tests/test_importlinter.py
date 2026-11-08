"""import-linter 계약이 실제 트리에서 지켜지고, 위반을 실제로 잡는지(core와 같은 방식, PD-8).

(d)는 M4-0 리뷰 L-6: control-api는 `pureiron_core`를 의존하지만 `platform`(DB 풀·migrate)만 쓴다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
SRC = SERVICE / "src" / "pureiron_control"
CORE_SRC = SERVICE.parent / "core" / "src" / "pureiron_core"
CONFIG = SERVICE / ".importlinter"
ENTRY_MODULES = {"runtime", "app", "cli", "cli_admin", "settings", "__init__"}
A = "(a) runtime/app are imported only by entry points"
B = "(b) api > domain > platform"
C = "(c) domain modules do not import each other"
D = "(d) control-api uses only pureiron_core.platform"
CONTRACTS = (A, B, C, D)


def _lint_imports(pythonpath: Path) -> str:
    lint = shutil.which("lint-imports")
    assert lint is not None, "lint-imports not on PATH (run via `uv run`)"
    result = subprocess.run(
        [lint, "--config", str(CONFIG), "--no-cache"],
        cwd=SERVICE,
        env={**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONUTF8": "1"},
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout + result.stderr


def _verdicts(output: str) -> dict[str, str]:
    verdicts: dict[str, str] = {}
    for name in CONTRACTS:
        match = re.search(rf"^{re.escape(name)} (KEPT|BROKEN)", output, re.MULTILINE)
        assert match is not None, output
        verdicts[name] = match[1]
    return verdicts


def test_contracts_hold_on_real_tree() -> None:
    assert _verdicts(_lint_imports(SRC.parent)) == dict.fromkeys(CONTRACTS, "KEPT")


@pytest.mark.parametrize(
    ("module", "line", "broken"),
    [
        # 간접 사슬이 다른 계약까지 깨지 않도록 의존이 없는 모듈을 import한다.
        ("api/health.py", "import pureiron_control.runtime", A),
        ("reports.py", "import pureiron_control.api.errors", B),
        ("platform/envelopes.py", "import pureiron_control.api.errors", B),
        ("reports.py", "import pureiron_control.sites", C),
        # core 비-platform 모듈: 직접(도메인)·진입점 모듈 어디서든 (d)만 깨진다.
        ("reports.py", "import pureiron_core.historian", D),
        ("cli.py", "from pureiron_core.settings import Settings", D),
        ("platform/clock.py", "import pureiron_core.api", D),
    ],
)
def test_violation_breaks_exactly_its_contract(
    tmp_path: Path, module: str, line: str, broken: str
) -> None:
    copy = tmp_path / "pureiron_control"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    target = copy / module
    target.write_text(target.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")
    verdicts = _verdicts(_lint_imports(tmp_path))
    assert verdicts == {name: "BROKEN" if name == broken else "KEPT" for name in CONTRACTS}


def test_every_module_is_classified() -> None:
    """새 모듈이 계약 밖으로 새지 않게: 진입점 외 최상위 모듈·패키지는 (a)·(b)에 있어야 한다."""
    lines = [line.strip() for line in CONFIG.read_text(encoding="ascii").splitlines()]
    mentions = [part.strip() for line in lines for part in line.split(":")]
    names = {p.stem for p in SRC.glob("*.py")} | {
        p.name for p in SRC.iterdir() if (p / "__init__.py").exists()
    }
    missing = sorted(
        name for name in names - ENTRY_MODULES if mentions.count(f"pureiron_control.{name}") < 2
    )
    assert not missing, f"unclassified pureiron_control modules: {missing}"


def test_every_core_module_outside_platform_is_forbidden() -> None:
    """core에 최상위 모듈이 새로 생겨도 (d)가 빠뜨리지 않게: platform 외 전부 금지 목록에 있다."""
    text = CONFIG.read_text(encoding="ascii")
    section = text.split("[importlinter:contract:core-platform-only]")[1]
    forbidden = set(section.split("forbidden_modules =")[1].split("[importlinter")[0].split())
    names = {p.stem for p in CORE_SRC.glob("*.py")} | {
        p.name for p in CORE_SRC.iterdir() if (p / "__init__.py").exists()
    }
    missing = sorted(
        name
        for name in names - {"__init__", "platform"}
        if f"pureiron_core.{name}" not in forbidden
    )
    assert not missing, f"pureiron_core modules missing from contract (d): {missing}"
    assert "pureiron_core.platform" not in forbidden
