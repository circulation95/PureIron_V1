"""fleet 나머지 시나리오(M6-4b-1) — G2-4 revert·G2-6 ① 이어받기·G2-W 현장 쓰기·G2-9 중앙 화면
stale의 판정(순수 함수)과 흐름(가짜 중앙·VM, 중앙 직접 PUT). 변이 대상: 롤백 `E_HEALTH`·revert
applied == gen 3·이미지 == BASE, 이어받기 바이트·`.part` 기준·nft 정리, 쓰기 꺼짐 거부·readback·
감사 행·미선언 거부, stale·online 확인·control-web 경로, 시나리오마다 c1 복원."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
import site_api
from fleet_fakes import FakeRemote
from g2_fakes import digests, scenario_session
from harness import bundles, central, fleet_scenarios, fleet_session, g2, golden, judges, site
from harness.context import Context, E2EError, InfraError
from harness.vm import Vm, VmError

MIB = 2**20
SIZE = 700 * MIB


def vm_snap(applied: int, result: str, images: dict[str, str]) -> dict[str, object]:
    return {
        "updater": {"applied_gen": applied, "last_result": result, "attempt": None, "alarm": None},
        "containers": {s: {"image_id": d, "running": True} for s, d in images.items()},
        "project": {"status": 200, "body": {"edit_locked": False, "content_hash": "sha256:c"}},
        "readyz": {"status": 200},
    }


def vm_report(result: str, code: str | None = None) -> dict[str, object]:
    return {"result": result, "code": code, "failed_manual": False, "logs": "updater log"}


def fleet_state(s: g2.Session) -> fleet_session.FleetSession:
    return fleet_session.FleetSession(
        run_id=s.run_id,
        c1_name="c1-x",
        ca=Path("ca"),
        ca_sha256="AB",
        c1_fleet={"site-a": {"max_published_gen": 0}, "site-b": {"max_published_gen": 0}},
        c1_nft={"site-a": {}, "site-b": {}},
    )


def nft_kind(script: str) -> str:
    """`central_filter` 스크립트의 종류(시험 기록용)."""
    if script == fleet_scenarios.CLEAR_FILTER:
        return "clear"
    if "limit rate over" in script:
        return "limit"
    return "drop" if script.rstrip().count(" drop") else "count"


@dataclass
class Flow:
    """가짜 중앙(`FakeRemote`) + 가짜 VM(적용·nft·`.part`·쓰기 API) 위의 fleet 시나리오."""

    session: g2.Session
    fake: FakeRemote
    scenario: fleet_scenarios.Scenario = field(init=False)
    events: list[str] = field(default_factory=list[str])
    restores: list[str] = field(default_factory=list[str])
    builds: list[tuple[str, int, str, Path, bool]] = field(
        default_factory=list[tuple[str, int, str, Path, bool]]
    )
    built: dict[tuple[str, int], tuple[str, bool]] = field(
        default_factory=dict[tuple[str, int], tuple[str, bool]]
    )
    vm: dict[str, tuple[int, str, str]] = field(default_factory=dict[str, tuple[int, str, str]])
    """현장 → (applied gen, 실행 이미지 release, 마지막 결과)."""
    reports: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    parts: list[int | None] = field(default_factory=list[int | None])
    failures: list[int] = field(default_factory=lambda: [0, 1])
    received: int = SIZE - 150 * MIB + 6 * MIB
    """해제 뒤 nft 카운터 — 기본은 `.part`(150 MiB)를 잇고 헤더 몫이 얹힌 값."""
    writes_on: dict[str, bool] = field(default_factory=dict[str, bool])
    memory: dict[str, float] = field(default_factory=lambda: {fleet_scenarios.WRITE_TAG: 0.0})
    audit: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    transient: list[list[str]] = field(default_factory=list[list[str]])
    """다음 쓰기들이 차례로 받을 일시 거부(실패 검사 이름 — 비면 정상 처리)."""
    ui_status: int = 200
    """control-web 경로 응답 상태(401·502 = 행 없음)."""
    stale_after: int = 2
    """차단 뒤 몇 번째 control-web 읽기부터 stale인가(0 = 바뀌지 않음)."""
    online_after: int = 1

    def site_of(self, host: Vm) -> str:
        return next(s for s, r in self.session.sites.items() if r.ctx.site.ip == host.ip)

    def images(self, release: str) -> dict[str, str]:
        return digests(release)


def flow(tmp_path: Path) -> Flow:
    s = scenario_session(tmp_path)
    fake = FakeRemote(s)
    f = Flow(s, fake)
    pair = s.pair
    for site_id, run in s.sites.items():
        f.vm[site_id] = (fleet_session.C0_GEN, run.release, "succeeded")

    def restore(
        session: g2.Session, fs: object, sites: tuple[str, ...], **_: object
    ) -> dict[str, object]:
        f.restores.append(",".join(sites))
        f.events.append("restore " + ",".join(sites))
        return {}

    def build(
        ctx: Context,
        gen: int,
        release: str,
        window: tuple[datetime, datetime],
        tree: Path,
        *,
        writes: bool = False,
    ) -> Path:
        site_id = ctx.site.site_id
        f.builds.append((site_id, gen, release, tree, writes))
        f.built[(site_id, gen)] = (release, writes)
        f.events.append(f"build {site_id} g{gen} {release}")
        return tmp_path / f"pureiron-{site_id}-g{gen}-{release}.pib"

    def probe(host: Vm) -> dict[str, object]:
        applied, images, result = f.vm[f.site_of(host)]
        return vm_snap(applied, result, f.images(images))

    def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
        site_id = f.site_of(host)
        f.events.append(f"converge {site_id} g{gen}")
        applied, images, _ = f.vm[site_id]
        release, writes = f.built[(site_id, gen)]
        if release == pair.fault:  # FAULT-h: health 실패 → 자동 롤백(이미지·applied 그대로)
            f.vm[site_id] = (applied, images, "failed")
            f.reports[f"g{gen}"] = vm_report("failed", judges.FAULT_CODE)
        else:
            f.vm[site_id] = (gen, release, "succeeded")
            f.reports[f"g{gen}"] = vm_report("succeeded")
            f.writes_on[site_id] = writes
            fake.current[site_id] = gen
            fake.fleet_rows[site_id] = {
                **fake.fleet_rows.get(site_id, {}),
                "current": {"gen": gen, "product": pair.releases[release]},
            }
        return probe(host), 40.0

    def report(host: Vm, pib: Path) -> dict[str, object]:
        gen = pib.name.split("-g")[1].split("-")[0]
        return f.reports.get(f"g{gen}", {})

    def nft(host: Vm, script: str) -> None:
        kind = nft_kind(script)
        f.events.append(f"nft {kind}")
        site_id = f.site_of(host)
        blocked = kind == "drop"
        fake.fleet_rows[site_id] = {
            **fake.fleet_rows.get(site_id, {}),
            "blocked": blocked,
            "reads": 0,
        }

    def part_bytes(host: Vm) -> int | None:
        part = f.parts.pop(0) if len(f.parts) > 1 else f.parts[0]
        f.events.append(f"part {part}")
        return part

    def poll_failures(host: Vm, since: float) -> int:
        return f.failures.pop(0) if len(f.failures) > 1 else f.failures[0]

    def add_user(host: Vm, name: str, role: str, password: str) -> dict[str, object]:
        f.events.append(f"user {name} {role}")
        return {"rc": 0, "stderr": ""}

    def write_tag(host: Vm, tag: str, value: float, reason: str) -> dict[str, object]:
        f.events.append(f"write {tag} {value}")
        declared = tag == fleet_scenarios.WRITE_TAG
        enabled = f.writes_on.get(f.site_of(host), False)
        failed = [n for n, ok in (("writes_enabled", enabled), ("declared", declared)) if not ok]
        if f.transient:
            failed = sorted({*failed, *f.transient.pop(0)})
        out: dict[str, object] = {"login": [200, {"user": "e2e-engineer"}], "confirm": None}
        if failed:
            checks = [{"name": n, "ok": False, "detail": "no"} for n in failed]
            out["prepare"] = [409, {"detail": "write rejected by checks", "checks": checks}]
            f.audit.append(
                {
                    "action": judges.WRITE_REJECTED_ACTION,
                    "target": tag,
                    "detail": {"reason": reason, "failed": failed, "outcome": "REJECTED"},
                }
            )
        else:
            out["prepare"] = [200, {"write_id": "w1"}]
            f.memory[tag] = value
            out["confirm"] = [200, {"write_id": "w1", "outcome": "CONFIRMED", "readback": value}]
            f.audit.append(
                {
                    "action": judges.WRITE_ACTION,
                    "target": tag,
                    "detail": {"reason": reason, "write_id": "w1", "outcome": "CONFIRMED"},
                }
            )
        out["audit"] = [r for r in f.audit if judges.sub(r, "detail").get("reason") == reason]
        return out

    def live(host: Vm) -> dict[str, object]:
        rows = [{"path": tag, "quality": "GOOD", "value": value} for tag, value in f.memory.items()]
        rows.append({"path": fleet_scenarios.UNDECLARED_TAG, "quality": "GOOD", "value": 1003.0})
        return {"tags": [200, {"tags": rows}]}

    central = fake.central

    def received(host: Vm) -> int:
        """해제(`nft count`) 뒤 수렴(`converge`)이 끝나야 바이트가 쌓인다 — 그 전에 읽으면 0."""
        marks = [i for i, e in enumerate(f.events) if e == "nft count"]
        done = marks and any(e.startswith("converge") for e in f.events[marks[-1] :])
        return f.received if done else 0

    def central_view(*args: str) -> dict[str, object]:
        """control-web 경로 읽기: 차단 뒤 `stale_after`번째부터 stale, 해제 뒤 `online_after`번째
        부터 online(0이면 바뀌지 않는다)."""
        if args[:2] == ("fleet", "--ui"):
            f.events.append("ui-read")
            for site_id, row in fake.fleet_rows.items():
                if "blocked" not in row:
                    continue
                reads = cast(int, row["reads"]) + 1
                limit = f.stale_after if row["blocked"] else f.online_after
                changed = limit and reads >= limit
                state = ("stale" if row["blocked"] else "online") if changed else None
                fake.fleet_rows[site_id] = {
                    **row,
                    "reads": reads,
                    **({"connection": state} if state else {}),
                }
            if f.ui_status != 200:
                return {"status": f.ui_status, "body": "bad gateway", "via": "caddy-ui"}
        elif args[0] == "fleet":
            f.events.append(f"read {' '.join(args[1:])}")
        return central(*args)

    remote = dataclasses.replace(
        fake.remote(),
        central=central_view,
        probe=probe,
        converge=converge,
        nft=nft,
        part_bytes=part_bytes,
        poll_failures=poll_failures,
        central_in_bytes=received,
        add_user=add_user,
        write_tag=write_tag,
        live=live,
    )
    f.scenario = fleet_scenarios.Scenario(remote, restore=restore, build=build, report=report)
    return f


def failed(checks: dict[str, bool]) -> list[str]:
    return sorted(k for k, ok in checks.items() if not ok)


# ---------------------------------------------------------------- G2-4 revert


def test_revert_publishes_fault_then_base_and_passes(tmp_path: Path) -> None:
    f = flow(tmp_path)
    s, pair = f.session, f.session.pair
    checks = fleet_scenarios.revert(s, fleet_state(s), f.scenario)
    assert f.restores == ["site-a"]  # 시나리오가 c1 복원으로 시작한다
    assert f.events[0] == "restore site-a"
    assert all(checks.values()), failed(checks)
    assert tuple(checks) == fleet_scenarios.G2_4_REVERT_ITEMS
    # FAULT-h g2(FAULT 트리) → BASE g3(BASE = G2 트리), 둘 다 쓰기 스위치 없음
    assert f.builds == [
        ("site-a", 2, pair.fault, s.tree_of(pair.fault), False),
        ("site-a", 3, pair.base, s.tree, False),
    ]
    puts = [e for e in f.fake.events if e.startswith("central put")]
    assert puts == ["central put --site site-a", "central put --site site-a"]
    record = next(
        r for r in s.sites["site-a"].ctx.results if r["name"] == fleet_scenarios.ITEM_G2_4_REVERT
    )
    assert (record["fault_gen"], record["revert_gen"], record["fault_code"]) == (2, 3, "E_HEALTH")


BASE = digests("r5")
NEXT = digests("n-next")


def revert_judge(**change: object) -> dict[str, bool]:
    put: dict[str, object] = {"status": 201}
    fault = (
        put,
        cast(dict[str, object], change.get("fault_snap") or vm_snap(1, "failed", BASE)),
        cast(dict[str, object], change.get("fault_report") or vm_report("failed", "E_HEALTH")),
    )
    again = (
        put,
        cast(dict[str, object], change.get("revert_snap") or vm_snap(3, "succeeded", BASE)),
        vm_report("succeeded"),
    )
    row = cast(dict[str, object], change.get("row") or {"current": {"gen": 3, "product": "B"}})
    return judges.judge_revert(
        fault, again, row, c1_applied=1, revert_gen=3, base_digests=BASE, base_version="B"
    )


@pytest.mark.parametrize(
    ("change", "broken"),
    [
        ({"fault_report": vm_report("failed", "E_MIGRATE")}, ["rollback_code"]),  # E_HEALTH 아님
        ({"fault_snap": vm_snap(1, "failed", NEXT)}, ["rollback_images_match"]),  # BASE로 안 돌아옴
        (
            {"fault_snap": vm_snap(2, "succeeded", BASE)},
            ["rollback_applied_gen", "rollback_result"],
        ),
        ({"revert_snap": vm_snap(2, "succeeded", BASE)}, ["revert_applied_gen"]),  # gen 3 아님
        ({"revert_snap": vm_snap(3, "succeeded", NEXT)}, ["revert_images_match"]),  # BASE 아님
        ({"row": {"current": {"gen": 2, "product": "B"}}}, ["fleet_current"]),
    ],
)
def test_revert_judge_each_check_matters(change: dict[str, object], broken: list[str]) -> None:
    assert all(revert_judge().values())
    assert failed(revert_judge(**change)) == broken


def test_a_base_republish_that_does_not_apply_is_judged_not_waited_for(tmp_path: Path) -> None:
    """BASE 다시 게시가 적용되지 않으면(제품) 중앙 current를 기다리지 않는다 — 대기 시간 초과
    (infra)로 바뀌지 않고 판정이 거짓이다."""
    f = flow(tmp_path)
    converge = f.scenario.remote.converge

    def base_fails(host: Vm, gen: int) -> tuple[dict[str, object], float]:
        snap, took = converge(host, gen)
        if gen == 3:
            f.vm["site-a"] = (1, f.session.pair.base, "failed")
            f.fake.current["site-a"] = 1
            f.fake.fleet_rows["site-a"] = {}
            return vm_snap(1, "failed", BASE), took
        return snap, took

    remote = dataclasses.replace(f.scenario.remote, converge=base_fails)
    sc = dataclasses.replace(f.scenario, remote=remote)
    checks = fleet_scenarios.revert(f.session, fleet_state(f.session), sc)
    assert {"revert_result", "revert_applied_gen", "fleet_current"} <= set(failed(checks))


# ---------------------------------------------------------------- G2-6 ① 이어받기


def resume_flow(tmp_path: Path) -> Flow:
    f = flow(tmp_path)
    f.parts = [None, 50 * MIB, 140 * MIB, 150 * MIB]
    return f


def test_resume_blocks_after_the_part_passes_the_mark_and_judges_the_resume(
    tmp_path: Path,
) -> None:
    f = resume_flow(tmp_path)
    s = f.session
    checks = fleet_scenarios.resume(s, fleet_state(s), f.scenario)
    assert f.restores == ["site-a"]
    assert all(checks.values()), failed(checks)
    assert tuple(checks) == fleet_scenarios.G2_6_1_ITEMS
    nft = [e for e in f.events if e.startswith("nft")]
    assert nft == ["nft limit", "nft drop", "nft count", "nft clear"]
    # 속도 제한이 PUT(빌드) 전에, 전부 차단은 `.part`가 기준(128 MiB)을 넘은 읽기 뒤에
    assert f.events.index("nft limit") < f.events.index("build site-a g2 " + s.pair.next)
    assert f.events.index(f"part {140 * MIB}") < f.events.index("nft drop")
    record = next(
        r for r in s.sites["site-a"].ctx.results if r["name"] == fleet_scenarios.ITEM_G2_6_1
    )
    assert (record["part_at_block"], record["part_at_release"]) == (140 * MIB, 150 * MIB)
    assert record["received_after_release"] == f.received
    assert record["size"] == SIZE


def test_a_download_that_starts_over_is_not_a_resume(tmp_path: Path) -> None:
    """해제 뒤 번들 전체(+ 헤더 몫)를 받았다면 처음부터 받은 것이다."""
    f = resume_flow(tmp_path)
    f.received = SIZE + 25 * MIB
    checks = fleet_scenarios.resume(f.session, fleet_state(f.session), f.scenario)
    assert failed(checks) == ["resumed"]


def test_a_part_that_never_reaches_the_mark_is_infra_and_the_filter_is_cleared(
    tmp_path: Path,
) -> None:
    f = resume_flow(tmp_path)
    f.parts = [10 * MIB]
    with pytest.raises(InfraError, match="did not pass"):
        fleet_scenarios.resume(f.session, fleet_state(f.session), f.scenario)
    nft = [e for e in f.events if e.startswith("nft")]
    assert nft == ["nft limit", "nft clear"]  # 기준 전에는 차단하지 않고, 필터는 지운다


def test_a_part_that_vanishes_before_the_block_is_infra(tmp_path: Path) -> None:
    f = resume_flow(tmp_path)
    f.parts = [50 * MIB, None]
    with pytest.raises(InfraError, match="vanished"):
        fleet_scenarios.resume(f.session, fleet_state(f.session), f.scenario)
    assert f.events[-1] == "nft clear"


def test_the_filter_is_cleared_when_the_scenario_breaks(tmp_path: Path) -> None:
    f = resume_flow(tmp_path)

    def broken(host: Vm, gen: int) -> tuple[dict[str, object], float]:
        raise VmError("ssh 255")

    sc = dataclasses.replace(
        f.scenario, remote=dataclasses.replace(f.scenario.remote, converge=broken)
    )
    with pytest.raises(VmError):
        fleet_scenarios.resume(f.session, fleet_state(f.session), sc)
    assert [e for e in f.events if e.startswith("nft")][-1] == "nft clear"


def resume_judge(**change: object) -> dict[str, bool]:
    values: dict[str, Any] = {
        "gen": 2,
        "next_digests": NEXT,
        "part_at_block": 140 * MIB,
        "part_at_release": 150 * MIB,
        "poll_failures": 1,
        "received": SIZE - 150 * MIB + 6 * MIB,
        "size": SIZE,
        "converge_s": 90.0,
        **change,
    }
    return judges.judge_resume({"status": 201}, vm_snap(2, "succeeded", NEXT), **values)


@pytest.mark.parametrize(
    ("change", "broken"),
    [
        ({"poll_failures": 0}, ["interrupted_logged"]),
        ({"part_at_release": 100 * MIB}, ["part_kept"]),  # 차단 중에 줄었다(지워지고 다시 받음)
        ({"part_at_release": None}, ["part_kept", "resumed"]),
        ({"received": SIZE - 74 * MIB}, ["resumed"]),  # 번들 - .part/2 이상
        ({"received": -1}, ["resumed"]),  # 카운터를 못 읽음
        ({"size": -1}, ["resumed"]),
        ({"converge_s": 600.5}, ["within_10min"]),
    ],
)
def test_resume_judge_each_check_matters(change: dict[str, object], broken: list[str]) -> None:
    assert all(resume_judge().values())
    assert failed(resume_judge(**change)) == broken


def test_the_central_filter_scripts() -> None:
    """한 표(`e2e_fleet`)를 통째로 바꾼다: 카운터 규칙이 늘 먼저(표를 다시 만들 때 0), 그 뒤 차단·
    속도 제한. 출발지는 중앙 주소·포트뿐이다(ssh는 건드리지 않는다)."""
    match = "ip saddr 10.77.0.1 tcp sport 28443"
    count = fleet_scenarios.central_filter(None)
    drop = fleet_scenarios.central_filter("drop")
    limit = fleet_scenarios.central_filter(fleet_scenarios.RESUME_LIMIT)
    for script in (count, drop, limit):
        assert script.startswith(fleet_scenarios.CLEAR_FILTER)
        assert f'{match} counter comment "central_in"' in script
        assert "type filter hook input priority -5; policy accept;" in script
    assert f"{match} drop" in drop
    assert f"{match} limit rate over 4096 kbytes/second drop" in limit
    assert " drop" not in count
    assert fleet_scenarios.CLEAR_FILTER == "table inet e2e_fleet\ndelete table inet e2e_fleet\n"


def test_central_in_bytes_reads_the_counter_bytes() -> None:
    listing = {
        "nftables": [
            {"table": {"name": "e2e_fleet"}},
            {"rule": {"comment": "central_in", "expr": [{"counter": {"packets": 9, "bytes": 77}}]}},
        ]
    }

    class Host:
        def run(self, command: str, **_: object) -> Any:
            assert command == "sudo nft -j list table inet e2e_fleet"
            return argparse.Namespace(stdout=json.dumps(listing))

    assert site.central_in_bytes(cast(Vm, Host())) == 77
    listing["nftables"] = []
    assert site.central_in_bytes(cast(Vm, Host())) == -1


# ---------------------------------------------------------------- G2-W 현장 쓰기


def test_writes_flow_rejects_while_off_then_writes_after_enabling(tmp_path: Path) -> None:
    f = flow(tmp_path)
    s = f.session
    checks = fleet_scenarios.writes(s, fleet_state(s), f.scenario)
    assert f.restores == ["site-b"]
    assert all(checks.values()), failed(checks)
    assert tuple(checks) == fleet_scenarios.G2_W_ITEMS
    # c1과 같은 G2 트리 + 쓰기 스위치, NEXT gen 2
    assert f.builds == [("site-b", 2, s.pair.next, s.tree, True)]
    writes = [e for e in f.events if e.startswith(("write", "user", "build"))]
    assert writes == [
        "user e2e-engineer engineer",
        "write line1/temp_sp 77.5",  # 스위치 꺼짐
        f"build site-b g2 {s.pair.next}",
        "write line1/temp_sp 42.5",  # 켠 뒤 선언 태그
        "write sim/r001 7.0",  # 미선언
    ]
    assert f.memory[fleet_scenarios.WRITE_TAG] == 42.5


def test_writes_picks_a_value_other_than_the_c1_value(tmp_path: Path) -> None:
    f = flow(tmp_path)
    f.memory[fleet_scenarios.WRITE_TAG] = 42.5
    checks = fleet_scenarios.writes(f.session, fleet_state(f.session), f.scenario)
    assert all(checks.values()), failed(checks)
    assert "write line1/temp_sp 57.5" in f.events


TAG, UNDECLARED = fleet_scenarios.WRITE_TAG, fleet_scenarios.UNDECLARED_TAG


def rejected(tag: str, check: str, *, audited: bool = True) -> dict[str, object]:
    checks = [{"name": check, "ok": False, "detail": "x"}, {"name": "role", "ok": True}]
    detail = {"failed": [check], "reason": "r"}
    rows = [{"action": "tag.write_rejected", "target": tag, "detail": detail}] if audited else []
    return {"prepare": [409, {"checks": checks}], "confirm": None, "audit": rows}


def confirmed(
    value: float, *, readback: float | None = None, audited: bool = True
) -> dict[str, object]:
    result = {"write_id": "w9", "outcome": "CONFIRMED", "readback": readback or value}
    rows = [
        {"action": "tag.write", "target": TAG, "detail": {"write_id": "w9", "outcome": "SENDING"}},
        {
            "action": "tag.write",
            "target": TAG,
            "detail": {"write_id": "w9", "outcome": "CONFIRMED"},
        },
    ]
    return {
        "prepare": [200, {"write_id": "w9"}],
        "confirm": [200, result],
        "audit": rows if audited else rows[:1],
    }


def writes_judge(**change: object) -> dict[str, bool]:
    parts: dict[str, Any] = {
        "off": rejected(TAG, "writes_enabled"),
        "on": confirmed(42.5),
        "undeclared": rejected(UNDECLARED, "declared"),
        "seen": (0.0, 0.0, 42.5),
        **change,
    }
    return judges.judge_writes(
        parts["off"],
        ({"status": 201}, vm_snap(2, "succeeded", NEXT)),
        parts["on"],
        parts["undeclared"],
        tags=(TAG, UNDECLARED),
        on_value=42.5,
        seen=parts["seen"],
        gen=2,
    )


@pytest.mark.parametrize(
    ("change", "broken"),
    [
        # 스위치가 꺼졌는데 prepare가 통과
        ({"off": confirmed(77.5)}, ["off_audited", "off_rejected"]),
        # 꺼짐 거부가 다른 검사 때문(스위치를 안 봄)
        ({"off": rejected(TAG, "tag_good")}, ["off_audited", "off_rejected"]),
        ({"off": rejected(TAG, "writes_enabled", audited=False)}, ["off_audited"]),
        ({"seen": (0.0, 77.5, 42.5)}, ["off_value_unchanged"]),
        ({"seen": (None, None, 42.5)}, ["off_value_unchanged"]),
        ({"on": confirmed(42.5, readback=41.0)}, ["on_readback"]),
        ({"on": confirmed(42.5, audited=False)}, ["on_audited"]),
        ({"on": rejected(TAG, "plan_reported")}, ["on_audited", "on_confirmed", "on_readback"]),
        ({"seen": (0.0, 0.0, 0.0)}, ["on_tag_value"]),
        ({"undeclared": confirmed(7.0)}, ["undeclared_audited", "undeclared_rejected"]),
        ({"undeclared": rejected(UNDECLARED, "declared", audited=False)}, ["undeclared_audited"]),
    ],
)
def test_writes_judge_each_check_matters(change: dict[str, object], broken: list[str]) -> None:
    assert all(writes_judge().values())
    assert failed(writes_judge(**change)) == broken


def test_site_api_write_prepares_confirms_and_reads_its_own_audit_rows(tmp_path: Path) -> None:
    password = tmp_path / "pw"
    password.write_text("secret\n", encoding="utf-8")
    calls: list[tuple[str, str, object]] = []
    entries = [
        {"action": "tag.write", "detail": {"reason": "mine", "outcome": "CONFIRMED"}},
        {"action": "tag.write", "detail": {"reason": "older"}},
    ]

    timeouts: dict[str, float] = {}

    def call(
        method: str,
        path: str,
        body: object | None = None,
        cookie: str | None = None,
        timeout: float = 10,
    ) -> tuple[int, object, list[str]]:
        calls.append((method, path, body))
        timeouts[path] = timeout
        if path.endswith("/login"):
            return 200, {"user": {"name": "e2e-engineer"}}, ["pureiron_session=tok; Path=/"]
        if path == "/api/v1/writes":
            return 200, {"write_id": "ab12"}, []
        if path.startswith("/api/v1/audit"):
            action = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["action"][0]
            return 200, {"entries": [e for e in entries if e["action"] == action]}, []
        return 200, {"outcome": "CONFIRMED"}, []

    args = argparse.Namespace(
        username="e2e-engineer",
        password_file=str(password),
        tag="line1/temp_sp",
        value=42.5,
        reason="mine",
    )
    out = site_api.write(args, call)
    assert [(m, p) for m, p, _ in calls] == [
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/writes"),
        ("POST", "/api/v1/writes/ab12/confirm"),
        ("GET", "/api/v1/audit?action=tag.write"),
        ("GET", "/api/v1/audit?action=tag.write_rejected"),
        ("POST", "/api/v1/auth/logout"),
    ]
    assert calls[1][2] == {"tag_path": "line1/temp_sp", "value": 42.5, "reason": "mine"}
    # confirm만 core 쓰기 시한(10초)보다 길게 기다린다(fix r1 L3 — NO_RESPONSE를 받는다)
    assert timeouts["/api/v1/writes/ab12/confirm"] == site_api.CONFIRM_TIMEOUT_S == 30
    assert timeouts["/api/v1/writes"] == 10
    # VM에서는 JSON 한 줄로 나가 목록이 된다(`judges.reply`가 읽는 모양)
    assert json.loads(json.dumps(out))["confirm"] == [200, {"outcome": "CONFIRMED"}]
    assert out["audit"] == [entries[0]]  # 이 사유의 행만
    assert "secret" not in json.dumps(out)


def test_site_api_write_does_not_confirm_a_rejected_prepare(tmp_path: Path) -> None:
    password = tmp_path / "pw"
    password.write_text("secret\n", encoding="utf-8")
    paths: list[str] = []

    def call(
        method: str, path: str, body: object | None = None, cookie: str | None = None
    ) -> tuple[int, object, list[str]]:
        paths.append(path)
        if path.endswith("/login"):
            return 200, {"user": {}}, ["pureiron_session=tok"]
        if path == "/api/v1/writes":
            return 409, {"checks": [{"name": "writes_enabled", "ok": False}]}, []
        return 200, {"entries": []}, []

    args = argparse.Namespace(
        username="u", password_file=str(password), tag="t", value=1.0, reason="r"
    )
    out = site_api.write(args, call)
    assert out["confirm"] is None
    assert not any("/confirm" in p for p in paths)


def test_the_writes_switch_lands_in_the_fleet_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_run(*cmd: str, **kwargs: object) -> None:
        del cmd, kwargs

    monkeypatch.setattr(bundles, "_run", no_run)
    common: dict[str, Any] = {
        "gen": 2,
        "product": "2026.11.2",
        "start": "2026-11-15T00:00:00Z",
        "end": "2026-11-15T01:00:00Z",
        "site_id": "site-b",
    }
    bundles.site(tmp_path, tmp_path / "out", **common, writes_enabled=True)
    text = (tmp_path / "sites" / "site-b-g2.yaml").read_text(encoding="utf-8")
    assert text.endswith("writes: { enabled: true }\n")
    bundles.site(tmp_path, tmp_path / "out", **common)
    assert "writes" not in (tmp_path / "sites" / "site-b-g2.yaml").read_text(encoding="utf-8")


# ---------------------------------------------------------------- G2-9 중앙 화면 stale


def test_fleet_view_goes_stale_and_back_through_the_control_web_path(tmp_path: Path) -> None:
    f = flow(tmp_path)
    s = f.session
    checks = fleet_scenarios.fleet_view(s, fleet_state(s), f.scenario)
    assert f.restores == ["site-b"]
    assert all(checks.values()), failed(checks)
    assert tuple(checks) == fleet_scenarios.G2_9_ITEMS
    assert [e for e in f.events if e.startswith("nft")] == ["nft drop", "nft clear"]
    # 판정 읽기는 모두 control-web 경로(Caddy basic auth) — control-api 직접·publish 토큰 경로 없음
    reads = [e for e in f.events if e == "ui-read" or e.startswith("read ")]
    assert reads
    assert set(reads) == {"ui-read"}
    record = next(
        r for r in s.sites["site-b"].ctx.results if r["name"] == fleet_scenarios.ITEM_G2_9
    )
    # 가짜 시계는 부를 때마다 1초 — 차단 뒤 둘째 읽기에서 stale, 해제 뒤 첫 읽기에서 online
    assert (record["block_to_stale_s"], record["release_to_online_s"]) == (2.0, 1.0)
    assert record["via"] == "caddy-ui"


def test_a_site_that_never_goes_stale_fails_g2_9_and_is_still_released(tmp_path: Path) -> None:
    f = flow(tmp_path)
    f.stale_after = 0
    checks = fleet_scenarios.fleet_view(f.session, fleet_state(f.session), f.scenario)
    assert failed(checks) == ["stale_after_block"]
    assert [e for e in f.events if e.startswith("nft")] == ["nft drop", "nft clear"]


def test_a_site_that_never_comes_back_fails_g2_9(tmp_path: Path) -> None:
    f = flow(tmp_path)
    f.online_after = 0
    f.fake.fleet_rows["site-b"] = {"connection": "online"}
    checks = fleet_scenarios.fleet_view(f.session, fleet_state(f.session), f.scenario)
    assert failed(checks) == ["online_after_release"]


def test_a_block_that_cannot_be_lifted_is_infra(tmp_path: Path) -> None:
    f = flow(tmp_path)
    nft = f.scenario.remote.nft

    def stuck(host: Vm, script: str) -> None:
        if script == fleet_scenarios.CLEAR_FILTER:
            raise VmError("ssh 255")
        nft(host, script)

    sc = dataclasses.replace(f.scenario, remote=dataclasses.replace(f.scenario.remote, nft=stuck))
    with pytest.raises(InfraError, match="could not lift"):
        fleet_scenarios.fleet_view(f.session, fleet_state(f.session), sc)


@pytest.mark.parametrize(
    ("first", "stale_s", "online_s", "broken"),
    [
        ({"via": "control-api"}, 30.0, 10.0, ["via_control_web"]),  # control-api 직접 경로
        ({"via": "caddy"}, 30.0, 10.0, ["via_control_web"]),  # publish 토큰 경로
        ({"status": 401}, 30.0, 10.0, ["online_before", "via_control_web"]),
        ({"connection": "stale"}, 30.0, 10.0, ["online_before"]),
        ({}, None, 10.0, ["stale_after_block"]),
        ({}, 120.5, 10.0, ["stale_after_block"]),
        ({}, 30.0, None, ["online_after_release"]),
        ({}, 30.0, 60.5, ["online_after_release"]),
    ],
)
def test_fleet_view_judge_each_check_matters(
    first: dict[str, object], stale_s: float | None, online_s: float | None, broken: list[str]
) -> None:
    def judged(change: dict[str, object], stale: float | None, online: float | None) -> list[str]:
        row = {"site": "site-b", "connection": change.get("connection", "online")}
        reply = {"status": change.get("status", 200), "body": [row], "via": "caddy-ui"}
        reply |= {k: v for k, v in change.items() if k == "via"}
        checks = judges.judge_fleet_view(
            reply, "site-b", stale, online, stale_limit_s=120.0, online_limit_s=60.0
        )
        return failed(checks)

    assert judged({}, 30.0, 10.0) == []
    assert judged(first, stale_s, online_s) == broken


def test_central_reads_the_fleet_like_control_web(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fleet --ui`: Caddy basic auth(`ops` + 비밀 파일의 원문)로 `/ui/fleet`을 읽는다 — 토큰을
    붙이지 않는다(Caddy가 viewer 토큰을 붙인다). 비밀번호는 응답에 없다."""
    seen: dict[str, object] = {}

    class Response:
        status = 200

        def read(self) -> bytes:
            return b'[{"site": "site-b", "connection": "online"}]'

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def urlopen(request: Any, **kwargs: object) -> Response:
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return Response()

    def read_secret(name: str) -> str:
        return {"ui_password": "pw-1"}[name]

    monkeypatch.setattr(central, "read_secret", read_secret)
    monkeypatch.setattr(central, "context", lambda: None)
    monkeypatch.setattr(central.urllib.request, "urlopen", urlopen)
    out = central.fleet_ui()
    assert seen["url"] == "https://10.77.0.1:28443/ui/fleet"
    assert seen["auth"] == "Basic b3BzOnB3LTE="  # base64("ops:pw-1")
    assert out == {
        "status": 200,
        "body": [{"site": "site-b", "connection": "online"}],
        "via": "caddy-ui",
    }
    args = argparse.Namespace(command="fleet", ui=True, direct=False, site=None)
    assert central.run_command(args)["via"] == "caddy-ui"


