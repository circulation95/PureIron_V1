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
import probe
import pycdlib
import pytest
import run
import site_api
from release_pair import BASE, EPOCHS, FAULT, NEXT, NEXT_INDEXES, RELEASES

from pureiron_project import canonical
from pureiron_project.project import load_project


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
    assert not new.is_symlink()
    assert new.read_text("utf-8") == "x"
    with pytest.raises(SystemExit):  # 진짜 디렉터리(store 링크 자리)도 그대로 둔다
        bundles.relink(other, store)
    assert other.is_dir()
    assert not other.is_symlink()


class FakeDocker:
    """`ensure_image` 주입용 — 태그별 로컬 digest와 tar를 load했을 때 남길 digest."""

    def __init__(self, local: str | None, after_load: str) -> None:
        self.local, self.after_load, self.loaded = local, after_load, 0

    def digest_of(self, tag: str) -> str | None:
        return self.local

    def load(self, tar: Path) -> None:
        self.loaded += 1
        self.local = self.after_load


@pytest.mark.parametrize(
    ("local", "after_load", "loads"),
    [
        ("sha256:want", "sha256:want", 0),  # 이미 기록과 같다 → 적재하지 않는다
        (None, "sha256:want", 1),  # 태그 없음 → store tar 적재
        ("sha256:rebuilt", "sha256:want", 1),  # 같은 태그로 다시 빌드한 이미지 → 다시 적재
    ],
)
def test_ensure_image_loads_only_when_digest_differs(
    local: str | None, after_load: str, loads: int
) -> None:
    docker = FakeDocker(local, after_load)
    bundles.ensure_image(
        "pureiron/core:r3",
        Path("core.tar"),
        "sha256:want",
        digest_of=docker.digest_of,
        load=docker.load,
    )
    assert docker.loaded == loads


def test_ensure_image_stops_when_store_tar_differs() -> None:
    docker = FakeDocker("sha256:rebuilt", "sha256:other")
    with pytest.raises(SystemExit):
        bundles.ensure_image(
            "pureiron/core:r3",
            Path("core.tar"),
            "sha256:want",
            digest_of=docker.digest_of,
            load=docker.load,
        )
    assert docker.loaded == 1


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
        # envelope가 [epoch, epoch + 1]이 아니면 롤백 판정의 전제가 기록과 어긋난다
        {
            "version": RELEASES[NEXT],
            "schema": {
                "epoch": EPOCHS[NEXT],
                "app_expected": EPOCHS[NEXT],
                "app_max_compatible": 9,
            },
        },
        {
            "version": RELEASES[NEXT],
            "schema": {"epoch": EPOCHS[NEXT], "app_expected": EPOCHS[BASE]},
        },
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


def test_release_pair_is_r3_to_r4() -> None:
    """M5 마감 쌍 — 두 기록(r3·r4)이 모두 저장소에 있어야 한다
    (`test_release_pair_matches_repo_records`)."""
    assert (BASE, NEXT, FAULT) == ("r3", "r4", "r4f")
    assert (EPOCHS[BASE], EPOCHS[NEXT]) == (4, 5)


def test_expected_items_are_thirty_five_and_follow_pair() -> None:
    items = run.expected_items()
    assert len(items) == len(set(items)) == 35
    assert items[2] == "install bundle bootstrap + g1(r3) on offline VM"
    assert items[6:16] == [
        "r3→r4 on compressed epoch-4 DB",
        "r4 schema after upgrade",
        "reject lower gen",
        "r4 project tree applied (project_files, definitions, base_site_rev)",
        "site PATCH via web (admin login, tag + alarm) → site_rev +2",
        "reject unabsorbed site drafts (E_DRAFT)",
        "draft export → absorb → gate → reapply (PATCH kept, base_site_rev == site_rev)",
        "r4' health failure → rollback (transition, E_DEFINITIONS_LEFT)",
        "rollback within 5 min to r3 digests",
        "r4 tree bundle after transition rollback → succeeded, alarm cleared",
    ]
    assert items[16] == "crash kill @ migrating"
    assert items[-1] == run.ITEM_NO_EXTERNAL
    # S4의 30개 중 쌍과 무관한 항목·crash 18개는 이름 그대로다(실측끼리 대응표로 비교)
    assert len([i for i in items if i.startswith("crash ")]) == 18


