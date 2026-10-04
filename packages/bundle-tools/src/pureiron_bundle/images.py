"""이미지 신원 = image config digest(PD-19, ADR-023).

docker save 형식 tar(압축 여부 무관)의 `manifest.json` 첫 이미지 `Config` 항목에서 config digest를
읽는다. config digest는 설정 blob의 내용 주소라 save/load·image store 종류와 무관하게 보존되고,
classic image store에서는 `docker image inspect -f '{{.Id}}'`와 같다.
빌더(deploy/build·deploy/bundle)와 검증(verify)이 이 구현 하나를 쓴다.

검증에서는 **sha256이 manifest와 일치한 뒤에만**(= 서명된 내용) 추출된 파일을 이 모듈로 해석한다.
"""

from __future__ import annotations

import json
import re
import tarfile
from pathlib import Path
from typing import cast

from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.tarsafe import PARSER_EXHAUSTION, Readable, open_tar_stream

_BLOB = re.compile(r"blobs/sha256/([0-9a-f]{64})")
_LEGACY = re.compile(r"([0-9a-f]{64})\.json")
_MANIFEST_LIMIT = 1024 * 1024
_MANIFEST = "manifest.json"


class ImageFormatError(ValueError):
    pass


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
    match = _BLOB.fullmatch(config) or _LEGACY.fullmatch(config)
    if match is None:
        raise ImageFormatError(f"{source}: unexpected Config entry {config!r}")
    return f"sha256:{match[1]}"


def config_digest_from_stream(stream: Readable, source: str = "<image>") -> str:
    """docker save 형식 tar 스트림(gzip 자동 판별)을 끝까지 읽어 config digest를 돌려준다.

    `manifest.json`이 없거나 2개 이상이면 거부한다. 헤더 제한은 `tarsafe`를 따른다.
    """
    found: str | None = None
    try:
        with open_tar_stream(stream, "r|*") as tar:
            for member in tar:
                if member.name.removeprefix("./") != _MANIFEST:
                    continue
                if found is not None:
                    raise ImageFormatError(f"{source}: duplicate manifest.json")
                if not member.isreg() or member.sparse is not None:
                    raise ImageFormatError(f"{source}: manifest.json is not a regular file")
                if member.size > _MANIFEST_LIMIT:
                    raise ImageFormatError(f"{source}: manifest.json larger than {_MANIFEST_LIMIT}")
                handle = tar.extractfile(member)
                if handle is None:
                    raise ImageFormatError(f"{source}: manifest.json unreadable")
                found = _parse_manifest(handle.read(), source)
    except ImageFormatError:
        raise
    except (tarfile.TarError, EOFError, OSError, ValueError, *PARSER_EXHAUSTION) as exc:
        raise ImageFormatError(f"{source}: not a readable image tar ({exc!r})") from exc
    if found is None:
        raise ImageFormatError(f"{source}: manifest.json missing")
    return found


def config_digest_from_tar(path: Path) -> str:
    """docker save 형식 tar(.tar 또는 .tar.gz) 파일의 config digest(`sha256:<hex>`)."""
    with path.open("rb") as stream:
        return config_digest_from_stream(stream, str(path))


def check_loaded_image(service: str, image_id: str, config_digest: str) -> None:
    """`docker load` 후 `docker image inspect -f '{{.Id}}'` 값이 manifest config digest와 같은지.

    Updater는 **load 직후와 `up -d` 직전 두 번** 반드시 호출해야 한다(PD-19). `verify_bundle`을
    `extract_to` 없이 돌린 경우 이미지 tar를 해석하지 않으므로, 그때는 이 검사가 유일한 이미지
    신원 확인이다. 다르면 `E_DIGEST`.
    """
    if image_id.strip() != config_digest:
        raise BundleRejectedError(
            RejectCode.E_DIGEST,
            f"{service}: loaded image id {image_id.strip()} != config digest {config_digest}",
        )
