"""G2 nightly 실행기(M6-2) — Windows 호스트(self-hosted runner `e2e`)에서 돈다. 단계마다 따로
부를 수 있다:

    uv run python tests/e2e/gates/nightly.py all [--dry-run]
    uv run python tests/e2e/gates/nightly.py preflight [--judge-only] [--dry-run]
    uv run python tests/e2e/gates/nightly.py run [--run RUN]
    uv run python tests/e2e/gates/nightly.py collect [--run RUN]
    uv run python tests/e2e/gates/nightly.py publish [--run RUN] [--dry-run] [--junit F --sha S]
    uv run python tests/e2e/gates/nightly.py cleanup [--run RUN]

- `preflight`(`harness.preflight`): 메모리·디스크·WSL·Docker·foundation-ci 판단, 복구·정리.
- `run`: `pytest -c tests/e2e/gates/pytest.ini -m <묶음 표지> --junitxml …/junit.xml` → 분류
  (`harness.report`) → 실패가 모두 `infra`면 복구(WSL·남은 VM·호스트 메모리) 뒤 실패한 시험만 1회
  다시(`junit-retry.xml`) → `summary.json`. `product`는 다시 돌리지 않는다. pytest가 결과(junit)를
  남기지 못하면 시험 id `pytest`(infra) 한 줄로 남긴다 — 시간 초과였으면 다시 돌리지 않고(멈춘
  제품은 재시도로 낫지 않는다), 크래시였으면 전체를 1회 다시 돌린다.
- `collect`(`harness.collect`): VM마다 `updater.json`·메트릭·`journalctl -u pureiron-updater`·
  compose 로그를 `<결과>/<site>/`로(실패해도 계속).
- `publish`(`harness.publish`): `e2e-history` 브랜치 `history.jsonl`에 줄 추가, commit status
  `g2/nightly`, product 실패가 2회 연속이면 이슈(열린 것이 있으면 댓글). 같은 시험이 2회 연속
  infra로 실패해도 따로 이슈(라벨 `g2-nightly-infra`)를 연다 — 재시도로도 낫지 않는 실행 환경
  문제나 제품 멈춤(시간 초과)이 "error"로만 쌓이지 않게. `--dry-run`이면 무엇을 보낼지 출력만 한다.
- `cleanup`: 이 run의 번들 디렉터리를 지우고 VM을 끈다(다음 preflight가 VM을 지운다).

묶음(`harness.suites`)은 환경변수 `PUREIRON_G2_SUITE`로 받는다: `g2`(평일, 기본 —
`-m "g2 and not weekly"`)·`weekly`(일요일 — `-m g2`, crash 16건 더). 시작은 Windows 작업
스케줄러가 03:17 KST에 `gh workflow run nightly.yml`을 부르는 것이다
(`tests/e2e/infra/register-nightly.ps1`, ★H13 — GitHub cron은 이 저장소에서 3.5~7.5시간 늦게
만들어져 지웠다). PC가 꺼져 있으면 실행 자체가 없으므로 늦은 시작 가드도 지웠다.

`all`이 예상 밖 예외로 멈추면(`harness.runner`) 시험 id `runner`(infra) 하나로
`runner-summary.json`을 남기고 publish를 시도한 뒤 exit 2로 끝난다 — 그 run의 `summary.json`이
이미 있으면 덮어쓰지 않고 그것으로 publish를 다시 시도한다. run id는 `20261110T180000Z` 형식만
받는다.

시간 예산(묶음마다, `harness.suites`): `all`은 그 묶음의 workflow 시간(평일 120분·주간 270분)
안에 끝나야 status·이력이 남는다. preflight 시작부터 `run_budget_s` 안에 첫 실행·복구·재시도를
끝내고(pytest 시간 제한을 남은 시간으로 줄이고, 남은 시간이 `retry_min_s`보다 짧으면 재시도를
건너뛴다), collect·publish·cleanup은 `TAIL_RESERVE_S` 안에 든다.

결과는 `<PUREIRON_E2E_ROOT>/results/g2-<run>/`(junit.xml·summary.json·preflight.json·publish.json·
<site>/). 종료 코드: 0 통과, 1 product, 2 infra.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (
    buildhost,
    collect,
    flake,
    g2,
    hyperv,
    memwatch,
    order,
    paths,
    preflight,
    report,
    runner,
    suites,
)
from harness.context import say, write_json
from harness.failures import INFRA, PRODUCT
from harness.github import DryRun, GhCli, GitHub, GitHubError
from harness.publish import PYTEST_TEST, PYTEST_TIMED_OUT, step_publish
from harness.site import Site

GATES = Path(__file__).resolve().parent
GATES_REL = GATES.relative_to(paths.REPO).as_posix()
INI = GATES / "pytest.ini"
QUARANTINE = GATES / "quarantine.yaml"
WORKFLOW_TIMEOUT_S = max(s.workflow_s for s in suites.SUITES.values())
"""`nightly.yml` `timeout-minutes` = 가장 긴 묶음의 workflow 시간 — 넘기면 Actions가 job을 죽여
status·이력·publish.json이 하나도 남지 않는다(리뷰 r2 H1). 묶음별 한도는 `suites.Suite`."""
SETUP_RESERVE_S = 10 * 60
"""nightly.py 앞의 checkout·`uv sync`."""
PUBLISH_RESERVE_S = 5 * 60
"""publish(gh 호출)·cleanup(번들 삭제·VM 끄기)."""
TAIL_RESERVE_S = 15 * 60
"""run 뒤 collect·publish·cleanup에 남겨 두는 시간."""
EXIT = {report.PASSED: 0, PRODUCT: 1, INFRA: 2}
G2_SITES: tuple[Site, ...] = tuple(s for s, _ in g2.SITE_ROLES)
RunPytest = Callable[[Path, list[str], float], int]
Recover = Callable[[], tuple[bool, list[str]]]
"""infra 재시도 전 복구 — (다시 돌려도 되는가, 조치)."""
SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN")
"""G2 pytest 자식(VM·WSL을 부르는 프로세스 트리)에는 넘기지 않는 토큰(리뷰 L9)."""


def latest_run() -> str:
    runs = sorted(
        d.name.removeprefix("g2-")
        for d in g2.RESULTS.glob("g2-*")
        if d.is_dir() and g2.RUN_ID.fullmatch(d.name.removeprefix("g2-"))
    )
    if not runs:
        raise SystemExit(f"no g2 results under {g2.RESULTS} (give --run)")
    return runs[-1]


# ---------------------------------------------------------------- run


def pytest_env(run_id: str, sha: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    base = os.environ if environ is None else environ
    kept = {k: v for k, v in base.items() if k.upper() not in SECRET_ENV}
    return {**kept, "PUREIRON_G2_RUN": run_id, "PUREIRON_G2_SHA": sha}


def pytest_command(suite: suites.Suite) -> list[str]:
    """G2 pytest 명령 — 묶음 표지로 고른다(`g2`는 `weekly` 표지 시험을 빼고 `weekly`는 다 돈다)."""
    return [sys.executable, "-m", "pytest", "-c", str(INI), "-m", suite.marker]


def make_pytest(run_id: str, sha: str, suite: suites.Suite) -> RunPytest:
    def run_pytest(junit: Path, targets: list[str], timeout_s: float) -> int:
        cmd = pytest_command(suite)
        try:
            return subprocess.run(
                [*cmd, "--junitxml", str(junit), *targets],
                cwd=paths.REPO,
                env=pytest_env(run_id, sha),
                timeout=timeout_s,
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            return PYTEST_TIMED_OUT

    return run_pytest


def read_results(junit: Path) -> list[report.TestResult]:
    return report.parse_junit(junit.read_text(encoding="utf-8")) if junit.is_file() else []


def ci_state() -> tuple[int, bool]:
    count, note = preflight.ci_running()
    return count, note is None


def recover(
    *,
    ci: Callable[[], tuple[int, bool]] = ci_state,
    alive: Callable[[], bool] = preflight.wsl_ok,
    restart: Callable[[], list[str]] = preflight.recover_wsl,
    sleep: Callable[[float], None] = time.sleep,
    remove: Callable[[], list[str]] = lambda: preflight.remove_vms(list(G2_SITES)),
    reclaim: Callable[[], None] = buildhost.reclaim_host_memory,
    mem_now: Callable[[], float | None] = preflight.windows_mem_gb,
    min_mem_gb: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, list[str]]:
    """infra 재시도 전 복구: WSL이 끝내 답하지 않으면 foundation-ci가 돌지 않을 때만 재시작
    (`preflight.revive_wsl` — CI가 돌거나 상태를 모르면 재시작하지 않고 재시도도 하지 않는다), 남은
    G2 VM 삭제, WSL 페이지 캐시 반환(`preflight.reclaim_memory` — preflight와 같은 회수·대기).
    회수 뒤에도 Windows 가용 메모리가 기준(`min_mem_gb`, 기본 `PUREIRON_G2_MIN_MEM_GB`) 미만이면
    preflight가 멈출 상태와 같으므로 재시도하지 않는다."""
    ready, actions = preflight.revive_wsl(ci, alive=alive, restart=restart, sleep=sleep)
    if not ready:
        return False, actions
    actions += [f"removed vm {name}" for name in remove()]
    need = preflight.Limits.from_env(os.environ).min_mem_gb if min_mem_gb is None else min_mem_gb
    mem_gb, freed = preflight.reclaim_memory(
        need, mem_now(), mem_now=mem_now, reclaim=reclaim, sleep=sleep, clock=clock
    )
    actions += freed
    if mem_gb is None or mem_gb < need:
        return False, [*actions, f"windows available memory {mem_gb} GB < {need} GB: no retry"]
    return True, actions


FIRST_SESSION = "first"
"""재시도 전에 첫 세션의 현장 기록(`<결과>/<site>/`)을 옮겨 두는 하위 디렉터리(리뷰 L2)."""


def keep_first_session(out: Path, errors: list[str]) -> list[str]:
    """재시도 세션이 첫 세션의 현장 기록(`state.json`·로그 꼬리·`vm-memory.json`·
    `restore-memory.json`)을 덮어쓰거나 배열에 섞지 않게 `<결과>/first/<site>/`로 옮긴다(이미 있으면
    그대로 둔다 — 재시도는 한 번뿐이다). 옮긴 현장 id. 최선 노력이다: 옮기지 못하면(Windows에서
    백신·인덱서가 파일을 잡고 있으면 `PermissionError`) 현장마다 `errors`에 남기고 넘어간다 —
    기록 하나 때문에 재시도(그날의 판정)를 잃지 않게(M6-3b 리뷰 L3)."""
    moved: list[str] = []
    for one in G2_SITES:
        source, target = out / one.site_id, out / FIRST_SESSION / one.site_id
        if source.is_dir() and not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                source.rename(target)
            except OSError as exc:
                errors.append(f"{one.site_id}: {exc!r}")
                continue
            moved.append(one.site_id)
    return moved


def step_run(
    run_id: str,
    sha: str,
    out: Path,
    *,
    run_pytest: RunPytest,
    recover_env: Recover = recover,
    today: date,
    quarantine: list[flake.Quarantine] | None = None,
    warnings: list[str] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    suite: suites.Suite = suites.SUITES[suites.DEFAULT_SUITE],
) -> dict[str, object]:
    """첫 실행 → (infra 실패만) 복구 뒤 1회 재시도 → `summary.json`(묶음 이름 `suite` 포함). 시간
    제한은 `suite`의 값이다. `deadline`(`clock` 기준)을 주면 pytest 시간 제한을 남은 시간으로
    줄이고, 재시도할 시간이 `suite.retry_min_s`보다 적으면 건너뛴다.
    결과가 하나도 없으면(junit 없음) 시험 id `pytest`(infra) 한 줄 — 이력에 남아야 같은 실패가
    이틀 이어질 때 infra 이슈가 열린다(리뷰 r2 H1)."""
    out.mkdir(parents=True, exist_ok=True)
    entries = flake.load_quarantine(QUARANTINE) if quarantine is None else quarantine
    active, expired = flake.split_expired(entries, today)

    def left(cap: float) -> float:
        return cap if deadline is None else max(0.0, min(cap, deadline - clock()))

    codes = {"first": run_pytest(out / "junit.xml", [GATES_REL], left(suite.pytest_timeout_s))}
    first = read_results(out / "junit.xml")
    # 재시도는 새 세션이다 — 시나리오와 같은 현장의 외부 접속 0을 함께 묶는다(order.retry_closure)
    again_ids = order.retry_closure(report.retry_targets(first, active), [r.test_id for r in first])
    targets = [f"{GATES_REL}/{t}" for t in again_ids]
    retry: dict[str, object] = {"targets": targets, "done": False}
    if not first and codes["first"] == PYTEST_TIMED_OUT:
        retry["skipped"] = "pytest timed out without results (a hang does not heal on retry)"
    elif not first:  # 크래시 — 결과가 없으니 전체를 다시
        retry["targets"] = targets = [GATES_REL]
    results = first
    if targets and left(suite.retry_timeout_s) < suite.retry_min_s:
        retry["skipped"] = f"less than {suite.retry_min_s}s left before the workflow timeout"
    elif targets:
        ready, retry["recovery"] = recover_env()
        if not ready:
            retry["skipped"] = "environment not recovered (see recovery)"
        elif left(suite.retry_timeout_s) < suite.retry_min_s:
            retry["skipped"] = f"less than {suite.retry_min_s}s left after recovery"
        else:
            errors: list[str] = []
            retry["first_session"] = keep_first_session(out, errors)
            if errors:
                retry["first_session_error"] = errors
            timeout_s = left(suite.retry_timeout_s)
            codes["retry"] = run_pytest(out / "junit-retry.xml", targets, timeout_s)
            again = read_results(out / "junit-retry.xml")
            results = report.merge_retry(first, again) if first else again
            retry["done"] = True
    if not results:
        results = [report.TestResult(PYTEST_TEST, report.FAILED, INFRA, 0.0)]
    summary = report.summary(
        run_id=run_id,
        date=today.isoformat(),
        product_sha=sha,
        results=results,
        quarantined=active,
        extra={
            "suite": suite.name,
            "retry": retry,
            "pytest_rc": codes,
            "quarantine_expired": [{**asdict(e), "added": e.added.isoformat()} for e in expired],
            "warnings": list(warnings or []),
            "windows_memory": memwatch.read(out / memwatch.MEMORY_FILE),
        },
    )
    write_json(out / "summary.json", summary)
    return summary


def preflight_summary(
    run_id: str,
    sha: str,
    today: date,
    outcome: preflight.Outcome,
    suite: str = suites.DEFAULT_SUITE,
) -> dict[str, object]:
    """preflight에서 멈춘 실행 — 시험 대신 `preflight` 한 줄(infra)로 남긴다. 이력·status는 남고
    이슈·flake 비율에서는 빠진다(`report.not_started`)."""
    failed = report.TestResult(report.PREFLIGHT_TEST, report.FAILED, INFRA, 0.0)
    return report.summary(
        run_id=run_id,
        date=today.isoformat(),
        product_sha=sha,
        results=[failed],
        extra={"suite": suite, "preflight": outcome.as_dict(), "warnings": outcome.warnings},
    )


def announce(
    warnings: list[str], environ: Mapping[str, str] | None = None, *, level: str = "warning"
) -> None:
    """눈에 띄게 남길 메모 — GitHub Actions 안이면 `::warning::`(`level`이 notice면 `::notice::`)
    주석(실행 화면에 뜬다)."""
    actions = (os.environ if environ is None else environ).get("GITHUB_ACTIONS") == "true"
    for text in warnings:
        print(f"::{level}::{text}" if actions else f"{level.upper()}: {text}", flush=True)


# ---------------------------------------------------------------- collect·cleanup


def step_collect(run_id: str) -> dict[str, object]:
    return collect.step_collect(g2.results_dir(run_id), G2_SITES)


def inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def step_cleanup(run_id: str) -> dict[str, object]:
    target = g2.bundles_dir(run_id)
    if not inside(target, paths.BUNDLES) or target.resolve() == paths.BUNDLES.resolve():
        raise SystemExit(f"refusing to remove {target}: not a run directory under {paths.BUNDLES}")
    shutil.rmtree(target, ignore_errors=True)
    stopped: list[str] = []
    for one in G2_SITES:
        if hyperv.vm_state(one.vm_name) not in (None, "Off"):
            hyperv.turn_off(one.vm_name)
            stopped.append(one.vm_name)
    return {"bundles_removed": str(target), "vms_stopped": stopped}


def make_github(dry_run: bool) -> GitHub:
    if not dry_run:
        return GhCli.from_env()
    try:
        return DryRun(GhCli.from_env())
    except GitHubError:
        return DryRun()


def cleanup_actions(gh: GitHub) -> list[str]:
    removed = preflight.remove_vms(list(G2_SITES))
    old = preflight.prune_results(g2.RESULTS, datetime.now(UTC).timestamp())
    branches = preflight.prune_branches(gh, datetime.now(UTC))
    return [
        *(f"removed vm {name}" for name in removed),
        *(f"removed results {name}" for name in old),
        *(f"deleted branch {name}" for name in branches),
    ]


class AllBody(Protocol):
    """`run_all`과 같은 호출 모양 — 실행기 시험이 가짜로 바꿔 끼운다."""

    def __call__(
        self,
        run_id: str,
        sha: str,
        today: date,
        out: Path,
        gh: GitHub,
        /,
        *,
        suite: suites.Suite,
    ) -> int: ...


def run_all(
    run_id: str, sha: str, today: date, out: Path, gh: GitHub, *, suite: suites.Suite
) -> int:
    """preflight → run → collect → publish → cleanup. preflight 시작부터 `suite.run_budget_s` 안에
    run을 끝낸다(`step_run(deadline=)`)."""
    deadline = time.monotonic() + suite.run_budget_s
    limits = preflight.Limits.from_env(os.environ)
    outcome = preflight.run(
        limits,
        measure_now=lambda: preflight.measure(paths.E2E),
        cleanup=lambda: cleanup_actions(gh),
    )
    write_json(out / "preflight.json", outcome.as_dict())
    announce(outcome.warnings)
    try:
        if outcome.ok:
            summary = step_run(
                run_id,
                sha,
                out,
                run_pytest=make_pytest(run_id, sha, suite),
                today=today,
                warnings=outcome.warnings,
                deadline=deadline,
                suite=suite,
            )
            step_collect(run_id)
        else:
            summary = preflight_summary(run_id, sha, today, outcome, suite.name)
            write_json(out / "summary.json", summary)
        published = step_publish(summary, gh, today)
        write_json(out / "publish.json", published)
        say({"run": run_id, "verdict": summary["verdict"], "publish": published})
    finally:
        if outcome.ok:
            say(step_cleanup(run_id))
    return EXIT[str(summary["verdict"])]


def step_all(
    dry_run: bool,
    *,
    body: AllBody = run_all,
    github: Callable[[bool], GitHub] = make_github,
    head: Callable[[], str] = g2.head_sha,
    run_id: str | None = None,
    today: date | None = None,
    out_dir: Callable[[str], Path] = g2.results_dir,
    environ: Mapping[str, str] | None = None,
) -> int:
    """`run_all`을 묶음(`PUREIRON_G2_SUITE`, `harness.suites`)으로 돌리고, 예상 밖 예외(모르는 묶음
    이름·복구 중 Hyper-V 실패·gh 네트워크 실패 등)는 시험 id `runner`(infra) summary로 남겨
    publish를 시도한 뒤 exit 2로 끝낸다(리뷰 L2 — 기본 종료 코드 1은 product와 겹친다)."""
    env = os.environ if environ is None else environ
    run_id = run_id or g2.new_run_id()
    today = today or date.today()
    out = out_dir(run_id)
    sha, gh = runner.UNKNOWN_SHA, None
    suite_name = env.get(suites.SUITE_ENV) or suites.DEFAULT_SUITE
    try:
        suite = suites.from_env(env)
        sha = head()
        gh = github(dry_run)
        return body(run_id, sha, today, out, gh, suite=suite)
    except Exception as exc:
        runner.report_failure(run_id, sha, today, out, gh=gh, exc=exc, suite=suite_name)
        return EXIT[INFRA]


def step_preflight(*, judge_only: bool, dry_run: bool) -> int:
    limits = preflight.Limits.from_env(os.environ)

    def measure_now() -> preflight.Measure:
        return preflight.measure(paths.E2E)

    if judge_only:
        outcome = preflight.judge_only(limits, measure_now)
    else:
        gh = make_github(dry_run)
        outcome = preflight.run(
            limits, measure_now=measure_now, cleanup=lambda: cleanup_actions(gh)
        )
    say(outcome.as_dict())
    announce(outcome.warnings)
    return 0 if outcome.ok else EXIT[INFRA]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nightly", description="G2 nightly 실행기")
    sub = parser.add_subparsers(dest="command", required=True)
    every = sub.add_parser("all", help="preflight → run → collect → publish → cleanup")
    every.add_argument("--dry-run", action="store_true", help="GitHub에 쓰지 않고 출력만")
    pre = sub.add_parser("preflight", help="실행 환경 판단(조치 포함)")
    pre.add_argument("--judge-only", action="store_true", help="재고 판단만(복구·대기·정리 없음)")
    pre.add_argument("--dry-run", action="store_true", help="임시 브랜치를 지우지 않고 출력만")
    for name, text in (
        ("run", "G2 pytest + 분류 + infra 재시도"),
        ("collect", "VM 로그 수집"),
        ("publish", "이력·commit status·이슈"),
        ("cleanup", "run 번들 삭제·VM 끄기"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("--run", default=None, help="run id(기본: 가장 최근 결과)")
        if name == "run":
            cmd.add_argument("--sha", default=None, help="제품 커밋(기본: HEAD)")
        if name == "publish":
            cmd.add_argument("--dry-run", action="store_true", help="GitHub에 쓰지 않고 출력만")
            cmd.add_argument(
                "--junit", type=Path, default=None, help="이 junit으로 summary를 만든다"
            )
            cmd.add_argument("--sha", default=None, help="--junit과 함께: 제품 커밋")
    args = parser.parse_args(argv)
    today = date.today()
    if args.command == "all":
        return step_all(args.dry_run)
    if args.command == "preflight":
        return step_preflight(judge_only=args.judge_only, dry_run=args.dry_run)
    run_id = args.run or (g2.new_run_id() if args.command == "run" else latest_run())
    if g2.RUN_ID.fullmatch(run_id) is None:
        parser.error(f"--run must look like 20261110T180000Z, got {run_id!r}")
    if args.command == "run":
        sha = args.sha or g2.head_sha()
        out = g2.results_dir(run_id)
        suite = suites.from_env(os.environ)
        summary = step_run(
            run_id,
            sha,
            out,
            run_pytest=make_pytest(run_id, sha, suite),
            today=today,
            suite=suite,
        )
        say(summary)
        return EXIT[str(summary["verdict"])]
    if args.command == "collect":
        say(step_collect(run_id))
        return 0
    if args.command == "publish":
        if args.junit is not None:
            summary = report.summary(
                run_id=run_id,
                date=today.isoformat(),
                product_sha=args.sha or g2.head_sha(),
                results=report.parse_junit(args.junit.read_text(encoding="utf-8")),
            )
        else:
            summary = json.loads((g2.results_dir(run_id) / "summary.json").read_text("utf-8"))
        say(step_publish(summary, make_github(args.dry_run), today))
        return 0
    say(step_cleanup(run_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
