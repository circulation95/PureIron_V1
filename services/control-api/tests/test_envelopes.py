"""서명 확인 전 `payload.site_id` 꺼내기: 403 판정에만 쓰고, 형식이 틀리면 None(검증이 거부)."""

from __future__ import annotations

import json

import pytest

from pureiron_bundle import keys
from pureiron_bundle.sitesign import (
    Json,
    SitePurpose,
    dumps,
    pack_draft,
    sign_document,
)
from pureiron_control.platform.envelopes import claimed_draft_site, claimed_site
from pureiron_control.sites import EnrollmentError, parse_enrollment

SEED = bytes(range(32))


def test_signed_envelope_site_is_read() -> None:
    data = dumps(sign_document(SEED, SitePurpose.HEARTBEAT, {"site_id": "site-a"}))
    assert claimed_site(data) == "site-a"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not json",
        b"[]",
        b'{"payload": []}',
        b'{"payload": {"site_id": 3}}',
        json.dumps({"payload": {}}).encode(),
        "￾".encode("utf-16"),
    ],
)
def test_unusable_envelope_has_no_claimed_site(data: bytes) -> None:
    assert claimed_site(data) is None


def test_draft_tar_site_is_read_from_its_envelope() -> None:
    envelope = dumps(sign_document(SEED, SitePurpose.DRAFT, {"site_id": "site-b"}))
    assert claimed_draft_site(pack_draft(envelope, b"tree")) == "site-b"


@pytest.mark.parametrize("data", [b"", b"x" * 1024, pack_draft(b"{}", b"tree")[:600]])
def test_unusable_draft_has_no_claimed_site(data: bytes) -> None:
    assert claimed_draft_site(data) is None


def test_draft_envelope_without_payload_has_no_claimed_site() -> None:
    assert claimed_draft_site(pack_draft(b"{}", b"tree")) is None


def test_enrollment_with_unpaired_surrogate_is_an_enrollment_error() -> None:
    """리뷰 r3 R3-1: 서명 단계에서 서로게이트를 거부해 `POST /sites`·rekey가 500이 아니라
    400이다."""
    public = keys.public_key(SEED)
    payload: dict[str, Json] = {
        "site_id": "site-a",
        "public_key": public.hex(),
        "fingerprint": keys.fingerprint(public),
        "next_report_seq": 1,
        "note": "ab",
    }
    data = dumps(sign_document(SEED, SitePurpose.ENROLLMENT, payload))
    with pytest.raises(EnrollmentError, match="non-UTF-8 code points"):
        parse_enrollment(data.replace(b'"ab"', b'"a\\ud800b"'))