def test_scenario_gens_keep_bundle_names_apart() -> None:
    gens = run.scenario_gens(2)
    # NEXT 줄(업그레이드 → ⓒ → ⓓ)은 차례로 올라가야 한다(낮은 gen은 E_GEN)
    assert gens["upgrade"] < gens["draft_gate"] < gens["absorb"]
    # c0 복원 뒤 줄: applied g1 위 FAULT → 경보 해제
    assert 1 < gens["fault"] < gens["alarm_clear"]
    # 번들 파일 이름 `g<gen>-<release>`가 겹치지 않는다
    named = [
        (gens["upgrade"], NEXT),
        (gens["draft_gate"], NEXT),
        (gens["absorb"], NEXT),
        (gens["fault"], FAULT),
        (gens["alarm_clear"], NEXT),
    ]
    assert len(set(named)) == len(named)
    assert gens["next"] > max(v for k, v in gens.items() if k != "next")


# 로컬 TimescaleDB(site-db 2.30.2-pg16, compose pureiron-it-m5c)에서 Updater 순서로 실측한 probe
# `db` 값: r3 core `migrate up`(0001~0012) → r3 `project apply`(메타만, 트리 이전 release) →
# 시드(run.SEED_SQL) → C0_DB / r4 core `migrate up`(0013) → r4 `project apply --tar`(fixture 트리) →
# NEXT_DB. `columns`는 판정에 쓰는 열만 남겼다.
C0_DB: dict[str, object] = {
    "epoch": "4",
    "caggs": "tag_history_1h,tag_history_1m",
    "compressed_chunks": "3",
    "tag_history_rows": "7141",
    "realtime_caggs": "tag_history_1h,tag_history_1m",
    "tables": "alarm_journal,alarms,audit_log,devices,project_state,schema_meta,schema_migrations,"
    "sessions,tag_history,tags,users",
    "indexes": "alarm_journal_alarm_id_time_idx,alarm_journal_pkey,alarm_journal_time_idx,"
    "alarms_pkey,audit_log_pkey,audit_log_time_idx,audit_log_username_time_idx,devices_pkey,"
    "project_state_pkey,schema_meta_pkey,schema_migrations_pkey,sessions_pkey,"
    "sessions_user_id_idx,tag_history_tag_id_time_key,tag_history_time_idx,tags_path_key,"
    "tags_pkey,users_pkey,users_username_key",
    "columns": "alarms.definition,devices.definition,devices.id,project_state.definitions_rev,"
    "project_state.historian,project_state.id,tags.definition,tags.id",
    "project_files": {"error": 'ERROR:  relation "project_files" does not exist'},
    "device_ids": "e2e-dev",
    "tag_paths": "e2e/tag",
    "alarm_ids": "",
    "tag_defs": "{}",
    "alarm_defs": "{}",
}
NEXT_DB: dict[str, object] = {
    **C0_DB,
    "epoch": "5",
    "tag_history_rows": "7146",
    "tables": "alarm_journal,alarms,audit_log,devices,project_files,project_state,schema_meta,"
    "schema_migrations,sessions,tag_history,tags,users",
    "indexes": "alarm_journal_alarm_id_time_idx,alarm_journal_pkey,alarm_journal_time_idx,"
    "alarms_pkey,audit_log_pkey,audit_log_time_idx,audit_log_username_time_idx,devices_pkey,"
    "project_files_pkey,project_state_pkey,schema_meta_pkey,schema_migrations_pkey,"
    "sessions_pkey,sessions_user_id_idx,tag_history_tag_id_time_key,tag_history_time_idx,"
    "tags_path_key,tags_pkey,users_pkey,users_username_key",
    "columns": str(C0_DB["columns"]) + ",project_files.content,project_files.path",
    "project_files": "4",
    "device_ids": "plc-1",
    "tag_paths": "line1/remote_ok,line1/running,line1/start,line1/temp,line1/temp_sp",
    "alarm_ids": "temp-high,temp-low",
    "tag_defs": '{ "line1/temp" : {"eu": "°C", "path": "line1/temp", "device": "plc-1", '
    '"modbus": {"address": 0, "data_type": "float32", "register_type": "holding"}, '
    '"record": {"deadband": 0.5, "max_interval_s": 60}, "description": "1라인 온도"} }',
    "alarm_defs": '{ "temp-high" : {"id": "temp-high", "tag": "line1/temp", "type": "above", '
    '"priority": "high", "setpoint": 80}, "temp-low" : {"id": "temp-low", "tag": "line1/temp", '
    '"type": "below", "deadband": 1.5, "setpoint": 5, "on_delay_s": 10} }',
}


