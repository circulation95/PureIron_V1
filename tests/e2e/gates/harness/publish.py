"""nightly `publish` 단계(M6-2) — `e2e-history` 브랜치 `history.jsonl`에 줄 추가, commit status
`g2/nightly`, product 실패가 2회 연속이면 이슈(열린 것이 있으면 댓글). 같은 시험이 2회 연속 infra로
실패해도 따로 이슈(라벨 `g2-nightly-infra`)를 연다 — 재시도로도 낫지 않는 실행 환경 문제나 제품
멈춤(시간 초과)이 "error"로만 쌓이지 않게. GitHub 호출은 `GitHub`(가짜·`DryRun`으로 바꿔 끼운다).
"""

from __future__ import annotations

import json
from datetime import date

from harness import flake, report
from harness.failures import PRODUCT
from harness.github import HISTORY_BRANCH, HISTORY_FILE, GitHub

PYTEST_TIMED_OUT = -1
"""nightly `run_pytest`가 시간 초과일 때 돌려주는 값."""
PYTEST_TEST = "pytest"
"""pytest가 결과(junit)를 남기지 못한 실행의 시험 id(infra)."""


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
