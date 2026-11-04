"""revert(F8): 순수 `revert_content` 표 + 임시 fleet repo에서 `--no-push` CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from build_bundle import ReleaseRecord
from fleet import revert
from fleet.fleetfiles import SiteFile, parse_site
from fleet.github import Git
from fleet.revert import RevertError, revert_content

from .kit import GIT_ENV, Repo, git, site_yaml


def record(version: str, epoch: int, expected: int, max_compatible: int) -> ReleaseRecord:
    return ReleaseRecord(
        name=version,
        version=version,
        images={},
        epoch=epoch,
        app_expected=expected,
        app_max_compatible=max_compatible,
    )


RELEASES = {
    "2026.10.0": record("2026.10.0", 4, 4, 5),
    "2026.11.0": record("2026.11.0", 5, 5, 6),
    "2027.1.0": record("2027.1.0", 7, 7, 8),
}


def site(text: str) -> SiteFile:
    parsed, problems = parse_site(text.encode(), "sites/site-a.yaml")
    assert parsed is not None, problems
    return parsed


HISTORY = [
    site(site_yaml("site-a", 1, product="2026.10.0", version="v1")),
    site(site_yaml("site-a", 2, product="2026.11.0", version="v2", writes=True)),
    site(site_yaml("site-a", 3, product="2026.11.0", version="v3", writes=True)),
]


def test_revert_takes_old_content_with_next_generation() -> None:
    current = HISTORY[-1]
    result = revert_content(HISTORY, current, 2, RELEASES)
    assert result.document == {**HISTORY[1].document, "generation": 4}
    assert result.warnings == ()


def test_revert_across_envelope_warns_and_writes_change_warns() -> None:
    current = site(site_yaml("site-a", 5, product="2027.1.0", writes=True))
    result = revert_content([*HISTORY, current], current, 1, RELEASES)
    assert result.document["generation"] == 6
    assert "writes" not in result.document
    assert len(result.warnings) == 2
    assert "schema epoch 7가 2026.10.0의 envelope [4, 5] 밖" in result.warnings[0]
    assert "writes.enabled가 꺼진다" in result.warnings[1]


def test_revert_within_envelope_has_no_epoch_warning() -> None:
    current = site(site_yaml("site-a", 4, product="2026.11.0", version="v1"))
    result = revert_content([*HISTORY, current], current, 1, RELEASES)
    assert result.warnings == ()  # epoch 5 ∈ 2026.10.0 envelope [4, 5]


def test_revert_unknown_release_warns() -> None:
    current = site(site_yaml("site-a", 4, product="2099.1.0"))
    result = revert_content([*HISTORY, current], current, 3, RELEASES)
    assert any("release 기록 없음(2099.1.0)" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("to_gen", "message"),
    [(3, "작아야 한다"), (9, "작아야 한다"), (0, "generation 0 파일이 없다")],
)
def test_revert_rejects(to_gen: int, message: str) -> None:
    with pytest.raises(RevertError, match=message):
        revert_content(HISTORY, HISTORY[-1], to_gen, RELEASES)


def test_revert_rejects_conflicting_history() -> None:
    other = site(site_yaml("site-a", 2, product="2026.10.0"))
    with pytest.raises(RevertError, match="여럿"):
        revert_content([*HISTORY, other], HISTORY[-1], 2, RELEASES)


def test_cli_creates_branch_and_commit_without_push(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in GIT_ENV.items():  # revert.main의 git commit도 전역 설정 없이 돈다
        monkeypatch.setenv(name, value)
    repo = Repo.create(tmp_path / "fleet")
    repo.commit({"sites/site-a.yaml": site_yaml("site-a", 1, version="v1")}, "g1")
    repo.commit({"sites/site-a.yaml": site_yaml("site-a", 2, version="v2")}, "g2")
    repo.commit({"sites/site-b.yaml": site_yaml("site-b", 1)}, "other site")
    code = revert.main(["site-a", "--to-gen", "1", "--repo", str(repo.path), "--no-push"])
    assert code == 0
    assert git(repo.path, "branch", "--show-current") == "revert/site-a-g3"
    written = site((repo.path / "sites" / "site-a.yaml").read_text(encoding="utf-8"))
    assert (written.generation, written.project_version) == (3, "v1")
    assert git(repo.path, "log", "-1", "--format=%s") == "revert(site-a): gen 3 = gen 1 내용"
    assert "revert/site-a-g3" in capsys.readouterr().out


def test_cli_missing_site_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = Repo.create(tmp_path / "fleet")
    repo.commit({"README.md": "x"}, "init")
    assert revert.main(["site-a", "--to-gen", "1", "--repo", str(repo.path), "--no-push"]) == 1
    assert "sites/site-a.yaml가 없다" in capsys.readouterr().err


def test_history_follows_first_parent_only(tmp_path: Path) -> None:
    """r1 L3: merge commit 방식 PR의 중간 커밋(gen 3 초안)은 이력 후보가 아니다."""
    repo = Repo.create(tmp_path / "fleet")
    repo.commit({"sites/site-a.yaml": site_yaml("site-a", 1, version="v1")}, "g1")
    repo.commit({"sites/site-a.yaml": site_yaml("site-a", 2, version="v2")}, "g2")
    repo.branch("pr", "main")
    repo.commit({"sites/site-a.yaml": site_yaml("site-a", 3, product="2026.10.0")}, "draft")
    repo.commit({"sites/site-a.yaml": site_yaml("site-a", 3, version="v3")}, "final")
    repo.switch("main")
    repo.merge("pr")
    current, history = revert.site_history(Git(repo.path), "HEAD", "site-a")
    assert [(h.generation, h.product, h.project_version) for h in history] == [
        (3, "2026.11.0", "v3"),
        (2, "2026.11.0", "v2"),
        (1, "2026.11.0", "v1"),
    ]
    assert revert_content(history, current, 1, RELEASES).document["generation"] == 4
