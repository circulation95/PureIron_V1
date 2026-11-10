"""G2 nightly 실행기(E8·E9) — junit 분류·실행 판정·infra만 1회 재시도, 이력 줄 형식, commit status,
product 2연속 이슈, flake 비율(같은 SHA 안에서만), quarantine 만료, preflight 판단(가짜 측정값)."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

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


def test_preflight_waits_for_ci_before_restarting_wsl() -> None:
    """M2: CI 대기가 WSL 재시작 판단보다 먼저다. CI가 끝난 뒤에도 echo가 끝내 답하지 않을 때만
    재시작한다."""
    seen = [
        measure(wsl_ok=False, ci_running=1),
        measure(wsl_ok=False, ci_running=1),
        measure(wsl_ok=False),
        measure(),
    ]
    clock = iter([0.0, 0.0, 60.0, 120.0])
    cleaned: list[str] = []
    echoes: list[str] = []

    def cleanup() -> list[str]:
        cleaned.append("x")
        return ["removed vm pureiron-site-a"]

    def alive() -> bool:
        echoes.append("echo")
        return False

    outcome = preflight.run(
        preflight.Limits(),
        measure_now=lambda: seen.pop(0),
        recover=lambda: ["wsl --shutdown -> 0"],
        wsl_alive=alive,
        cleanup=cleanup,
        sleep=lambda _: None,
        clock=lambda: next(clock),
    )
    assert outcome.ok
    assert outcome.actions == [
        "waiting for 1 foundation-ci run(s)",
        "waiting for 1 foundation-ci run(s)",
        "wsl not responding: restart",
        "wsl --shutdown -> 0",
        "removed vm pureiron-site-a",
    ]
    assert len(echoes) == preflight.WSL_ECHO_TRIES - 1  # 측정이 1번, 재시작 전에 더 묻는다
    assert cleaned == ["x"]


def test_preflight_never_restarts_wsl_while_ci_runs() -> None:
    """M2: CI 대기가 끝나도 CI가 돌면 재시작하지 않고 infra로 멈춘다."""
    clock = iter([0.0, 0.0, 2000.0])
    outcome = preflight.run(
        preflight.Limits(),
        measure_now=lambda: measure(wsl_ok=False, ci_running=1),
        recover=lambda: pytest.fail("WSL must not restart while foundation-ci runs"),
        wsl_alive=lambda: False,
        cleanup=lambda: pytest.fail("cleanup must not run on a failed preflight"),
        sleep=lambda _: None,
        clock=lambda: next(clock),
    )
    assert not outcome.ok
    assert "1 foundation-ci run(s) still in progress" in outcome.problems
    assert "wsl not responding, 1 foundation-ci run(s) in progress: no restart" in outcome.actions


def test_preflight_does_not_restart_wsl_when_ci_status_is_unknown() -> None:
    outcome = preflight.run(
        preflight.Limits(),
        measure_now=lambda: measure(wsl_ok=False, ci_known=False),
        recover=lambda: pytest.fail("WSL must not restart without knowing foundation-ci"),
        wsl_alive=lambda: False,
        sleep=lambda _: None,
        clock=lambda: 0.0,
    )
    assert "wsl not responding, foundation-ci status unknown: no restart" in outcome.actions
    assert outcome.problems == ["WSL is not responding"]


def test_preflight_keeps_wsl_when_a_retried_echo_answers() -> None:
    """M2(c): 30초 echo 한 번의 무응답은 "느림"일 수 있다 — 다시 물어 답하면 재시작하지 않는다."""
    answers = iter([False, True])
    seen = [measure(wsl_ok=False), measure()]
    outcome = preflight.run(
        preflight.Limits(),
        measure_now=lambda: seen.pop(0),
        recover=lambda: pytest.fail("WSL answered on a retry: no restart"),
        wsl_alive=lambda: next(answers),
        sleep=lambda _: None,
        clock=lambda: 0.0,
    )
    assert outcome.ok
    assert outcome.actions == []


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
    """pytest 실행 대신 정해 둔 junit을 차례로 쓴다(None이면 junit 없음). `codes`는 호출마다 돌려줄
    종료 코드(기본 0, `nightly.PYTEST_TIMED_OUT` = 시간 초과), `on_call`은 호출마다 부른다(가짜
    시계를 움직인다)."""

    def __init__(
        self,
        *outputs: str | None,
        codes: tuple[int, ...] = (),
        on_call: Callable[[], None] = lambda: None,
    ) -> None:
        self.outputs = list(outputs)
        self.codes = list(codes)
        self.on_call = on_call
        self.calls: list[tuple[str, list[str]]] = []
        self.timeouts: list[float] = []

    def __call__(self, junit_path: Path, targets: list[str], timeout_s: float) -> int:
        self.calls.append((junit_path.name, targets))
        self.timeouts.append(timeout_s)
        self.on_call()
        text = self.outputs.pop(0)
        if text is not None:
            junit_path.write_text(text, encoding="utf-8")
        return self.codes.pop(0) if self.codes else 0


def run_once(tmp_path: Path, fake: FakePytest) -> tuple[dict[str, object], list[str]]:
    recovered: list[str] = []

    def recover() -> tuple[bool, list[str]]:
        recovered.append("x")
        return True, ["reclaimed host memory"]

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


# ---------------------------------------------------------------- 리뷰 r1(H1·M1·M2·L2·L7·L9·N5·M3)


def previous_infra_run(test_id: str = CASE_B) -> str:
    return json.dumps({**rows("20261109T180000Z", "fail", "infra"), "test_id": test_id}) + "\n"


def test_repeated_infra_needs_the_same_test_in_the_last_two_runs() -> None:
    def row(run: str, test_id: str, outcome: str = "fail", klass: str | None = "infra"):
        return {**rows(run, outcome, klass), "test_id": test_id}

    assert report.repeated_infra([row("r1", CASE_B), row("r2", CASE_B)]) == [CASE_B]
    assert report.repeated_infra([row("r1", CASE_A), row("r2", CASE_B)]) == []
    assert report.repeated_infra([row("r1", CASE_B), row("r2", CASE_B, "fail", "product")]) == []
    assert report.repeated_infra([row("r1", CASE_B), row("r2", CASE_B, "pass", None)]) == []
    assert report.repeated_infra([row("r2", CASE_B)]) == []
    # 더 오래된 실행은 보지 않는다
    assert (
        report.repeated_infra(
            [row("r1", CASE_B), row("r2", CASE_A, "pass", None), row("r3", CASE_B)]
        )
        == []
    )


def test_second_infra_failure_of_the_same_test_opens_an_infra_issue() -> None:
    """H1-3 안전망: 재시도 뒤에도 같은 시험이 infra로 2연속이면 사람에게 알린다(제품 멈춤·시간
    초과가 "error"로만 쌓이지 않게). 이슈는 product 이슈와 라벨이 다르다."""
    failing = summary_of("20261110T180000Z", (CASE_B, "failed", "infra"))
    gh = FakeGitHub(history=previous_infra_run())
    out = nightly.step_publish(failing, gh, TODAY)
    assert out["infra_issue"] == "created"
    assert out["issue"] is None
    assert ("issue", (report.INFRA_ISSUE_TITLE, "g2-nightly-infra")) in gh.calls
    assert report.INFRA_ISSUE_LABEL != report.ISSUE_LABEL
    assert ("status", (SHA, "error", "g2/nightly")) in gh.calls
    gh = FakeGitHub(history=previous_infra_run(), open_issue=9)
    assert nightly.step_publish(failing, gh, TODAY)["infra_issue"] == "commented #9"
    # 다른 시험이었으면 열지 않는다
    gh = FakeGitHub(history=previous_infra_run(CASE_A))
    assert nightly.step_publish(failing, gh, TODAY)["infra_issue"] is None


def test_infra_issue_body_names_the_tests_and_the_hang_possibility() -> None:
    body = nightly.infra_issue_body(summary_of("R2", (CASE_B, "failed", "infra")), [CASE_B])
    assert CASE_B in body
    assert "timeout" in body


def test_retry_is_skipped_when_the_environment_cannot_be_recovered(tmp_path: Path) -> None:
    """M2(d): 재시도 전 복구가 CI 때문에 WSL을 살리지 못하면 다시 돌리지 않고 infra로 남는다."""
    fake = FakePytest(junit(case(NAME_A), case(NAME_B, "error", "infra")))
    summary = nightly.step_run(
        "R1",
        SHA,
        tmp_path,
        run_pytest=fake,
        recover_env=lambda: (False, ["wsl not responding, 1 foundation-ci run(s) in progress"]),
        today=TODAY,
        quarantine=[],
    )
    assert len(fake.calls) == 1
    assert summary["verdict"] == "infra"
    retry = cast(dict[str, object], summary["retry"])
    assert retry["done"] is False
    assert "skipped" in retry


def test_recover_checks_ci_before_restarting_wsl() -> None:
    """M2(d): nightly.recover도 WSL 재시작 전에 foundation-ci를 본다."""
    restarted: list[str] = []
    removed: list[str] = []

    def restart() -> list[str]:
        restarted.append("x")
        return ["wsl --shutdown -> 0"]

    def remove() -> list[str]:
        removed.append("x")
        return []

    def recover(ci: tuple[int, bool]) -> tuple[bool, list[str]]:
        return nightly.recover(
            ci=lambda: ci,
            alive=lambda: False,
            restart=restart,
            sleep=lambda _: None,
            remove=remove,
            reclaim=lambda: None,
        )

    ready, actions = recover((1, True))
    assert (ready, restarted, removed) == (False, [], [])
    assert actions == ["wsl not responding, 1 foundation-ci run(s) in progress: no restart"]
    ready, actions = recover((0, True))
    assert ready
    assert restarted == ["x"]
    assert actions[0] == "wsl not responding: restart"
    assert actions[-1] == "reclaimed host memory"


def test_recover_keeps_a_responding_wsl() -> None:
    ready, actions = nightly.recover(
        ci=lambda: pytest.fail("CI is only asked when WSL does not answer"),
        alive=lambda: True,
        restart=lambda: pytest.fail("no restart"),
        sleep=lambda _: None,
        remove=lambda: ["pureiron-site-b"],
        reclaim=lambda: None,
    )
    assert ready
    assert actions == ["removed vm pureiron-site-b", "reclaimed host memory"]


def test_ci_status_failure_blocks_only_inside_github_actions() -> None:
    """M1: Actions 안에서 gh run list가 실패하면 조용히 지나가지 않는다(로컬은 메모만)."""
    unknown = measure(ci_known=False, notes=(preflight.CI_UNAVAILABLE_NOTE,))
    actions = preflight.Limits.from_env({"GITHUB_ACTIONS": "true"})
    assert actions.require_ci_status
    assert preflight.judge(unknown, actions) == [preflight.CI_UNKNOWN_PROBLEM]
    assert preflight.judge(unknown, preflight.Limits.from_env({})) == []
    outcome = preflight.judge_only(preflight.Limits(), lambda: unknown)
    assert outcome.as_dict()["warnings"] == [preflight.CI_UNAVAILABLE_NOTE]


def test_preflight_warnings_reach_the_summary_and_the_actions_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakePytest(junit(case(NAME_A), case(NAME_B)))
    summary = nightly.step_run(
        "R1",
        SHA,
        tmp_path,
        run_pytest=fake,
        today=TODAY,
        quarantine=[],
        warnings=[preflight.CI_UNAVAILABLE_NOTE],
    )
    assert summary["warnings"] == [preflight.CI_UNAVAILABLE_NOTE]
    nightly.announce([preflight.CI_UNAVAILABLE_NOTE], {"GITHUB_ACTIONS": "true"})
    assert f"::warning::{preflight.CI_UNAVAILABLE_NOTE}" in capsys.readouterr().out


def gh_runs(*outputs: tuple[int, str]):
    replies = list(outputs)

    def run(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        del timeout_s
        code, out = replies.pop(0)
        return subprocess.CompletedProcess[str](cmd, code, out, "")

    return run


def test_ci_running_counts_runs_and_notes_unreadable_output() -> None:
    """N5: gh 출력이 JSON이 아니어도 preflight 전체가 예외로 끝나지 않는다."""
    assert preflight.ci_running(gh_runs((0, '[{"databaseId": 1}]'), (0, "[]"))) == (1, None)
    count, note = preflight.ci_running(gh_runs((0, "<html>rate limited</html>")))
    assert count == 0
    assert note is not None
    assert "not JSON" in note
    assert preflight.ci_running(gh_runs((1, ""))) == (0, preflight.CI_UNAVAILABLE_NOTE)


def test_pytest_children_do_not_get_the_github_token() -> None:
    """L9: VM·WSL을 부르는 G2 pytest 프로세스 트리에는 토큰을 넘기지 않는다."""
    env = nightly.pytest_env(
        "R1", SHA, {"GH_TOKEN": "t1", "github_token": "t2", "PATH": "x", "PUREIRON_E2E_SWITCH": "s"}
    )
    assert env == {
        "PATH": "x",
        "PUREIRON_E2E_SWITCH": "s",
        "PUREIRON_G2_RUN": "R1",
        "PUREIRON_G2_SHA": SHA,
    }


@pytest.mark.parametrize("bad", ["..", "../results", "", "20261110T180000Z/..", "2026-11-10"])
def test_run_ids_that_could_leave_the_run_directory_are_refused(bad: str) -> None:
    """L7: `cleanup --run ..`이 E2E 루트를 지우지 않는다."""
    with pytest.raises(E2EError, match="run id"):
        nightly.step_cleanup(bad)
    with pytest.raises(SystemExit) as raised:
        nightly.main(["cleanup", "--run", bad]) if bad else nightly.main(["cleanup", "--run", "."])
    assert raised.value.code == 2


def test_runner_failure_is_reported_as_infra_and_exits_2(tmp_path: Path) -> None:
    """L2: 실행기 자신의 예상 밖 예외는 product 종료 코드(1)가 아니라 infra(2)로, status를
    남긴다."""
    gh = FakeGitHub()

    def broken(run_id: str, sha: str, today: date, out: Path, client: object) -> int:
        raise RuntimeError("hyperv remove failed during recovery")

    code = nightly.step_all(
        False,
        body=broken,
        github=lambda dry_run: gh,
        head=lambda: SHA,
        run_id="20261110T180000Z",
        today=TODAY,
        out_dir=lambda run_id: tmp_path / run_id,
    )
    assert code == 2
    out = tmp_path / "20261110T180000Z"
    summary = json.loads((out / "runner-summary.json").read_text("utf-8"))
    assert summary["verdict"] == "infra"
    assert [t["test_id"] for t in summary["tests"]] == ["runner"]
    assert "hyperv remove failed" in summary["runner_error"]
    assert ("status", (SHA, "error", "g2/nightly")) in gh.calls
    assert not (out / "summary.json").exists()
    published = json.loads((out / "publish.json").read_text("utf-8"))
    assert published["published_summary"] == "runner-summary.json"


def test_runner_failure_survives_a_failing_publish(tmp_path: Path) -> None:
    def no_github(dry_run: bool) -> FakeGitHub:
        raise RuntimeError("gh: network unreachable")

    def body(run_id: str, sha: str, today: date, out: Path, client: object) -> int:
        pytest.fail("body must not run without a GitHub client")

    code = nightly.step_all(
        True,
        body=body,
        github=no_github,
        head=lambda: SHA,
        run_id="20261110T180000Z",
        today=TODAY,
        out_dir=lambda run_id: tmp_path / run_id,
    )
    assert code == 2
    published = json.loads((tmp_path / "20261110T180000Z" / "publish.json").read_text("utf-8"))
    assert "not published" in str(published["error"])


def test_prune_results_keeps_14_days_and_only_touches_g2_runs(tmp_path: Path) -> None:
    """M3-3: 파괴적 정리의 배선 — 13일 된 결과는 남고 15일 된 것만 지운다. `g2-` 접두가 아닌
    디렉터리와 파일은 오래돼도 그대로 둔다."""
    now = datetime(2026, 11, 10, tzinfo=UTC).timestamp()
    ages = {"g2-new": 13, "g2-old": 15, "m2d-old": 30, "keep": 30}
    for name, days in ages.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "summary.json").write_text("{}", encoding="utf-8")
        os.utime(tmp_path / name, (now - days * 86400, now - days * 86400))
    stray = tmp_path / "g2-file.txt"
    stray.write_text("x", encoding="utf-8")
    os.utime(stray, (now - 30 * 86400, now - 30 * 86400))
    assert preflight.prune_results(tmp_path, now) == ["g2-old"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "g2-file.txt",
        "g2-new",
        "keep",
        "m2d-old",
    ]
    assert preflight.prune_results(tmp_path / "missing", now) == []


# ---------------------------------------------------------------- 리뷰 r2(H1·L4)

DAY1, DAY2 = "20261109T180000Z", "20261110T180000Z"


def timed_out_run(tmp_path: Path, run_id: str) -> tuple[dict[str, object], FakePytest, list[str]]:
    """pytest가 시간 제한에 죽어 junit을 남기지 못한 날(제품이 멈춤)."""
    fake = FakePytest(None, codes=(nightly.PYTEST_TIMED_OUT,))
    recovered: list[str] = []

    def recover() -> tuple[bool, list[str]]:
        recovered.append("x")
        return True, []

    summary = nightly.step_run(
        run_id,
        SHA,
        tmp_path / run_id,
        run_pytest=fake,
        recover_env=recover,
        today=TODAY,
        quarantine=[],
    )
    return summary, fake, recovered


def test_pytest_timeout_without_junit_is_one_infra_line_and_is_not_retried(
    tmp_path: Path,
) -> None:
    """R2-H1: junit이 없어도 이력에 남을 시험 id `pytest`(infra) 한 줄이 생긴다. 시간 초과였으면
    다시 돌리지 않는다(멈춘 제품은 재시도로 낫지 않고 120분 예산에도 들지 않는다)."""
    summary, fake, recovered = timed_out_run(tmp_path, DAY1)
    assert len(fake.calls) == 1
    assert recovered == []
    assert summary["verdict"] == "infra"
    assert [(t["test_id"], t["failure_class"]) for t in report.test_rows(summary)] == [
        ("pytest", "infra")
    ]
    assert summary["pytest_rc"] == {"first": nightly.PYTEST_TIMED_OUT}
    retry = cast(dict[str, object], summary["retry"])
    assert retry["done"] is False
    assert "timed out" in str(retry["skipped"])
    assert len(report.history_lines(summary)) == 1


def test_two_days_of_pytest_timeouts_open_an_infra_issue(tmp_path: Path) -> None:
    """R2-H1: junit 없는 실행이 이틀 이어지면 `g2-nightly-infra` 이슈가 열린다(이전에는 이력이
    0줄이라 영영 열리지 않았다)."""
    day1, _, _ = timed_out_run(tmp_path, DAY1)
    gh = FakeGitHub()
    first = nightly.step_publish(day1, gh, TODAY)
    assert (first["history_lines"], first["infra_issue"]) == (1, None)
    ((_, _, appended),) = gh.appends
    day2, _, _ = timed_out_run(tmp_path, DAY2)
    gh = FakeGitHub(history=appended)
    second = nightly.step_publish(day2, gh, TODAY)
    assert second["infra_issue"] == "created"
    assert ("issue", (report.INFRA_ISSUE_TITLE, "g2-nightly-infra")) in gh.calls
    assert ("status", (SHA, "error", "g2/nightly")) in gh.calls
    body = nightly.infra_issue_body(day2, ["pytest"])
    assert '"first": -1' in body


def test_crash_without_junit_reruns_everything_and_still_leaves_a_line(tmp_path: Path) -> None:
    """크래시(시간 초과가 아닌 0 아닌 종료, junit 없음)는 전체를 1회 다시 돌리고, 그래도 결과가
    없으면 `pytest` 한 줄이다."""
    fake = FakePytest(None, None, codes=(3, 3))
    summary, recovered = run_once(tmp_path, fake)
    assert fake.calls == [
        ("junit.xml", ["tests/e2e/gates"]),
        ("junit-retry.xml", ["tests/e2e/gates"]),
    ]
    assert recovered == ["x"]
    assert [t["test_id"] for t in report.test_rows(summary)] == ["pytest"]
    assert summary["pytest_rc"] == {"first": 3, "retry": 3}
    assert cast(dict[str, object], summary["retry"])["done"] is True


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_deadline_shortens_pytest_and_skips_a_retry_that_cannot_finish(tmp_path: Path) -> None:
    """R2-H1 시간 예산: pytest 시간 제한은 남은 시간으로 줄고, 재시도할 시간이
    `RETRY_MIN_S`보다 적으면 복구도 재시도도 하지 않는다."""
    clock = Clock(20 * 60)
    deadline = 80 * 60

    def first_run_takes_55_minutes() -> None:
        clock.now += 55 * 60

    fake = FakePytest(
        junit(case(NAME_A), case(NAME_B, "error", "infra")), on_call=first_run_takes_55_minutes
    )
    summary = nightly.step_run(
        "R1",
        SHA,
        tmp_path,
        run_pytest=fake,
        recover_env=lambda: pytest.fail("no time left: must not recover"),
        today=TODAY,
        quarantine=[],
        deadline=deadline,
        clock=clock,
    )
    assert fake.timeouts == [60 * 60]  # min(75분, 80 - 20분)
    assert summary["verdict"] == "infra"
    assert "left before the workflow timeout" in str(
        cast(dict[str, object], summary["retry"])["skipped"]
    )


def test_retry_timeout_is_capped_by_the_time_left(tmp_path: Path) -> None:
    clock = Clock(0.0)

    def tick() -> None:
        clock.now += 70 * 60

    fake = FakePytest(
        junit(case(NAME_A), case(NAME_B, "error", "infra")), junit(case(NAME_B)), on_call=tick
    )
    summary = nightly.step_run(
        "R1",
        SHA,
        tmp_path,
        run_pytest=fake,
        recover_env=lambda: (True, []),
        today=TODAY,
        quarantine=[],
        deadline=nightly.RUN_BUDGET_S,
        clock=clock,
    )
    assert fake.timeouts == [nightly.PYTEST_TIMEOUT_S, nightly.RUN_BUDGET_S - 70 * 60]
    assert summary["verdict"] == "passed"


def test_time_budget_fits_the_workflow_timeout() -> None:
    """R2-H1: workflow `timeout-minutes`를 넘기면 Actions가 job을 죽여 status·이력이 남지 않는다 —
    준비(checkout·uv sync) + run 예산(preflight·첫 실행·복구·재시도, 실행 중 남은 시간으로 자른다)
    + 뒷정리(collect 최악·publish·cleanup)가 120분보다 작아야 한다."""
    workflow = (nightly.paths.REPO / ".github" / "workflows" / "nightly.yml").read_text("utf-8")
    assert f"timeout-minutes: {nightly.WORKFLOW_TIMEOUT_S // 60}\n" in workflow
    total = nightly.SETUP_RESERVE_S + nightly.RUN_BUDGET_S + nightly.TAIL_RESERVE_S
    assert total < nightly.WORKFLOW_TIMEOUT_S
    collect_worst = len(nightly.G2_SITES) * len(nightly.REMOTE_LOGS) * nightly.COLLECT_TIMEOUT_S
    assert collect_worst + nightly.PUBLISH_RESERVE_S <= nightly.TAIL_RESERVE_S
    assert nightly.RETRY_MIN_S < nightly.RETRY_TIMEOUT_S < nightly.PYTEST_TIMEOUT_S
    assert nightly.PYTEST_TIMEOUT_S < nightly.RUN_BUDGET_S


def test_publish_does_not_append_the_same_run_twice() -> None:
    """R2-L4: publish를 다시 시도해도(runner 보호) 이력 줄은 한 번만 들어간다."""
    failing = summary_of(DAY2, (CASE_B, "failed", "product"))
    already = "".join(line + "\n" for line in report.history_lines(failing))
    gh = FakeGitHub(history=already)
    out = nightly.step_publish(failing, gh, TODAY)
    assert out["history_lines"] == 0
    assert gh.appends == []
    assert ("status", (SHA, "failure", "g2/nightly")) in gh.calls


def test_runner_failure_keeps_the_real_summary_and_publishes_it(tmp_path: Path) -> None:
    """R2-L4: run 뒤(collect·publish·cleanup)에서 멈춰도 그날의 실제 결과(product 실패)를
    덮어쓰지 않고 그것으로 publish를 다시 시도한다 — product 줄이 이력에서 빠지지 않게."""
    gh = FakeGitHub()
    real = summary_of(DAY2, (CASE_B, "failed", "product"))

    def body(run_id: str, sha: str, today: date, out: Path, client: object) -> int:
        nightly.write_json(out / "summary.json", real)
        raise RuntimeError("gh: status call failed")

    code = nightly.step_all(
        False,
        body=body,
        github=lambda dry_run: gh,
        head=lambda: SHA,
        run_id=DAY2,
        today=TODAY,
        out_dir=lambda run_id: tmp_path / run_id,
    )
    assert code == 2
    out = tmp_path / DAY2
    assert json.loads((out / "summary.json").read_text("utf-8")) == real
    assert json.loads((out / "runner-summary.json").read_text("utf-8"))["tests"][0]["test_id"] == (
        "runner"
    )
    ((_, _, appended),) = gh.appends
    assert [json.loads(line)["class"] for line in appended.splitlines()] == ["product"]
    assert ("status", (SHA, "failure", "g2/nightly")) in gh.calls
    published = json.loads((out / "publish.json").read_text("utf-8"))
    assert published["published_summary"] == "summary.json"
    assert "status call failed" in str(published["runner_error"])
