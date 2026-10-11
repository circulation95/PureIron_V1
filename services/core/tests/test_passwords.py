"""scrypt 해시 형식·검증(RFC 7914 기지값)·형식 오류 거부·비밀번호/username 정책."""

from __future__ import annotations

import base64

import pytest

from pureiron_core.auth.passwords import (
    PasswordHashError,
    hash_password,
    password_problem,
    username_problem,
    verify_password,
)
from pureiron_core.auth.service import DUMMY_HASH

# RFC 7914 §12 테스트 벡터: scrypt(P="password", S="NaCl", N=1024, r=8, p=16, dkLen=64).
RFC7914_DK = bytes.fromhex(
    "fdbabe1c9d3472007856e7190d01e9fe7c6ad7cbc8237830e77376634b373162"
    "2eaf30d92e22a3886ff109279d9830dac727afb94a83ee6d8360cbdfa2cc0640"
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode().rstrip("=")


def test_hash_format_has_default_parameters_salt_and_digest() -> None:
    stored = hash_password("secret123")
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", "16384", "8", "1")
    assert len(base64.b64decode(salt + "==")) == 16
    assert len(base64.b64decode(digest + "=")) == 32
    assert "secret123" not in stored


def test_verify_accepts_right_and_rejects_wrong_password() -> None:
    stored = hash_password("secret123")
    assert verify_password("secret123", stored)
    assert not verify_password("secret124", stored)
    assert not verify_password("", stored)


def test_same_password_gets_a_different_salt_each_time() -> None:
    first, second = hash_password("secret123"), hash_password("secret123")
    assert first != second
    assert verify_password("secret123", first)
    assert verify_password("secret123", second)


def test_verify_uses_parameters_stored_in_the_hash_rfc7914_vector() -> None:
    stored = f"scrypt$1024$8$16${_b64(b'NaCl')}${_b64(RFC7914_DK)}"
    assert verify_password("password", stored)
    assert not verify_password("Password", stored)


def test_non_ascii_password_round_trips() -> None:
    stored = hash_password("비밀번호-테스트1")
    assert verify_password("비밀번호-테스트1", stored)
    assert not verify_password("비밀번호-테스트2", stored)


def test_dummy_hash_uses_default_parameters_and_matches_nothing_obvious() -> None:
    assert DUMMY_HASH.split("$")[:4] == ["scrypt", "16384", "8", "1"]
    assert not verify_password("", DUMMY_HASH)
    assert not verify_password("admin", DUMMY_HASH)


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "bcrypt$2b$12$abc",
        "scrypt$16384$8$1$c2FsdA",
        "scrypt$x$8$1$c2FsdA$ZGlnZXN0",
        "scrypt$1000$8$1$c2FsdA$ZGlnZXN0",  # n이 2의 거듭제곱이 아님
        f"scrypt${2**21}$8$1$c2FsdA$ZGlnZXN0",  # n 상한 초과
        "scrypt$16384$0$1$c2FsdA$ZGlnZXN0",
        "scrypt$16384$8$99$c2FsdA$ZGlnZXN0",
        "scrypt$16384$8$1$!!!$ZGlnZXN0",
        "scrypt$16384$8$1$$ZGlnZXN0",
    ],
)
def test_malformed_hash_is_rejected(stored: str) -> None:
    with pytest.raises(PasswordHashError):
        verify_password("secret123", stored)


@pytest.mark.parametrize(
    ("password", "ok"),
    [("a" * 7, False), ("a" * 8, True), ("a" * 128, True), ("a" * 129, False), ("", False)],
)
def test_password_length_policy(password: str, ok: bool) -> None:
    assert (password_problem(password) is None) is ok


@pytest.mark.parametrize(
    ("username", "ok"),
    [
        ("admin", True),
        ("op.line-1_a", True),
        ("abc", True),
        ("a" * 32, True),
        ("ab", False),
        ("a" * 33, False),
        ("Admin", False),
        ("with space", False),
        ("한글이름", False),
        ("x/y", False),
    ],
)
def test_username_policy(username: str, ok: bool) -> None:
    assert (username_problem(username) is None) is ok
