"""Updater가 바깥 세계(docker·compose·core·시계)를 부르는 경계(plan M2).

상태기계(`phases`)는 이 Protocol만 알고, 실제 구현(`images.DockerCli`·`compose.ComposeCli`·
`core.CoreHttp`)과 단위 테스트의 fake가 같은 표면을 구현한다. 모든 호출은 **멱등**이어야 한다 —
crash 뒤 `recover()`가 같은 단계를 다시 실행하기 때문이다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

COMPOSE_PROJECT = "pureiron"
LOCK_OWNER = "updater"


class CommandError(Exception):
    """docker·compose 명령 실패(종료 코드 ≠ 0, 시간 초과)."""


class CoreUnavailableError(Exception):
    """core HTTP에 닿지 않거나 5xx — 잠시 뒤 다시 시도할 수 있는 상태."""


class LockConflictError(Exception):
    """편집 잠금을 다른 owner·generation이 쥐고 있다(409)."""


@dataclass(frozen=True, slots=True)
class ComposeTarget:
    """generation 하나의 compose 정의: `<dir>/{compose.yml, .env, manifest.json}`."""

    directory: Path

    @property
    def compose_file(self) -> Path:
        return self.directory / "compose.yml"

    @property
    def env_file(self) -> Path:
        return self.directory / ".env"

    @property
    def manifest_file(self) -> Path:
        return self.directory / "manifest.json"


@dataclass(frozen=True, slots=True)
class Readiness:
    ready: bool
    """`/readyz` 200(DB + 스키마 envelope + collector 세션)."""
    collector: bool
    good_ratio: float | None = None
    """GOOD 품질 태그 비율(0~1). core가 아직 내보내지 않으면 None."""


class Docker(Protocol):
    def image_id(self, ref: str) -> str | None:
        """`docker image inspect -f '{{.Id}}' <ref>`. 없으면 None."""
        ...

    def load(self, archive: Path) -> None: ...

    def tag(self, source: str, target: str) -> None: ...


class Compose(Protocol):
    """`docker compose -p pureiron -f <target>/compose.yml --env-file <target>/.env …`."""

    def config_images(self, target: ComposeTarget) -> Mapping[str, str]:
        """활성 서비스 → 해석된 image 참조(`config --format json`)."""
        ...

    def up(self, target: ComposeTarget, services: Sequence[str] = ()) -> None:
        """`up -d --wait`(서비스 미지정이면 `--remove-orphans`로 전체)."""
        ...

    def running_images(self, target: ComposeTarget) -> Mapping[str, str]:
        """실행 중 컨테이너의 서비스 → image ID(`.Image`)."""
        ...

    def run_core(
        self,
        target: ComposeTarget,
        args: Sequence[str],
        *,
        mounts: Mapping[Path, str] | None = None,
    ) -> str:
        """`run --rm --no-deps -T core <args>`(이미지 ENTRYPOINT = pureiron-core). stdout 반환."""
        ...

    def dump_db(self, target: ComposeTarget, out: Path) -> None:
        """`exec -T site-db pg_dump -Fc …`(tag_history·Timescale 내부 데이터 제외) → `out`."""
        ...

    def down(self, target: ComposeTarget) -> None:
        """`down`(볼륨 유지)."""
        ...


class Core(Protocol):
    """core HTTP(`/api/v1`, web 프록시 경유). 잠금 owner는 항상 `updater`."""

    def acquire_lock(self, generation: int) -> int:
        """잠금 후 그 시점 site_rev. `LockConflictError`·`CoreUnavailableError`."""
        ...

    def release_lock(self) -> None: ...

    def project(self) -> tuple[int, int]:
        """(site_rev, base_site_rev)."""
        ...

    def readiness(self) -> Readiness: ...


class Clock(Protocol):
    def now(self) -> datetime:
        """aware datetime(UTC)."""
        ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...
