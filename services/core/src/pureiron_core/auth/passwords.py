"""비밀번호 정책과 scrypt 해시(plan M3b D4, 표준 라이브러리 `hashlib.scrypt`).

저장 형식: `scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>`(base64는 패딩 없는 표준 알파벳).
매개변수를 해시에 같이 두므로 기본값을 올려도 기존 해시는 그대로 검증된다. 검증은 CPU를
수십 ms 쓰므로 호출자가 `asyncio.to_thread`로 돌린다. 이 모듈은 동기 함수만 둔다.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets

SCHEME = "scrypt"
DEFAULT_N = 2**14
DEFAULT_R = 8
DEFAULT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32
# 저장된 해시의 매개변수 상한(손상·조작된 행이 메모리·CPU를 과하게 쓰지 않게).
_MAX_N = 2**20
_MAX_R = 32
_MAX_P = 16
# OpenSSL scrypt 메모리 = 128·r·(n + p + 2) 바이트. 그 위 여유분.
_MAXMEM_SLACK_BYTES = 1 << 20

PASSWORD_MIN_CHARS = 8
PASSWORD_MAX_CHARS = 128
USERNAME_PATTERN = re.compile(r"^[a-z0-9_.-]{3,32}$")


class PasswordHashError(ValueError):
    """저장된 해시 문자열이 형식에 맞지 않는다(손상된 행)."""


def password_problem(password: str) -> str | None:
    """정책 위반 이유(영어) 또는 None. 길이만 본다(legacy 규칙: 8~128자)."""
    if not PASSWORD_MIN_CHARS <= len(password) <= PASSWORD_MAX_CHARS:
        return (
            f"password must be {PASSWORD_MIN_CHARS}-{PASSWORD_MAX_CHARS} characters, "
            f"got {len(password)}"
        )
    return None


def username_problem(username: str) -> str | None:
    if USERNAME_PATTERN.fullmatch(username) is None:
        return f"username must match {USERNAME_PATTERN.pattern}, got {username!r}"
    return None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int, size: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=128 * r * (n + p + 2) + _MAXMEM_SLACK_BYTES,
        dklen=size,
    )


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = secrets.token_bytes(SALT_BYTES) if salt is None else salt
    digest = _derive(password, salt, n=DEFAULT_N, r=DEFAULT_R, p=DEFAULT_P, size=HASH_BYTES)
    return f"{SCHEME}${DEFAULT_N}${DEFAULT_R}${DEFAULT_P}${_b64(salt)}${_b64(digest)}"


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes]:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != SCHEME:
        raise PasswordHashError("password hash is not in scrypt$n$r$p$salt$hash form")
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except (ValueError, binascii.Error):
        raise PasswordHashError("password hash has malformed fields") from None
    power_of_two = n > 1 and n & (n - 1) == 0
    if not (power_of_two and n <= _MAX_N and 1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
        raise PasswordHashError(f"password hash parameters out of range (n={n} r={r} p={p})")
    if not salt or not digest:
        raise PasswordHashError("password hash has empty salt or digest")
    return n, r, p, salt, digest


def verify_password(password: str, stored: str) -> bool:
    """맞으면 True. 형식이 깨진 해시는 `PasswordHashError`(틀린 비밀번호와 구분해 로그를 남기게)."""
    n, r, p, salt, digest = _parse(stored)
    candidate = _derive(password, salt, n=n, r=r, p=p, size=len(digest))
    return hmac.compare_digest(candidate, digest)
