"""infra 재시도 대상 묶기(M6-3a 리뷰 M1, M6-4b-1 리뷰 M2) — 재시도는 새 세션(새 VM)이라 한 현장의
시나리오와 그 현장의 외부 접속 0(맨 끝)을 함께 다시 돌린다. 외부 접속 0 자체의 infra 실패는 다시
돌리지 않는다. 합친 결과에서 다시 돈 시험은 재시도 결과로 남는다."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import nightly
import pytest
from harness import order, report

ROLLBACK_4 = "test_g2_rollback.py::test_g2_4_rollback"
ROLLBACK_63 = "test_g2_rollback.py::test_g2_6_3_health_failure"
FAULTS_62 = "test_g2_faults.py::test_g2_6_2_migrate_failure"
INSTALL_A = "test_g2_install.py::test_g2_1_install_and_100_tags_good[site-a]"
INSTALL_B = "test_g2_install.py::test_g2_1_install_and_100_tags_good[site-b]"
NO_EXT_A = "test_g2_no_external.py::test_g2_no_external[site-a]"
NO_EXT_B = "test_g2_no_external.py::test_g2_no_external[site-b]"
EVERY = [INSTALL_A, INSTALL_B, ROLLBACK_4, ROLLBACK_63, FAULTS_62, NO_EXT_A, NO_EXT_B]


@pytest.mark.parametrize(
    ("targets", "want"),
    [
        # (b) 시나리오만 → 그 현장의 외부 접속 0을 더한다
        ([ROLLBACK_4], [ROLLBACK_4, NO_EXT_A]),
        ([FAULTS_62], [FAULTS_62, NO_EXT_B]),
        # (a) 외부 접속 0의 infra 실패는 다시 돌리지 않는다(리뷰 M2 ⓑ — infra로만 남는다)
        ([NO_EXT_A], []),
        ([NO_EXT_B], []),
        ([FAULTS_62, NO_EXT_B], [FAULTS_62, NO_EXT_B]),  # 시나리오가 끌고 온 것은 다시 돈다
        # G2-1은 묶지 않는다(세션 fixture가 어차피 다시 만든다), 연쇄하지 않는다
        ([INSTALL_B], [INSTALL_B]),
        ([ROLLBACK_63, INSTALL_B], [INSTALL_B, ROLLBACK_63, NO_EXT_A]),
    ],
)
def test_retry_bundles_scenarios_with_their_site_no_external(
    targets: list[str], want: list[str]
) -> None:
    assert order.retry_closure(targets, EVERY) == want


def test_crash_cases_are_bundled_with_site_a() -> None:
    """crash 묶음은 site-a를 쓴다 — crash 재시도에는 외부 접속 0[site-a]가 따라온다."""
    crash = "test_g2_crash.py::test_g2_6_crash[reset@switching]"
    every = [*EVERY[:5], crash, *EVERY[5:]]
    assert order.retry_closure([crash], every) == [crash, NO_EXT_A]
    assert order.retry_closure([NO_EXT_A], every) == []


CRASH = "test_g2_crash.py::test_g2_6_crash[kill@verified]"


@pytest.mark.parametrize(
    ("selected", "want"),
    [
        ([CRASH, NO_EXT_A], {"site-a"}),  # 주간 crash 재시도 — site-b를 띄우지 않는다
        ([FAULTS_62, NO_EXT_B], {"site-b"}),
        ([INSTALL_A], {"site-a"}),
        ([ROLLBACK_4, FAULTS_62], {"site-a", "site-b"}),
        (EVERY, {"site-a", "site-b"}),
        (["test_g2_install.py::test_g2_1_install_and_100_tags_good"], {"site-a", "site-b"}),
        (["test_g2_unknown.py::test_x"], {"site-a", "site-b"}),
    ],
)
def test_only_the_sites_of_the_selected_tests_are_brought_up(
    selected: list[str], want: set[str]
) -> None:
    """3a 리뷰 L3·변이: 재시도가 현장 둘을 다 올리면 메모리가 모자라 실패한 현장을 같은 조건으로 또
    띄운다(약 7분·VM 한 대 몫)."""
    assert order.sites_needed(selected) == want


def test_bundles_follow_the_order_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """3b가 표에 현장 시험 파일을 더하면 묶음이 따라온다(표에서 현장을 읽는다)."""
    crash = "test_g2_crash.py::test_g2_crash[reset-switching]"
    table = (*order.FILES[:2], ("test_g2_crash.py", "site-a"), *order.FILES[2:])
    monkeypatch.setattr(order, "FILES", table)
    assert order.retry_closure([crash], [*EVERY, crash]) == [NO_EXT_A, crash]
    assert order.retry_closure([ROLLBACK_4, crash], [*EVERY, crash]) == [
        ROLLBACK_4,
        NO_EXT_A,
        crash,
    ]


def junit(*cases: tuple[str, str]) -> str:
    """(시험 id, 결과: pass·infra·product)마다 testcase 하나."""
    body: list[str] = []
    for test_id, outcome in cases:
        file_name, name = test_id.split("::")
        fail = (
            ""
            if outcome == "pass"
            else f'<failure message="x"/><properties><property name="failure_class" '
            f'value="{outcome}"/></properties>'
        )
        body.append(f'<testcase file="{file_name}" name="{name}" time="1">{fail}</testcase>')
    return f'<testsuites><testsuite name="pytest">{"".join(body)}</testsuite></testsuites>'


def test_retry_moves_the_first_session_records_aside(tmp_path: Path) -> None:
    """리뷰 L2·변이: 재시도 세션이 `<site>/state.json`을 다시 만들면 첫 세션(infra로 실패한
    시나리오의
    로그 꼬리·phase)이 사라진다 — 재시도 전에 `first/<site>/`로 옮긴다."""
    first = [(t, "infra" if t == FAULTS_62 else "pass") for t in EVERY]
    calls: list[list[str]] = []

    def run_pytest(path: Path, targets: list[str], timeout_s: float) -> int:
        calls.append(targets)
        site_b = tmp_path / "site-b"
        site_b.mkdir(exist_ok=True)
        (site_b / "state.json").write_text(f"session {len(calls)}", encoding="utf-8")
        path.write_text(junit(*(first if len(calls) == 1 else [(FAULTS_62, "pass")])), "utf-8")
        return 1

    summary = nightly.step_run(
        "20261113T181700Z",
        "a" * 40,
        tmp_path,
        run_pytest=run_pytest,
        recover_env=lambda: (True, []),
        today=date(2026, 11, 13),
        quarantine=[],
    )
    assert (tmp_path / "first" / "site-b" / "state.json").read_text("utf-8") == "session 1"
    assert (tmp_path / "site-b" / "state.json").read_text("utf-8") == "session 2"
    retry = summary["retry"]
    assert isinstance(retry, dict)
    assert retry["first_session"] == ["site-b"]


def test_a_first_session_move_that_fails_does_not_stop_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """리뷰 L3·변이: Windows에서 rename이 `PermissionError`(백신·인덱서가 파일을 잡음)를 내도
    재시도는 돌고, 오류는 summary `retry.first_session_error`에 남는다 — 예외가 step_run 밖으로
    나가면 그날이 `runner` 한 줄이 된다."""
    first = [(t, "infra" if t == FAULTS_62 else "pass") for t in EVERY]
    calls: list[list[str]] = []

    def run_pytest(path: Path, targets: list[str], timeout_s: float) -> int:
        calls.append(targets)
        for site_id in ("site-a", "site-b"):
            (tmp_path / site_id).mkdir(exist_ok=True)
        path.write_text(junit(*(first if len(calls) == 1 else [(FAULTS_62, "pass")])), "utf-8")
        return 1

    real_rename = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == "site-a":
            raise PermissionError(32, "being used by another process", str(self))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    summary = nightly.step_run(
        "20261113T181700Z",
        "a" * 40,
        tmp_path,
        run_pytest=run_pytest,
        recover_env=lambda: (True, []),
        today=date(2026, 11, 13),
        quarantine=[],
    )
    assert len(calls) == 2
    assert summary["verdict"] == "passed"
    retry = summary["retry"]
    assert isinstance(retry, dict)
    assert retry["first_session"] == ["site-b"]
    (error,) = cast(list[str], retry["first_session_error"])
    assert error.startswith("site-a: PermissionError")


def test_step_run_retries_the_bundle_and_keeps_a_failed_retry_of_an_added_test(
    tmp_path: Path,
) -> None:
    """롤백(site-a)만 infra로 실패 → 재시도 대상 = 롤백 + 외부 접속 0[site-a]. 첫 세션에서 통과했던
    외부 접속 0이 재시도 세션에서 실패하면 합친 결과에 실패로 남는다."""
    first = [(t, "infra" if t == ROLLBACK_4 else "pass") for t in EVERY]
    again = [(ROLLBACK_4, "pass"), (ROLLBACK_63, "pass"), (NO_EXT_A, "product")]
    calls: list[list[str]] = []

    def run_pytest(path: Path, targets: list[str], timeout_s: float) -> int:
        calls.append(targets)
        path.write_text(junit(*(first if len(calls) == 1 else again)), encoding="utf-8")
        return 1

    summary = nightly.step_run(
        "20261113T181700Z",
        "a" * 40,
        tmp_path,
        run_pytest=run_pytest,
        recover_env=lambda: (True, []),
        today=date(2026, 11, 13),
        quarantine=[],
    )
    assert calls[1] == [f"{nightly.GATES_REL}/{t}" for t in (ROLLBACK_4, NO_EXT_A)]
    rows = {str(r["test_id"]): r for r in report.test_rows(summary)}
    assert rows[NO_EXT_A]["outcome"] == report.FAILED
    assert rows[NO_EXT_A]["retried"] is True
    assert rows[ROLLBACK_4]["outcome"] == report.PASSED
    assert summary["verdict"] == "product"


def test_a_no_external_infra_failure_alone_is_not_retried(tmp_path: Path) -> None:
    """리뷰 M2 ⓑ: 외부 접속 0[site-b]만 infra로 실패하면 재시도하지 않는다 — 실패는 infra로 남고
    (이력·2연속 infra 이슈), site-b 시나리오·fleet 묶음을 다시 돌려 재시도 시간 제한을 넘지
    않는다."""
    first = [(t, "infra" if t == NO_EXT_B else "pass") for t in EVERY]
    calls: list[list[str]] = []

    def run_pytest(path: Path, targets: list[str], timeout_s: float) -> int:
        calls.append(targets)
        path.write_text(junit(*first), encoding="utf-8")
        return 1

    summary = nightly.step_run(
        "20261113T181700Z",
        "a" * 40,
        tmp_path,
        run_pytest=run_pytest,
        recover_env=lambda: pytest.fail("no retry, so no recovery"),
        today=date(2026, 11, 13),
        quarantine=[],
    )
    assert len(calls) == 1
    assert summary["verdict"] == "infra"
    retry = summary["retry"]
    assert isinstance(retry, dict)
    assert retry["targets"] == []
    assert retry["done"] is False
