"""G2 nightly cron(M6-2b) — 늦게 시작한 cron 실행 건너뛰기(G7), Hyper-V 호스트 점검 실패 =
infra(A3), 시작하지 못한 실행(preflight에서 멈춤)의 집계(L8·G2)."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from pathlib import Path
from typing import NoReturn

import nightly
import pytest
import run
from harness import failures, flake, golden, hyperv, preflight, publish, report, sites, suites
from harness.context import Context, InfraError
from harness.github import DryRun
from harness.release_pair import DEFAULT

SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
TODAY = date(2026, 11, 10)
# ---------------------------------------------------------------- 시작(★H13 — cron·가드 없음)


@pytest.mark.parametrize(
    ("environ", "suite"),
    [
        ({}, "g2"),  # 로컬 실행
        ({"GITHUB_EVENT_NAME": "workflow_dispatch", "PUREIRON_G2_SUITE": "g2"}, "g2"),
        ({"GITHUB_EVENT_NAME": "workflow_dispatch", "PUREIRON_G2_SUITE": "weekly"}, "weekly"),
        ({"GITHUB_EVENT_NAME": "schedule"}, "g2"),  # 예전 cron 이벤트도 가드 없이 돈다
    ],
)
def test_every_start_reaches_the_body_with_its_suite(
    tmp_path: Path, environ: dict[str, str], suite: str
) -> None:
    called: list[tuple[str, str]] = []

    def body(
        run_id: str, sha: str, today: date, out: Path, gh: object, *, suite: suites.Suite
    ) -> int:
        called.append((run_id, suite.name))
        return 0

    code = nightly.step_all(
        False,
        body=body,
        github=lambda dry_run: DryRun(),
        head=lambda: SHA,
        run_id="20261111T090000Z",
        today=TODAY,
        out_dir=lambda run_id: tmp_path / run_id,
        environ=environ,
    )
    assert (code, called) == (0, [("20261111T090000Z", suite)])


def test_an_unknown_suite_is_a_runner_failure_recorded_under_that_name(tmp_path: Path) -> None:
    def body(*args: object, suite: object) -> NoReturn:
        pytest.fail(f"an unknown suite must not reach the body {args!r} {suite!r}")

    code = nightly.step_all(
        True,
        body=body,
        github=lambda dry_run: DryRun(),
        head=lambda: SHA,
        run_id="20261111T090000Z",
        today=TODAY,
        out_dir=lambda run_id: tmp_path / run_id,
        environ={"PUREIRON_G2_SUITE": "monthly"},
    )
    assert code == 2
    runner = json.loads((tmp_path / "20261111T090000Z" / "runner-summary.json").read_text("utf-8"))
    assert runner["suite"] == "monthly"
    assert "PUREIRON_G2_SUITE" in runner["runner_error"]


def refuse(*args: object) -> NoReturn:
    pytest.fail(f"must not reach this call {args!r}")


# ---------------------------------------------------------------- Hyper-V 호스트 점검(A3)


@pytest.mark.parametrize(("available", "switch"), [(False, True), (True, False)])
def test_host_check_failure_is_infra(
    monkeypatch: pytest.MonkeyPatch, available: bool, switch: bool
) -> None:
    """Hyper-V 권한 없음·vSwitch 없음은 PC 설정 문제다 — product로 가면 제품 실패로 기록되고
    이틀이면 product 이슈가 열린다."""
    monkeypatch.setattr(hyperv, "available", lambda: available)

    def switch_exists(name: str) -> bool:
        return switch and name == sites.SWITCH

    monkeypatch.setattr(hyperv, "switch_exists", switch_exists)
    with pytest.raises(InfraError, match=r"setup-host\.ps1") as raised:
        sites.check_host()
    assert failures.classify(raised.value) == "infra"


def test_run_py_vm_step_still_stops_at_a_host_check_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run.py`의 흐름은 그대로다 — `step_vm`은 이 예외를 잡지 않고 멈추며(golden·VM으로 가지
    않는다) 결과 항목도 남기지 않는다. 바뀐 것은 예외 종류(`E2EError` → `InfraError`, 둘 다
    `RuntimeError`)뿐이다."""
    monkeypatch.setattr(hyperv, "available", lambda: False)
    monkeypatch.setattr(golden, "build_golden", refuse)
    site_a = dataclasses.replace(run.SITE_A, state_file=tmp_path / "state.json")
    ctx = Context(run_id="t", artifacts=str(tmp_path), site=site_a, pair=DEFAULT)
    with pytest.raises(RuntimeError, match="Hyper-V not available") as raised:
        run.step_vm(ctx)
    assert isinstance(raised.value, InfraError)
    assert ctx.results == []


# ---------------------------------------------------------------- 시작하지 못한 실행(A4·A5)

CASE = "test_g2_install.py::test_g2_1_install_and_100_tags_good[site-b]"
SHA_B = "b" * 40


def line(
    run: str, test_id: str, outcome: str = "fail", klass: str | None = "infra", sha: str = SHA
) -> dict[str, object]:
    """history.jsonl 한 줄(`report.history_lines` 형식)."""
    return {
        "date": "2026-11-09",
        "run": run,
        "product_sha": sha,
        "test_id": test_id,
        "outcome": outcome,
        "class": klass if outcome == "fail" else None,
        "seconds": 1.0,
    }


