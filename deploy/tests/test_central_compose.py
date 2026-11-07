"""중앙 compose·Caddyfile을 실제 도구로 검사한다(M4-3 §2-6, docker 필요 — integration-compose).

- `caddy validate`: control-web 이미지(= 운영 Caddy 버전)로 deploy/central/Caddyfile을 읽는다.
  비밀 파일 import·`{file.*}` placeholder까지 실제로 해석된다.
- `docker compose config`: 변수 보간·비밀 파일 경로가 맞는지(비밀 디렉터리 없으면 거부하는지).
이미지 태그는 `PUREIRON_RELEASE`(build_images.sh와 같은 값, 기본 0.1.0-dev).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

CENTRAL = Path(__file__).resolve().parents[1] / "central"
RELEASE = os.environ.get("PUREIRON_RELEASE", "0.1.0-dev")
IMAGE = f"pureiron/control-web:{RELEASE}"


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )


def _validate(caddyfile: Path, secrets_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            *("docker", "run", "--rm", "-e", "PUREIRON_CENTRAL_HOST=localhost, 192.0.2.10"),
            *("-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro", "-v", f"{secrets_dir}:/run/secrets:ro"),
            *(IMAGE, "caddy", "validate", "--config", "/etc/caddy/Caddyfile"),
            *("--adapter", "caddyfile"),
        ]
    )


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    hashed = _run(["docker", "run", "--rm", IMAGE, "caddy", "hash-password", "--plaintext", "pw"])
    assert hashed.returncode == 0, hashed.stderr
    for name, value in (
        ("control_db_password", "pw"),
        ("viewer_token", "tok"),
        ("ui_basic_auth", f"ops {hashed.stdout.strip()}"),
    ):
        (secrets / name).write_text(value + "\n", encoding="utf-8")
        (secrets / name).chmod(0o644)
    return secrets


def test_caddyfile_validates_with_the_shipped_caddy(secrets_dir: Path) -> None:
    result = _validate(CENTRAL / "Caddyfile", secrets_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Valid configuration" in result.stdout + result.stderr


def test_caddyfile_needs_the_basic_auth_secret(tmp_path: Path) -> None:
    """basic auth 비밀 파일이 없으면 Caddy는 뜨지 않는다(인증 없이 열리지 않는다)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _validate(CENTRAL / "Caddyfile", empty)
    assert result.returncode != 0
    assert "ui_basic_auth" in result.stdout + result.stderr


def test_compose_config_resolves(secrets_dir: Path) -> None:
    compose = str(CENTRAL / "compose.yml")
    base = ["docker", "compose", "-p", "pureiron-it-central-config", "-f", compose]
    result = _run(
        [*base, "config", "--quiet"],
        {"PUREIRON_CENTRAL_SECRETS_DIR": str(secrets_dir), "PUREIRON_CENTRAL_RELEASE": RELEASE},
    )
    assert result.returncode == 0, result.stderr
    missing = _run([*base, "config", "--quiet"], {"PUREIRON_CENTRAL_SECRETS_DIR": ""})
    assert missing.returncode != 0
    assert "PUREIRON_CENTRAL_SECRETS_DIR" in missing.stderr
