"""리뷰 r1(`review-m4-2.md`) 회귀: PoC 5개(단언을 뒤집어 옮김), 변이 공백 T1~T3, L1·L2·L4.

각 테스트 docstring의 H1·M3·M4·M5·L1… 은 리뷰 항목 번호다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fleet.fleetfiles import SiteStatus
from fleet.github import Git, GitError
from fleet.publish import ProjectCommits, trust_base

from .kit import NoTokens, World, approve, git, make_world, pull, site_yaml

TREE_MISMATCH = "E_APPROVAL merge 결과 트리가 merge 직전 main"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


def _add_unreviewed_site_b(world: World) -> None:
    (world.fleet.path / "sites" / "site-b.yaml").write_text(
        site_yaml("site-b", 1), encoding="utf-8", newline="\n"
    )
    git(world.fleet.path, "add", "sites/site-b.yaml")


def test_evil_merge_commit_is_rejected(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    """H1(PoC test_evil_merge_commit_signs_unreviewed_site): 승인된 head를 둘째 부모로 둔 손 merge
    commit에 리뷰 안 된 site-b를 덧붙이면 트리 동치(⑤)에서 거부 — 서명·게시 0."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    git(world.fleet.path, "merge", "-q", "--no-ff", "--no-commit", "pr-7")
    _add_unreviewed_site_b(world)
    git(world.fleet.path, "commit", "-q", "-m", "Merge pull request #7")
    evil = world.fleet.head()
    world.github.add(
        pull(7, merge_commit_sha=evil, head_sha=head), [approve("circulation95", head)], evil
    )
    assert world.run(evil) == 1
    assert (world.builder.requests, world.control.puts) == ([], [])
    assert TREE_MISMATCH in capsys.readouterr().out
    assert world.chain.last_approved() == world.base


