"""e2e 하네스의 순수 로직(VM·Hyper-V 없이). 실제 VM 실행은 `tests/e2e/m2d/run.py`."""

from __future__ import annotations

import dataclasses
import io
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import cast

import crash_at
import probe
import pycdlib
import pytest
import run
import site_api
from harness import bundles, context, golden, hyperv, judges, paths, site, trees, vm
from harness.context import Context
from harness.release_pair import DEFAULT as PAIR

from pureiron_project import canonical
from pureiron_project.project import load_project

BASE, NEXT, FAULT = PAIR.base, PAIR.next, PAIR.fault
RELEASES, EPOCHS = PAIR.releases, PAIR.epochs
NEXT_TABLES, NEXT_INDEXES = PAIR.next_tables, PAIR.next_indexes
TREE_TAG, TREE_DESCRIPTIONS = PAIR.tree_tag, PAIR.tree_descriptions
SITE = run.SITE_A.site_id


def tmp_ctx(tmp_path: Path) -> Context:
    """run.py의 site-a 문맥 — 상태 파일만 `tmp_path`로(실제 state.json 보호)."""
    site_a = dataclasses.replace(run.SITE_A, state_file=tmp_path / "state.json")
    return Context(run_id="t", artifacts=str(tmp_path), site=site_a, pair=PAIR)


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


AUDIT_FWD_WITHOUT_FIELD = """\
    chain audit_fwd {
        type filter hook forward priority -10; policy accept;
        oifname "eth0" counter log prefix "e2e-ext_fwd " comment "ext_fwd"
    }
"""
"""r4 이전(현장 장비 끝점 없음)의 `audit_fwd` 체인 — 끝점이 없으면 출력이 이와 같아야 한다."""


def fwd_chain(text: str) -> list[str]:
    return text.split("chain audit_fwd {", 1)[1].split("    }", 1)[0].strip().splitlines()


def test_nft_audit_without_field_is_unchanged() -> None:
    text = golden.nft_audit("10.77.0.0/24")
    assert AUDIT_FWD_WITHOUT_FIELD in text
    assert golden.FIELD_RULE not in text
    assert golden.nft_audit("10.77.0.0/24", ()) == text


def test_nft_audit_field_rule_precedes_ext_fwd() -> None:
    """r4 실측: collector의 선언 장비(10.0.0.5:502) 폴링이 ext_fwd 216으로 잡혔다."""
    text = golden.nft_audit("10.77.0.0/24", (("10.0.0.5", 502), ("10.0.0.6", 5020)))
    rules = [line.strip() for line in fwd_chain(text)[1:]]
    assert rules == [
        'oifname "eth0" ip daddr . tcp dport { 10.0.0.5 . 502, 10.0.0.6 . 5020 } '
        'counter accept comment "field_fwd"',
        'oifname "eth0" counter log prefix "e2e-ext_fwd " comment "ext_fwd"',
    ]
    # 나가는 쪽(audit_out)은 끝점과 무관하다.
    plain = golden.nft_audit("10.77.0.0/24")
    assert text.split("chain audit_fwd", 1)[0] == plain.split("chain audit_fwd", 1)[0]


def test_seed_files_carry_field_rule() -> None:
    field = (("10.0.0.5", 502),)
    user_data = golden.seed_files("site-a", "10.77.0.10", "k", golden.Network(), field)
    text = user_data["user-data"].decode()
    assert '      oifname "eth0" ip daddr . tcp dport { 10.0.0.5 . 502 } counter accept' in text
    assert text.index('comment "field_fwd"') < text.index('comment "ext_fwd"')


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
    assert judges.running_digests(SNAP) == {"core": "sha256:c", "web": "sha256:w"}


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
    assert judges.edit_unlocked({**SNAP, "project": project}) is ok
    assert judges.ready(SNAP)


WANT = {"core": "sha256:c", "web": "sha256:w"}
DONE: dict[str, object] = {
    **SNAP,
    "updater": {"last_result": "failed", "alarm": None, "applied_gen": 3, "attempt": None},
}


