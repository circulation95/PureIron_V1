"""`pureiron-control` CLI: 자체 마이그레이션 lint·ADD-only 위반 거부·역할 선택지·설정 오류."""

from __future__ import annotations

from pathlib import Path

import pytest

from pureiron_control.cli import main

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
EXIT_FAILED = 4
EXIT_LINT = 5


def test_own_migrations_pass_lint(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate", "lint", str(MIGRATIONS)]) == 0
    assert "migrate lint: ok" in capsys.readouterr().out


def test_lint_rejects_a_drop_after_baseline(tmp_path: Path) -> None:
    baseline = "0001_control_schema.sql"
    (tmp_path / baseline).write_bytes((MIGRATIONS / baseline).read_bytes())
    (tmp_path / "0002_drop.sql").write_text("DROP TABLE drafts;\n", encoding="utf-8")
    (tmp_path / "epochs.yaml").write_text(
        f"epochs:\n  1: {baseline}\n  2: 0002_drop.sql\n", encoding="utf-8"
    )
    assert main(["migrate", "lint", str(tmp_path)]) == EXIT_LINT


@pytest.mark.parametrize("role", ["root", "operator", ""])
def test_token_roles_are_fixed(role: str) -> None:
    with pytest.raises(SystemExit):
        main(["token", "create", "--role", role, "--name", "x"])


def test_bad_settings_exit_4(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUREIRON_CONTROL_POLL_S", "zero")
    assert main(["token", "list"]) == EXIT_FAILED