# ---------------------------------------------------------------- fix r1


def test_a_counter_read_before_the_download_ends_is_not_a_resume(tmp_path: Path) -> None:
    """리뷰 M3(M1 변이): 해제 직후(수렴 전) 카운터를 읽으면 0 바이트라 처음부터 받아도 이어받기로
    보인다 — 가짜 카운터가 수렴 뒤에만 바이트를 내므로 시나리오가 수렴 뒤에 읽는지 본다."""
    f = resume_flow(tmp_path)
    checks = fleet_scenarios.resume(f.session, fleet_state(f.session), f.scenario)
    assert checks["resumed"] is True
    record = next(
        r for r in f.session.sites["site-a"].ctx.results if r["name"] == fleet_scenarios.ITEM_G2_6_1
    )
    assert record["received_after_release"] == f.received  # 0이 아니다
    # 차단 → 첫 poll failed까지 초(fix r1 L9) — 가짜 시계는 부를 때마다 1초, 둘째 읽기에서 보인다
    assert record["block_to_failure_s"] == 2.0


def test_an_ssh_drop_while_waiting_for_the_part_is_asked_again(tmp_path: Path) -> None:
    """fix r1 L1: `.part` 대기 중 ssh가 한 번 끊겨도(VmError) 다시 묻는다."""
    f = resume_flow(tmp_path)
    part_bytes = f.scenario.remote.part_bytes
    dropped: list[str] = []

    def flaky(host: Vm) -> int | None:
        if not dropped:
            dropped.append("x")
            raise VmError("ssh 255")
        return part_bytes(host)

    remote = dataclasses.replace(f.scenario.remote, part_bytes=flaky)
    checks = fleet_scenarios.resume(
        f.session, fleet_state(f.session), dataclasses.replace(f.scenario, remote=remote)
    )
    assert all(checks.values()), failed(checks)
    assert dropped == ["x"]