def test_judge_attempt_passes_clean_rollback() -> None:
    checks = judges.judge_attempt(
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
    checks = judges.judge_attempt(snap, report, expect="failed", want_digests=WANT, want_applied=3)
    assert not checks[failing]


def test_nft_accumulate_survives_reset_and_restore() -> None:
    total = judges.nft_accumulate({"dns_out": 0}, {"dns_out": 2}, {"dns_out": 5})
    assert total == {"dns_out": 3}
    # 표시 없이 재부팅(카운터 0부터) — 줄어든 값은 새로 센 것으로 더한다
    assert judges.nft_accumulate(total, {"dns_out": 5}, {"dns_out": 1}) == {"dns_out": 4}
    # 리셋 직후 기준 {} → 처음 보는 키는 0부터
    assert judges.nft_accumulate(total, {}, {"ext_out_v4": 2}) == {"dns_out": 3, "ext_out_v4": 2}
    # 현장 장비 폴링(field_fwd)도 같이 모은다 — 판정에서만 뺀다
    assert judges.nft_accumulate({}, {"field_fwd": 3}, {"field_fwd": 10}) == {"field_fwd": 7}


QUIET_COUNTERS = {"ext_out_v4": 0, "dns_out": 0, "ext_out_v6": 0, "ext_fwd": 0}


@pytest.mark.parametrize(
    ("counters", "quiet"),
    [
        (QUIET_COUNTERS, True),
        ({**QUIET_COUNTERS, "field_fwd": 217}, True),  # r4 VM 2차 실측 — 선언 장비 폴링뿐
        ({**QUIET_COUNTERS, "ext_fwd": 1, "field_fwd": 216}, False),
        ({**QUIET_COUNTERS, "dns_out": 2, "field_fwd": 0}, False),
        ({**QUIET_COUNTERS, "ext_out_v6": 1}, False),
    ],
)
def test_no_external_ignores_only_field_fwd(counters: dict[str, int], quiet: bool) -> None:
    """ITEM_OS_QUIET(기준값)·ITEM_NO_EXTERNAL(누적)이 같은 판정을 쓴다."""
    assert judges.no_external(counters) is quiet


@pytest.mark.parametrize("missing", ["ext_out_v4", "dns_out", "ext_out_v6", "ext_fwd"])
def test_no_external_requires_every_audit_counter(missing: str) -> None:
    """nft 출력을 못 읽어 카운터가 비거나 빠지면 '외부 접속 없음'이 거짓 통과하지 않는다."""
    assert judges.no_external({}) is False
    partial = {name: 0 for name in QUIET_COUNTERS if name != missing}
    assert judges.no_external({**partial, "field_fwd": 0}) is False


def test_field_endpoints_read_fixture(tmp_path: Path) -> None:
    assert trees.FIXTURE == bundles.FIXTURE
    assert trees.field_endpoints(trees.FIXTURE) == (("10.0.0.5", 502),)
    # m5 사본(absorbs만 뺀 것)도 장비가 같다
    m5 = bundles.unabsorbed_copy(bundles.FIXTURE, tmp_path / "m5")
    assert trees.field_endpoints(m5) == (("10.0.0.5", 502),)


def test_field_endpoints_sorted_and_reject_hostname(tmp_path: Path) -> None:
    tree = tmp_path / "acme"
    shutil.copytree(trees.FIXTURE, tree)
    (tree / "devices" / "plc-0.yaml").write_text(
        "host: 10.0.0.4\nid: plc-0\nport: 5020\nprotocol: modbus_tcp\n", "utf-8"
    )
    assert trees.field_endpoints(tree) == (("10.0.0.4", 5020), ("10.0.0.5", 502))
    plc = tree / "devices" / "plc-1.yaml"
    plc.write_text(plc.read_text("utf-8").replace("10.0.0.5", "plc-1.local"), "utf-8")
    with pytest.raises(run.E2EError, match="not an IPv4 address"):
        trees.field_endpoints(tree)


@pytest.mark.parametrize(("value", "want"), [("7141", 7141), (12, 12), ({"error": "x"}, -1)])
def test_as_int(value: object, want: int) -> None:
    assert judges.as_int(value) == want


def test_dns_names_counts_lookups_except_local_host() -> None:
    log = (
        "Looking up RR for site-a IN A.\n"
        "Looking up RR for changelogs.ubuntu.com IN AAAA.\n"
        "Looking up RR for changelogs.ubuntu.com IN A.\n"
        "Cache miss for x IN A\n"
    )
    assert judges.dns_names(log, SITE) == {"changelogs.ubuntu.com": 2}


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


def test_release_pair_epoch_step_and_derived_fault() -> None:
    # NEXT는 BASE와 같은 epoch(마이그레이션 없음, M4 마감 r4→r5)이거나 한 단계 위다. 같으면 NEXT
    # 산출물 집합이 비어 있어야 한다(늘 참인 판정을 남기지 않는다).
    assert EPOCHS[NEXT] in (EPOCHS[BASE], EPOCHS[BASE] + 1)
    if EPOCHS[NEXT] == EPOCHS[BASE]:
        assert frozenset() == NEXT_TABLES == NEXT_INDEXES
    assert RELEASES[FAULT].startswith(RELEASES[NEXT] + "-")
    assert EPOCHS[FAULT] == EPOCHS[NEXT]
    assert bundles.fault_source(PAIR) == (FAULT, RELEASES[FAULT], NEXT)
    assert RELEASES[BASE] == run.INSTALL_RELEASE


@pytest.mark.parametrize("name", [BASE, NEXT])
def test_release_pair_matches_repo_records(name: str) -> None:
    """저장소 release 기록이 쌍의 달력 버전·epoch와 같다(prepare가 VM 전에 같은 대조를 한다).
    쌍을 바꾸는 커밋은 NEXT 기록(release_record.py)을 먼저 커밋해야 한다 — 없으면 실패."""
    path = RELEASE_RECORDS / f"{name}.json"
    judges.check_record(PAIR, name, json.loads(path.read_text(encoding="utf-8")))


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
        judges.check_record(PAIR, NEXT, record)


def test_crash_plan_covers_every_phase_and_mode_once() -> None:
    plan = run.crash_plan(PAIR)
    assert len(plan) == 2 * len(run.CRASH_PHASES)
    pairs = {(c.phase, c.mode) for c in plan}
    assert pairs == {(p, m) for p in run.CRASH_PHASES for m in ("kill", "reset")}
    assert len({c.name for c in plan}) == len(plan)


def test_crash_plan_releases() -> None:
    plan = run.crash_plan(PAIR)
    # migrating: c0(BASE epoch)에서 BASE→NEXT — 마이그레이션이 있는 쌍이면 실제로 일어나는 유일한
    # 경로(M4 마감 r4→r5는 같은 epoch라 phase만 지나간다 — Updater는 migrate_to가 없어도
    # `migrating`을 기록한다)
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


def test_release_pair_is_r4_to_r5() -> None:
    """M4 마감 쌍 — 두 기록(r4·r5)이 모두 저장소에 있어야 한다
    (`test_release_pair_matches_repo_records`). 같은 epoch 5."""
    assert (BASE, NEXT, FAULT) == ("r4", "r5", "r5f")
    assert (EPOCHS[BASE], EPOCHS[NEXT], EPOCHS[FAULT]) == (5, 5, 5)
    assert RELEASES == {"r4": "2026.11.0", "r5": "2026.11.1", "r5f": "2026.11.1-fault1"}


def test_expected_items_are_thirty_nine_and_follow_pair() -> None:
    items = run.expected_items()
    assert len(items) == len(set(items)) == 39
    assert items[2] == "install bundle bootstrap + g1(r4) on offline VM"
    assert items[6:16] == [
        "r4→r5 on compressed epoch-5 DB",
        "r5 schema after upgrade",
        "reject lower gen",
        "r5 project tree applied (project_files, definitions, base_site_rev)",
        "site PATCH via web (admin login, tag + alarm) → site_rev +2",
        "reject unabsorbed site drafts (E_DRAFT)",
        "draft export → absorb → gate → reapply (PATCH kept, base_site_rev == site_rev)",
        "r5' health failure → rollback (no alarm, r4 tree and definitions back)",
        "rollback within 5 min to r4 digests",
        "r5 retry after r5' rollback → succeeded",
    ]
    assert items[16] == "crash kill @ migrating"
    # 온라인 4개는 crash 뒤, 외부 접속(report) 앞 — report가 online 구간까지 누적한다
    assert items[34:38] == [
        "online: HTTPS heartbeat to central c0 (private CA trusted) → /fleet online",
        "online: central-published r4 g2 → download + apply succeeded",
        "online: ApplyAttempt report → /fleet current + last_attempt (source online)",
        "online: central web down → site keeps running, /fleet stale → back online",
    ]
    assert items[-1] == run.ITEM_NO_EXTERNAL
    # S4의 30개 중 쌍과 무관한 항목·crash 18개는 이름 그대로다(실측끼리 대응표로 비교)
    assert len([i for i in items if i.startswith("crash ")]) == 18


def test_steps_put_online_between_crash_and_report() -> None:
    assert run.STEPS == ("prepare", "vm", "install", "scenarios", "crash", "online", "report")


def test_scenario_gens_keep_bundle_names_apart() -> None:
    gens = run.scenario_gens(2)
    # NEXT 줄(업그레이드 → ⓒ → ⓓ)은 차례로 올라가야 한다(낮은 gen은 E_GEN)
    assert gens["upgrade"] < gens["draft_gate"] < gens["absorb"]
    # c0 복원 뒤 줄: applied g1 위 FAULT → 재시도
    assert 1 < gens["fault"] < gens["retry"]
    # 번들 파일 이름 `g<gen>-<release>`가 겹치지 않는다
    named = [
        (gens["upgrade"], NEXT),
        (gens["draft_gate"], NEXT),
        (gens["absorb"], NEXT),
        (gens["fault"], FAULT),
        (gens["retry"], NEXT),
    ]
    assert len(set(named)) == len(named)
    assert gens["next"] > max(v for k, v in gens.items() if k != "next")


def test_online_gen_follows_c0_and_leaves_gen3_for_the_measured_pr() -> None:
    # c0의 applied gen은 설치 g1 — 온라인 ②는 그다음. 15분 실측 PR은 site-a gen 2 → 3.
    assert judges.ONLINE_GEN == 2
    assert run.OUTAGE_WAIT_S > 3 * site.CENTRAL_POLL_S


# M4 마감 수정 r1 실측(`fixtures/m4c-tree-db.json`, 로컬 site-db 2.30.2-pg16 + core r4·r5 CLI,
# Updater 순서 — 파일의 `measured`): c0(fixture g1 + 시드) → 같은 트리 재적용(no-op, H1 재현)·NEXT
# 트리 업그레이드·FAULT 트리 적용 → r4 백업 복원(롤백) → NEXT 트리 재시도. probe `db`에서 판정에
# 쓰는 키(장비·태그·알람 목록, 정의 JSON 전체)만 남겼다.
TREE_MEASURED = cast(
    dict[str, object],
    json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "m4c-tree-db.json").read_text("utf-8")
    ),
)
TREE_STAGES = cast(dict[str, dict[str, object]], TREE_MEASURED["stages"])
FIXTURE_TAG_DEFS = TREE_STAGES["c0"]["tag_defs"]
"""fixture 트리를 적용한 DB의 태그 정의 JSON 전체(같은 fixture라 M5 실측 R4_TREE_DB와 같은 값)."""

