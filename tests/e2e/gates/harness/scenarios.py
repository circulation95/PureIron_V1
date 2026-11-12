"""G2 시나리오(M6-3 H6) — `(g2.Session, 현장) → 기록`.

계약:
① 시작할 때 그 현장을 체크포인트 c0로 복원한다(`begin` — 복원 직전 메모리 확보, `g2.restore_c0`).
② 다른 시나리오가 남긴 VM 상태·기록에 기대지 않는다 — nightly의 infra 재시도는 남은 VM을 지우고
   실패한 시험 id만 새 pytest로 다시 돌리므로 시험 하나만 골라 돌려도 통과해야 한다.
③ 판정은 `judges`의 순수 함수다. 원격·WSL·Hyper-V 호출은 `Remote`로 받는다(단위 시험이 가짜로
   바꾼다).
④ 부팅·설치가 실패한 현장이면 그 예외를 다시 던진다(`g2.site_up` — 분류는 예외 종류).

한 시나리오가 판정 여러 개를 내면 시험 파일의 module fixture가 한 번 돌리고 시험들이 항목별로
단언한다. 시나리오가 내는 기록은 그 현장 `Context`(`state.json`·출력 줄)에 남는다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness import buildhost, g2, golden, judges, site, trees
from harness.context import TAIL_CHARS, Context
from harness.site import INCOMING
from harness.vm import Vm

Probe = Callable[[Vm], dict[str, object]]


def build_bundle(ctx: Context, gen: int, release: str, tree: Path) -> Path:
    """G2 현장 번들(nightly 작업 디렉터리 `NIGHTLY_WORK`의 기록·store, 트리 `tree`)."""
    return buildhost.build_site_bundle(
        ctx,
        gen,
        release,
        datetime.now(UTC) - timedelta(minutes=5),
        golden.wsl_path(tree),
        work=buildhost.NIGHTLY_WORK,
    )


def drop_bundle(host: Vm, pib: Path) -> None:
    host.run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)


def stage(host: Vm, pib: Path) -> object:
    return site.stage(host, pib, check=False)


def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
    return site.converge(host, gen)


@dataclass(frozen=True)
class Remote:
    """시나리오가 부르는 원격·WSL·Hyper-V 조작. 기본값이 실제 호출이다."""

    restore: Callable[[g2.SiteRun], object] = g2.restore_c0
    probe: Probe = site.probe
    wait_ready: Callable[[Vm], dict[str, object]] = field(
        default=lambda host: site.wait_ready(host, g2.READY_TIMEOUT_S)
    )
    """복원 뒤 ready까지 기다리고 마지막 probe 스냅샷을 돌려준다."""
    build: Callable[[Context, int, str, Path], Path] = build_bundle
    stage: Callable[[Vm, Path], object] = stage
    converge: Callable[[Vm, int], tuple[dict[str, object], float]] = converge
    report_for: Callable[[Vm, Path], dict[str, object]] = site.report_for
    drop: Callable[[Vm, Path], None] = drop_bundle
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class Attempt:
    """번들 하나를 적용하고 수렴을 기다린 결과."""

    gen: int
    release: str
    snap: dict[str, object]
    """수렴(또는 `site.CONVERGE_S` 시간 초과) 뒤 마지막 probe."""
    report: dict[str, object]
    """그 번들의 Updater 보고(`site.report_for`, 없으면 빈 dict)."""
    seconds: float
    """스테이징부터 수렴 판정까지(`converge`가 잰 초)."""


def begin(
    session: g2.Session, site_id: str, remote: Remote
) -> tuple[g2.SiteRun, dict[str, object]]:
    """시나리오 시작(계약 ①·④): 현장이 올라와 있는지 → c0 복원 → ready까지 기다린 c0 스냅샷."""
    run = g2.site_up(session, site_id)
    remote.restore(run)
    return run, remote.wait_ready(run.ctx.site.host())


def attempt(run: g2.SiteRun, gen: int, release: str, tree: Path, remote: Remote) -> Attempt:
    """`release` g`gen` 번들(트리 `tree`)을 만들어 스테이징하고 시도가 끝날 때까지 기다린다. 판정은
    하지 않는다(호출자가 `judges`로)."""
    ctx = run.ctx
    host = ctx.site.host()
    ctx.log(f"apply g{gen} {release} ({tree.name} tree)")
    pib = remote.build(ctx, gen, release, tree)
    remote.stage(host, pib)
    snap, seconds = remote.converge(host, gen)
    report = remote.report_for(host, pib)
    remote.drop(host, pib)
    return Attempt(gen, release, snap, report, round(seconds, 1))


def failed(checks: dict[str, bool]) -> list[str]:
    """거짓인 판정 이름(정렬) — 시험의 단언 메시지."""
    return sorted(name for name, ok in checks.items() if not ok)


# ---------------------------------------------------------------- G2-4 롤백 + G2-6 ③ (site-a)

ROLLBACK_SITE = "site-a"
FAULT_GEN = 2
RETRY_GEN = 3
"""c0(applied g1)에서 FAULT-h g2 → NEXT g3 재시도."""
ITEM_ROLLBACK = "G2-4 rollback before revert (FAULT-h -> BASE)"
ITEM_HEALTH = "G2-6 3 health failure converges"
ITEM_RETRY = "G2-4 retry NEXT after rollback"


@dataclass(frozen=True)
class RollbackOutcome:
    """시험 둘(`test_g2_4_rollback`·`test_g2_6_3_health_failure`)이 나눠 보는 판정."""

    rollback: dict[str, bool]
    retry: dict[str, bool]
    health: dict[str, bool]


def rows(db: dict[str, object]) -> dict[str, object]:
    return {key: db.get(key) for key in (*judges.ROW_KEYS, "compressed_chunks")}


def rollback(session: g2.Session, remote: Remote | None = None) -> RollbackOutcome:
    """site-a: c0 복원 → 행 수 4종·site_rev/base_site_rev·스키마 집합 기록(c0 스냅샷) → FAULT-h
    g2(FAULT-h 트리) → 수렴 → NEXT g3(NEXT 트리) → 수렴. 판정은 `judges.judge_g2_rollback`·
    `judge_health_failure`·`judge_g2_retry`, 기록은 현장 `Context`에 셋."""
    remote = remote or Remote()
    pair = session.pair
    run, before = begin(session, ROLLBACK_SITE, remote)
    ctx = run.ctx
    base = ctx.digests[run.release]
    fault = attempt(run, FAULT_GEN, pair.fault, session.tree_of(pair.fault), remote)
    rolled = judges.judge_g2_rollback(
        before,
        fault.snap,
        fault.report,
        base_digests=base,
        base_tree=trees.tree_expect(session.tree, pair),
    )
    health = judges.judge_health_failure(
        fault.snap, fault.report, fault.seconds, gen=FAULT_GEN, base_digests=base
    )
    before_db, fault_db = judges.db_of(before), judges.db_of(fault.snap)
    common: dict[str, object] = {
        "gen": FAULT_GEN,
        "release": pair.fault,
        "result": judges.updater_state(fault.snap).get("last_result"),
        "code": fault.report.get("code"),
        "seconds": fault.seconds,
        "rolling_back_s": judges.rolling_back_seconds(fault.report),
        "phase_durations_s": fault.report.get("phase_durations_s"),
    }
    ctx.record(
        ITEM_ROLLBACK,
        all(rolled.values()),
        **common,
        checks=rolled,
        rows_before=rows(before_db),
        rows_after=rows(fault_db),
        revs_before=judges.revs(judges.project_body(before)),
        revs_after=judges.revs(judges.project_body(fault.snap)),
        logs=None if all(rolled.values()) else str(fault.report.get("logs"))[-TAIL_CHARS:],
    )
    ctx.record(ITEM_HEALTH, all(health.values()), **common, checks=health)
    retry = attempt(run, RETRY_GEN, pair.next, session.tree_of(pair.next), remote)
    again = judges.judge_g2_retry(
        before_db,
        retry.snap,
        retry.report,
        gen=RETRY_GEN,
        next_digests=ctx.digests[pair.next],
        next_tree=trees.tree_expect(session.tree_of(pair.next), pair),
        expected_schema=session.expected_schema,
    )
    retry_db = judges.db_of(retry.snap)
    ctx.record(
        ITEM_RETRY,
        all(again.values()),
        gen=RETRY_GEN,
        release=pair.next,
        result=judges.updater_state(retry.snap).get("last_result"),
        code=retry.report.get("code"),
        seconds=retry.seconds,
        checks=again,
        schema_diff=judges.schema_diff(retry_db, session.expected_schema),
        rows_after=rows(retry_db),
        logs=None if all(again.values()) else str(retry.report.get("logs"))[-TAIL_CHARS:],
    )
    return RollbackOutcome(rolled, again, health)


# ---------------------------------------------------------------- G2-6 ② migrate 실패 (site-b)

MIGRATE_SITE = "site-b"
ITEM_MIGRATE = "G2-6 2 migrate failure keeps the schema"


def migrate_failure(session: g2.Session, remote: Remote | None = None) -> dict[str, bool]:
    """site-b(c0 = NEXT): c0 복원 → 스키마 집합 5종·epoch·행 수 기록(c0 스냅샷) → FAULT-m
    g2(FAULT-m 트리) → 수렴. 판정 `judges.judge_migrate_failure`, 기록은 현장 `Context`에."""
    remote = remote or Remote()
    pair = session.pair
    run, before = begin(session, MIGRATE_SITE, remote)
    ctx = run.ctx
    fault = attempt(run, FAULT_GEN, pair.fault_migrate, session.tree_of(pair.fault_migrate), remote)
    checks = judges.judge_migrate_failure(
        before,
        fault.snap,
        fault.report,
        fault.seconds,
        gen=FAULT_GEN,
        next_digests=ctx.digests[pair.next],
        epoch=pair.epochs[pair.next],
    )
    before_db, after_db = judges.db_of(before), judges.db_of(fault.snap)
    ok = all(checks.values())
    ctx.record(
        ITEM_MIGRATE,
        ok,
        gen=FAULT_GEN,
        release=pair.fault_migrate,
        result=judges.updater_state(fault.snap).get("last_result"),
        code=fault.report.get("code"),
        seconds=fault.seconds,
        phase_durations_s=fault.report.get("phase_durations_s"),
        checks=checks,
        epoch_before=before_db.get("epoch"),
        epoch_after=after_db.get("epoch"),
        schema_diff=judges.schema_diff(after_db, before_db),
        rows_before=rows(before_db),
        rows_after=rows(after_db),
        logs=None if ok else str(fault.report.get("logs"))[-TAIL_CHARS:],
    )
    return checks