def stopped(run: str, sha: str = SHA) -> dict[str, object]:
    """preflight에서 멈춘 실행의 줄."""
    return line(run, report.PREFLIGHT_TEST, sha=sha)


def product(run: str, sha: str = SHA) -> dict[str, object]:
    return line(run, CASE, klass="product", sha=sha)


def test_only_a_run_with_nothing_but_the_preflight_line_did_not_start() -> None:
    assert report.not_started([stopped("r1")])
    assert not report.not_started([line("r1", "runner")])  # 실행기가 시작한 뒤 멈춤
    assert not report.not_started([line("r1", "pytest")])  # pytest가 junit 없이 죽음
    assert not report.not_started([line("r1", CASE)])
    assert not report.not_started([stopped("r1"), line("r1", CASE, "pass")])
    assert not report.not_started([])


def test_flake_rate_leaves_runs_that_did_not_start_out_of_both_counts() -> None:
    """L8: 커밋이 없는 밤의 "어제 통과·오늘 preflight 멈춤"은 흔들림이 아니다. 분자에서만 빼면
    2/4 = 0.5, 분모에서만 빼면 3/3 = 1.0이 된다."""
    history = [
        line("r1", CASE, "pass"),
        product("r2"),
        stopped("r3"),
        line("r4", CASE, "pass", sha=SHA_B),
    ]
    assert flake.flake_rate(history, TODAY) == round(2 / 3, 3)
    assert flake.flake_rate([line("r1", CASE, "pass"), stopped("r2")], TODAY) == 0.0
    # 시작한 뒤 깨진 실행(runner·pytest)은 그대로 실패로 센다
    assert flake.flake_rate([line("r1", CASE, "pass"), line("r2", "runner")], TODAY) == 1.0


def test_product_issue_skips_a_night_that_did_not_start() -> None:
    """★G2: product → preflight 멈춤 → product는 2연속이다."""
    assert report.needs_issue([product("r1"), stopped("r2"), product("r3")])
    assert report.needs_issue([product("r1"), stopped("r2"), stopped("r3"), product("r4")])
    # 시작한 뒤 깨진 실행은 건너뛰지 않는다
    assert not report.needs_issue([product("r1"), line("r2", "runner"), product("r3")])
    assert not report.needs_issue([product("r1"), line("r2", "pytest"), product("r3")])
    # preflight에서 멈춘 밤 자신은 이슈를 열지 않는다
    assert not report.needs_issue([product("r1"), product("r2"), stopped("r3")])


def test_repeated_infra_never_counts_nights_that_did_not_start() -> None:
    """★G2: preflight에서 멈춘 밤은 infra 이슈 사유가 아니고, 최근 두 실행을 고를 때 건너뛴다.
    시험 id `runner`·`pytest`는 지금처럼 이틀이면 이슈다."""
    assert report.repeated_infra([stopped("r1"), stopped("r2")]) == []
    assert report.repeated_infra([line("r1", "pytest"), stopped("r2"), line("r3", "pytest")]) == [
        "pytest"
    ]
    assert report.repeated_infra([line("r1", "runner"), line("r2", "runner")]) == ["runner"]
    assert report.repeated_infra([line("r1", CASE), line("r2", CASE), stopped("r3")]) == []


class History:
    """`step_publish`가 읽는 이력과 쓰기 호출만 흉내 낸다."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.text = "".join(json.dumps(r) + "\n" for r in rows)
        self.calls: list[str] = []

    def read_file(self, branch: str, path: str) -> str | None:
        return self.text or None

    def append_file(self, branch: str, path: str, text: str, message: str) -> None:
        self.calls.append("append")

    def set_status(self, sha: str, state: str, context: str, description: str) -> None:
        self.calls.append(f"status {state}")

    def open_issue(self, label: str) -> int | None:
        return 7

    def create_issue(self, title: str, body: str, label: str) -> None:
        self.calls.append(f"issue {label}")

    def comment_issue(self, number: int, body: str) -> None:
        self.calls.append(f"comment #{number}")

    def temp_branches(self, prefix: str) -> list[tuple[str, datetime]]:
        return []

    def delete_branch(self, name: str) -> None:
        self.calls.append(f"delete {name}")


@pytest.mark.parametrize(
    "before",
    [
        [stopped("20261109T180000Z")],  # 이틀 연속 preflight 멈춤
        [line("20261108T180000Z", CASE), line("20261109T180000Z", CASE)],  # infra 이슈가 열린 뒤
    ],
)
def test_a_night_stopped_at_preflight_leaves_a_line_and_an_error_status_but_no_issue(
    before: list[dict[str, object]],
) -> None:
    gh = History(before)
    measure = preflight.Measure(
        mem_gb=2.0, disk_gb=100.0, wsl_ok=True, docker_ok=True, ci_running=0
    )
    outcome = preflight.Outcome(measure, preflight.Limits(), ["memory 2.0 GB < 8.0 GB"], [])
    summary = nightly.preflight_summary("20261110T180000Z", SHA, TODAY, outcome)
    out = publish.step_publish(summary, gh, TODAY)
    assert gh.calls == ["append", "status error"]
    assert (out["issue"], out["infra_issue"]) == (None, None)
