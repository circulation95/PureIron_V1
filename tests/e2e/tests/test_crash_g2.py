"""G2-6 ④⑤·주간 crash(M6-3b J3) — 계획 18건(평일 2건·weekly 16건)·release·대상, 매 건 c0 복원 뒤
같은 gen, 판정(run.py crash_once 항목 + 10분 안 수렴), E10 재시도 1회(두 번째도 E10이면 infra)를 VM
없이 고정한다."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from g2_fakes import scenario_session, snap, tree_expect
from harness import crash, g2, judges, scenarios
from harness.context import Context
from harness.vm import Vm

NOW = datetime(2026, 11, 15, 18, 20, tzinfo=UTC)
STAGED: dict[str, object] = {"rc": 0, "tries": 1, "margin_s": 120.0, "attempts": []}
LATE: dict[str, object] = {"rc": 0, "tries": 1, "margin_s": -3.0, "attempts": []}


def test_plan_has_18_cases_two_on_weekdays_and_16_weekly() -> None:
    ids = [case.id for case in crash.PLAN]
    assert len(ids) == len(set(ids)) == 18
    assert {case.id for case in crash.PLAN if not case.weekly} == {
        "reset@switching",
        "kill@migrating",
    }
    assert sum(case.weekly for case in crash.PLAN) == 16
    assert {case.phase for case in crash.PLAN} == set(crash.PHASES)
    assert [c.target for c in crash.PLAN if c.target == "cli"] == ["cli", "cli"]
    assert {c.phase for c in crash.PLAN if c.target == "cli"} == {"fetched"}


def test_rolling_back_goes_to_fault_h_and_the_rest_to_next(tmp_path: Path) -> None:
    pair = scenario_session(tmp_path).pair
    for case in crash.PLAN:
        want = pair.fault if case.phase == "rolling_back" else pair.next
        assert case.release(pair) == want
        assert case.expect == ("failed" if case.phase == "rolling_back" else "succeeded")


class CrashFake:
    """site-a c0(BASE g1)에서 crash 한 건의 원격 가짜. `injections`를 차례로 내고(E10 재시도용) 수렴
    스냅샷은 성공 기대면 NEXT g2 succeeded, 실패 기대면 BASE·failed다."""

    def __init__(self, session: g2.Session, case: crash.CrashCase) -> None:
        self.session = session
        self.case = case
        pair = session.pair
        self.c0 = snap(
            tree_expect(session.tree), pair.base, applied=1, attempted=1, result="succeeded"
        )
        self.events: list[str] = []
        self.injections: list[crash.Injection] = [self.injection(True, True)]
        self.converge_s = 200.0
        self.effect_ok = True
        self.now = 0.0
        self.result_override: str | None = None

    def injection(self, hit: bool, staged: bool) -> crash.Injection:
        stage = STAGED if staged else LATE
        in_time = None if self.case.target == "cli" else staged
        return crash.Injection(hit, "HIT", in_time, dict(stage), "boot-1", 0)

    def after(self) -> dict[str, object]:
        pair = self.session.pair
        if self.case.expect == "failed":
            return snap(
                tree_expect(self.session.tree), pair.base, applied=1, attempted=2, result="failed"
            )
        result = self.result_override or "succeeded"
        tree = tree_expect(self.session.tree_of(pair.next))
        return snap(tree, pair.next, applied=2, attempted=2, result=result)

    def remote(self) -> crash.CrashRemote:
        def build(ctx: Context, gen: int, release: str, tree: Path, open_at: datetime) -> Path:
            self.events.append(f"build g{gen} {release} {tree.name} {open_at:%H:%M:%S}")
            return ctx.out / f"g{gen}.pib"

        def inject(
            ctx: Context, case: crash.CrashCase, gen: int, pib: Path, open_at: datetime
        ) -> crash.Injection:
            self.events.append(f"inject {case.id} g{gen}")
            return self.injections.pop(0) if len(self.injections) > 1 else self.injections[0]

        def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
            self.now += self.converge_s
            return self.after(), self.converge_s

        def effect(
            ctx: Context, case: crash.CrashCase, injection: crash.Injection
        ) -> dict[str, bool]:
            if case.mode == "reset":
                return {"rebooted": self.effect_ok}
            return {"service_restarted": self.effect_ok} if case.target == "service" else {}

        base = scenarios.Remote(
            restore=lambda run: self.events.append(f"restore {run.ctx.site.site_id}"),
            wait_ready=lambda host: self.c0,
            report_for=lambda host, pib: {
                "code": "E_HEALTH" if self.case.expect == "failed" else None,
                "failed_manual": False,
            },
            drop=lambda host, pib: None,
            clock=lambda: self.now,
        )
        return crash.CrashRemote(
            base=base, build=build, inject=inject, effect=effect, converge=converge, now=lambda: NOW
        )


def case_of(case_id: str) -> crash.CrashCase:
    return next(c for c in crash.PLAN if c.id == case_id)


def go(tmp_path: Path, case_id: str, **changes: object) -> tuple[dict[str, bool], CrashFake]:
    session = scenario_session(tmp_path)
    fake = CrashFake(session, case_of(case_id))
    for key, value in changes.items():
        setattr(fake, key, value)
    return crash.run_case(session, fake.case, fake.remote()), fake


@pytest.mark.parametrize("case_id", [case.id for case in crash.PLAN])
def test_every_case_starts_from_c0_with_gen_2_and_passes(tmp_path: Path, case_id: str) -> None:
    """변이: c0 복원 없이 시작하면 앞 건의 상태 위에 주입한다(★H3 — 매 건 독립)."""
    checks, fake = go(tmp_path, case_id)
    pair = fake.session.pair
    release = fake.case.release(pair)
    assert fake.events == [
        "restore site-a",
        f"build g2 {release} {release} 18:22:30",  # 창 = 지금 + 150초
        f"inject {case_id} g2",
    ]
    assert scenarios.failed(checks) == []
    effect = {"rebooted"} if fake.case.mode == "reset" else set[str]()
    if fake.case.mode == "kill" and fake.case.target == "service":
        effect = {"service_restarted"}
    assert effect <= set(checks)
    assert ("staged_before_window" in checks) is (fake.case.target == "service")


def test_convergence_after_ten_minutes_is_a_product_failure(tmp_path: Path) -> None:
    checks, _ = go(tmp_path, "reset@switching", converge_s=600.5)
    assert scenarios.failed(checks) == ["converged_in_time"]


@pytest.mark.parametrize(
    ("changes", "want"),
    [
        ({"effect_ok": False}, ["rebooted"]),
        ({"result_override": "failed"}, ["result"]),
    ],
)
def test_crash_judges_effect_and_result(
    tmp_path: Path, changes: dict[str, object], want: list[str]
) -> None:
    checks, _ = go(tmp_path, "reset@switching", **changes)
    assert scenarios.failed(checks) == want


def test_a_missed_phase_is_a_product_failure_and_is_not_retried(tmp_path: Path) -> None:
    session = scenario_session(tmp_path)
    fake = CrashFake(session, case_of("kill@migrating"))
    fake.injections = [fake.injection(False, True)]
    checks = crash.run_case(session, fake.case, fake.remote())
    assert scenarios.failed(checks) == ["hit"]
    assert fake.events.count("restore site-a") == 1


def test_e10_late_staging_is_retried_once_from_c0(tmp_path: Path) -> None:
    """변이: E10 재시도를 빼면 창 여유 부족(제품은 정상)이 실패로 남는다."""
    session = scenario_session(tmp_path)
    fake = CrashFake(session, case_of("reset@switching"))
    fake.injections = [fake.injection(True, False), fake.injection(True, True)]
    checks = crash.run_case(session, fake.case, fake.remote())
    assert scenarios.failed(checks) == []
    assert fake.events.count("restore site-a") == 2
    assert fake.events.count("inject reset@switching g2") == 2
    saved = session.sites["site-a"].ctx.site.state_file.read_text("utf-8")
    assert '"e10"' in saved


def test_e10_twice_keeps_the_judgment_as_a_failure(tmp_path: Path) -> None:
    """M6-3b 리뷰 M3·변이: 두 번째 E10을 `InfraError`로 바꾸면 스테이징이 느려진 제품 회귀가
    infra로 보이고 nightly 재시도가 그 건을 다시 돈다. run.py처럼 판정(`staged_before_window`
    거짓)을 그대로 남기고, 첫 시도의 `e10` 기록과 두 시도의 `margin_s`가 남는다."""
    session = scenario_session(tmp_path)
    fake = CrashFake(session, case_of("reset@switching"))
    fake.injections = [fake.injection(True, False)]
    checks = crash.run_case(session, fake.case, fake.remote())
    assert scenarios.failed(checks) == ["staged_before_window"]
    assert fake.events.count("restore site-a") == 2
    assert fake.events.count("inject reset@switching g2") == 2
    saved = session.sites["site-a"].ctx.site.state_file.read_text("utf-8")
    assert '"e10"' in saved
    assert saved.count('"margin_s"') >= 2


def test_judge_crash_reads_rows_against_c0() -> None:
    before: dict[str, object] = {
        "db": {
            "users_rows": "2",
            "audit_log_rows": "5",
            "alarm_journal_rows": "0",
            "tag_history_rows": "9",
        }
    }
    after: dict[str, object] = {
        "db": {
            "users_rows": "2",
            "audit_log_rows": "5",
            "alarm_journal_rows": "0",
            "tag_history_rows": "8",
        },
        "updater": {
            "attempt": None,
            "last_attempted_gen": 2,
            "last_result": "succeeded",
            "applied_gen": 2,
        },
    }
    checks = judges.judge_crash(
        before,
        after,
        {"failed_manual": False},
        expect="succeeded",
        gen=2,
        want_digests={},
        hit=True,
        staged_before_window=None,
        effect={},
        seconds=10.0,
    )
    assert checks["rows_tag_history"] is False
    assert checks["converged_in_time"] is True
