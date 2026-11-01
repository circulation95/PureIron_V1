"""site 서명 용도(purpose) 도메인 분리: 온라인 heartbeat·report 서명은 다른 용도로 열리지 않는다(M4).

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
    with pytest.raises(SignedDocumentError, match="signature does not match"):
        open_document(data, purpose, keys.public_key(bytes(32)))