@pytest.mark.parametrize("helper", ["part_bytes", "poll_failures"])
def test_an_ssh_failure_is_not_read_as_no_part_or_no_failures(helper: str) -> None:
    """fix r1 L1: `check=False` 원격 명령도 ssh 자체 실패(255)는 VmError(infra) — "파일 없음"·
    "0줄"(제품 판정 거짓)로 읽지 않는다."""

    class Host:
        ip = "10.77.0.11"

        def __init__(self, rc: int, out: str) -> None:
            self.rc, self.out = rc, out

        def run(self, command: str, **_: object) -> Any:
            return argparse.Namespace(returncode=self.rc, stdout=self.out, stderr="reset")

    def call(host: Host) -> object:
        vm = cast(Vm, host)
        return site.part_bytes(vm) if helper == "part_bytes" else site.poll_failures(vm, 1.0)

    with pytest.raises(VmError, match="255"):
        call(Host(255, ""))
    assert call(Host(0, "3")) == 3
    assert call(Host(1, "")) in (None, 0)  # grep 0줄(1)·파일 없음은 그대로


def test_writes_retries_a_write_rejected_only_by_transient_checks(tmp_path: Path) -> None:
    """fix r1 M1: 켠 뒤 쓰기가 일시 검사(`plan_reported` — replay batch가 GOOD을 만들지만 plan
    보고는 아직)로 거부되면 5초 간격으로 다시 묻는다. 꺼짐 쪽도 [writes_enabled + tag_good]이면
    다시 물어 [writes_enabled]만 남을 때 판정한다. 판정·감사 행 고르기는 그대로 맞다."""
    f = flow(tmp_path)
    f.transient = [["tag_good"], [], ["plan_reported"], ["plan_reported", "tag_good"]]
    checks = fleet_scenarios.writes(f.session, fleet_state(f.session), f.scenario)
    assert all(checks.values()), failed(checks)
    writes = [e for e in f.events if e.startswith("write ")]
    assert writes == [
        "write line1/temp_sp 77.5",
        "write line1/temp_sp 77.5",  # 꺼짐: [writes_enabled, tag_good] → 다시
        "write line1/temp_sp 42.5",
        "write line1/temp_sp 42.5",
        "write line1/temp_sp 42.5",  # 켬: plan_reported, plan_reported+tag_good → 다시
        "write sim/r001 7.0",
    ]
    record = next(
        r for r in f.session.sites["site-b"].ctx.results if r["name"] == fleet_scenarios.ITEM_G2_W
    )
    assert record["tries"] == {"off": 2, "on": 3, "undeclared": 1}


