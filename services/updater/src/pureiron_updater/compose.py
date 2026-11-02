"""generation별 compose 정의와 `docker compose -p pureiron` 호출.

`state/compose/g<gen>/`에 둔다:
- `compose.yml` = 번들의 서명된 `compose/site.compose.yml` 사본
- `.env` = manifest 이미지 태그에서 만든 `PUREIRON_RELEASE`·`PUREIRON_SITE_DB_VERSION` + 현장 경로
  + 쓰기 스위치 `PUREIRON_WRITES_ENABLED`(manifest `site_config.writes_enabled`, 없으면 `false` —
  늘 쓴다, M4-1 C2)
- `manifest.json` = 검증된 manifest(JCS) — 롤백·상태 보고가 이 파일에서 이미지 digest를 읽는다

적용 전에 `docker compose config`가 해석한 활성 서비스·이미지가 manifest 태그와 **정확히** 같아야
한다(`check_config`). compose 파일과 manifest가 어긋난 번들은 서비스를 건드리기 전에 거부한다.

쓰기 스위치는 서명된 manifest에서만 온다: docker compose 보간은 셸 환경이 `--env-file`보다
우선하므로 compose 호출 환경에서 `PUREIRON_WRITES_ENABLED`를 지운다(`compose_env`) —
`updater.env`(systemd `EnvironmentFile`)나 운영자 셸에 남은 값이 `.env`를 덮지 못하게.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.manifest import Manifest, manifest_bytes, parse_manifest
from pureiron_updater.ports import COMPOSE_PROJECT, ComposeTarget
from pureiron_updater.proc import Runner, check, subprocess_runner
from pureiron_updater.state import atomic_write

COMPOSE_PAYLOAD = "compose/site.compose.yml"
WRITES_VAR = "PUREIRON_WRITES_ENABLED"
"""core 쓰기 스위치 — generation `.env`에만 있고 compose 호출 환경에서는 지운다."""
PG_DUMP_EXCLUDE = ("tag_history", "_timescaledb_internal.*")
WAIT_TIMEOUT_S = 180
UNLOCK_SQL = (
    "UPDATE project_state SET edit_lock_owner = NULL, edit_lock_gen = NULL, updated_at = now() "
    "WHERE id AND edit_lock_owner = 'updater'"
)


def target_dir(compose_root: Path, generation: int) -> ComposeTarget:
    return ComposeTarget(compose_root / f"g{generation}")


def _tag_version(tag: str) -> str:
    return tag.rsplit(":", 1)[1] if ":" in tag.rsplit("/", 1)[-1] else ""


def env_lines(manifest: Manifest, site_env: Mapping[str, str]) -> list[str]:
    images = manifest.product_release.images
    values = {
        "PUREIRON_RELEASE": _tag_version(images["core"].tag),
        "PUREIRON_SITE_DB_VERSION": _tag_version(images["site-db"].tag),
        **site_env,
        WRITES_VAR: "true" if manifest.site_config.writes_enabled else "false",
    }
    return [f"{key}={value}" for key, value in sorted(values.items())]


def compose_env(environ: Mapping[str, str]) -> dict[str, str]:
    """docker compose에 물려줄 환경 = 프로세스 환경에서 쓰기 스위치를 뺀 것."""
    return {key: value for key, value in environ.items() if key != WRITES_VAR}


def write_target(
    target: ComposeTarget, manifest: Manifest, compose_source: Path, site_env: Mapping[str, str]
) -> None:
    """target 디렉터리를 (다시) 쓴다. 같은 입력이면 같은 내용 — recover 재실행에 안전."""
    if not compose_source.is_file():
        raise BundleRejectedError(RejectCode.E_FORMAT, f"bundle has no {COMPOSE_PAYLOAD}")
    target.directory.mkdir(parents=True, exist_ok=True)
    atomic_write(target.compose_file, compose_source.read_bytes())
    atomic_write(target.env_file, ("\n".join(env_lines(manifest, site_env)) + "\n").encode())
    atomic_write(target.manifest_file, manifest_bytes(manifest.document))


def read_manifest(target: ComposeTarget) -> Manifest:
    return parse_manifest(target.manifest_file.read_bytes())


def check_config(resolved: Mapping[str, str], manifest: Manifest) -> None:
    """compose가 띄울 서비스·이미지 == manifest 이미지 태그(`E_DIGEST`)."""
    expected = {svc: ref.tag for svc, ref in manifest.product_release.images.items()}
    if dict(resolved) != expected:
        raise BundleRejectedError(
            RejectCode.E_DIGEST,
            f"compose services/images {dict(sorted(resolved.items()))} "
            f"!= manifest {dict(sorted(expected.items()))}",
        )


def remove_target(target: ComposeTarget) -> None:
    shutil.rmtree(target.directory, ignore_errors=True)


class ComposeCli:
    def __init__(self, runner: Runner = subprocess_runner, docker: str = "docker") -> None:
        self._run = runner
        self._docker = docker

    def _base(self, target: ComposeTarget) -> list[str]:
        return [
            self._docker,
            "compose",
            "-p",
            COMPOSE_PROJECT,
            "-f",
            str(target.compose_file),
            "--env-file",
            str(target.env_file),
        ]

    def _check(self, argv: list[str], timeout: float, stdout: Path | None = None) -> str:
        env = compose_env(os.environ)
        return check(self._run(argv, timeout=timeout, stdout=stdout, env=env), argv)

    def config_images(self, target: ComposeTarget) -> Mapping[str, str]:
        out = self._check([*self._base(target), "config", "--format", "json"], 120)
        data = cast(dict[str, object], json.loads(out))
        services = cast(dict[str, dict[str, object]], data.get("services", {}))
        return {name: str(svc.get("image", "")) for name, svc in services.items()}

    def up(self, target: ComposeTarget, services: Sequence[str] = ()) -> None:
        argv = [*self._base(target), "up", "-d", "--wait", "--wait-timeout", str(WAIT_TIMEOUT_S)]
        argv += list(services) if services else ["--remove-orphans"]
        self._check(argv, 600)

    def running_images(self, target: ComposeTarget) -> Mapping[str, str]:
        ids = self._check([*self._base(target), "ps", "-q"], 60).split()
        if not ids:
            return {}
        fmt = '{{index .Config.Labels "com.docker.compose.service"}} {{.Image}}'
        out = self._check([self._docker, "inspect", "--format", fmt, *ids], 60)
        running: dict[str, str] = {}
        for line in out.splitlines():
            service, _, image = line.strip().partition(" ")
            if service:
                running[service] = image
        return running

    def run_core(
        self,
        target: ComposeTarget,
        args: Sequence[str],
        *,
        mounts: Mapping[Path, str] | None = None,
        writable: Mapping[Path, str] | None = None,
    ) -> str:
        argv = [*self._base(target), "run", "--rm", "--no-deps", "-T"]
        for host, container in sorted((mounts or {}).items()):
            argv += ["--volume", f"{host}:{container}:ro"]
        for host, container in sorted((writable or {}).items()):
            argv += ["--volume", f"{host}:{container}"]
        return self._check([*argv, "core", *args], 1800)

    def dump_db(self, target: ComposeTarget, out: Path) -> None:
        argv = [*self._base(target), "exec", "-T", "site-db", "pg_dump", "-U", "pureiron"]
        argv += ["-d", "pureiron", "-Fc"]
        argv += [f"--exclude-table-data={pattern}" for pattern in PG_DUMP_EXCLUDE]
        self._check(argv, 3600, stdout=out)

    def down(self, target: ComposeTarget) -> None:
        self._check([*self._base(target), "down"], 300)

    def release_lock_db(self, target: ComposeTarget) -> None:
        argv = [*self._base(target), "exec", "-T", "site-db", "psql", "-U", "pureiron"]
        argv += ["-d", "pureiron", "-v", "ON_ERROR_STOP=1", "-c", UNLOCK_SQL]
        self._check(argv, 60)
