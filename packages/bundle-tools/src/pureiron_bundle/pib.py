"""`.pib` 컨테이너 형식: 압축하지 않은 POSIX tar(pax).

멤버 순서는 고정이다: `manifest.json`, `manifest.sig`, `release-cert.json`, `release-cert.sig`,
(선택) `trust-update.sig`, 그다음 payload를 경로(UTF-8 바이트) 오름차순으로.

- **writer**는 결정적이다: mtime 0, uid/gid 0, uname/gname 빈 값, mode 0644, 일반 파일만.
  payload는 디스크에서 스트리밍한다(이미지 수백 MB를 메모리에 올리지 않음).
- **reader**는 앞에서부터 한 번만 읽고(streaming), 적대적 입력을 가정한다: 절대 경로·`..`·
  역슬래시·계약 패턴 밖의 경로, 일반 파일이 아닌 멤버(디렉터리 포함 — 이 형식에는 디렉터리
  항목이 없다), 중복, 메타데이터 순서 위반, payload 정렬 위반, 크기 초과 메타데이터를
  `E_FORMAT`으로 거부한다. 메타데이터를 먼저 돌려주고, 호출자가 서명을 확인한 뒤에만
  payload를 읽게 한다(`read_metadata` → `iter_payload`).
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pureiron_bundle.errors import BundleRejected, RejectCode
from pureiron_bundle.images import Readable
from pureiron_bundle.manifest import MANIFEST_FILE, file_path_pattern

MANIFEST_SIG = "manifest.sig"
CERT_FILE = "release-cert.json"
CERT_SIG = "release-cert.sig"
TRUST_UPDATE_SIG = "trust-update.sig"
METADATA_ORDER = (MANIFEST_FILE, MANIFEST_SIG, CERT_FILE, CERT_SIG, TRUST_UPDATE_SIG)
REQUIRED_METADATA = METADATA_ORDER[:4]
METADATA_LIMIT = 1024 * 1024
CHUNK = 1024 * 1024
_REGULAR = (tarfile.REGTYPE, tarfile.AREGTYPE)


def tar_info(name: str, size: int) -> tarfile.TarInfo:
    """결정적 tar 헤더(mtime 0, root 소유, 0644, 일반 파일)."""
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def check_payload_name(name: str) -> None:
    if name in METADATA_ORDER:
        raise ValueError(f"payload path collides with bundle metadata: {name!r}")
    if not file_path_pattern().match(name):
        raise ValueError(f"invalid payload path: {name!r}")


def write_pib(out: Path, metadata: Mapping[str, bytes], payload: Mapping[str, Path]) -> None:
    """메타데이터(이름 → 바이트)와 payload(번들 경로 → 디스크 파일)로 `.pib`를 쓴다(원자적 교체)."""
    missing = [name for name in REQUIRED_METADATA if name not in metadata]
    unknown = [name for name in metadata if name not in METADATA_ORDER]
    if missing or unknown:
        raise ValueError(f"bundle metadata missing {missing} / unknown {unknown}")
    for name in payload:
        check_payload_name(name)
    tmp = out.with_name(out.name + ".tmp")
    try:
        with (
            tmp.open("wb") as raw,
            tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar,
        ):
            for name in METADATA_ORDER:
                data = metadata.get(name)
                if data is not None:
                    tar.addfile(tar_info(name, len(data)), io.BytesIO(data))
            for name in sorted(payload, key=lambda n: n.encode("utf-8")):
                with payload[name].open("rb") as handle:
                    tar.addfile(tar_info(name, os.fstat(handle.fileno()).st_size), handle)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)


def _reject(detail: str) -> BundleRejected:
    return BundleRejected(RejectCode.E_FORMAT, detail)


def _check_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if not name or name.startswith("/") or "\\" in name or ".." in name.split("/"):
        raise _reject(f"unsafe member path {name!r}")
    if member.type not in _REGULAR or member.sparse is not None:
        raise _reject(f"member {name!r} is not a regular file (type {member.type!r})")


@dataclass(frozen=True, slots=True)
class PayloadMember:
    name: str
    size: int
    stream: IO[bytes]
    """이 멤버의 데이터(다음 멤버로 넘어가면 무효)."""


class HashingReader(io.RawIOBase):
    """읽는 바이트를 sha256과 (선택) 출력 파일에 함께 흘려보내는 읽기 래퍼."""

    def __init__(self, source: Readable, sink: IO[bytes] | None = None) -> None:
        super().__init__()
        self._source = source
        self._sink = sink
        self._hash = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(CHUNK if size < 0 else size)
        self._hash.update(data)
        if self._sink is not None:
            self._sink.write(data)
        return data

    def readable(self) -> bool:
        return True

    def drain(self) -> str:
        """남은 바이트를 모두 읽고 `sha256:<hex>`를 돌려준다."""
        while self.read(CHUNK):
            pass
        return "sha256:" + self._hash.hexdigest()


@contextmanager
def open_pib(path: Path) -> Generator[PibReader]:
    """`with open_pib(path) as reader:` — 파일과 tar 스트림을 함께 닫는다.

    읽는 도중의 tar 손상(잘림·헤더 오류)은 `E_FORMAT`으로 바꾼다.
    """
    with path.open("rb") as raw:
        try:
            with tarfile.open(fileobj=raw, mode="r|") as tar:
                yield PibReader(tar)
        except tarfile.TarError as exc:
            raise _reject(f"{path.name}: corrupt tar archive ({exc})") from exc


class PibReader:
    """스트리밍 `.pib` 읽기(`open_pib`로 연다).

    사용 순서: `read_metadata()` → (서명 확인) → `iter_payload()`.
    """

    def __init__(self, tar: tarfile.TarFile) -> None:
        self._tar = tar
        self._pending: tarfile.TarInfo | None = None
        self._seen: set[str] = set()
        self._metadata_done = False

    def _next(self) -> tarfile.TarInfo | None:
        try:
            member = self._tar.next()
        except tarfile.TarError as exc:
            raise _reject(f"corrupt tar archive ({exc})") from exc
        if member is not None:
            _check_member(member)
            if member.name in self._seen:
                raise _reject(f"duplicate member {member.name!r}")
            self._seen.add(member.name)
        return member

    def _read_small(self, member: tarfile.TarInfo) -> bytes:
        if member.size > METADATA_LIMIT:
            raise _reject(f"{member.name}: metadata member larger than {METADATA_LIMIT} bytes")
        handle = self._tar.extractfile(member)
        data = b"" if handle is None else handle.read()
        if len(data) != member.size:
            raise _reject(f"{member.name}: truncated")
        return data

    def read_metadata(self) -> dict[str, bytes]:
        """고정 순서 메타데이터 멤버들(이름 → 바이트). 첫 payload 멤버는 보류해 둔다."""
        if self._metadata_done:
            raise RuntimeError("metadata already read")
        self._metadata_done = True
        metadata: dict[str, bytes] = {}
        while True:
            member = self._next()
            if member is None or member.name not in METADATA_ORDER:
                self._pending = member
                break
            if member.name != METADATA_ORDER[len(metadata)]:
                raise _reject(f"metadata member {member.name!r} out of order")
            metadata[member.name] = self._read_small(member)
        missing = [name for name in REQUIRED_METADATA if name not in metadata]
        if missing:
            raise _reject(f"missing or misplaced metadata members {missing}")
        return metadata

    def iter_payload(self) -> Iterator[PayloadMember]:
        """payload 멤버를 순서대로. 경로 패턴·정렬·메타데이터 이름 재등장을 검사한다."""
        if not self._metadata_done:
            raise RuntimeError("read_metadata() first")
        member, self._pending = self._pending, None
        previous: bytes | None = None
        pattern = file_path_pattern()
        while member is not None:
            name = member.name
            if name in METADATA_ORDER:
                raise _reject(f"metadata member {name!r} after payload")
            if not pattern.match(name):
                raise _reject(f"payload path {name!r} does not match the manifest files pattern")
            key = name.encode("utf-8")
            if previous is not None and key <= previous:
                raise _reject(f"payload member {name!r} out of order")
            previous = key
            handle = self._tar.extractfile(member)
            if handle is None:
                raise _reject(f"payload member {name!r} unreadable")
            yield PayloadMember(name, member.size, handle)
            member = self._next()


def safe_target(root: Path, name: str) -> Path:
    """`root` 아래 payload 경로. 기존 링크를 따라 root 밖으로 나가면 거부."""
    target = root.joinpath(*name.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve().is_relative_to(root.resolve()):
        raise _reject(f"payload path {name!r} escapes the extraction directory")
    return target
