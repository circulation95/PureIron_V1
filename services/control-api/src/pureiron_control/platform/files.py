"""데이터 디렉터리 파일 쓰기: 업로드를 임시 파일로 받으며 sha256·크기를 재고 이름 바꾸기로 둔다.

번들(수백 MB~GB)은 메모리에 올리지 않는다. 디스크 쓰기는 이벤트 루프를 막지 않게 스레드에서 한다.
임시 파일은 같은 디렉터리(`.upload-*.part`)에 만들어 `os.replace`가 원자적이게 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

PART_PREFIX = ".upload-"
PART_SUFFIX = ".part"


class UploadTooLargeError(Exception):
    def __init__(self, limit_bytes: int) -> None:
        super().__init__(f"upload exceeds {limit_bytes} bytes")
        self.limit_bytes = limit_bytes


@dataclass(frozen=True, slots=True)
class SpooledFile:
    path: Path
    sha256: str
    """`sha256:<hex>`."""
    size_bytes: int


def _open_part(directory: Path) -> tuple[Path, BinaryIO]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{PART_PREFIX}{uuid.uuid4().hex}{PART_SUFFIX}"
    return path, path.open("xb")


def _finish(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()


async def spool(chunks: AsyncIterator[bytes], directory: Path, *, limit_bytes: int) -> SpooledFile:
    """`chunks`를 `directory` 안 임시 파일로 받는다. 상한을 넘거나 실패하면 임시 파일을 지운다."""
    path, handle = await asyncio.to_thread(_open_part, directory)
    digest = hashlib.sha256()
    size = 0
    try:
        async for chunk in chunks:
            size += len(chunk)
            if size > limit_bytes:
                raise UploadTooLargeError(limit_bytes)
            digest.update(chunk)
            await asyncio.to_thread(handle.write, chunk)
        await asyncio.to_thread(_finish, handle)
    except BaseException:
        handle.close()
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    return SpooledFile(path=path, sha256="sha256:" + digest.hexdigest(), size_bytes=size)


def discard(spooled: SpooledFile) -> None:
    with contextlib.suppress(FileNotFoundError):
        spooled.path.unlink()


def _fsync_dir(directory: Path) -> None:
    # Windows는 디렉터리 fd를 열 수 없다 — 개발 PC에서만 생략된다(중앙은 Linux).
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def place(spooled: SpooledFile, target: Path) -> None:
    """임시 파일을 `target` 이름으로 옮긴다(같은 이름이 있으면 덮어쓴다)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    spooled.path.replace(target)
    _fsync_dir(target.parent)


def write_new(target: Path, data: bytes) -> None:
    """작은 파일을 임시 이름으로 쓰고 fsync 뒤 `target`으로 옮긴다."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{PART_PREFIX}{uuid.uuid4().hex}{PART_SUFFIX}")
    try:
        with tmp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_dir(target.parent)