def test_judge_c0() -> None:
    assert all(run.judge_c0(C0_DB).values())
    assert not run.judge_c0({**C0_DB, "epoch": "3"})["epoch"]
    assert not run.judge_c0({**C0_DB, "compressed_chunks": "0"})["compressed_chunks"]
    # BASE 산출물이 빠졌거나(epoch 3 DB: 0010~0012 없음) NEXT 테이블이 이미 있으면(업그레이드된
    # DB) c0가 아니다
    assert not run.judge_c0({**C0_DB, "realtime_caggs": ""})["base_schema"]
    assert not run.judge_c0({**C0_DB, "columns": "devices.id"})["base_schema"]
    no_alarms = str(C0_DB["tables"]).replace("alarm_journal,alarms,", "")
    assert not run.judge_c0({**C0_DB, "tables": no_alarms})["base_schema"]
    no_index = str(C0_DB["indexes"]).replace("sessions_user_id_idx,", "")
    assert not run.judge_c0({**C0_DB, "indexes": no_index})["base_schema"]
    assert not run.judge_c0(NEXT_DB)["next_absent"]
    assert not any(run.judge_c0({"error": "site-db not running"}).values())


def test_judge_next_schema() -> None:
    assert all(run.judge_next_schema(NEXT_DB).values())
    # 업그레이드 전(BASE) 스키마는 epoch·0013 산출물에서 걸린다(서로 바꿔 넣으면 실패)
    failing = {k for k, ok in run.judge_next_schema(C0_DB).items() if not ok}
    assert failing == {"epoch", "next_tables"}
    partial = {**NEXT_DB, "realtime_caggs": "tag_history_1m"}
    assert not run.judge_next_schema(partial)["realtime_caggs"]
    # 집합 비교: 이름의 부분 문자열로는 통과하지 않는다
    lookalike = {**NEXT_DB, "caggs": "tag_history_1h_old,tag_history_1m"}
    assert not run.judge_next_schema(lookalike)["caggs"]
    lookalike_table = {
        **NEXT_DB,
        "tables": str(NEXT_DB["tables"]).replace("project_files,", "project_files_old,"),
    }
    assert not run.judge_next_schema(lookalike_table)["next_tables"]
    # BASE(epoch 4) 산출물이 업그레이드 뒤 사라지면 걸린다
    no_index = {**NEXT_DB, "indexes": str(NEXT_DB["indexes"]).replace(",sessions_user_id_idx", "")}
    assert not run.judge_next_schema(no_index)["base_indexes"]
    no_users = {**NEXT_DB, "tables": str(NEXT_DB["tables"]).replace(",users", "")}
    assert not run.judge_next_schema(no_users)["base_tables"]
    assert not run.judge_next_schema({**NEXT_DB, "columns": {"error": "x"}})["base_columns"]


