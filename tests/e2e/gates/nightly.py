"""G2 nightly 실행기(M6-2) — Windows 호스트(self-hosted runner `e2e`)에서 돈다. 단계마다 따로
부를 수 있다:

    uv run python tests/e2e/gates/nightly.py all [--dry-run]
    uv run python tests/e2e/gates/nightly.py preflight [--judge-only] [--dry-run]
    uv run python tests/e2e/gates/nightly.py run [--run RUN]
    uv run python tests/e2e/gates/nightly.py collect [--run RUN]
    uv run python tests/e2e/gates/nightly.py publish [--run RUN] [--dry-run] [--junit F --sha S]
    uv run python tests/e2e/gates/nightly.py cleanup [--run RUN]

- `preflight`(`harness.preflight`): 메모리·디스크·WSL·Docker·foundation-ci 판단, 복구·정리.
- `run`: `pytest -c tests/e2e/gates/pytest.ini -m g2 --junitxml …/junit.xml` → 분류
  (`harness.report`) → 실패가 모두 `infra`면 복구(WSL·남은 VM·호스트 메모리) 뒤 실패한 시험만 1회
  다시(`junit-retry.xml`) → `summary.json`. `product`는 다시 돌리지 않는다. pytest가 결과(junit)를
  남기지 못하면 시험 id `pytest`(infra) 한 줄로 남긴다 — 시간 초과였으면 다시 돌리지 않고(멈춘
  제품은 재시도로 낫지 않는다), 크래시였으면 전체를 1회 다시 돌린다.
- `collect`: VM마다 `updater.json`·메트릭·`journalctl -u pureiron-updater`·compose 로그를
  `<결과>/<site>/`로(실패해도 계속).
- `publish`: `e2e-history` 브랜치 `history.jsonl`에 줄 추가, commit status `g2/nightly`, product
  실패가 2회 연속이면 이슈(열린 것이 있으면 댓글). 같은 시험이 2회 연속 infra로 실패해도 따로
  이슈(라벨 `g2-nightly-infra`)를 연다 — 재시도로도 낫지 않는 실행 환경 문제나 제품 멈춤(시간
  초과)이 "error"로만 쌓이지 않게. `--dry-run`이면 무엇을 보낼지 출력만 한다.
- `cleanup`: 이 run의 번들 디렉터리를 지우고 VM을 끈다(다음 preflight가 VM을 지운다).

`all`은 cron(`schedule` 이벤트)으로 시작한 실행이 cron 시각보다 2시간 넘게 늦었으면 아무것도 하지
않고 exit 0으로 끝난다(`harness.schedule` — preflight·GitHub 호출·결과 디렉터리 없음). PC가 꺼져
있다가 낮에 켜졌을 때 VM을 띄우지 않게. 수동 실행·로컬 실행에는 적용하지 않는다.

`all`이 예상 밖 예외로 멈추면 시험 id `runner`(infra) 하나로 `runner-summary.json`을 남기고
publish를 시도한 뒤 exit 2로 끝난다 — 그 run의 `summary.json`이 이미 있으면 덮어쓰지 않고 그것으로
publish를 다시 시도한다. run id는 `20261110T180000Z` 형식만 받는다.

시간 예산: `all`은 workflow `timeout-minutes`(120분) 안에 끝나야 status·이력이 남는다. preflight
시작부터 `RUN_BUDGET_S` 안에 첫 실행·복구·재시도를 끝내고(pytest 시간 제한을 남은 시간으로
줄이고, 남은 시간이 `RETRY_MIN_S`보다 짧으면 재시도를 건너뛴다), collect·publish·cleanup은
`TAIL_RESERVE_S` 안에 든다.

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
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import buildhost, flake, g2, hyperv, paths, preflight, report, schedule
from harness.context import TAIL_CHARS, log
from harness.failures import INFRA, PRODUCT
from harness.github import HISTORY_BRANCH, HISTORY_FILE, DryRun, GhCli, GitHub, GitHubError
from harness.site import Site
from harness.vm import VmError

GATES = Path(__file__).resolve().parent
GATES_REL = GATES.relative_to(paths.REPO).as_posix()
INI = GATES / "pytest.ini"
QUARANTINE = GATES / "quarantine.yaml"
WORKFLOW_TIMEOUT_S = 120 * 60
"""`nightly.yml` `timeout-minutes` — 넘기면 Actions가 job을 죽여 status·이력·publish.json이 하나도
남지 않는다(리뷰 r2 H1)."""
SETUP_RESERVE_S = 10 * 60
"""nightly.py 앞의 checkout·`uv sync`."""
COLLECT_TIMEOUT_S = 60
"""collect의 원격 명령 하나 — 현장 2개, 명령 5개, 60초씩이면 최대 10분."""
PUBLISH_RESERVE_S = 5 * 60
"""publish(gh 호출)·cleanup(번들 삭제·VM 끄기)."""
TAIL_RESERVE_S = 15 * 60
"""run 뒤 collect·publish·cleanup에 남겨 두는 시간."""
RUN_BUDGET_S = 90 * 60
"""preflight 시작부터 첫 실행·복구·재시도가 끝나야 하는 시간."""
PYTEST_TIMEOUT_S = 75 * 60
RETRY_TIMEOUT_S = 30 * 60
RETRY_MIN_S = 10 * 60
"""재시도에 이만큼도 남지 않으면 건너뛴다(끝내지 못할 재시도로 예산만 쓰지 않게)."""
PYTEST_TIMED_OUT = -1
"""`run_pytest`가 시간 초과일 때 돌려주는 값."""
PYTEST_TEST = "pytest"
"""pytest가 결과(junit)를 남기지 못한 실행의 시험 id(infra)."""
EXIT = {report.PASSED: 0, PRODUCT: 1, INFRA: 2}
G2_SITES: tuple[Site, ...] = tuple(s for s, _ in g2.SITE_ROLES)
REMOTE_LOGS = {
    "updater.json": "sudo cat /var/lib/pureiron/state/updater.json",
    "updater.prom": "sudo cat /var/lib/pureiron/state/metrics/updater.prom",
    "journal-pureiron-updater.txt": "sudo journalctl -u pureiron-updater --no-pager -o short-iso",
    "compose-logs.txt": (
        "for c in $(sudo docker ps -aq --filter label=com.docker.compose.project=pureiron); do "
        "echo \"== $(sudo docker inspect -f '{{.Name}}' $c)\"; "
        "sudo docker logs --timestamps --tail 3000 $c 2>&1; done"
    ),
    "simulator-logs.txt": "sudo docker logs --timestamps --tail 300 e2e-simulator 2>&1",
}
RunPytest = Callable[[Path, list[str], float], int]
Recover = Callable[[], tuple[bool, list[str]]]
"""infra 재시도 전 복구 — (다시 돌려도 되는가, 조치)."""
SECRET_ENV = ("GH_TOKEN", "GITHUB_TOKEN")
"""G2 pytest 자식(VM·WSL을 부르는 프로세스 트리)에는 넘기지 않는 토큰(리뷰 L9)."""
RUNNER_TEST = "runner"
UNKNOWN_SHA = "0" * 40


def say(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), flush=True)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")


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


def make_pytest(run_id: str, sha: str) -> RunPytest:
    def run_pytest(junit: Path, targets: list[str], timeout_s: float) -> int:
        cmd = [sys.executable, "-m", "pytest", "-c", str(INI), "-m", "g2"]
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
) -> dict[str, object]:
    """첫 실행 → (infra 실패만) 복구 뒤 1회 재시도 → `summary.json`. `deadline`(`clock` 기준)을 주면
    pytest 시간 제한을 남은 시간으로 줄이고, 재시도할 시간이 `RETRY_MIN_S`보다 적으면 건너뛴다.
    결과가 하나도 없으면(junit 없음) 시험 id `pytest`(infra) 한 줄 — 이력에 남아야 같은 실패가
    이틀 이어질 때 infra 이슈가 열린다(리뷰 r2 H1)."""
    out.mkdir(parents=True, exist_ok=True)
    entries = flake.load_quarantine(QUARANTINE) if quarantine is None else quarantine
    active, expired = flake.split_expired(entries, today)

    def left(cap: float) -> float:
        return cap if deadline is None else max(0.0, min(cap, deadline - clock()))

    codes = {"first": run_pytest(out / "junit.xml", [GATES_REL], left(PYTEST_TIMEOUT_S))}
    first = read_results(out / "junit.xml")
    targets = [f"{GATES_REL}/{t}" for t in report.retry_targets(first, active)]
    retry: dict[str, object] = {"targets": targets, "done": False}
    if not first and codes["first"] == PYTEST_TIMED_OUT:
        retry["skipped"] = "pytest timed out without results (a hang does not heal on retry)"
    elif not first:  # 크래시 — 결과가 없으니 전체를 다시
        retry["targets"] = targets = [GATES_REL]
    results = first
    if targets and left(RETRY_TIMEOUT_S) < RETRY_MIN_S:
        retry["skipped"] = f"less than {RETRY_MIN_S}s left before the workflow timeout"
    elif targets:
        ready, retry["recovery"] = recover_env()
        if not ready:
            retry["skipped"] = "environment not recovered (see recovery)"
        elif left(RETRY_TIMEOUT_S) < RETRY_MIN_S:
            retry["skipped"] = f"less than {RETRY_MIN_S}s left after recovery"
        else:
            codes["retry"] = run_pytest(out / "junit-retry.xml", targets, left(RETRY_TIMEOUT_S))
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
            "retry": retry,
            "pytest_rc": codes,
            "quarantine_expired": [{**asdict(e), "added": e.added.isoformat()} for e in expired],
            "warnings": list(warnings or []),
        },
    )
    write_json(out / "summary.json", summary)
    return summary


def preflight_summary(
    run_id: str, sha: str, today: date, outcome: preflight.Outcome
) -> dict[str, object]:
    """preflight에서 멈춘 실행 — 시험 대신 `preflight` 한 줄(infra)로 남긴다."""
    failed = report.TestResult("preflight", report.FAILED, INFRA, 0.0)
    return report.summary(
        run_id=run_id,
        date=today.isoformat(),
        product_sha=sha,
        results=[failed],
        extra={"preflight": outcome.as_dict(), "warnings": outcome.warnings},
    )


def runner_summary(run_id: str, sha: str, today: date, exc: BaseException) -> dict[str, object]:
    """실행기 자신이 예상 밖 예외로 멈춘 실행 — 시험 id `runner` 한 줄(infra)로 남긴다."""
    failed = report.TestResult(RUNNER_TEST, report.FAILED, INFRA, 0.0)
    return report.summary(
        run_id=run_id,
        date=today.isoformat(),
        product_sha=sha,
        results=[failed],
        extra={"runner_error": repr(exc)[-TAIL_CHARS:]},
    )


def announce(
    warnings: list[str], environ: Mapping[str, str] | None = None, *, level: str = "warning"
) -> None:
    """눈에 띄게 남길 메모 — GitHub Actions 안이면 `::warning::`(`level`이 notice면 `::notice::`)
    주석(실행 화면에 뜬다)."""
    actions = (os.environ if environ is None else environ).get("GITHUB_ACTIONS") == "true"
    for text in warnings:
        print(f"::{level}::{text}" if actions else f"{level.upper()}: {text}", flush=True)


# ---------------------------------------------------------------- collect·publish·cleanup


def collect_site(one: Site, dest: Path) -> dict[str, object]:
    """원격 명령마다 출력 파일 하나. 실패(ssh·시간 초과)는 결과에 남기고 다음으로 간다."""
    dest.mkdir(parents=True, exist_ok=True)
    host = one.host()
    got: dict[str, object] = {}
    for name, command in REMOTE_LOGS.items():
        try:
            done = host.run(command, check=False, timeout_s=COLLECT_TIMEOUT_S)
        except (VmError, subprocess.TimeoutExpired) as exc:
            got[name] = f"error: {exc}"
            continue
        (dest / name).write_text(done.stdout, encoding="utf-8")
        got[name] = done.returncode
    return got


def step_collect(run_id: str) -> dict[str, object]:
    out = g2.results_dir(run_id)
    collected: dict[str, object] = {
        one.site_id: collect_site(one, out / one.site_id) for one in G2_SITES
    }
    write_json(out / "collect.json", collected)
    return collected


def issue_body(summary: dict[str, object]) -> str:
    failed = [
        f"- `{t.get('test_id')}` ({t.get('failure_class')})"
        for t in report.test_rows(summary)
        if t.get("outcome") == report.FAILED
    ]
    return "\n".join(
        [
            f"G2 nightly run `{summary.get('run')}` on `{summary.get('product_sha')}` "
            "failed with a product failure, and so did the previous run.",
            "",
            *failed,
            "",
            f"Results: `results/g2-{summary.get('run')}` on the e2e runner.",
        ]
    )


def infra_issue_body(summary: dict[str, object], tests: list[str]) -> str:
    return "\n".join(
        [
            f"G2 nightly run `{summary.get('run')}` on `{summary.get('product_sha')}`: these tests "
            "failed as infra in this run and in the previous run (after the infra retry):",
            "",
            *(f"- `{t}`" for t in tests),
            "",
            "Infra failures are not product verdicts, but a repeated one needs a person: the test "
            "environment may be broken, or the product may hang (a timeout is classified as "
            "infra) or a remote step may exit 255. Check the junit and logs.",
            "",
            f"pytest exit codes: `{json.dumps(summary.get('pytest_rc'))}` "
            f"({PYTEST_TIMED_OUT} = timed out; test `{PYTEST_TEST}` = no junit at all)",
            "",
            f"Results: `results/g2-{summary.get('run')}` on the e2e runner.",
        ]
    )


def raise_issue(gh: GitHub, label: str, title: str, body: str) -> str:
    """`label`로 열린 이슈가 있으면 댓글, 없으면 새 이슈."""
    number = gh.open_issue(label)
    if number is None:
        gh.create_issue(title, body, label)
        return "created"
    gh.comment_issue(number, body)
    return f"commented #{number}"


def step_publish(summary: dict[str, object], gh: GitHub, today: date) -> dict[str, object]:
    lines = report.history_lines(summary)
    old = report.parse_history(gh.read_file(HISTORY_BRANCH, HISTORY_FILE) or "")
    verdict, sha = str(summary["verdict"]), str(summary["product_sha"])
    if any(row.get("run") == summary["run"] for row in old):
        lines = []  # 이 run은 이미 이력에 있다(publish 재시도) — 줄을 두 번 쓰지 않는다
    if lines:
        gh.append_file(
            HISTORY_BRANCH,
            HISTORY_FILE,
            "".join(line + "\n" for line in lines),
            f"g2 nightly {summary['run']}: {verdict} ({sha[:7]})",
        )
    state = report.STATUS_STATE[verdict]
    gh.set_status(sha, state, report.STATUS_CONTEXT, report.status_description(summary))
    rows = old + report.parse_history("\n".join(lines))
    issue: str | None = None
    if verdict == PRODUCT and report.needs_issue(rows):
        issue = raise_issue(gh, report.ISSUE_LABEL, report.ISSUE_TITLE, issue_body(summary))
    infra_issue: str | None = None
    repeated = report.repeated_infra(rows)
    if repeated:
        infra_issue = raise_issue(
            gh,
            report.INFRA_ISSUE_LABEL,
            report.INFRA_ISSUE_TITLE,
            infra_issue_body(summary, repeated),
        )
    return {
        "history_lines": len(lines),
        "status": state,
        "issue": issue,
        "infra_issue": infra_issue,
        "flake_rate_7d": flake.flake_rate(rows, today),
    }


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


AllBody = Callable[[str, str, date, Path, GitHub], int]


def run_all(run_id: str, sha: str, today: date, out: Path, gh: GitHub) -> int:
    """preflight → run → collect → publish → cleanup. preflight 시작부터 `RUN_BUDGET_S` 안에 run을
    끝낸다(`step_run(deadline=)`)."""
    deadline = time.monotonic() + RUN_BUDGET_S
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
                run_pytest=make_pytest(run_id, sha),
                today=today,
                warnings=outcome.warnings,
                deadline=deadline,
            )
            step_collect(run_id)
        else:
            summary = preflight_summary(run_id, sha, today, outcome)
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
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """`run_all`을 돌리고, 예상 밖 예외(복구 중 Hyper-V 실패·gh 네트워크 실패 등)는 시험 id
    `runner`(infra) summary로 남겨 publish를 시도한 뒤 exit 2로 끝낸다(리뷰 L2 — 기본 종료 코드 1은
    product와 겹친다). 맨 앞에서 늦게 시작한 cron 실행이면(`schedule.late_start`) 아무것도 하지
    않고 0으로 끝낸다 — preflight는 VM·결과를 지우므로 부르지 않는다(G7)."""
    env = os.environ if environ is None else environ
    started = now()
    if schedule.late_start(env.get("GITHUB_EVENT_NAME", ""), schedule.CRON, started):
        announce(
            [
                f"scheduled run started at {started:%Y-%m-%d %H:%M} UTC, more than "
                f"{schedule.LATE_AFTER} after cron '{schedule.CRON}': skipped (nothing run)"
            ],
            env,
            level="notice",
        )
        return 0
    run_id = run_id or g2.new_run_id()
    today = today or date.today()
    out = out_dir(run_id)
    sha, gh = UNKNOWN_SHA, None
    try:
        sha = head()
        gh = github(dry_run)
        return body(run_id, sha, today, out, gh)
    except Exception as exc:
        log(f"nightly runner failed: {exc!r}")
        runner = runner_summary(run_id, sha, today, exc)
        write_json(out / "runner-summary.json", runner)
        # 그 run의 실제 결과가 이미 있으면(예: publish·cleanup에서 멈춤) 덮어쓰지 않고 그것을 낸다
        # (리뷰 r2 L4) — 이미 이력에 들어갔으면 step_publish가 줄을 다시 쓰지 않는다.
        real = read_summary(out / "summary.json", run_id)
        summary = runner if real is None else real
        published: dict[str, object] = {"error": "not published (no GitHub client or sha)"}
        if gh is not None and sha != UNKNOWN_SHA:
            try:
                published = step_publish(summary, gh, today)
            except Exception as publish_exc:
                published = {"error": f"publish failed: {publish_exc!r}"[-TAIL_CHARS:]}
        published["runner_error"] = runner["runner_error"]
        published["published_summary"] = "runner-summary.json" if real is None else "summary.json"
        write_json(out / "publish.json", published)
        say({"run": run_id, "verdict": summary["verdict"], "publish": published})
        return EXIT[INFRA]


def read_summary(path: Path, run_id: str) -> dict[str, object] | None:
    """이 run의 `summary.json`(없거나, 깨졌거나, 다른 run이면 None)."""
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    summary = cast(dict[str, object], data)
    if summary.get("run") != run_id or summary.get("verdict") not in EXIT:
        return None
    return summary


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
        summary = step_run(run_id, sha, out, run_pytest=make_pytest(run_id, sha), today=today)
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
