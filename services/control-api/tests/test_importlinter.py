"""import-linter 계약이 실제 트리에서 지켜지고, 위반을 실제로 잡는지(core와 같은 방식, PD-8)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
SRC = SERVICE / "src" / "pureiron_control"
CONFIG = SERVICE / ".importlinter"
ENTRY_MODULES = {"runtime", "app", "cli", "cli_admin", "settings", "__init__"}
A = "(a) runtime/app are imported only by entry points"
B = "(b) api > domain > platform"
C = "(c) domain modules do not import each other"


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
    for name in (A, B, C):
        match = re.search(rf"^{re.escape(name)} (KEPT|BROKEN)", output, re.MULTILINE)
        assert match is not None, output
        verdicts[name] = match[1]
    return verdicts


def test_contracts_hold_on_real_tree() -> None:
    assert _verdicts(_lint_imports(SRC.parent)) == {A: "KEPT", B: "KEPT", C: "KEPT"}


@pytest.mark.parametrize(
    ("module", "line", "broken"),
    [
        # 간접 사슬이 다른 계약까지 깨지 않도록 의존이 없는 모듈을 import한다.
        ("api/health.py", "import pureiron_control.runtime", A),
        ("reports.py", "import pureiron_control.api.errors", B),
        ("platform/envelopes.py", "import pureiron_control.api.errors", B),
        ("reports.py", "import pureiron_control.sites", C),
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
    assert verdicts == {name: "BROKEN" if name == broken else "KEPT" for name in (A, B, C)}


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
