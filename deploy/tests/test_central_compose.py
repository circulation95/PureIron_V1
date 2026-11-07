"""중앙 compose·Caddyfile을 실제 도구로 검사한다(M4-3 §2-6, docker 필요 — integration-compose).

- `caddy validate`: control-web 이미지(= 운영 Caddy 버전)로 deploy/central/Caddyfile을 읽는다.
  비밀 파일 import·`{file.*}` placeholder까지 실제로 해석된다.
- `docker compose config`: 변수 보간·비밀 파일 경로가 맞는지(비밀 디렉터리 없으면 거부하는지).
- 실제 기동(`_caddy`): 저장소 Caddyfile을 그대로 띄워 SNI 없는 TLS(IP 접속, 리뷰 M1)·맞지 않는
  Host(421, N4)·빈 basic auth 파일(모두 401, L5)을 본다. upstream(control-api)은 띄우지 않는다 —
  TLS·인증·라우팅 앞단만 본다. 호스트 포트는 docker가 고른다(다른 게이트·리허설 포트와 안 겹치게).
이미지 태그는 `PUREIRON_RELEASE`(build_images.sh와 같은 값, 기본 0.1.0-dev).
"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

CENTRAL = Path(__file__).resolve().parents[1] / "central"
RELEASE = os.environ.get("PUREIRON_RELEASE", "0.1.0-dev")
IMAGE = f"pureiron/control-web:{RELEASE}"
# 문서용 주소(TEST-NET-1). 실제로는 127.0.0.1로 붙고, IP 접속처럼 SNI를 보내지 않는다.
IP = "192.0.2.10"
SECURITY_HEADERS = {
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
    "referrer-policy",
}
READY_TIMEOUT_S = 30.0


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
            *("docker", "run", "--rm", "-e", f"PUREIRON_CENTRAL_HOST=localhost, {IP}"),
            *("-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro", "-v", f"{secrets_dir}:/run/secrets:ro"),
            *(IMAGE, "caddy", "validate", "--config", "/etc/caddy/Caddyfile"),
            *("--adapter", "caddyfile"),
        ]
    )


def _write_secrets(secrets: Path, basic_auth: str) -> Path:
    secrets.mkdir()
    for name, value in (
        ("control_db_password", "pw\n"),
        ("viewer_token", "tok\n"),
        ("ui_basic_auth", basic_auth),
    ):
        (secrets / name).write_text(value, encoding="utf-8")
        (secrets / name).chmod(0o644)
    return secrets


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    hashed = _run(["docker", "run", "--rm", IMAGE, "caddy", "hash-password", "--plaintext", "pw"])
    assert hashed.returncode == 0, hashed.stderr
    return _write_secrets(tmp_path / "secrets", f"ops {hashed.stdout.strip()}\n")


class _Caddy:
    """저장소 Caddyfile로 띄운 control-web 컨테이너 하나(127.0.0.1의 임시 포트)."""

    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port

    def tls(self, server_hostname: str | None) -> ssl.SSLSocket:
        """server_hostname=None이면 SNI를 보내지 않는다(IP 주소로 접속하는 클라이언트와 같다)."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        return context.wrap_socket(raw, server_hostname=server_hostname)

    def get(self, path: str, host: str, server_hostname: str | None) -> tuple[int, dict[str, str]]:
        """HTTP/1.1 GET 한 번 → (상태, 소문자 헤더)."""
        request = f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
        data = b""
        with self.tls(server_hostname) as conn:
            conn.sendall(request.encode())
            while chunk := conn.recv(65536):
                data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        pairs = (line.split(": ", 1) for line in head[1:] if ": " in line)
        return int(head[0].split()[1]), {key.lower(): value for key, value in pairs}

    def logs(self) -> str:
        result = _run(["docker", "logs", self.name])
        return result.stdout + result.stderr


@contextmanager
def _caddy(secrets: Path, env: dict[str, str]) -> Iterator[_Caddy]:
    name = f"pureiron-it-central-caddy-{uuid.uuid4().hex[:8]}"
    env_args = [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]
    started = _run(
        [
            *("docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8443", *env_args),
            *("-v", f"{CENTRAL / 'Caddyfile'}:/etc/caddy/Caddyfile:ro"),
            *("-v", f"{secrets}:/run/secrets:ro", IMAGE),
        ]
    )
    try:
        assert started.returncode == 0, started.stderr
        port = _run(["docker", "port", name, "8443/tcp"]).stdout.splitlines()[0]
        caddy = _Caddy(name, int(port.rsplit(":", 1)[1]))
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            try:
                caddy.get("/", "localhost", "localhost")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise AssertionError(f"caddy not serving\n{caddy.logs()}") from None
                time.sleep(0.5)
        yield caddy
    finally:
        _run(["docker", "rm", "-f", name])


@pytest.fixture
def ip_caddy(secrets_dir: Path) -> Iterator[_Caddy]:
    """IP 운영 설정(.env.example 안내 그대로): HOST에 IP를 넣고 DEFAULT_SNI = 그 IP."""
    env = {"PUREIRON_CENTRAL_HOST": f"localhost, {IP}", "PUREIRON_CENTRAL_DEFAULT_SNI": IP}
    with _caddy(secrets_dir, env) as caddy:
        yield caddy


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


def test_ip_clients_without_sni_complete_the_handshake(ip_caddy: _Caddy) -> None:
    """리뷰 M1: IP로 접속하는 클라이언트(SNI 없음)도 DEFAULT_SNI 인증서로 핸드셰이크하고 라우팅된다.

    default_sni가 없으면 Caddy는 컨테이너 IP로 인증서를 찾다가 TLS alert 80으로 끊는다(실측).
    """
    with ip_caddy.tls(server_hostname=None) as conn:
        assert conn.getpeercert(binary_form=True)
    status, headers = ip_caddy.get("/ui/fleet", IP, server_hostname=None)
    assert status == 401
    assert headers["www-authenticate"].startswith("Basic")
    assert set(headers) >= SECURITY_HEADERS


def test_other_hosts_get_421_with_headers(ip_caddy: _Caddy) -> None:
    """리뷰 N4: 설정에 없는 Host는 빈 200이 아니라 421 + 보안 헤더."""
    status, headers = ip_caddy.get("/", "evil.example", server_hostname="localhost")
    assert status == 421
    assert set(headers) >= SECURITY_HEADERS


def test_empty_basic_auth_file_starts_but_rejects_everyone(tmp_path: Path) -> None:
    """리뷰 L5: 빈 ui_basic_auth는 파일 없음과 달리 Caddy가 경고만 남기고 뜬다 — UI는 모두 401."""
    secrets = _write_secrets(tmp_path / "secrets", "")
    with _caddy(secrets, {}) as caddy:
        for path in ("/", "/index.html", "/ui/fleet"):
            status, headers = caddy.get(path, "localhost", "localhost")
            assert status == 401, path
            assert headers["www-authenticate"].startswith("Basic"), path
        assert "import file is empty" in caddy.logs().lower()