def test_an_expected_reject_is_never_retried(tmp_path: Path) -> None:
    """꺼짐의 [writes_enabled]·미선언의 [declared]는 시나리오가 일부러 내는 거부라 한 번만
    부른다."""
    f = flow(tmp_path)
    fleet_scenarios.writes(f.session, fleet_state(f.session), f.scenario)
    writes = [e for e in f.events if e.startswith("write ")]
    assert writes.count("write line1/temp_sp 77.5") == 1
    assert writes.count("write sim/r001 7.0") == 1
    off: dict[str, object] = {
        "prepare": [409, {"checks": [{"name": "writes_enabled", "ok": False}]}]
    }
    assert not fleet_scenarios.transient_reject(off)
    wrong: dict[str, object] = {"prepare": [409, {"checks": [{"name": "role", "ok": False}]}]}
    assert not fleet_scenarios.transient_reject(wrong)  # 일시 검사가 아니다
    late: dict[str, object] = {
        "prepare": [200, {}],
        "confirm": [409, {"checks": [{"name": "tag_good", "ok": False}]}],
    }
    assert fleet_scenarios.transient_reject(late)  # confirm 단계의 일시 거부도 다시 묻는다


def test_a_write_that_stays_transiently_rejected_stops_at_the_limit(tmp_path: Path) -> None:
    """일시 거부가 끝나지 않으면 상한(`WRITE_RETRY_S`)에서 마지막 응답으로 판정한다(거짓)."""
    f = flow(tmp_path)
    calls: list[str] = []

    def stuck(host: Vm, tag: str, value: float, reason: str) -> dict[str, object]:
        calls.append(tag)
        if len(calls) > 40:
            pytest.fail("no retry limit")
        checks = [{"name": "plan_reported", "ok": False}]
        return {"prepare": [409, {"checks": checks}], "confirm": None, "audit": []}

    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    remote = dataclasses.replace(
        f.scenario.remote, write_tag=stuck, clock=lambda: now[0], sleep=sleep
    )
    out, tries = fleet_scenarios.write_settled(remote, Vm("10.77.0.12", Path("k")), TAG, 1.0, "r")
    # 0·5·…·60초에 부른다 = 60 / 5 + 1번
    want = int(fleet_scenarios.WRITE_RETRY_S / fleet_scenarios.WRITE_RETRY_POLL_S) + 1
    assert tries == len(calls) == want
    assert fleet_scenarios.transient_reject(out)


