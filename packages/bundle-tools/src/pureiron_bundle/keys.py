"""Ed25519 키 형식·서명 원시 연산(plan M2 키 형식).

- 공개키 정본 = raw 32바이트(`*.pub`), 서명 = raw 64바이트, 지문 = sha256(raw 32B) hex.
- 개인키 = raw 32바이트 seed(`*.key`, 0600). 저장소에 커밋하지 않는다(`.gitignore` `*.key`).
- openssl용 PEM(SubjectPublicKeyInfo)은 raw에서 결정적으로 만든다: DER 접두
  `302a300506032b6570032100` + raw 32B → base64, 64자 줄바꿈.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

KEY_LEN = 32
SIGNATURE_LEN = 64
SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
_PEM_BEGIN = "-----BEGIN PUBLIC KEY-----"
_PEM_END = "-----END PUBLIC KEY-----"


class KeyFormatError(ValueError):
    pass


def _check_len(data: bytes, what: str) -> bytes:
    if len(data) != KEY_LEN:
        raise KeyFormatError(f"{what} must be {KEY_LEN} raw bytes, got {len(data)}")
    return data


def generate_seed() -> bytes:
    return os.urandom(KEY_LEN)


def public_key(seed: bytes) -> bytes:
    """seed → raw 32바이트 공개키."""
    key = Ed25519PrivateKey.from_private_bytes(_check_len(seed, "private key seed"))
    return key.public_key().public_bytes_raw()


def sign(seed: bytes, data: bytes) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(_check_len(seed, "private key seed"))
    return key.sign(data)


def verify(public: bytes, signature: bytes, data: bytes) -> bool:
    """서명 일치 여부. 길이가 틀린 서명·공개키도 False(예외 없음)."""
    if len(public) != KEY_LEN or len(signature) != SIGNATURE_LEN:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, data)
    except InvalidSignature:
        return False
    return True


def fingerprint(public: bytes) -> str:
    return hashlib.sha256(_check_len(public, "public key")).hexdigest()


def read_public_key(path: Path) -> bytes:
    return _check_len(path.read_bytes(), f"{path}")


def read_private_key(path: Path) -> bytes:
    return _check_len(path.read_bytes(), f"{path}")


def write_public_key(path: Path, public: bytes) -> None:
    path.write_bytes(_check_len(public, "public key"))


def write_private_key(path: Path, seed: bytes) -> None:
    """새 파일로만 쓴다(O_EXCL, 0600) — 기존 키를 덮어쓰지 않는다."""
    _check_len(seed, "private key seed")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(seed)


def export_pem(public: bytes) -> str:
    """raw 32B → SubjectPublicKeyInfo PEM(결정적, LF)."""
    body = base64.b64encode(SPKI_PREFIX + _check_len(public, "public key")).decode("ascii")
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return "\n".join([_PEM_BEGIN, *lines, _PEM_END]) + "\n"


def import_pem(text: str) -> bytes:
    """`export_pem`의 역. Ed25519 SubjectPublicKeyInfo 외에는 거부."""
    lines = [line.strip() for line in text.strip().splitlines()]
    if len(lines) < 3 or lines[0] != _PEM_BEGIN or lines[-1] != _PEM_END:
        raise KeyFormatError("not a PEM public key")
    try:
        der = base64.b64decode("".join(lines[1:-1]), validate=True)
    except binascii.Error as exc:
        raise KeyFormatError(f"invalid PEM base64: {exc}") from exc
    if not der.startswith(SPKI_PREFIX):
        raise KeyFormatError("not an Ed25519 SubjectPublicKeyInfo")
    return _check_len(der[len(SPKI_PREFIX) :], "public key")
