"""M6-4a A7·A6 — 시험마다 바로 쓰는 jsonl 결과(pytest 시간 초과 대비), fleet 묶음의 순서·현장·재시도
묶음, 고른 현장만 준비(`prepare(only=)`의 결과 — `site_up`)."""

from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Literal, cast

import nightly
import pytest
from g2_fakes import session
from harness import g2, order, report
from harness.context import E2EError
from harness.failures import INFRA, PRODUCT
from harness.publish import PYTEST_TEST, PYTEST_TIMED_OUT

SCHEDULED = "test_g2_scheduled.py::test_g2_2_scheduled_update[succeeded]"
ISOLATION = "test_g2_scheduled.py::test_g2_3_isolation[images_unchanged]"
ROLLBACK = "test_g2_rollback.py::test_g2_4_rollback"
FAULTS = "test_g2_faults.py::test_g2_6_2_migrate_failure"
CRASH = "test_g2_crash.py::test_g2_6_crash[reset@switching]"
NO_EXT_A = "test_g2_no_external.py::test_g2_no_external[site-a]"
NO_EXT_B = "test_g2_no_external.py::test_g2_no_external[site-b]"
REVERT = "test_g2_revert.py::test_g2_4_revert[fleet_current]"
RESUME = "test_g2_resume.py::test_g2_6_1_resume[resumed]"
WRITES = "test_g2_writes.py::test_g2_w_writes[on_readback]"
VIEW = "test_g2_fleet_view.py::test_g2_9_fleet_view[stale_after_block]"
AIRGAP = "test_g2_airgap.py::test_g2_7_airgap[fleet_last_attempt]"
FLEET = [SCHEDULED, ISOLATION, REVERT, RESUME, WRITES, VIEW, AIRGAP]
EVERY = [ROLLBACK, FAULTS, *FLEET, CRASH, NO_EXT_A, NO_EXT_B]


def line(test_id: str, when: str, outcome: str, cls: str | None = None) -> str:
    row = {"test_id": test_id, "when": when, "outcome": outcome, "failure_class": cls}
    return json.dumps({**row, "seconds": 1.5})


# ---------------------------------------------------------------- jsonl


def test_jsonl_lines_fold_into_one_result_per_test() -> None:
    text = "\n".join(
        [
            line("a.py::x", "call", "passed"),
            line("a.py::y", "setup", "failed", INFRA),
            line("a.py::z", "call", "failed", INFRA),
            line("a.py::z", "teardown", "failed", PRODUCT),
            line("a.py::s", "setup", "skipped"),
            "not json",
        ]
    )
    got = {r.test_id: (r.outcome, r.failure_class) for r in report.parse_jsonl(text)}
    assert got == {
        "a.py::x": ("passed", None),
        "a.py::y": ("failed", INFRA),
        "a.py::z": ("failed", PRODUCT),  # 실패 줄이 모두 infra일 때만 infra
        "a.py::s": ("skipped", None),
    }


def test_without_junit_the_jsonl_results_never_count_as_a_pass(tmp_path: Path) -> None:
    """변이: jsonl에서 빠진 시험(끝나지 못함)을 통과로 세면 시간 초과 run이 green이 된다."""
    (tmp_path / "junit.jsonl").write_text(line("a.py::x", "call", "passed") + "\n", "utf-8")
    results = nightly.read_results(tmp_path / "junit.xml")
    assert [r.test_id for r in results] == ["a.py::x", PYTEST_TEST]
    assert report.verdict(results) == INFRA
    assert nightly.read_results(tmp_path / "missing.xml") == []


