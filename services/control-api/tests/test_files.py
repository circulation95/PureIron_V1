"""업로드 임시 파일: sha256·크기를 재며 받고, 상한을 넘으면 임시 파일 없이 실패한다."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pureiron_control.platform.files import UploadTooLargeError, discard, place, spool, write_new


def _entries(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def test_spool_measures_and_place_moves(tmp_path: Path) -> None:
    spooled = await spool(_chunks(b"ab", b"", b"c"), tmp_path / "in", limit_bytes=3)
    # 기대값: `printf abc | sha256sum`
    assert spooled.sha256 == (
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert spooled.size_bytes == 3
    target = tmp_path / "out" / "g1.pib"
    place(spooled, target)
    assert target.read_bytes() == b"abc"
    assert _entries(tmp_path / "in") == []
    discard(spooled)  # 이미 옮긴 파일은 조용히 넘어간다


async def test_spool_over_limit_leaves_nothing(tmp_path: Path) -> None:
    with pytest.raises(UploadTooLargeError):
        await spool(_chunks(b"ab", b"cd"), tmp_path, limit_bytes=3)
    assert _entries(tmp_path) == []


def test_write_new_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "d" / "x.tar"
    write_new(target, b"one")
    write_new(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in target.parent.iterdir()] == ["x.tar"]