def test_a_central_view_that_cannot_be_read_never_counts_as_stale(tmp_path: Path) -> None:
    """리뷰 M3(M9 변이): control-web 경로가 502(행 없음)면 stale로 치지 않는다 — 판정 거짓."""
    f = flow(tmp_path)
    nft = f.scenario.remote.nft

    def nft_then_502(host: Vm, script: str) -> None:
        nft(host, script)
        f.ui_status = 502 if nft_kind(script) == "drop" else 200

    remote = dataclasses.replace(f.scenario.remote, nft=nft_then_502)
    checks = fleet_scenarios.fleet_view(
        f.session, fleet_state(f.session), dataclasses.replace(f.scenario, remote=remote)
    )
    assert failed(checks) == ["stale_after_block"]


def test_the_block_is_lifted_before_waiting_for_online(tmp_path: Path) -> None:
    """리뷰 M3(M10 변이): 가짜 connection은 마지막 nft 모드를 따른다 — 막힌 채로 online을 기다리면
    online이 오지 않는다. 차단 해제가 online 대기보다 먼저인지 본다."""
    f = flow(tmp_path)
    fleet_scenarios.fleet_view(f.session, fleet_state(f.session), f.scenario)
    clear = f.events.index("nft clear")
    reads = [i for i, e in enumerate(f.events) if e == "ui-read"]
    assert any(i > clear for i in reads)
    row = f.fake.fleet_rows["site-b"]
    assert (row["blocked"], row["connection"]) == (False, "online")


