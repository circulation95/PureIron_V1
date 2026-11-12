"""G2(M6-2a)를 위한 하네스 변경의 불변 조건 — 현장 둘(SITE_A·SITE_B)의 작업 디렉터리·로그·site id가
섞이지 않고, seed 기본값은 바이트까지 그대로이며, crash 창 여유 flake(E10)만 1회 다시 한다."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
import run
from harness import buildhost, g2, golden, judges, paths, preflight, site, sites, trees, vm
from harness.context import Context, E2EError, InfraError
from harness.release_pair import DEFAULT, nightly
from harness.vm import Vm

MASTER_SEED_SHA256 = {
    # master 330e12c의 `golden.seed_files("site-b", "10.77.0.11", "ssh-ed25519 AAAA test",
    # Network(), (("10.0.0.5", 502),))` — dummy 옵션을 끈 seed가 바이트까지 같아야 run.py가
    # 그대로다.
    "meta-data": "d659bcb2bbed0bbf226e8198ca6330984588b06653d3b7f2511e58d8c6f8f694",
    "network-config": "d5a5a179cba457abd79a687a4d6d0d3c3b4218a656fa37897d7b1cb68a9f66fb",
    "user-data": "b3f88e69f45278e2c06b329c712a17d9221a577e4c90811764a5c075bd0dc30f",
}


def site_ctx(tmp_path: Path, which: str) -> Context:
    base = sites.SITE_A if which == "site-a" else sites.SITE_B
    one = dataclasses.replace(base, state_file=tmp_path / f"{which}.json")
    return Context(run_id="t", artifacts=str(tmp_path), site=one, pair=DEFAULT)


def test_site_b_is_the_second_g2_site() -> None:
    assert sites.SITE_B.site_id == "site-b"
    assert sites.SITE_B.vm_name == "pureiron-site-b"
    assert sites.SITE_B.ip == "10.77.0.11"
    assert sites.SITE_B.state_file != sites.SITE_A.state_file
    assert run.SITE_A is sites.SITE_A


def test_each_site_has_its_own_bundle_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L4: 같은 run_id라도 번들·트리·m5·online이 현장 디렉터리로 갈린다."""
    monkeypatch.setattr(paths, "BUNDLES", tmp_path / "bundles")
    a, b = site_ctx(tmp_path, "site-a"), site_ctx(tmp_path, "site-b")
    assert a.work == tmp_path / "bundles" / "t" / "site-a"
    assert b.work == tmp_path / "bundles" / "t" / "site-b"
    assert buildhost.site_bundle(b, 2, "r5") == b.work / "pureiron-site-b-g2-r5.pib"
    assert buildhost.release_tree(a, "r5") == a.work / "trees" / "r5"
    assert buildhost.release_tree(b, "r5") == b.work / "trees" / "r5"


def test_reject_site_uses_an_id_no_g2_site_has() -> None:
    """L4: "reject site" 번들의 다른 현장은 실제 G2 현장이 아니다."""
    assert run.OTHER_SITE not in (sites.SITE_A.site_id, sites.SITE_B.site_id)


def test_record_lines_carry_the_site_prefix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site_ctx(tmp_path, "site-b").record("x", True, n=1)
    site_ctx(tmp_path, "site-b").log("hello")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == '[site-b] [PASS] x {"n": 1}'
    assert out[1].endswith(" [site-b] hello")


def test_current_defaults_to_the_pair_base(tmp_path: Path) -> None:
    """N1: `current`를 주지 않으면 설치 g1(BASE)이다. 상태 파일 값은 그대로 읽는다."""
    ctx = site_ctx(tmp_path, "site-a")
    assert ctx.current == "r4"
    ctx.current = "r5"
    ctx.save()
    assert Context.load(ctx.site, DEFAULT).current == "r5"


def test_infra_error_is_not_an_e2e_error() -> None:
    """E8: `except E2EError`가 인프라 실패를 제품 실패로 삼키지 않는다."""
    assert not issubclass(InfraError, E2EError)
    assert issubclass(golden.WslError, RuntimeError)


