"""이미지 적재·신원 확인(PD-19): 신원 = config digest = classic store의 `.Id`.

- 이미 같은 `.Id`가 태그에 붙어 있으면 아무것도 하지 않는다.
- 같은 ID가 다른 태그로 있으면 `docker tag <id> <tag>`만 한다(load 생략, PD-6).
- 없으면 **검증된 추출본**(`.pib` 재오픈 금지)에서 `docker load`.
- 그 뒤 `.Id == config_digest`를 `check_loaded_image`로 확인한다. Updater는 load 직후와
  `up -d` 직전(`pin_images`)에 이 확인을 반복한다.

containerd image store에서는 `.Id`가 manifest digest라 config digest와 달라 `E_DIGEST`로 멈춘다 —
현장은 `daemon.json`으로 classic store를 고정해야 한다(PD-19).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path

from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.images import check_loaded_image
from pureiron_bundle.manifest import ImageRef
from pureiron_updater.ports import CommandError, Docker
from pureiron_updater.proc import Result, Runner, check, subprocess_runner


class DockerCli:
    def __init__(self, runner: Runner = subprocess_runner, docker: str = "docker") -> None:
        self._run = runner
        self._docker = docker

    def image_id(self, ref: str) -> str | None:
        argv = [self._docker, "image", "inspect", "--format", "{{.Id}}", ref]
        result: Result = self._run(argv, timeout=60)
        if result.returncode != 0:
            if "No such image" in result.stderr or "not found" in result.stderr.lower():
                return None
            check(result, argv)
        return result.stdout.strip() or None

    def load(self, archive: Path) -> None:
        argv = [self._docker, "load", "--input", str(archive)]
        check(self._run(argv, timeout=1800), argv)

    def tag(self, source: str, target: str) -> None:
        argv = [self._docker, "tag", source, target]
        check(self._run(argv, timeout=60), argv)

    def available(self) -> bool:
        argv = [self._docker, "version", "--format", "{{.Server.Version}}"]
        try:
            return self._run(argv, timeout=30).returncode == 0
        except CommandError:
            return False


def ensure_image(docker: Docker, service: str, ref: ImageRef, archive: Path | None) -> bool:
    """`ref.tag`가 `ref.config_digest`를 가리키게 한다. load 했으면 True."""
    if docker.image_id(ref.tag) == ref.config_digest:
        return False
    loaded = False
    if docker.image_id(ref.config_digest) is None:
        if archive is None:
            raise BundleRejectedError(
                RejectCode.E_DIGEST, f"{service}: image {ref.config_digest} is not loaded"
            )
        docker.load(archive)
        loaded = True
    if docker.image_id(ref.tag) != ref.config_digest and docker.image_id(ref.config_digest):
        docker.tag(ref.config_digest, ref.tag)
    check_loaded_image(service, docker.image_id(ref.tag) or "<missing>", ref.config_digest)
    return loaded


def load_images(
    docker: Docker, images: Mapping[str, ImageRef], archives: Mapping[str, Path]
) -> float:
    """번들 이미지 전부 적재·확인. 걸린 시간(load_s)을 돌려준다."""
    started = time.monotonic()
    for service in sorted(images):
        ensure_image(docker, service, images[service], archives.get(service))
    return time.monotonic() - started


def pin_images(docker: Docker, images: Mapping[str, ImageRef]) -> None:
    """`up -d` 직전 재확인: 태그가 다른 이미지로 옮겨졌으면 ID로 되돌리고 `.Id`를 assert."""
    for service in sorted(images):
        ensure_image(docker, service, images[service], None)


def check_running(running: Mapping[str, str], images: Mapping[str, ImageRef]) -> None:
    """실행 중 컨테이너의 image ID가 manifest config digest와 같은지(서비스 누락도 거부)."""
    for service in sorted(images):
        check_loaded_image(
            f"{service} (running)",
            running.get(service, "<not running>"),
            images[service].config_digest,
        )
