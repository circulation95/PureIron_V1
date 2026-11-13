"""G2 시나리오 틀(M6-3a I1) — 실행 순서 표, c0 복원 직전 메모리 확보·카운터 처리, 시나리오 시작
계약(현장 예외 다시 던짐·c0 복원 먼저), 맨 끝 외부 접속 0 판정."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import probe
import pytest
from g2_fakes import C0_GOOD, session
from harness import buildhost, g2, golden, hyperv, judges, order, paths, scenarios, site, sites
from harness.context import Context, E2EError, InfraError
from harness.vm import RemoteCommandError, Vm

GATES = paths.REPO / "tests" / "e2e" / "gates"
VM_3GIB = {"PUREIRON_E2E_VM_GIB": "3"}
DRAFT_ITEMS = ("patch", "draft_gate", "absorb", "reapply", "rollback_keeps_drafts")
REJECT_ITEMS = (
    *("E_SITE", "E_DIGEST", "E_SIG", "E_SCHEMA", "E_UPDATER", "same_gen"),
    *("next_g2", "E_CERT", "E_GEN"),
)


# ---------------------------------------------------------------- 실행 순서


def test_order_puts_install_first_then_site_a_site_b_and_no_external_last() -> None:
    files = [
        "test_g2_no_external.py",
        "test_g2_faults.py",
        "test_g2_rollback.py",
        "test_g2_no_external.py",
        "test_g2_install.py",
        "test_g2_rollback.py",
        "test_g2_install.py",
    ]
    ranked = [files[i] for i in order.sort_order(files)]
    assert ranked == [
        "test_g2_install.py",
        "test_g2_install.py",
        "test_g2_rollback.py",
        "test_g2_rollback.py",
        "test_g2_faults.py",
        "test_g2_no_external.py",
        "test_g2_no_external.py",
    ]
    # 같은 파일 안은 수집 순서 그대로(안정 정렬)
    assert order.sort_order(["test_g2_rollback.py", "test_g2_rollback.py"]) == [0, 1]


def test_order_table_keeps_groups_in_sequence_and_no_external_last() -> None:
    order.check_table()
    assert order.FILES[0] == ("test_g2_install.py", order.FIRST)
    assert order.FILES[-1] == ("test_g2_no_external.py", order.LAST)
    sites = {group: [n for n, g in order.FILES if g == group] for group in ("site-a", "site-b")}
    assert sites == {
        "site-a": ["test_g2_rollback.py"],
        "site-b": ["test_g2_rejections.py", "test_g2_faults.py", "test_g2_draft.py"],
    }
    swapped = [order.FILES[-1], *order.FILES[:-1]]
    with pytest.raises(E2EError, match="out of order"):
        order.check_table(swapped)


def test_a_test_file_without_a_place_stops_collection() -> None:
    with pytest.raises(E2EError, match=r"test_g2_new\.py has no place"):
        order.sort_order(["test_g2_install.py", "test_g2_new.py"])


def collected_g2() -> list[str]:
    done = subprocess.run(
        [
            *(sys.executable, "-m", "pytest", "-c", str(GATES / "pytest.ini"), str(GATES)),
            *("-m", "g2", "--collect-only", "-q", "-p", "no:cacheprovider"),
        ],
        capture_output=True,
        text=True,
        cwd=paths.REPO,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return [line.split("::", 1)[1] for line in done.stdout.splitlines() if "::" in line]


def test_real_g2_collection_follows_the_table() -> None:
    """수집된 실제 G2 시험 id와 순서 — G2-1 → site-a(롤백) → site-b(거부·②) → 외부 접속 0(맨 끝)."""
    assert collected_g2() == [
        "test_g2_1_install_and_100_tags_good[site-a]",
        "test_g2_1_install_and_100_tags_good[site-b]",
        "test_g2_4_rollback",
        "test_g2_6_3_health_failure",
        "test_g2_5_tampered_before_install[tampered_bootstrap]",
        "test_g2_5_tampered_before_install[tampered_deb]",
        *(f"test_g2_5_rejected[{item}]" for item in REJECT_ITEMS),
        "test_g2_6_2_migrate_failure",
        *(f"test_g2_8_draft_recall[{item}]" for item in DRAFT_ITEMS),
        "test_g2_no_external[site-a]",
        "test_g2_no_external[site-b]",
    ]


# ---------------------------------------------------------------- c0 복원


class Host:
    """복원 직전 메모리·Hyper-V·카운터 표시의 가짜 — 측정값을 차례로 내고 호출을 `events`에
    남긴다."""

    def __init__(self, readings: list[float | None], vm_state: str | None = "Running") -> None:
        self.readings = readings
        self.vm_state = vm_state
        self.events: list[str] = []
        self.now = 0.0

    def mem_now(self) -> float | None:
        value = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        self.events.append(f"read {value}")
        return value

    def restore(self, run: g2.SiteRun) -> g2.MemoryGate:
        def mark(ctx: Context, host: Vm) -> None:
            self.events.append(f"mark {ctx.site.site_id}")
            ctx.nft_total = {**ctx.nft_total, "ext_out_v4": ctx.nft_total["ext_out_v4"] + 1}

        def state(name: str) -> str | None:
            self.events.append(f"state {name}")
            return self.vm_state

        def restore(name: str, snapshot: str) -> None:
            self.events.append(f"restore {name} {snapshot}")

        def sleep(seconds: float) -> None:
            self.now += seconds

        return g2.restore_c0(
            run,
            VM_3GIB,
            state=state,
            mark=mark,
            restore=restore,
            wait_ssh=lambda host: self.events.append("ssh"),
            mem_now=self.mem_now,
            reclaim=lambda: self.events.append("reclaim"),
            sleep=sleep,
            clock=lambda: self.now,
        )


def test_restore_of_a_running_vm_marks_counters_then_waits_for_headroom(tmp_path: Path) -> None:
    """켜진 VM: 카운터 표시 → 회수 → 여유 2 GB가 될 때까지 대기 → 복원 → 카운터 기준 = c0 값 →
    ssh."""
    run = session(tmp_path).sites["site-a"]
    host = Host([1.2, 1.5, 2.1])
    gate = host.restore(run)
    assert host.events == [
        "state pureiron-site-a",
        "mark site-a",
        "read 1.2",
        "reclaim",
        "read 1.5",
        "read 2.1",
        "restore pureiron-site-a c0",
        "ssh",
    ]
    assert (gate.need_gb, gate.ok) == (g2.VM_START_HEADROOM_GB, True)
    assert run.ctx.nft_last == {"ext_out_v4": 7}
    assert run.unmarked_restores == 0
    saved = json.loads((run.ctx.out / g2.RESTORE_MEMORY_FILE).read_text("utf-8"))
    assert [(e["vm_state"], e["before_gb"], e["after_gb"], e["ok"]) for e in saved] == [
        ("Running", 1.2, 2.1, True)
    ]


def test_restore_of_a_stopped_vm_needs_a_whole_vm_and_counts_the_lost_interval(
    tmp_path: Path,
) -> None:
    run = session(tmp_path).sites["site-b"]
    host = Host([5.0], vm_state="Off")
    gate = host.restore(run)
    assert "mark site-b" not in host.events
    assert gate.need_gb == g2.vm_start_need_gb(1, VM_3GIB) == 5.0
    assert run.unmarked_restores == 1
    assert host.restore(run).ok  # 두 번째 복원 — 기록이 배열로 쌓인다
    saved = json.loads((run.ctx.out / g2.RESTORE_MEMORY_FILE).read_text("utf-8"))
    assert len(saved) == 2


def test_restore_is_refused_as_infra_when_memory_stays_short(tmp_path: Path) -> None:
    """끝까지 모자라면 복원하지 않는다(`InfraError`, 현장마다 새 객체). 기록은 남는다."""
    runs = session(tmp_path).sites
    errors: list[InfraError] = []
    for site_id in ("site-a", "site-b"):
        host = Host([1.0])
        with pytest.raises(InfraError, match=r"1\.0 GB < 2\.0 GB") as raised:
            host.restore(runs[site_id])
        assert not [e for e in host.events if e.startswith("restore")]
        errors.append(raised.value)
        saved = json.loads((runs[site_id].ctx.out / g2.RESTORE_MEMORY_FILE).read_text("utf-8"))
        assert saved[-1]["ok"] is False
    assert errors[0] is not errors[1]


def test_restore_needs_headroom_when_running_and_a_whole_vm_when_off() -> None:
    assert g2.restore_need_gb(True, VM_3GIB) == 2.0
    assert g2.restore_need_gb(False, VM_3GIB) == 5.0
    assert g2.restore_need_gb(False, {"PUREIRON_E2E_VM_GIB": "4"}) == 6.0


# ---------------------------------------------------------------- 시나리오 시작·외부 접속 0


def test_begin_rethrows_the_site_failure_before_touching_the_vm(tmp_path: Path) -> None:
    s = session(tmp_path)
    boom = RemoteCommandError("install.sh exited 1", 1)
    s.sites["site-a"].error = boom
    remote = scenarios.Remote(restore=lambda run: pytest.fail("must not restore"))
    with pytest.raises(RemoteCommandError) as raised:
        scenarios.begin(s, "site-a", remote)
    assert raised.value is boom


def test_begin_restores_c0_before_reading_the_site(tmp_path: Path) -> None:
    s = session(tmp_path)
    events: list[str] = []

    def ready(host: Vm) -> dict[str, object]:
        events.append(f"ready {host.ip}")
        return {"readyz": {"status": 200}}

    remote = scenarios.Remote(
        restore=lambda run: events.append(f"restore {run.ctx.site.site_id}"), wait_ready=ready
    )
    run, snap = scenarios.begin(s, "site-b", remote)
    assert events == ["restore site-b", "ready 10.77.0.11"]
    assert run is s.sites["site-b"]
    assert judges.ready(snap)


def test_no_external_counts_every_scenario_and_lost_intervals(tmp_path: Path) -> None:
    s = session(tmp_path)

    def mark(ctx: Context, host: Vm) -> None:
        ctx.nft_total = {**ctx.nft_total, "dns_out": ctx.nft_total["dns_out"] + 0}

    assert all(g2.check_no_external(s, "site-a", mark=mark).values())
    s.sites["site-a"].ctx.nft_total["ext_out_v6"] = 2
    assert g2.check_no_external(s, "site-a", mark=mark)["no_external"] is False
    s.sites["site-b"].unmarked_restores = 1
    assert g2.check_no_external(s, "site-b", mark=mark) == {
        "no_external": True,
        "every_interval_counted": False,
    }
    s.sites["site-b"].error = InfraError("vm memory")
    with pytest.raises(InfraError):
        g2.check_no_external(s, "site-b", mark=mark)


# ---------------------------------------------------------------- c0 보강(I2)


@pytest.mark.parametrize(
    ("change", "item"),
    [
        ({"compressed_chunks": "0"}, "c0_compressed_chunks"),
        ({"tag_history_rows": "0"}, "c0_seed_rows"),
        ({"epoch": "4"}, "c0_epoch"),
        ({"compressed_chunks": {"error": "x"}}, "c0_compressed_chunks"),
    ],
)
def test_c0_base_needs_compressed_seed_rows_and_the_release_epoch(
    change: dict[str, object], item: str
) -> None:
    assert all(judges.judge_c0_base(C0_GOOD, 5).values())
    checks = judges.judge_c0_base({**C0_GOOD, **change}, 5)
    assert scenarios.failed(checks) == [item]


def test_install_check_fails_on_a_c0_without_compressed_history(tmp_path: Path) -> None:
    """G2-1 기록·단언에 c0 기반이 들어간다 — 압축 chunk가 없으면 그 현장 G2-1이 실패한다."""
    s = session(tmp_path)
    run = s.sites["site-b"]
    run.c0_db = {**C0_GOOD, "compressed_chunks": "0"}
    run.live = g2.LiveWatch({}, {"tags_good": True}, 1.0)
    run.ctx.digests = {run.release: {}}
    checks = g2.check_install(
        s, "site-b", probe=lambda _: {"readyz": {"status": 200}}, mark=lambda ctx, host: None
    )
    assert checks["c0_compressed_chunks"] is False
    assert checks["c0_seed_rows"] is True
    saved = json.loads(run.ctx.site.state_file.read_text("utf-8"))["results"][-1]
    assert saved["ok"] is False
    assert saved["c0_db"]["compressed_chunks"] == "0"


def test_bring_up_seeds_history_and_measures_c0_base_before_the_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F5: 시드 → c0 기반 측정(`run.c0_db`, `Context.seed_rows`) → 체크포인트 c0 순서."""
    s = session(tmp_path)
    run = s.sites["site-a"]
    events: list[str] = []

    def note(name: str, value: object = None) -> object:
        def call(*args: object, **kwargs: object) -> object:
            events.append(name)
            return value

        return call

    monkeypatch.setattr(golden, "seed_files", note("seed_files", {}))
    monkeypatch.setattr(sites, "boot_vm", note("boot", 1.0))
    monkeypatch.setattr(sites, "site_vm_spec", note("spec"))
    monkeypatch.setattr(site, "nft_counters", note("nft", {}))
    monkeypatch.setattr(site, "resolved_lookups", note("dns", (0, dict[str, int]())))
    monkeypatch.setattr(site, "install_runbook", note("install", 1.0))
    monkeypatch.setattr(buildhost, "site_bundle", note("bundle", tmp_path / "g1.pib"))
    monkeypatch.setattr(site, "start_simulator", note("simulator"))
    monkeypatch.setattr(site, "wait_ready", note("ready", {}))
    monkeypatch.setattr(site, "seed_history", note("seed"))
    monkeypatch.setattr(site, "probe", note("probe", {"db": C0_GOOD}))
    monkeypatch.setattr(site, "add_viewer", note("viewer", {}))
    monkeypatch.setattr(site, "nft_mark", note("mark"))
    monkeypatch.setattr(hyperv, "checkpoint", note("checkpoint"))
    monkeypatch.setattr(g2, "watch_live", note("live", g2.LiveWatch({}, {}, 0.0)))
    monkeypatch.setattr(g2, "trees", type("T", (), {"field_endpoints": note("field", ())}))
    g2.bring_up(run, s, "ssh-ed25519 AAAA test")
    assert events.index("ready") < events.index("seed") < events.index("probe")
    assert events.index("probe") < events.index("checkpoint")
    assert run.c0_db == C0_GOOD
    assert run.ctx.seed_rows == 6900


