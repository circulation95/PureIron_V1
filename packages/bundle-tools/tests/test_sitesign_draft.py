"""현장 드래프트 파일(M5-2 P6·P7): `pack_draft` 결정성과 `open_draft` 거부 표 — 서명 위조·다른
키·다른 purpose·다른 site_id·tar 바뀜·항목 추가·누락·payload 형식."""

from __future__ import annotations

import io
import tarfile

import pytest

from pureiron_bundle import keys
from pureiron_bundle.manifest import Json
from pureiron_bundle.sitesign import (
    DRAFT_ENVELOPE,
    DRAFT_TREE,
    SignedDocumentError,
    SitePurpose,
    dumps,
    open_draft,
    pack_draft,
    sign_document,
    tree_digest,
)

SEED = bytes(range(32))
SITE = "site-a"
TREE = b"project-tree-bytes"


def _payload(overrides: dict[str, Json] | None = None, tree: bytes = TREE) -> dict[str, Json]:
    payload: dict[str, Json] = {
        "site_id": SITE,
        "site_rev": 3,
        "base_site_rev": 0,
        "content_hash": "sha256:" + "a" * 64,
        "exported_at": "2026-10-25T00:00:00Z",
        "project_tar_sha256": tree_digest(tree),
    }
    payload.update(overrides or {})
    return payload


def _draft(
    payload: dict[str, Json] | None = None,
    *,
    tree: bytes = TREE,
    seed: bytes = SEED,
    purpose: SitePurpose = SitePurpose.DRAFT,
) -> bytes:
    envelope = dumps(sign_document(seed, purpose, payload or _payload(tree=tree)))
    return pack_draft(envelope, tree)


def _raw_tar(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _members(data: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar:
            handle = tar.extractfile(member)
            assert handle is not None
            out[member.name] = handle.read()
    return out


def test_open_draft_returns_signed_payload_and_tree() -> None:
    payload, tree = open_draft(_draft(), keys.public_key(SEED), site_id=SITE)
    assert payload == _payload()
    assert tree == TREE


def test_pack_draft_is_deterministic_regular_files_in_byte_order() -> None:
    first, second = _draft(), _draft()
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first)) as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == [DRAFT_ENVELOPE, DRAFT_TREE]
    assert {(m.mtime, m.uid, m.gid, m.mode, m.isreg()) for m in members} == {(0, 0, 0, 0o644, True)}


def test_other_site_key_is_rejected() -> None:
    other = keys.public_key(bytes(32))
    with pytest.raises(SignedDocumentError, match="signature does not match"):
        open_draft(_draft(), other, site_id=SITE)


def test_tampered_payload_is_rejected() -> None:
    members = _members(_draft())
    forged = members[DRAFT_ENVELOPE].replace(b'"site_rev": 3', b'"site_rev": 9')
    assert forged != members[DRAFT_ENVELOPE]
    with pytest.raises(SignedDocumentError, match="signature does not match"):
        open_draft(pack_draft(forged, TREE), keys.public_key(SEED), site_id=SITE)


def test_payload_for_other_site_is_rejected() -> None:
    """현장 X 키로 서명됐어도 payload site_id가 기대 현장과 다르면 그 현장 드래프트가 아니다."""
    with pytest.raises(SignedDocumentError, match="site_id 'site-b'"):
        open_draft(_draft(_payload({"site_id": "site-b"})), keys.public_key(SEED), site_id=SITE)


@pytest.mark.parametrize("purpose", [SitePurpose.REPORTS, SitePurpose.ENROLLMENT])
def test_other_purpose_is_rejected(purpose: SitePurpose) -> None:
    with pytest.raises(SignedDocumentError, match="purpose site-draft"):
        open_draft(_draft(purpose=purpose), keys.public_key(SEED), site_id=SITE)


def test_changed_tree_is_rejected() -> None:
    envelope = _members(_draft())[DRAFT_ENVELOPE]
    with pytest.raises(SignedDocumentError, match="project_tar_sha256"):
        open_draft(pack_draft(envelope, TREE + b"x"), keys.public_key(SEED), site_id=SITE)


@pytest.mark.parametrize(
    "members",
    [
        [(DRAFT_ENVELOPE, b"{}")],
        [(DRAFT_ENVELOPE, b"{}"), (DRAFT_TREE, TREE), ("extra.txt", b"x")],
        [(DRAFT_ENVELOPE, b"{}"), (DRAFT_ENVELOPE, b"{}"), (DRAFT_TREE, TREE)],
        [("../draft.json", b"{}"), (DRAFT_TREE, TREE)],
    ],
    ids=["missing-tree", "extra-entry", "duplicate", "escape"],
)
def test_unexpected_tar_layout_is_rejected(members: list[tuple[str, bytes]]) -> None:
    with pytest.raises(SignedDocumentError):
        open_draft(_raw_tar(members), keys.public_key(SEED), site_id=SITE)


def test_not_a_tar_is_rejected() -> None:
    with pytest.raises(SignedDocumentError, match="not a valid tar"):
        open_draft(b"not a tar" * 100, keys.public_key(SEED), site_id=SITE)


@pytest.mark.parametrize(
    "overrides",
    [{"site_rev": -1}, {"base_site_rev": "0"}, {"site_rev": True}, {"content_hash": 1}],
)
def test_malformed_payload_is_rejected(overrides: dict[str, Json]) -> None:
    with pytest.raises(SignedDocumentError, match="payload"):
        open_draft(_draft(_payload(overrides)), keys.public_key(SEED), site_id=SITE)