def test_squash_with_extra_content_is_rejected(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """H1: squash 결과에 승인 밖 파일을 덧붙인 커밋(head는 main 이력에 없어 따로 받는다)."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)}, commits=2)
    git(world.fleet.path, "merge", "-q", "--squash", "pr-7")
    _add_unreviewed_site_b(world)
    git(world.fleet.path, "commit", "-q", "-m", "Squash PR #7")
    forged = world.fleet.head()
    world.github.add(
        pull(7, merge_commit_sha=forged, head_sha=head, commits=2),
        [approve("circulation95", head)],
        forged,
    )
    assert world.run(forged) == 1
    assert world.builder.requests == []
    assert TREE_MISMATCH in capsys.readouterr().out


def test_conflict_resolved_locally_is_rejected(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """H1: 승인 head가 main과 충돌해 로컬에서 풀고 push한 merge commit — merge-tree 충돌 = 거부."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2, product="2026.11.2")})
    other = world.merge_pr(6, {"sites/site-a.yaml": site_yaml("site-a", 2, product="2026.11.1")})
    assert world.run(other) == 0
    result = git(world.fleet.path, "merge", "--no-ff", "--no-commit", "pr-7", check=False)
    assert "CONFLICT" in result
    (world.fleet.path / "sites" / "site-a.yaml").write_text(
        site_yaml("site-a", 3), encoding="utf-8", newline="\n"
    )
    git(world.fleet.path, "add", "sites/site-a.yaml")
    git(world.fleet.path, "commit", "-q", "-m", "Merge pull request #7")
    resolved = world.fleet.head()
    world.github.add(
        pull(7, merge_commit_sha=resolved, head_sha=head),
        [approve("circulation95", head)],
        resolved,
    )
    world.builder.requests.clear()
    assert world.run(resolved) == 1
    assert world.builder.requests == []
    assert TREE_MISMATCH in capsys.readouterr().out


def test_pull_head_falls_back_to_refs_pull(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """H1: head sha를 바로 못 받으면 `refs/pull/<n>/head`로 받는다(브랜치를 지운 squash PR)."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)}, commits=2)
    squashed = world.fleet.squash("pr-7")
    git(world.fleet.path, "update-ref", "refs/pull/7/head", head)
    git(world.fleet.path, "branch", "-q", "-D", "pr-7")
    world.github.add(
        pull(7, merge_commit_sha=squashed, head_sha=head, commits=2),
        [approve("circulation95", head)],
        squashed,
    )
    original = Git.fetch
    seen: list[tuple[str, ...]] = []

    def fetch(self: Git, repo: str, *refspecs: str, depth: int | None = None) -> None:
        seen.append(refspecs)
        if refspecs == (head,):
            raise GitError("not our ref")  # GitHub가 sha 직접 fetch를 거부한 경우
        original(self, repo, *refspecs, depth=depth)

    monkeypatch.setattr(Git, "fetch", fetch)
    assert world.run(squashed) == 0, capsys.readouterr().out
    assert ("+refs/pull/7/head:refs/remotes/pull/7",) in seen
    assert world.control.puts == [("site-a", 2, "pureiron-site-a-g2.pib")]


def test_unfetchable_pull_head_fails_closed(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)}, commits=2)
    squashed = world.fleet.squash("pr-7")
    world.github.add(
        pull(7, merge_commit_sha=squashed, head_sha=head, commits=2),
        [approve("circulation95", head)],
        squashed,
    )
    original = Git.fetch

    def fetch(self: Git, repo: str, *refspecs: str, depth: int | None = None) -> None:
        if refspecs[0] == head or refspecs[0].startswith("+refs/pull/"):
            raise GitError("not our ref")
        original(self, repo, *refspecs, depth=depth)

    monkeypatch.setattr(Git, "fetch", fetch)
    assert world.run(squashed) == 1
    assert world.builder.requests == []
    assert "E_APPROVAL 판정 입력을 받지 못했다: not our ref" in capsys.readouterr().out


def test_same_gen_content_change_fails_with_e_gen(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """M3(PoC test_same_gen_content_change_is_silently_skipped): gen을 안 올린 내용 변경 →
    publish도 `new == old + 1`을 본다 → E_GEN, 빌드 0, exit 1."""
    merged = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 1, product="2026.11.1")})
    assert world.run(merged) == 1
    assert world.builder.requests == []
    out = capsys.readouterr().out
    assert "::error file=sites/site-a.yaml::E_GEN generation은 정확히 +1: 1 → 2" in out


def test_failed_site_is_published_by_a_later_run(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """M4(PoC test_failed_site_is_never_retried_after_next_merge): M1의 미등록 실패 → 등록 →
    그 사이 M2(sites 무관) → main 머리(M2) 재실행이 정합화로 site-b gen 1을 게시."""
    world.control.status["site-b"] = None
    m1 = world.merge_pr(7, {"sites/site-b.yaml": site_yaml("site-b", 1)})
    assert world.run(m1) == 1
    m2 = world.merge_pr(8, {"notes.txt": "x"})
    assert world.run(m2) == 1  # 아직 미등록: 뒤 merge도 계속 빨강(조용히 넘어가지 않는다)
    world.control.status["site-b"] = SiteStatus(None, None)  # 이제 등록됨
    capsys.readouterr()
    assert world.run(m2) == 0
    assert world.control.puts == [("site-b", 1, "pureiron-site-b-g1.pib")]
    assert world.run(m1) == 1  # M1 job 재실행은 여전히 사슬 끊김 — README: main 머리로 dispatch


def test_unapproved_merge_recovery_a_force_push_and_new_pr(world: World) -> None:
    """M5(PoC test_readme_recovery_new_pr_after_unapproved_merge_does_not_work) 복구 (a):
    main을 마지막 승인으로 force push → 같은 브랜치로 새 PR(merge된 PR은 다시 열 수 없다) → 승인 →
    merge → 게시."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    m1 = world.fleet.merge("pr-7")
    world.github.add(pull(7, merge_commit_sha=m1, head_sha=head), [], m1)
    assert world.run(m1) == 1
    git(world.fleet.path, "reset", "-q", "--hard", world.base)  # = force push main
    again = world.fleet.merge("pr-7", "Merge pull request #9")  # 새 PR = 새 merge commit
    world.github.add(
        pull(9, merge_commit_sha=again, head_sha=head), [approve("circulation95", head)], again
    )
    assert world.run(again) == 0
    assert world.control.puts == [("site-a", 2, "pureiron-site-a-g2.pib")]


def test_unapproved_merge_recovery_b_trust_base_then_next_merge(world: World) -> None:
    """M5 복구 (b): 사람이 M1을 검토하고 trust-base → 다음 승인 merge(빈 PR이어도)가 정합화로
    M1의 site-a gen 2를 게시한다(gen 재증가 불필요). 옛 README 절차(바로 새 PR)는 사슬 끊김."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    m1 = world.fleet.merge("pr-7")
    world.github.add(pull(7, merge_commit_sha=m1, head_sha=head), [], m1)
    assert world.run(m1) == 1
    assert trust_base(world.config, m1, NoTokens(), "reviewed-m1") == 0
    m2 = world.merge_pr(8, {})
    assert world.run(m2) == 0
    assert world.control.puts == [("site-a", 2, "pureiron-site-a-g2.pib")]


def test_trust_base_repairs_corrupt_chain_file(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """L1(PoC test_trust_base_cannot_repair_corrupt_chain_file): 깨진 `last_approved`도 덮어쓴다."""
    world.chain.file.write_text("garbage\n", encoding="utf-8")
    assert trust_base(world.config, world.base, NoTokens(), "repair") == 0
    assert world.chain.last_approved() == world.base
    log = (world.chain.directory / "approved.log").read_text(encoding="utf-8")
    assert "trust-base previous=<unreadable> reason=repair" in log
    assert "이전 <unreadable>" in capsys.readouterr().out


def test_corrupt_chain_file_blocks_run(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    """T2(변이 M23): `last_approved` 형식 오류 → E_APPROVAL, 빌드 0."""
    world.chain.file.write_text("garbage\n", encoding="utf-8")
    merged = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(merged) == 1
    assert world.builder.requests == []
    out = capsys.readouterr().out
    assert "E_APPROVAL 판정 입력을 받지 못했다" in out
    assert "not a commit sha" in out


def test_merge_commit_outside_main_is_rejected(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """T1(변이 M17): main 밖 브랜치에서 만든 merge commit(가짜 GitHub는 merged로 답함) →
    원격 main 조상 판정이 거부."""
    head = world.open_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    world.fleet.branch("side", "main")
    side = world.fleet.merge("pr-7")
    world.fleet.switch("main")
    world.github.add(
        pull(7, merge_commit_sha=side, head_sha=head), [approve("circulation95", head)], side
    )
    assert world.run(side) == 1
    assert world.builder.requests == []
    assert f"E_APPROVAL {side}가 main 브랜치 이력에 없다" in capsys.readouterr().out


def test_trust_base_rejects_sha_outside_main(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """T3(변이 M24): trust-base에 main 밖 커밋 → exit 1, 사슬 그대로."""
    world.fleet.branch("side", "main")
    off_main = world.fleet.commit({"sites/site-b.yaml": site_yaml("site-b", 1)}, "off main")
    world.fleet.switch("main")
    assert trust_base(world.config, off_main, NoTokens(), "nope") == 1
    assert world.chain.last_approved() == world.base
    assert f"E_APPROVAL {off_main}가 main 이력에 없다" in capsys.readouterr().out


def test_project_tag_move_is_warned(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    """L2: metrics에 프로젝트 커밋, 같은 (repo, 태그)가 다른 커밋을 가리키면 경고."""
    m1 = world.merge_pr(7, {"sites/site-a.yaml": site_yaml("site-a", 2)})
    assert world.run(m1) == 0
    first = world.project.head("v3")
    commits = ProjectCommits.of(world.config)
    assert commits.previous("acme/pureiron-project-acme@v3") == first
    world.project.commit({"notes.txt": "moved"}, "moved")
    git(world.project.path, "tag", "-f", "v3")
    world.control.status["site-a"] = SiteStatus(2, "2026.11.0")
    m2 = world.merge_pr(8, {"sites/site-a.yaml": site_yaml("site-a", 3)})
    capsys.readouterr()
    assert world.run(m2) == 0
    moved = world.project.head("v3")
    out = capsys.readouterr().out
    assert (
        f"::warning file=sites/site-a.yaml::프로젝트 태그 이동: acme/pureiron-project-acme@v3 "
        f"직전 게시 {first} → 지금 {moved}"
    ) in out
    assert commits.previous("acme/pureiron-project-acme@v3") == moved


def test_unexpected_error_fails_that_site_only_and_is_fenced(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """L4·N1: 빌더가 목록 밖 예외를 던져도 그 현장만 E_PUBLISH, 다른 현장은 계속.
    traceback은 stop-commands로 감싸 workflow 명령으로 해석되지 않는다."""
    world.builder.fail["site-a"] = RuntimeError("boom\n::add-mask::x")
    merged = world.merge_pr(
        7,
        {"sites/site-a.yaml": site_yaml("site-a", 2), "sites/site-b.yaml": site_yaml("site-b", 1)},
    )
    assert world.run(merged) == 1
    assert world.control.puts == [("site-b", 1, "pureiron-site-b-g1.pib")]
    lines = capsys.readouterr().out.splitlines()
    assert (
        "::error file=sites/site-a.yaml::E_PUBLISH 예상 밖 오류(RuntimeError): boom%0A::add-mask::x"
        in lines
    )
    start = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
    token = lines[start].removeprefix("::stop-commands::")
    end = lines.index(f"::{token}::")
    assert "::add-mask::x" in lines[start + 1 : end]  # 날 줄은 울타리 안에만 있다
    assert "::add-mask::x" not in lines[:start] + lines[end + 1 :]
