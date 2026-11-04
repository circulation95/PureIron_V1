"""check-gen(F4) 판정 표 + 임시 fleet repo에서 `run`·CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from fleet import check_gen
from fleet.check_gen import check, release_epochs, report
from fleet.fleetfiles import SiteStatus, SiteUpdate, parse_site
from fleet.github import Git

from .kit import Repo, site_yaml

EPOCHS = {"2026.10.0": 4, "2026.11.0": 5, "2027.1.0": 7}
REGISTERED = SiteStatus(max_published_gen=2, current_product="2026.11.0")


def update(new: str, old: str | None = None) -> SiteUpdate:
    site, problems = parse_site(new.encode(), "sites/site-a.yaml")
    assert site is not None, problems
    previous = None
    if old is not None:
        previous, problems = parse_site(old.encode(), "sites/site-a.yaml")
        assert previous is not None, problems
    return SiteUpdate("sites/site-a.yaml", site, previous, ())


def lines(up: SiteUpdate, status: SiteStatus | None = REGISTERED) -> tuple[list[str], list[str]]:
    findings = check([up], {"site-a": status}, EPOCHS)
    errors = [f"{f.code} {f.message}" for f in findings if f.is_error]
    warnings = [f.message for f in findings if not f.is_error]
    return errors, warnings


def test_next_generation_passes() -> None:
    assert lines(update(site_yaml("site-a", 3), site_yaml("site-a", 2))) == ([], [])


@pytest.mark.parametrize("new", [2, 4], ids=["same", "skip"])
def test_generation_must_be_exactly_plus_one(new: int) -> None:
    errors, _ = lines(update(site_yaml("site-a", new), site_yaml("site-a", 2)))
    assert any("E_GEN generation은 정확히 +1" in e for e in errors)


def test_generation_must_exceed_published() -> None:
    status = SiteStatus(max_published_gen=5, current_product=None)
    errors, _ = lines(update(site_yaml("site-a", 3), site_yaml("site-a", 2)), status)
    assert errors == ["E_GEN generation 3 ≤ 게시된 최대 gen 5(control-api max_published_gen)"]


def test_generation_equal_to_published_is_rejected() -> None:
    """r1 T4(변이 M26): new == max_published_gen 경계도 E_GEN(`>`이어야 한다)."""
    status = SiteStatus(max_published_gen=2, current_product=None)
    errors, _ = lines(update(site_yaml("site-a", 2), site_yaml("site-a", 1)), status)
    assert errors == ["E_GEN generation 2 ≤ 게시된 최대 gen 2(control-api max_published_gen)"]


def test_unregistered_site_only_gen_one_and_warns() -> None:
    errors, warnings = lines(update(site_yaml("site-a", 1)), None)
    assert errors == []
    assert any("미등록" in w and "site register" in w for w in warnings)
    errors, _ = lines(update(site_yaml("site-a", 2), site_yaml("site-a", 1)), None)
    assert errors == ["E_GEN control-api에 미등록 현장 'site-a'은 gen 1만 — 2"]


def test_new_file_starts_at_one() -> None:
    status = SiteStatus(max_published_gen=None, current_product=None)
    assert lines(update(site_yaml("site-a", 1)), status) == ([], [])
    errors, _ = lines(update(site_yaml("site-a", 2)), status)
    assert any("0 → 1" in e for e in errors)


def test_unreadable_base_file_is_e_gen() -> None:
    site, _ = parse_site(site_yaml("site-a", 3).encode(), "sites/site-a.yaml")
    assert site is not None
    bad = SiteUpdate("sites/site-a.yaml", site, None, ("broken",))
    findings = check([bad], {"site-a": REGISTERED}, EPOCHS)
    assert [f.code for f in findings] == ["E_GEN"]


def test_warnings_release_writes_epoch_jump() -> None:
    _, missing = lines(update(site_yaml("site-a", 3, product="2099.1.0"), site_yaml("site-a", 2)))
    assert missing == ["product 2099.1.0 release 기록이 V1 deploy/releases에 없다"]
    _, writes = lines(update(site_yaml("site-a", 3, writes=True), site_yaml("site-a", 2)))
    assert writes == [
        "writes.enabled: true — 현장 Updater 0.2.0 이상만 적용한다(구 Updater는 E_UPDATER)"
    ]
    _, jump = lines(update(site_yaml("site-a", 3, product="2027.1.0"), site_yaml("site-a", 2)))
    assert len(jump) == 1
    assert "schema epoch 5(2026.11.0) → 7(2027.1.0)" in jump[0]
    _, one = lines(
        update(site_yaml("site-a", 3, product="2026.11.0"), site_yaml("site-a", 2)),
        SiteStatus(max_published_gen=2, current_product="2026.10.0"),
    )
    assert one == []  # epoch 4 → 5는 경고 없음


def test_release_epochs_reads_v1_records() -> None:
    epochs = release_epochs(check_gen.RELEASES)
    assert epochs  # V1 deploy/releases의 정식 기록(r0~)
    assert all(isinstance(v, int) for v in epochs.values())


def test_run_on_git_repo(tmp_path: Path) -> None:
    repo = Repo.create(tmp_path / "fleet")
    base = repo.commit(
        {"sites/site-a.yaml": site_yaml("site-a", 2), "sites/.retired/.gitkeep": ""}, "base"
    )
    head = repo.commit(
        {"sites/site-a.yaml": site_yaml("site-a", 3), "sites/site-b.yaml": site_yaml("site-c", 1)},
        "pr",
    )
    looked: list[str] = []

    def lookup(site: str) -> SiteStatus | None:
        looked.append(site)
        return REGISTERED

    findings = check_gen.run(Git(repo.path), base, head, lookup, EPOCHS)
    assert looked == ["site-a"]  # 파일명 위반(site-b ≠ site-c)은 조회하지 않는다
    assert [(f.code, f.path) for f in findings] == [("E_FLEET", "sites/site-b.yaml")]
    assert report(findings) == 1


def test_run_reports_lookup_failure(tmp_path: Path) -> None:
    repo = Repo.create(tmp_path / "fleet")
    base = repo.commit({"README.md": "x"}, "base")
    head = repo.commit({"sites/site-a.yaml": site_yaml("site-a", 1)}, "pr")

    def lookup(site: str) -> SiteStatus | None:
        raise LookupError(f"HTTP 401 for {site}")

    findings = check_gen.run(Git(repo.path), base, head, lookup, EPOCHS)
    assert [(f.code, f.message) for f in findings] == [
        ("E_GEN", "control-api 조회 실패: HTTP 401 for site-a")
    ]


def test_cli_requires_viewer_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(check_gen.VIEWER_TOKEN_ENV, raising=False)
    assert check_gen.main(["--base", "a", "--head", "b"]) == 1
    assert "PUREIRON_FLEET_VIEWER_TOKEN" in capsys.readouterr().out
