"""G2 메모리 백로그(M6-3a I7) — B-2 세션 최저 가용 메모리 기록·summary 실림, r1 L3 Hyper-V 호스트
점검이 preflight 판단에 들어가 "시작하지 못한 실행"으로 멈추는지. B-1·B-3은
`test_harness_g2.py`."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import nightly
import pytest
from harness import memwatch, preflight, report
from harness.context import InfraError
from harness.hyperv import HyperVError

TODAY = date(2026, 11, 13)


def watch(values: list[float | None]) -> memwatch.MemoryWatch:
    stamps = iter(datetime(2026, 11, 13, 18, 20, i, tzinfo=UTC) for i in range(60))
    w = memwatch.MemoryWatch(lambda: values.pop(0), now=lambda: next(stamps), clock=lambda: 0.0)
    for _ in range(len(values)):
        w.sample()
    return w


def test_watch_keeps_the_lowest_reading_and_counts_unreadable_ones() -> None:
    summary = watch([5.1, None, 1.4, 2.0]).stop()
    assert (summary["min_gb"], summary["samples"], summary["unread"]) == (1.4, 3, 1)
    assert summary["min_at"] == "2026-11-13T18:20:01+00:00"  # 새 최저값일 때만 시각을 읽는다
    assert summary["interval_s"] == memwatch.SAMPLE_S


def test_watch_samples_in_the_background_until_stopped() -> None:
    seen: list[float] = []

    def read() -> float:
        seen.append(3.0)
        return 3.0

    w = memwatch.MemoryWatch(read, interval_s=0.01)
    w.start()
    while len(seen) < 3:
        pass
    summary = w.stop()
    assert summary["min_gb"] == 3.0
    assert isinstance(summary["samples"], int)
    assert summary["samples"] >= 3


def test_a_retry_session_merges_into_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / memwatch.MEMORY_FILE
    first = memwatch.save(path, watch([4.0, 1.9]).stop())
    assert first["sessions"] == 1
    merged = memwatch.save(path, watch([2.5, 1.2, 3.0]).stop())
    assert (merged["min_gb"], merged["samples"], merged["sessions"]) == (1.2, 5, 2)
    again = memwatch.save(path, watch([6.0]).stop())
    assert (again["min_gb"], again["samples"], again["sessions"]) == (1.2, 6, 3)


JUNIT = (
    '<testsuites><testsuite name="pytest"><testcase classname="test_g2_install" '
    'name="test_g2_1_install_and_100_tags_good[site-a]" file="test_g2_install.py" '
    'time="1"/></testsuite></testsuites>'
)


def passing_pytest(path: Path, targets: list[str], timeout_s: float) -> int:
    path.write_text(JUNIT, encoding="utf-8")
    return 0


def no_recovery() -> tuple[bool, list[str]]:
    pytest.fail("a passing run must not recover (real recover touches WSL and Hyper-V)")


def run_once(out: Path) -> dict[str, object]:
    return nightly.step_run(
        "20261113T181700Z",
        "a" * 40,
        out,
        run_pytest=passing_pytest,
        recover_env=no_recovery,
        today=TODAY,
        quarantine=[],
    )


def test_summary_carries_the_lowest_windows_memory(tmp_path: Path) -> None:
    memwatch.save(tmp_path / memwatch.MEMORY_FILE, watch([3.3, 1.7]).stop())
    summary = run_once(tmp_path)
    memory = summary["windows_memory"]
    assert isinstance(memory, dict)
    assert memory["min_gb"] == 1.7
    saved = json.loads((tmp_path / "summary.json").read_text("utf-8"))
    assert saved["windows_memory"]["min_gb"] == 1.7
    assert saved["verdict"] == report.PASSED


def test_summary_without_a_memory_file_says_none(tmp_path: Path) -> None:
    assert run_once(tmp_path)["windows_memory"] is None


# ---------------------------------------------------------------- Hyper-V 호스트 점검(r1 L3)


@pytest.mark.parametrize(
    "exc",
    [
        InfraError("vSwitch pureiron-int missing (run tests/e2e/infra/setup-host.ps1)"),
        HyperVError("powershell failed (1): access denied"),
        subprocess.TimeoutExpired("powershell", 60),
    ],
)
def test_host_check_failure_is_a_preflight_problem(exc: BaseException) -> None:
    def check() -> None:
        raise exc

    problem = preflight.host_problem(check)
    assert problem
    measured = preflight.Measure(9.0, 100.0, True, True, 0, host_problem=problem)
    problems = preflight.judge(measured, preflight.Limits())
    assert problems == [f"Hyper-V host check failed: {problem}"]
    assert preflight.host_problem(lambda: None) is None


def test_host_check_failure_stops_preflight_as_a_run_that_did_not_start() -> None:
    measured = preflight.Measure(9.0, 100.0, True, True, 0, host_problem="vSwitch missing")
    cleaned: list[str] = []

    def cleanup() -> list[str]:
        cleaned.append("cleanup")
        return []

    outcome = preflight.run(preflight.Limits(), measure_now=lambda: measured, cleanup=cleanup)
    assert not outcome.ok
    assert cleaned == []  # 통과하지 못한 preflight는 VM·결과를 지우지 않는다
    summary = nightly.preflight_summary("20261113T181700Z", "a" * 40, TODAY, outcome)
    rows = report.parse_history("\n".join(report.history_lines(summary)))
    assert [r["test_id"] for r in rows] == [report.PREFLIGHT_TEST]
    assert report.not_started(rows)


def test_a_memory_file_that_cannot_be_saved_is_only_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """L5: 저장 실패(디스크·권한·잘못된 run id)는 teardown 오류가 아니라 로그 한 줄."""

    def broken() -> Path:
        raise OSError("disk full")

    assert memwatch.finish(watch([2.0]), broken) is None
    assert "windows memory not saved" in capsys.readouterr().out
    saved = memwatch.finish(watch([3.0]), lambda: tmp_path / memwatch.MEMORY_FILE)
    assert saved is not None
    assert saved["min_gb"] == 3.0


def test_a_missing_powershell_is_a_host_problem() -> None:
    """L6: `powershell.exe`를 못 찾으면(OSError) 호스트 문제로 preflight에서 멈춘다."""

    def missing() -> None:
        raise FileNotFoundError("powershell.exe")

    assert preflight.host_problem(missing) == "powershell.exe"
