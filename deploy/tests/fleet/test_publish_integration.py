"""publish 종단 통합(M4-2 §3).

control-db(PostgreSQL 16, compose) + 실제 control-api(uvicorn) + 실제 `build_bundle`(가짜 이미지
tar·임시 키, `test_build_bundle.Setup`) + 로컬 git 원격 + 가짜 GitHub 승인.

흐름: fleet 이력(템플릿 → PR head → merge commit) → `publish.run`이 `.pib`를 만들어 실제 `PUT`
→ `GET /fleet` max_published_gen 1 → 같은 sha 재실행은 건너뜀 → gen 2 PR 게시 → 옛 gen 번들을 다시
올리면 control-api가 본문 전에 409, 미등록 현장은 404(본문 전).

compose 프로젝트 `<PUREIRON_IT_PROJECT>-fleet`(기본 `pureiron-it-fleet`), 호스트 포트
`PUREIRON_DEV_CONTROL_DB_PORT`(기본 25433) — control-api 통합과 같은 compose 파일, 다른 프로젝트.
`scripts/g1.sh integration-compose`가 control-api 통합 다음에 따로 실행한다(동시 실행 아님).
"""

from __future__ import annotations

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
import urllib.request
import uuid
from collections.abc import Generator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import uvicorn
from fleet.fleetfiles import SiteStatus
from fleet.publish import (
    Chain,
    Config,
    ControlApi,
    ControlApiError,
    Deps,
    bundle_builder,
    run,
)

from pureiron_control.app import create_app
from pureiron_control.cli import main as control_cli
from pureiron_control.runtime import ControlRuntime
from pureiron_control.settings import Settings
from pureiron_updater.enroll import enroll
from pureiron_updater.settings import Settings as UpdaterSettings

from ..test_build_bundle import Setup
from .kit import APPROVERS, FakeGitHub, NoTokens, Repo, approve, git, pull, site_yaml

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILE = REPO / "deploy" / "compose" / "control.dev.yml"
PROJECT = os.environ.get("PUREIRON_IT_PROJECT", "pureiron-it") + "-fleet"
DB_PORT = int(os.environ.get("PUREIRON_DEV_CONTROL_DB_PORT", "25433"))
OWNER = "acme"
STARTUP_TIMEOUT_S = 15.0


def compose(env: Mapping[str, str], *args: str) -> None:
    result = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"docker compose {' '.join(args)}: {result.stdout}{result.stderr}")


@dataclass(frozen=True, slots=True)
class ControlDb:
    env: Mapping[str, str]
    password_file: Path


@pytest.fixture(scope="module")
def control_db(tmp_path_factory: pytest.TempPathFactory) -> Generator[ControlDb]:
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    secrets_dir = tmp_path_factory.mktemp("fleet-control-secrets")
    password = secrets_dir / "control_db_password"
    password.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    env = {"PUREIRON_SECRETS_DIR": str(secrets_dir)}
    with contextlib.suppress(AssertionError):
        compose(env, "down", "-v")
    try:
        compose(env, "up", "-d", "--wait", "control-db")
        yield ControlDb(env, password)
    finally:
        if os.environ.get("PUREIRON_IT_KEEP") != "1":
            with contextlib.suppress(AssertionError):
                compose(env, "down", "-v")


@dataclass(frozen=True, slots=True)
class Central:
    url: str
    env: Mapping[str, str]

    def run(self, *args: str) -> str:
        """`pureiron-control <args>`를 이 중앙 환경으로 실행(성공해야 함) → stdout."""
        out = io.StringIO()
        saved = dict(os.environ)
        os.environ.update(self.env)
        try:
            with contextlib.redirect_stdout(out):
                code = control_cli(list(args))
        finally:
            os.environ.clear()
            os.environ.update(saved)
        assert code == 0, out.getvalue()
        return out.getvalue()

    def cli(self, *args: str) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.run(*args, "--json")))

    def token(self, role: str) -> str:
        return str(self.cli("token", "create", "--role", role, "--name", f"it-{role}")["token"])

    def request(self, method: str, path: str, token: str, body: bytes | None = None) -> object:
        request = urllib.request.Request(f"{self.url}{path}", data=body, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def central(control_db: ControlDb, tmp_path: Path) -> Generator[Central]:
    database = f"it_fleet_{uuid.uuid4().hex[:12]}"
    psql = ("exec", "-T", "control-db", "psql", "-v", "ON_ERROR_STOP=1", "-U", "pureiron")
    compose(control_db.env, *psql, "-d", "postgres", "-c", f'CREATE DATABASE "{database}"')
    env = {
        "PUREIRON_CONTROL_DB_DSN": f"postgresql://pureiron@127.0.0.1:{DB_PORT}/{database}",
        "PUREIRON_CONTROL_DB_PASSWORD_FILE": str(control_db.password_file),
        "PUREIRON_CONTROL_DATA_DIR": str(tmp_path / "control-data"),
        "PUREIRON_RELEASE": "0.1.0-it",
    }
    central = Central(url="", env=env)
    central.run("migrate", "up")
    port = _free_port()
    app = create_app(ControlRuntime.build(Settings.from_env(env)))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while not server.started:
        assert time.monotonic() < deadline, "control-api server did not start"
        time.sleep(0.05)
    try:
        yield Central(url=f"http://127.0.0.1:{port}/api/v1", env=env)
    finally:
        server.should_exit = True
        thread.join(timeout=STARTUP_TIMEOUT_S)
        compose(
            control_db.env,
            *psql,
            "-d",
            "postgres",
            "-c",
            f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)',
        )


