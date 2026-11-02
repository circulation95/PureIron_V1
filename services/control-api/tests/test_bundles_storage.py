"""번들 게시의 데이터 디렉터리 쓰기 실패(리뷰 L-1): control-db 장애(503)가 아니라 저장 오류(507)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pureiron_control.bundles import BundleStorageError, place_bundle
from pureiron_control.platform.files import spool


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def test_storage_error_is_not_an_oserror() -> None:
    # DB 연결 본문 안의 OSError는 `Database.connection()`이 DatabaseUnavailableError(503)로 바꾼다.
    assert not issubclass(BundleStorageError, OSError)


async def test_place_failure_becomes_storage_error(tmp_path: Path) -> None:
    spooled = await spool(_chunks(b"pib"), tmp_path / "in", limit_bytes=16)
    # 현장 디렉터리 자리에 파일이 있으면 mkdir이 실패한다(디스크 가득·권한 오류와 같은 OSError).
    blocker = tmp_path / "bundles" / "site-a"
    blocker.parent.mkdir()
    blocker.write_bytes(b"")
    with pytest.raises(BundleStorageError, match="cannot place bundle file") as caught:
        place_bundle(spooled, blocker / "g1.pib")
    assert isinstance(caught.value.__cause__, OSError)
    assert spooled.path.read_bytes() == b"pib"  # 임시 파일은 호출자(finally)가 지운다
