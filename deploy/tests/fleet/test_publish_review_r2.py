"""리뷰 r2(`review-m4-2-r2.md`) 회귀: 템플릿 견본(R2-1), 퇴역 이름 재사용(R2-2 ③), 깨진 프로젝트
커밋 기록 복구(R2-4), main이 움직인 뒤 squash·merge·rebase(R2-6), 변이 공백(R2-8 ①~④).

각 테스트 docstring의 R2-n·V..은 리뷰 항목·변이 번호다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fleet.fleetfiles import SiteStatus
from fleet.publish import ProjectCommits, trust_base

from .kit import NoTokens, World, approve, make_world, pull, site_yaml

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "pureiron-fleet"
SITE_A_G2 = ("site-a", 2, "pureiron-site-a-g2.pib")
PROJECT_REF = "acme/pureiron-project-acme@v3"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def _main_moves(world: World) -> None:
    """PR을 연 뒤 다른 PR이 main에 먼저 들어간다(일반적인 운영 모양)."""
    other = world.merge_pr(6, {"sites/site-b.yaml": site_yaml("site-b", 1)})
    assert world.run(other) == 0


# ---------------------------------------------------------------- R2-1 템플릿 견본


def test_first_merge_on_template_tree_is_green(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-1: 템플릿 트리 전체를 첫 커밋으로 trust-base한 repo에서 첫 merge가 exit 0이다.

    견본이 `sites/` 아래(활성 경로)에 있으면 정합화가 미등록 현장으로 보고 매 run을 빨강으로 만든다.
    """
    files = {
        p.relative_to(TEMPLATE).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(TEMPLATE.rglob("*"))
        if p.is_file()
    }
    assert "examples/site.yaml" in files
    pushed = world.fleet.commit(files, "pureiron-fleet template")
    assert trust_base(world.config, pushed, NoTokens(), "template") == 0
    m1 = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(m1) == 0
    out = capsys.readouterr().out
    assert world.control.puts == [SITE_A_G2]
    assert "example" not in world.control.lookups
    assert "::error" not in out
    m2 = world.merge_pr(8, {"notes/unrelated.txt": "x"})
    assert world.run(m2) == 0


# ---------------------------------------------------------------- R2-2 ③ 퇴역 이름 재사용


