"""성공한 적용 뒤 정리: 오래된 generation의 compose 정의·백업·이미지(M2b 이월).

보존 = 방금 적용한 generation과 직전 적용 generation(`from_generation`, 다음 시도가 실패하면 그
직전이 롤백 대상이고, 그보다 하나 더 앞은 수동 복구용). 나머지 `state/compose/g*`·`backups/g*`와
보존 정의가 참조하지 않는 `pureiron/*` 이미지 태그를 지운다.

정리 실패는 적용 결과에 영향을 주지 않는다(경고만 남기고 다음 성공 때 다시 시도). 시도가 진행
중일 때는 부르지 않는다(대기 중 시도의 적재 이미지를 지우지 않게).
"""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from pureiron_bundle.errors import BundleRejectedError
from pureiron_updater.compose import read_manifest, target_dir
from pureiron_updater.ports import CommandError, Docker

log = logging.getLogger(__name__)

IMAGE_PREFIX = "pureiron/"
_GEN_DIR = re.compile(r"g([0-9]+)")


def _generation_dirs(parent: Path) -> dict[int, Path]:
    if not parent.is_dir():
        return {}
    found: dict[int, Path] = {}
    for child in parent.iterdir():
        match = _GEN_DIR.fullmatch(child.name)
        if match is not None and child.is_dir() and not child.is_symlink():
            found[int(match[1])] = child
    return found


def collect(
    docker: Docker, compose_dir: Path, backups_dir: Path, keep: Iterable[int | None]
) -> list[str]:
    """`keep` 밖의 generation 산출물과 쓰지 않는 이미지 태그를 지운다. 지운 것 목록을 돌려준다."""
    kept = {gen for gen in keep if gen is not None}
    removed: list[str] = []
    for parent in (compose_dir, backups_dir):
        for gen, directory in sorted(_generation_dirs(parent).items()):
            if gen not in kept:
                shutil.rmtree(directory, ignore_errors=True)
                removed.append(str(directory))
    wanted: set[str] = set()
    for gen in sorted(kept):
        try:
            images = read_manifest(target_dir(compose_dir, gen)).product_release.images
        except (OSError, BundleRejectedError) as exc:
            # 보존 정의를 못 읽으면 어떤 이미지가 필요한지 모른다 — 이미지는 건드리지 않는다.
            log.warning("gc: cannot read g%s manifest, keeping all images: %s", gen, exc)
            return removed
        wanted.update(ref.tag for ref in images.values())
    try:
        tags = docker.tags(IMAGE_PREFIX)
    except CommandError as exc:
        log.warning("gc: cannot list images: %s", exc)
        return removed
    for tag in tags:
        if tag in wanted:
            continue
        try:
            docker.remove(tag)
            removed.append(tag)
        except CommandError as exc:
            log.warning("gc: cannot remove %s: %s", tag, exc)
    return removed
