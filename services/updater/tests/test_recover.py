"""crash 주입과 phase 표 전수 → `recover()` 수렴(plan §4.1, M2 종료 조건의 단위 판).

각 시나리오를 crash 없이 한 번 돌려 기준 결과와 바깥 호출 수(N)를 얻고, 1..N 모든 지점에서
crash(프로세스 강제 종료)를 주입한 뒤 새 Updater로 `recover()` 한다. 수렴 조건:
- 시도가 끝나 있고(중간 phase 잔류 0) 편집 잠금이 풀려 있다.
- 결과(applied/last_attempted/last_result/schema epoch/alarm)와 바깥 상태(실행 이미지·스키마·
  project_state·site_rev)가 crash 없는 실행과 **같다**.
- crash가 난 지점의 저장 phase 집합이 시나리오의 phase 표를 전부 덮는다.

창이 닫힌 뒤 복구하는 경우: 서비스 영향 전이면 `verified`(잠금 없음, 서비스 무변경)로 돌아가고,
`migrating` 이후면 창과 무관하게 끝까지 간다.
"""

from __future__ import annotations

import copy
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from updater_fakes import T0, Crash, Kit, Site, open_window, release_ids

from pureiron_bundle.verify import AttemptResult
from pureiron_updater.recover import recover
from pureiron_updater.state import Phase

Setup = Callable[[Site, Path], None]


@pytest.fixture(autouse=True)
def _no_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """crash는 파이썬 수준(op 경계)에서 주입하므로 fsync 내구성은 필요 없다(test_state가 따로 검사).
    수천 번 실행하는 이 파일에서만 끈다."""

    def skip(_fd: int) -> None:
        return None

    monkeypatch.setattr(os, "fsync", skip)


def install_g1(site: Site, g1: Path) -> None:
    assert site.apply(g1).applied_gen == 1


def nothing(_site: Site, _g1: Path) -> None:
    return None


def fail_migrate(site: Site, g1: Path) -> None:
    install_g1(site, g1)
    site.world.fail_migrate_ids.add(release_ids("r1")["core"])


def fail_health(site: Site, g1: Path) -> None:
    install_g1(site, g1)
    site.world.unhealthy_ids.add(release_ids("r1")["core"])


def fail_rollback(site: Site, g1: Path) -> None:
    install_g1(site, g1)
    site.world.unhealthy_ids.update({release_ids("r1")["core"], release_ids("r0")["core"]})


@dataclass(frozen=True)
class Scenario:
    setup: Setup
    generation: int
    release: str
    result: AttemptResult
    phases: frozenset[Phase]


COMMON = {Phase.FETCHED, Phase.VERIFIED, Phase.COMMITTED, Phase.LOCKED, Phase.BACKED_UP}
SCENARIOS = {
    "first_install": Scenario(
        nothing,
        1,
        "r0",
        AttemptResult.SUCCEEDED,
        frozenset(COMMON | {Phase.MIGRATING, Phase.SWITCHING, Phase.CHECKING, Phase.SUCCEEDED}),
    ),
    "update": Scenario(
        install_g1,
        2,
        "r1",
        AttemptResult.SUCCEEDED,
        frozenset(COMMON | {Phase.MIGRATING, Phase.SWITCHING, Phase.CHECKING, Phase.SUCCEEDED}),
    ),
    "migrate_fail": Scenario(
        fail_migrate,
        2,
        "r1",
        AttemptResult.FAILED,
        frozenset(COMMON | {Phase.MIGRATING, Phase.ROLLING_BACK, Phase.FAILED}),
    ),
    "health_fail": Scenario(
        fail_health,
        2,
        "r1",
        AttemptResult.FAILED,
        frozenset(
            COMMON
            | {Phase.MIGRATING, Phase.SWITCHING, Phase.CHECKING, Phase.ROLLING_BACK, Phase.FAILED}
        ),
    ),
    "rollback_fail": Scenario(
        fail_rollback,
        2,
        "r1",
        AttemptResult.FAILED,
        frozenset(
            COMMON
            | {Phase.MIGRATING, Phase.SWITCHING, Phase.CHECKING, Phase.ROLLING_BACK}
            | {Phase.FAILED_MANUAL}
        ),
    ),
}
# 검증 단계 E_DRAFT는 커밋 전 거부라 잠금 단계까지 가지 않는다 — 잠금 아래 거부는 test_phases.


def outcome(site: Site) -> dict[str, object]:
    state = site.updater().state
    return {
        "attempt": None if state.attempt is None else state.attempt.phase.value,
        "applied_gen": state.applied_gen,
        "last_attempted_gen": state.last_attempted_gen,
        "last_result": state.last_result,
        "schema_epoch": state.schema_epoch,
        "alarm": state.alarm,
        "skipped": state.skipped_bundles,
        **site.world.snapshot(),
    }