def test_next_indexes_empty_until_named() -> None:
    """0013은 이름 붙은 인덱스를 만들지 않는다 — 빈 집합 포함 검사는 늘 참이라 항목에서 뺐다."""
    assert frozenset() == NEXT_INDEXES
    assert "next_indexes" not in run.judge_next_schema(NEXT_DB)


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


# ---------------------------------------------------------------- M5 마감: 전환기 경보·M5 확인


LEFT_ALARM = "g2: E_HEALTH E_DEFINITIONS_LEFT"
LEFT_LOGS = (
    "not healthy in time: collector session down\n"
    "rollback: E_DEFINITIONS_LEFT: project definitions of the failed generation left in place"
)
TRANSITION: dict[str, object] = {
    **DONE,
    "updater": {**cast(dict[str, object], DONE["updater"]), "alarm": LEFT_ALARM},
    "metrics": {"updater_failed_manual": 1.0},
}


def test_judge_attempt_expects_transition_alarm() -> None:
    report: dict[str, object] = {"failed_manual": False, "logs": LEFT_LOGS}
    checks = run.judge_attempt(
        TRANSITION,
        report,
        expect="failed",
        want_digests=WANT,
        want_applied=3,
        expect_alarm=run.E_DEFINITIONS_LEFT,
        check_metric=True,
    )
    assert all(checks.values())
    assert "no_manual_alarm" not in checks
    # S4 기본 판정(경보 없음 기대)이면 같은 상태가 실패한다 — c0(r3)에서 r4f는 설계상 경보를 세운다
    default = run.judge_attempt(
        TRANSITION, report, expect="failed", want_digests=WANT, want_applied=3
    )
    assert not default["no_manual_alarm"]
    assert "alarm_metric" not in default


@pytest.mark.parametrize(
    ("alarm", "report", "metric", "failing"),
    [
        (None, {"failed_manual": False, "logs": LEFT_LOGS}, 0.0, "expected_alarm"),
        ("g2: E_HEALTH", {"failed_manual": False, "logs": LEFT_LOGS}, 1.0, "expected_alarm"),
        (LEFT_ALARM, {"failed_manual": False, "logs": "rollback ok"}, 1.0, "expected_alarm"),
        (LEFT_ALARM, {"failed_manual": True, "logs": LEFT_LOGS}, 1.0, "expected_alarm"),
        (LEFT_ALARM, {}, 1.0, "expected_alarm"),  # 보고를 못 찾으면 통과시키지 않는다
        (LEFT_ALARM, {"failed_manual": False, "logs": LEFT_LOGS}, 0.0, "alarm_metric"),
        (LEFT_ALARM, {"failed_manual": False, "logs": LEFT_LOGS}, None, "alarm_metric"),
    ],
)
def test_judge_attempt_transition_alarm_flags(
    alarm: str | None, report: dict[str, object], metric: float | None, failing: str
) -> None:
    snap = {
        **TRANSITION,
        "updater": {**cast(dict[str, object], TRANSITION["updater"]), "alarm": alarm},
        "metrics": {} if metric is None else {"updater_failed_manual": metric},
    }
    checks = run.judge_attempt(
        snap,
        report,
        expect="failed",
        want_digests=WANT,
        want_applied=3,
        expect_alarm=run.E_DEFINITIONS_LEFT,
        check_metric=True,
    )
    assert not checks[failing]


