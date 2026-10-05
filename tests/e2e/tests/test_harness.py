"""e2e 하네스의 순수 로직(VM·Hyper-V 없이). 실제 VM 실행은 `tests/e2e/m2d/run.py`."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path, PureWindowsPath

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
