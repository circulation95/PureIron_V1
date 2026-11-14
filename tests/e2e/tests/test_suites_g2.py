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
    assert (g2.workflow_s, g2.run_budget_s, g2.pytest_timeout_s) == (
        190 * 60,
        160 * 60,
        105 * 60,
    )
    assert (weekly.workflow_s, weekly.run_budget_s, weekly.pytest_timeout_s) == (
        270 * 60,
        240 * 60,
        200 * 60,
    )
    assert (g2.retry_timeout_s, weekly.retry_timeout_s) == (45 * 60, 40 * 60)


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
    assert "New-ScheduledTaskTrigger -Daily -At $Time" in text
    assert "Register-One $TaskName $At '-Dispatch' $copy $bin" in text
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
    assert "foreach ($name in @($TaskName, $CiTaskName)) {" in text
    assert "Unregister-ScheduledTask -TaskName $name -Confirm:$false" in text
    assert "if ($WhatIf) {" in text  # 요일 → suite를 gh 없이 확인하는 모드


def test_registration_never_writes_the_token() -> None:
    text = script()
    log_lines = [line for line in text.splitlines() if "Write-DispatchLog" in line]
    assert log_lines
    assert not any("GH_TOKEN" in line or "$token" in line for line in log_lines)
    assert "Remove-Item Env:\\GH_TOKEN" in text
    assert not re.search(r"(Set-Content|Out-File)", text)


def test_registration_calls_gh_only_outside_the_stop_context() -> None:
    """M6-3b 리뷰 M1: PS 5.1에서 `$ErrorActionPreference = 'Stop'`이면 네이티브 stderr가
    `2>&1`·`2>$null` 어느 쪽이든 예외가 되어 실패한 밤의 로그 줄이 남지 않는다. gh는 `Invoke-Gh`
    한 곳에서만, 그 함수의 지역 Continue 뒤에 부르고, 두 호출(auth token·workflow run)은 그 함수를
    거친다. 파일은 UTF-8 BOM이다(BOM이 없으면 PS 5.1이 CP949로 읽는다)."""
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    text = script()
    body = text.split("function Invoke-Gh(", 1)[1].split("\nfunction ", 1)[0]
    native = [line for line in text.splitlines() if re.search(r"&\s*(\$Gh|gh)\b", line)]
    assert native == ["        & $Gh @GhArgs 2>&1 | ForEach-Object {"]
    assert native[0] in body
    assert body.index("$ErrorActionPreference = 'Continue'") < body.index(native[0])
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("2>$null" in line for line in code)
    assert "$auth = Invoke-Gh @('auth', 'token', '-u', $GhUser)" in text
    assert "$run = Invoke-Gh $ghArgs" in text
    assert "[string]$Gh = 'gh'" in text


def test_foundation_ci_night_run_is_a_second_task_at_0147() -> None:
    """M6-4a P9: foundation-ci 밤 실행은 같은 등록 스크립트의 둘째 작업(01:47, master,
    workflow_dispatch)이다 — 주간 G2가 05:45까지 가도 겹치지 않는다. 같은 `Invoke-Dispatch`(토큰은
    프로세스 환경변수, 로그 줄)를 거친다."""
    text = script()
    assert "[string]$CiAt = '01:47'" in text
    assert "[string]$CiTaskName = 'PureIron foundation-ci nightly'" in text
    assert "$CiWorkflow = 'foundation-ci.yml'" in text
    assert "return @('workflow', 'run', $CiWorkflow, '--ref', $Ref, '-R', $Repo)" in text
    assert "Register-One $CiTaskName $CiAt '-DispatchCi' $copy $bin" in text
    assert "exit (Invoke-Dispatch -Ci)" in text


def test_foundation_ci_has_no_schedule_and_dispatch_runs_the_conditional_jobs() -> None:
    """변이: schedule을 되살리면 GitHub cron(늦게 만들어짐)과 작업 스케줄러가 같이 뜬다. 조건부
    job(n-minus-1·repro-build)은 plan job의 출력만 보고, plan은 pull_request가 아닌 이벤트를 모두
    전체 실행으로 본다 — workflow_dispatch(작업 스케줄러)도 schedule과 같은 조건이다."""
    text = (paths.REPO / ".github" / "workflows" / "foundation-ci.yml").read_text(encoding="utf-8")
    assert "schedule:" not in text
    assert "cron:" not in text
    assert "  workflow_dispatch:\n" in text
    assert 'if [ "$EVENT" != "pull_request" ] || [ "$FULL_GATE" = "true" ]; then' in text
    assert "if: needs.plan.outputs.n_minus_1 == 'true'" in text
    assert "if: needs.plan.outputs.repro_build == 'true'" in text
    assert "event_name == 'schedule'" not in text


def test_setup_host_script_is_utf8_with_bom() -> None:
    """BOM이 없으면 Windows PowerShell 5.1이 한글 주석을 CP949로 읽는다(register-nightly.ps1과 같은
    처리)."""
    raw = (paths.REPO / "tests" / "e2e" / "infra" / "setup-host.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw[3:].decode("utf-8").startswith("#Requires -RunAsAdministrator")


def test_nightly_workflow_has_no_schedule() -> None:
    """변이: schedule을 되살리면 GitHub cron(3.5~7.5시간 지연)과 작업 스케줄러가 같이 뜬다."""
    text = (paths.REPO / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8")
    assert "schedule:" not in text
    assert "cron:" not in text