def register_site(central: Central, tmp: Path, site: str, admin: str) -> None:
    root = tmp / f"updater-{site}"
    settings = UpdaterSettings(
        site_id=site,
        root=root,
        root_pubkey_file=root / "root.pub",
        core_url="http://127.0.0.1:9/api/v1",
        env_file=root / "updater.env",
    )
    enrollment = enroll(settings, site, now=datetime(2026, 11, 1, tzinfo=UTC)).read_bytes()
    central.request("POST", "/sites", admin, enrollment)


def merge_pr(fleet: Repo, github: FakeGitHub, number: int, files: dict[str, str | None]) -> str:
    fleet.branch(f"pr-{number}", "main")
    head = fleet.commit(files, f"pr {number}")
    fleet.switch("main")
    merged = fleet.merge(f"pr-{number}")
    github.add(
        pull(number, merge_commit_sha=merged, head_sha=head),
        [approve("circulation95", head)],
        merged,
    )
    return merged


def test_publish_end_to_end_against_real_control_api(
    central: Central, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "build").mkdir()
    kit = Setup(tmp_path / "build")  # release 기록 2개·가짜 이미지 store·release key·cert·root.pub
    admin, ci, viewer = central.token("admin"), central.token("ci"), central.token("viewer")
    register_site(central, tmp_path, "site-a", admin)

    remotes = tmp_path / "remotes"
    fleet = Repo.create(remotes / OWNER / "fleet")
    base = fleet.commit(
        {"allowed_approvers.txt": APPROVERS, "sites/.retired/.gitkeep": ""}, "template"
    )
    project = Repo.create(remotes / OWNER / "pureiron-project-acme")
    shutil.copytree(kit.project, project.path, dirs_exist_ok=True)
    git(project.path, "add", "-A")
    project.commit({}, "acme")
    project.tag("v3")

    config = Config(
        fleet_repo=f"{OWNER}/fleet",
        control_api=central.url,
        keys_dir=kit.keys,
        git_base=str(remotes),
        state_dir=tmp_path / "state",
        bundles_dir=tmp_path / "bundles",
        metrics_dir=tmp_path / "metrics",
        releases=kit.releases,
        store=kit.store,
    )
    Chain.of(config).record(base, "trust-base(it)")
    control = ControlApi(central.url, viewer_token=viewer, ci_token=ci)
    github = FakeGitHub()
    deps = Deps(github, control, NoTokens(), bundle_builder(config))

    # gen 1: 3 commit 이력(템플릿 → PR head → merge commit)
    merged = merge_pr(fleet, github, 7, {"sites/site-a.yaml": site_yaml("site-a", 1)})
    assert run(config, merged, deps) == 0, capsys.readouterr().out
    pib = config.bundles_dir / "site-a" / "pureiron-site-a-g1.pib"
    assert pib.is_file()
    assert control.site_status("site-a") == SiteStatus(max_published_gen=1, current_product=None)
    assert (config.metrics_dir / "publish-site-a-g1.json").is_file()
    rows = cast(list[dict[str, object]], central.request("GET", "/fleet?site=site-a", viewer))
    desired = cast(dict[str, object], rows[0]["desired"])
    assert (desired["generation"], desired["product_version"], desired["project_version"]) == (
        1,
        "2026.11.0",
        "v3",
    )

    # 같은 sha 재실행: 이미 게시 → 건너뜀(빌드·PUT 없음)
    capsys.readouterr()
    pib.unlink()
    assert run(config, merged, deps) == 0
    assert "이미 게시됨 — 건너뜀" in capsys.readouterr().out
    assert not pib.exists()

    # gen 2 PR → 게시, max_published_gen 증가
    merged2 = merge_pr(fleet, github, 8, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert run(config, merged2, deps) == 0, capsys.readouterr().out
    assert control.site_status("site-a") == SiteStatus(max_published_gen=2, current_product=None)

    # gen 역행: 옛 gen 번들을 다시 올리면 본문 전에 409(실제 control-api)
    g2 = config.bundles_dir / "site-a" / "pureiron-site-a-g2.pib"
    with pytest.raises(ControlApiError) as conflict:
        control.put_bundle("site-a", 2, g2)
    assert conflict.value.status == 409
    # 미등록 현장: 본문 전 404
    with pytest.raises(ControlApiError) as missing:
        control.put_bundle("site-z", 1, g2)
    assert missing.value.status == 404
    assert control.site_status("site-z") is None