class Harness:
    def __init__(self, tmp: Path, scenario: Scenario) -> None:
        self.tmp = tmp
        self.kit = Kit(tmp / "kit")
        self.scenario = scenario
        self.window = open_window(T0 - timedelta(hours=1), 2)
        # created_at은 만든 순서로 증가한다 — g1을 먼저 만들어야 대상 번들이 E_CERT(역행)가 아니다.
        self.g1 = self.kit.bundle(1, "r0")
        self.pib = self.kit.bundle(scenario.generation, scenario.release, window=self.window)
        self.runs = 0
        # setup(최초 설치 등)은 한 번만 하고 실행마다 상태 디렉터리·바깥 세계를 복사한다.
        self.base = Site(root=tmp / "base", kit=self.kit)
        scenario.setup(self.base, self.g1)

    def site(self) -> Site:
        self.runs += 1
        root = self.tmp / f"run{self.runs}"
        if self.base.root.exists():
            shutil.copytree(self.base.root, root)
        site = Site(root=root, kit=self.kit, world=copy.deepcopy(self.base.world))
        site.world.ops = 0
        site.world.log.clear()
        return site

    def reference(self) -> tuple[dict[str, object], int]:
        site = self.site()
        site.apply(self.pib)
        return outcome(site), site.world.ops

    def crash(self, at: int) -> tuple[Site, Phase | None]:
        site = self.site()
        site.world.crash_at = at
        with pytest.raises(Crash):
            site.apply(self.pib)
        attempt = site.updater().state.attempt
        return site, None if attempt is None else attempt.phase


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_crash_at_every_point_converges(tmp_path: Path, name: str) -> None:
    scenario = SCENARIOS[name]
    harness = Harness(tmp_path, scenario)
    expected, total = harness.reference()
    assert expected["attempt"] is None
    assert expected["lock"] is None
    assert (expected["last_attempted_gen"], expected["last_result"]) == (
        scenario.generation,
        scenario.result,
    )
    crashed_in: set[Phase] = set()
    for at in range(1, total + 1):
        site, phase = harness.crash(at)
        if phase is not None:
            crashed_in.add(phase)
        state = recover(site.updater())
        assert state.attempt is None, (at, phase, site.world.log[-3:])
        assert outcome(site) == expected, (at, phase, site.world.log)
    assert scenario.phases <= crashed_in, sorted(scenario.phases - crashed_in)


@pytest.mark.parametrize("name", ["update", "health_fail"])
def test_crash_during_recover_still_converges(tmp_path: Path, name: str) -> None:
    """복구 도중 다시 crash(재부팅 반복) → 다시 recover → 같은 결과."""
    harness = Harness(tmp_path, SCENARIOS[name])
    expected, total = harness.reference()
    first_points: dict[Phase | None, int] = {}
    for at in range(1, total + 1):
        _, phase = harness.crash(at)
        first_points.setdefault(phase, at)
    for at in first_points.values():
        probe, _ = harness.crash(at)
        probe.world.ops = 0
        recover(probe.updater())
        for again in range(1, probe.world.ops + 1):
            site, _ = harness.crash(at)
            site.world.ops = 0
            site.world.crash_at = again
            with pytest.raises(Crash):
                recover(site.updater())
            site.world.crash_at = None
            state = recover(site.updater())
            assert state.attempt is None, (at, again)
            assert outcome(site) == expected, (at, again, site.world.log)


@pytest.mark.parametrize("name", ["update", "health_fail", "migrate_fail"])
def test_recover_after_window_closed(tmp_path: Path, name: str) -> None:
    """창이 닫힌 뒤 복구: 서비스 영향 전이면 대기(verified)로, migrating 이후면 끝까지."""
    harness = Harness(tmp_path, SCENARIOS[name])
    expected, total = harness.reference()
    prev = release_ids("r0")
    service_phases = {Phase.MIGRATING, Phase.SWITCHING, Phase.CHECKING, Phase.ROLLING_BACK}
    for at in range(1, total + 1):
        site, phase = harness.crash(at)
        site.clock.t = T0 + timedelta(hours=3)  # 창(T0-1h ~ T0+1h) 밖
        state = recover(site.updater())
        assert site.world.lock is None, (at, phase)
        if state.attempt is None:
            assert outcome(site) == expected, (at, phase)
            continue
        assert phase not in service_phases, (at, phase)
        assert state.attempt.phase is Phase.VERIFIED, (at, phase)
        assert site.world.running == prev, (at, phase)
        assert site.world.schema_epoch == 1, (at, phase)
