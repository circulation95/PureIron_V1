"""리뷰 M1: 등록된 site 키로 서명이 확인되지 않는 봉투는 409(rekey 대기일 수 있음 — 현장은 버리지
않고 다시 보낸다), 봉투 JSON·purpose·payload 형식 위반은 400(현장이 `rejected/`로 뺀다)."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import HTTPException

from pureiron_bundle import keys
from pureiron_bundle.sitesign import Json, SitePurpose, dumps, sign_document
from pureiron_control.api.errors import KEY_MISMATCH
from pureiron_control.api.reports import open_registered

SEED = bytes(range(32))
PUBLIC = keys.public_key(SEED)
PAYLOAD: dict[str, Json] = {"site_id": "site-a", "report_seq": 1, "result": "succeeded"}


def _status(
    data: bytes, purpose: SitePurpose = SitePurpose.REPORT, *, token: str | None = None
) -> tuple[int, str]:
    with pytest.raises(HTTPException) as caught:
        open_registered(data, "site-a", purpose, PUBLIC, token=token)
    return caught.value.status_code, str(caught.value.detail)


def test_registered_key_opens_the_envelope() -> None:
    data = dumps(sign_document(SEED, SitePurpose.REPORT, PAYLOAD))
    assert open_registered(data, "site-a", SitePurpose.REPORT, PUBLIC) == PAYLOAD


@pytest.mark.parametrize("purpose", [SitePurpose.HEARTBEAT, SitePurpose.REPORT])
def test_signature_by_another_key_is_409(purpose: SitePurpose) -> None:
    new_key = bytes(range(1, 33))  # 현장이 re-enroll로 바꾼 키(중앙은 아직 옛 키)
    assert _status(dumps(sign_document(new_key, purpose, PAYLOAD)), purpose) == (409, KEY_MISMATCH)


def test_key_mismatch_is_logged_with_site_purpose_and_key(caplog: pytest.LogCaptureFixture) -> None:
    """R2-L2: 409 원인이 중앙 로그에 남는다(현장·용도·등록 키 지문, 반입이면 token 이름)."""
    other = dumps(sign_document(bytes(range(1, 33)), SitePurpose.REPORTS, PAYLOAD))
    with caplog.at_level(logging.WARNING, logger="pureiron_control.api.errors"):
        assert _status(other, SitePurpose.REPORTS, token="usb-import")[0] == 409
    (record,) = caplog.records
    message = record.getMessage()
    assert "site site-a: site-reports signature" in message
    assert keys.fingerprint(PUBLIC) in message
    assert "(token usb-import)" in message


def test_tampered_payload_is_also_409() -> None:
    """서명 뒤 payload가 바뀐 봉투도 등록 키로 확인되지 않는다 — 현장 Updater는 매번 새로 서명하므로
    현장에서 이 응답은 키 불일치뿐이다."""
    doc = json.loads(dumps(sign_document(SEED, SitePurpose.REPORT, PAYLOAD)))
    doc["payload"]["result"] = "failed"
    assert _status(json.dumps(doc).encode())[0] == 409


def _short_signature() -> bytes:
    """R2-N1: 길이가 64B가 아닌 서명은 형식 위반(400)이지 키 불일치(409)가 아니다."""
    doc = json.loads(dumps(sign_document(SEED, SitePurpose.REPORT, PAYLOAD)))
    doc["signature"] = "00"
    return json.dumps(doc).encode()


@pytest.mark.parametrize(
    ("data", "purpose"),
    [
        (b"{not json", SitePurpose.REPORT),
        (dumps(sign_document(SEED, SitePurpose.HEARTBEAT, PAYLOAD)), SitePurpose.REPORT),
        (
            json.dumps({"format": "x", "purpose": "site-report", "payload": {}}).encode(),
            SitePurpose.REPORT,
        ),
        (dumps(sign_document(SEED, SitePurpose.REPORT, {"report_seq": 1})), SitePurpose.REPORT),
        (_short_signature(), SitePurpose.REPORT),
    ],
    ids=["json", "purpose", "envelope-keys", "payload-site-id", "signature-length"],
)
def test_format_violations_stay_400(data: bytes, purpose: SitePurpose) -> None:
    assert _status(data, purpose)[0] == 400