def test_seed_without_dummy_is_byte_identical_to_master() -> None:
    files = golden.seed_files(
        "site-b", "10.77.0.11", "ssh-ed25519 AAAA test", golden.Network(), (("10.0.0.5", 502),)
    )
    assert {k: hashlib.sha256(v).hexdigest() for k, v in files.items()} == MASTER_SEED_SHA256


def test_seed_dummy_adds_the_field_address_inside_the_vm() -> None:
    plain = golden.seed_files("site-b", "10.77.0.11", "k", golden.Network())["user-data"].decode()
    user_data = golden.seed_files("site-b", "10.77.0.11", "k", golden.Network(), dummy="10.0.0.5")[
        "user-data"
    ].decode()
    assert "Kind=dummy" in user_data
    assert "Address=10.0.0.5/32" in user_data
    assert "runcmd:\n  - [networkctl, reload]\n" in user_data
    # 감시 규칙은 그대로다(10.0.0.5를 제외 목록에 넣지 않는다 — 판정은 패킷 경로로 성립)
    assert user_data.count("comment ") == plain.count("comment ")
    with pytest.raises(ValueError, match=r"plc.local"):
        golden.seed_files("s", "10.77.0.11", "k", golden.Network(), dummy="plc.local")


class FakeVm:
    """`nft_mark`가 부르는 원격 명령(nft 카운터·resolved 로그)만 흉내 낸다."""

    def __init__(self, log: str) -> None:
        self.log = log
        self.commands: list[str] = []

    def run(
        self, command: str, *, timeout_s: float = 600, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout_s, check, input
        self.commands.append(command)
        if "nft -j" in command:
            rules = [
                {"rule": {"comment": name, "expr": [{"counter": {"packets": 0, "bytes": 0}}]}}
                for name in ("ext_out_v4", "dns_out", "ext_out_v6", "ext_fwd")
            ]
            return subprocess.CompletedProcess[str](command, 0, json.dumps({"nftables": rules}), "")
        return subprocess.CompletedProcess[str](command, 0, f"1700000100\n{self.log}", "")


def test_nft_mark_passes_the_site_id_of_the_context(tmp_path: Path) -> None:
    """L3: site-b에서는 자기 호스트 이름(site-b)이 외부 조회로 세어지지 않는다(변이: site id를
    "site-a"로 고정하면 site-b가 남는다)."""
    ctx = site_ctx(tmp_path, "site-b")
    host = FakeVm("Looking up RR for site-b. IN A\nLooking up RR for example.com. IN A\n")
    site.nft_mark(ctx, cast(Vm, host))
    assert ctx.dns_lookups == {"example.com": 1}
    assert ctx.nft_mark_at == 1700000100
    assert judges.no_external(ctx.nft_total)


# ---------------------------------------------------------------- E10 창 여유 flake


PRODUCT_OK = {"result": True, "images_match": True, "hit": True, "rows_kept": True}


@pytest.mark.parametrize(
    ("staged", "margin_s", "rc", "product", "flake"),
    [
        (False, -3.0, 0, PRODUCT_OK, True),
        (False, 0.0, 0, PRODUCT_OK, True),
        (False, -3.0, 0, {**PRODUCT_OK, "images_match": False}, False),
        (False, -3.0, 1, PRODUCT_OK, False),
        (True, 12.0, 0, PRODUCT_OK, False),
        # L1 경계: 실제 여유 0 < m < 0.05초는 판정이 참인데 반올림해 0.0으로 남는다 — 통과한 건
        (True, 0.0, 0, PRODUCT_OK, False),
    ],
)
def test_window_flake_needs_late_staging_and_all_product_checks(
    staged: bool, margin_s: float, rc: int, product: dict[str, bool], flake: bool
) -> None:
    checks = {**product, "staged_before_window": staged}
    stage: dict[str, object] = {"rc": rc, "margin_s": margin_s, "tries": 1}
    assert judges.judge_window_flake(checks, stage) is flake


def test_cli_target_is_never_a_window_flake() -> None:
    assert not judges.judge_window_flake(PRODUCT_OK, {"rc": 0})


class FakeCrash:
    """`crash_once` 대신 호출을 기록한다. `flaky[이름]`번만 창 여유 flake를 돌려준다."""

    def __init__(self, flaky: dict[str, int]) -> None:
        self.flaky = dict(flaky)
        self.calls: list[tuple[str, int, bool]] = []

    def __call__(
        self, ctx: Context, case: run.CrashCase, *, gen: int, note: dict[str, object] | None = None
    ) -> dict[str, object] | None:
        del ctx
        self.calls.append((case.name, gen, note is not None))
        if self.flaky.get(case.name, 0) > 0:
            self.flaky[case.name] -= 1
            return {"gen": gen}
        return None


def run_crash(
    tmp_path: Path, flaky: dict[str, int]
) -> tuple[FakeCrash, list[tuple[int, str]], list[str], Context]:
    ctx = site_ctx(tmp_path, "site-a")
    ctx.next_gen = 10
    crash = FakeCrash(flaky)
    settled: list[tuple[int, str]] = []
    restored: list[str] = []

    def settle(_: Context, gen: int, release: str) -> None:
        settled.append((gen, release))

    def restore(_: Context) -> None:
        restored.append(crash.calls[-1][0] if crash.calls else "start")

    run.step_crash(ctx, crash=crash, settle=settle, restore=restore)
    return crash, settled, restored, ctx


def test_crash_without_flake_runs_each_case_once(tmp_path: Path) -> None:
    crash, settled, restored, ctx = run_crash(tmp_path, {})
    assert [name for name, _, _ in crash.calls] == [c.name for c in run.crash_plan(DEFAULT)]
    assert [gen for _, gen, _ in crash.calls[:3]] == [10, 10, 11]
    assert crash.calls[-1][1] == 26
    assert (settled, len(restored), ctx.next_gen) == ([], 2, 27)


def test_window_flake_is_retried_once_from_the_previous_release(tmp_path: Path) -> None:
    """E10: FAULT가 아닌 건은 `prev`로 되돌린 뒤(gen 하나) 다음 gen으로 같은 건을 1회 더 한다."""
    crash, settled, _, ctx = run_crash(tmp_path, {"crash kill @ verified": 1})
    names = [name for name, _, _ in crash.calls]
    at = names.index("crash kill @ verified")
    assert crash.calls[at] == ("crash kill @ verified", 13, False)
    assert settled == [(14, "r5")]
    assert crash.calls[at + 1] == ("crash kill @ verified", 15, True)
    assert crash.calls[at + 2] == ("crash reset @ verified", 16, False)
    assert len(crash.calls) == len(run.crash_plan(DEFAULT)) + 1
    assert ctx.next_gen == 29


def test_window_flake_twice_is_left_as_a_failure(tmp_path: Path) -> None:
    crash, settled, _, _ = run_crash(tmp_path, {"crash kill @ verified": 2})
    assert [n for n, _, _ in crash.calls].count("crash kill @ verified") == 2
    assert len(settled) == 1


def test_window_flake_from_c0_restores_c0_and_reuses_the_gen(tmp_path: Path) -> None:
    crash, settled, restored, _ = run_crash(tmp_path, {"crash reset @ migrating": 1})
    assert crash.calls[:3] == [
        ("crash kill @ migrating", 10, False),
        ("crash reset @ migrating", 10, False),
        ("crash reset @ migrating", 10, True),
    ]
    assert restored == ["start", "crash kill @ migrating", "crash reset @ migrating"]
    assert settled == []


def test_window_flake_on_fault_retries_without_settling(tmp_path: Path) -> None:
    """FAULT는 이미 `prev`로 롤백돼 있어 되돌리기 없이 다음 gen으로 다시 한다."""
    crash, settled, _, _ = run_crash(tmp_path, {"crash kill @ rolling_back": 1})
    retried = [c for c in crash.calls if c[0] == "crash kill @ rolling_back"]
    assert [(gen, noted) for _, gen, noted in retried] == [(25, False), (26, True)]
    assert settled == []


# ---------------------------------------------------------------- 리뷰 r1 M3·L3(현장 격리·배선)


SIM_PATHS = sorted(trees.g2_tag_paths())
NEXT_DIGESTS = {"core": "sha256:n1", "collector": "sha256:n2", "web": "sha256:n3"}
C0_DB: dict[str, object] = {
    "epoch": "5",
    "compressed_chunks": "3",
    "tag_history_rows": "6900",
    **dict.fromkeys(judges.SCHEMA_SETS, "x"),
}
"""이력 시드 뒤 c0 기반 probe `db`(M6-3a I2) — 압축 chunk·행 > 0, epoch = 쌍의 epoch 5."""


def g2_session(tmp_path: Path) -> g2.Session:
    pair = nightly({"r5": {"version": "2026.11.1", "schema": {"epoch": 5}}}, {"r5"}, "a" * 40, 5)
    runs: dict[str, g2.SiteRun] = {}
    for base, _ in g2.SITE_ROLES:
        release = g2.site_release(pair, base.site_id)
        one = dataclasses.replace(base, state_file=tmp_path / base.site_id / "state.json")
        ctx = Context(
            run_id="20261110T180000Z",
            artifacts=str(tmp_path / base.site_id),
            digests={release: NEXT_DIGESTS},
            current=release,
            site=one,
            pair=pair,
        )
        ctx.nft_total = dict.fromkeys(judges.AUDIT_COUNTERS, 0)
        runs[base.site_id] = g2.SiteRun(ctx, release, c0_at=0.0, c0_db=dict(C0_DB))
    expected = {k: C0_DB[k] for k in ("epoch", *judges.SCHEMA_SETS)}
    return g2.Session(
        "20261110T180000Z",
        "a" * 40,
        pair,
        tmp_path,
        tmp_path / "sim.tar",
        runs,
        expected_schema=expected,
    )


def installed(_: Vm) -> dict[str, object]:
    return {
        "readyz": {"status": 200},
        "containers": {svc: {"image_id": d, "running": True} for svc, d in NEXT_DIGESTS.items()},
        "docker": {"driver": "overlay2", "driver_status": [["Backing Filesystem", "extfs"]]},
    }


def live_reply(bad: str | None = None) -> dict[str, object]:
    tags = [{"path": p, "quality": "BAD" if p == bad else "GOOD", "value": 1.0} for p in SIM_PATHS]
    device = {
        "device_id": trees.SIM_DEVICE,
        "state": "CONNECTED",
        "connected": True,
        "silent": False,
    }
    return {"tags": [200, {"tags": tags}], "devices": [200, {"devices": [device]}]}


def check(
    session: g2.Session,
    site_id: str,
    reply: dict[str, object],
    clock: list[float],
) -> tuple[dict[str, bool], list[str]]:
    reads: list[str] = []

    def live(_: Vm) -> dict[str, object]:
        reads.append("live")
        return reply

    ticks = iter(clock)
    checks = g2.check_install(
        session,
        site_id,
        probe=installed,
        live=live,
        mark=lambda ctx, host: None,
        clock=lambda: next(ticks),
        sleep=lambda _: None,
    )
    return checks, reads


def test_check_install_expects_all_100_simulator_tags(tmp_path: Path) -> None:
    """M3-2: 배선(`want = trees.g2_tag_paths()`) — 기대 태그를 일부만 넘기면 100개 중 하나가 BAD여도
    통과한다(리뷰 변이 M10)."""
    session = g2_session(tmp_path)
    good, _ = check(session, "site-b", live_reply(), [5.0, 5.0, 5.0])
    assert all(good.values()), good
    for bad in (SIM_PATHS[0], SIM_PATHS[57], SIM_PATHS[-1]):
        checks, _ = check(session, "site-b", live_reply(bad=bad), [5.0, 5.0, 301.0, 301.0])
        assert checks["tags_good"] is False
    record = {r["name"]: r for r in session.sites["site-b"].ctx.results}[g2.ITEM_G2_1]
    assert record["ok"] is False
    assert record["tags_not_good"] == [SIM_PATHS[-1]]


def test_live_deadline_counts_from_c0(tmp_path: Path) -> None:
    """L3: "c0 뒤 5분" — 앞 현장 판정이 시간을 쓴 만큼 뒤 현장의 기다림이 줄어든다(c0 + 300초가
    마감). 마감이 지났어도 한 번은 읽지만, 그 값이 GOOD이어도 5분 안이 증명되지 않았으므로
    통과하지 않는다(R2-L3 `live_in_time`)."""
    session = g2_session(tmp_path)
    # c0 = 0초, 판정 시작 = 290초: 첫 읽기(290)는 BAD, 다음 확인(305)에서 멈춘다(300초를 새로 받지
    # 않는다)
    checks, reads = check(session, "site-b", live_reply(bad=SIM_PATHS[3]), [290.0, 305.0, 305.0])
    assert (checks["tags_good"], len(reads)) == (False, 1)
    record = {r["name"]: r for r in session.sites["site-b"].ctx.results}[g2.ITEM_G2_1]
    assert record["live_s"] == 305.0
    # 마감(300) 뒤에 시작해도 한 번은 읽는다 — 다만 5분 안이 아니므로 G2-1은 실패다
    checks, reads = check(session, "site-a", live_reply(), [400.0, 400.0, 400.0])
    assert (checks["tags_good"], checks["live_in_time"], len(reads)) == (True, False, 1)
    record = {r["name"]: r for r in session.sites["site-a"].ctx.results}[g2.ITEM_G2_1]
    assert (record["ok"], record["live_s"]) == (False, 400.0)


def test_check_install_judges_the_watch_started_right_after_c0(tmp_path: Path) -> None:
    """R2-L3: `bring_up`이 c0 직후부터 지켜본 결과(`run.live`)로 판정한다 — 판정이 늦게(다른 현장
    부팅 뒤, 직렬 모드) 시작해도 live를 다시 읽지 않고 "c0부터 120초에 모두 GOOD"을 기록한다."""
    session = g2_session(tmp_path)
    want = trees.g2_tag_paths()
    good = live_reply()
    session.sites["site-a"].live = g2.LiveWatch(
        good, judges.judge_live(good, want, trees.SIM_DEVICE), 120.0
    )
    checks = g2.check_install(
        session,
        "site-a",
        probe=installed,
        live=lambda _: pytest.fail("the bring-up watch already proved it"),
        mark=lambda ctx, host: None,
        clock=lambda: pytest.fail("no new clock reads"),
        sleep=lambda _: None,
    )
    assert all(checks.values()), checks
    record = {r["name"]: r for r in session.sites["site-a"].ctx.results}[g2.ITEM_G2_1]
    assert (record["ok"], record["live_s"]) == (True, 120.0)
    # 지켜본 결과가 BAD로 끝났으면 실패다
    bad = live_reply(bad=SIM_PATHS[9])
    session.sites["site-b"].live = g2.LiveWatch(
        bad, judges.judge_live(bad, want, trees.SIM_DEVICE), 301.0
    )
    checks = g2.check_install(
        session,
        "site-b",
        probe=installed,
        live=lambda _: pytest.fail("no re-read"),
        mark=lambda ctx, host: None,
    )
    assert (checks["tags_good"], checks["live_in_time"]) == (False, False)


def test_watch_live_polls_from_c0_until_all_good(tmp_path: Path) -> None:
    """R2-L3: `watch_live`는 c0부터 잰다 — c0 = 100초, 110초에 BAD, 130초에 GOOD이면 30초다."""
    session = g2_session(tmp_path)
    run_a = session.sites["site-a"]
    run_a.c0_at = 100.0
    replies = [live_reply(bad=SIM_PATHS[0]), live_reply()]
    ticks = iter([110.0, 130.0, 130.0])
    watch = g2.watch_live(
        run_a, live=lambda _: replies.pop(0), clock=lambda: next(ticks), sleep=lambda _: None
    )
    assert replies == []
    assert all(watch.checks.values())
    assert watch.seconds == 30.0


def session_for_bring_up(tmp_path: Path) -> g2.Session:
    session = g2_session(tmp_path)
    for one in session.sites.values():
        one.c0_at = None
    return session


@pytest.mark.parametrize("serial", ["1", "0"])
def test_one_site_failure_is_kept_for_its_test_and_does_not_stop_the_other(
    tmp_path: Path, serial: str
) -> None:
    """M3-1: 현장 하나의 부팅·설치 예외를 삼키지 않는다 — 그 현장의 결과(`SiteRun.error`)로 두고 그
    현장 시험(`check_install`)이 같은 예외를 다시 던진다. 다른 현장은 끝까지 간다(리뷰 변이 M5)."""
    session = session_for_bring_up(tmp_path)
    boom = vm.RemoteCommandError("ssh `sudo bash install.sh` exited 1: broken", 1)
    done: list[str] = []

    def up(one: g2.SiteRun, s: g2.Session, pub: str) -> None:
        assert pub == "ssh-ed25519 AAAA test"
        if one.ctx.site.site_id == "site-a":
            raise boom
        done.append(one.ctx.site.site_id)

    g2.bring_up_all(
        session,
        {"PUREIRON_G2_SERIAL": serial},
        up=up,
        prepare_host=lambda: "ssh-ed25519 AAAA test",
        room=lambda runs, env: g2.MemoryGate(0.0, 9.0, 9.0, []),
    )
    assert session.sites["site-a"].error is boom
    assert session.sites["site-b"].error is None
    assert done == ["site-b"]
    with pytest.raises(vm.RemoteCommandError) as raised:
        g2.check_install(session, "site-a", probe=lambda _: pytest.fail("must not probe"))
    assert raised.value is boom


class Host:
    """VM 시작 직전 메모리 확인의 가짜 호스트 — Windows 가용 메모리(GB) 측정값을 차례로 내고(마지막
    값은 계속), 회수·대기·VM 시작을 `events`에 시간순으로 남긴다. `sleep`이 시계를 앞으로 민다."""

    def __init__(self, readings: list[float | None]) -> None:
        self.readings = readings
        self.events: list[str] = []
        self.now = 0.0

    def mem_now(self) -> float | None:
        value = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        self.events.append(f"read {value}")
        return value

    def reclaim(self) -> None:
        self.events.append("reclaim")

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.events.append("sleep")

    def clock(self) -> float:
        return self.now

    def room(self) -> g2.MakeRoom:
        def room(runs: list[g2.SiteRun], env: Mapping[str, str]) -> g2.MemoryGate:
            self.events.append("room " + ",".join(r.ctx.site.site_id for r in runs))
            return g2.make_room(
                runs,
                env,
                mem_now=self.mem_now,
                reclaim=self.reclaim,
                sleep=self.sleep,
                clock=self.clock,
            )

        return room

    def up(self, one: g2.SiteRun, s: g2.Session, pub: str) -> None:
        self.events.append(f"start {one.ctx.site.site_id}")


VM_3GIB = {"PUREIRON_E2E_VM_GIB": "3"}


def bring_up_with(host: Host, session: g2.Session, env: dict[str, str]) -> None:
    g2.bring_up_all(
        session, env, up=host.up, prepare_host=lambda: "ssh-ed25519 AAAA test", room=host.room()
    )


def test_vm_start_needs_every_vm_memory_plus_headroom() -> None:
    """R1-1 (c): 필요량 = VM 메모리 * 지금 시작할 대수 + 여유(3 GiB VM 2대 = 3 * 2 + 2 = 8)."""
    assert g2.VM_START_HEADROOM_GB == 2.0
    assert g2.vm_start_need_gb(2, VM_3GIB) == 8.0
    assert g2.vm_start_need_gb(1, VM_3GIB) == 5.0
    assert g2.vm_start_need_gb(2, {}) == 8.0


def test_vms_start_only_after_reclaimed_memory_has_come_back(tmp_path: Path) -> None:
    """R1-1 (a)·(c): 빌드 뒤 1.4 GB → 회수분이 늦게 돌아온다(3.4 → 6.0 → 7.9 → 8.5). 두 VM 합
    8 GB가 될 때까지 기다린 뒤에야 VM을 시작한다 — 7.9(VM 하나 몫 5 GB는 넘는다)에서는 아직
    시작하지 않는다. 회수 전·후와 기다린 시간이 현장 결과 디렉터리에 남는다."""
    session = session_for_bring_up(tmp_path)
    host = Host([1.4, 3.4, 6.0, 7.9, 8.5])
    bring_up_with(host, session, VM_3GIB)
    assert host.events[:10] == [
        "room site-a,site-b",
        "read 1.4",
        "reclaim",
        "read 3.4",
        "sleep",
        "read 6.0",
        "sleep",
        "read 7.9",
        "sleep",
        "read 8.5",
    ]
    assert sorted(host.events[10:]) == ["start site-a", "start site-b"]
    for one in session.sites.values():
        assert one.error is None
        assert one.evidence["boot"] == "concurrent"
        (gate,) = json.loads((one.ctx.out / g2.VM_MEMORY_FILE).read_text(encoding="utf-8"))
        assert gate["sites"] == ["site-a", "site-b"]
        assert (gate["need_gb"], gate["before_gb"], gate["after_gb"], gate["ok"]) == (
            8.0,
            1.4,
            8.5,
            True,
        )
        assert "1.4 -> 8.5 GB" in gate["notes"][0]
        assert "waited 30s" in gate["notes"][0]


def test_vms_are_not_started_when_memory_never_comes_back(tmp_path: Path) -> None:
    """R1-1 (b): `RECLAIM_WAIT_S` 끝까지 모자라고 VM 한 대 몫(5 GB)도 안 되면 VM 시작 호출 0, 두
    현장 모두 `InfraError`(필요·실제 GB를 메시지에, 현장마다 새 객체 — B-1) — 그 현장 시험이
    infra로 다시 던진다."""
    session = session_for_bring_up(tmp_path)
    host = Host([1.6, 4.0])
    bring_up_with(host, session, VM_3GIB)
    assert not [e for e in host.events if e.startswith("start")]
    assert [e for e in host.events if e.startswith("room")] == ["room site-a,site-b"]
    assert host.now >= preflight.RECLAIM_WAIT_S
    for one in session.sites.values():
        assert isinstance(one.error, InfraError)
        assert "4.0 GB < 8.0 GB" in str(one.error)
        (gate,) = json.loads((one.ctx.out / g2.VM_MEMORY_FILE).read_text(encoding="utf-8"))
        assert (gate["before_gb"], gate["after_gb"], gate["ok"]) == (1.6, 4.0, False)
    a, b = session.sites["site-a"].error, session.sites["site-b"].error
    assert a is not b
    assert str(a) == str(b)
    with pytest.raises(InfraError):
        g2.check_install(session, "site-a", probe=lambda _: pytest.fail("must not probe"))


def test_unreadable_memory_is_short(tmp_path: Path) -> None:
    """재지 못한 값(None)은 모자란 것으로 친다(preflight `judge`와 같다)."""
    session = session_for_bring_up(tmp_path)
    host = Host([None])
    bring_up_with(host, session, VM_3GIB)
    assert not [e for e in host.events if e.startswith("start")]
    assert all(isinstance(one.error, InfraError) for one in session.sites.values())


def test_serial_mode_makes_room_for_each_vm_in_turn(tmp_path: Path) -> None:
    """R1-1 (d): 직렬 모드(`PUREIRON_G2_SERIAL=1`)는 VM마다 그 VM 몫(3 + 2 = 5 GB)으로 확인하고 바로
    그 VM을 시작한다. 두 번째 VM 몫이 끝까지 모자라면 그 VM만 시작하지 않는다(첫 현장은 그대로)."""
    session = session_for_bring_up(tmp_path)
    host = Host([4.0, 5.0, 4.5])
    bring_up_with(host, session, {**VM_3GIB, "PUREIRON_G2_SERIAL": "1"})
    starts = [e for e in host.events if e.startswith(("room", "start"))]
    assert starts == ["room site-a", "start site-a", "room site-b"]
    assert host.events[:4] == ["room site-a", "read 4.0", "reclaim", "read 5.0"]
    assert session.sites["site-a"].error is None
    error = session.sites["site-b"].error
    assert isinstance(error, InfraError)
    assert "4.5 GB < 5.0 GB" in str(error)
    gate_a = json.loads((session.sites["site-a"].ctx.out / g2.VM_MEMORY_FILE).read_text("utf-8"))[
        -1
    ]
    gate_b = json.loads((session.sites["site-b"].ctx.out / g2.VM_MEMORY_FILE).read_text("utf-8"))[
        -1
    ]
    assert (gate_a["sites"], gate_a["need_gb"], gate_a["ok"]) == (["site-a"], 5.0, True)
    assert (gate_b["sites"], gate_b["need_gb"], gate_b["ok"]) == (["site-b"], 5.0, False)


def test_serial_mode_starts_both_vms_when_each_share_fits(tmp_path: Path) -> None:
    """R1-1 (d): 직렬 모드에서 VM마다 5 GB가 되면 둘 다 차례로 시작한다(동시 모드라면 모자랄
    7 GB)."""
    session = session_for_bring_up(tmp_path)
    host = Host([7.0])
    bring_up_with(host, session, {**VM_3GIB, "PUREIRON_G2_SERIAL": "1"})
    starts = [e for e in host.events if e.startswith(("room", "start"))]
    assert starts == ["room site-a", "start site-a", "room site-b", "start site-b"]
    assert "sleep" not in host.events


def test_short_concurrent_share_switches_to_serial_when_one_vm_fits(tmp_path: Path) -> None:
    """B-3: 두 VM 몫(8 GB)이 끝내 모자라도 한 대 몫(5 GB)이 되면 직렬로 자동 전환한다 — site-a를
    그 몫으로 다시 재서 띄우고, site-b는 그 뒤 다시 잰 값(3.0 GB)이 모자라 띄우지 않는다."""
    session = session_for_bring_up(tmp_path)
    gates = iter(
        [
            g2.MemoryGate(8.0, 1.5, 6.0, ["both"]),
            g2.MemoryGate(5.0, 6.0, 6.0, ["a"]),
            g2.MemoryGate(5.0, 3.0, 3.0, ["b"]),
        ]
    )
    events: list[str] = []

    def room(runs: list[g2.SiteRun], env: Mapping[str, str]) -> g2.MemoryGate:
        events.append("room " + ",".join(r.ctx.site.site_id for r in runs))
        return next(gates)

    def up(one: g2.SiteRun, s: g2.Session, pub: str) -> None:
        events.append(f"start {one.ctx.site.site_id}")

    g2.bring_up_all(
        session, dict(VM_3GIB), up=up, prepare_host=lambda: "ssh-ed25519 AAAA test", room=room
    )
    assert events == ["room site-a,site-b", "room site-a", "start site-a", "room site-b"]
    assert session.sites["site-a"].error is None
    assert str(session.sites["site-a"].evidence["boot"]).startswith("serial (auto: 6.0 GB < 8.0 GB")
    error = session.sites["site-b"].error
    assert isinstance(error, InfraError)
    assert "3.0 GB < 5.0 GB" in str(error)


def test_concurrent_mode_stays_concurrent_when_both_fit(tmp_path: Path) -> None:
    session = session_for_bring_up(tmp_path)
    events: list[str] = []

    def room(runs: list[g2.SiteRun], env: Mapping[str, str]) -> g2.MemoryGate:
        events.append(f"room {len(runs)}")
        return g2.MemoryGate(8.0, 8.5, 8.5, [])

    g2.bring_up_all(
        session,
        dict(VM_3GIB),
        up=lambda one, s, pub: events.append("start"),
        prepare_host=lambda: "k",
        room=room,
    )
    assert events == ["room 2", "start", "start"]
