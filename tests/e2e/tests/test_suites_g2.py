"""G2 묶음(M6-3b J4, ★H13·H11) — suite → pytest 표지·시간 예산, summary·이력의 suite, Windows 작업
스케줄러 등록 스크립트(03:17·일요일 weekly·저장소·ref, 로그온 세션·놓친 예약 실행 안 함·토큰을
로그에
쓰지 않음)를 고정한다. 등록 스크립트는 실행하지 않고 내용만 본다(문법은 PowerShell 파서로 따로)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import nightly
import pytest
from harness import paths, report, suites
from harness.context import E2EError

SCRIPT = paths.REPO / "tests" / "e2e" / "infra" / "register-nightly.ps1"


def test_weekday_suite_leaves_weekly_cases_out_and_weekly_runs_everything() -> None:
    """변이: weekly가 `not weekly`로 돌면 crash 16건이 한 번도 돌지 않는다."""
    assert nightly.pytest_command(suites.SUITES["g2"])[-2:] == ["-m", "g2 and not weekly"]
    assert nightly.pytest_command(suites.SUITES["weekly"])[-2:] == ["-m", "g2"]


def test_suite_budgets() -> None:
    g2, weekly = suites.SUITES["g2"], suites.SUITES["weekly"]
    assert (g2.workflow_s, g2.run_budget_s, g2.pytest_timeout_s) == (120 * 60, 90 * 60, 75 * 60)
    assert (weekly.workflow_s, weekly.run_budget_s, weekly.pytest_timeout_s) == (
        270 * 60,
        240 * 60,
        200 * 60,
    )
    assert (g2.retry_timeout_s, weekly.retry_timeout_s) == (30 * 60, 40 * 60)


@pytest.mark.parametrize(
    ("environ", "name"),
    [({}, "g2"), ({"PUREIRON_G2_SUITE": ""}, "g2"), ({"PUREIRON_G2_SUITE": "weekly"}, "weekly")],
)
def test_suite_comes_from_the_environment(environ: dict[str, str], name: str) -> None:
    assert suites.from_env(environ).name == name


def test_an_unknown_suite_is_refused() -> None:
    with pytest.raises(E2EError, match="PUREIRON_G2_SUITE"):
        suites.from_env({"PUREIRON_G2_SUITE": "nightly"})


def test_step_run_uses_the_suite_budget_and_records_the_suite(tmp_path: Path) -> None:
    timeouts: list[float] = []

    def run_pytest(junit: Path, targets: list[str], timeout_s: float) -> int:
        timeouts.append(timeout_s)
        junit.write_text(
            '<testsuites><testsuite name="pytest"><testcase file="test_g2_install.py" '
            'name="test_g2_1_install_and_100_tags_good[site-a]" time="1"/></testsuite>'
            "</testsuites>",
            encoding="utf-8",
        )
        return 0

    summary = nightly.step_run(
        "20261115T181700Z",
        "a" * 40,
        tmp_path,
        run_pytest=run_pytest,
        recover_env=lambda: (True, []),
        today=date(2026, 11, 15),
        quarantine=[],
        suite=suites.SUITES["weekly"],
    )
    assert timeouts == [200 * 60]
    assert summary["suite"] == "weekly"
    assert all('"suite": "weekly"' in line for line in report.history_lines(summary))


# ---------------------------------------------------------------- 작업 스케줄러 등록 스크립트


def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_registration_runs_daily_at_0317_and_picks_weekly_on_sunday() -> None:
    text = script()
    assert "[string]$At = '03:17'" in text
    assert "New-ScheduledTaskTrigger -Daily -At $At" in text
    assert "$WeeklyDay = [System.DayOfWeek]::Sunday" in text
    assert re.search(r"if \(\$Day\.DayOfWeek -eq \$WeeklyDay\) \{ return 'weekly' \}", text)
    assert "return 'g2'" in text


def test_registration_dispatches_the_nightly_workflow_on_master_of_this_repo() -> None:
    text = script()
    assert "[string]$Repo = 'circulation95/PureIron_V1'" in text
    assert "[string]$GhUser = 'circulation95'" in text
    assert "[string]$Ref = 'master'" in text
    assert "$Workflow = 'nightly.yml'" in text
    assert (
        "@('workflow', 'run', $Workflow, '--ref', $Ref, '-R', $Repo, '-f', \"ref=$Ref\", "
        "'-f', \"suite=$Suite\")"
    ) in text


def test_registration_runs_only_in_the_logon_session_and_skips_missed_runs() -> None:
    text = script()
    assert "-LogonType Interactive" in text
    assert "$settings.StartWhenAvailable = $false" in text
    assert "-MultipleInstances IgnoreNew" in text
    assert "Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false" in text
    assert "if ($WhatIf) {" in text  # 요일 → suite를 gh 없이 확인하는 모드


def test_registration_never_writes_the_token() -> None:
    text = script()
    log_lines = [line for line in text.splitlines() if "Write-DispatchLog" in line]
    assert log_lines
    assert not any("GH_TOKEN" in line or "$token" in line for line in log_lines)
    assert "Remove-Item Env:\\GH_TOKEN" in text
    assert not re.search(r"(Set-Content|Out-File)", text)


def test_nightly_workflow_has_no_schedule() -> None:
    """변이: schedule을 되살리면 GitHub cron(3.5~7.5시간 지연)과 작업 스케줄러가 같이 뜬다."""
    text = (paths.REPO / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    assert "schedule:" not in text
    assert "cron:" not in text