def test_a_timed_out_run_keeps_partial_results_and_does_not_retry(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_pytest(junit: Path, targets: list[str], timeout_s: float) -> int:
        calls.append(targets)
        junit.with_suffix(".jsonl").write_text(
            line("test_g2_install.py::test_g2_1[site-a]", "call", "passed") + "\n", "utf-8"
        )
        return PYTEST_TIMED_OUT

    summary = nightly.step_run(
        "20261115T181700Z",
        "a" * 40,
        tmp_path,
        run_pytest=run_pytest,
        recover_env=lambda: (True, []),
        today=date(2026, 11, 16),
        quarantine=[],
    )
    assert len(calls) == 1
    assert summary["verdict"] == INFRA
    assert summary["partial_results"] is True
    ids = [t["test_id"] for t in cast(list[dict[str, object]], summary["tests"])]
    assert ids == ["test_g2_install.py::test_g2_1[site-a]", PYTEST_TEST]


def test_a_crashed_run_with_partial_results_reruns_everything(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_pytest(junit: Path, targets: list[str], timeout_s: float) -> int:
        calls.append(targets)
        if len(calls) == 1:
            junit.with_suffix(".jsonl").write_text(
                line("x.py::a", "call", "passed") + "\n", "utf-8"
            )
            return 3
        junit.write_text(
            '<testsuites><testsuite><testcase file="x.py" name="a" time="1"/>'
            '<testcase file="x.py" name="b" time="1"/></testsuite></testsuites>',
            "utf-8",
        )
        return 0

    summary = nightly.step_run(
        "20261115T181700Z",
        "a" * 40,
        tmp_path,
        run_pytest=run_pytest,
        recover_env=lambda: (True, []),
        today=date(2026, 11, 16),
        quarantine=[],
    )
    assert calls[1] == [nightly.GATES_REL]
    assert summary["verdict"] == "passed"  # 전체 재실행 결과 — `pytest` 줄이 남지 않는다


def gates_conftest() -> ModuleType:
    path = Path(nightly.__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("g2_gates_conftest", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_conftest_hook_writes_one_line_per_finished_phase(tmp_path: Path) -> None:
    hooks = gates_conftest()
    path = tmp_path / "junit.jsonl"
    path.write_text("", "utf-8")
    hooks.JSONL["path"] = path
    phases: tuple[tuple[Literal["setup", "call", "teardown"], Literal["passed", "failed"]], ...] = (
        ("setup", "passed"),
        ("call", "failed"),
        ("teardown", "passed"),
    )
    for when, outcome in phases:
        props: list[tuple[str, object]] = [("failure_class", INFRA)] if outcome == "failed" else []
        hooks.pytest_runtest_logreport(
            pytest.TestReport(
                nodeid="test_g2_x.py::t",
                location=("test_g2_x.py", 1, "t"),
                keywords={},
                outcome=outcome,
                longrepr=None,
                when=when,
                duration=0.25,
                user_properties=props,
            )
        )
    rows = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
    assert rows == [
        {
            "test_id": "test_g2_x.py::t",
            "when": "call",
            "outcome": "failed",
            "failure_class": INFRA,
            "seconds": 0.25,
        }
    ]


# ---------------------------------------------------------------- fleet 묶음


def test_fleet_runs_after_site_b_and_before_crash() -> None:
    backwards = [n for n, _ in reversed(order.FILES)]
    ranked = [backwards[i] for i in order.sort_order(backwards)]
    names = [n for n, _ in order.FILES]
    assert ranked == names
    assert names.index("test_g2_draft.py") < names.index("test_g2_scheduled.py")
    assert "test_g2_approval.py" not in names  # E_APPROVAL은 릴리스 마감 fleet 리허설(★P5)
    assert names.index("test_g2_scheduled.py") < names.index("test_g2_crash.py")
    # 4b 계획 R6: G2-2/3 → G2-4 revert → G2-6 ①(site-a) → G2-W → G2-9(site-b) → G2-7(site-b
    # c0 + 중앙 c1, 묶음 맨 끝) → crash
    fleet = ["test_g2_scheduled.py", "test_g2_revert.py", "test_g2_resume.py"]
    fleet += ["test_g2_writes.py", "test_g2_fleet_view.py", "test_g2_airgap.py", "test_g2_crash.py"]
    assert [n for n, g in order.FILES if g == order.FLEET][-1] == "test_g2_airgap.py"
    assert [names.index(n) for n in fleet] == sorted(names.index(n) for n in fleet)


def test_fleet_tests_need_both_sites_and_the_central() -> None:
    assert order.sites_needed([ISOLATION]) == {"site-a", "site-b"}
    # site-b만 쓰는 fleet 시험도 c1 쌍(두 현장 온라인)이 필요하다
    assert order.sites_needed([VIEW]) == order.sites_needed([WRITES]) == {"site-a", "site-b"}
    assert order.sites_needed([REVERT]) == order.sites_needed([RESUME]) == {"site-a", "site-b"}
    # G2-7은 site-b c0만 쓰지만 중앙 c1 DB(site-b 등록)가 필요하다 — 4a 규칙("c0 시험만이면 중앙을
    # 띄우지 않는다")의 예외가 아니라 fleet 묶음이라 중앙을 띄운다
    assert order.sites_needed([AIRGAP]) == {"site-a", "site-b"}
    assert order.needs_central([AIRGAP])
    assert all(order.needs_central([t]) for t in FLEET)
    assert order.needs_central([ROLLBACK, SCHEDULED])
    assert not order.needs_central([ROLLBACK, FAULTS, CRASH, NO_EXT_A, NO_EXT_B])


@pytest.mark.parametrize(
    ("targets", "want"),
    [
        # fleet 시험 → 두 현장의 외부 접속 0
        ([SCHEDULED], [SCHEDULED, NO_EXT_A, NO_EXT_B]),
        ([ISOLATION], [ISOLATION, NO_EXT_A, NO_EXT_B]),
        ([WRITES], [WRITES, NO_EXT_A, NO_EXT_B]),
        ([REVERT, VIEW], [REVERT, VIEW, NO_EXT_A, NO_EXT_B]),
        ([AIRGAP], [AIRGAP, NO_EXT_A, NO_EXT_B]),
        # 외부 접속 0의 infra 실패는 다시 돌리지 않는다 — fleet 묶음을 끌고 오지 않는다(리뷰 M2 ⓑ)
        ([NO_EXT_B], []),
        ([NO_EXT_A], []),
        ([NO_EXT_A, VIEW], [VIEW, NO_EXT_A, NO_EXT_B]),
        # c0 시나리오만 → 중앙이 필요 없다
        ([ROLLBACK], [ROLLBACK, NO_EXT_A]),
    ],
)
def test_retry_closure_bundles_fleet_tests_with_both_sites(
    targets: list[str], want: list[str]
) -> None:
    got = order.retry_closure(targets, EVERY)
    assert got == want
    assert order.needs_central(got) is any(t in got for t in FLEET)


def test_an_unprepared_site_is_a_clear_error(tmp_path: Path) -> None:
    s = session(tmp_path)
    del s.sites["site-b"]  # prepare(only={"site-a"})
    assert g2.site_up(s, "site-a") is s.sites["site-a"]
    with pytest.raises(E2EError, match="site-b not prepared"):
        g2.site_up(s, "site-b")
    assert not (tmp_path / "site-b" / "state.json").exists()


def test_keep_first_session_moves_the_fleet_record(tmp_path: Path) -> None:
    (tmp_path / "fleet").mkdir()
    (tmp_path / "fleet" / "session.json").write_text(
        json.dumps({"restores": 2, "evidence": {"prepare_s": 230.5}})
    )
    (tmp_path / "site-a").mkdir()
    moved = nightly.keep_first_session(tmp_path, [])
    assert moved == ["site-a", "fleet"]
    assert nightly.fleet_record(tmp_path) == {"first": {"restores": 2, "prepare_s": 230.5}}
