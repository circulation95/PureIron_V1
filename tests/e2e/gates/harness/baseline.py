"""fleet-test 기준선·정리·사슬과 acme-test G2 태그(M6-4a A5 — 계획 P2·P8·★P3).

- `clear_leftovers`(R5): 사람이 연 PR이 있으면 아무것도 쓰지 않고 `InfraError`(건너뜀), 남은 bot PR
  (`[g2-nightly`)은 닫고 `g2/*` 브랜치는 지운다.
- `reconcile`(R6): 승인 사슬 `last_approved`(publish 호스트)와 main을 맞춘다 — 되돌림 또는
trust-base, 기록(사유)을 돌려준다.
- `ensure_project_tag`(★P3): G2 트리(absorbs 비움)의 content_hash로 acme-test 태그
`v1<15자리>`(fleet `project.version` 패턴 `^v[1-9][0-9]*$`), 없으면 `g2/base`에 그 트리만 담은 커밋
+ 태그.
- `baseline`(P2): site-a gen+1(BASE)·site-b gen+1(없으면 1, NEXT), 같은 G2 태그, 늘 열린 1회 창의
bot PR → check-gen → 승인 → 머리 승인 확인 뒤 merge → publish가 새 중앙에 둘 다 게시. check-gen
실패· publish 판정 실패는 제품(`E2EError`), GitHub 대기 초과는 `FleetError`(infra).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from harness import fleet, g2, judges
from harness.context import E2EError, InfraError
from harness.fleet import Repo
from pureiron_project import canonical
from pureiron_project.project import load_project

PROJECT_BASE_BRANCH = "g2/base"
SITES = ("site-a", "site-b")
BASELINE_WINDOW = (timedelta(hours=-1), timedelta(days=1))
"""기준선 파일의 1회 창 — 늘 열려 있다(스키마상 `window`는 필수, 계획의 "창 없음")."""
PROJECT_TAG_PREFIX = "v1"
"""G2 기준 트리 태그 = `v1` + content_hash 앞 12 hex의 10진(15자리 0채움). fleet `project.version`
패턴 `^v[1-9][0-9]*$`을 따른다(버전은 문자열로만 비교된다 — 순서·범위 규칙 없음). 4b 흡수 트리
태그는 `v2` + 같은 방식(기준 트리 태그와 겹치지 않는다)."""


# ---------------------------------------------------------------- ③ 정리·사슬(P8)


def clear_leftovers(fleet_repo: Repo) -> dict[str, object]:
    """사람이 연 PR(제목이 `[g2-nightly`로 시작하지 않음)이 있으면 아무것도 쓰지 않고 `InfraError`
    (R5 — 건너뜀). 남은 bot PR은 닫고 `g2/*` 브랜치는 지운다."""
    pulls = fleet_repo.open_pulls()
    human = [p for p in pulls if not str(p.get("title") or "").startswith(fleet.BOT_TITLE)]
    if human:
        numbers = ", ".join(f"#{p.get('number')}" for p in human)
        raise InfraError(f"{fleet_repo.name} has open PR(s) not made by the nightly: {numbers}")
    closed: list[int] = []
    for pull in pulls:
        fleet_repo.close_pull(int(str(pull.get("number"))))
        closed.append(int(str(pull.get("number"))))
    deleted = fleet_repo.branches(fleet.BOT_BRANCH)
    for branch in deleted:
        fleet_repo.delete_branch(branch)
    return {"closed": closed, "deleted": deleted}


def approved_merge(fleet_repo: Repo, sha: str) -> int | None:
    """`sha`가 승인된(머리 승인) PR의 merge 커밋이면 그 PR 번호."""
    for pull in fleet_repo.pulls_of_commit(sha):
        if pull.get("merge_commit_sha") != sha or not pull.get("merged_at"):
            continue
        number = int(str(pull.get("number")))
        head = str(cast(dict[str, object], pull.get("head") or {}).get("sha") or "")
        if fleet.approved_at_head(fleet_repo.reviews(number), head, fleet_repo.approver):
            return number
    return None


def reconcile(
    fleet_repo: Repo,
    read_chain: Callable[[], str | None],
    trust_base: Callable[[str, str], dict[str, object]],
    run_id: str,
) -> dict[str, object]:
    """승인 사슬 `last_approved`와 fleet-test main을 맞춘다(R6):
    - 같으면 그대로.
    - main 머리가 승인된 PR의 merge(사슬만 못 남김)면 `trust-base main`.
    - 아니면(PR 없음·승인 없음) main을 `last_approved`로 force 되돌림 → 그 push가 띄운 publish run을
      기다린다(되돌린 커밋이 PR 승인 커밋이면 success, 아니면 빨강이어도 사슬은 그대로 — M4-2 §3-4)
      → 그래도 어긋나면 `trust-base`.
    기록(사유 포함)을 돌려준다."""
    main, last = fleet_repo.main_sha(), read_chain()
    if last is None:
        raise InfraError(f"publish host has no approval chain for {fleet.FLEET_REPO} (trust-base)")
    note: dict[str, object] = {"main": main, "last_approved": last}
    if main == last:
        return {**note, "action": "none"}
    number = approved_merge(fleet_repo, main)
    if number is not None:
        reason = f"g2-nightly {run_id}: approved PR #{number} merge {main[:12]} missing in chain"
        return {**note, "action": "trust-base", "trust": trust_base(main, reason)}
    known = {r.id for r in fleet_repo.runs(fleet.PUBLISH_WORKFLOW, last)}
    fleet_repo.force_branch(fleet.MAIN, last)
    run = fleet_repo.wait_run(fleet_repo.wait_new_run(fleet.PUBLISH_WORKFLOW, last, known))
    note |= {"action": "reverted", "reverted_from": main, "revert_run": run.id}
    note["revert_conclusion"] = run.conclusion
    if fleet_repo.main_sha() == last and read_chain() == last:
        return note
    head = fleet_repo.main_sha()
    reason = f"g2-nightly {run_id}: chain still broken after reverting {main[:12]} to {last[:12]}"
    return {**note, "action": "trust-base", "trust": trust_base(head, reason)}


# ---------------------------------------------------------------- ④ acme-test G2 태그(★P3)


def tag_tree(source: Path, dest: Path) -> Path:
    """G2 트리 사본에서 `project.yaml` `absorbs`를 비운 트리(acme-test 기준 태그 내용)."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    project_file = dest / "project.yaml"
    doc = cast(
        dict[str, canonical.Json], canonical.load(project_file.read_text("utf-8"), "project")
    )
    doc.pop("absorbs", None)
    project_file.write_text(canonical.dump(doc), encoding="utf-8", newline="\n")
    project = load_project(dest)
    if not project.ok:
        raise E2EError(f"{dest}: " + "; ".join(project.problems))
    return dest


