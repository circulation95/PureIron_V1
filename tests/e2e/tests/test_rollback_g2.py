"""G2-4 롤백(revert 전) + G2-6 ③ health 실패(M6-3a I5) — 가짜 원격으로 시나리오 흐름(c0 복원 →
FAULT-h g2 → NEXT g3)과 판정 항목을 고정한다. `rolling_back` 구간은 Updater 보고
`phase_durations_s`에서 읽는다."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from g2_fakes import FakeRemote, scenario_session, snap, tree_expect
from harness import g2, judges, paths, scenarios
from harness.context import InfraError

GATES = paths.REPO / "tests" / "e2e" / "gates"
FAULT_REPORT: dict[str, object] = {
    "code": "E_HEALTH",
    "failed_manual": False,
    "phase_durations_s": {"checking": 120.0, "rolling_back": 41.5, "failed": 0.4},
}
RETRY_REPORT: dict[str, object] = {"code": None, "failed_manual": False}


class Case:
    """롤백 시나리오 한 번 — 기본값은 모두 통과하는 가짜 결과. 바꾸고 `run()`."""

    def __init__(self, tmp_path: Path) -> None:
        self.session = scenario_session(tmp_path)
        p = self.session.pair
        self.base_tree = tree_expect(self.session.tree)
        self.fault_tree = tree_expect(self.session.tree_of(p.fault))
        self.next_tree = tree_expect(self.session.tree_of(p.next))
        self.before = snap(self.base_tree, p.base, applied=1, attempted=1, result="succeeded")
        self.fault = snap(self.base_tree, p.base, applied=1, attempted=2, result="failed")
        self.fault_report = dict(FAULT_REPORT)
        self.fault_seconds = 170.0
        self.retry = snap(self.next_tree, p.next, applied=3, attempted=3, result="succeeded")
        self.retry_report = dict(RETRY_REPORT)

    def run(self) -> tuple[scenarios.RollbackOutcome, FakeRemote]:
        fake = FakeRemote(
            self.before,
            {
                2: (self.fault, self.fault_report, self.fault_seconds),
                3: (self.retry, self.retry_report, 95.0),
            },
        )
        return scenarios.rollback(self.session, fake.remote()), fake


def db_change(s: dict[str, object], **changes: object) -> dict[str, object]:
    return {**s, "db": {**judges.db_of(s), **changes}}


def test_rollback_scenario_restores_c0_then_fault_then_retry(tmp_path: Path) -> None:
    case = Case(tmp_path)
    out, fake = case.run()
    p = case.session.pair
    assert fake.events == [
        "restore site-a",
        "ready",
        f"build g2 {p.fault} {p.fault}",
        "stage g2.pib",
        f"build g3 {p.next} {p.next}",
        "stage g3.pib",
    ]
    assert scenarios.failed(out.rollback) == []
    assert scenarios.failed(out.health) == []
    assert scenarios.failed(out.retry) == []
    saved = json.loads(case.session.sites["site-a"].ctx.site.state_file.read_text("utf-8"))
    names = [r["name"] for r in saved["results"]]
    assert names == [scenarios.ITEM_ROLLBACK, scenarios.ITEM_HEALTH, scenarios.ITEM_RETRY]
    assert saved["results"][0]["rolling_back_s"] == 41.5


def test_scenario_does_not_build_or_stage_when_c0_restore_fails(tmp_path: Path) -> None:
    case = Case(tmp_path)
    fake = FakeRemote(case.before, {})

    def refuse(run: g2.SiteRun) -> None:
        raise InfraError("windows available memory 1.0 GB < 2.0 GB")

    def build(*args: object) -> Path:
        pytest.fail(f"built before c0: {args}")

    remote = scenarios.Remote(restore=refuse, build=build, stage=fake.remote().stage)
    with pytest.raises(InfraError):
        scenarios.rollback(case.session, remote)
    assert fake.events == []


# ---------------------------------------------------------------- G2-4 롤백 판정


def test_rolling_back_window_is_read_from_the_report_and_capped_at_300s(tmp_path: Path) -> None:
    case = Case(tmp_path)
    case.fault_report = {**FAULT_REPORT, "phase_durations_s": {"rolling_back": 300.0}}
    assert case.run()[0].rollback["rolling_back_window"] is True
    case.fault_report = {**FAULT_REPORT, "phase_durations_s": {"rolling_back": 300.5}}
    assert scenarios.failed(case.run()[0].rollback) == ["rolling_back_window"]
    case.fault_report = {**FAULT_REPORT, "phase_durations_s": {"checking": 10.0}}
    assert scenarios.failed(case.run()[0].rollback) == ["rolling_back_window"]
    # 전체 적용 시간이 길어도(체크 대기 포함) rolling_back 구간만 본다
    case.fault_report, case.fault_seconds = dict(FAULT_REPORT), 590.0
    assert case.run()[0].rollback["rolling_back_window"] is True


@pytest.mark.parametrize("key", judges.ROW_KEYS)
def test_each_of_the_four_row_counts_must_not_drop(tmp_path: Path, key: str) -> None:
    case = Case(tmp_path)
    before = int(str(judges.db_of(case.before)[key]))
    case.before = db_change(case.before, **{key: str(before + 1)})
    assert scenarios.failed(case.run()[0].rollback) == [f"rows_{key.removesuffix('_rows')}"]


def test_site_rev_and_base_site_rev_must_be_kept(tmp_path: Path) -> None:
    case = Case(tmp_path)
    p = case.session.pair
    case.before = snap(
        case.base_tree, p.base, applied=1, attempted=1, result="succeeded", revs=(4, 4)
    )
    assert scenarios.failed(case.run()[0].rollback) == ["revs_kept"]


def test_rollback_must_bring_back_the_base_tree_and_definition(tmp_path: Path) -> None:
    case = Case(tmp_path)
    p = case.session.pair
    case.fault = snap(case.fault_tree, p.base, applied=1, attempted=2, result="failed")
    failed = scenarios.failed(case.run()[0].rollback)
    assert "base_tree_definition" in failed
    assert "base_tree_project_version" in failed
    case.fault = db_change(
        snap(case.base_tree, p.base, applied=1, attempted=2, result="failed"),
        tag_defs=json.dumps({p.tree_tag: {"description": p.tree_descriptions[p.fault]}}),
    )
    assert scenarios.failed(case.run()[0].rollback) == ["base_tree_definition"]


@pytest.mark.parametrize(
    ("change", "item"),
    [
        ({"code": "E_SWITCH"}, "code"),
        ({"failed_manual": True}, "no_manual_alarm"),
    ],
)
def test_rollback_needs_e_health_and_an_automatic_rollback(
    tmp_path: Path, change: dict[str, object], item: str
) -> None:
    case = Case(tmp_path)
    case.fault_report = {**FAULT_REPORT, **change}
    assert item in scenarios.failed(case.run()[0].rollback)


def test_rollback_needs_base_images_no_alarm_metric_and_unlocked(tmp_path: Path) -> None:
    case = Case(tmp_path)
    p = case.session.pair
    case.fault = snap(case.base_tree, p.fault, applied=1, attempted=2, result="failed")
    assert "images_match" in scenarios.failed(case.run()[0].rollback)
    case.fault = snap(
        case.base_tree,
        p.base,
        applied=1,
        attempted=2,
        result="failed",
        metrics={"updater_failed_manual": 1.0},
    )
    assert scenarios.failed(case.run()[0].rollback) == ["alarm_metric"]
    locked = snap(case.base_tree, p.base, applied=1, attempted=2, result="failed")
    locked["project"] = {
        "status": 200,
        "body": {**judges.project_body(locked), "edit_locked": True},
    }
    case.fault = locked
    assert scenarios.failed(case.run()[0].rollback) == ["edit_unlocked"]


# ---------------------------------------------------------------- G2-6 ③ health 실패


def test_health_failure_needs_failed_in_ten_minutes_and_no_attempt_left(tmp_path: Path) -> None:
    case = Case(tmp_path)
    case.fault_seconds = 601.0
    assert scenarios.failed(case.run()[0].health) == ["failed_in_time"]
    case = Case(tmp_path)
    p = case.session.pair
    stuck = snap(case.base_tree, p.base, applied=1, attempted=2, result="failed")
    stuck["updater"] = {**judges.updater_state(stuck), "attempt": {"phase": "rolling_back"}}
    case.fault = stuck
    assert scenarios.failed(case.run()[0].health) == ["no_attempt_left"]
    unread = snap(case.base_tree, p.base, applied=1, attempted=2, result="failed")
    unread["updater"] = {"error": "PermissionError"}
    case.fault = unread
    assert "no_attempt_left" in scenarios.failed(case.run()[0].health)


def test_health_failure_needs_the_previous_images_ready(tmp_path: Path) -> None:
    case = Case(tmp_path)
    p = case.session.pair
    case.fault = snap(
        case.base_tree, p.base, applied=1, attempted=2, result="failed", readyz={"status": 503}
    )
    assert "previous_ready" in scenarios.failed(case.run()[0].health)
    case.fault = snap(case.base_tree, p.next, applied=1, attempted=2, result="failed")
    assert "previous_ready" in scenarios.failed(case.run()[0].health)


# ---------------------------------------------------------------- 재시도(NEXT)


def test_retry_needs_next_images_tree_and_the_expected_schema(tmp_path: Path) -> None:
    case = Case(tmp_path)
    p = case.session.pair
    case.retry = snap(case.base_tree, p.next, applied=3, attempted=3, result="succeeded")
    assert "next_tree_definition" in scenarios.failed(case.run()[0].retry)
    case.retry = snap(case.next_tree, p.base, applied=3, attempted=3, result="succeeded")
    assert "images_match" in scenarios.failed(case.run()[0].retry)


def test_retry_schema_must_equal_the_expected_one_exactly(tmp_path: Path) -> None:
    """D9 ⓐ: 기대에 없는 테이블 하나도 실패다(⊇로 느슨해지지 않는다)."""
    case = Case(tmp_path)
    tables = str(judges.db_of(case.retry)["tables"])
    case.retry = db_change(case.retry, tables=f"{tables},zz_extra")
    assert scenarios.failed(case.run()[0].retry) == ["schema_tables"]


def test_retry_must_keep_every_c0_name(tmp_path: Path) -> None:
    """D9 ⓑ: c0(BASE)에 있던 이름이 업그레이드 뒤에 없으면 실패다."""
    case = Case(tmp_path)
    indexes = str(judges.db_of(case.before)["indexes"])
    case.before = db_change(case.before, indexes=f"{indexes},base_only_idx")
    assert scenarios.failed(case.run()[0].retry) == ["kept_indexes"]


def test_retry_must_keep_the_compressed_chunks(tmp_path: Path) -> None:
    case = Case(tmp_path)
    case.retry = db_change(case.retry, compressed_chunks="1")
    assert scenarios.failed(case.run()[0].retry) == ["compressed_chunks_kept"]
    case = Case(tmp_path)
    case.before = db_change(case.before, compressed_chunks="0")
    assert "compressed_chunks_kept" in scenarios.failed(case.run()[0].retry)


# ---------------------------------------------------------------- 한 시험만 골라 돌릴 때


def test_selecting_one_test_still_sets_up_the_module_scenario() -> None:
    """infra 재시도는 실패한 시험 id만 새 pytest로 돌린다 — G2-6 ③ 하나만 골라도 module fixture
    (`rollback`)가 c0부터 시나리오를 돈다(실행하지 않고 setup 계획만 본다)."""
    done = subprocess.run(
        [
            *(sys.executable, "-m", "pytest", "-c", str(GATES / "pytest.ini")),
            f"{GATES}/test_g2_rollback.py::test_g2_6_3_health_failure",
            *("-m", "g2", "--setup-plan", "-p", "no:cacheprovider"),
        ],
        capture_output=True,
        text=True,
        cwd=paths.REPO,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line.split() for line in done.stdout.splitlines()]
    assert next(x[:3] for x in lines if x[2:3] == ["g2_session"]) == ["SETUP", "S", "g2_session"]
    assert ["SETUP", "M", "rollback", "(fixtures", "used:", "g2_session)"] in lines
    assert any(x[:3] == ["SETUP", "S", "windows_memory"] for x in lines)  # B-2: 세션 내내 잰다
