"""G2-6 ④⑤·주간 G2-X crash(M6-3b J3, 계획 ★H3, site-a) — 시도의 phase마다 Updater를 죽이거나(kill)
VM 전원을 끊어도(reset) 10분 안에 맞는 결과로 수렴하는지.

18건 = 9 phase(`PHASES`) x kill(서비스 SIGKILL)·reset(`Restart-VM -Force`). 매 건 독립이다(★H3): c0
복원(site-a = BASE, applied g1) → BASE→NEXT g2(NEXT 트리)에 주입, `rolling_back`만 BASE→FAULT-h g2
(FAULT 트리 — 실패해서 롤백으로 들어간다), `fetched`(번들 검증)는 서비스가 아니라 CLI가 실행하므로
CLI를 대상으로 한다. 서비스 대상은 창이 `WINDOW_LEAD_S` 뒤에 열리는 번들을 CLI로 먼저 스테이징한다
(창 전에 끝나야 서비스가 그 phase를 실행한다 — `staged_before_window`). VM 쪽 `site/crash_at.py`가
그 phase 진입 순간 프로세스를 얼린 뒤 kill/리셋한다. 평일 묶음은 `WEEKDAY` 2건, 나머지 16건은 pytest
표지 `weekly`(주간 묶음에서만 돈다).

판정은 `judges.judge_crash`(run.py `crash_once`와 같은 항목 + 주입 뒤 10분 안 수렴). 창 여유 부족
(E10 — 제품 판정은 모두 참인데 스테이징이 창 뒤에 끝남, `judges.judge_window_flake`)은 infra로 보고
그 건을 c0부터 1회 다시 한다. 두 번째도 E10이면 run.py `retry_window_flake`처럼 판정을 그대로
남긴다 — `staged_before_window` 거짓 = 그 건의 실패(product). 스테이징(검증 + 이미지 적재)이 꾸준히
창 대기(150초)를 넘기게 느려진 것은 제품 성능 회귀이고, infra로 두면 nightly 재시도가 그 건을 최대
4번 돌리며 매일 "error"로만 쌓인다(M6-3b 리뷰 M3). 사람은 기록 `e10`(첫 시도)·두 시도의 `margin_s`로
가른다. run.py의 crash 경로(`crash_once`·`step_crash`·E10)는 그대로이고 이 모듈은 그것을 부르지
않는다.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness import buildhost, g2, golden, hyperv, judges, scenarios, site
from harness.context import TAIL_CHARS, Context
from harness.release_pair import ReleasePair
from harness.site import INCOMING, REMOTE
from harness.vm import Vm

PHASES = (
    "fetched",
    "verified",
    "committed",
    "locked",
    "backed_up",
    "migrating",
    "switching",
    "checking",
    "rolling_back",
)
MODES = ("kill", "reset")
WEEKDAY = frozenset({("reset", "switching"), ("kill", "migrating")})
"""평일 묶음에서도 도는 2건(G2-6 ④ 전원 리셋·⑤ 서비스 kill)."""
CRASH_SITE = "site-a"
CRASH_GEN = 2
"""c0(applied g1) 다음 — 매 건 c0로 복원하므로 gen이 늘 같다."""
WINDOW_LEAD_S = 150
"""번들 창이 열리기까지(run.py `WINDOW_LEAD_S`와 같다) — 그 전에 CLI 스테이징이 끝나야 한다."""
WATCH_TIMEOUT_S = WINDOW_LEAD_S + 600
CONVERGE_TIMEOUT_S = judges.CONVERGE_LIMIT_S + 300
"""주입 뒤 수렴을 기다리는 시간 — "10분 안" 한도보다 길게 둬서 10~15분에 끝난 수렴이 시간 초과가
아니라 `converged_in_time` 거짓(product)으로 남게 한다."""
WATCHER_EXIT_S = 60
CLI_EXIT_S = 1800


@dataclass(frozen=True)
class CrashCase:
    phase: str
    mode: str
    """`kill`(서비스 SIGKILL — CLI 대상은 CLI SIGKILL) 또는 `reset`(VM 전원 리셋)."""

    @property
    def id(self) -> str:
        return f"{self.mode}@{self.phase}"

    @property
    def weekly(self) -> bool:
        return (self.mode, self.phase) not in WEEKDAY

    @property
    def target(self) -> str:
        return "cli" if self.phase == "fetched" else "service"

    @property
    def expect(self) -> str:
        return "failed" if self.phase == "rolling_back" else "succeeded"

    def release(self, pair: ReleasePair) -> str:
        """주입할 시도의 release — `rolling_back`은 FAULT-h(롤백으로 들어가야 한다), 나머지는
        NEXT."""
        return pair.fault if self.phase == "rolling_back" else pair.next


PLAN: tuple[CrashCase, ...] = tuple(CrashCase(phase, mode) for phase in PHASES for mode in MODES)


def item_name(case: CrashCase) -> str:
    return f"G2-X crash {case.mode} @ {case.phase}"


@dataclass(frozen=True)
class Injection:
    """주입 결과(판정 전)."""

    hit: bool
    """감시자가 그 phase에서 프로세스를 얼렸다(`HIT`, 종료 코드 0)."""
    watcher: str
    staged_in_time: bool | None
    """서비스 대상: CLI 스테이징이 창 전에 끝났는가. CLI 대상은 None."""
    stage: dict[str, object]
    boot_before: str
    restarts_before: int


def build_window_bundle(
    ctx: Context, gen: int, release: str, tree: Path, open_at: datetime
) -> Path:
    """창이 `open_at`에 열리는 번들(창은 서명되므로 빌드 전에 정한다)."""
    return buildhost.build_site_bundle(
        ctx, gen, release, open_at, golden.wsl_path(tree), work=buildhost.NIGHTLY_WORK
    )


def inject(ctx: Context, case: CrashCase, gen: int, pib: Path, open_at: datetime) -> Injection:
    """번들을 올리고 감시자(`crash_at.py`)를 띄운 뒤 스테이징한다 — 서비스 대상은 CLI로 창 전에
    스테이징(`site.stage_for_window`), CLI 대상은 CLI가 그 phase를 실행한다. 감시자가 `HIT`면
    reset은 카운터를 표시한 뒤 `Restart-VM -Force`(리셋은 nft 카운터를 0으로 되돌린다). run.py
    `crash_once`의
    주입 부분과 같은 순서다."""
    host = ctx.site.host()
    host.put(pib, f"{INCOMING}/{pib.name}")
    boot_before, restarts_before = site.boot_id(host), site.service_restarts(host)
    watch_mode = "kill" if case.mode == "kill" else "freeze"
    watcher = host.start(
        f"sudo python3 {REMOTE}/crash_at.py --generation {gen} --phase {case.phase} "
        f"--mode {watch_mode} --target {case.target} --timeout {WATCH_TIMEOUT_S}"
    )
    time.sleep(2)
    apply_cmd = f"sudo pureiron-site apply {INCOMING}/{pib.name}"
    staged: bool | None = None
    stage_proc: subprocess.Popen[str] | None = None
    info: dict[str, object] = {"target": case.target}
    if case.target == "cli":
        stage_proc = host.start(apply_cmd, capture=False)
    else:
        staged, more = site.stage_for_window(
            lambda: host.run(apply_cmd, check=False, timeout_s=scenarios.STAGE_TIMEOUT_S),
            open_at,
            watcher_alive=lambda: watcher.poll() is None,
        )
        info.update(more)
    line = watcher.stdout.readline().strip() if watcher.stdout else ""
    watcher_rc = watcher.wait(timeout=WATCHER_EXIT_S)
    hit = line.startswith("HIT") and watcher_rc == 0
    if hit and case.mode == "reset":
        site.nft_mark(ctx, host)
        hyperv.restart_force(ctx.site.vm_name)
        ctx.nft_last = {}
    if stage_proc is not None:
        info["rc"] = stage_proc.wait(timeout=CLI_EXIT_S)  # CLI 대상은 감시자가 죽인다
    return Injection(hit, line, staged, info, boot_before, restarts_before)


def effect(ctx: Context, case: CrashCase, injection: Injection) -> dict[str, bool]:
    """주입 효과: reset이면 boot id가 바뀌었고, 서비스 kill이면 systemd 재시작 횟수가 늘었다(CLI
    kill은 볼 것이 없다)."""
    host = ctx.site.host()
    if case.mode == "reset":
        return {"rebooted": site.boot_id(host) != injection.boot_before}
    if case.target == "service":
        return {"service_restarted": site.service_restarts(host) > injection.restarts_before}
    return {}


def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
    return site.converge(host, gen, CONVERGE_TIMEOUT_S)


@dataclass(frozen=True)
class CrashRemote:
    """crash 시나리오의 원격·WSL·Hyper-V 조작. 기본값이 실제 호출이다(단위 시험은 가짜)."""

    base: scenarios.Remote = field(default_factory=scenarios.Remote)
    build: Callable[[Context, int, str, Path, datetime], Path] = build_window_bundle
    inject: Callable[[Context, CrashCase, int, Path, datetime], Injection] = inject
    effect: Callable[[Context, CrashCase, Injection], dict[str, bool]] = effect
    converge: Callable[[Vm, int], tuple[dict[str, object], float]] = converge
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Outcome:
    checks: dict[str, bool]
    stage: dict[str, object]


def attempt_case(
    session: g2.Session, case: CrashCase, remote: CrashRemote, note: dict[str, object] | None = None
) -> Outcome:
    """c0 복원 → 창 여유 번들 → 주입 → 수렴 → 판정·기록(`note`는 기록에 함께)."""
    pair = session.pair
    run, before = scenarios.begin(session, CRASH_SITE, remote.base)
    ctx = run.ctx
    release = case.release(pair)
    open_at = remote.now() + timedelta(seconds=WINDOW_LEAD_S)
    ctx.log(f"crash {case.id}: g{CRASH_GEN} {release}, window opens {open_at:%H:%M:%S}")
    pib = remote.build(ctx, CRASH_GEN, release, session.tree_of(release), open_at)
    injection = remote.inject(ctx, case, CRASH_GEN, pib, open_at)
    started = remote.base.clock()
    snap, _ = remote.converge(ctx.site.host(), CRASH_GEN)
    seconds = remote.base.clock() - started
    report = remote.base.report_for(ctx.site.host(), pib)
    want = ctx.digests[release if case.expect == "succeeded" else run.release]
    checks = judges.judge_crash(
        before,
        snap,
        report,
        expect=case.expect,
        gen=CRASH_GEN,
        want_digests=want,
        hit=injection.hit,
        staged_before_window=injection.staged_in_time,
        effect=remote.effect(ctx, case, injection),
        seconds=seconds,
    )
    ok = all(checks.values())
    state = judges.updater_state(snap)
    ctx.record(
        item_name(case),
        ok,
        gen=CRASH_GEN,
        release=release,
        watcher=injection.watcher,
        result=state.get("last_result"),
        code=report.get("code"),
        converge_s=round(seconds, 1),
        checks=checks,
        stage=injection.stage,
        alarm=state.get("alarm"),
        logs=None if ok else str(report.get("logs"))[-TAIL_CHARS:],
        **(note or {}),
    )
    remote.base.drop(ctx.site.host(), pib)
    return Outcome(checks, injection.stage)


def run_case(
    session: g2.Session, case: CrashCase, remote: CrashRemote | None = None
) -> dict[str, bool]:
    """한 건을 돌리고 판정을 돌려준다. 첫 시도가 E10이면 c0부터 1회 다시 하고(기록 `e10`), 두 번째
    판정을 그대로 돌려준다 — 그것도 E10이면 `staged_before_window` 거짓으로 그 건이 실패한다
    (product, 더 다시 하지 않는다)."""
    remote = remote or CrashRemote()
    first = attempt_case(session, case, remote)
    if not judges.judge_window_flake(first.checks, first.stage):
        return first.checks
    session.sites[CRASH_SITE].ctx.log(
        f"crash {case.id}: staged after the window opened (E10, infra) - retrying once from c0"
    )
    note: dict[str, object] = {"e10": {"class": "infra", "first": first.stage}}
    again = attempt_case(session, case, remote, note)
    if judges.judge_window_flake(again.checks, again.stage):
        session.sites[CRASH_SITE].ctx.log(
            f"crash {case.id}: staged after the window opened twice (E10) - kept as a failure"
            f" (staged_before_window false); margins {first.stage.get('margin_s')}s,"
            f" {again.stage.get('margin_s')}s"
        )
    return again.checks
