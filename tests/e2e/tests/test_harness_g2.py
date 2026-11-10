"""G2(M6-2a)를 위한 하네스 변경의 불변 조건 — 현장 둘(SITE_A·SITE_B)의 작업 디렉터리·로그·site id가
섞이지 않고, seed 기본값은 바이트까지 그대로이며, crash 창 여유 flake(E10)만 1회 다시 한다."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
import run
from harness import buildhost, golden, judges, paths, site, sites
from harness.context import Context, E2EError, InfraError
from harness.release_pair import DEFAULT
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
