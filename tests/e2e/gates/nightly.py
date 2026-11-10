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
  다시(`junit-retry.xml`) → `summary.json`. `product`는 다시 돌리지 않는다.
- `collect`: VM마다 `updater.json`·메트릭·`journalctl -u pureiron-updater`·compose 로그를
  `<결과>/<site>/`로(실패해도 계속).
- `publish`: `e2e-history` 브랜치 `history.jsonl`에 줄 추가, commit status `g2/nightly`, product
  실패가 2회 연속이면 이슈(열린 것이 있으면 댓글). `--dry-run`이면 무엇을 보낼지 출력만 한다.
- `cleanup`: 이 run의 번들 디렉터리를 지우고 VM을 끈다(다음 preflight가 VM을 지운다).

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
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import buildhost, flake, g2, hyperv, paths, preflight, report
from harness.failures import INFRA, PRODUCT
from harness.github import HISTORY_BRANCH, HISTORY_FILE, DryRun, GhCli, GitHub, GitHubError
from harness.site import Site
from harness.vm import VmError

GATES = Path(__file__).resolve().parent
GATES_REL = GATES.relative_to(paths.REPO).as_posix()
INI = GATES / "pytest.ini"
QUARANTINE = GATES / "quarantine.yaml"
PYTEST_TIMEOUT_S = 75 * 60
RETRY_TIMEOUT_S = 30 * 60
COLLECT_TIMEOUT_S = 120
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


def say(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), flush=True)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", "utf-8")


def latest_run() -> str:
    runs = sorted(d.name for d in g2.RESULTS.glob("g2-*") if d.is_dir())
    if not runs:
        raise SystemExit(f"no g2 results under {g2.RESULTS} (give --run)")
    return runs[-1].removeprefix("g2-")


# ---------------------------------------------------------------- run


def pytest_env(run_id: str, sha: str) -> dict[str, str]:
    return {**os.environ, "PUREIRON_G2_RUN": run_id, "PUREIRON_G2_SHA": sha}


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
            return -1

    return run_pytest


def read_results(junit: Path) -> list[report.TestResult]:
    return report.parse_junit(junit.read_text(encoding="utf-8")) if junit.is_file() else []


def recover() -> list[str]:
    """infra 재시도 전 복구: WSL 무응답이면 재시작, 남은 G2 VM 삭제, WSL 페이지 캐시 반환."""
    actions = [] if preflight.wsl_ok() else preflight.recover_wsl()
    actions += [f"removed vm {name}" for name in preflight.remove_vms(list(G2_SITES))]
    buildhost.reclaim_host_memory()
    return [*actions, "reclaimed host memory"]


def step_run(
    run_id: str,
    sha: str,
    out: Path,
    *,
    run_pytest: RunPytest,
    recover_env: Callable[[], list[str]] = recover,
    today: date,
    quarantine: list[flake.Quarantine] | None = None,
) -> dict[str, object]:
    out.mkdir(parents=True, exist_ok=True)
    entries = flake.load_quarantine(QUARANTINE) if quarantine is None else quarantine
    active, expired = flake.split_expired(entries, today)
    run_pytest(out / "junit.xml", [GATES_REL], PYTEST_TIMEOUT_S)
    first = read_results(out / "junit.xml")
    targets = [f"{GATES_REL}/{t}" for t in report.retry_targets(first, active)]
    if not first:  # pytest가 결과를 못 남김(시간 초과·크래시) — infra로 보고 전체를 다시
        targets = [GATES_REL]
    retry: dict[str, object] = {"targets": targets, "done": False}
    results = first
    if targets:
        retry["recovery"] = recover_env()
        run_pytest(out / "junit-retry.xml", targets, RETRY_TIMEOUT_S)
        again = read_results(out / "junit-retry.xml")
        results = report.merge_retry(first, again) if first else again
        retry["done"] = True
    summary = report.summary(
        run_id=run_id,
        date=today.isoformat(),
        product_sha=sha,
        results=results,
        quarantined=active,
        extra={
            "retry": retry,
            "quarantine_expired": [{**asdict(e), "added": e.added.isoformat()} for e in expired],
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
        extra={"preflight": outcome.as_dict()},
    )


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


def step_publish(summary: dict[str, object], gh: GitHub, today: date) -> dict[str, object]:
    lines = report.history_lines(summary)
    old = report.parse_history(gh.read_file(HISTORY_BRANCH, HISTORY_FILE) or "")
    verdict, sha = str(summary["verdict"]), str(summary["product_sha"])
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
        number = gh.open_issue(report.ISSUE_LABEL)
        if number is None:
            gh.create_issue(report.ISSUE_TITLE, issue_body(summary), report.ISSUE_LABEL)
            issue = "created"
        else:
            gh.comment_issue(number, issue_body(summary))
            issue = f"commented #{number}"
    return {
        "history_lines": len(lines),
        "status": state,
        "issue": issue,
        "flake_rate_7d": flake.flake_rate(rows, today),
    }


def step_cleanup(run_id: str) -> dict[str, object]:
    shutil.rmtree(paths.BUNDLES / run_id, ignore_errors=True)
    stopped: list[str] = []
    for one in G2_SITES:
        if hyperv.vm_state(one.vm_name) not in (None, "Off"):
            hyperv.turn_off(one.vm_name)
            stopped.append(one.vm_name)
    return {"bundles_removed": str(paths.BUNDLES / run_id), "vms_stopped": stopped}


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


def step_all(dry_run: bool) -> int:
    run_id, sha, today = g2.new_run_id(), g2.head_sha(), date.today()
    out = g2.results_dir(run_id)
    gh = make_github(dry_run)
    limits = preflight.Limits.from_env(os.environ)
    outcome = preflight.run(
        limits,
        measure_now=lambda: preflight.measure(paths.E2E),
        cleanup=lambda: cleanup_actions(gh),
    )
    write_json(out / "preflight.json", outcome.as_dict())
    try:
        if outcome.ok:
            summary = step_run(run_id, sha, out, run_pytest=make_pytest(run_id, sha), today=today)
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
        limits = preflight.Limits.from_env(os.environ)

        def measure_now() -> preflight.Measure:
            return preflight.measure(paths.E2E)

        if args.judge_only:
            outcome = preflight.judge_only(limits, measure_now)
        else:
            gh = make_github(args.dry_run)
            outcome = preflight.run(
                limits, measure_now=measure_now, cleanup=lambda: cleanup_actions(gh)
            )
        say(outcome.as_dict())
        return 0 if outcome.ok else EXIT[INFRA]
    run_id = args.run or (g2.new_run_id() if args.command == "run" else latest_run())
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