def test_judge_attempt_alarm_cleared_by_tree_release() -> None:
    cleared: dict[str, object] = {
        **DONE,
        "updater": {"last_result": "succeeded", "alarm": None, "applied_gen": 5, "attempt": None},
        "metrics": {"updater_failed_manual": 0.0},
    }

    def judge(snap: dict[str, object]) -> dict[str, bool]:
        return run.judge_attempt(
            snap,
            {"failed_manual": False},
            expect="succeeded",
            want_digests=WANT,
            want_applied=5,
            check_metric=True,
        )

    assert all(judge(cleared).values())
    # 경보가 남았으면(정의를 교체하지 않은 성공 등) 상태·메트릭 둘 다에서 걸린다
    left = {
        **cleared,
        "updater": {**cast(dict[str, object], cleared["updater"]), "alarm": LEFT_ALARM},
        "metrics": {"updater_failed_manual": 1.0},
    }
    checks = judge(left)
    assert not checks["no_manual_alarm"]
    assert not checks["alarm_metric"]


def test_alarm_has_matches_joined_entries() -> None:
    code = run.E_DEFINITIONS_LEFT
    assert run.alarm_has(LEFT_ALARM, code)
    assert run.alarm_has("g3: E_HEALTH; g2: E_HEALTH E_DEFINITIONS_LEFT", code)
    assert not run.alarm_has("g3: E_HEALTH", code)
    assert not run.alarm_has("g2: E_HEALTH E_DEFINITIONS_LEFT_X", code)
    assert not run.alarm_has(None, code)


def test_metric_and_prom_parsing() -> None:
    text = (
        "# TYPE updater_last_result gauge\n"
        'updater_last_result{result="failed"} 1\n'
        "updater_applied_generation 1\n"
        "updater_failed_manual 1\n"
        'updater_phase_duration_seconds{phase="checking"} 92.5\n'
    )
    parsed = probe.parse_prom(text)
    assert parsed["updater_failed_manual"] == 1.0
    assert parsed['updater_last_result{result="failed"}'] == 1.0
    assert parsed['updater_phase_duration_seconds{phase="checking"}'] == 92.5
    assert run.metric({"metrics": parsed}, run.ALARM_METRIC) == 1.0
    assert run.metric({"metrics": {"error": "FileNotFoundError"}}, run.ALARM_METRIC) is None
    assert run.metric({}, run.ALARM_METRIC) is None


TREE = run.tree_expect(run.FIXTURE)
PROJECT_BODY: dict[str, object] = {
    "project_id": "acme",
    "version": "v1",
    "content_hash": TREE.content_hash,
    "base_site_rev": 0,
    "site_rev": 0,
    "edit_locked": False,
}
TREE_SNAP: dict[str, object] = {
    **SNAP,
    "project": {"status": 200, "body": PROJECT_BODY},
    "db": NEXT_DB,
}


def test_tree_expect_reads_fixture() -> None:
    assert TREE.files == 4  # project.yaml·devices·tags·alarms(screens/.gitkeep은 파일이 아니다)
    assert TREE.devices == {"plc-1"}
    assert TREE.tags == {
        "line1/temp",
        "line1/running",
        "line1/temp_sp",
        "line1/remote_ok",
        "line1/start",
    }
    assert TREE.alarms == {"temp-high", "temp-low"}
    assert TREE.content_hash == load_project(run.FIXTURE).content_hash()


def with_body(**changes: object) -> dict[str, object]:
    return {**TREE_SNAP, "project": {"status": 200, "body": {**PROJECT_BODY, **changes}}}


def test_judge_tree() -> None:
    assert all(run.judge_tree(TREE_SNAP, TREE).values())
    assert not run.judge_tree(with_body(site_rev=1), TREE)["base_site_rev"]
    assert not run.judge_tree(with_body(version="v2"), TREE)["project_version"]
    assert not run.judge_tree(with_body(content_hash="sha256:00"), TREE)["project_version"]
    # r3(트리 이전) 적용이면 project_files가 없고 시드 장비·태그가 retire되지 않는다
    r3 = {**TREE_SNAP, "db": C0_DB}
    failing = {k for k, ok in run.judge_tree(r3, TREE).items() if not ok}
    assert failing == {"project_files", "devices", "tags", "alarms"}
    extra = {**TREE_SNAP, "db": {**NEXT_DB, "device_ids": "e2e-dev,plc-1"}}
    assert not run.judge_tree(extra, TREE)["devices"]
    assert not any(run.judge_tree({"project": {"status": 503}, "db": {}}, TREE).values())


