"""e2e 하네스의 순수 로직(VM·Hyper-V 없이). 실제 VM 실행은 `tests/e2e/m2d/run.py`."""

from __future__ import annotations

import io
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import cast

import bundles
import crash_at
import golden
import hyperv
import pycdlib
import pytest
import run
from release_pair import BASE, EPOCHS, FAULT, NEXT, RELEASES


def test_passed_follows_phase_order() -> None:
    assert crash_at.passed("locked", "migrating")
    assert crash_at.passed("checking", "succeeded")
    assert crash_at.passed("rolling_back", "failed")
    assert not crash_at.passed("migrating", "locked")
    assert not crash_at.passed("migrating", "migrating")


def test_read_attempt(tmp_path: Path) -> None:
    state = tmp_path / "updater.json"
    assert crash_at.read_attempt(state) == (None, None)
    state.write_text(json.dumps({"attempt": {"generation": 4, "phase": "switching"}}), "utf-8")
    assert crash_at.read_attempt(state) == (4, "switching")
    state.write_text(json.dumps({"attempt": None}), "utf-8")
    assert crash_at.read_attempt(state) == (None, None)
    state.write_text("{not json", "utf-8")
    assert crash_at.read_attempt(state) == (None, None)


def test_wsl_path() -> None:
    assert golden.wsl_path(Path(PureWindowsPath("C:/PureIron_e2e/x y"))).endswith("/x y")


def test_nft_audit_counts_every_rule_by_name() -> None:
    text = golden.nft_audit("10.77.0.0/24")
    assert "10.77.0.0/24" in text
    for name, _ in (*golden.OUT_RULES, *golden.FWD_RULES):
        assert f'counter log prefix "e2e-{name} " comment "{name}"' in text
    assert text.count("{") == text.count("}")
    # 첫 실측 회귀: `fwd`는 nft 예약어라 chain 이름이 되면 테이블 전체가 로드되지 않았다.
    chains = re.findall(r"^\s*chain (\S+) \{", text, re.MULTILINE)
    assert chains
    assert not {"fwd", "forward", "output", "input"} & set(chains)
    # docker0 IGMP(224.0.0.22)는 링크 밖으로 나가지 않는다 — 외부 접속으로 세지 않는다.
    assert "224.0.0.0/4" in dict(golden.OUT_RULES)["ext_out_v4"]


def test_seed_masks_phone_home_before_network() -> None:
    """pollinate·snapd는 runcmd 전에 DNS를 질의했다(첫 실측 dns_out 36) → bootcmd에서 mask."""
    user_data = golden.seed_files("site-a", "10.77.0.10", "k", golden.Network())["user-data"]
    bootcmd = user_data.decode().split("bootcmd:", 1)[1].split("write_files:", 1)[0]
    for unit in ("pollinate.service", "snapd.service", "snapd.socket", "systemd-timesyncd"):
        assert unit in bootcmd
    # 로그인마다 changelogs.ubuntu.com 조회(실측 dns_lookups) — sshd 전에 끈다.
    assert "Prompt=never" in bootcmd
    assert "91-release-upgrade" in bootcmd


def test_seed_iso_round_trip(tmp_path: Path) -> None:
    files = golden.seed_files("site-a", "10.77.0.10", "ssh-ed25519 AAAA test", golden.Network())
    assert "10.77.0.10/24" in files["network-config"].decode()
    user_data = files["user-data"].decode()
    assert user_data.startswith("#cloud-config\n")
    assert "ssh-ed25519 AAAA test" in user_data
    assert "table inet e2e_audit {" in user_data
    iso_path = golden.write_seed_iso(tmp_path / "seed.iso", files)
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    try:
        assert iso.pvd.volume_identifier.rstrip(b" ") == b"cidata"
        for name, data in files.items():
            out = io.BytesIO()
            iso.get_file_from_iso_fp(out, rr_path=f"/{name}")
            assert out.getvalue() == data
    finally:
        iso.close()


def test_ps_quote() -> None:
    assert hyperv.ps_quote("a'b") == "'a''b'"


SNAP: dict[str, object] = {
    "containers": {
        "core": {"image_id": "sha256:c", "running": True},
        "web": {"image_id": "sha256:w", "running": True},
        "collector": {"image_id": "sha256:x", "running": False},
        "simulator": {"image_id": "sha256:s", "running": True},
    },
    "readyz": {"status": 200},
    "project": {"status": 200, "body": {"edit_locked": False}},
}


def test_running_digests_only_app_services_that_run() -> None:
    assert run.running_digests(SNAP) == {"core": "sha256:c", "web": "sha256:w"}


