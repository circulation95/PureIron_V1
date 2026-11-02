"""현장 site key 서명 문서(PD-3): enrollment 파일·report export·현장 드래프트(M5-2)·온라인
heartbeat·보고(M4).

현장은 설치 때 만든 Ed25519 site key(raw 32B seed)로 자기 문서에 서명하고, control-api(M4)는
같은 함수로 검증한다(P3). 봉투 형식:

    {"format": 1, "purpose": "<용도>", "payload": {…}, "signature": "<hex raw 64B>"}

서명 대상은 JCS(`{"format": 1, "payload": …, "purpose": …}`)다 — 용도(`purpose`)를 서명에 넣어
enrollment 서명을 report로(또는 그 반대로) 재사용할 수 없게 한다. root 서명(`trust.py`)과 같은
도메인 분리 규칙이다.

현장 드래프트 파일(`draft.tar`, plan M5-2 P6·P7)은 `draft.json`(purpose `site-draft` 봉투) +
`project.tar`(유효 트리) 두 일반 파일만 담은 결정적 tar다(경로 바이트 순, PAX, mtime 0, root,
0644). 봉투 payload `project_tar_sha256`이 tree를 묶는다 — `open_draft`가 서명과 함께 확인한다.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from collections.abc import Mapping
from enum import StrEnum
from typing import cast

import rfc8785

from pureiron_bundle import keys
from pureiron_bundle.pib import tar_info
from pureiron_bundle.tarsafe import PARSER_EXHAUSTION, open_tar_stream

ENVELOPE_FORMAT = 1
_ENVELOPE_KEYS = {"format", "purpose", "payload", "signature"}

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


class SitePurpose(StrEnum):
    ENROLLMENT = "site-enrollment"
    """site 공개키 등록(자기 키로 서명 = 개인키 보유 증명)."""
    REPORTS = "site-reports"
    """ApplyAttempt 보고 묶음(폐쇄망 USB 반출, `report_seq` 포함)."""
    DRAFT = "site-draft"
    """현장 드래프트(운영 값 PATCH가 실린 유효 트리, `draft.tar`의 `draft.json`)."""
    HEARTBEAT = "site-heartbeat"
    """온라인 heartbeat 한 건(`report_seq`·현재 gen·Updater 단계, `POST …/heartbeat`)."""
    REPORT = "site-report"
    """온라인 ApplyAttempt 보고 한 건(`report_seq` 포함, `POST …/reports`). 묶음
    `site-reports`(USB 반출)와 용도를 나눠 한 건짜리 서명을 묶음으로(또는 그 반대로)
    쓸 수 없게 한다."""


DRAFT_ENVELOPE = "draft.json"
DRAFT_TREE = "project.tar"
# core project.tar 상한(64 MiB, core `apply.TREE_LIMIT_BYTES`) + 봉투. 메모리에서 읽는다.
DRAFT_LIMIT_BYTES = 65 * 1024 * 1024
_ENVELOPE_LIMIT_BYTES = 64 * 1024
_DRAFT_INTS = ("site_rev", "base_site_rev")
_DRAFT_TEXTS = ("site_id", "content_hash", "exported_at", "project_tar_sha256")


class SignedDocumentError(ValueError):
    pass


class SignatureMismatchError(SignedDocumentError):
    """봉투 형식·용도는 맞지만 주어진 공개키로 서명이 확인되지 않는다. 중앙은 이것을 형식
    위반(400)과 구분해 409(등록 키 불일치 — 현장 재등록 뒤 rekey 대기일 수 있음)로 답한다(M4-1
    리뷰 M1)."""


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
    형식·용도·서명이 틀리면 `SignedDocumentError`(서명만 틀리면 그 하위
    `SignatureMismatchError`)."""
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
    if len(raw_signature) != keys.SIGNATURE_LEN:
        # 길이가 틀린 서명은 형식 위반(400)이다 — 키 불일치(409)로 보이지 않게(R2-N1).
        raise SignedDocumentError(f"signature must be {keys.SIGNATURE_LEN} bytes")
    try:
        signed = _signed_bytes(purpose, payload)
    except rfc8785.CanonicalizationError as exc:
        # json.loads는 짝 없는 서로게이트(`\ud800`)를 받지만 JCS는 거부한다 — 서명할 수 없는 본문.
        raise SignedDocumentError("payload contains non-UTF-8 code points") from exc
    if not keys.verify(public, raw_signature, signed):
        raise SignatureMismatchError("signature does not match")
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


