"""bearer token 원문 생성과 저장용 해시(plan M4 C5). 원문은 발급 응답에 한 번만 나간다.

접두로 종류를 구분한다: 현장 token `pis_`, 사람·CI token `pia_`. 인증은 접두로 표를 고르고
SHA-256(hex)로 찾는다 — 원문이 DB에 남지 않고, 고엔트로피 난수라 느린 해시가 필요 없다.
"""

from __future__ import annotations

import hashlib
import secrets
from enum import StrEnum

TOKEN_BYTES = 32


class TokenKind(StrEnum):
    SITE = "pis_"
    API = "pia_"


def new_token(kind: TokenKind) -> str:
    return kind.value + secrets.token_urlsafe(TOKEN_BYTES)


def token_kind(token: str) -> TokenKind | None:
    for kind in TokenKind:
        if token.startswith(kind.value) and len(token) > len(kind.value):
            return kind
    return None


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
