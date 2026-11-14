"""중앙 수명(M6-4a 리뷰 M1) — `up`이 중간에 실패해도 내림, fleet 묶음의 마지막 시험 뒤에 내림
(conftest `pytest_runtest_teardown` → `fleet_session.Lifetime`), fixture 끝에 내림, 지난 run이 남긴
중앙을 preflight 정리가 멱등으로 내림. 원격 호출은 모두 가짜다."""

from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import nightly
import pytest
from fleet_fakes import FakeRemote
from g2_fakes import scenario_session
from harness import fleet_session, order, paths, preflight
from harness.context import InfraError
from harness.github import DryRun

FLEET_FILE = "test_g2_scheduled.py"
CRASH_FILE = "test_g2_crash.py"


def downs(fake: FakeRemote) -> int:
    return fake.events.count("central down")


def test_a_central_up_that_fails_midway_is_still_brought_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`compose up --wait` 시간 초과처럼 컨테이너가 뜬 채 `up`이 실패해도 내린다."""
    monkeypatch.setattr(paths, "BUNDLES", tmp_path / "bundles")
    s = scenario_session(tmp_path)
    fake = FakeRemote(s)

    def central(*args: str) -> dict[str, object]:
        if args[0] == "up":
            fake.events.append("central up")
            raise ValueError("compose up --wait timed out")
        return fake.central(*args)

    remote = dataclasses.replace(fake.remote(), central=central)
    with pytest.raises(InfraError, match=r"central\.py up failed"):
        fleet_session.prepare_c1(s, remote=remote, results=tmp_path)
    assert fake.events == ["central up", "central down"]


def test_in_central_reads_the_order_table() -> None:
    assert order.in_central(FLEET_FILE)
    assert not order.in_central(CRASH_FILE)
    assert not order.in_central(None)
    assert not order.in_central("test_g2_unknown.py")


@pytest.mark.parametrize(
    ("current", "following", "closed"),
    [
        (FLEET_FILE, CRASH_FILE, 1),  # fleet 묶음을 벗어남 → 내림
        (FLEET_FILE, None, 1),  # 세션의 마지막 시험
        (FLEET_FILE, FLEET_FILE, 0),  # 같은 묶음 안 — 다음 fleet 시나리오가 중앙을 쓴다
        ("test_g2_draft.py", FLEET_FILE, 0),  # 묶음 앞
        (CRASH_FILE, "test_g2_no_external.py", 0),  # 묶음 밖끼리
    ],
)
def test_the_lifetime_closes_only_when_leaving_the_central_group(
    tmp_path: Path, current: str, following: str | None, closed: int
) -> None:
    fake = FakeRemote(scenario_session(tmp_path))
    life = fleet_session.Lifetime(fake.remote(), up=True)
    life.after(current, following)
    assert downs(fake) == closed
    life.end()  # 세션 끝: 이미 내렸으면 다시 부르지 않는다
    assert downs(fake) == 1


def test_a_lifetime_that_never_came_up_never_calls_down(tmp_path: Path) -> None:
    fake = FakeRemote(scenario_session(tmp_path))
    life = fleet_session.Lifetime(fake.remote())
    life.after(FLEET_FILE, CRASH_FILE)
    life.end()
    assert downs(fake) == 0


def prepared_fs(tmp_path: Path) -> fleet_session.FleetSession:
    return fleet_session.FleetSession("R", "c1-R-0001", tmp_path / "ca", "AB")


def test_the_fixture_body_brings_the_central_down_even_on_errors(tmp_path: Path) -> None:
    """변이: fixture 끝의 내림을 빼면 crash 묶음(주간 약 100분)과 그 뒤까지 중앙이 떠 있다."""
    s = scenario_session(tmp_path)
    fake = FakeRemote(s)
    life = fleet_session.Lifetime(fake.remote())
    selected = [f"{FLEET_FILE}::test_g2_2_scheduled_update[put_201]"]
    with fleet_session.fleet_lifetime(
        s, selected, life, prepare=lambda session: prepared_fs(tmp_path)
    ) as fs:
        assert fs.c1_name == "c1-R-0001"
        assert life.up
    assert downs(fake) == 1
    assert not life.up
    with (
        pytest.raises(RuntimeError, match="boom"),
        fleet_session.fleet_lifetime(
            s, selected, life, prepare=lambda session: prepared_fs(tmp_path)
        ),
    ):
        raise RuntimeError("boom")
    assert downs(fake) == 2


def gates_conftest() -> ModuleType:
    path = Path(nightly.__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("g2_gates_conftest_life", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_conftest_teardown_hook_closes_after_the_last_fleet_test(tmp_path: Path) -> None:
    """변이: conftest 훅(또는 그 안의 `after`)을 빼면 fleet 묶음 뒤에도 세션 끝까지 중앙이
    남는다."""
    hooks = gates_conftest()
    fake = FakeRemote(scenario_session(tmp_path))
    life = fleet_session.Lifetime(fake.remote(), up=True)
    hooks.LIFE = life

    def item(name: str) -> SimpleNamespace:
        return SimpleNamespace(path=Path(name))

    hooks.pytest_runtest_teardown(item(FLEET_FILE), item(FLEET_FILE))
    assert downs(fake) == 0
    hooks.pytest_runtest_teardown(item(FLEET_FILE), item(CRASH_FILE))
    assert downs(fake) == 1
    assert not life.up


def test_preflight_cleanup_brings_a_leftover_central_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """지난 run이 시간 제한 kill로 남긴 중앙(compose `restart: unless-stopped`)은 다음 밤 preflight
    정리가 내린다 — 실패해도 정리 기록에만 남는다."""
    monkeypatch.setattr(preflight, "remove_vms", lambda sites: [])
    monkeypatch.setattr(preflight, "prune_results", lambda results, now: [])
    monkeypatch.setattr(preflight, "prune_branches", lambda gh, now: [])
    called: list[str] = []

    def down() -> list[str]:
        called.append("down")
        return ["central down (leftover)"]

    assert nightly.cleanup_actions(DryRun(), down_central=down) == ["central down (leftover)"]
    assert called == ["down"]
    fake = FakeRemote(scenario_session(tmp_path))
    assert fleet_session.down_leftover(fake.remote()) == ["central down (leftover)"]

    def broken(*args: str) -> dict[str, object]:
        raise ValueError("wsl gone")

    remote = dataclasses.replace(fake.remote(), central=broken)
    assert fleet_session.down_leftover(remote) == ["central down (leftover) failed: wsl gone"]


def test_the_default_cleanup_includes_the_leftover_down() -> None:
    """`cleanup_actions`의 기본 내림이 `down_leftover`다(실제 preflight가 부른다)."""
    default = nightly.cleanup_actions.__defaults__
    assert default == (fleet_session.down_leftover,)
