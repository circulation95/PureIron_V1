"""site-db(TimescaleDB) 통합 테스트 픽스처.

실행 모드(환경변수):
- 기본: compose(`deploy/compose/site.compose.yml` + `site.dev.yml`, 프로젝트 `pureiron-it`)를 올리고
  테스트마다 새 데이터베이스를 만들어 현재 카탈로그로 migrate 한다.
- `PUREIRON_TEST_EXISTING_DSN`: 이미 migrate된 DB를 그대로 쓴다(n-minus-1: N-1 앱 테스트를
  N 스키마에서 실행). 새 DB가 필요한 테스트(`fresh_db`)와 compose 테스트는 건너뛴다.
- `PUREIRON_IT_KEEP=1`: 끝나도 compose를 내리지 않는다(디버깅).
- `PUREIRON_IT_PROJECT`·`PUREIRON_DEV_DB_PORT`·`PUREIRON_DEV_CORE_PORT`·`PUREIRON_IT_WEB_PORT`:
  compose 프로젝트 이름·호스트 포트(기본 `pureiron-it`·25432·18000·18080).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncGenerator, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest

from pureiron_core.platform import migrate

type JsonObject = dict[str, object]

REPO = Path(__file__).resolve().parents[4]
COMPOSE_DIR = REPO / "deploy" / "compose"
MIGRATIONS = REPO / "services" / "core" / "migrations"
# 같은 Docker 데몬을 여러 작업 트리가 함께 쓸 때 스택이 서로 내리지 않게 이름·포트를 바꿀 수 있다
# (이미지 태그는 PUREIRON_RELEASE).
PROJECT = os.environ.get("PUREIRON_IT_PROJECT", "pureiron-it")
DB_PORT = int(os.environ.get("PUREIRON_DEV_DB_PORT", "25432"))
CORE_PORT = int(os.environ.get("PUREIRON_DEV_CORE_PORT", "18000"))
WEB_PORT = int(os.environ.get("PUREIRON_IT_WEB_PORT", "18080"))
EXISTING_DSN = os.environ.get("PUREIRON_TEST_EXISTING_DSN")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if EXISTING_DSN and ("fresh_db" in item.keywords or "compose" in item.keywords):
            item.add_marker(pytest.mark.skip(reason="PUREIRON_TEST_EXISTING_DSN mode"))


@dataclass(frozen=True, slots=True)
class Stack:
    secrets_dir: Path
    password: str
    updater_token: str
    compose_env: Mapping[str, str]

    def dsn(self, database: str) -> str:
        return f"postgresql://pureiron@127.0.0.1:{DB_PORT}/{database}"

    def cli_env(self, database: str, migrations_dir: Path = MIGRATIONS) -> dict[str, str]:
        return {
            **os.environ,
            "PUREIRON_DB_DSN": self.dsn(database),
            "PUREIRON_DB_PASSWORD_FILE": str(self.secrets_dir / "db_password"),
            "PUREIRON_MIGRATIONS_DIR": str(migrations_dir),
            "PYTHONUTF8": "1",
        }


def compose(
    env: Mapping[str, str], *args: str, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(COMPOSE_DIR / "site.compose.yml"),
        "-f",
        str(COMPOSE_DIR / "site.dev.yml"),
        "--profile",
        "dev",
        *args,
    ]
    result = subprocess.run(
        cmd,
        env={**os.environ, **env},
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check and result.returncode != 0:
        # CalledProcessError는 출력을 보여주지 않아 CI 로그에서 원인(포트 충돌 등)이 사라진다.
        raise AssertionError(
            f"docker compose {' '.join(args)} exited {result.returncode}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result


def wait_http(url: str, *, status: int = 200, timeout_s: float = 120.0) -> bytes:
    deadline = time.monotonic() + timeout_s
    last: str = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                body: bytes = response.read()
                if response.status == status:
                    return body
                last = f"status {response.status}"
        except urllib.error.HTTPError as exc:
            last = f"status {exc.code}: {exc.read()[:300]!r}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last = str(exc)
        time.sleep(1)
    raise AssertionError(f"{url} did not return {status} within {timeout_s}s (last: {last})")


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Generator[Stack]:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    secrets_dir = tmp_path_factory.mktemp("secrets")
    password = secrets.token_urlsafe(24)
    token = secrets.token_urlsafe(32)
    (secrets_dir / "db_password").write_text(password, encoding="utf-8")
    (secrets_dir / "updater_token").write_text(token, encoding="utf-8")
    env = {
        "PUREIRON_SECRETS_DIR": str(secrets_dir),
        "PUREIRON_DB_DATA": "pureiron-db",
        "PUREIRON_WEB_BIND": "127.0.0.1",
        "PUREIRON_WEB_PORT": str(WEB_PORT),
    }
    compose(env, "down", "-v", check=False)
    try:
        compose(env, "up", "-d", "--wait", "site-db")
        migrated = compose(env, "run", "--rm", "--no-deps", "core", "migrate", "up", check=False)
        assert migrated.returncode == 0, migrated.stdout + migrated.stderr
        compose(env, "up", "-d")
        yield Stack(
            secrets_dir=secrets_dir, password=password, updater_token=token, compose_env=env
        )
    finally:
        if os.environ.get("PUREIRON_IT_KEEP") != "1":
            logs = compose(env, "logs", "--no-color", "--tail", "200", check=False)
            (REPO / "artifacts").mkdir(exist_ok=True)
            (REPO / "artifacts" / "integration-compose.log").write_text(
                logs.stdout + logs.stderr, encoding="utf-8"
            )
            compose(env, "down", "-v", check=False)


@pytest.fixture
def fresh_database(stack: Stack) -> Generator[str]:
    """빈 데이터베이스 이름(테스트 끝에 삭제)."""
    name = f"it_{uuid.uuid4().hex[:12]}"
    _admin_execute(stack, f'CREATE DATABASE "{name}"')
    try:
        yield name
    finally:
        _admin_execute(stack, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _admin_execute(stack: Stack, sql: str) -> None:
    """이벤트 루프와 무관하게(동기·비동기 테스트 모두에서) site-db 컨테이너의 psql로 실행한다."""
    result = compose(
        stack.compose_env,
        "exec",
        "-T",
        "site-db",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "pureiron",
        "-d",
        "postgres",
        "-c",
        sql,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@dataclass(frozen=True, slots=True)
class SiteDb:
    dsn: str
    password: str | None


@pytest.fixture
async def site_db(request: pytest.FixtureRequest) -> AsyncGenerator[SiteDb]:
    """현재 카탈로그로 migrate된 DB. EXISTING 모드에서는 주어진 DB를 그대로 쓴다."""
    if EXISTING_DSN:
        yield SiteDb(dsn=EXISTING_DSN, password=None)
        return
    stack: Stack = request.getfixturevalue("stack")
    database: str = request.getfixturevalue("fresh_database")
    db = SiteDb(dsn=stack.dsn(database), password=stack.password)
    conn = await asyncpg.connect(db.dsn, password=db.password)
    try:
        await migrate.up(conn, migrate.load_catalog(MIGRATIONS), release="it")
    finally:
        await conn.close()
    yield db


def write_project(
    root: Path,
    devices: Mapping[str, JsonObject],
    tags: Mapping[str, list[JsonObject]],
    *,
    historian: JsonObject | None = None,
) -> Path:
    """고객 프로젝트 트리를 만든다(JSON은 YAML이기도 하다 — load_project가 그대로 읽는다).

    core-test 이미지에는 project-tools fixture가 없으므로 테스트가 트리를 직접 쓴다.
    """
    if root.exists():
        shutil.rmtree(root)
    (root / "devices").mkdir(parents=True)
    (root / "tags").mkdir()
    project: JsonObject = {"schema_version": 1, "project_id": "it", "name": "integration"}
    if historian is not None:
        project["historian"] = historian
    (root / "project.yaml").write_text(json.dumps(project), encoding="utf-8")
    for device_id, device in devices.items():
        (root / "devices" / f"{device_id}.yaml").write_text(json.dumps(device), encoding="utf-8")
    for device_id, items in tags.items():
        (root / "tags" / f"{device_id}.yaml").write_text(json.dumps(items), encoding="utf-8")
    return root


def modbus_device(device_id: str, host: str, port: int = 502) -> JsonObject:
    return {"id": device_id, "protocol": "modbus_tcp", "host": host, "port": port}


def holding_tag(path: str, device_id: str, address: int, data_type: str = "uint16") -> JsonObject:
    return {
        "path": path,
        "device": device_id,
        "modbus": {"register_type": "holding", "address": address, "data_type": data_type},
    }


def core_cli(
    env: Mapping[str, str], *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """호스트 venv의 `pureiron-core` CLI(dev compose의 site-db 포트로 접속)."""
    exe = shutil.which("pureiron-core")
    assert exe is not None
    return subprocess.run(
        [exe, *args],
        env=dict(env),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