TAG_PATCHED = {"line1/temp": {"path": "line1/temp", "record": run.PATCH_TAG_BODY["record"]}}
ALARM_PATCHED = {"temp-high": {"id": "temp-high", "setpoint": 82.5, "priority": "high"}}
PATCHED_DB: dict[str, object] = {
    **NEXT_DB,
    "tag_defs": json.dumps(TAG_PATCHED),
    "alarm_defs": json.dumps(ALARM_PATCHED),
}
PATCH_OUT: dict[str, object] = {
    "project_before": [200, {**PROJECT_BODY}],
    "login": [200, {"user": {"username": "e2e-admin", "role": "admin"}}],
    "session_cookie": True,
    "tag": [200, {"site_rev": 1, "definition": TAG_PATCHED["line1/temp"]}],
    "alarm": [200, {"site_rev": 2, "definition": ALARM_PATCHED["temp-high"]}],
    "logout": 204,
    "project_after": [200, {**PROJECT_BODY, "site_rev": 2}],
}


def test_judge_patch() -> None:
    assert all(run.judge_patch(PATCH_OUT, PATCHED_DB).values())
    # PATCH 전 정의(fixture 값)이면 읽기 대조에서 걸린다
    assert not run.judge_patch(PATCH_OUT, NEXT_DB)["readback"]


@pytest.mark.parametrize(
    ("changes", "failing"),
    [
        ({"login": [401, {"detail": "invalid credentials"}]}, "login"),
        ({"session_cookie": False}, "login"),
        ({"tag": [403, {"detail": "E_ORIGIN"}]}, "tag_patch"),
        ({"alarm": [423, {"detail": "E_EDIT_LOCKED"}]}, "alarm_patch"),
        ({"alarm": [200, {"site_rev": 1}]}, "alarm_patch"),  # no-op PATCH(값이 같음)
        ({"project_after": [200, {**PROJECT_BODY, "site_rev": 1}]}, "site_rev"),
        ({"project_after": [200, {**PROJECT_BODY, "site_rev": 2, "base_site_rev": 2}]}, "site_rev"),
        ({"project_before": [503, None]}, "site_rev"),
        ({"error": "ssh exited 255"}, "login"),
    ],
)
def test_judge_patch_flags(changes: dict[str, object], failing: str) -> None:
    out = {"error": changes["error"]} if "error" in changes else {**PATCH_OUT, **changes}
    assert not run.judge_patch(out, PATCHED_DB)[failing]


ABSORB_HASH = "sha256:" + "ab" * 32
# 로컬 사전 검증의 `bundles.py absorb` 실제 출력(fingerprint·hash만 줄임)
ABSORBED: dict[str, object] = {
    "dir": "/home/x/m2d-e2e/work/m5/absorbed",
    "absorb": {
        "rc": 0,
        "stdout": "site key: site-a 9aa4 (compare with the installation record)\n"
        "draft: site_rev=2 base_site_rev=0 content_hash=sha256:6a8c\n"
        "alarms/line1.yaml: alarm temp-high setpoint: 80 -> 82.5\n"
        'tags/plc-1.yaml: tag line1/temp record: {"deadband":0.5,"max_interval_s":60} -> '
        '{"deadband":0.8,"max_interval_s":30}\n'
        "absorbs[site-a]: (absent) -> 2\n"
        "wrote alarms/line1.yaml\n"
        "wrote project.yaml\n"
        "wrote tags/plc-1.yaml\n"
        f"ok: {ABSORB_HASH}\n"
        "Absorbs-Draft: site-a rev=2 sha256=b03d\n",
        "stderr": "",
    },
    "gate": {
        "rc": 0,
        "stdout": "site key: site-a 9aa4 (compare with the installation record)\n"
        "ok: site site-a site_rev=2 base_site_rev=0 absorbs=2\n",
        "stderr": "",
    },
}


