"""승인 판정 표(★F2 ①~④): 순수 함수 `approval.decide`."""

from __future__ import annotations

import dataclasses

import pytest
from fleet.approval import (
    CHANGES_REQUESTED,
    DISMISSED,
    Commit,
    Evidence,
    PullRequest,
    Review,
    decide,
    parse_approvers,
)

from .kit import approve, pull


def sha(n: int) -> str:
    return f"{n:040x}"


M0, H0, H, S, X, S1, S2 = (sha(n) for n in (0xA0, 0xB0, 0xB1, 0xC0, 0xD0, 0xC1, 0xC2))
COMMENTED = "COMMENTED"


def merged_pr(**overrides: object) -> PullRequest:
    base = pull(7, merge_commit_sha=S, head_sha=H)
    return dataclasses.replace(base, **overrides)


def evidence(**overrides: object) -> Evidence:
    """정상: main M0(= 마지막 승인) ← PR #7(head H, 작성자 ws-blip) merge commit S.

    circulation95가 H에 승인.
    """
    base = Evidence(
        sha=S,
        on_main=True,
        ancestry=(Commit(S, (M0, H)), Commit(M0, (sha(0x9F),))),
        pulls=(merged_pr(),),
        associated={},
        reviews=(approve("circulation95", H),),
        approvers_text="# 승인자\ncirculation95\n",
        last_approved=M0,
        result_tree_matches=True,
    )
    return dataclasses.replace(base, **overrides)


def reasons(ev: Evidence) -> str:
    decision = decide(ev)
    assert not decision.approved
    return " | ".join(decision.reasons)


def test_normal_merge_commit_is_approved() -> None:
    decision = decide(evidence())
    assert decision.approved, decision.reasons
    assert (decision.pull, decision.base, decision.approvers) == (7, M0, ("circulation95",))


@pytest.mark.parametrize(
    ("reviews", "expected"),
    [
        ((), "유효 승인 0건(리뷰 없음)"),
        ((approve("circulation95", H0),), "오래된 승인"),
        (
            (approve("circulation95", H), approve("circulation95", H, CHANGES_REQUESTED)),
            "CHANGES_REQUESTED",
        ),
        ((approve("circulation95", H, DISMISSED),), "dismiss"),
        ((approve("outsider", H),), "allowed_approvers.txt(merge 직전 main) 밖"),
        ((approve("ws-blip", H),), "PR 작성자 자기 승인"),
        ((Review("circulation95", COMMENTED, H),), "리뷰 없음"),
    ],
    ids=[
        "no-reviews",
        "stale",
        "changes-requested",
        "dismissed",
        "outsider",
        "self",
        "comment-only",
    ],
)
def test_review_rules_reject(reviews: tuple[Review, ...], expected: str) -> None:
    assert expected in reasons(evidence(reviews=reviews))


def test_comment_after_approval_keeps_approval_and_login_case_is_ignored() -> None:
    reviews = (approve("Circulation95", H), Review("circulation95", COMMENTED, H))
    assert decide(evidence(reviews=reviews)).approved


def test_one_valid_approver_among_invalid_is_enough() -> None:
    reviews = (approve("outsider", H), approve("ws-blip", H), approve("circulation95", H))
    decision = decide(evidence(reviews=reviews))
    assert decision.approved
    assert decision.approvers == ("circulation95",)


def test_approver_list_is_read_from_merge_base_not_from_the_pr() -> None:
    # PR이 목록에 newbie를 넣었어도 판정 입력은 merge base의 목록(newbie 없음)이다.
    text = "circulation95\n"
    assert "newbie: allowed_approvers.txt" in reasons(
        evidence(reviews=(approve("newbie", H),), approvers_text=text)
    )


def test_direct_push_without_pr() -> None:
    ev = evidence(pulls=(), ancestry=(Commit(S, (M0,)), Commit(M0, ())))
    assert "직접 push — " in reasons(ev)


def test_pr_head_pushed_directly_to_main() -> None:
    # PR head H를 main에 직접 push: H에 연결된 PR은 있으나 merge_commit_sha가 H가 아니다.
    ev = evidence(sha=H, pulls=(merged_pr(merge_commit_sha=S),), ancestry=(Commit(H, (M0,)),))
    assert "머리 커밋을 main에 직접 push" in reasons(ev)


@pytest.mark.parametrize(
    ("reviews", "expected"),
    [
        ((approve("circulation95", H),), None),
        ((), "PR #7 유효 승인 0건(리뷰 없음)"),
    ],
    ids=["approved-head", "unapproved-head-S4b"],
)
def test_pr_head_fast_forwarded_to_main(reviews: tuple[Review, ...], expected: str | None) -> None:
    # GitHub은 PR 머리 커밋 H를 main으로 fast-forward하면 PR을 merged로 바꾸고 merge_commit_sha = H로
    # 둔다(M4-2 리허설 §3-3 S4b) — ①은 버튼 merge와 구분하지 못한다. 승인 없는 머리는 ②가 막고,
    # 승인된 최종 head면 결과 트리가 merge-tree(M0, H) = H 트리라(⑤ 참) 내용이 승인본과 같아 통과한다.
    ev = evidence(
        sha=H,
        pulls=(merged_pr(merge_commit_sha=H),),
        ancestry=(Commit(H, (M0,)), Commit(M0, (sha(0x9F),))),
        reviews=reviews,
    )
    decision = decide(ev)
    if expected is None:
        assert decision.approved, decision.reasons
        assert (decision.pull, decision.base) == (7, M0)
    else:
        assert expected in reasons(ev)