def test_a_wsl_hiccup_while_watching_the_view_is_asked_again(tmp_path: Path) -> None:
    """fix r1 L2: control-web 읽기 중 WSL 한 번 흔들림(InfraError)은 빈 행으로 치고 다시 묻는다.
    끝내 못 읽으면 None(판정 거짓) — 시나리오 전체가 infra가 되지 않는다."""
    f = flow(tmp_path)
    central = f.scenario.remote.central
    hiccups: list[str] = []

    def flaky(*args: str) -> dict[str, object]:
        if args[:2] == ("fleet", "--ui") and len(f.events) > 3 and not hiccups:
            hiccups.append("x")
            raise golden.WslError("wsl failed (-1): instance has terminated")
        return central(*args)

    remote = dataclasses.replace(f.scenario.remote, central=flaky)
    checks = fleet_scenarios.fleet_view(
        f.session, fleet_state(f.session), dataclasses.replace(f.scenario, remote=remote)
    )
    assert all(checks.values()), failed(checks)
    assert hiccups == ["x"]

    def dead(*args: str) -> dict[str, object]:
        if args[:2] == ("fleet", "--ui"):
            raise golden.WslError("wsl failed (-1)")
        return central(*args)

    remote = dataclasses.replace(f.scenario.remote, central=dead)
    assert fleet_scenarios.wait_connection(remote, "site-b", "stale", 10.0) is None


