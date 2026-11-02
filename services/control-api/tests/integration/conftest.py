"""control-db(PostgreSQL 16) 통합 테스트 픽스처.

- compose `deploy/compose/control.dev.yml`(control-db만)을 프로젝트 `<PUREIRON_IT_PROJECT>-control`
  (기본 `pureiron-it-control`), 호스트 포트 `PUREIRON_DEV_CONTROL_DB_PORT`(기본 25433)로 올린다.
- 테스트마다 새 데이터베이스 → `pureiron-control migrate up`(CLI 그대로) → `ControlRuntime`
  (시계 주입) + `TestClient`(lifespan 포함). 사람·CI token은 `pureiron-control token create`로만
  만든다.
- 경합 시험(`Central.concurrently`)은 같은 DB에 런타임을 하나 더 만들어 `httpx2.AsyncClient` +
  `ASGITransport`로 요청을 한 이벤트 루프에서 `asyncio.gather`한다(TestClient는 하나씩 보낸다).
- `server`: 같은 DB의 앱을 실제 HTTP(uvicorn, 스레드)로 띄운다 — 현장 Updater 코드(urllib)가 붙는
  시험용(`test_updater_*_db.py`).
- `PUREIRON_IT_KEEP=1`: 끝나도 compose를 내리지 않는다(디버깅).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Generator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import asyncpg
import httpx2 as httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from pureiron_control.app import create_app
from pureiron_control.cli import main as control_cli
from pureiron_control.runtime import ControlRuntime
from pureiron_control.settings import Settings

from .site_kit import T0

REPO = Path(__file__).resolve().parents[4]
COMPOSE_FILE = REPO / "deploy" / "compose" / "control.dev.yml"
PROJECT = os.environ.get("PUREIRON_IT_PROJECT", "pureiron-it") + "-control"
DB_PORT = int(os.environ.get("PUREIRON_DEV_CONTROL_DB_PORT", "25433"))


def compose(env: Mapping[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"docker compose {' '.join(args)} exited {result.returncode}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


@dataclass(frozen=True, slots=True)
class ControlDb:
    secrets_dir: Path
    compose_env: Mapping[str, str]

    @property
    def password_file(self) -> Path:
        return self.secrets_dir / "control_db_password"

    def dsn(self, database: str) -> str:
        return f"postgresql://pureiron@127.0.0.1:{DB_PORT}/{database}"

    def psql(self, sql: str) -> None:
        compose(
            self.compose_env,
            "exec",
            "-T",
            "control-db",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "pureiron",
            "-d",
            "postgres",
            "-c",
            sql,
        )


@pytest.fixture(scope="session")
def control_db(tmp_path_factory: pytest.TempPathFactory) -> Generator[ControlDb]:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    secrets_dir = tmp_path_factory.mktemp("control-secrets")
    (secrets_dir / "control_db_password").write_text(secrets.token_urlsafe(24), encoding="utf-8")
    env = {"PUREIRON_SECRETS_DIR": str(secrets_dir)}
    with contextlib.suppress(AssertionError):
        compose(env, "down", "-v")
    try:
        compose(env, "up", "-d", "--wait", "control-db")
        yield ControlDb(secrets_dir=secrets_dir, compose_env=env)
    finally:
        if os.environ.get("PUREIRON_IT_KEEP") != "1":
            with contextlib.suppress(AssertionError):
                compose(env, "down", "-v")


@dataclass(slots=True)
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass(slots=True)
class Central:
    """한 테스트의 중앙: 새 DB, 데이터 디렉터리, 시계, CLI 환경, HTTP client."""

    client: TestClient
    clock: Clock
    settings: Settings
    env: dict[str, str]
    tokens: dict[str, str] = field(default_factory=dict[str, str])

    def run(self, *args: str) -> tuple[int, str]:
        """`pureiron-control <args>`를 이 중앙의 환경으로 실행한다(종료 코드, stdout)."""
        out = io.StringIO()
        saved = dict(os.environ)
        os.environ.update(self.env)
        try:
            with contextlib.redirect_stdout(out):
                code = control_cli(list(args))
        finally:
            os.environ.clear()
            os.environ.update(saved)
        return code, out.getvalue()

    def cli(self, *args: str) -> dict[str, object]:
        code, out = self.run(*args, "--json")
        assert code == 0, f"pureiron-control {' '.join(args)} exited {code}: {out}"
        value: object = json.loads(out)
        assert isinstance(value, dict)
        return cast(dict[str, object], value)

    def api_token(self, role: str) -> str:
        if role not in self.tokens:
            created = self.cli("token", "create", "--role", role, "--name", f"it-{role}")
            token = created["token"]
            assert isinstance(token, str)
            self.tokens[role] = token
        return self.tokens[role]

    def auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def as_role(self, role: str) -> dict[str, str]:
        return self.auth(self.api_token(role))

    def fetch(self, sql: str, *args: object) -> list[asyncpg.Record]:
        """이 중앙의 control-db에 직접 질의한다(저장 형태 단언용)."""

        async def go() -> list[asyncpg.Record]:
            rt = ControlRuntime.build(self.settings, clock=self.clock)
            await rt.start()
            try:
                async with rt.db.connection() as conn:
                    return list(await conn.fetch(sql, *args))
            finally:
                await rt.stop()

        return asyncio.run(go())

    def concurrently(self, *requests: Request) -> list[httpx.Response]:
        """요청들을 같은 DB의 새 앱에 동시에 보낸다(응답은 요청 순서)."""

        async def go() -> list[httpx.Response]:
            rt = ControlRuntime.build(self.settings, clock=self.clock)
            await rt.start()  # ASGITransport는 lifespan을 돌리지 않는다
            try:
                transport = httpx.ASGITransport(app=create_app(rt))
                async with httpx.AsyncClient(transport=transport, base_url="http://control") as c:
                    return list(
                        await asyncio.gather(
                            *(
                                c.request(r.method, r.url, content=r.content, headers=r.headers)
                                for r in requests
                            )
                        )
                    )
            finally:
                await rt.stop()

        return asyncio.run(go())


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    url: str
    content: bytes
    headers: Mapping[str, str]


@pytest.fixture
def central(control_db: ControlDb, tmp_path: Path) -> Generator[Central]:
    database = f"it_{uuid.uuid4().hex[:12]}"
    control_db.psql(f'CREATE DATABASE "{database}"')
    env = {
        "PUREIRON_CONTROL_DB_DSN": control_db.dsn(database),
        "PUREIRON_CONTROL_DB_PASSWORD_FILE": str(control_db.password_file),
        "PUREIRON_CONTROL_DATA_DIR": str(tmp_path / "data"),
        "PUREIRON_RELEASE": os.environ.get("PUREIRON_RELEASE", "0.1.0-it"),
    }
    clock = Clock()
    settings = Settings.from_env(env)
    try:
        with TestClient(create_app(ControlRuntime.build(settings, clock=clock))) as client:
            central = Central(client=client, clock=clock, settings=settings, env=env)
            code, out = central.run("migrate", "up")
            assert code == 0, out
            yield central
    finally:
        control_db.psql(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


STARTUP_TIMEOUT_S = 15.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def server(central: Central) -> Generator[str]:
    port = _free_port()
    app = create_app(ControlRuntime.build(central.settings, clock=central.clock))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None)
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while not instance.started:
        assert time.monotonic() < deadline, "control-api server did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/api/v1"
    finally:
        instance.should_exit = True
        thread.join(timeout=STARTUP_TIMEOUT_S)
