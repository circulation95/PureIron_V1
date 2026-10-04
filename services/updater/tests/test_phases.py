"""상태기계 시나리오: 최초 설치·업데이트·no-op·창·거부 코드·실패/롤백·잠금 해제."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from updater_fakes import (
    SITE_COMPOSE,
    FakeCompose,
    FakeCore,
    Kit,
    Site,
    closed_window,
    open_window,
    release_ids,
)

from pureiron_bundle.verify import AttemptResult
from pureiron_updater.phases import Updater
from pureiron_updater.ports import CoreUnavailableError
from pureiron_updater.state import Phase, UpdaterState


@pytest.fixture
def site(tmp_path: Path) -> Site:
    return Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))


def installed(site: Site) -> Site:
    """g1 = r0(epoch 1) 최초 설치 완료 상태."""
    state = site.apply(site.kit.bundle(1, "r0"))
    assert state.applied_gen == 1
    return site


def assert_untouched(site: Site, before: dict[str, object], up_calls: int) -> None:
    """거부·실패 전 단계: 실행 중 스택·잠금·project_state가 그대로(core `StartedAt` 불변 대용)."""
    assert site.world.snapshot() == before
    assert site.world.up_calls == up_calls


def reports(site: Site) -> list[dict[str, object]]:
    directory = site.settings.state_dir / "reports"
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def test_first_install_needs_no_window_lock_or_backup(site: Site) -> None:
    state = site.apply(site.kit.bundle(1, "r0", window=closed_window()))
    assert state.attempt is None
    assert (state.applied_gen, state.last_attempted_gen) == (1, 1)
    assert state.last_result is AttemptResult.SUCCEEDED
    assert state.schema_epoch == 1
    assert site.world.running == release_ids("r0")
    assert site.world.schema_epoch == 1
    assert site.world.lock is None
    assert site.world.project["version"] == "v1"
    assert not site.settings.backups_dir.exists()
    target = site.settings.compose_dir / "g1"
    assert (target / "compose.yml").read_bytes() == SITE_COMPOSE.read_bytes()
    env = (target / ".env").read_text(encoding="utf-8")
    assert "PUREIRON_RELEASE=r0\n" in env
    assert "PUREIRON_SITE_DB_VERSION=2.30.2-pg16\n" in env
    # 추출본·번들 사본은 정리되고 보고·메트릭이 남는다.
    assert list(site.settings.bundles_dir.iterdir()) == []
    (report,) = reports(site)
    assert (report["result"], report["generation"], report["from"]) == ("succeeded", 1, None)
    prom = (site.settings.state_dir / "metrics" / "updater.prom").read_text(encoding="utf-8")
    assert 'updater_last_result{result="succeeded"} 1' in prom
    assert "updater_applied_generation 1" in prom


def test_update_migrates_backs_up_switches_and_releases_lock(site: Site) -> None:
    installed(site)
    site.world.site_rev = site.world.base_site_rev = 4
    state = site.apply(site.kit.bundle(2, "r1", version="v2"))
    assert (state.applied_gen, state.last_result, state.schema_epoch) == (
        2,
        AttemptResult.SUCCEEDED,
        2,
    )
    assert site.world.running == release_ids("r1")
    assert site.world.lock is None
    assert site.world.project["version"] == "v2"
    assert (site.world.site_rev, site.world.base_site_rev) == (4, 4)
    backup = site.settings.backups_dir / "g2"
    assert sorted(p.name for p in backup.iterdir()) == [
        "COMPLETE",
        "compose",
        "db.dump",
        "project.json",
        "secrets.txt",
    ]
    snapshot = json.loads((backup / "project.json").read_text(encoding="utf-8"))
    assert (snapshot["version"], snapshot["site_rev"]) == ("v1", 4)
    assert (backup / "compose" / ".env").read_text(encoding="utf-8").count("r0") == 1
    report = reports(site)[-1]
    assert (report["from"], report["to"], report["result"]) == (1, 2, "succeeded")
    timeline = cast(dict[str, str], report["timeline"])
    order = ["fetched", "verified", "committed", "locked", "backed_up", "migrating"]
    order += ["switching", "checking", "succeeded"]
    stamps = [str(timeline[phase]) for phase in order]
    assert stamps == sorted(stamps)


def test_same_generation_is_a_no_op(site: Site) -> None:
    installed(site)
    before, ups = site.world.snapshot(), site.world.up_calls
    state = site.apply(site.kit.bundle(1, "r0"))
    assert state.attempt is None
    assert state.applied_gen == 1
    assert_untouched(site, before, ups)


def test_window_closed_waits_in_verified_without_lock(site: Site) -> None:
    installed(site)
    before, ups = site.world.snapshot(), site.world.up_calls
    window = open_window(site.clock.t + timedelta(hours=2), 1)
    state = site.apply(site.kit.bundle(2, "r1", window=window))
    assert state.attempt is not None
    assert state.attempt.phase is Phase.VERIFIED
    assert state.last_attempted_gen == 1  # 커밋 전: generation 기록 없음
    assert_untouched(site, before, ups)
    site.clock.sleep(2.5 * 3600)
    state = site.updater().advance()
    assert (state.applied_gen, state.last_result) == (2, AttemptResult.SUCCEEDED)


def test_higher_generation_supersedes_waiting_attempt(site: Site) -> None:
    installed(site)
    site.apply(site.kit.bundle(2, "r1", window=closed_window()))
    state = site.apply(site.kit.bundle(3, "r1", version="v3"))
    assert (state.applied_gen, site.world.project["version"]) == (3, "v3")
    assert not (site.settings.compose_dir / "g2").exists()


def test_lower_generation_rejected_and_not_recorded(site: Site) -> None:
    installed(site)
    site.apply(site.kit.bundle(5, "r0", version="v5"))
    before, ups = site.world.snapshot(), site.world.up_calls
    replay = site.kit.bundle(4, "r0")
    state = site.apply(replay)
    assert (state.applied_gen, state.last_attempted_gen) == (5, 5)
    assert state.last_result is AttemptResult.SUCCEEDED
    assert reports(site)[-1]["code"] == "E_GEN"
    assert len(state.skipped_bundles) == 1
    assert_untouched(site, before, ups)


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("other_site", "E_SITE"),
        ("tampered", "E_SIG"),
        ("schema_jump", "E_SCHEMA"),
        ("updater_too_old", "E_UPDATER"),
        ("unabsorbed_draft", "E_DRAFT"),
        ("compose_mismatch", "E_DIGEST"),
        ("site_db_change", "E_DIGEST"),
    ],
)
def test_rejections_happen_before_service_impact(site: Site, case: str, code: str) -> None:
    installed(site)
    kit = site.kit
    if case == "unabsorbed_draft":
        site.world.site_rev = 3
    pib = {
        "other_site": lambda: kit.bundle(2, "r1", site="site-b"),
        "tampered": lambda: kit.bundle(2, "r1"),
        "schema_jump": lambda: kit.bundle(2, "r2"),
        "updater_too_old": lambda: kit.bundle(2, "r1", min_updater="9.0.0"),
        "unabsorbed_draft": lambda: kit.bundle(2, "r1"),
        "compose_mismatch": lambda: kit.bundle(
            2, "r1", compose=SITE_COMPOSE.read_bytes().replace(b"pureiron/web:", b"evil/web:")
        ),
        "site_db_change": lambda: kit.bundle(2, "r1", site_db_release="r1"),
    }[case]()
    if case == "tampered":
        data = bytearray(pib.read_bytes())
        data[600] ^= 1  # manifest.json 본문
        pib.write_bytes(bytes(data))
    before, ups = site.world.snapshot(), site.world.up_calls
    state = site.apply(pib)
    assert state.attempt is None
    assert (state.applied_gen, state.last_attempted_gen) == (1, 1)
    assert state.last_result is AttemptResult.SUCCEEDED
    report = reports(site)[-1]
    assert (report["result"], report["code"]) == ("rejected", code)
    assert_untouched(site, before, ups)
    assert not (site.settings.compose_dir / "g2").exists()
    # 같은 파일은 다시 받지 않는다(poll이 skipped_bundles를 본다).
    assert len(state.skipped_bundles) == 1


def test_absorbed_draft_passes_gate(site: Site) -> None:
    installed(site)
    site.world.site_rev = 3
    state = site.apply(site.kit.bundle(2, "r1", absorbs={"site-a": 3}))
    assert state.applied_gen == 2
    assert (site.world.site_rev, site.world.base_site_rev) == (3, 3)


def test_draft_appearing_after_verify_is_rejected_under_lock(site: Site) -> None:
    installed(site)
    window = open_window(site.clock.t + timedelta(hours=1), 2)
    site.apply(site.kit.bundle(2, "r1", window=window))
    site.world.site_rev = 1  # 창 대기 중 현장 PATCH
    before, ups = site.world.snapshot(), site.world.up_calls
    site.clock.sleep(3700)
    state = site.updater().advance()
    assert state.attempt is None
    assert (state.last_attempted_gen, state.last_result) == (2, AttemptResult.REJECTED)
    assert reports(site)[-1]["code"] == "E_DRAFT"
    assert reports(site)[-1]["phase"] == "locked"
    assert_untouched(site, before, ups)
    # 자동 재시도 없음: 같은 generation은 다시 시도하지 않는다.
    again = site.apply(site.kit.bundle(2, "r1", absorbs={"site-a": 1}))
    assert again.last_result is AttemptResult.REJECTED
    assert reports(site)[-1]["code"] == "E_GEN"


def test_migration_failure_rolls_back_and_revert_is_accepted(site: Site) -> None:
    installed(site)
    site.world.site_rev = site.world.base_site_rev = 2
    site.world.fail_migrate_ids.add(release_ids("r1")["core"])
    state = site.apply(site.kit.bundle(2, "r1", version="v2"))
    assert (state.applied_gen, state.last_attempted_gen) == (1, 2)
    assert state.last_result is AttemptResult.FAILED
    assert site.world.running == release_ids("r0")
    assert site.world.lock is None
    assert (site.world.project["version"], site.world.site_rev, site.world.base_site_rev) == (
        "v1",
        2,
        2,
    )
    report = reports(site)[-1]
    assert (report["phase"], report["code"]) == ("migrating", "E_MIGRATE")
    # 취소 = 이전 내용을 담은 더 높은 generation(revert PR)
    state = site.apply(site.kit.bundle(3, "r0", version="v1"))
    assert (state.applied_gen, state.last_result) == (3, AttemptResult.SUCCEEDED)


def test_health_failure_rolls_back_schema_stays_forward(site: Site) -> None:
    installed(site)
    site.world.unhealthy_ids.add(release_ids("r1")["core"])
    state = site.apply(site.kit.bundle(2, "r1", version="v2"))
    assert (state.applied_gen, state.last_result) == (1, AttemptResult.FAILED)
    assert reports(site)[-1]["code"] == "E_HEALTH"
    assert site.world.running == release_ids("r0")
    assert site.world.schema_epoch == 2  # 스키마는 전진만
    assert state.schema_epoch == 2
    assert site.world.project["version"] == "v1"
    # r0 envelope [1, 2]가 epoch 2를 포함하므로 revert가 수락된다(NO_MIGRATE).
    state = site.apply(site.kit.bundle(3, "r0", version="v1"))
    assert (state.applied_gen, state.last_result) == (3, AttemptResult.SUCCEEDED)
    assert site.world.running == release_ids("r0")


def test_switch_failure_rolls_back(site: Site) -> None:
    installed(site)
    site.world.fail_up_ids.add(release_ids("r1")["web"])
    state = site.apply(site.kit.bundle(2, "r1"))
    assert state.last_result is AttemptResult.FAILED
    assert reports(site)[-1]["code"] == "E_SWITCH"
    assert site.world.running == release_ids("r0")


def test_repeated_rollback_failure_is_failed_manual(site: Site) -> None:
    installed(site)
    site.world.unhealthy_ids.update({release_ids("r1")["core"], release_ids("r0")["core"]})
    state = site.apply(site.kit.bundle(2, "r1"))
    assert state.attempt is None
    assert state.last_result is AttemptResult.FAILED
    assert state.alarm == "g2: E_HEALTH"
    assert site.world.lock is None
    report = reports(site)[-1]
    assert report["failed_manual"] is True
    assert "rollback #2" in str(report["logs"])
    prom = (site.settings.state_dir / "metrics" / "updater.prom").read_text(encoding="utf-8")
    assert "updater_failed_manual 1" in prom


class NoReleaseCore(FakeCore):
    """잠금 해제 HTTP만 실패하는 core(롤백 직후 core 재기동 중 등)."""

    def release_lock(self) -> None:
        raise CoreUnavailableError("connection reset")


def test_lock_released_through_core_cli_when_http_is_down(site: Site) -> None:
    installed(site)
    deps = replace(site.deps(), core=NoReleaseCore(site.world))
    site.submit(site.kit.bundle(2, "r1"))
    state = Updater(site.settings, deps).advance()
    assert (state.applied_gen, state.attempt) == (2, None)
    assert site.world.lock is None
    compose = deps.compose
    assert isinstance(compose, FakeCompose)
    assert ("run_core", "g1", "project", "unlock", "--owner", "updater") in compose.calls


def test_unreleasable_lock_keeps_terminal_attempt_until_released(site: Site) -> None:
    installed(site)
    deps = replace(site.deps(), core=NoReleaseCore(site.world))
    site_db = site.world.running.pop("site-db")  # pg_dump도 CLI 해제도 실패한다
    site.submit(site.kit.bundle(2, "r1"))
    state = Updater(site.settings, deps).advance()
    assert state.attempt is not None
    assert (state.attempt.phase, state.attempt.lock_held) == (Phase.FAILED, True)
    assert site.world.lock == ("updater", 2)
    site.world.running["site-db"] = site_db
    state = Updater(site.settings, deps).advance()
    assert state.attempt is None
    assert (state.last_attempted_gen, state.last_result) == (2, AttemptResult.FAILED)
    assert site.world.lock is None
    assert reports(site)[-1]["code"] == "E_BACKUP"


def test_lock_conflict_from_stale_updater_lock_is_cleared(site: Site) -> None:
    installed(site)
    site.world.lock = ("updater", 99)
    state = site.apply(site.kit.bundle(2, "r1"))
    assert state.applied_gen == 2
    assert site.world.lock is None


def test_core_down_before_lock_waits_in_verified(site: Site) -> None:
    installed(site)
    site.world.core_http_down = True
    state = site.apply(site.kit.bundle(2, "r1"))
    assert state.attempt is not None
    assert state.attempt.phase is Phase.VERIFIED
    assert state.last_result is AttemptResult.PENDING  # 커밋은 됐고 다음 tick에서 재개
    site.world.core_http_down = False
    state = site.updater().advance()
    assert (state.applied_gen, state.last_result) == (2, AttemptResult.SUCCEEDED)


def test_state_file_is_persisted_after_every_phase(site: Site) -> None:
    installed(site)
    site.saves.clear()
    site.apply(site.kit.bundle(2, "r1"))
    phases = [s.attempt.phase.value for s in site.saves if s.attempt is not None]
    for phase in ("fetched", "verified", "committed", "locked", "backed_up"):
        assert phase in phases
    assert phases.index("migrating") < phases.index("switching") < phases.index("checking")
    final: UpdaterState = site.saves[-1]
    assert final.attempt is None
    assert site.updater().state == final
