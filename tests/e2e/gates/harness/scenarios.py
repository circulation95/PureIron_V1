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

from harness import buildhost, g2, golden, site
from harness.context import Context
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
