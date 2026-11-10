"""G2 nightly 실행기(E8·E9) — junit 분류·실행 판정·infra만 1회 재시도, 이력 줄 형식, commit status,
product 2연속 이슈, flake 비율(같은 SHA 안에서만), quarantine 만료, preflight 판단(가짜 측정값)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from pathlib import Path

import nightly
import pytest
from harness import flake, preflight, report
from harness.context import E2EError
from harness.github import DryRun

SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
CASE_A = "test_g2_install.py::test_g2_1_install_and_100_tags_good[site-a]"
CASE_B = "test_g2_install.py::test_g2_1_install_and_100_tags_good[site-b]"
TODAY = date(2026, 11, 10)


def case(name: str, kind: str | None = None, klass: str | None = None) -> str:
    props = (
        f'<properties><property name="failure_class" value="{klass}" /></properties>'
        if klass
        else ""
    )
    body = f'<{kind} message="boom">trace</{kind}>' if kind else ""
    return (
        f'<testcase classname="test_g2_install" name="{name}" file="test_g2_install.py" '
        f'time="12.5">{props}{body}</testcase>'
    )


def junit(*cases: str) -> str:
    suite = f'<testsuite name="pytest">{"".join(cases)}</testsuite>'
    return f'<?xml version="1.0"?><testsuites>{suite}</testsuites>'


NAME_A = CASE_A.split("::")[1]
NAME_B = CASE_B.split("::")[1]


def test_junit_cases_carry_outcome_and_class() -> None:
    results = report.parse_junit(
        junit(case(NAME_A), case(NAME_B, "error", "infra"), case("x", "failure"))
    )
    assert [(r.test_id, r.outcome, r.failure_class, r.seconds) for r in results] == [
        (CASE_A, "passed", None, 12.5),
        (CASE_B, "failed", "infra", 12.5),
        ("test_g2_install.py::x", "failed", "product", 12.5),
    ]


def results(*rows: tuple[str, str, str | None]) -> list[report.TestResult]:
    return [report.TestResult(t, o, c, 1.0) for t, o, c in rows]


def test_verdict_product_wins_over_infra() -> None:
    assert report.verdict(results((CASE_A, "passed", None))) == "passed"
    assert report.verdict(results((CASE_A, "failed", "infra"))) == "infra"
    mixed = results((CASE_A, "failed", "infra"), (CASE_B, "failed", "product"))
    assert report.verdict(mixed) == "product"
    assert report.verdict([]) == "infra"
    quarantined = results((CASE_A, "passed", None), (CASE_B, "failed", "product"))
    assert report.verdict(quarantined, frozenset({CASE_B})) == "passed"


def test_only_infra_failures_are_retried() -> None:
    """변이 (iii): product 실패를 다시 돌리면 안 된다."""
    infra = results((CASE_A, "passed", None), (CASE_B, "failed", "infra"))
    assert report.retry_targets(infra) == [CASE_B]
    product = results((CASE_A, "failed", "infra"), (CASE_B, "failed", "product"))
    assert report.retry_targets(product) == []
    assert report.retry_targets(results((CASE_A, "passed", None))) == []


def test_retry_result_replaces_the_first() -> None:
    first = results((CASE_A, "passed", None), (CASE_B, "failed", "infra"))
    merged = report.merge_retry(first, results((CASE_B, "passed", None)))
    assert [(r.test_id, r.outcome, r.retried) for r in merged] == [
        (CASE_A, "passed", False),
        (CASE_B, "passed", True),
    ]


def summary_of(run: str, *rows: tuple[str, str, str | None]) -> dict[str, object]:
    return report.summary(run_id=run, date="2026-11-10", product_sha=SHA, results=results(*rows))


def test_history_line_format() -> None:
    lines = report.history_lines(
        summary_of("20261110T180000Z", (CASE_A, "passed", None), (CASE_B, "failed", "infra"))
    )
    assert lines[1] == (
        '{"class": "infra", "date": "2026-11-10", "outcome": "fail", '
        f'"product_sha": "{SHA}", "run": "20261110T180000Z", "seconds": 1.0, '
        f'"test_id": "{CASE_B}"}}'
    )
    assert json.loads(lines[0])["class"] is None
    assert json.loads(lines[0])["outcome"] == "pass"


def test_status_state_and_description() -> None:
    assert report.STATUS_STATE == {"passed": "success", "product": "failure", "infra": "error"}
    text = report.status_description(
        summary_of("r", (CASE_A, "passed", None), (CASE_B, "failed", "product"))
    )
    assert text.startswith("G2 nightly product: 1/2 passed - failed test_g2_1_install")
    assert len(text) <= 140


def rows(
    run: str, outcome: str, klass: str | None, sha: str = SHA, day: str = "2026-11-09"
) -> dict[str, object]:
    return {"run": run, "outcome": outcome, "class": klass, "product_sha": sha, "date": day}


def test_issue_needs_two_consecutive_product_runs() -> None:
    assert report.needs_issue([rows("r1", "fail", "product"), rows("r2", "fail", "product")])
    assert not report.needs_issue([rows("r1", "fail", "product"), rows("r2", "fail", "infra")])
    assert not report.needs_issue([rows("r1", "fail", "infra"), rows("r2", "fail", "product")])
    assert not report.needs_issue([rows("r2", "fail", "product")])
    assert not report.needs_issue(
        [rows("r1", "fail", "product"), rows("r2", "fail", "product"), rows("r3", "pass", None)]
    )


def test_flake_rate_counts_mixed_outcomes_within_one_sha() -> None:
    """변이 (iv): SHA 구분을 없애면 다른 커밋의 통과·실패가 flake로 세어진다."""
    history = [
        rows("r1", "pass", None, sha="a" * 40),
        rows("r2", "fail", "infra", sha="a" * 40),
        rows("r3", "pass", None, sha="b" * 40),
        rows("r4", "fail", "product", sha="c" * 40),
    ]
    assert flake.flake_rate(history, TODAY) == 0.5
    different = [rows("r1", "pass", None, sha="a" * 40), rows("r2", "fail", None, sha="b" * 40)]
    assert flake.flake_rate(different, TODAY) == 0.0
    old = [rows("r1", "pass", None, day="2026-11-01"), rows("r2", "fail", None, day="2026-11-01")]
    assert flake.flake_rate(old, TODAY) == 0.0
    assert flake.flake_rate([], TODAY) == 0.0


def test_quarantine_needs_an_issue_and_expires_after_7_days() -> None:
    entries = flake.parse_quarantine(
        "quarantine:\n"
        f'  - test_id: "{CASE_B}"\n'
        "    issue: https://github.com/circulation95/PureIron_V1/issues/1\n"
        '    added: "2026-11-03"\n'
        f'  - test_id: "{CASE_A}"\n'
        "    issue: https://github.com/circulation95/PureIron_V1/issues/2\n"
        '    added: "2026-11-02"\n'
    )
    active, expired = flake.split_expired(entries, TODAY)
    assert active == frozenset({CASE_B})
    assert [e.test_id for e in expired] == [CASE_A]
    with pytest.raises(E2EError, match="issue link"):
        flake.parse_quarantine(
            'quarantine:\n  - test_id: x\n    issue: none\n    added: "2026-11-03"\n'
        )
    with pytest.raises(E2EError, match="YYYY-MM-DD"):
        flake.parse_quarantine(
            "quarantine:\n  - test_id: x\n"
            "    issue: https://github.com/a/b/issues/1\n    added: soon\n"
        )


def test_checked_in_quarantine_is_empty() -> None:
    assert flake.load_quarantine(nightly.QUARANTINE) == []


# ---------------------------------------------------------------- preflight


HEALTHY = preflight.Measure(mem_gb=9.7, disk_gb=128.0, wsl_ok=True, docker_ok=True, ci_running=0)


def measure(**changes: object) -> preflight.Measure:
    return dataclasses.replace(HEALTHY, **changes)


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({}, None),
        ({"mem_gb": 7.9}, "windows available memory 7.9 GB < 8.0 GB"),
        ({"mem_gb": None}, "windows available memory None GB < 8.0 GB"),
        ({"disk_gb": 39.0}, "C: free 39.0 GB < 40.0 GB"),
        ({"wsl_ok": False}, "WSL is not responding"),
        ({"docker_ok": False}, "docker (WSL) is not responding"),
        ({"ci_running": 1}, "1 foundation-ci run(s) still in progress"),
    ],
)
def test_preflight_judges_each_limit(changes: dict[str, object], problem: str | None) -> None:
    problems = preflight.judge(measure(**changes), preflight.Limits())
    assert problems == ([] if problem is None else [problem])


def test_preflight_limits_come_from_the_environment() -> None:
    limits = preflight.Limits.from_env(
        {"PUREIRON_G2_MIN_MEM_GB": "64", "PUREIRON_G2_CI_WAIT_S": "0"}
    )
    assert (limits.min_mem_gb, limits.min_disk_gb, limits.ci_wait_s) == (64.0, 40.0, 0.0)
    assert preflight.judge(measure(), limits) == ["windows available memory 9.7 GB < 64.0 GB"]
    with pytest.raises(E2EError, match="PUREIRON_G2_MIN_DISK_GB"):
        preflight.Limits.from_env({"PUREIRON_G2_MIN_DISK_GB": "lots"})


def test_preflight_restarts_wsl_once_and_waits_for_ci() -> None:
    seen = [measure(wsl_ok=False), measure(ci_running=1), measure(ci_running=1), measure()]
    clock = iter([0.0, 0.0, 60.0, 120.0])
    cleaned: list[str] = []

    def cleanup() -> list[str]:
        cleaned.append("x")
        return ["removed vm pureiron-site-a"]

    outcome = preflight.run(
        preflight.Limits(),
        measure_now=lambda: seen.pop(0),
        recover=lambda: ["wsl --shutdown -> 0"],
        cleanup=cleanup,
        sleep=lambda _: None,
        clock=lambda: next(clock),
    )
    assert outcome.ok
    assert outcome.actions == [
        "wsl not responding: restart",
        "wsl --shutdown -> 0",
        "waiting for 1 foundation-ci run(s)",
        "waiting for 1 foundation-ci run(s)",
        "removed vm pureiron-site-a",
    ]
    assert cleaned == ["x"]


def test_preflight_gives_up_and_skips_cleanup_when_ci_keeps_running() -> None:
    clock = iter([0.0, 0.0, 1000.0, 2000.0])
    outcome = preflight.run(
        preflight.Limits(),
        measure_now=lambda: measure(ci_running=2),
        cleanup=lambda: pytest.fail("cleanup must not run on a failed preflight"),
        sleep=lambda _: None,
        clock=lambda: next(clock),
    )
    assert outcome.problems == ["2 foundation-ci run(s) still in progress"]


def test_judge_only_takes_no_action() -> None:
    outcome = preflight.judge_only(preflight.Limits(), lambda: measure(wsl_ok=False))
    assert (outcome.ok, outcome.actions) == (False, [])
    assert outcome.as_dict()["problems"] == ["WSL is not responding"]


def test_stale_entries_are_older_than_the_keep_window() -> None:
    now = datetime(2026, 11, 10, tzinfo=UTC).timestamp()
    entries = {"g2-new": now - 13 * 86400, "g2-old": now - 15 * 86400}
    assert preflight.stale(entries, now, 14) == ["g2-old"]


# ---------------------------------------------------------------- run·publish


class FakePytest:
    """pytest 실행 대신 정해 둔 junit을 차례로 쓴다."""

    def __init__(self, *outputs: str | None) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, junit_path: Path, targets: list[str], timeout_s: float) -> int:
        del timeout_s
        self.calls.append((junit_path.name, targets))
        text = self.outputs.pop(0)
        if text is not None:
            junit_path.write_text(text, encoding="utf-8")
        return 0


def run_once(tmp_path: Path, fake: FakePytest) -> tuple[dict[str, object], list[str]]:
    recovered: list[str] = []

    def recover() -> list[str]:
        recovered.append("x")
        return ["reclaimed host memory"]

    summary = nightly.step_run(
        "R1", SHA, tmp_path, run_pytest=fake, recover_env=recover, today=TODAY, quarantine=[]
    )
    return summary, recovered


def test_infra_failure_is_retried_once_with_the_failed_node(tmp_path: Path) -> None:
    fake = FakePytest(junit(case(NAME_A), case(NAME_B, "error", "infra")), junit(case(NAME_B)))
    summary, recovered = run_once(tmp_path, fake)
    assert fake.calls == [
        ("junit.xml", ["tests/e2e/gates"]),
        ("junit-retry.xml", [f"tests/e2e/gates/{CASE_B}"]),
    ]
    assert recovered == ["x"]
    assert summary["verdict"] == "passed"
    assert (
        json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["verdict"] == "passed"
    )


def test_product_failure_is_not_retried(tmp_path: Path) -> None:
    fake = FakePytest(junit(case(NAME_A, "failure", "product"), case(NAME_B, "error", "infra")))
    summary, recovered = run_once(tmp_path, fake)
    assert len(fake.calls) == 1
    assert recovered == []
    assert summary["verdict"] == "product"


def test_missing_junit_is_infra_and_reruns_everything(tmp_path: Path) -> None:
    fake = FakePytest(None, junit(case(NAME_A, "failure", "product"), case(NAME_B)))
    summary, _ = run_once(tmp_path, fake)
    assert fake.calls[1] == ("junit-retry.xml", ["tests/e2e/gates"])
    assert summary["verdict"] == "product"


class FakeGitHub:
    def __init__(self, history: str = "", open_issue: int | None = None) -> None:
        self.history = history
        self.issue = open_issue
        self.calls: list[tuple[str, object]] = []
        self.appends: list[tuple[str, str, str]] = []

    def read_file(self, branch: str, path: str) -> str | None:
        return self.history or None

    def append_file(self, branch: str, path: str, text: str, message: str) -> None:
        self.calls.append(("append", branch))
        self.appends.append((branch, path, text))

    def set_status(self, sha: str, state: str, context: str, description: str) -> None:
        self.calls.append(("status", (sha, state, context)))

    def open_issue(self, label: str) -> int | None:
        return self.issue

    def create_issue(self, title: str, body: str, label: str) -> None:
        self.calls.append(("issue", (title, label)))

    def comment_issue(self, number: int, body: str) -> None:
        self.calls.append(("comment", number))

    def temp_branches(self, prefix: str) -> list[tuple[str, datetime]]:
        return []

    def delete_branch(self, name: str) -> None:
        self.calls.append(("delete", name))


def previous_product_run() -> str:
    return json.dumps(rows("20261109T180000Z", "fail", "product")) + "\n"


def test_publish_appends_history_and_sets_status() -> None:
    gh = FakeGitHub()
    out = nightly.step_publish(
        summary_of("20261110T180000Z", (CASE_A, "passed", None), (CASE_B, "passed", None)),
        gh,
        TODAY,
    )
    assert [name for name, _ in gh.calls] == ["append", "status"]
    ((branch, path, text),) = gh.appends
    assert (branch, path) == ("e2e-history", "history.jsonl")
    assert text.count("\n") == 2
    assert gh.calls[1] == ("status", (SHA, "success", "g2/nightly"))
    assert out["issue"] is None


def test_second_product_failure_opens_one_issue_then_comments() -> None:
    failing = summary_of("20261110T180000Z", (CASE_B, "failed", "product"))
    gh = FakeGitHub(history=previous_product_run())
    assert nightly.step_publish(failing, gh, TODAY)["issue"] == "created"
    assert ("issue", (report.ISSUE_TITLE, "g2-nightly")) in gh.calls
    gh = FakeGitHub(history=previous_product_run(), open_issue=7)
    assert nightly.step_publish(failing, gh, TODAY)["issue"] == "commented #7"
    assert ("comment", 7) in gh.calls


def test_infra_failure_sets_error_status_without_an_issue() -> None:
    gh = FakeGitHub(history=previous_product_run())
    out = nightly.step_publish(
        summary_of("20261110T180000Z", (CASE_B, "failed", "infra")), gh, TODAY
    )
    assert ("status", (SHA, "error", "g2/nightly")) in gh.calls
    assert out["issue"] is None


def test_dry_run_prints_writes_instead_of_sending(capsys: pytest.CaptureFixture[str]) -> None:
    reader = FakeGitHub(history=previous_product_run(), open_issue=None)
    gh = DryRun(reader)
    out = nightly.step_publish(
        summary_of("20261110T180000Z", (CASE_B, "failed", "product")), gh, TODAY
    )
    assert reader.calls == []
    assert [w["dry_run"] for w in gh.writes] == ["append_file", "set_status", "create_issue"]
    assert out["issue"] == "created"
    assert '"dry_run": "set_status"' in capsys.readouterr().out
