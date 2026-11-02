"""M4-1 E9(M4-0 리뷰 R-3·L-5): 드래프트·enrollment 저장 범위·문자 검사와 드래프트 파일 쓰기 오류.

DB가 필요한 것(drafts/latest 순서, 저장 실패 시 행·파일 없음, desired의 `last_seen`)은
`tests/integration/test_updater_send_db.py`가 실제 control-db로 확인한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pureiron_bundle import keys
from pureiron_bundle.sitesign import (
    Json,
    SitePurpose,
    dumps,
    pack_draft,
    sign_document,
    tree_digest,
)
from pureiron_control.drafts import (
    DraftKeyMismatchError,
    DraftSignatureError,
    DraftStorageError,
    check_payload,
    draft_exists,
    open_site_draft,
    place_draft,
)
from pureiron_control.platform.seqmarks import SEQ_MAX
from pureiron_control.platform.storable import unstorable_at
from pureiron_control.sites import EnrollmentError, parse_enrollment

SEED = bytes(range(32))
NUL = chr(0)
LONE_SURROGATE = chr(0xD800)


def _draft_payload(**changes: Json) -> dict[str, Json]:
    payload: dict[str, Json] = {
        "site_id": "site-a",
        "site_rev": 5,
        "base_site_rev": 2,
        "content_hash": "sha256:" + "0" * 64,
        "exported_at": "2026-11-02T00:00:00Z",
        "project_tar_sha256": "sha256:" + "1" * 64,
    }
    return {**payload, **changes}


def test_draft_payload_at_the_bigint_limit_is_storable() -> None:
    check_payload(_draft_payload(site_rev=SEQ_MAX, base_site_rev=0))


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"site_rev": SEQ_MAX + 1}, "site_rev"),
        ({"base_site_rev": SEQ_MAX + 1}, "base_site_rev"),
        ({"content_hash": "sha256:" + NUL}, "content_hash"),
        ({"exported_at": "2026" + LONE_SURROGATE}, "exported_at"),
    ],
)
def test_unstorable_draft_payload_is_400(changes: dict[str, Json], field: str) -> None:
    with pytest.raises(DraftSignatureError, match=f"payload.{field}"):
        check_payload(_draft_payload(**changes))


def test_draft_file_write_failure_is_storage_error_not_oserror(tmp_path: Path) -> None:
    """DB 연결 본문 안의 `OSError`는 연결 계층이 503으로 바꾼다 — 저장 오류는 507이어야 한다."""
    assert not issubclass(DraftStorageError, OSError)
    blocker = tmp_path / "drafts" / "site-a"
    blocker.parent.mkdir()
    blocker.write_bytes(b"")
    with pytest.raises(DraftStorageError, match="cannot write draft file") as caught:
        place_draft(blocker / "x.tar", b"draft")
    assert isinstance(caught.value.__cause__, OSError)


def test_unreadable_draft_directory_is_a_storage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """리뷰 L5: 디렉터리 실행 권한이 없으면 `exists()`도 `PermissionError` — 503이 아니라 507."""

    def denied(_self: Path) -> bool:
        raise PermissionError(13, "Permission denied")

    target = tmp_path / "drafts" / "site-a" / "x.tar"
    assert not draft_exists(target)
    monkeypatch.setattr(Path, "exists", denied)
    with pytest.raises(DraftStorageError, match="cannot check draft file") as caught:
        draft_exists(target)
    assert isinstance(caught.value.__cause__, PermissionError)


# drafts/latest의 실제 순서(가장 높은 site_rev)는 통합
# `test_updater_send_db.py::test_draft_upload_latest_is_the_highest_site_rev`가 고정한다.


def _draft(seed: bytes, **changes: Json) -> bytes:
    tree = b"project tree"
    payload = _draft_payload(project_tar_sha256=tree_digest(tree), **changes)
    return pack_draft(dumps(sign_document(seed, SitePurpose.DRAFT, payload)), tree)


def test_draft_signed_by_another_key_is_a_key_mismatch_not_a_format_error() -> None:
    """리뷰 M1: 등록 키로 확인되지 않는 서명은 409(rekey 대기), 형식 위반은 400."""
    public = keys.public_key(SEED)
    assert open_site_draft(_draft(SEED), public, "site-a")["site_rev"] == 5
    with pytest.raises(DraftKeyMismatchError, match="signature does not match"):
        open_site_draft(_draft(bytes(32)), public, "site-a")
    with pytest.raises(DraftSignatureError, match="not a valid tar"):
        open_site_draft(b"not a tar", public, "site-a")
    with pytest.raises(DraftSignatureError, match="does not match expected site"):
        open_site_draft(_draft(SEED), public, "site-b")


def _enrollment(next_seq: int) -> bytes:
    public = keys.public_key(SEED)
    payload: dict[str, Json] = {
        "site_id": "site-a",
        "public_key": public.hex(),
        "fingerprint": keys.fingerprint(public),
        "next_report_seq": next_seq,
        "seq_pending": False,
    }
    return dumps(sign_document(SEED, SitePurpose.ENROLLMENT, payload))


def test_enrollment_next_seq_is_bounded() -> None:
    """서명(JCS)은 2^53-1까지만 표현한다 — 그 위(bigint 상한 포함)는 서명 단계나 범위 검사에서
    400(범위 검사는 방어선)."""
    jcs_max = 2**53 - 1
    assert parse_enrollment(_enrollment(jcs_max)).next_report_seq == jcs_max
    forged = _enrollment(jcs_max).replace(str(jcs_max).encode(), str(SEQ_MAX + 1).encode())
    with pytest.raises(EnrollmentError):
        parse_enrollment(forged)
    with pytest.raises(EnrollmentError, match="next_report_seq"):
        parse_enrollment(_enrollment(0))


def test_unstorable_at_reports_the_first_path() -> None:
    assert unstorable_at({"a": ["ok", {"b": "x" + NUL}]}, "payload") == "payload.a[].b"
    assert unstorable_at({LONE_SURROGATE: 1}, "payload") == "payload key"
    assert unstorable_at({"a": [1, None, "fine"]}, "payload") is None
