"""M4-2 리허설에서 나온 publish 수정(M4 마감 §2-1 #3·#5·#6).

- #3 App ID: env·인자 값도 strip, 숫자가 아니면 원인을 적어 멈춘다(리허설 §3-2 401).
- #5 파일 권한: `main()`이 umask 0o027 — 사슬·번들·메트릭이 그룹 쓰기·기타 읽기 불가.
- #6 같은 sha `run` 재실행은 `approved.log`에 줄을 더하지 않는다. trust-base는 같은 sha여도 남긴다.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fleet import publish
from fleet.publish import Config, PublishError, trust_base

from .kit import FLEET, NoTokens, World, make_world, site_yaml


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def log_lines(world: World) -> list[str]:
    return (world.chain.directory / "approved.log").read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------- #3 App ID


@pytest.mark.parametrize("raw", ["12345\n", " 12345 ", "12345\r\n", "\t12345"])
def test_app_id_from_env_is_stripped(raw: str) -> None:
    assert Config(fleet_repo=FLEET, app_id=raw).resolved_app_id() == "12345"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12a45", "10진 숫자여야 한다(env PUREIRON_PUBLISH_APP_ID(또는 --app-id)): '12a45'"),
        ("123 45", "'123 45'"),
        ("\uff11\uff12\uff13", "10진 숫자"),  # 전각: isdecimal은 받지만 JWT iss 정수가 아니다
        (" \n", "GitHub App ID가 비어 있다(env PUREIRON_PUBLISH_APP_ID(또는 --app-id))"),
    ],
    ids=["letter", "inner-space", "fullwidth", "blank"],
)
def test_app_id_from_env_must_be_decimal(raw: str, expected: str) -> None:
    with pytest.raises(PublishError) as caught:
        Config(fleet_repo=FLEET, app_id=raw).resolved_app_id()
    assert expected in str(caught.value)


def test_app_id_file_is_checked_the_same_way(tmp_path: Path) -> None:
    path = tmp_path / "app_id"
    path.write_text(" 777\n", encoding="utf-8")
    assert Config(fleet_repo=FLEET, app_id_file=path).resolved_app_id() == "777"
    path.write_text("app-777\n", encoding="utf-8")
    with pytest.raises(PublishError, match=r"10진 숫자여야 한다\(.*app_id\): 'app-777'"):
        Config(fleet_repo=FLEET, app_id_file=path).resolved_app_id()
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(PublishError, match="비어 있다"):
        Config(fleet_repo=FLEET, app_id_file=path).resolved_app_id()


def test_bad_app_id_fails_the_cli_before_github(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(publish.os, "umask", lambda mask: 0o022)
    key = tmp_path / "app.pem"
    key.write_text("not a key", encoding="utf-8")
    code = publish.main(
        [
            *("run", "--fleet-repo", FLEET, "--sha", "0" * 40),
            *("--app-key-file", str(key), "--app-id", "12a45"),
            *("--state-dir", str(tmp_path / "state")),
        ]
    )
    assert code == 1
    assert "E_PUBLISH GitHub App ID는 10진 숫자여야 한다" in capsys.readouterr().out


# ---------------------------------------------------------------------------- #5 umask


def test_main_sets_umask_before_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    masks: list[int] = []

    def fake_umask(mask: int) -> int:
        masks.append(mask)
        return 0o022

    monkeypatch.setattr(publish.os, "umask", fake_umask)
    assert publish.main(["run", "--fleet-repo", "no-slash", "--sha", "0" * 40]) == 1
    capsys.readouterr()
    assert masks == ([] if sys.platform == "win32" else [0o027])


@pytest.fixture
def publish_umask() -> Generator[None]:
    previous = os.umask(publish.FILE_UMASK)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 파일 모드")
def test_chain_bundle_and_metrics_files_are_not_group_writable(
    tmp_path: Path, publish_umask: None
) -> None:
    del publish_umask
    world = make_world(tmp_path)  # 사슬 디렉터리·첫 기록도 umask 아래에서 만든다
    merged = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(merged) == 0
    chain = world.chain.directory
    files = [
        chain / "last_approved",
        chain / "approved.log",
        chain / "lock",
        chain / "project_commits.json",
        *world.config.bundles_dir.rglob("*.pib"),
        *world.config.metrics_dir.glob("*.json"),
    ]
    assert len(files) == 6
    modes = {p.relative_to(tmp_path).as_posix(): stat.S_IMODE(p.stat().st_mode) for p in files}
    assert modes == dict.fromkeys(modes, 0o640)
    for directory in (chain, world.config.bundles_dir, world.config.metrics_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o750


# ---------------------------------------------------------------------------- #6 같은 sha


def test_rerun_of_same_sha_does_not_grow_the_log(world: World) -> None:
    merged = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(merged) == 0
    after_first = log_lines(world)
    assert after_first[-1].split()[1:3] == [merged, "pr=#7"]
    assert world.run(merged) == 0  # 재실행(Re-run·workflow_dispatch)
    assert world.run(merged) == 0
    assert log_lines(world) == after_first
    assert world.chain.last_approved() == merged


def test_trust_base_of_the_same_sha_is_still_logged(world: World) -> None:
    merged = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(merged) == 0
    before = log_lines(world)
    assert trust_base(world.config, merged, NoTokens(), "re-reviewed") == 0
    lines = log_lines(world)
    assert lines[:-1] == before
    want = [merged, "trust-base", f"previous={merged}", "reason=re-reviewed"]
    assert lines[-1].split()[1:] == want
    assert world.chain.last_approved() == merged
