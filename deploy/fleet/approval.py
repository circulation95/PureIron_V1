"""publish 승인 판정(M4-2 ★F2, foundation PD-23 ③, plan C9) — 순수 함수 `decide`, I/O 없음.

main에 들어온 커밋 `sha`를 서명해도 되는지. 하나라도 어기면 `E_APPROVAL`(사유마다 한 줄),
서명하지 않는다.

① `sha`는 **merge된 PR의 `merge_commit_sha`와 정확히 같다**(직접 push·PR 머리 커밋 직접 push 차단).
   PR base 브랜치는 main, `sha`는 main 이력 안에 있어야 한다.
② 그 PR의 **최종 head sha**에 대한 승인이 1명 이상: 리뷰어별 마지막 상태(APPROVED·CHANGES_REQUESTED·
   DISMISSED만 상태를 바꾼다, COMMENTED·PENDING은 무시)가 APPROVED이고 `commit_id == head sha`
   (Free에는 "stale review dismiss"가 없다 — 승인 뒤 커밋이 붙으면 그 승인은 무효), 리뷰어 ∈
   `allowed_approvers.txt`, 리뷰어 ≠ PR 작성자. login 비교는 대소문자 무시(GitHub login 규칙).
③ `allowed_approvers.txt`는 **merge 직전 main(merge base)**의 것 — PR이 목록을 바꿔 자기 승인자를
   넣을 수 없다. 호출자가 `merge_base()`로 base를 구해 그 커밋의 파일을 `approvers_text`로 넘긴다.
④ **승인 사슬**: merge base == 호스트에 기록된 마지막 승인 sha(`last_approved`). 직접 push가 한 번
   끼면 이후 merge도 그 내용을 싣고 있으므로 거부한다. 같은 sha 재실행(`sha == last_approved`)은
   ①②③⑤만 다시 본다.
⑤ **트리 동치**: `sha`의 트리 == merge base와 승인된 head를 3-way merge한 트리
   (`git merge-tree --write-tree <base> <head>`, 충돌 없음). 손으로 만든 merge commit(부모 = base·
   승인된 head)에 리뷰받지 않은 내용을 덧붙이거나, 로컬에서 충돌을 풀어 push한 경우를 막는다
   (squash·merge·rebase 모두).
   계산은 가장자리(`publish.gather`)가 하고 여기에는 결과 bool만 들어온다.

merge base(merge 직전 main):
- merge commit(부모 2개): 첫 부모. 둘째 부모는 PR head sha여야 한다.
- squash(부모 1개): 첫 부모.
- rebase(부모 1개, PR 커밋 N개): main에 N개가 새로 생긴다 — `sha~1 … sha~(N-1)`이 모두 같은 PR에
  연결된 커밋(`commits/{c}/pulls`)이어야 하고 base = `sha~N`. 일부만 연결되면 거부(실패 쪽으로
  닫음).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
DISMISSED = "DISMISSED"
STATEFUL = frozenset({APPROVED, CHANGES_REQUESTED, DISMISSED})
"""리뷰어 상태를 바꾸는 리뷰 상태. COMMENTED·PENDING은 앞 상태를 그대로 둔다."""
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


@dataclass(frozen=True, slots=True)
class Review:
    reviewer: str
    state: str
    commit_id: str | None


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    author: str
    merged: bool
    merge_commit_sha: str | None
    head_sha: str
    base_ref: str
    commits: int


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    parents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Evidence:
    """판정 입력(모두 GitHub API·git에서 그 sha로 다시 받은 값)."""

    sha: str
    on_main: bool
    """`sha`가 원격 main 이력 안에 있는가(`git merge-base --is-ancestor sha main`)."""
    ancestry: tuple[Commit, ...]
    """`sha`부터 첫 부모를 따라간 커밋들([0] = sha). rebase면 PR 커밋 수 + 1개 이상."""
    pulls: tuple[PullRequest, ...]
    """`commits/{sha}/pulls`의 PR(전체 객체)."""
    associated: Mapping[str, frozenset[int]]
    """rebase 판정용: `sha~1`… 커밋 → 연결된 PR 번호들."""
    reviews: tuple[Review, ...]
    """`select_pull`이 고른 PR의 리뷰(API 순서 = 제출 순)."""
    approvers_text: str | None
    """merge base 커밋의 `allowed_approvers.txt`(없으면 None)."""
    last_approved: str | None
    """호스트 승인 사슬 기록(없으면 None — `publish.py trust-base` 먼저)."""
    result_tree_matches: bool
    """⑤ `sha^{tree}` == `merge-tree --write-tree <merge base> <PR head>`(충돌이면 False).

    merge base를 구하지 못했거나 계산하지 않았으면 False(그때는 다른 사유로 이미 거부된다)."""
    main_branch: str = "main"


@dataclass(frozen=True, slots=True)
class Decision:
    approved: bool
    reasons: tuple[str, ...]
    pull: int | None = None
    base: str | None = None
    approvers: tuple[str, ...] = field(default=())


def parse_approvers(text: str) -> tuple[frozenset[str], list[str]]:
    """`allowed_approvers.txt`(한 줄 한 login, `#` 뒤는 주석) → (소문자 login 집합, 형식 오류)."""
    logins: set[str] = set()
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        login = line.split("#", 1)[0].strip()
        if not login:
            continue
        if not _LOGIN.fullmatch(login):
            problems.append(f"allowed_approvers.txt {number}행: GitHub login이 아니다 {login!r}")
            continue
        logins.add(login.lower())
    return frozenset(logins), problems


def select_pull(sha: str, pulls: Sequence[PullRequest]) -> PullRequest | None:
    """`sha`를 merge commit으로 만든 merge된 PR(①)."""
    for pull in pulls:
        if pull.merged and pull.merge_commit_sha == sha:
            return pull
    return None


def merge_base(evidence: Evidence, pull: PullRequest) -> tuple[str | None, str | None]:
    """merge 직전 main 커밋(base, 문제). 문제가 있으면 base는 None."""
    ancestry = evidence.ancestry
    if not ancestry or ancestry[0].sha != evidence.sha:
        return None, "커밋 이력을 읽지 못했다"
    parents = ancestry[0].parents
    if len(parents) == 2:
        if parents[1] != pull.head_sha:
            return None, (
                f"merge commit 둘째 부모 {parents[1]} != PR #{pull.number} 최종 head "
                f"{pull.head_sha}"
            )
        return parents[0], None
    if len(parents) != 1:
        return None, f"부모가 {len(parents)}개인 커밋은 판정하지 않는다"
    return _single_parent_base(evidence, pull)


def _single_parent_base(evidence: Evidence, pull: PullRequest) -> tuple[str | None, str | None]:
    """squash(base = 첫 부모) 또는 rebase(PR 커밋 N개 → base = sha~N)."""
    ancestry = evidence.ancestry
    rebased = 0
    for commit in ancestry[1 : pull.commits]:
        if pull.number not in evidence.associated.get(commit.sha, frozenset()):
            break
        if len(commit.parents) != 1:
            return None, f"rebase 커밋 {commit.sha}의 부모가 {len(commit.parents)}개"
        rebased += 1
    if rebased and rebased != pull.commits - 1:
        return None, (
            f"rebase merge로 보이나 PR #{pull.number} 커밋 {pull.commits}개 중 "
            f"{rebased + 1}개만 main에 이어져 있다"
        )
    if len(ancestry) <= rebased + 1:
        return None, "merge base까지 커밋 이력을 읽지 못했다"
    return ancestry[rebased + 1].sha, None


def _no_pull_reason(sha: str, pulls: Sequence[PullRequest]) -> str:
    heads = [p for p in pulls if p.head_sha == sha]
    if heads:
        p = heads[0]
        return (
            f"PR #{p.number} 머리 커밋을 main에 직접 push했다"
            f"(merge_commit_sha {p.merge_commit_sha} != {sha})"
        )
    if pulls:
        numbers = ", ".join(f"#{p.number}" for p in pulls)
        return f"연결된 PR({numbers})의 merge_commit_sha가 {sha}가 아니다 — 직접 push"
    return f"직접 push — {sha}를 만든 merge된 PR이 없다"


def _approvals(
    pull: PullRequest, reviews: Sequence[Review], approvers: frozenset[str]
) -> tuple[list[str], list[str]]:
    """(유효 승인자, 무효 사유)."""
    last: dict[str, Review] = {}
    for review in reviews:
        if review.state in STATEFUL:
            last[review.reviewer.lower()] = review
    author = pull.author.lower()
    valid: list[str] = []
    rejected: list[str] = []
    for login, review in sorted(last.items()):
        if review.state != APPROVED:
            if review.state == DISMISSED:
                rejected.append(f"{login}: 승인이 dismiss됨")
            else:
                rejected.append(f"{login}: 마지막 상태 {review.state}")
        elif review.commit_id != pull.head_sha:
            rejected.append(
                f"{login}: 오래된 승인(commit {review.commit_id} != 최종 head {pull.head_sha})"
            )
        elif login == author:
            rejected.append(f"{login}: PR 작성자 자기 승인")
        elif login not in approvers:
            rejected.append(f"{login}: allowed_approvers.txt(merge 직전 main) 밖")
        else:
            valid.append(login)
    return valid, rejected


def decide(evidence: Evidence) -> Decision:
    sha = evidence.sha
    reasons: list[str] = []
    if not evidence.on_main:
        reasons.append(f"{sha}가 {evidence.main_branch} 브랜치 이력에 없다")
    if evidence.last_approved is None:
        reasons.append("승인 사슬 기준이 없다 — 사람이 확인한 sha로 publish.py trust-base 먼저")
    pull = select_pull(sha, evidence.pulls)
    if pull is None:
        reasons.append(_no_pull_reason(sha, evidence.pulls))
        return Decision(False, tuple(reasons))
    if pull.base_ref != evidence.main_branch:
        reasons.append(f"PR #{pull.number} base가 {pull.base_ref}(≠ {evidence.main_branch})")
    base, problem = merge_base(evidence, pull)
    if problem is not None:
        reasons.append(problem)
    else:
        if evidence.last_approved not in (None, sha, base):
            reasons.append(
                f"승인 사슬 끊김: merge 직전 main {base} != 마지막 승인 {evidence.last_approved} "
                "— 그 사이 승인 없이 들어온 커밋이 있다(README E_APPROVAL 복구)"
            )
        if not evidence.result_tree_matches:
            reasons.append(
                f"merge 결과 트리가 merge 직전 main {base} + PR #{pull.number} 승인 head "
                f"{pull.head_sha}의 merge 결과와 다르다 — 리뷰받지 않은 내용(손 merge commit·"
                "충돌 해결 push 등)"
            )
    if evidence.approvers_text is None:
        reasons.append("merge 직전 main에 allowed_approvers.txt가 없다")
        valid: list[str] = []
    else:
        approvers, problems = parse_approvers(evidence.approvers_text)
        reasons.extend(problems)
        if not approvers:
            reasons.append("allowed_approvers.txt에 승인자가 없다")
        valid, rejected = _approvals(pull, evidence.reviews, approvers)
        if not valid:
            detail = "; ".join(rejected) if rejected else "리뷰 없음"
            reasons.append(f"PR #{pull.number} 유효 승인 0건({detail})")
    return Decision(
        approved=not reasons,
        reasons=tuple(reasons),
        pull=pull.number,
        base=base,
        approvers=tuple(valid),
    )