# M5 마감 실측(로컬 site-db 2.30.2-pg16, compose pureiron-it-m5c, Updater 순서): r3 core
# `migrate up`(0001~0012) → r3 `project apply`(메타만) → 시드(site.SEED_SQL) → R3_C0_DB(epoch 4
# c0) / r4 `migrate up`(0013) → r4 `project apply --tar`(fixture 트리) → R4_TREE_DB. `columns`는
# 판정에 쓰는 열만 남겼다. M4 마감에서는 트리·PATCH 판정 시험과 "지난 쌍의 c0는 지금 c0가
# 아니다" 교차 확인에 쓴다. R4_TREE_DB `tag_defs`는 M5 기록이 `line1/temp`만 남겼던 것을 같은
# fixture 트리의 전체 실측값(FIXTURE_TAG_DEFS)으로 채웠다 — `judge_tree`가 `TREE_TAG` 정의를 본다.
R3_C0_DB: dict[str, object] = {
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
R4_TREE_DB: dict[str, object] = {
    **R3_C0_DB,
    "epoch": "5",
    "tag_history_rows": "7146",
    "tables": "alarm_journal,alarms,audit_log,devices,project_files,project_state,schema_meta,"
    "schema_migrations,sessions,tag_history,tags,users",
    "indexes": "alarm_journal_alarm_id_time_idx,alarm_journal_pkey,alarm_journal_time_idx,"
    "alarms_pkey,audit_log_pkey,audit_log_time_idx,audit_log_username_time_idx,devices_pkey,"
    "project_files_pkey,project_state_pkey,schema_meta_pkey,schema_migrations_pkey,"
    "sessions_pkey,sessions_user_id_idx,tag_history_tag_id_time_key,tag_history_time_idx,"
    "tags_path_key,tags_pkey,users_pkey,users_username_key",
    "columns": str(R3_C0_DB["columns"]) + ",project_files.content,project_files.path",
    "project_files": "4",
    "device_ids": "plc-1",
    "tag_paths": "line1/remote_ok,line1/running,line1/start,line1/temp,line1/temp_sp",
    "alarm_ids": "temp-high,temp-low",
    "tag_defs": FIXTURE_TAG_DEFS,
    "alarm_defs": '{ "temp-high" : {"id": "temp-high", "tag": "line1/temp", "type": "above", '
    '"priority": "high", "setpoint": 80}, "temp-low" : {"id": "temp-low", "tag": "line1/temp", '
    '"type": "below", "deadband": 1.5, "setpoint": 5, "on_delay_s": 10} }',
}


# M4 마감 실측(2026-11-09, WSL 로컬 site-db 2.30.2-pg16 컨테이너, Updater 설치·업그레이드 순서):
# r4 core `migrate up`(0001~0013) → r4 `project apply --tar`(fixture acme 트리, g1) → 시드
# (site.SEED_SQL) → probe `db` = C0_DB / r5 core `migrate up`(applied 0 — 같은 epoch) → r5
# `project apply --tar`(같은 트리, changed false) → probe = NEXT_DB. **두 값은 바이트까지 같다**
# (F1: r4..r5 마이그레이션 없음, 같은 트리) — 그래서 c0·NEXT 스키마 판정은 서로 바꿔 넣어도
# 통과하는 것이 맞고, 교차 실패는 지난 쌍의 c0(R3_C0_DB, epoch 4)로 확인한다. `columns`는 판정에
# 쓰는 열만, 정의 JSON(`tag_defs`·`alarm_defs`)은 스키마 판정이 보지 않아 뺐다. 같은 트리
# 재적용이 no-op이라는 이 사실이 리뷰 H1이다 — #10 트리 판정은 NEXT_DB에서 실패해야 하고, 그래서
# 업그레이드 번들은 다른 트리(NEXT 트리)를 싣는다(`test_tree_item_fails_on_same_tree_upgrade_db`).
C0_DB: dict[str, object] = {
    "epoch": "5",
    "caggs": "tag_history_1h,tag_history_1m",
    "compressed_chunks": "3",
    "tag_history_rows": "7141",
    "realtime_caggs": "tag_history_1h,tag_history_1m",
    "tables": "alarm_journal,alarms,audit_log,devices,project_files,project_state,schema_meta,"
    "schema_migrations,sessions,tag_history,tags,users",
    "indexes": "alarm_journal_alarm_id_time_idx,alarm_journal_pkey,alarm_journal_time_idx,"
    "alarms_pkey,audit_log_pkey,audit_log_time_idx,audit_log_username_time_idx,devices_pkey,"
    "project_files_pkey,project_state_pkey,schema_meta_pkey,schema_migrations_pkey,"
    "sessions_pkey,sessions_user_id_idx,tag_history_tag_id_time_key,tag_history_time_idx,"
    "tags_path_key,tags_pkey,users_pkey,users_username_key",
    "columns": "alarms.definition,devices.definition,devices.id,project_files.content,"
    "project_files.path,project_state.definitions_rev,project_state.historian,project_state.id,"
    "tags.definition,tags.id",
    "project_files": "4",
    "device_ids": "e2e-dev,plc-1",
    "tag_paths": "e2e/tag,line1/remote_ok,line1/running,line1/start,line1/temp,line1/temp_sp",
    "alarm_ids": "temp-high,temp-low",
}
NEXT_DB: dict[str, object] = dict(C0_DB)


def test_judge_c0() -> None:
    assert all(judges.judge_c0(C0_DB, PAIR).values())
    assert not judges.judge_c0({**C0_DB, "epoch": "4"}, PAIR)["epoch"]
    assert not judges.judge_c0({**C0_DB, "compressed_chunks": "0"}, PAIR)["compressed_chunks"]
    assert not judges.judge_c0({**C0_DB, "tag_history_rows": "0"}, PAIR)["seed_rows"]
    # BASE(epoch 5) 산출물이 빠졌으면 c0가 아니다 — 지난 쌍의 c0(epoch 4, project_files 없음)도
    failing = {k for k, ok in judges.judge_c0(R3_C0_DB, PAIR).items() if not ok}
    assert failing == {"epoch", "base_schema"}
    no_files = str(C0_DB["tables"]).replace("project_files,", "")
    assert not judges.judge_c0({**C0_DB, "tables": no_files}, PAIR)["base_schema"]
    assert not judges.judge_c0({**C0_DB, "realtime_caggs": ""}, PAIR)["base_schema"]
    assert not judges.judge_c0({**C0_DB, "columns": "devices.id"}, PAIR)["base_schema"]
    no_index = str(C0_DB["indexes"]).replace("sessions_user_id_idx,", "")
    assert not judges.judge_c0({**C0_DB, "indexes": no_index}, PAIR)["base_schema"]
    # NEXT 산출물이 없으므로(같은 epoch) next_absent는 판정에서 뺐다
    assert "next_absent" not in judges.judge_c0(C0_DB, PAIR)
    assert not any(judges.judge_c0({"error": "site-db not running"}, PAIR).values())


def test_judge_next_schema() -> None:
    assert all(judges.judge_next_schema(NEXT_DB, PAIR).values())
    # 같은 epoch라 c0와 NEXT는 실측값이 같다 — 서로 바꿔 넣어도 통과한다(정상). 교차 실패는 지난
    # 쌍의 c0: epoch 4이고 0013 산출물(project_files)이 없다
    assert NEXT_DB == C0_DB
    failing = {k for k, ok in judges.judge_next_schema(R3_C0_DB, PAIR).items() if not ok}
    assert failing == {"epoch", "base_tables"}
    partial = {**NEXT_DB, "realtime_caggs": "tag_history_1m"}
    assert not judges.judge_next_schema(partial, PAIR)["realtime_caggs"]
    # 집합 비교: 이름의 부분 문자열로는 통과하지 않는다
    lookalike = {**NEXT_DB, "caggs": "tag_history_1h_old,tag_history_1m"}
    assert not judges.judge_next_schema(lookalike, PAIR)["caggs"]
    lookalike_table = {
        **NEXT_DB,
        "tables": str(NEXT_DB["tables"]).replace("project_files,", "project_files_old,"),
    }
    assert not judges.judge_next_schema(lookalike_table, PAIR)["base_tables"]
    # BASE(epoch 5) 산출물이 업그레이드 뒤 사라지면 걸린다
    no_index = {**NEXT_DB, "indexes": str(NEXT_DB["indexes"]).replace(",sessions_user_id_idx", "")}
    assert not judges.judge_next_schema(no_index, PAIR)["base_indexes"]
    no_users = {**NEXT_DB, "tables": str(NEXT_DB["tables"]).replace(",users", "")}
    assert not judges.judge_next_schema(no_users, PAIR)["base_tables"]
    assert not judges.judge_next_schema({**NEXT_DB, "columns": {"error": "x"}}, PAIR)[
        "base_columns"
    ]
    assert not judges.judge_next_schema({**NEXT_DB, "compressed_chunks": "0"}, PAIR)[
        "compressed_chunks"
    ]


def test_next_artifacts_empty_until_epoch_moves() -> None:
    """r5는 epoch 5 그대로 — NEXT 산출물 집합이 비어 있고, 빈 집합 검사(늘 참)는 판정에 없다.
    다음 쌍이 epoch를 올리면 집합을 채우고 `next_absent`·`next_tables`를 되살린다."""
    assert frozenset() == NEXT_TABLES == NEXT_INDEXES
    assert {"next_tables", "next_indexes"}.isdisjoint(judges.judge_next_schema(NEXT_DB, PAIR))
    assert "next_absent" not in judges.judge_c0(C0_DB, PAIR)


def test_names_splits_probe_lists() -> None:
    assert judges.names({"k": "a,b"}, "k") == {"a", "b"}
    assert judges.names({"k": ""}, "k") == frozenset()
    assert judges.names({"k": {"error": "x"}}, "k") == frozenset()
    assert judges.names({}, "k") == frozenset()


def test_tree_measurements_agree_with_earlier_c0() -> None:
    """두 실측(D4 C0_DB·수정 r1 TREE_STAGES)의 c0가 같은 값이고, 같은 트리 재적용은 DB를 한 글자도
    바꾸지 않는다(core `changed=False`) — H1의 원인. 다른 트리는 모두 `changed=True`."""
    c0 = TREE_STAGES["c0"]
    assert {k: C0_DB[k] for k in c0 if k in C0_DB} == {k: v for k, v in c0.items() if k in C0_DB}
    assert TREE_STAGES["next_same_tree"] == c0
    changed = cast(dict[str, bool], TREE_MEASURED["apply_changed"])
    assert changed == {
        "next_same_tree": False,
        "next_tree": True,
        "fault_applied": True,
        "fault_rolled_back": True,
        "retry": True,
    }


def release_expect(tmp_path: Path, release: str) -> judges.TreeExpect:
    return trees.tree_expect(bundles.release_tree(tmp_path / release, release, PAIR), PAIR)


def tree_snap(want: judges.TreeExpect, db: dict[str, object]) -> dict[str, object]:
    """`want` 트리가 적용된 `GET /project` 본문(드래프트 없음) + 실측 DB."""
    body = {**PROJECT_BODY, "content_hash": want.content_hash}
    return {**SNAP, "project": {"status": 200, "body": body}, "db": db}


def failing_keys(checks: dict[str, bool]) -> set[str]:
    return {k for k, ok in checks.items() if not ok}


def test_release_trees_differ_only_in_the_tree_tag_description(tmp_path: Path) -> None:
    """NEXT·FAULT 트리 = fixture에서 `TREE_TAG` description만 바꾼 것 — 파일·장비·태그·알람 집합은
    같고 content_hash와 정의 값만 다르다. 실측 파일의 hash와도 같다(같은 함수로 만든 트리)."""
    hashes = cast(dict[str, str], TREE_MEASURED["content_hashes"])
    assert TREE.content_hash == hashes["fixture"]
    assert TREE.tree_tag_description == "1라인 온도 설정값"
    assert TREE_TAG not in (judges.PATCH_TAG, judges.PATCH_ALARM)
    for release, key in ((NEXT, "next"), (FAULT, "fault")):
        want = release_expect(tmp_path, release)
        assert want.content_hash == hashes[key]
        assert want.tree_tag_description == TREE_DESCRIPTIONS[release]
        assert (want.files, want.devices, want.tags, want.alarms) == (
            TREE.files,
            TREE.devices,
            TREE.tags,
            TREE.alarms,
        )
        changed = sorted(
            p.relative_to(tmp_path / release).as_posix()
            for p in (tmp_path / release).rglob("*.yaml")
            if p.read_bytes() != (bundles.FIXTURE / p.relative_to(tmp_path / release)).read_bytes()
        )
        assert changed == ["tags/plc-1.yaml"]
    assert (
        len({TREE.content_hash, *(release_expect(tmp_path, r).content_hash for r in (NEXT, FAULT))})
        == 3
    )


def test_tree_item_fails_on_same_tree_upgrade_db(tmp_path: Path) -> None:
    """H1: 업그레이드가 c0와 같은 트리를 실으면 core가 no-op이라 DB는 c0 그대로다(실측 NEXT_DB·
    `next_same_tree`, 시드 장비·태그 남음, 정의 = fixture). #10은 이 DB에서 실패해야 한다 — 같은
    epoch에서 늘 참인 항목이 아니다."""
    want = release_expect(tmp_path, NEXT)
    for db in (NEXT_DB, TREE_STAGES["next_same_tree"]):
        assert failing_keys(judges.judge_tree(tree_snap(want, db), want)) == {
            "devices",
            "tags",
            "seed_retired",
            "definition",
        }
    # 예전 판정(fixture 기대값)으로 봐도 c0 DB는 시드 때문에 실패한다 — VM에서 #10이 깨질 자리
    assert failing_keys(trees.judge_fixture_tree(tree_snap(TREE, NEXT_DB), PAIR)) >= {
        "devices",
        "tags",
    }


def test_tree_item_passes_on_next_tree_upgrade_db(tmp_path: Path) -> None:
    """NEXT 트리를 실은 업그레이드(실측 `next_tree`): 시드 retire, 정의 = NEXT 트리 값 → #10 통과.
    다른 트리 기대값(fixture·FAULT)과 바꿔 넣으면 실패한다."""
    want = release_expect(tmp_path, NEXT)
    upgraded = TREE_STAGES["next_tree"]
    assert all(judges.judge_tree(tree_snap(want, upgraded), want).values())
    # 본문은 NEXT인데 DB 정의가 fixture(정의 미적용) → definition만
    assert failing_keys(
        judges.judge_tree(tree_snap(want, TREE_STAGES["fault_rolled_back"]), want)
    ) == {"definition"}
    # fixture 기대값으로 보면 content_hash·정의 둘 다 다르다
    assert failing_keys(judges.judge_tree(tree_snap(want, upgraded), TREE)) == {
        "project_version",
        "definition",
    }
    fault = release_expect(tmp_path, FAULT)
    assert failing_keys(judges.judge_tree(tree_snap(want, upgraded), fault)) == {
        "project_version",
        "definition",
    }
    # 정의 JSON을 못 읽었거나 태그가 없으면 실패
    no_defs = {**upgraded, "tag_defs": {"error": "x"}}
    assert failing_keys(judges.judge_tree(tree_snap(want, no_defs), want)) == {"definition"}
    # 시드가 남았으면(트리 밖 장비만 남은 경우도) seed_retired
    seeded = {**upgraded, "device_ids": "e2e-dev,plc-1"}
    assert {"devices", "seed_retired"} <= failing_keys(
        judges.judge_tree(tree_snap(want, seeded), want)
    )


def test_fault_rollback_restores_the_c0_definitions(tmp_path: Path) -> None:
    """M1 #14: FAULT 트리 적용 → r4 백업 복원(실측 `fault_rolled_back`)이면 content_hash·정의 값이
    fixture(c0)로 돌아온다. 복원이 hash·state만 되돌리고 정의를 남기면(실측 `fault_applied` DB +
    fixture 본문) `definition`이, 트리도 못 되돌리면 `project_version`도 실패한다."""
    fault = release_expect(tmp_path, FAULT)
    rolled = TREE_STAGES["fault_rolled_back"]
    assert all(trees.judge_fixture_tree(tree_snap(TREE, rolled), PAIR).values())
    left = TREE_STAGES["fault_applied"]
    assert failing_keys(trees.judge_fixture_tree(tree_snap(TREE, left), PAIR)) == {"definition"}
    assert failing_keys(trees.judge_fixture_tree(tree_snap(fault, left), PAIR)) == {
        "project_version",
        "definition",
    }


def test_retry_writes_the_next_definitions(tmp_path: Path) -> None:
    """M1 #16: 롤백(fixture) 위 NEXT 트리 재시도(실측 `retry`) → 정의 = NEXT 값. 롤백 상태 그대로
    (재시도가 트리를 쓰지 않음)면 실패한다 — fixture를 실었다면 늘 참이었을 항목."""
    want = release_expect(tmp_path, NEXT)
    assert all(judges.judge_tree(tree_snap(want, TREE_STAGES["retry"]), want).values())
    assert TREE_STAGES["retry"] == TREE_STAGES["next_tree"]
    stuck = tree_snap(TREE, TREE_STAGES["fault_rolled_back"])
    assert failing_keys(judges.judge_tree(stuck, want)) == {"project_version", "definition"}


class FakeApply:
    """`apply_and_check` 대신 호출을 기록한다. 결과는 FAULT 판정에 쓰는 키만."""

    def __init__(self, snapshot: dict[str, object] | None = None) -> None:
        self.calls: dict[str, dict[str, object]] = {}
        self.snapshot = snapshot or {}

    def __call__(
        self, ctx: Context, name: str, gen: int, release: str, **kw: object
    ) -> dict[str, object]:
        self.calls[name] = {"gen": gen, "release": release, **kw}
        return {
            "images_match": True,
            "seconds": 120.0,
            "code": "E_HEALTH",
            "snapshot": self.snapshot,
        }


def wiring_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Context:
    monkeypatch.setattr(paths, "BUNDLES", tmp_path / "bundles")
    return tmp_ctx(tmp_path)


def test_upgrade_carries_the_next_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H1 배선: 업그레이드 번들은 NEXT 트리(이 run의 `trees/r5`)를 싣는다 — 빠지면(fixture) #10이
    VM에서 c0 DB를 본다."""
    ctx = wiring_ctx(tmp_path, monkeypatch)
    fake = FakeApply({"db": {**NEXT_DB, **TREE_STAGES["next_tree"]}})
    run.upgrade_next(ctx, 2, apply=fake)
    call = fake.calls[run.ITEM_UPGRADE]
    tree = tmp_path / "bundles" / "t" / "trees" / NEXT
    assert call["project_dir"] == golden.wsl_path(tree)
    assert (call["release"], call["expect"], call["prev"]) == (NEXT, "succeeded", BASE)
    assert trees.tree_expect(tree, PAIR).tree_tag_description == TREE_DESCRIPTIONS[NEXT]
    assert {r["name"]: r["ok"] for r in ctx.results} == {run.ITEM_SCHEMA: True}


def test_fault_and_retry_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """M1 배선: FAULT 번들은 FAULT 트리, 판정 `extra`는 fixture 트리·정의 복원(롤백 실측에서 통과,
    정의가 남은 DB에서 실패). 재시도는 NEXT 트리, `extra`는 NEXT 트리·정의(재시도 실측에서 통과,
    롤백 상태 그대로면 실패). 변이 M20(FAULT를 fixture 트리로)·M25·M21(`extra` 제거)을 잡는다."""
    ctx = wiring_ctx(tmp_path, monkeypatch)
    fake = FakeApply()
    gens = run.scenario_gens(2)
    run.fault_and_retry(ctx, gens, apply=fake)
    tree_dir = tmp_path / "bundles" / "t" / "trees"

    fault = fake.calls[run.ITEM_FAULT]
    assert (fault["gen"], fault["release"], fault["expect"]) == (gens["fault"], FAULT, "failed")
    assert fault["project_dir"] == golden.wsl_path(tree_dir / FAULT)
    assert (
        trees.tree_expect(tree_dir / FAULT, PAIR).tree_tag_description == TREE_DESCRIPTIONS[FAULT]
    )
    assert fault["expect_code"] == judges.FAULT_CODE
    assert fault["check_metric"] is True
    fault_extra = cast(Callable[[dict[str, object]], dict[str, bool]], fault["extra"])
    assert all(fault_extra(tree_snap(TREE, TREE_STAGES["fault_rolled_back"])).values())
    assert not all(fault_extra(tree_snap(TREE, TREE_STAGES["fault_applied"])).values())

    retry = fake.calls[run.ITEM_RETRY]
    assert (retry["gen"], retry["release"], retry["expect"]) == (gens["retry"], NEXT, "succeeded")
    assert retry["project_dir"] == golden.wsl_path(tree_dir / NEXT)
    assert retry["check_metric"] is True
    retry_extra = cast(Callable[[dict[str, object]], dict[str, bool]], retry["extra"])
    next_want = trees.tree_expect(tree_dir / NEXT, PAIR)
    assert all(retry_extra(tree_snap(next_want, TREE_STAGES["retry"])).values())
    assert not all(retry_extra(tree_snap(TREE, TREE_STAGES["fault_rolled_back"])).values())

    by_name = {r["name"]: r for r in ctx.results}
    assert by_name[run.ITEM_ROLLBACK_TIME]["ok"] is True


OPEN_AT = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)


def completed(rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess[str]([], rc, out, err)


def test_stage_for_window_records_each_try() -> None:
    results = iter([completed(2), completed(0, "attempt: g3 verified")])
    ok, info = site.stage_for_window(
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
    ok, info = site.stage_for_window(
        lambda: completed(rc, "o" * 2000, "Connection reset"),
        OPEN_AT,
        watcher_alive=lambda: False,
        now=lambda: OPEN_AT - timedelta(seconds=margin),
        pause_s=0,
    )
    assert not ok
    assert (info["rc"], info["tries"], info["margin_s"]) == (rc, 1, margin)
    assert info["stderr_tail"] == "Connection reset"
    assert len(str(info["stdout_tail"])) == context.TAIL_CHARS


def test_stage_for_window_gives_up_after_busy_tries() -> None:
    ok, info = site.stage_for_window(
        lambda: completed(2, "", "error: another updater is running"),
        OPEN_AT,
        watcher_alive=lambda: True,
        now=lambda: OPEN_AT - timedelta(seconds=10),
        pause_s=0,
    )
    assert not ok
    assert (info["rc"], info["tries"]) == (2, site.STAGE_TRIES)


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
    report: dict[str, object] = {"failed_manual": False, "logs": LEFT_LOGS, "code": "E_HEALTH"}
    checks = judges.judge_attempt(
        TRANSITION,
        report,
        expect="failed",
        want_digests=WANT,
        want_applied=3,
        expect_alarm=judges.E_DEFINITIONS_LEFT,
        expect_code=judges.FAULT_CODE,
        check_metric=True,
    )
    assert all(checks.values())
    assert "no_manual_alarm" not in checks
    assert checks["code"]
    # S4 기본 판정(경보 없음 기대)이면 같은 상태가 실패한다 — c0(r3)에서 r4f는 설계상 경보를 세운다
    default = judges.judge_attempt(
        TRANSITION, report, expect="failed", want_digests=WANT, want_applied=3
    )
    assert not default["no_manual_alarm"]
    assert "alarm_metric" not in default
    assert "code" not in default  # expect_code가 없으면 보고 code를 판정하지 않는다


FAULT_REPORT: dict[str, object] = {"failed_manual": False, "logs": LEFT_LOGS, "code": "E_HEALTH"}


@pytest.mark.parametrize(
    ("alarm", "report", "metric", "failing"),
    [
        (None, FAULT_REPORT, 0.0, "expected_alarm"),
        ("g2: E_HEALTH", FAULT_REPORT, 1.0, "expected_alarm"),
        (LEFT_ALARM, {**FAULT_REPORT, "logs": "rollback ok"}, 1.0, "expected_alarm"),
        (LEFT_ALARM, {**FAULT_REPORT, "failed_manual": True}, 1.0, "expected_alarm"),
        (LEFT_ALARM, {}, 1.0, "expected_alarm"),  # 보고를 못 찾으면 통과시키지 않는다
        (LEFT_ALARM, {}, 1.0, "code"),
        (LEFT_ALARM, FAULT_REPORT, 0.0, "alarm_metric"),
        (LEFT_ALARM, FAULT_REPORT, None, "alarm_metric"),
        # health가 아닌 phase 실패도 전환기 경보는 똑같이 붙는다 — 보고 code로만 가려진다
        (
            "g2: E_SWITCH E_DEFINITIONS_LEFT",
            {**FAULT_REPORT, "code": "E_SWITCH"},
            1.0,
            "code",
        ),
        (LEFT_ALARM, {**FAULT_REPORT, "code": "E_MIGRATE"}, 1.0, "code"),
        (LEFT_ALARM, {**FAULT_REPORT, "code": None}, 1.0, "code"),
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
    checks = judges.judge_attempt(
        snap,
        report,
        expect="failed",
        want_digests=WANT,
        want_applied=3,
        expect_alarm=judges.E_DEFINITIONS_LEFT,
        expect_code=judges.FAULT_CODE,
        check_metric=True,
    )
    assert not checks[failing]
    # 실패한 판정 외에는 기준 상태 그대로다(매개변수가 엉뚱한 곳을 깨지 않는다)
    if alarm == LEFT_ALARM and metric == 1.0 and report.get("logs") == LEFT_LOGS:
        assert {k for k, ok in checks.items() if not ok} == {failing}


def test_judge_attempt_alarm_cleared_by_tree_release() -> None:
    cleared: dict[str, object] = {
        **DONE,
        "updater": {"last_result": "succeeded", "alarm": None, "applied_gen": 5, "attempt": None},
        "metrics": {"updater_failed_manual": 0.0},
    }

    def judge(snap: dict[str, object]) -> dict[str, bool]:
        return judges.judge_attempt(
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


def test_alarm_clear_sees_tree_rewritten() -> None:
    """`judge_fixture_tree`(M5 마감 경보 해제, M4 마감 ITEM_FAULT·ITEM_RETRY의 `extra`): 메타만이
    아니라 fixture 트리가 실제로 적용돼 있는지(content_hash·project_files·정의)를 본다."""
    assert all(trees.judge_fixture_tree(TREE_SNAP, PAIR).values())
    # 정의 교체 없이 메타만 남은 성공(회귀)이면 GET /project content_hash가 r3 값이다
    r3_meta = with_body(content_hash="sha256:" + "33" * 32)
    assert not trees.judge_fixture_tree(r3_meta, PAIR)["project_version"]
    # 트리 적용이 project_files·정의를 쓰지 않았으면(BASE DB 그대로) 그 항목들이 실패한다
    untouched = {**TREE_SNAP, "db": R3_C0_DB}
    failing = {k for k, ok in trees.judge_fixture_tree(untouched, PAIR).items() if not ok}
    assert failing == {"project_files", "devices", "tags", "alarms", "seed_retired", "definition"}


def test_fault_tree_differs_so_rollback_is_visible(tmp_path: Path) -> None:
    """M4 마감 ITEM_FAULT: FAULT 번들은 FAULT 트리(fixture에서 `TREE_TAG` description만 바꾼 것)를
    싣는다 — content_hash와 정의 값이 달라서, 롤백이 BASE 트리를 되돌리지 못하면
    `judge_fixture_tree`의 `project_version`이, 정의를 되돌리지 못하면 `definition`이 실패한다
    (같은 트리였다면 "되돌아왔다"를 구별할 수 없다)."""
    other = release_expect(tmp_path, FAULT)
    assert other.content_hash != TREE.content_hash
    assert other.tree_tag_description != TREE.tree_tag_description
    left_in_place = with_body(content_hash=other.content_hash)
    assert {k for k, ok in trees.judge_fixture_tree(left_in_place, PAIR).items() if not ok} == {
        "project_version"
    }


def test_alarm_has_matches_joined_entries() -> None:
    code = judges.E_DEFINITIONS_LEFT
    assert judges.alarm_has(LEFT_ALARM, code)
    assert judges.alarm_has("g3: E_HEALTH; g2: E_HEALTH E_DEFINITIONS_LEFT", code)
    assert not judges.alarm_has("g3: E_HEALTH", code)
    assert not judges.alarm_has("g2: E_HEALTH E_DEFINITIONS_LEFT_X", code)
    assert not judges.alarm_has(None, code)


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
    assert judges.metric({"metrics": parsed}, judges.ALARM_METRIC) == 1.0
    assert judges.metric({"metrics": {"error": "FileNotFoundError"}}, judges.ALARM_METRIC) is None
    assert judges.metric({}, judges.ALARM_METRIC) is None


TREE = trees.tree_expect(trees.FIXTURE, PAIR)
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
    "db": R4_TREE_DB,
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
    assert TREE.content_hash == load_project(trees.FIXTURE).content_hash()


def with_body(**changes: object) -> dict[str, object]:
    return {**TREE_SNAP, "project": {"status": 200, "body": {**PROJECT_BODY, **changes}}}


def test_judge_tree() -> None:
    assert all(judges.judge_tree(TREE_SNAP, TREE).values())
    assert not judges.judge_tree(with_body(site_rev=1), TREE)["base_site_rev"]
    assert not judges.judge_tree(with_body(version="v2"), TREE)["project_version"]
    assert not judges.judge_tree(with_body(content_hash="sha256:00"), TREE)["project_version"]
    # r3(트리 이전) 적용이면 project_files가 없고 시드 장비·태그가 retire되지 않는다
    r3 = {**TREE_SNAP, "db": R3_C0_DB}
    failing = {k for k, ok in judges.judge_tree(r3, TREE).items() if not ok}
    assert failing == {"project_files", "devices", "tags", "alarms", "seed_retired", "definition"}
    extra = {**TREE_SNAP, "db": {**R4_TREE_DB, "device_ids": "e2e-dev,plc-1"}}
    assert not judges.judge_tree(extra, TREE)["devices"]
    assert not any(judges.judge_tree({"project": {"status": 503}, "db": {}}, TREE).values())


TAG_PATCHED = {"line1/temp": {"path": "line1/temp", "record": judges.PATCH_TAG_BODY["record"]}}
ALARM_PATCHED = {"temp-high": {"id": "temp-high", "setpoint": 82.5, "priority": "high"}}
PATCHED_DB: dict[str, object] = {
    **R4_TREE_DB,
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
    assert all(judges.judge_patch(PATCH_OUT, PATCHED_DB).values())
    # PATCH 전 정의(fixture 값)이면 읽기 대조에서 걸린다
    assert not judges.judge_patch(PATCH_OUT, R4_TREE_DB)["readback"]


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
    assert not judges.judge_patch(out, PATCHED_DB)[failing]


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
    assert all(judges.judge_absorb(ABSORBED, 2, SITE).values())
    assert judges.absorbed_hash(ABSORBED) == ABSORB_HASH
    assert not judges.judge_absorb(ABSORBED, 3, SITE)["absorbs_rev"]
    # absorb가 실패하면 gate는 돌지 않는다(결과에 `gate` 없음)
    conflict = {"dir": ABSORBED["dir"], "absorb": {"rc": 2, "stdout": "", "stderr": "conflict: x"}}
    checks = judges.judge_absorb(conflict, 2, SITE)
    assert not checks["absorb"]
    assert not checks["gate"]
    assert judges.absorbed_hash(conflict) is None
    gate_failed = {**ABSORBED, "gate": {"rc": 1, "stdout": "", "stderr": "error: E_DRAFT"}}
    assert not judges.judge_absorb(gate_failed, 2, SITE)["gate"]


def test_judge_reapplied() -> None:
    body = {**PROJECT_BODY, "content_hash": ABSORB_HASH, "site_rev": 2, "base_site_rev": 2}
    snap = {**TREE_SNAP, "project": {"status": 200, "body": body}, "db": PATCHED_DB}
    assert all(judges.judge_reapplied(snap, 2, ABSORB_HASH).values())
    # 흡수 전 트리(fixture hash)가 적용됐거나 기준점이 안 옮겨졌거나 PATCH 값이 되돌아가면 실패
    assert not judges.judge_reapplied(snap, 2, None)["content_hash"]
    moved = {**snap, "project": {"status": 200, "body": {**body, "base_site_rev": 0}}}
    assert not judges.judge_reapplied(moved, 2, ABSORB_HASH)["base_site_rev"]
    reverted = {**snap, "db": R4_TREE_DB}
    assert not judges.judge_reapplied(reverted, 2, ABSORB_HASH)["patch_kept"]


def test_unabsorbed_copy_drops_only_absorbs(tmp_path: Path) -> None:
    fixture = canonical.load((bundles.FIXTURE / "project.yaml").read_text("utf-8"), "project.yaml")
    # fixture가 이미 site-a를 PATCH 수 이상 흡수했다고 적고 있어 VM site_rev로는 게이트가 통과한다
    absorbs = cast(dict[str, object], cast(dict[str, object], fixture)["absorbs"])
    assert cast(int, absorbs["site-a"]) >= judges.PATCHES
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
    assert site_api.session_of([cookie]) == "tok123"
    assert site_api.session_of([]) is None
    # Set-Cookie가 여럿이어도(CSRF 쿠키 등) 앞뒤 순서와 무관하게 세션 쿠키를 찾는다
    other = "pureiron_csrf=x9; Path=/; SameSite=strict"
    assert site_api.session_of([other, cookie]) == "tok123"
    assert site_api.session_of([cookie, other]) == "tok123"
    assert site_api.session_of([other]) is None
    # 비안전 메서드의 브라우저 헤더: Origin = web(요청 Host와 같은 host:port), same-origin
    assert site_api.BROWSER_HEADERS["Origin"] == site_api.WEB
    assert site_api.BROWSER_HEADERS["Sec-Fetch-Site"] == "same-origin"


def test_m5_guarded_records_unreached_items_and_returns(tmp_path: Path) -> None:
    """M5 블록의 전송·WSL 예외: 이번에 기록한 항목은 그대로, 못 간 항목만 실패(`error`)로 남기고
    돌아온다(호출자가 restore_c0부터 이어 간다). 이전 실행의 같은 이름 결과는 덮어쓴다."""
    ctx = tmp_ctx(tmp_path)
    ctx.record(run.ITEM_ABSORB, True, stale=True)  # 이전 실행(`--from scenarios`)이 남긴 결과
    ctx.record(run.ITEM_UPGRADE, True)

    def broken(c: Context, gens: dict[str, int]) -> None:
        c.record(run.ITEM_TREE, True)
        raise vm.VmError("scp exited 255: connection reset")

    run.m5_guarded(ctx, {}, broken)
    by_name = {r["name"]: r for r in ctx.results}
    assert by_name[run.ITEM_TREE]["ok"] is True
    assert by_name[run.ITEM_UPGRADE]["ok"] is True
    for name in (run.ITEM_PATCH, run.ITEM_DRAFT_GATE, run.ITEM_ABSORB):
        assert by_name[name]["ok"] is False
        assert "connection reset" in str(by_name[name]["error"])
    assert "stale" not in by_name[run.ITEM_ABSORB]
    assert set(run.M5_ITEMS) <= set(run.expected_items())
    # 예외가 없으면 아무것도 덧붙이지 않는다
    calm = tmp_ctx(tmp_path)
    run.m5_guarded(calm, {}, lambda c, g: None)
    assert calm.results == []
