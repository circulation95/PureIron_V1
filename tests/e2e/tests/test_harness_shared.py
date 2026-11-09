"""M6-1 하네스 공용화(`tests/e2e/gates/harness/`)의 불변 조건 — 옮기기 전과 같은 값·이름·순서이고,
현장 둘(G2 site-a·site-b)이 상태·이름을 섞지 않는다."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest
import run
from harness import buildhost, hyperv, judges, paths
from harness.context import Context
from harness.release_pair import DEFAULT
from harness.site import Site

M4_CLOSE_ITEMS = [
    "vm boot (cloud-init, internal-only switch)",
    "OS quiet before install (nftables baseline)",
    "install bundle bootstrap + g1(r4) on offline VM",
    "reject site",
    "reject tampered",
    "same gen rebuild → no-op",
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
    "crash kill @ migrating",
    "crash reset @ migrating",
    "crash kill @ fetched",
    "crash reset @ fetched",
    "crash kill @ verified",
    "crash reset @ verified",
    "crash kill @ committed",
    "crash reset @ committed",
    "crash kill @ locked",
    "crash reset @ locked",
    "crash kill @ backed_up",
    "crash reset @ backed_up",
    "crash kill @ switching",
    "crash reset @ switching",
    "crash kill @ checking",
    "crash reset @ checking",
    "crash kill @ rolling_back",
    "crash reset @ rolling_back",
    "online: HTTPS heartbeat to central c0 (private CA trusted) → /fleet online",
    "online: central-published r4 g2 → download + apply succeeded",
    "online: ApplyAttempt report → /fleet current + last_attempt (source online)",
    "online: central web down → site keeps running, /fleet stale → back online",
    "no external connection attempts since install (nftables, accumulated)",
]
"""master 0fa6ce4 `run.expected_items()`(M4 마감 39/39 실측의 항목) — 순서까지 그대로여야 실측끼리
비교된다."""


def test_expected_items_are_the_m4_close_list_in_order() -> None:
    assert run.expected_items() == M4_CLOSE_ITEMS


def test_default_pair_equals_the_old_module_constants() -> None:
    """옮기기 전 `release_pair.py` 모듈 상수(BASE·NEXT·FAULT·RELEASES·EPOCHS·*_TABLES 등) 값."""
    assert (DEFAULT.base, DEFAULT.next, DEFAULT.fault) == ("r4", "r5", "r5f")
    assert dict(DEFAULT.releases) == {
        "r4": "2026.11.0",
        "r5": "2026.11.1",
        "r5f": "2026.11.1-fault1",
    }
    assert dict(DEFAULT.epochs) == {"r4": 5, "r5": 5, "r5f": 5}
    assert frozenset({"tag_history_1m", "tag_history_1h"}) == DEFAULT.caggs
    assert DEFAULT.base_columns == {
        "devices.definition",
        "tags.definition",
        "project_state.definitions_rev",
        "project_state.historian",
    }
    assert DEFAULT.base_tables == {
        "users",
        "sessions",
        "audit_log",
        "alarms",
        "alarm_journal",
        "project_files",
    }
    assert DEFAULT.base_indexes == {
        "sessions_user_id_idx",
        "audit_log_time_idx",
        "audit_log_username_time_idx",
        "alarm_journal_alarm_id_time_idx",
        "alarm_journal_time_idx",
    }
    assert frozenset() == DEFAULT.next_tables == DEFAULT.next_indexes
    assert DEFAULT.tree_tag == "line1/temp_sp"
    assert dict(DEFAULT.tree_descriptions) == {
        "r5": "1라인 온도 설정값 (r5)",
        "r5f": "1라인 온도 설정값 (r5f)",
    }
    assert run.PAIR is DEFAULT


def test_judges_follow_the_given_pair() -> None:
    """판정은 인자로 받은 쌍을 본다(모듈 상수가 아니다) — 다른 쌍이면 같은 DB가 다르게 판정된다."""
    other = dataclasses.replace(DEFAULT, base="r9", epochs={**DEFAULT.epochs, "r9": 7})
    db: dict[str, object] = {"epoch": "5", "compressed_chunks": "1", "tag_history_rows": "1"}
    assert judges.judge_c0(db, DEFAULT)["epoch"]
    assert not judges.judge_c0(db, other)["epoch"]
    record: dict[str, object] = {
        "version": "2026.11.0",
        "schema": {"epoch": 5, "app_expected": 5, "app_max_compatible": 6},
    }
    judges.check_record(DEFAULT, "r4", record)
    renamed = dataclasses.replace(DEFAULT, releases={"r4": "2027.1.0"})
    with pytest.raises(run.E2EError):
        judges.check_record(renamed, "r4", record)


@pytest.mark.parametrize(
    ("seconds", "images_match", "ok"),
    [(120.0, True, True), (300.0, True, True), (300.1, True, False), (120.0, False, False)],
)
def test_rollback_time_limit_is_five_minutes(seconds: float, images_match: bool, ok: bool) -> None:
    result: dict[str, object] = {"seconds": seconds, "images_match": images_match}
    assert judges.judge_rollback_time(result) is ok


def two_sites(root: Path) -> tuple[Site, Site]:
    """G2 현장 둘(M6 계획 ★D2) — 상태 파일은 `root` 아래 현장마다 따로."""
    return (
        Site("site-a", "pureiron-site-a", "10.77.0.10", root / "state-site-a.json"),
        Site("site-b", "pureiron-site-b", "10.77.0.11", root / "state-site-b.json"),
    )


def test_two_sites_keep_state_and_names_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "E2E", tmp_path)
    monkeypatch.setattr(paths, "BUNDLES", tmp_path / "bundles")
    a, b = two_sites(tmp_path)
    ctx_a = Context(run_id="t", artifacts=str(tmp_path), site=a, pair=DEFAULT)
    ctx_b = Context(run_id="t", artifacts=str(tmp_path), site=b, pair=DEFAULT)
    ctx_a.record("only a", True)
    ctx_b.record("only b", False)
    ctx_b.nft_total = {"dns_out": 3}
    ctx_b.save()
    loaded_a, loaded_b = Context.load(a, DEFAULT), Context.load(b, DEFAULT)
    assert [r["name"] for r in loaded_a.results] == ["only a"]
    assert [r["name"] for r in loaded_b.results] == ["only b"]
    assert (loaded_a.nft_total, loaded_b.nft_total) == ({}, {"dns_out": 3})
    assert loaded_a.site is a
    assert loaded_b.site is b
    # 상태 파일에는 실행 설정(site·pair)이 들어가지 않는다 — 옮기기 전 state.json과 같은 키
    assert "site" not in json.loads(a.state_file.read_text(encoding="utf-8"))
    # VM·ssh 대상·번들 이름이 현장마다 다르다
    assert a.vm_dir != b.vm_dir
    assert (a.host().ip, b.host().ip) == ("10.77.0.10", "10.77.0.11")
    assert buildhost.site_bundle(ctx_a, 2, "r5") != buildhost.site_bundle(ctx_b, 2, "r5")
    assert buildhost.site_bundle(ctx_b, 2, "r5").name == "pureiron-site-b-g2-r5.pib"


def test_site_judges_use_the_given_site_id() -> None:
    rows: dict[str, object] = {
        "status": 200,
        "body": [{"site": "site-a", "n": 1}, {"site": "site-b", "n": 2}],
    }
    assert judges.fleet_row(rows, "site-a")["n"] == 1
    assert judges.fleet_row(rows, "site-b")["n"] == 2
    log = "Looking up RR for site-a. IN A\nLooking up RR for site-b. IN A\n"
    assert judges.dns_names(log, "site-a") == {"site-b": 1}
    assert judges.dns_names(log, "site-b") == {"site-a": 1}
    absorbed: dict[str, object] = {
        "absorb": {"rc": 0, "stdout": "absorbs[site-b]: (absent) -> 2\n"},
        "gate": {"rc": 0},
    }
    assert all(judges.judge_absorb(absorbed, 2, "site-b").values())
    assert not judges.judge_absorb(absorbed, 2, "site-a")["absorbs_rev"]


def test_run_keeps_one_site_a_on_the_old_state_file() -> None:
    assert Site("site-a", "pureiron-site-a", "10.77.0.10", paths.E2E / "state.json") == run.SITE_A
    assert run.SITE_A.vm_dir == paths.E2E / "vms" / "pureiron-site-a"


def test_vm_memory_defaults_to_three_gib() -> None:
    """이번 공용화의 유일한 의도된 동작 변경: 현장 VM 4 → 3 GiB(M6-0 실측). VmSpec 기본값은 4."""
    assert run.vm_memory_gib({}) == 3
    assert run.vm_memory_gib({"PUREIRON_E2E_VM_GIB": "4"}) == 4
    spec = hyperv.VmSpec("x", Path("d.vhdx"), Path("s.iso"), "sw")
    assert spec.memory_gib == 4


@pytest.mark.parametrize("script", ["bundles.py", "central.py", "golden.py"])
def test_wsl_scripts_run_as_files(script: str) -> None:
    """WSL은 `python tests/e2e/gates/harness/<이름>.py`로 부른다(`buildhost`) — 패키지 밖에서
    파일로 실행해도 import가 풀려야 한다(central.py가 bundles import의 경로 부수효과에 기대던
    결함)."""
    path = paths.REPO / "tests" / "e2e" / "gates" / "harness" / script
    done = subprocess.run(
        [sys.executable, str(path), "--help"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr[-800:]
