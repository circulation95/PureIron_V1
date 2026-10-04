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
- payload를 끝까지 읽으면 **정규형**인지 확인한다: 각 멤버 헤더(필요 시 pax `path` 확장 헤더 포함)가
  writer가 같은 이름·크기로 만드는 바이트와 같고, 데이터 블록 패딩이 0이며, 끝은 0 블록 2개 +
  `RECORDSIZE`(10240) 경계까지 0 패딩. 그래서 서명된 내용이 같으면 `.pib` 바이트도 하나뿐이다.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from collections.abc import Generator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.manifest import MANIFEST_FILE, file_path_pattern, path_collision
from pureiron_bundle.tarsafe import PARSER_EXHAUSTION, open_tar_stream

MANIFEST_SIG = "manifest.sig"
CERT_FILE = "release-cert.json"
CERT_SIG = "release-cert.sig"
TRUST_UPDATE_SIG = "trust-update.sig"
METADATA_ORDER = (MANIFEST_FILE, MANIFEST_SIG, CERT_FILE, CERT_SIG, TRUST_UPDATE_SIG)
REQUIRED_METADATA = METADATA_ORDER[:4]
METADATA_LIMIT = 1024 * 1024
CHUNK = 1024 * 1024
_REGULAR = (tarfile.REGTYPE, tarfile.AREGTYPE)
_HEADER_FAILURES = (tarfile.TarError, ValueError, UnicodeError, *PARSER_EXHAUSTION)


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
    if not file_path_pattern().fullmatch(name):
        raise ValueError(f"invalid payload path: {name!r}")


def check_payload_names(names: Iterable[str]) -> None:
    """개별 경로 검사 + 경로끼리의 충돌(디렉터리 접두·대소문자 무시 동일) 검사."""
    names = list(names)
    for name in names:
        check_payload_name(name)
    collision = path_collision(names)
    if collision:
        raise ValueError(collision)


def write_pib(out: Path, metadata: Mapping[str, bytes], payload: Mapping[str, Path]) -> None:
    """메타데이터(이름 → 바이트)와 payload(번들 경로 → 디스크 파일)로 `.pib`를 쓴다(원자적 교체)."""
    missing = [name for name in REQUIRED_METADATA if name not in metadata]
    unknown = [name for name in metadata if name not in METADATA_ORDER]
    if missing or unknown:
        raise ValueError(f"bundle metadata missing {missing} / unknown {unknown}")
    check_payload_names(payload)
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


def _reject(detail: str) -> BundleRejectedError:
    return BundleRejectedError(RejectCode.E_FORMAT, detail)


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


def hash_stream(source: IO[bytes], sink: IO[bytes] | None = None) -> str:
    """끝까지 읽으며 sha256(`sha256:<hex>`)을 계산하고, 주면 `sink`에도 쓴다."""
    digest = hashlib.sha256()
    while chunk := source.read(CHUNK):
        digest.update(chunk)
        if sink is not None:
            sink.write(chunk)
    return "sha256:" + digest.hexdigest()


@contextmanager
def open_pib(path: Path) -> Generator[PibReader]:
    """`with open_pib(path) as reader:` — 파일과 tar 스트림을 함께 닫는다.

    헤더 해석 실패(헤더 제한 위반·pax 값 오류·파서 고갈)는 헤더를 읽는 지점(열 때의 첫 멤버,
    `PibReader._next`)에서만 `E_FORMAT`으로 바꾸고, 데이터 읽기 중 tar 손상(잘림)도 `E_FORMAT`이다.
    """
    with path.open("rb") as raw:
        try:
            tar = open_tar_stream(raw, "r|")
        except _HEADER_FAILURES as exc:
            raise _reject(f"{path.name}: corrupt tar header ({exc!r})") from exc
        try:
            with tar:
                yield PibReader(tar, raw)
        except tarfile.TarError as exc:
            raise _reject(f"{path.name}: corrupt tar archive ({exc!r})") from exc


