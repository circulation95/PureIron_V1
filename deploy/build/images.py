"""이미지 신원 도구: docker save/exporter tar에서 config digest를 읽는다(PD-19, ADR-023).

config digest는 이미지 설정 blob의 내용 주소라 save/load·image store 종류와 무관하게 보존된다.
config digest 읽기의 단일 구현은 bundle-tools(`pureiron_bundle.images`)에 있고 — 번들 검증과
같은 코드 — repro_build·release_record·번들 빌더는 이 모듈을 거쳐 그 함수를 쓴다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from pureiron_bundle.images import config_digest_from_tar

# 현장(core·collector·web·simulator·core-test) + 중앙(control-api·control-web, M4-3 G5). 중앙 이미지는
# release_record.py --central 기록에만 들어가고 현장 번들·manifest(SITE_IMAGES)에는 들어가지 않는다.
SERVICES = ("core", "collector", "web", "simulator", "core-test", "control-api", "control-web")


class ImageError(RuntimeError):
    pass


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
