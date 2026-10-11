"""`pureiron-core users`: 비밀번호 입력(파일·stdin), DB 접속 전 입력 검증, 오류 종료 코드.

DB까지 가는 경로(add·passwd·disable·enable·list·감사 기록)는 통합 테스트 `test_auth_db.py`가 덮는다.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from pureiron_core.cli import main, read_password
from pureiron_core.platform.migrate import EXIT_FAILED

UNREACHABLE_DSN = "postgresql://pureiron@127.0.0.1:1/none"


@pytest.fixture(autouse=True)
def _unreachable_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUREIRON_DB_DSN", UNREACHABLE_DSN)
    monkeypatch.delenv("PUREIRON_DB_PASSWORD_FILE", raising=False)


def test_unknown_role_is_an_argument_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["users", "add", "alice", "--role", "root"])
    assert caught.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_password_file_first_line_without_newline(tmp_path: Path) -> None:
    path = tmp_path / "pw"
    path.write_bytes(b"s3cret-pass \r\nignored\n")
    assert read_password(path) == "s3cret-pass "


def test_non_utf8_password_file_is_a_clean_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "pw"
    path.write_bytes(b"\xff\xfe-not-utf8")
    argv = ["users", "add", "alice", "--role", "viewer", "--password-file", str(path)]
    assert main(argv) == EXIT_FAILED
    assert "not UTF-8" in capsys.readouterr().err


def test_password_from_piped_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("piped-secret\nrest\n"))
    assert read_password(None) == "piped-secret"
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert read_password(None) == ""


@pytest.mark.parametrize(
    ("argv", "stdin", "message"),
    [
        (["users", "add", "Bad Name", "--role", "viewer"], "long-enough-1\n", "username must"),
        (["users", "add", "alice", "--role", "viewer"], "short\n", "password must be 8-128"),
        (["users", "passwd", "alice"], "\n", "password must be 8-128"),
    ],
)
def test_invalid_input_fails_before_touching_the_db(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    stdin: str,
    message: str,
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert main(argv) == EXIT_FAILED
    err = capsys.readouterr().err
    assert message in err
    assert "Traceback" not in err


def test_missing_password_file_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["users", "add", "alice", "--role", "viewer", "--password-file", str(tmp_path / "x")]
    assert main(argv) == EXIT_FAILED
    assert "error:" in capsys.readouterr().err


def test_unreachable_db_is_a_clean_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["users", "list"]) == EXIT_FAILED
    err = capsys.readouterr().err
    assert err.startswith("error: site-db error")
    assert "Traceback" not in err