def test_unmerged_pr_is_not_an_approval() -> None:
    assert "직접 push" in reasons(evidence(pulls=(merged_pr(merged=False),)))


def test_chain_broken_by_earlier_direct_push() -> None:
    # 마지막 승인 M0 뒤에 직접 push X가 끼고, 그 위에 정상 merge S가 왔다.
    ev = evidence(ancestry=(Commit(S, (X, H)), Commit(X, (M0,))))
    assert f"승인 사슬 끊김: merge 직전 main {X} != 마지막 승인 {M0}" in reasons(ev)


def test_no_trust_base_yet() -> None:
    assert "trust-base" in reasons(evidence(last_approved=None))


def test_rerun_of_recorded_sha_is_approved() -> None:
    assert decide(evidence(last_approved=S)).approved


def test_sha_not_on_main() -> None:
    assert "main 브랜치 이력에 없다" in reasons(evidence(on_main=False))


def test_pr_into_other_branch() -> None:
    assert "base가 develop" in reasons(evidence(pulls=(merged_pr(base_ref="develop"),)))


def test_merge_commit_second_parent_must_be_pr_head() -> None:
    ev = evidence(ancestry=(Commit(S, (M0, H0)), Commit(M0, ())))
    assert "둘째 부모" in reasons(ev)


def test_missing_or_malformed_approver_file() -> None:
    assert "allowed_approvers.txt가 없다" in reasons(evidence(approvers_text=None))
    assert "2행: GitHub login이 아니다" in reasons(
        evidence(approvers_text="circulation95\nbad login\n")
    )
    assert "승인자가 없다" in reasons(evidence(approvers_text="# 비어 있음\n"))


def test_squash_merge_uses_first_parent() -> None:
    # PR 커밋 3개를 squash: main에는 S 하나(부모 M0). M0는 다른 PR(#6)에 연결.
    ev = evidence(
        ancestry=(Commit(S, (M0,)), Commit(M0, (sha(0x9F),))),
        pulls=(merged_pr(commits=3),),
        associated={M0: frozenset({6})},
    )
    decision = decide(ev)
    assert decision.approved, decision.reasons
    assert decision.base == M0


def test_rebase_merge_walks_back_pr_commits() -> None:
    # PR 커밋 3개 rebase: S(=merge_commit_sha) ← S2 ← S1 ← M0.
    ev = evidence(
        ancestry=(Commit(S, (S2,)), Commit(S2, (S1,)), Commit(S1, (M0,)), Commit(M0, ())),
        pulls=(merged_pr(commits=3),),
        associated={S2: frozenset({7}), S1: frozenset({7})},
    )
    decision = decide(ev)
    assert decision.approved, decision.reasons
    assert decision.base == M0


def test_rebase_merge_with_unconnected_commit_is_rejected() -> None:
    ev = evidence(
        ancestry=(Commit(S, (S2,)), Commit(S2, (S1,)), Commit(S1, (M0,)), Commit(M0, ())),
        pulls=(merged_pr(commits=3),),
        associated={S2: frozenset({7})},
    )
    assert "rebase merge로 보이나" in reasons(ev)


def test_squash_on_top_of_direct_push_breaks_chain() -> None:
    ev = evidence(
        ancestry=(Commit(S, (X,)), Commit(X, (M0,)), Commit(M0, ())),
        pulls=(merged_pr(commits=2),),
        associated={},
    )
    assert "승인 사슬 끊김" in reasons(ev)


TREE_MISMATCH = "merge 결과 트리가 merge 직전 main"


def test_evil_merge_commit_with_unreviewed_content_is_rejected() -> None:
    # 부모 = (마지막 승인 M0, 승인된 head H)인 손 merge commit에 리뷰 안 된 파일을 덧붙였다:
    # ①~④는 모두 통과하지만 결과 트리 != merge-tree(M0, H)(⑤).
    decision = decide(evidence(result_tree_matches=False))
    assert not decision.approved
    assert decision.reasons == (
        f"{TREE_MISMATCH} {M0} + PR #7 승인 head {H}의 merge 결과와 다르다 — 리뷰받지 않은 "
        "내용(손 merge commit·충돌 해결 push 등)",
    )


def test_squash_with_forged_tree_is_rejected() -> None:
    ev = evidence(
        ancestry=(Commit(S, (M0,)), Commit(M0, (sha(0x9F),))),
        pulls=(merged_pr(commits=3),),
        result_tree_matches=False,
    )
    assert TREE_MISMATCH in reasons(ev)


def test_rerun_of_recorded_sha_still_checks_tree() -> None:
    assert TREE_MISMATCH in reasons(evidence(last_approved=S, result_tree_matches=False))


def test_parse_approvers_comments_and_case() -> None:
    logins, problems = parse_approvers("Circulation95  # 대표\n\n# 주석\nws-blip\n")
    assert logins == {"circulation95", "ws-blip"}
    assert problems == []
