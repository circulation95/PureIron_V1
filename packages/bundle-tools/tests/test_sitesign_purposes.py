"""site 서명 용도 도메인 분리: 온라인 heartbeat·report 서명은 다른 용도로 열리지 않는다(M4).

기존 용도(enrollment·reports·draft)의 값·봉투 형식은 그대로다 — 현장 r4 Updater가 이미 그 값으로
서명한 파일을 control-api가 같은 함수로 연다.
"""

from __future__ import annotations

import json

import pytest

from pureiron_bundle import keys
from pureiron_bundle.sitesign import (
    ENVELOPE_FORMAT,
    Json,
    SignatureMismatchError,
    SignedDocumentError,
    SitePurpose,
    dumps,
    open_document,
    sign_document,
)

SEED = bytes(range(1, 33))
PUBLIC = keys.public_key(SEED)
PAYLOAD: dict[str, Json] = {"site_id": "site-a", "report_seq": 7, "at": "2026-11-01T00:00:00Z"}


def test_purpose_values_are_fixed() -> None:
    assert {p.name: p.value for p in SitePurpose} == {
        "ENROLLMENT": "site-enrollment",
        "REPORTS": "site-reports",
        "DRAFT": "site-draft",
        "HEARTBEAT": "site-heartbeat",
        "REPORT": "site-report",
    }


@pytest.mark.parametrize("purpose", [SitePurpose.HEARTBEAT, SitePurpose.REPORT])
def test_online_documents_round_trip_with_registered_key(purpose: SitePurpose) -> None:
    data = dumps(sign_document(SEED, purpose, PAYLOAD))
    envelope = json.loads(data)
    assert envelope["format"] == ENVELOPE_FORMAT
    assert envelope["purpose"] == purpose.value
    assert open_document(data, purpose, PUBLIC) == PAYLOAD


@pytest.mark.parametrize(
    ("signed", "opened"),
    [
        (signed, opened)
        for signed in SitePurpose
        for opened in (SitePurpose.HEARTBEAT, SitePurpose.REPORT, SitePurpose.REPORTS)
        if signed is not opened
    ],
    ids=lambda p: p.value,
)
def test_signature_of_one_purpose_does_not_open_as_another(
    signed: SitePurpose, opened: SitePurpose
) -> None:
    data = dumps(sign_document(SEED, signed, PAYLOAD))
    with pytest.raises(SignedDocumentError, match=f"purpose {opened.value}"):
        open_document(data, opened, PUBLIC)


@pytest.mark.parametrize(
    ("signed", "relabelled"),
    [
        (SitePurpose.HEARTBEAT, SitePurpose.REPORT),
        (SitePurpose.REPORT, SitePurpose.HEARTBEAT),
        (SitePurpose.REPORT, SitePurpose.REPORTS),
    ],
    ids=lambda p: p.value,
)
def test_relabelled_purpose_breaks_the_signature(
    signed: SitePurpose, relabelled: SitePurpose
) -> None:
    """봉투의 purpose 문자열만 바꿔도 서명 대상(JCS에 purpose 포함)이 달라져 검증에 실패한다."""
    envelope = json.loads(dumps(sign_document(SEED, signed, PAYLOAD)))
    envelope["purpose"] = relabelled.value
    with pytest.raises(SignedDocumentError, match="signature does not match"):
        open_document(json.dumps(envelope).encode(), relabelled, PUBLIC)


@pytest.mark.parametrize("purpose", [SitePurpose.HEARTBEAT, SitePurpose.REPORT])
def test_online_documents_need_the_registered_key(purpose: SitePurpose) -> None:
    payload: dict[str, Json] = {**PAYLOAD, "public_key": PUBLIC.hex()}
    data = dumps(sign_document(SEED, purpose, payload))
    with pytest.raises(SignedDocumentError, match="registered site key"):
        open_document(data, purpose)
    with pytest.raises(SignatureMismatchError, match="signature does not match"):
        open_document(data, purpose, keys.public_key(bytes(32)))


def test_only_a_signature_failure_is_a_key_mismatch() -> None:
    """리뷰 M1: 중앙은 서명 불일치(409 — rekey 대기)와 형식 위반(400)을 구분한다."""
    data = dumps(sign_document(SEED, SitePurpose.REPORT, PAYLOAD))
    with pytest.raises(SignatureMismatchError):
        open_document(data, SitePurpose.REPORT, keys.public_key(bytes(32)))
    for broken, purpose in [
        (b"{not json", SitePurpose.REPORT),
        (data, SitePurpose.HEARTBEAT),  # 용도 다름
        (data.replace(b'"signature"', b'"sig"'), SitePurpose.REPORT),
    ]:
        with pytest.raises(SignedDocumentError) as caught:
            open_document(broken, purpose, PUBLIC)
        assert not isinstance(caught.value, SignatureMismatchError)


def test_unpaired_surrogate_in_payload_is_a_signed_document_error() -> None:
    """리뷰 r3 R3-1: JSON 이스케이프 `\\ud800`은 json.loads를 지나지만 서명 대상(JCS)으로
    직렬화할 수 없다 — 처리되지 않은 CanonicalizationError(API 500) 대신
    SignedDocumentError(400)다."""
    data = dumps(sign_document(SEED, SitePurpose.REPORT, {**PAYLOAD, "logs": "ab"}))
    tampered = data.replace(b'"ab"', b'"a\\ud800b"')
    assert tampered != data
    with pytest.raises(SignedDocumentError, match="non-UTF-8 code points"):
        open_document(tampered, SitePurpose.REPORT, PUBLIC)