def test_probe_adds_three_row_counts_and_keeps_every_existing_key() -> None:
    """F6: users·audit_log·alarm_journal 행 수 3개. 기존 키(run.py가 같이 쓴다)는 그대로."""
    existing = {
        "epoch",
        "caggs",
        "compressed_chunks",
        "tag_history_rows",
        "realtime_caggs",
        "tables",
        "indexes",
        "columns",
        "project_files",
        "device_ids",
        "tag_paths",
        "alarm_ids",
        "tag_defs",
        "alarm_defs",
    }
    assert set(probe.DB_SQL) == existing | {"users_rows", "audit_log_rows", "alarm_journal_rows"}
    assert probe.DB_SQL["users_rows"] == "SELECT count(*) FROM users"
    assert probe.DB_SQL["audit_log_rows"] == "SELECT count(*) FROM audit_log"
    assert probe.DB_SQL["alarm_journal_rows"] == "SELECT count(*) FROM alarm_journal"
    assert set(judges.ROW_KEYS) <= set(probe.DB_SQL)


def test_no_external_marks_the_last_interval_before_judging(tmp_path: Path) -> None:
    """M3 ①: 맨 끝 시험의 표시(`mark`)가 마지막 복원 뒤 시나리오 전체 구간을 센다 — 그 구간에
    외부 접속이 있으면(가짜 `mark`가 카운터를 올린다) 판정이 거짓이다."""
    s = session(tmp_path)
    marked: list[str] = []

    def mark(ctx: Context, host: Vm) -> None:
        marked.append(ctx.site.site_id)
        ctx.nft_total = {**ctx.nft_total, "ext_out_v4": ctx.nft_total["ext_out_v4"] + 3}

    checks = g2.check_no_external(s, "site-a", mark=mark)
    assert marked == ["site-a"]
    assert checks["no_external"] is False
    assert s.sites["site-a"].ctx.nft_total["ext_out_v4"] == 3


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"error": "x"}, "2"),
        ("2", {"error": "x"}),
        ({"error": "x"}, {"error": "x"}),
        (None, None),
    ],
)
def test_unreadable_row_counts_are_not_kept(before: object, after: object) -> None:
    """M3 ②: 행 수를 전·후 어느 쪽이든(둘 다 포함) 못 읽으면 `rows_kept`는 거짓이다."""
    rows = dict.fromkeys(judges.ROW_KEYS, "2")
    old = {**rows, "users_rows": before}
    new = {**rows, "users_rows": after}
    checks = judges.rows_kept(old, new)
    assert scenarios.failed(checks) == ["rows_users"]
