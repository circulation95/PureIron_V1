"""`pureiron-site` CLI: apply·bundle verify·status·recover 종료 코드와 출력."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from updater_fakes import T0, Crash, Kit, Site, open_window, release_ids

from pureiron_updater import cli
from pureiron_updater.phases import Deps, Updater
from pureiron_updater.settings import Settings
from pureiron_updater.state import Phase, UpdaterState


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    site = Site(root=tmp_path / "pureiron", kit=Kit(tmp_path))
    monkeypatch.setenv("PUREIRON_SITE_ID", "site-a")
    monkeypatch.setenv("PUREIRON_ROOT", str(site.root))
    monkeypatch.setenv("PUREIRON_ROOT_PUB", str(site.kit.root_pub))
    monkeypatch.setenv("PUREIRON_CHECK_TIMEOUT_S", "10")

    def fake_deps(_settings: Settings) -> Deps:
        return site.deps()

    monkeypatch.setattr(cli, "real_deps", fake_deps)
    return site


def test_apply_status_and_noop(site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    pib = site.kit.bundle(1, "r0")
    assert cli.main(["apply", str(pib)]) == cli.EXIT_OK
    assert site.world.running == release_ids("r0")
    assert pib.exists()  # 원본(USB)은 건드리지 않는다
    assert cli.main(["status", "--json"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    status = json.loads(out[out.index("{") :])
    assert (status["applied_gen"], status["last_result"]) == (1, "succeeded")
    assert cli.main(["apply", str(pib)]) == cli.EXIT_OK  # 같은 generation = no-op
    assert cli.main(["recover"]) == cli.EXIT_OK


def test_apply_rejection_exits_1(site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["apply", str(site.kit.bundle(1, "r0", site="site-b"))]) == cli.EXIT_FAILED
    assert "applied generation: None" in capsys.readouterr().out


def test_failed_update_exits_1(site: Site) -> None:
    assert cli.main(["apply", str(site.kit.bundle(1, "r0"))]) == cli.EXIT_OK
    site.world.unhealthy_ids.add(release_ids("r1")["core"])
    assert cli.main(["apply", str(site.kit.bundle(2, "r1"))]) == cli.EXIT_FAILED
    assert site.world.running == release_ids("r0")


def test_bundle_verify_has_no_side_effects(site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["bundle", "verify", str(site.kit.bundle(1, "r0"))]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert (out["generation"], out["decision"], out["schema"]) == (1, "apply", "migrate")
    assert not (site.root / "state" / "updater.json").exists()
    assert site.world.ops == 0
    other = site.kit.bundle(1, "r0", site="site-b")
    assert cli.main(["bundle", "verify", str(other)]) == cli.EXIT_FAILED
    assert capsys.readouterr().err.startswith("E_SITE:")


def test_missing_site_id_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUREIRON_SITE_ID", raising=False)
    assert cli.main(["status"]) == cli.EXIT_CONFIG


def test_same_rejected_bundle_exits_1_every_time(site: Site) -> None:
    pib = site.kit.bundle(1, "r0", site="site-b")
    assert cli.main(["apply", str(pib)]) == cli.EXIT_FAILED
    assert cli.main(["apply", str(pib)]) == cli.EXIT_FAILED


def test_service_tick_enforces_window_after_killed_apply(site: Site) -> None:
    """`apply`가 잠금 단계에서 죽고 창이 닫히면, 서비스 tick은 진행하지 않고 대기로 되돌린다."""
    assert cli.main(["apply", str(site.kit.bundle(1, "r0"))]) == cli.EXIT_OK
    site.submit(site.kit.bundle(2, "r1", window=open_window(T0 - timedelta(hours=1), 2)))

    def killed_when_locked(state: UpdaterState) -> None:
        if state.attempt is not None and state.attempt.phase is Phase.LOCKED:
            raise Crash("apply killed")

    killer = Updater(site.settings, replace(site.deps(), on_save=killed_when_locked))
    with pytest.raises(Crash):
        killer.advance()
    assert site.world.lock == ("updater", 2)
    site.clock.t = T0 + timedelta(hours=3)  # 창 닫힘
    cli.tick(site.settings, None, first=False)
    state = site.updater().state
    assert state.attempt is not None
    assert state.attempt.phase is Phase.VERIFIED
    assert site.world.lock is None
    assert site.world.running == release_ids("r0")


def test_superseding_apply_exits_0(site: Site) -> None:
    """리뷰 N-2: 대체된 이전 시도의 실패는 이번 apply의 종료 코드가 아니다."""
    assert cli.main(["apply", str(site.kit.bundle(1, "r0"))]) == cli.EXIT_OK
    site.world.core_http_down = True  # 커밋 후 잠금 불가 → pending 대기
    assert cli.main(["apply", str(site.kit.bundle(2, "r1"))]) == cli.EXIT_OK
    site.world.core_http_down = False
    assert cli.main(["apply", str(site.kit.bundle(3, "r1", version="v3"))]) == cli.EXIT_OK
    assert site.world.project["version"] == "v3"
