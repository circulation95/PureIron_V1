"""쓰기 스위치(M4-1 C2·E10): 서명된 manifest `site_config.writes_enabled` → generation `.env`의
`PUREIRON_WRITES_ENABLED`(늘 기록), compose 호출 환경에서 같은 키 제거, `updater.env` 키는
무시+경고."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from updater_fakes import FakeRunner, Kit, Site

from pureiron_updater import cli
from pureiron_updater.compose import ComposeCli, compose_env
from pureiron_updater.images import DockerCli
from pureiron_updater.ports import ComposeTarget
from pureiron_updater.proc import subprocess_runner
from pureiron_updater.settings import Settings


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))


def _env_file(site: Site, generation: int) -> dict[str, str]:
    text = (site.settings.compose_dir / f"g{generation}" / ".env").read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines())


@pytest.mark.parametrize(
    ("writes", "expected"), [(None, "false"), (False, "false"), (True, "true")]
)
def test_generation_env_always_carries_the_signed_switch(
    site: Site, writes: bool | None, expected: str
) -> None:
    state = site.apply(site.kit.bundle(1, "r0", writes=writes))
    assert state.applied_gen == 1
    env = _env_file(site, 1)
    assert env["PUREIRON_WRITES_ENABLED"] == expected
    assert env["PUREIRON_RELEASE"]  # 나머지 줄은 그대로


def test_switch_follows_each_generation(site: Site) -> None:
    site.apply(site.kit.bundle(1, "r0", writes=True))
    site.apply(site.kit.bundle(2, "r1"))
    assert _env_file(site, 1)["PUREIRON_WRITES_ENABLED"] == "true"
    assert _env_file(site, 2)["PUREIRON_WRITES_ENABLED"] == "false"


def test_compose_calls_drop_the_switch_from_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docker compose 보간은 셸 환경이 `--env-file`보다 우선 — 그 키를 지워야 `.env`가 이긴다."""
    monkeypatch.setenv("PUREIRON_WRITES_ENABLED", "true")
    monkeypatch.setenv("PUREIRON_KEEP_ME", "1")
    runner = FakeRunner()
    target = ComposeTarget(tmp_path / "g1")
    ComposeCli(runner).up(target)
    env = runner.envs[0]
    assert env is not None
    assert "PUREIRON_WRITES_ENABLED" not in env
    assert env["PUREIRON_KEEP_ME"] == "1"
    docker = FakeRunner()
    DockerCli(docker).available()
    assert docker.envs == [None]  # docker CLI 호출은 환경을 그대로 물려준다
    assert compose_env({"PUREIRON_WRITES_ENABLED": "true", "A": "b"}) == {"A": "b"}


def test_subprocess_runner_uses_the_given_environment() -> None:
    probe = [
        sys.executable,
        "-c",
        "import os; print(os.environ.get('PUREIRON_WRITES_ENABLED', '-'))",
    ]
    assert subprocess_runner(probe, env={"PUREIRON_WRITES_ENABLED": "x"}).stdout.strip() == "x"
    assert subprocess_runner(probe, env={}).stdout.strip() == "-"


def test_updater_env_switch_is_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = {"PUREIRON_SITE_ID": "site-a", "PUREIRON_WRITES_ENABLED": "true"}
    settings = Settings.from_env(env)
    assert settings.ignored_env == ("PUREIRON_WRITES_ENABLED",)
    assert "PUREIRON_WRITES_ENABLED" not in settings.site_env()
    with caplog.at_level(logging.WARNING):
        cli.warn_ignored_env(settings)
    assert "PUREIRON_WRITES_ENABLED is set" in caplog.text
    assert Settings.from_env({"PUREIRON_SITE_ID": "site-a"}).ignored_env == ()
