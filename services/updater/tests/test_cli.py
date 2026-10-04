"""`pureiron-site` CLI: apply·bundle verify·status·recover 종료 코드와 출력."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from updater_fakes import Kit, Site, release_ids

from pureiron_updater import cli
from pureiron_updater.phases import Deps
from pureiron_updater.settings import Settings


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
