"""G2-6 ② 마이그레이션 실패(M6-3a I6) — 가짜 원격으로 흐름(site-b c0 복원 → FAULT-m g2)과 판정
항목(E_MIGRATE·NEXT 이미지·epoch·스키마 불변·표지 테이블 없음·행 수·10분)을 고정한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from g2_fakes import FakeRemote, scenario_session, snap, tree_expect
from harness import judges, scenarios

REPORT: dict[str, object] = {
    "code": "E_MIGRATE",
    "failed_manual": False,
    "phase_durations_s": {"migrating": 3.2, "rolling_back": 30.0},
}


class Case:
    def __init__(self, tmp_path: Path) -> None:
        self.session = scenario_session(tmp_path)
        p = self.session.pair
        self.tree = tree_expect(self.session.tree)
        self.before = snap(self.tree, p.next, applied=1, attempted=1, result="succeeded")
        self.before["updater"] = {**judges.updater_state(self.before), "schema_epoch": 5}
        self.after = snap(self.tree, p.next, applied=1, attempted=2, result="failed")
        self.after["updater"] = {**judges.updater_state(self.after), "schema_epoch": 5}
        self.report = dict(REPORT)
        self.seconds = 140.0

    def run(self) -> tuple[dict[str, bool], FakeRemote]:
        fake = FakeRemote(self.before, {2: (self.after, self.report, self.seconds)})
        return scenarios.migrate_failure(self.session, fake.remote()), fake


def db_change(s: dict[str, object], **changes: object) -> dict[str, object]:
    return {**s, "db": {**judges.db_of(s), **changes}}


def test_migrate_failure_restores_site_b_c0_then_applies_fault_m(tmp_path: Path) -> None:
    case = Case(tmp_path)
    checks, fake = case.run()
    p = case.session.pair
    assert fake.events == [
        "restore site-b",
        "ready",
        f"build g2 {p.fault_migrate} {p.fault_migrate}",
        "stage g2.pib",
    ]
    assert scenarios.failed(checks) == []
    saved = json.loads(case.session.sites["site-b"].ctx.site.state_file.read_text("utf-8"))
    assert saved["results"][-1]["name"] == scenarios.ITEM_MIGRATE
    assert saved["results"][-1]["schema_diff"] == {}


def test_a_left_marker_table_is_a_partial_apply(tmp_path: Path) -> None:
    case = Case(tmp_path)
    tables = str(judges.db_of(case.after)["tables"])
    case.after = db_change(case.after, tables=f"{tables},e2e_fault_marker")
    assert scenarios.failed(case.run()[0]) == ["no_fault_marker", "schema_unchanged_tables"]


@pytest.mark.parametrize("key", judges.SCHEMA_SETS)
def test_every_schema_set_must_stay_exactly_as_it_was(tmp_path: Path, key: str) -> None:
    case = Case(tmp_path)
    value = str(judges.db_of(case.after)[key])
    case.after = db_change(case.after, **{key: f"{value},zz_partial"})
    assert f"schema_unchanged_{key}" in scenarios.failed(case.run()[0])


def test_the_code_must_be_e_migrate(tmp_path: Path) -> None:
    case = Case(tmp_path)
    case.report = {**REPORT, "code": "E_HEALTH"}
    assert scenarios.failed(case.run()[0]) == ["code"]


def test_the_epoch_must_not_move(tmp_path: Path) -> None:
    case = Case(tmp_path)
    case.after = db_change(case.after, epoch="6")
    assert scenarios.failed(case.run()[0]) == ["db_epoch_kept"]
    case = Case(tmp_path)
    case.after["updater"] = {**judges.updater_state(case.after), "schema_epoch": 6}
    assert scenarios.failed(case.run()[0]) == ["updater_epoch_kept"]


def test_next_images_rows_and_time(tmp_path: Path) -> None:
    case = Case(tmp_path)
    p = case.session.pair
    case.after = snap(case.tree, p.fault_migrate, applied=1, attempted=2, result="failed")
    case.after["updater"] = {**judges.updater_state(case.after), "schema_epoch": 5}
    assert scenarios.failed(case.run()[0]) == ["images_match"]
    case = Case(tmp_path)
    case.after = db_change(case.after, users_rows="1")
    assert scenarios.failed(case.run()[0]) == ["rows_users"]
    case = Case(tmp_path)
    case.seconds = 600.5
    assert scenarios.failed(case.run()[0]) == ["converged_in_time"]


def test_a_700s_staging_is_not_in_ten_minutes(tmp_path: Path) -> None:
    """H1: 수렴 확인은 1.5초여도 스테이징(동기 적용)이 700초면 "10분 안" 거짓."""
    case = Case(tmp_path)
    case.seconds = 700.0
    assert scenarios.failed(case.run()[0]) == ["converged_in_time"]
    saved = json.loads(case.session.sites["site-b"].ctx.site.state_file.read_text("utf-8"))
    assert saved["results"][-1]["seconds"] == 701.5
