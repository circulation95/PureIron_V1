"""G2 nightly cron(M6-2b) — 늦게 시작한 cron 실행 건너뛰기(G7), Hyper-V 호스트 점검 실패 =
infra(A3), 시작하지 못한 실행(preflight에서 멈춤)의 집계(L8·G2)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import nightly
import pytest
import run
from harness import failures, flake, golden, hyperv, preflight, publish, report, schedule, sites
from harness.context import Context, InfraError
from harness.github import DryRun
from harness.release_pair import DEFAULT

SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
TODAY = date(2026, 11, 10)
CRON_AT = datetime(2026, 11, 10, 18, 17, tzinfo=UTC)
"""`schedule.CRON`("17 18 * * *")의 한 시각."""


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------- 늦은 시작(G7)


@pytest.mark.parametrize(
    ("now", "late"),
    [
        (at(10, 18, 17), False),  # 제시간
        (at(10, 18, 24), False),  # GitHub cron은 몇 분씩 늦는다
        (at(10, 20, 17, 0), False),  # 정확히 2시간 — 아직 돈다
        (at(10, 20, 17, 1), True),  # 2시간 넘음
        (at(10, 23, 30), True),
        (at(11, 0, 30), True),  # 자정을 넘김(6시간 13분)
        (at(11, 9, 0), True),  # 아침에 PC를 켬
        (at(11, 18, 0), True),  # 다음 날 18:00 = 23시간 43분 지연
    ],
)
def test_scheduled_run_is_late_after_two_hours_past_the_cron_time(
    now: datetime, late: bool
) -> None:
    assert schedule.late_start("schedule", schedule.CRON, now) is late


def test_cron_is_off_the_hour() -> None:
    """M1: GitHub는 매시 정각의 schedule 이벤트를 늦추거나 버릴 수 있다 — 2시간 넘게 늦으면 그 밤은
    늦은 시작으로 조용히 건너뛰어진다."""
    _, minute = schedule.daily_time(schedule.CRON) or (0, 0)
    assert minute != 0


@pytest.mark.parametrize(
    ("now", "late"),
    [
        (at(10, 18, 2, 0), False),  # 직전 15분 정각 — 제시간
        (at(10, 18, 16), False),  # 직전 1분 — 제시간
        (at(10, 18, 1, 59), True),  # 직전 15분 1초 — 어제 cron부터 약 24시간 지연
    ],
)
def test_a_start_just_before_the_cron_time_is_on_time(now: datetime, late: bool) -> None:
    """L1: runner PC 시계가 GitHub보다 늦으면 정시 실행이 cron 시각 직전으로 읽힌다(여유 15분)."""
    assert schedule.late_start("schedule", schedule.CRON, now) is late


@pytest.mark.parametrize("event", ["workflow_dispatch", "", "push"])
def test_manual_and_local_runs_are_never_late(event: str) -> None:
    """수동 실행(workflow_dispatch)·로컬 실행(GITHUB_EVENT_NAME 없음)은 언제 시작해도 돈다."""
    assert not schedule.late_start(event, schedule.CRON, at(11, 9, 0))


def test_delay_counts_from_the_latest_cron_time_before_now() -> None:
    assert schedule.delay(schedule.CRON, at(10, 18, 17)) == timedelta(0)
    assert schedule.delay(schedule.CRON, at(11, 18, 0)) == at(11, 18, 0) - CRON_AT
    assert schedule.delay("30 1 * * *", at(11, 1, 0)) == at(11, 1, 0) - at(10, 1, 30)
    assert schedule.delay(schedule.CRON, at(10, 18, 10)) == timedelta(0)  # 직전 7분


def test_delay_just_past_the_grace_counts_from_yesterday() -> None:
    assert schedule.delay(schedule.CRON, at(10, 18, 1, 59)) == at(10, 18, 1, 59) - at(9, 18, 17)


@pytest.mark.parametrize("cron", ["*/30 * * * *", "0 18 * * 1-5", "0 24 * * *", "60 1 * * *", ""])
def test_unreadable_cron_is_never_late(cron: str) -> None:
    """해석하지 못하는 cron이면 돈다(건너뛰면 이력·status 없이 밤마다 조용히 사라진다)."""
    assert schedule.daily_time(cron) is None
    assert not schedule.late_start("schedule", cron, at(11, 9, 0))


def test_cron_constant_is_the_one_in_nightly_yml() -> None:
    """`schedule.CRON`이 실제로 해석된다(yml과 같은 값인지는 `test_frame_g2`가 본다)."""
    assert schedule.daily_time(schedule.CRON) == (18, 17)


def refuse(*args: object) -> NoReturn:
    pytest.fail(f"a late scheduled run must not reach this call {args!r}")


def test_late_scheduled_run_does_nothing_and_exits_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """preflight(본체)·GitHub·HEAD 조회·결과 디렉터리 어느 것도 부르지 않는다."""
    code = nightly.step_all(
        False,
        body=refuse,
        github=refuse,
        head=refuse,
        run_id="20261111T090000Z",
        today=TODAY,
        out_dir=refuse,
        environ={"GITHUB_EVENT_NAME": "schedule", "GITHUB_ACTIONS": "true"},
        now=lambda: at(11, 9, 0),
    )
    assert code == 0
    assert list(tmp_path.iterdir()) == []
    out = capsys.readouterr().out
    assert out.startswith("::notice::scheduled run started at 2026-11-11 09:00 UTC")
    assert "skipped" in out


@pytest.mark.parametrize(
    ("event", "now"),
    [
        ("schedule", at(10, 19, 30)),  # 제시간 cron
        ("workflow_dispatch", at(11, 9, 0)),  # 낮의 수동 실행
        ("", at(11, 9, 0)),  # 로컬 실행
    ],
)
def test_on_time_cron_and_manual_runs_reach_the_body(
    tmp_path: Path, event: str, now: datetime
) -> None:
    called: list[str] = []

    def body(run_id: str, sha: str, today: date, out: Path, gh: object) -> int:
        called.append(run_id)
        return 0

    code = nightly.step_all(
        False,
        body=body,
        github=lambda dry_run: DryRun(),
        head=lambda: SHA,
        run_id="20261111T090000Z",
        today=TODAY,
        out_dir=lambda run_id: tmp_path / run_id,
        environ={"GITHUB_EVENT_NAME": event} if event else {},
        now=lambda: now,
    )
    assert (code, called) == (0, ["20261111T090000Z"])


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
