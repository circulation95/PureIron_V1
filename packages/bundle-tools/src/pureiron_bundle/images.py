"""이미지 신원 = image config digest(PD-19, ADR-023).

docker save 형식 tar(압축 여부 무관)의 `manifest.json` 첫 이미지 `Config` 항목에서 config digest를
읽는다. config digest는 설정 blob의 내용 주소라 save/load·image store 종류와 무관하게 보존되고,
classic image store에서는 `docker image inspect -f '{{.Id}}'`와 같다.
빌더(deploy/build·deploy/bundle)와 검증(verify)이 이 구현 하나를 쓴다.
"""

from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path
from typing import Protocol, cast

from pureiron_bundle.errors import BundleRejected, RejectCode

_BLOB = re.compile(r"^blobs/sha256/([0-9a-f]{64})$")
_LEGACY = re.compile(r"^([0-9a-f]{64})\.json$")
_MANIFEST_LIMIT = 1024 * 1024


class ImageFormatError(ValueError):
    pass


class Readable(Protocol):
    """`tarfile.open(fileobj=…)`이 요구하는 파일 객체 모양(스트림 모드는 read만 실제로 쓴다)."""

    def read(self, size: int, /) -> bytes: ...

    def write(self, b: bytes, /) -> object: ...

    def tell(self) -> int: ...

    def seek(self, pos: int, /) -> object: ...

    def close(self) -> object: ...


def _parse_manifest(data: bytes, source: str) -> str:
    try:
        manifest: object = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageFormatError(f"{source}: manifest.json is not JSON ({exc})") from exc
    if not isinstance(manifest, list) or not manifest:
        raise ImageFormatError(f"{source}: manifest.json is not a non-empty list")
    first: object = cast(list[object], manifest)[0]
    if not isinstance(first, dict):
        raise ImageFormatError(f"{source}: manifest.json entry is not an object")
    config = str(cast(dict[str, object], first).get("Config", ""))
    match = _BLOB.match(config) or _LEGACY.match(config)
    if match is None:
        raise ImageFormatError(f"{source}: unexpected Config entry {config!r}")
    return f"sha256:{match[1]}"


def config_digest_from_stream(stream: Readable, source: str = "<image>") -> str:
    """앞에서부터 한 번만 읽는 스트림(gzip 자동 판별)에서 config digest를 읽는다.

    docker save는 `manifest.json`을 끝쪽에 두므로 결국 스트림 대부분을 읽는다. 호출자는 남은
    바이트(패딩·gzip 트레일러)를 직접 소진해야 한다(해시 계산 등).
    """
    try:
        with tarfile.open(fileobj=stream, mode="r|*") as tar:
            for member in tar:
                if member.name.removeprefix("./") != "manifest.json":
                    continue
                if not member.isreg() or member.size > _MANIFEST_LIMIT:
                    raise ImageFormatError(f"{source}: manifest.json is not a small regular file")
                handle = tar.extractfile(member)
                if handle is None:
                    raise ImageFormatError(f"{source}: manifest.json unreadable")
                return _parse_manifest(handle.read(), source)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ImageFormatError(f"{source}: not a readable image tar ({exc})") from exc
    raise ImageFormatError(f"{source}: manifest.json missing")


def config_digest_from_tar(path: Path) -> str:
    """docker save 형식 tar(.tar 또는 .tar.gz) 파일의 config digest(`sha256:<hex>`)."""
    with path.open("rb") as stream:
        return config_digest_from_stream(stream, str(path))


def check_loaded_image(service: str, image_id: str, config_digest: str) -> None:
    """`docker load` 후 `docker image inspect -f '{{.Id}}'` 값이 manifest config digest와 같은지.

    Updater가 load 직후와 `up -d` 직전에 호출한다(PD-19). 다르면 `E_DIGEST`.
    """
    if image_id.strip() != config_digest:
        raise BundleRejected(
            RejectCode.E_DIGEST,
            f"{service}: loaded image id {image_id.strip()} != config digest {config_digest}",
        )
