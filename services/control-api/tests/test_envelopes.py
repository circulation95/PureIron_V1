"""서명 확인 전 `payload.site_id` 꺼내기: 403 판정에만 쓰고, 형식이 틀리면 None(검증이 거부)."""

from __future__ import annotations

import json

import pytest

from pureiron_bundle.sitesign import (
    SitePurpose,
    dumps,
    pack_draft,
    sign_document,
)
from pureiron_control.platform.envelopes import claimed_draft_site, claimed_site

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