@pytest.mark.parametrize(
    ("project", "ok"),
    [
        ({"status": 200, "body": {"edit_locked": False}}, True),
        ({"status": 200, "body": {"edit_locked": True}}, False),
        ({"status": 503, "body": None}, False),
        ({"error": "x"}, False),
    ],
)
def test_edit_unlocked(project: dict[str, object], ok: bool) -> None:
    assert run.edit_unlocked({**SNAP, "project": project}) is ok
    assert run.ready(SNAP)


WANT = {"core": "sha256:c", "web": "sha256:w"}
DONE: dict[str, object] = {
    **SNAP,
    "updater": {"last_result": "failed", "alarm": None, "applied_gen": 3, "attempt": None},
}


def test_judge_attempt_passes_clean_rollback() -> None:
    checks = run.judge_attempt(
        DONE, {"failed_manual": False}, expect="failed", want_digests=WANT, want_applied=3
    )
    assert all(checks.values())


@pytest.mark.parametrize(
    ("updater", "report", "failing"),
    [
        # result_of()가 failed_manual도 "failed"로 접는다 — 경보·보고로 가린다
        # (첫 실측의 거짓 통과).
        ({"alarm": "g18: E_HEALTH"}, {"failed_manual": True}, "no_manual_alarm"),
        ({}, {"failed_manual": True}, "no_manual_alarm"),
        ({}, {}, "no_manual_alarm"),  # 보고를 못 찾으면 통과시키지 않는다
        ({"applied_gen": 4}, {"failed_manual": False}, "applied_gen"),
        ({"last_result": "succeeded"}, {"failed_manual": False}, "result"),
    ],
)
def test_judge_attempt_flags(
    updater: dict[str, object], report: dict[str, object], failing: str
) -> None:
    snap = {**DONE, "updater": {**cast(dict[str, object], DONE["updater"]), **updater}}
    checks = run.judge_attempt(snap, report, expect="failed", want_digests=WANT, want_applied=3)
    assert not checks[failing]


def test_nft_accumulate_survives_reset_and_restore() -> None:
    total = run.nft_accumulate({"dns_out": 0}, {"dns_out": 2}, {"dns_out": 5})
    assert total == {"dns_out": 3}
    # 표시 없이 재부팅(카운터 0부터) — 줄어든 값은 새로 센 것으로 더한다
    assert run.nft_accumulate(total, {"dns_out": 5}, {"dns_out": 1}) == {"dns_out": 4}
    # 리셋 직후 기준 {} → 처음 보는 키는 0부터
    assert run.nft_accumulate(total, {}, {"ext_out_v4": 2}) == {"dns_out": 3, "ext_out_v4": 2}


@pytest.mark.parametrize(("value", "want"), [("7141", 7141), (12, 12), ({"error": "x"}, -1)])
def test_as_int(value: object, want: int) -> None:
    assert run.as_int(value) == want


def test_dns_names_counts_lookups_except_local_host() -> None:
    log = (
        "Looking up RR for site-a IN A.\n"
        "Looking up RR for changelogs.ubuntu.com IN AAAA.\n"
        "Looking up RR for changelogs.ubuntu.com IN A.\n"
        "Cache miss for x IN A\n"
    )
    assert run.dns_names(log) == {"changelogs.ubuntu.com": 2}


def test_seed_enables_resolved_debug_for_dns_evidence() -> None:
    user_data = golden.seed_files("site-a", "10.77.0.10", "k", golden.Network())["user-data"]
    text = user_data.decode()
    assert "systemd-resolved.service.d/e2e-debug.conf" in text
    assert "SYSTEMD_LOG_LEVEL=debug" in text


def test_relink_repairs_broken_and_stale_symlinks(tmp_path: Path) -> None:
    old, new = tmp_path / "old.tar", tmp_path / "new.tar"
    new.write_text("x", "utf-8")
    link = tmp_path / "site-db.tar"
    link.symlink_to(old)  # 끊긴 symlink — 예전에는 exists() False → symlink_to가 FileExistsError
    bundles.relink(link, new)
    assert link.readlink() == new
    bundles.relink(link, new)  # 이미 맞으면 그대로
    assert link.readlink() == new
    store, other = tmp_path / "store", tmp_path / "other"
    store.mkdir()
    other.mkdir()
    dir_link = tmp_path / "r2"
    dir_link.symlink_to(other, target_is_directory=True)  # 다른 곳(이전 store)을 가리킴
    bundles.relink(dir_link, store)
    assert dir_link.readlink() == store
    with pytest.raises(SystemExit):
        bundles.relink(tmp_path / "x", tmp_path / "missing")
    with pytest.raises(SystemExit):  # 진짜 파일은 지우지 않는다
        bundles.relink(new, store)