def test_judge_absorb() -> None:
    assert all(run.judge_absorb(ABSORBED, 2).values())
    assert run.absorbed_hash(ABSORBED) == ABSORB_HASH
    assert not run.judge_absorb(ABSORBED, 3)["absorbs_rev"]
    # absorb가 실패하면 gate는 돌지 않는다(결과에 `gate` 없음)
    conflict = {"dir": ABSORBED["dir"], "absorb": {"rc": 2, "stdout": "", "stderr": "conflict: x"}}
    checks = run.judge_absorb(conflict, 2)
    assert not checks["absorb"]
    assert not checks["gate"]
    assert run.absorbed_hash(conflict) is None
    gate_failed = {**ABSORBED, "gate": {"rc": 1, "stdout": "", "stderr": "error: E_DRAFT"}}
    assert not run.judge_absorb(gate_failed, 2)["gate"]


def test_judge_reapplied() -> None:
    body = {**PROJECT_BODY, "content_hash": ABSORB_HASH, "site_rev": 2, "base_site_rev": 2}
    snap = {**TREE_SNAP, "project": {"status": 200, "body": body}, "db": PATCHED_DB}
    assert all(run.judge_reapplied(snap, 2, ABSORB_HASH).values())
    # 흡수 전 트리(fixture hash)가 적용됐거나 기준점이 안 옮겨졌거나 PATCH 값이 되돌아가면 실패
    assert not run.judge_reapplied(snap, 2, None)["content_hash"]
    moved = {**snap, "project": {"status": 200, "body": {**body, "base_site_rev": 0}}}
    assert not run.judge_reapplied(moved, 2, ABSORB_HASH)["base_site_rev"]
    reverted = {**snap, "db": NEXT_DB}
    assert not run.judge_reapplied(reverted, 2, ABSORB_HASH)["patch_kept"]


def test_unabsorbed_copy_drops_only_absorbs(tmp_path: Path) -> None:
    fixture = canonical.load((bundles.FIXTURE / "project.yaml").read_text("utf-8"), "project.yaml")
    # fixture가 이미 site-a를 PATCH 수 이상 흡수했다고 적고 있어 VM site_rev로는 게이트가 통과한다
    absorbs = cast(dict[str, object], cast(dict[str, object], fixture)["absorbs"])
    assert cast(int, absorbs["site-a"]) >= run.PATCHES
    dest = bundles.unabsorbed_copy(bundles.FIXTURE, tmp_path / "repo")
    doc = cast(dict[str, object], canonical.load((dest / "project.yaml").read_text("utf-8")))
    assert "absorbs" not in doc
    assert {k: v for k, v in cast(dict[str, object], fixture).items() if k != "absorbs"} == doc
    for rel in ("devices/plc-1.yaml", "tags/plc-1.yaml", "alarms/line1.yaml"):
        assert (dest / rel).read_bytes() == (bundles.FIXTURE / rel).read_bytes()
    assert load_project(dest).ok
    # 다시 만들면 이전 사본(absorb 결과 등)을 지우고 새로 쓴다
    (dest / "project.yaml").write_text("broken", "utf-8")
    assert load_project(bundles.unabsorbed_copy(bundles.FIXTURE, dest)).ok


def test_site_api_session_cookie() -> None:
    cookie = "pureiron_session=tok123; HttpOnly; Max-Age=28800; Path=/api; SameSite=strict"
    assert site_api.session_of({"set-cookie": cookie}) == "tok123"
    assert site_api.session_of({}) is None
    # 비안전 메서드의 브라우저 헤더: Origin = web(요청 Host와 같은 host:port), same-origin
    assert site_api.BROWSER_HEADERS["Origin"] == site_api.WEB
    assert site_api.BROWSER_HEADERS["Sec-Fetch-Site"] == "same-origin"