def version_of(content_hash: str, prefix: str = PROJECT_TAG_PREFIX) -> str:
    """`sha256:<hex>` → `<prefix><hex 앞 12자의 10진, 15자리 0채움>`."""
    digest = content_hash.removeprefix("sha256:")
    if len(digest) < 12:
        raise E2EError(f"content hash too short: {content_hash!r}")
    return f"{prefix}{int(digest[:12], 16):015d}"


def tree_files(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def ensure_project_tag(project_repo: Repo, tree: Path, run_id: str) -> dict[str, object]:
    """acme-test에 G2 태그가 없으면 `g2/base`에 그 트리만 담은 커밋을 얹고 태그를 만든다."""
    version = version_of(load_project(tree).content_hash())
    found = project_repo.tag_sha(version)
    if found is not None:
        return {"version": version, "created": False, "commit": found}
    parent = project_repo.ref_sha(PROJECT_BASE_BRANCH) or project_repo.main_sha()
    message = fleet.bot_title(run_id, f"G2 기준 트리 {version}")
    commit = project_repo.commit_files(parent, tree_files(tree), message, replace=True)
    project_repo.force_branch(PROJECT_BASE_BRANCH, commit)
    project_repo.create_tag(version, commit)
    return {"version": version, "created": True, "commit": commit}


# ---------------------------------------------------------------- ⑤ 기준선 PR(P2)


def site_path(site_id: str) -> str:
    return f"sites/{site_id}.yaml"


def file_gen(data: bytes | None) -> int:
    """fleet 현장 파일의 `generation`(없으면 0 — 새 파일은 gen 1)."""
    if data is None:
        return 0
    doc = canonical.load(data.decode("utf-8"), "fleet site")
    gen = cast(dict[str, object], doc).get("generation") if isinstance(doc, dict) else None
    if not isinstance(gen, int):
        raise E2EError("fleet site file has no integer generation")
    return gen


def site_file(
    site_id: str, gen: int, product: str, version: str, window: tuple[datetime, datetime]
) -> bytes:
    doc: dict[str, canonical.Json] = {
        "site": site_id,
        "generation": gen,
        "product": product,
        "project": {"repo": fleet.PROJECT_NAME, "version": version},
        "window": {"once": {"start": fleet.iso(window[0]), "end": fleet.iso(window[1])}},
    }
    return canonical.dump(doc).encode("utf-8")


def main_gens(fleet_repo: Repo, ref: str, sites: Sequence[str] = SITES) -> dict[str, int]:
    return {s: file_gen(fleet_repo.read_file(site_path(s), ref)) for s in sites}


@dataclass(frozen=True)
class Merged:
    pull: fleet.Pull
    sha: str
    run: fleet.Run
    outcome: fleet.PublishOutcome
    check: str


def pr_round(
    fleet_repo: Repo,
    run_id: str,
    what: str,
    files: Mapping[str, bytes],
    title: str,
    *,
    before_pr: Callable[[], None] | None = None,
) -> Merged:
    """bot PR 한 바퀴: `g2/<run>-<what>` 브랜치(main에서) → 파일 → PR → check-gen → 승인 → 머리
    승인 확인 뒤 squash merge → publish run 완료 → 로그 해석. 대기 초과는 `FleetError`."""
    base = fleet_repo.main_sha()
    branch = fleet.bot_branch(run_id, what)
    fleet_repo.create_branch(branch, base)
    for path, content in files.items():
        fleet_repo.put_file(branch, path, content, fleet.bot_title(run_id, f"{path}"))
    if before_pr is not None:
        before_pr()
    body = f"G2 nightly {run_id} — 무인 PR(시험 전용 repo, 작성 {fleet_repo.author})."
    pull = fleet_repo.open_pull(branch, fleet.bot_title(run_id, title), body)
    check = fleet_repo.wait_check(pull.number)
    head = str(cast(dict[str, object], fleet_repo.pull(pull.number).get("head") or {}).get("sha"))
    fleet_repo.approve(pull.number, head, f"승인(G2 nightly {run_id} 무인 승인 — ★D5)")
    sha = fleet_repo.merge(pull.number)
    run = fleet_repo.wait_run(fleet_repo.wait_new_run(fleet.PUBLISH_WORKFLOW, sha, set()))
    outcome = fleet.publish_outcome(fleet_repo.run_log(run))
    return Merged(pull, sha, run, outcome, check)


def baseline(
    session: g2.Session, fleet_repo: Repo, version: str, now: datetime
) -> tuple[dict[str, int], Merged]:
    """site-a gen+1(BASE)·site-b gen+1(없으면 1, NEXT), 같은 G2 태그, 늘 열린 1회 창 → 새 중앙에 둘
    다 게시. check-gen 실패·publish 판정 실패는 제품(`E2EError`)."""
    pair = session.pair
    gens = {s: g + 1 for s, g in main_gens(fleet_repo, fleet.MAIN).items()}
    products = {"site-a": pair.releases[pair.base], "site-b": pair.releases[pair.next]}
    window = (now + BASELINE_WINDOW[0], now + BASELINE_WINDOW[1])
    files = {site_path(s): site_file(s, gens[s], products[s], version, window) for s in SITES}
    title = f"기준선: site-a g{gens['site-a']} BASE·site-b g{gens['site-b']} NEXT"
    merged = pr_round(fleet_repo, session.run_id, "baseline", files, title)
    if merged.check != "success":
        raise E2EError(f"baseline PR #{merged.pull.number} check-gen {merged.check}")
    out = merged.outcome
    checks = judges.judge_baseline_publish(merged.run.conclusion, out.published, out.counts, gens)
    failed = sorted(k for k, ok in checks.items() if not ok)
    if failed:
        raise E2EError(
            f"baseline publish run {merged.run.id} ({merged.run.conclusion}): {failed} "
            f"published={out.published} counts={out.counts} e_approval={out.e_approval}"
        )
    return gens, merged
