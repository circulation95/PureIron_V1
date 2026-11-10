"""G2 nightly cron(M6-2b) — 늦게 시작한 cron 실행 건너뛰기(G7), Hyper-V 호스트 점검 실패 =
infra(A3), 시작하지 못한 실행(preflight에서 멈춤)의 집계(L8·G2)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NoReturn

import nightly
import pytest
import run
from harness import failures, golden, hyperv, schedule, sites
from harness.context import Context, InfraError
from harness.github import DryRun
from harness.release_pair import DEFAULT

SHA = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
TODAY = date(2026, 11, 10)
CRON_AT = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)
"""`schedule.CRON`("0 18 * * *")의 한 시각."""


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------- 늦은 시작(G7)


@pytest.mark.parametrize(
    ("now", "late"),
    [
        (at(10, 18, 0), False),  # 제시간
        (at(10, 18, 7), False),  # GitHub cron은 몇 분씩 늦는다
        (at(10, 20, 0, 0), False),  # 정확히 2시간 — 아직 돈다
        (at(10, 20, 0, 1), True),  # 2시간 넘음
        (at(10, 23, 30), True),
        (at(11, 0, 30), True),  # 자정을 넘김(6시간 30분)
        (at(11, 9, 0), True),  # 아침에 PC를 켬
        (at(11, 17, 50), True),  # 다음 날 17:50 = 23시간 50분 지연
    ],
)
def test_scheduled_run_is_late_after_two_hours_past_the_cron_time(
    now: datetime, late: bool
) -> None:
    assert schedule.late_start("schedule", schedule.CRON, now) is late


@pytest.mark.parametrize("event", ["workflow_dispatch", "", "push"])
def test_manual_and_local_runs_are_never_late(event: str) -> None:
    """수동 실행(workflow_dispatch)·로컬 실행(GITHUB_EVENT_NAME 없음)은 언제 시작해도 돈다."""
    assert not schedule.late_start(event, schedule.CRON, at(11, 9, 0))


def test_delay_counts_from_the_latest_cron_time_before_now() -> None:
    assert schedule.delay(schedule.CRON, at(10, 18, 0)) == at(10, 18) - CRON_AT
    assert schedule.delay(schedule.CRON, at(11, 17, 50)) == at(11, 17, 50) - CRON_AT
    assert schedule.delay("30 1 * * *", at(11, 1, 0)) == at(11, 1, 0) - at(10, 1, 30)


@pytest.mark.parametrize("cron", ["*/30 * * * *", "0 18 * * 1-5", "0 24 * * *", "60 1 * * *", ""])
def test_unreadable_cron_is_never_late(cron: str) -> None:
    """해석하지 못하는 cron이면 돈다(건너뛰면 이력·status 없이 밤마다 조용히 사라진다)."""
    assert schedule.daily_time(cron) is None
    assert not schedule.late_start("schedule", cron, at(11, 9, 0))


def test_cron_constant_is_the_one_in_nightly_yml() -> None:
    """`schedule.CRON`이 실제로 해석된다(yml과 같은 값인지는 `test_frame_g2`가 본다)."""
    assert schedule.daily_time(schedule.CRON) == (18, 0)


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
    monkeypatch.setattr(hyperv, "switch_exists", lambda name: switch)
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
