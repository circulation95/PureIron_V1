"""이미지 신원 도구: docker save/exporter tar에서 config digest를 읽는다(PD-19, ADR-023).

config digest는 이미지 설정 blob의 내용 주소라 save/load·image store 종류와 무관하게 보존된다.
repro_build·release_record·(M2) 번들 빌더가 같은 함수를 쓴다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import cast

SERVICES = ("core", "collector", "web", "simulator", "core-test")
_BLOB = re.compile(r"^blobs/sha256/([0-9a-f]{64})$")
_LEGACY = re.compile(r"^([0-9a-f]{64})\.json$")


class ImageError(RuntimeError):
    pass


def config_digest_from_tar(path: Path) -> str:
    """docker 형식 tar의 manifest.json에서 첫 이미지의 config digest(`sha256:<hex>`)."""
    with tarfile.open(path) as tar:
        member = tar.extractfile("manifest.json")
        if member is None:
            raise ImageError(f"{path}: manifest.json missing")
        manifest: object = json.loads(member.read())
    if not isinstance(manifest, list) or not manifest:
        raise ImageError(f"{path}: manifest.json is not a non-empty list")
    first = cast(list[dict[str, object]], manifest)[0]
    config = str(first.get("Config", ""))
    match = _BLOB.match(config) or _LEGACY.match(config)
    if match is None:
        raise ImageError(f"{path}: unexpected Config entry {config!r}")
    return f"sha256:{match[1]}"


def config_digest_of_image(image: str, workdir: Path) -> str:
    """로컬 이미지의 config digest(docker save 경유 — image store 종류와 무관)."""
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / (re.sub(r"[^A-Za-z0-9_.-]", "_", image) + ".tar")
    subprocess.run(["docker", "save", "-o", str(target), image], check=True)
    try:
        return config_digest_from_tar(target)
    finally:
        target.unlink(missing_ok=True)


def read_lock(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.startswith("#"):
            values[key.strip()] = value.strip()
    return values


def image_store() -> Path:
    """release 이미지 tar 보관소(PD-23 ⑤: runner 호스트 로컬 저장).

    `PUREIRON_IMAGE_STORE`가 없으면 `~/pureiron/images`. runner(WSL)는 sudo 없이 쓸 수 있는
    홈 아래를 쓴다(plan의 `/srv/pureiron/images`는 runner 전용 사용자를 만들 때 옮긴다).
    """
    configured = os.environ.get("PUREIRON_IMAGE_STORE")
    return Path(configured) if configured else Path.home() / "pureiron" / "images"


def bash_executable() -> str:
    """PATH 기준 bash의 전체 경로.

    Windows의 CreateProcess는 PATH보다 System32를 먼저 찾아 `bash`가 WSL bash로 잡힌다(그러면
    Windows 경로의 git worktree를 읽지 못한다). 그래서 이름이 아니라 전체 경로로 실행한다.
    """
    found = shutil.which("bash")
    if found is None:
        raise ImageError("bash not found on PATH")
    return found