def put_pib(tmp_path: Path) -> tuple[Path, str]:
    pib = tmp_path / "x.pib"
    pib.write_bytes(b"signed bundle")
    return pib, hashlib.sha256(b"signed bundle").hexdigest()


def test_a_put_whose_reply_was_lost_but_whose_bundle_landed_goes_on(tmp_path: Path) -> None:
    """fix r1 L4: PUT이 WSL에서 죽었어도 다시 읽은 desired가 이 gen·이 번들 sha256이면 중앙이 받은
    것이다 — 성공으로 보고 진행한다(409 product가 아니다)."""
    f = flow(tmp_path)
    pib, sha = put_pib(tmp_path)
    central = f.scenario.remote.central

    def lost_reply(*args: str) -> dict[str, object]:
        if args[0] == "put":
            f.fake.fleet_rows["site-a"] = {
                "max_published_gen": 2,
                "desired": {"generation": 2, "sha256": f"sha256:{sha}"},
            }
            raise golden.WslScriptError("wsl script failed (1): ConnectionResetError", 1)
        return central(*args)

    remote = dataclasses.replace(f.scenario.remote, central=lost_reply)
    done = fleet_scenarios.put(remote, "site-a", 2, pib)
    assert done["status"] == 201
    assert "ConnectionResetError" in str(done["reply_lost"])