RELEASE_RECORDS = Path(__file__).resolve().parents[3] / "deploy" / "releases"


def test_release_pair_is_one_epoch_step_with_derived_fault() -> None:
    assert EPOCHS[NEXT] == EPOCHS[BASE] + 1
    assert RELEASES[FAULT].startswith(RELEASES[NEXT] + "-")
    assert EPOCHS[FAULT] == EPOCHS[NEXT]
    assert (
        FAULT,
        RELEASES[FAULT],
        NEXT,
    ) == (bundles.FAULT_NAME, bundles.FAULT_VERSION, bundles.FAULT_BASE)
    assert RELEASES[BASE] == run.INSTALL_RELEASE


@pytest.mark.parametrize("name", [BASE, NEXT])
def test_release_pair_matches_repo_records(name: str) -> None:
    """저장소 release 기록이 쌍의 달력 버전·epoch와 같다(prepare가 VM 전에 같은 대조를 한다).
    쌍을 바꾸는 커밋은 NEXT 기록(release_record.py)을 먼저 커밋해야 한다 — 없으면 실패."""
    path = RELEASE_RECORDS / f"{name}.json"
    run.check_record(name, json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "record",
    [
        {"version": RELEASES[NEXT], "schema": {"epoch": EPOCHS[NEXT] - 1}},
        {"version": "2026.10.0", "schema": {"epoch": EPOCHS[NEXT]}},
        {"version": RELEASES[NEXT]},
    ],
)
def test_check_record_rejects_other_release(record: dict[str, object]) -> None:
    with pytest.raises(run.E2EError):
        run.check_record(NEXT, record)


def test_crash_plan_covers_every_phase_and_mode_once() -> None:
    plan = run.crash_plan()
    assert len(plan) == 2 * len(run.CRASH_PHASES)
    pairs = {(c.phase, c.mode) for c in plan}
    assert pairs == {(p, m) for p in run.CRASH_PHASES for m in ("kill", "reset")}
    assert len({c.name for c in plan}) == len(plan)


def test_crash_plan_releases() -> None:
    plan = run.crash_plan()
    # migrating: c0(BASE epoch)에서 BASE→NEXT — 실제 마이그레이션이 일어나는 유일한 경로
    migrating = [c for c in plan if c.phase == "migrating"]
    assert all(c.from_c0 and (c.prev, c.release) == (BASE, NEXT) for c in migrating)
    assert [c.from_c0 for c in plan] == [True, True] + [False] * (len(plan) - 2)
    current = NEXT  # c0 뒤 첫 비-c0 시도는 migrating 결과(NEXT) 위에서
    for case in plan[2:]:
        assert case.prev == current
        if case.phase == "rolling_back":
            assert case.release == FAULT
        else:
            assert {case.prev, case.release} == {BASE, NEXT}
            current = case.release


def test_expected_items_are_thirty_and_follow_pair() -> None:
    items = run.expected_items()
    assert len(items) == len(set(items)) == 30
    assert items[2] == "install bundle bootstrap + g1(r2) on offline VM"
    assert items[6:11] == [
        "r2→r3 on compressed epoch-3 DB",
        "r3 schema after upgrade",
        "reject lower gen",
        "r3' health failure → rollback",
        "rollback within 5 min to r2 digests",
    ]
    assert items[11] == "crash kill @ migrating"
    assert items[-1] == run.ITEM_NO_EXTERNAL


# 로컬 TimescaleDB(site-db 2.30.2-pg16)에서 r2 core `migrate up`(0001~0009) → 시드(run.SEED_SQL)
# → r3 core `migrate up`(0010~0012)로 확인한 probe 값. `columns`는 판정에 쓰는 열만 남겼다.
C0_DB: dict[str, object] = {
    "epoch": "3",
    "caggs": "tag_history_1h,tag_history_1m",
    "compressed_chunks": "3",
    "tag_history_rows": "7141",
    "realtime_caggs": "tag_history_1h,tag_history_1m",
    "tables": "devices,project_state,schema_meta,schema_migrations,tag_history,tags",
    "indexes": "devices_pkey,project_state_pkey,schema_meta_pkey,schema_migrations_pkey,"
    "tag_history_tag_id_time_key,tag_history_time_idx,tags_path_key,tags_pkey",
    "columns": "devices.definition,devices.id,project_state.definitions_rev,"
    "project_state.historian,project_state.id,tags.definition,tags.id",
}
NEXT_DB: dict[str, object] = {
    **C0_DB,
    "epoch": "4",
    "tables": "alarm_journal,alarms,audit_log,devices,project_state,schema_meta,"
    "schema_migrations,sessions,tag_history,tags,users",
    "indexes": "alarm_journal_alarm_id_time_idx,alarm_journal_pkey,alarm_journal_time_idx,"
    "alarms_pkey,audit_log_pkey,audit_log_time_idx,audit_log_username_time_idx,devices_pkey,"
    "project_state_pkey,schema_meta_pkey,schema_migrations_pkey,sessions_pkey,"
    "sessions_user_id_idx,tag_history_tag_id_time_key,tag_history_time_idx,tags_path_key,"
    "tags_pkey,users_pkey,users_username_key",
}