def test_retired_name_reuse_is_never_published(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-2 ③ + R2-8 ③(V20): 퇴역 이름을 다시 쓴 활성 파일은 그 커밋에서도, 다음 run에서도 PUT 0."""
    old = (world.fleet.path / "sites" / "site-a.yaml").read_text(encoding="utf-8")
    retire = world.merge_pr(5, {"sites/site-a.yaml": None, "sites/.retired/site-a.yaml": old})
    assert world.run(retire) == 0
    capsys.readouterr()

    m1 = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(m1) == 1
    out = capsys.readouterr().out
    assert world.control.puts == []
    assert world.builder.requests == []
    # 구조 규칙(이 커밋 diff)과 트리 규칙(active_sites)이 겹쳐 두 번 보고하지 않는다.
    assert out.count("퇴역한 현장 이름 'site-a'") == 1

    m2 = world.merge_pr(8, {"notes/unrelated.txt": "x"})
    assert world.run(m2) == 1
    out = capsys.readouterr().out
    assert world.control.puts == []
    assert world.builder.requests == []
    assert "::error file=sites/site-a.yaml::E_FLEET 퇴역한 현장 이름 'site-a'" in out


# ---------------------------------------------------------------- R2-4 깨진 project_commits.json


def test_corrupt_project_commits_is_restarted(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-4: 깨진 기록은 경고와 함께 빈 기록으로 새로 시작해 덮어쓴다(다음 게시부터 감지가 돈다)."""
    file = ProjectCommits.of(world.config).file
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("{broken", encoding="utf-8")
    m1 = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(m1) == 0
    out = capsys.readouterr().out
    assert "직전 프로젝트 커밋 기록을 읽지 못했다" in out
    assert "깨진 프로젝트 커밋 기록을 새로 시작했다" in out
    commit = world.project.head()
    assert json.loads(file.read_text(encoding="utf-8")) == {PROJECT_REF: commit}

    m2 = world.merge_pr(8, {"sites/site-a.yaml": site_yaml("site-a", 3)})
    assert world.run(m2) == 0
    out = capsys.readouterr().out
    assert "읽지 못했다" not in out
    assert "새로 시작" not in out
    assert world.control.puts == [SITE_A_G2, ("site-a", 3, "pureiron-site-a-g3.pib")]


# ---------------------------------------------------------------- R2-6 main이 움직인 뒤 ⑤ 정상 경로


def test_squash_after_main_moved_is_approved(world: World) -> None:
    """R2-6: PR(2커밋)을 연 뒤 다른 PR이 먼저 merge → squash는 ⑤를 통과한다."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)}, commits=2)
    _main_moves(world)
    squashed = world.fleet.squash("pr-7")
    world.github.add(
        pull(7, merge_commit_sha=squashed, head_sha=head, commits=2),
        [approve("circulation95", head)],
        squashed,
    )
    assert world.run(squashed) == 0
    assert SITE_A_G2 in world.control.puts
    assert world.chain.last_approved() == squashed


def test_merge_commit_after_main_moved_is_approved(world: World) -> None:
    """R2-6: 같은 조건 → merge commit은 ⑤를 통과한다."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)}, commits=2)
    _main_moves(world)
    merged = world.fleet.merge("pr-7")
    world.github.add(
        pull(7, merge_commit_sha=merged, head_sha=head, commits=2),
        [approve("circulation95", head)],
        merged,
    )
    assert world.run(merged) == 0
    assert SITE_A_G2 in world.control.puts
    assert world.chain.last_approved() == merged


def test_rebase_two_commits_after_main_moved_is_approved(world: World) -> None:
    """R2-6: 같은 조건 → rebase 2커밋(committer 시각이 바뀐 새 커밋)도 ⑤를 통과한다."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)}, commits=2)
    _main_moves(world)
    rebased = world.fleet.rebase_onto_main("pr-7")
    assert len(rebased) == 2
    world.github.add(
        pull(7, merge_commit_sha=rebased[-1], head_sha=head, commits=2),
        [approve("circulation95", head)],
        *rebased,
    )
    assert world.run(rebased[-1]) == 0
    assert SITE_A_G2 in world.control.puts
    assert world.chain.last_approved() == rebased[-1]


# ---------------------------------------------------------------- R2-8 변이 공백


def test_chain_record_failure_blocks_publishing(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-8 ①(V13): 승인은 됐지만 사슬 **기록**(`last_approved` 교체)이 실패하면 게시하지 않는다."""

    def broken_write(target: Path, data: str) -> None:
        raise OSError(f"disk full: {target.name}")

    monkeypatch.setattr("fleet.publish._write_atomic", broken_write)
    m1 = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(m1) == 1
    out = capsys.readouterr().out
    assert "E_PUBLISH 승인 사슬을 기록하지 못했다" in out
    assert world.builder.requests == []
    assert world.control.puts == []
    assert world.chain.last_approved() == world.base


def test_unchanged_broken_active_file_fails_every_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-8 ②(V09): 이 커밋에서 안 바뀐 해석 불가 활성 파일도 main에 있는 동안 매 run `E_FLEET`.

    다른 현장은 그대로 게시한다.
    """
    broken = "site: site-c\ngeneration: zero\n"
    m1 = world.merge_pr(7, {"sites/site-c.yaml": broken})
    assert world.run(m1) == 1
    capsys.readouterr()

    m2 = world.merge_pr(8, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(m2) == 1
    out = capsys.readouterr().out
    assert "::error file=sites/site-c.yaml::E_FLEET" in out
    assert world.control.puts == [SITE_A_G2]
    world.control.status["site-a"] = SiteStatus(max_published_gen=2, current_product="2026.11.0")

    m3 = world.merge_pr(9, {"notes/unrelated.txt": "x"})
    assert world.run(m3) == 1
    assert "::error file=sites/site-c.yaml::E_FLEET" in capsys.readouterr().out
    assert world.control.puts == [SITE_A_G2]


def test_project_commits_record_failure_only_warns(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """R2-8 ④(V15): 태그 이동 기록 실패는 PUT 성공 뒤의 일 — 경고만, 게시 결과 PUBLISHED, exit 0."""

    def broken_record(self: ProjectCommits, ref: str, commit: str) -> str | None:
        raise OSError(f"read-only file system: {self.file.name} {ref} {commit}")

    monkeypatch.setattr(ProjectCommits, "record", broken_record)
    m1 = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(m1) == 0
    out = capsys.readouterr().out
    assert "::warning file=sites/site-a.yaml::프로젝트 커밋 기록을 남기지 못했다" in out
    assert "publish: 게시 1, 건너뜀 0, 최신 0, 실패 0" in out
    assert world.control.puts == [SITE_A_G2]