def test_a_put_that_died_with_another_bundle_in_place_is_a_409(tmp_path: Path) -> None:
    f = flow(tmp_path)
    pib, _ = put_pib(tmp_path)
    central = f.scenario.remote.central

    def other(*args: str) -> dict[str, object]:
        if args[0] == "put":
            f.fake.fleet_rows["site-a"] = {
                "max_published_gen": 2,
                "desired": {"generation": 2, "sha256": "sha256:" + "0" * 64},
            }
            raise golden.WslScriptError("wsl script failed (1): ssl.SSLEOFError", 1)
        return central(*args)

    remote = dataclasses.replace(f.scenario.remote, central=other)
    with pytest.raises(E2EError, match="presumably 409"):
        fleet_scenarios.put(remote, "site-a", 2, pib)


def test_a_failed_reread_keeps_the_original_put_failure_in_the_message(tmp_path: Path) -> None:
    f = flow(tmp_path)
    pib, _ = put_pib(tmp_path)

    def down(*args: str) -> dict[str, object]:
        if args[0] == "put":
            raise golden.WslScriptError("wsl script failed (1): ssl.SSLEOFError", 1)
        raise golden.WslError("wsl failed (-1): instance has terminated")

    remote = dataclasses.replace(f.scenario.remote, central=down)
    with pytest.raises(InfraError, match=r"SSLEOFError.*re-read failed too.*terminated"):
        fleet_scenarios.put(remote, "site-a", 2, pib)