def test_judge_c0() -> None:
    assert all(run.judge_c0(C0_DB).values())
    assert not run.judge_c0({**C0_DB, "epoch": "2"})["epoch"]
    assert not run.judge_c0({**C0_DB, "compressed_chunks": "0"})["compressed_chunks"]
    # BASE 산출물이 빠졌거나(epoch 2 DB) NEXT 테이블이 이미 있으면(업그레이드된 DB) c0가 아니다
    assert not run.judge_c0({**C0_DB, "realtime_caggs": ""})["base_schema"]
    assert not run.judge_c0({**C0_DB, "columns": "devices.id"})["base_schema"]
    assert not run.judge_c0(NEXT_DB)["next_absent"]
    assert not any(run.judge_c0({"error": "site-db not running"}).values())


def test_judge_next_schema() -> None:
    assert all(run.judge_next_schema(NEXT_DB).values())
    # 업그레이드 전(BASE) 스키마는 epoch·0010~0012 산출물에서 걸린다
    failing = {k for k, ok in run.judge_next_schema(C0_DB).items() if not ok}
    assert failing == {"epoch", "next_tables", "next_indexes"}
    partial = {**NEXT_DB, "realtime_caggs": "tag_history_1m"}
    assert not run.judge_next_schema(partial)["realtime_caggs"]
    # 집합 비교: 이름의 부분 문자열(예전 판정)로는 통과하지 않는다
    lookalike = {**NEXT_DB, "caggs": "tag_history_1h_old,tag_history_1m"}
    assert not run.judge_next_schema(lookalike)["caggs"]
    no_index = {**NEXT_DB, "indexes": str(NEXT_DB["indexes"]).replace(",sessions_user_id_idx", "")}
    assert not run.judge_next_schema(no_index)["next_indexes"]
    assert not run.judge_next_schema({**NEXT_DB, "columns": {"error": "x"}})["base_columns"]


def test_names_splits_probe_lists() -> None:
    assert run.names({"k": "a,b"}, "k") == {"a", "b"}
    assert run.names({"k": ""}, "k") == frozenset()
    assert run.names({"k": {"error": "x"}}, "k") == frozenset()
    assert run.names({}, "k") == frozenset()


OPEN_AT = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


def completed(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess[str]([], rc, out, err)


def test_stage_for_window_records_each_try() -> None:
    results = iter([completed(2), completed(0, "attempt: g3 verified")])
    ok, info = run.stage_for_window(
        lambda: next(results),
        OPEN_AT,
        watcher_alive=lambda: True,
        now=lambda: OPEN_AT - timedelta(seconds=76.4),
        pause_s=0,
    )
    assert ok
    assert (info["rc"], info["tries"], info["margin_s"]) == (0, 2, 76.4)
    attempts = cast(list[dict[str, object]], info["attempts"])
    assert [a["rc"] for a in attempts] == [2, 0]
    assert all(a["watcher_alive"] is True for a in attempts)
    assert "stdout_tail" not in info


@pytest.mark.parametrize(
    ("rc", "margin"),
    [(255, 70.0), (1, 70.0), (0, -3.0), (0, 0.0)],
)
def test_stage_for_window_false_keeps_evidence(rc: int, margin: float) -> None:
    ok, info = run.stage_for_window(
        lambda: completed(rc, "o" * 2000, "Connection reset"),
        OPEN_AT,
        watcher_alive=lambda: False,
        now=lambda: OPEN_AT - timedelta(seconds=margin),
        pause_s=0,
    )
    assert not ok
    assert (info["rc"], info["tries"], info["margin_s"]) == (rc, 1, margin)
    assert info["stderr_tail"] == "Connection reset"
    assert len(str(info["stdout_tail"])) == run.TAIL_CHARS


def test_stage_for_window_gives_up_after_busy_tries() -> None:
    ok, info = run.stage_for_window(
        lambda: completed(2, "", "error: another updater is running"),
        OPEN_AT,
        watcher_alive=lambda: True,
        now=lambda: OPEN_AT - timedelta(seconds=10),
        pause_s=0,
    )
    assert not ok
    assert (info["rc"], info["tries"]) == (2, run.STAGE_TRIES)
