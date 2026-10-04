"""신뢰 체인: root key → release cert → manifest(ADR-023, plan §6.2 ①).

- `release-cert.json` = JCS(`{"format":1,"purpose":"release-cert","release_pubkey":"<hex raw32>",
  "serial":N,"not_before":"<RFC3339 Z>","not_after":"<RFC3339 Z>"}`),
  `release-cert.sig` = root 서명(파일 바이트 그대로 — bootstrap `openssl pkeyutl -rawin` 경로 유지).
- root 서명은 용도별로 도메인을 분리한다: cert는 `purpose: release-cert`, trust_update는 검증기가
  manifest 값으로 만든 JCS(`{"purpose":"trust-update","min_cert_serial":N}`)에 서명한다. 그래서
  한 용도의 root 서명을 다른 용도로 재사용할 수 없다.
- cert 유효성은 벽시계가 아니라 **서명된 manifest `created_at`** 기준이다(폐쇄망 시계 불신).
- `created_at`은 이전에 본 값보다 작을 수 없고(`max_seen_created_at`), cert serial은
  `min_cert_serial` 이상이어야 한다. `min_cert_serial`은 root 서명된 `trust_update`로만 오른다.
- 상태 저장은 Updater 몫이다. 여기서는 검증 후의 새 상태 값만 돌려준다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import rfc8785

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError, RejectCode

CERT_FORMAT = 1
DEFAULT_CERT_DAYS = 180
CERT_PURPOSE = "release-cert"
TRUST_UPDATE_PURPOSE = "trust-update"
_CERT_KEYS = {"format", "purpose", "release_pubkey", "serial", "not_before", "not_after"}
_HEX_KEY = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})"
)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_float(text: str) -> object:
    raise ValueError(f"non-integer number {text!r} is not allowed")


def loads_strict(data: bytes) -> object:
    """서명된 메타데이터 JSON 해석: 중복 키·실수·NaN/Infinity 거부(정수·문자열만 쓰는 형식)."""
    return json.loads(
        data, object_pairs_hook=_unique_pairs, parse_float=_no_float, parse_constant=_no_float
    )


def parse_timestamp(text: str) -> datetime:
    """RFC 3339 시각(시간대 필수) → aware datetime(UTC)."""
    if not _TIMESTAMP.fullmatch(text):
        raise ValueError(f"not an RFC 3339 timestamp with offset: {text!r}")
    return datetime.fromisoformat(text).astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """aware datetime → `YYYY-MM-DDTHH:MM:SSZ`(초 단위, UTC)."""
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class ReleaseCert:
    release_pubkey: bytes
    serial: int
    not_before: datetime
    not_after: datetime


@dataclass(frozen=True, slots=True)
class TrustState:
    """현장 신뢰 상태(Updater가 영속화). 초기값 = 아무것도 본 적 없음."""

    min_cert_serial: int = 0
    max_seen_created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TrustDecision:
    state: TrustState
    """이 번들을 받아들였을 때의 새 상태."""
    trust_update_applied: bool
    """manifest `trust_update`가 유효한 root 서명과 함께 와서 반영됐는지."""


def issue_cert(
    root_seed: bytes,
    release_pubkey: bytes,
    serial: int,
    not_before: datetime,
    days: int = DEFAULT_CERT_DAYS,
) -> tuple[bytes, bytes]:
    """(release-cert.json 바이트, release-cert.sig)."""
    if serial < 1:
        raise ValueError("cert serial must be >= 1")
    if days < 1:
        raise ValueError("cert validity must be >= 1 day")
    if len(release_pubkey) != keys.KEY_LEN:
        raise ValueError("release public key must be 32 raw bytes")
    document = {
        "format": CERT_FORMAT,
        "purpose": CERT_PURPOSE,
        "release_pubkey": release_pubkey.hex(),
        "serial": serial,
        "not_before": format_timestamp(not_before),
        "not_after": format_timestamp(not_before + timedelta(days=days)),
    }
    data = rfc8785.dumps(document)
    return data, keys.sign(root_seed, data)


def _format_error(detail: str) -> BundleRejectedError:
    return BundleRejectedError(RejectCode.E_FORMAT, f"release-cert.json: {detail}")


def parse_cert(data: bytes) -> ReleaseCert:
    try:
        document = loads_strict(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _format_error(f"invalid JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise _format_error("not an object")
    fields = cast(dict[str, object], document)
    if set(fields) != _CERT_KEYS:
        raise _format_error(f"keys must be exactly {sorted(_CERT_KEYS)}")
    if fields["format"] != CERT_FORMAT or isinstance(fields["format"], bool):
        raise _format_error("unsupported format")
    if fields["purpose"] != CERT_PURPOSE:
        raise _format_error(f"purpose must be {CERT_PURPOSE!r}")
    pubkey, serial = fields["release_pubkey"], fields["serial"]
    if not isinstance(pubkey, str) or not _HEX_KEY.fullmatch(pubkey):
        raise _format_error("release_pubkey must be 64 lowercase hex chars")
    if not isinstance(serial, int) or isinstance(serial, bool) or serial < 1:
        raise _format_error("serial must be an integer >= 1")
    try:
        not_before = parse_timestamp(str(fields["not_before"]))
        not_after = parse_timestamp(str(fields["not_after"]))
    except ValueError as exc:
        raise _format_error(str(exc)) from exc
    if not_after <= not_before:
        raise _format_error("not_after must be after not_before")
    return ReleaseCert(bytes.fromhex(pubkey), serial, not_before, not_after)


def verify_cert(root_pubkey: bytes, cert_json: bytes, cert_sig: bytes) -> ReleaseCert:
    """root 서명 확인(`E_SIG`) 후에만 cert를 해석한다."""
    if not keys.verify(root_pubkey, cert_sig, cert_json):
        raise BundleRejectedError(
            RejectCode.E_SIG, "release-cert.sig is not a valid root signature"
        )
    return parse_cert(cert_json)


def verify_manifest_signature(cert: ReleaseCert, manifest: bytes, manifest_sig: bytes) -> None:
    if not keys.verify(cert.release_pubkey, manifest_sig, manifest):
        raise BundleRejectedError(
            RejectCode.E_SIG, "manifest.sig is not a valid release-key signature"
        )


def trust_update_bytes(min_cert_serial: int) -> bytes:
    """`trust-update.sig`가 서명하는 바이트 = JCS(`{"purpose":"trust-update","min_cert_serial":N}`).

    manifest 계약의 `trust_update`는 `{min_cert_serial}`만 갖는다. purpose는 검증기가 덧붙인다.
    """
    return rfc8785.dumps({"purpose": TRUST_UPDATE_PURPOSE, "min_cert_serial": min_cert_serial})


def sign_trust_update(root_seed: bytes, min_cert_serial: int) -> bytes:
    return keys.sign(root_seed, trust_update_bytes(min_cert_serial))


def check_validity(
    *,
    root_pubkey: bytes,
    cert: ReleaseCert,
    created_at: datetime,
    state: TrustState,
    trust_update: int | None,
    trust_update_sig: bytes | None,
) -> TrustDecision:
    """cert 유효기간·created_at 역행·serial 하한(`E_CERT`).

    `trust_update`는 `trust-update.sig`가 그 값에 대한 유효한 root 서명일 때만 반영하고(상승만),
    새 하한을 이 번들의 cert 검사에도 적용한다. 서명이 없거나 틀리면 거부하지 않고 무시한다.
    """
    applied = (
        trust_update is not None
        and trust_update_sig is not None
        and keys.verify(root_pubkey, trust_update_sig, trust_update_bytes(trust_update))
    )
    floor = state.min_cert_serial
    if applied and trust_update is not None:
        floor = max(floor, trust_update)
    stamp = format_timestamp(created_at)
    if not cert.not_before <= created_at <= cert.not_after:
        raise BundleRejectedError(
            RejectCode.E_CERT,
            f"created_at {stamp} outside cert validity "
            f"[{format_timestamp(cert.not_before)}, {format_timestamp(cert.not_after)}]",
        )
    seen = state.max_seen_created_at
    if seen is not None and created_at < seen:
        raise BundleRejectedError(
            RejectCode.E_CERT,
            f"created_at {stamp} is older than last seen {format_timestamp(seen)}",
        )
    if cert.serial < floor:
        raise BundleRejectedError(
            RejectCode.E_CERT, f"cert serial {cert.serial} below min_cert_serial {floor}"
        )
    new_seen = created_at if seen is None else max(seen, created_at)
    return TrustDecision(TrustState(floor, new_seen), applied)