def tree_digest(project_tar: bytes) -> str:
    return "sha256:" + hashlib.sha256(project_tar).hexdigest()


def pack_draft(envelope: bytes, project_tar: bytes) -> bytes:
    """`draft.tar` 바이트(같은 입력 = 같은 바이트)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in sorted(
            {DRAFT_ENVELOPE: envelope, DRAFT_TREE: project_tar}.items(),
            key=lambda item: item[0].encode(),
        ):
            tar.addfile(tar_info(name, len(data)), io.BytesIO(data))
    return buffer.getvalue()


def _draft_members(data: bytes) -> dict[str, bytes]:
    """정확히 `draft.json`·`project.tar` 두 일반 파일. 그 밖의 항목·중복·크기 초과는 거부."""
    if len(data) > DRAFT_LIMIT_BYTES:
        raise SignedDocumentError(f"draft is {len(data)} bytes (limit {DRAFT_LIMIT_BYTES})")
    limits = {DRAFT_ENVELOPE: _ENVELOPE_LIMIT_BYTES, DRAFT_TREE: DRAFT_LIMIT_BYTES}
    members: dict[str, bytes] = {}
    try:
        with open_tar_stream(io.BytesIO(data)) as tar:
            for member in tar:
                name = member.name
                if name not in limits or name in members or not member.isreg():
                    raise SignedDocumentError(f"unexpected draft entry {name!r}")
                if member.size > limits[name]:
                    raise SignedDocumentError(f"draft entry {name!r} is too large")
                handle = tar.extractfile(member)
                if handle is None:
                    raise SignedDocumentError(f"draft entry {name!r} is unreadable")
                members[name] = handle.read()
    except SignedDocumentError:
        raise
    except (tarfile.TarError, ValueError, UnicodeError, *PARSER_EXHAUSTION) as exc:
        raise SignedDocumentError(f"draft is not a valid tar ({exc})") from exc
    missing = sorted(set(limits) - set(members))
    if missing:
        raise SignedDocumentError(f"draft is missing {missing}")
    return members


def open_draft(data: bytes, public: bytes, *, site_id: str) -> tuple[dict[str, Json], bytes]:
    """`draft.tar`를 등록된 site 공개키로 검증하고 (payload, project.tar 바이트)를 돌려준다.

    서명(purpose `site-draft`)·payload 형식·`site_id`가 그 공개키의 현장과 같은지·
    `project_tar_sha256` 일치를 모두 확인한다 — 하나라도 틀리면 `SignedDocumentError`(현장 X
    키로 서명했지만 payload에 다른 site_id를 적은 파일을 그 현장 드래프트로 읽지 않게). tree
    내용(프로젝트 검증·content_hash)은 호출자(absorb)가 본다.
    """
    members = _draft_members(data)
    payload = open_document(members[DRAFT_ENVELOPE], SitePurpose.DRAFT, public)
    for key in _DRAFT_INTS:
        value = payload.get(key)
        if type(value) is not int or value < 0:
            raise SignedDocumentError(f"payload.{key} must be a non-negative integer")
    for key in _DRAFT_TEXTS:
        if not isinstance(payload.get(key), str):
            raise SignedDocumentError(f"payload.{key} must be a string")
    if payload["site_id"] != site_id:
        raise SignedDocumentError(
            f"payload.site_id {payload['site_id']!r} does not match expected site {site_id!r}"
        )
    tree = members[DRAFT_TREE]
    if payload["project_tar_sha256"] != tree_digest(tree):
        raise SignedDocumentError("project.tar does not match payload.project_tar_sha256")
    return payload, tree
