"""fleet 시나리오(M6-4a A6) — 중앙·fleet-test PR 경로. M6-3 계약(H6)을 따른다: 출발 체크포인트(c1
쌍, `fleet_session.restore_c1`) 복원으로 시작하고, 다른 시나리오의 상태에 기대지 않으며, 판정은
`judges`의 순수 함수, 원격 호출은 `Remote`·`Repos`로 받는다.

- `scheduled`(G2-2 + G2-3, site-a·site-b c1): site-b 스냅샷 → bot PR(site-a gen+1, NEXT,
`window.once` = PR을 만들기 직전 now + `WINDOW_LEAD_S` ~ +1시간) → 승인 → merge → publish → 창 →
적용 수렴 → 판정(`judges.judge_scheduled`·`judge_publish_split`·`judge_isolation`).
- `e_approval`(`E_APPROVAL`, K14, VM 없음 — 중앙만 c1): bot PR(site-a gen+1)을 **승인 없이** merge
  (`Repo.merge_unapproved` — 이 시나리오만 쓰는 길) → publish run 실패·`E_APPROVAL`·중앙
  `max_published_gen`·사슬 불변 → main을 merge 전으로 force 되돌림 → 그 publish run success·게시 0.
  전제: main 현장 gen == c1 중앙 max(fleet 묶음 맨 앞에 돈다 — `harness.order`).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol, cast

from harness import baseline, fleet, fleet_session, g2, judges
from harness.context import TAIL_CHARS, InfraError
from harness.fleet_session import FleetSession, Remote, Repos
from harness.vm import Vm

SCHEDULED_SITE = "site-a"
ISOLATED_SITE = "site-b"
WINDOW_LEAD_S = 180
"""PR을 만들기 직전부터 창 시작까지(계획 `now+3m`). PR 한 바퀴(브랜치·파일·PR·check-gen·승인·merge →
publish 완료)는 M4 마감 15분 실측에서 t0 → 게시 1분 23초, 게시 → VM 시도 시작 4초였다(약 1.5~2분)
— 창이 열리기 전에 받아 검증(`verified`)까지 끝낼 여유가 1분쯤 남는다."""
WINDOW_LEN = timedelta(hours=1)
PREFETCH_ALLOWANCE_S = 45
"""publish 완료 뒤 VM이 받아 검증하기까지 잡는 몫(poll 10초 + 번들 약 0.8GB 다운로드·검증).
publish가 창 시작 - 이 값보다 늦게 끝나면 `verified < 창 시작`을 볼 수 없어 `InfraError`(GitHub
대기, K3)."""
APPLY_TIMEOUT_S = 600
"""창 시작 뒤 적용 수렴까지."""
G2_2_ITEMS = (
    *("verified_before_window", "locked_in_window", "locked_to_succeeded_120s", "succeeded"),
    *("images_next", "fleet_current", "publish_success", f"published_{SCHEDULED_SITE}"),
    *(f"untouched_{ISOLATED_SITE}", "publish_no_failure"),
)
"""G2-2 판정 항목(시험 `test_g2_2_scheduled_update[…]`) — `judge_scheduled` +
`judge_publish_split`."""
G2_3_ITEMS = (
    *("images_unchanged", "content_hash_unchanged", "applied_gen_unchanged"),
    *("core_started_at_unchanged", "not_published"),
)
"""G2-3 판정 항목(`judge_isolation` + 게시 없음)."""
E_APPROVAL_ITEMS = (
    *("main_matches_c1", "run_failed", "e_approval_logged", "not_signed"),
    *("max_published_unchanged", "chain_unchanged", "main_reverted", "revert_published_nothing"),
)
"""`E_APPROVAL` 판정 항목(전제 + `judge_e_approval`)."""
ITEM_G2_2 = "G2-2 scheduled update (bot PR -> publish -> window -> applied)"
ITEM_G2_3 = "G2-3 isolation (site-b untouched by the site-a update)"
ITEM_E_APPROVAL = "E_APPROVAL unapproved merge is not published (K14)"


class Shell(Protocol):
    """보고를 읽는 원격 셸 — `vm.Vm`이 만족한다(단위 시험은 가짜)."""

    def run(
        self, command: str, *, check: bool = True, timeout_s: float = 600
    ) -> subprocess.CompletedProcess[str]: ...


def report_for_sha(host: Shell, sha256: str) -> dict[str, object]:
    """현장 보고 중 번들 sha256(`/fleet` desired)의 것(`site.report_for`와 같은 파일 이름 규칙)."""
    sha12 = sha256.removeprefix("sha256:")[:12]
    if len(sha12) != 12:
        return {}
    out = host.run(
        f"sudo sh -c 'ls -t /var/lib/pureiron/state/reports/*-{sha12}.json "
        f"/var/lib/pureiron/state/reports/exported/*-{sha12}.json 2>/dev/null | head -1 | "
        "xargs -r cat'",
        check=False,
    ).stdout
    return cast(dict[str, object], json.loads(out)) if out.strip() else {}


@dataclass(frozen=True)
class Scenario:
    """fleet 시나리오의 조작 — 기본값이 실제 호출이다."""

    remote: Remote
    repos: Repos
    restore: Callable[..., dict[str, object]] = fleet_session.restore_c1
    report: Callable[[Vm, str], dict[str, object]] = report_for_sha

    @classmethod
    def default(cls) -> Scenario:
        return cls(Remote(), Repos.default())


@dataclass(frozen=True)
class ScheduledOutcome:
    g2_2: dict[str, bool]
    g2_3: dict[str, bool]


def fleet_status(remote: Remote, site_id: str) -> dict[str, object]:
    return judges.fleet_row(fleet_session.central(remote, "fleet", "--site", site_id), site_id)


def scheduled(
    session: g2.Session, fs: FleetSession, scenario: Scenario | None = None
) -> ScheduledOutcome:
    """G2-2 + G2-3(모듈 설명). 기록은 site-a·site-b `Context`에."""
    sc = scenario or Scenario.default()
    remote, repo = sc.remote, sc.repos.fleet
    sc.restore(session, fs, (SCHEDULED_SITE, ISOLATED_SITE), remote=remote, repos=sc.repos)
    a, b = g2.site_up(session, SCHEDULED_SITE), g2.site_up(session, ISOLATED_SITE)
    before_b = judges.isolation_view(remote.probe(b.ctx.site.host()))
    pair = session.pair
    gen = baseline.main_gens(repo, fleet.MAIN, (SCHEDULED_SITE,))[SCHEDULED_SITE] + 1
    # 창은 PR 한 바퀴를 시작하기 직전부터 잰다(파일에 들어가야 하므로 브랜치를 만들기 전)
    window = {"start": remote.now() + timedelta(seconds=WINDOW_LEAD_S)}
    window["end"] = window["start"] + WINDOW_LEN
    content = baseline.site_file(
        SCHEDULED_SITE,
        gen,
        pair.releases[pair.next],
        fs.project_version,
        (window["start"], window["end"]),
    )
    merged = baseline.pr_round(
        repo,
        session.run_id,
        "scheduled",
        {baseline.site_path(SCHEDULED_SITE): content},
        f"G2-2: {SCHEDULED_SITE} g{gen} NEXT, window {fleet.iso(window['start'])}",
    )
    published_at = remote.now()
    if published_at > window["start"] - timedelta(seconds=PREFETCH_ALLOWANCE_S):
        raise InfraError(
            f"publish run {merged.run.id} finished at {fleet.iso(published_at)}, too close to the "
            f"window start {fleet.iso(window['start'])} to see verified < window (GitHub delay)"
        )
    host = a.ctx.site.host()
    snap, waited = remote.converge(host, gen)
    row = fleet_session.wait_row(
        remote,
        SCHEDULED_SITE,
        lambda r: judges.sub(r, "current").get("gen") == gen,
        fleet_session.FLEET_TIMEOUT_S,
    )
    sha = str(judges.sub(row, "desired").get("sha256") or "")
    report = sc.report(host, sha)
    out = merged.outcome
    g2_2 = judges.judge_scheduled(
        report,
        snap,
        row,
        gen=gen,
        window_start=window["start"],
        next_digests=a.ctx.digests[pair.next],
        next_version=pair.releases[pair.next],
    )
    g2_2 |= judges.judge_publish_split(
        merged.run.conclusion,
        out.published,
        out.counts,
        out.current,
        out.skipped,
        changed=SCHEDULED_SITE,
        gen=gen,
        untouched=ISOLATED_SITE,
    )
    after_b = judges.isolation_view(remote.probe(b.ctx.site.host()))
    g2_3 = judges.judge_isolation(before_b, after_b)
    g2_3["not_published"] = g2_2[f"untouched_{ISOLATED_SITE}"]
    a.ctx.record(
        ITEM_G2_2,
        all(g2_2.values()),
        gen=gen,
        checks=g2_2,
        pull=merged.pull.number,
        merge=merged.sha,
        publish_run=merged.run.id,
        publish=out.counts,
        window_start=fleet.iso(window["start"]),
        published_at=fleet.iso(published_at),
        timeline=report.get("timeline"),
        converge_s=round(waited, 1),
        logs=None if all(g2_2.values()) else str(report.get("logs"))[-TAIL_CHARS:],
    )
    b.ctx.record(ITEM_G2_3, all(g2_3.values()), checks=g2_3, before=before_b, after=after_b)
    return ScheduledOutcome(g2_2, g2_3)


def e_approval(
    session: g2.Session, fs: FleetSession, scenario: Scenario | None = None
) -> dict[str, bool]:
    """`E_APPROVAL`(모듈 설명). 되돌림 GitHub 호출 실패는 `FleetError`(infra) — 다음 시나리오·세션의
    사슬 대조(`fleet_session.reconcile`)가 치운다. 기록은 site-a `Context`에."""
    sc = scenario or Scenario.default()
    remote, repo = sc.remote, sc.repos.fleet
    sc.restore(session, fs, (), remote=remote, repos=sc.repos)
    main_before = repo.main_sha()
    gens = baseline.main_gens(repo, main_before)
    c1_max = {s: fs.c1_fleet.get(s, {}).get("max_published_gen") for s in baseline.SITES}
    chain_before = fleet_session.read_chain(remote)
    max_before = fleet_status(remote, SCHEDULED_SITE).get("max_published_gen")
    pair = session.pair
    gen = gens[SCHEDULED_SITE] + 1
    now = remote.now()
    content = baseline.site_file(
        SCHEDULED_SITE,
        gen,
        pair.releases[pair.base],
        fs.project_version,
        (now + baseline.BASELINE_WINDOW[0], now + baseline.BASELINE_WINDOW[1]),
    )
    branch = fleet.bot_branch(session.run_id, "e-approval")
    repo.create_branch(branch, main_before)
    repo.put_file(branch, baseline.site_path(SCHEDULED_SITE), content, "E_APPROVAL probe")
    pull = repo.open_probe(
        branch,
        fleet.bot_title(session.run_id, f"E_APPROVAL: {SCHEDULED_SITE} g{gen} merged unapproved"),
        f"G2 nightly {session.run_id} — 승인 없이 merge해 publish 거부(E_APPROVAL)를 본다.",
    )
    merged = repo.merge_unapproved(pull)
    run = repo.wait_run(repo.wait_new_run(fleet.PUBLISH_WORKFLOW, merged, set()))
    out = fleet.publish_outcome(repo.run_log(run))
    chain_after = fleet_session.read_chain(remote)
    max_after = fleet_status(remote, SCHEDULED_SITE).get("max_published_gen")
    known = {r.id for r in repo.runs(fleet.PUBLISH_WORKFLOW, main_before)}
    repo.force_branch(fleet.MAIN, main_before)
    revert = repo.wait_run(repo.wait_new_run(fleet.PUBLISH_WORKFLOW, main_before, known))
    revert_out = fleet.publish_outcome(repo.run_log(revert))
    main_after = repo.main_sha()
    checks = {
        "main_matches_c1": bool(gens) and all(gens[s] == c1_max[s] for s in baseline.SITES),
        **judges.judge_e_approval(
            run_conclusion=run.conclusion,
            e_approval_logged=out.e_approval,
            signed=out.counts is not None,
            max_before=max_before,
            max_after=max_after,
            chain_before=chain_before,
            chain_after=chain_after,
            main_before=main_before,
            main_after=main_after,
            revert_conclusion=revert.conclusion,
            revert_counts=revert_out.counts,
            revert_e_approval=revert_out.e_approval,
        ),
    }
    g2.site_up(session, SCHEDULED_SITE).ctx.record(
        ITEM_E_APPROVAL,
        all(checks.values()),
        checks=checks,
        pull=pull.number,
        merge=merged,
        publish_run=run.id,
        revert_run=revert.id,
        revert_publish=revert_out.counts,
        gens=gens,
        c1_max=c1_max,
        max_published=(max_before, max_after),
    )
    return checks