class PibReader:
    """스트리밍 `.pib` 읽기(`open_pib`로 연다).

    사용 순서: `read_metadata()` → (서명 확인) → `iter_payload()`. payload를 끝까지 읽으면
    아카이브 끝 표시(0 블록 2개)와 레코드 패딩이 모두 0이고 그 뒤에 아무것도 없는지 확인한다.
    """

    def __init__(self, tar: tarfile.TarFile, raw: IO[bytes]) -> None:
        self._tar = tar
        self._raw = raw
        self._pending: tarfile.TarInfo | None = None
        self._seen: set[str] = set()
        self._metadata_done = False
        self._layout: list[tuple[int, int, str, int]] = []

    def _next(self) -> tarfile.TarInfo | None:
        try:
            member = self._tar.next()
        except _HEADER_FAILURES as exc:
            # 헤더 해석 실패·파서 고갈은 이 지점에서만 형식 오류로 본다(서명 뒤 단계와 구분).
            raise _reject(f"corrupt tar header ({exc!r})") from exc
        if member is not None:
            self._layout.append((member.offset, member.offset_data, member.name, member.size))
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
        """payload 멤버를 순서대로. 경로 패턴·정렬·메타데이터 이름 재등장·끝 표시를 검사한다."""
        if not self._metadata_done:
            raise RuntimeError("read_metadata() first")
        member, self._pending = self._pending, None
        previous: bytes | None = None
        pattern = file_path_pattern()
        while member is not None:
            name = member.name
            if name in METADATA_ORDER:
                raise _reject(f"metadata member {name!r} after payload")
            if not pattern.fullmatch(name):
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
        self._check_canonical_layout()
        self._check_trailer()

    def _zeros(self, start: int, length: int, what: str) -> int:
        """`start`부터 `length` 바이트가 모두 0인지. 실제로 읽은 바이트 수를 돌려준다."""
        self._raw.seek(start)
        done = 0
        while done < length:
            chunk = self._raw.read(min(length - done, CHUNK))
            if not chunk:
                break
            if chunk.count(0) != len(chunk):
                raise _reject(f"non-zero bytes in {what}")
            done += len(chunk)
        return done

    def _check_canonical_layout(self) -> None:
        """멤버 헤더가 writer의 결정적 헤더와 바이트 단위로 같고 블록 패딩이 0인지."""
        position = 0
        for offset, offset_data, name, size in self._layout:
            if offset != position:
                raise _reject(f"{name}: unexpected gap before member header")
            expected = tar_info(name, size).tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape")
            self._raw.seek(offset)
            if offset_data - offset != len(expected) or self._raw.read(len(expected)) != expected:
                raise _reject(f"{name}: member header is not canonical")
            padding = -size % tarfile.BLOCKSIZE
            if self._zeros(offset_data + size, padding, f"{name} block padding") != padding:
                raise _reject(f"{name}: archive ends inside block padding")
            position = offset_data + size + padding
        if position != self._tar.offset:
            raise _reject("unexpected data between the last member and the end-of-archive marker")

    def _check_trailer(self) -> None:
        """마지막 멤버 뒤 = 0 블록 2개 + RECORDSIZE(10240) 경계까지 0 패딩, 그 뒤 EOF."""
        end = self._tar.offset
        marker = 2 * tarfile.BLOCKSIZE
        expected = -(-(end + marker) // tarfile.RECORDSIZE) * tarfile.RECORDSIZE
        present = self._zeros(end, expected - end, "end-of-archive marker or record padding")
        if present < marker:
            raise _reject("truncated: archive ends before the end-of-archive marker")
        if present < expected - end:
            raise _reject(
                f"missing record padding: .pib must be padded with zeros to a multiple of "
                f"{tarfile.RECORDSIZE} bytes"
            )
        if self._raw.read(1):
            raise _reject("trailing data after the tar record padding")


def safe_target(root: Path, name: str) -> Path:
    """`root` 아래 payload 경로. 만들기 전과 후에 모두 root 안인지 확인한다(기존 링크 포함)."""
    base = root.resolve()
    target = root.joinpath(*name.split("/"))
    if not target.parent.resolve().is_relative_to(base):
        raise _reject(f"payload path {name!r} escapes the extraction directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve(strict=True).is_relative_to(base):
        raise _reject(f"payload path {name!r} escapes the extraction directory")
    return target
