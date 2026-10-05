"""현장 site key 서명 문서(PD-3): enrollment 파일·report export.

현장은 설치 때 만든 Ed25519 site key(raw 32B seed)로 자기 문서에 서명하고, control-api(M4)는
같은 함수로 검증한다(P3). 봉투 형식:

    {"format": 1, "purpose": "<용도>", "payload": {…}, "signature": "<hex raw 64B>"}

서명 대상은 JCS(`{"format": 1, "payload": …, "purpose": …}`)다 — 용도(`purpose`)를 서명에 넣어
enrollment 서명을 report로(또는 그 반대로) 재사용할 수 없게 한다. root 서명(`trust.py`)과 같은
도메인 분리 규칙이다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from typing import cast

import rfc8785

from pureiron_bundle import keys

ENVELOPE_FORMAT = 1
_ENVELOPE_KEYS = {"format", "purpose", "payload", "signature"}

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


class SitePurpose(StrEnum):
    ENROLLMENT = "site-enrollment"
    """site 공개키 등록(자기 키로 서명 = 개인키 보유 증명)."""
    REPORTS = "site-reports"
    """ApplyAttempt 보고 묶음(폐쇄망 USB 반출, `report_seq` 포함)."""


class SignedDocumentError(ValueError):
    pass


def _signed_bytes(purpose: SitePurpose, payload: Mapping[str, Json]) -> bytes:
    return rfc8785.dumps({"format": ENVELOPE_FORMAT, "payload": dict(payload), "purpose": purpose})


def sign_document(
    seed: bytes, purpose: SitePurpose, payload: Mapping[str, Json]
) -> dict[str, Json]:
    signature = keys.sign(seed, _signed_bytes(purpose, payload))
    return {
        "format": ENVELOPE_FORMAT,
        "purpose": purpose.value,
        "payload": dict(payload),
        "signature": signature.hex(),
    }


def dumps(envelope: Mapping[str, Json]) -> bytes:
    """파일로 쓰는 형태(사람이 읽기 쉬운 JSON, LF). 서명은 JCS 기준이라 들여쓰기와 무관하다."""
    return (json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def open_document(
    data: bytes, purpose: SitePurpose, public: bytes | None = None
) -> dict[str, Json]:
    """봉투를 검증하고 payload를 돌려준다. `public`이 None이면 payload의 `public_key`(hex)로
    검증한다 — enrollment(자기 서명)에서만 허용, 다른 용도는 등록된 키가 필요하다.
    형식·용도·서명이 틀리면 `SignedDocumentError`."""
    try:
        envelope: object = json.loads(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SignedDocumentError(f"invalid JSON ({exc})") from exc
    if not isinstance(envelope, dict) or set(cast(dict[str, object], envelope)) != _ENVELOPE_KEYS:
        raise SignedDocumentError(f"envelope must have exactly {sorted(_ENVELOPE_KEYS)}")
    doc = cast(dict[str, Json], envelope)
    if doc["format"] != ENVELOPE_FORMAT or doc["purpose"] != purpose.value:
        raise SignedDocumentError(
            f"expected format {ENVELOPE_FORMAT} purpose {purpose.value}, "
            f"got {doc['format']!r} {doc['purpose']!r}"
        )
    payload, signature = doc["payload"], doc["signature"]
    if not isinstance(payload, dict) or not isinstance(signature, str):
        raise SignedDocumentError("payload must be an object and signature a hex string")
    if public is None:
        if purpose is not SitePurpose.ENROLLMENT:
            # 자기 키 검증은 등록 문서에서만 — 보고 등은 등록된 키로만 검증해야 한다.
            raise SignedDocumentError(f"{purpose.value} documents need the registered site key")
        public = _payload_key(payload)
    try:
        raw_signature = bytes.fromhex(signature)
    except ValueError as exc:
        raise SignedDocumentError(f"signature is not hex ({exc})") from exc
    if not keys.verify(public, raw_signature, _signed_bytes(purpose, payload)):
        raise SignedDocumentError("signature does not match")
    return payload


def _payload_key(payload: Mapping[str, Json]) -> bytes:
    value = payload.get("public_key")
    if not isinstance(value, str):
        raise SignedDocumentError("payload.public_key missing")
    try:
        public = bytes.fromhex(value)
    except ValueError as exc:
        raise SignedDocumentError(f"payload.public_key is not hex ({exc})") from exc
    if len(public) != keys.KEY_LEN:
        raise SignedDocumentError(f"payload.public_key must be {keys.KEY_LEN} bytes")
    return public
