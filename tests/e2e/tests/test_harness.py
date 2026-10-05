"""e2e 하네스의 순수 로직(VM·Hyper-V 없이). 실제 VM 실행은 `tests/e2e/m2d/run.py`."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path, PureWindowsPath
from typing import cast

import crash_at
import golden
import hyperv
import pycdlib
import pytest
import run


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
