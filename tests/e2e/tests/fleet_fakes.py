"""fleet 단위 시험이 같이 쓰는 가짜(M6-4a) — fleet 세션·시나리오의 원격 호출(중앙 `central.py`,
Hyper-V, VM).

시험 파일이 아니다(수집되지 않는다). 받은 호출은 `FakeRemote.events`에 차례로 남는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness import fleet_session, g2
from harness.context import Context
from harness.vm import Vm


@dataclass
class FakeRemote:
    """중앙(`central.py`)·Hyper-V·VM 조작의 가짜 — 호출을 `events`에.

    `/fleet` 행은 현장마다 online·heartbeat 수위·current(수렴한 gen, 없으면 c0 gen)·게시 상태
    (`put`이 바꾼다)이고, `fleet_rows`가 그 위를 덮는다."""

    session: g2.Session
    events: list[str] = field(default_factory=list[str])
    fleet_rows: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    restore_reply: dict[str, object] = field(default_factory=lambda: {"fleet_match": True})
    dump_fleet: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    applied: dict[str, tuple[int, str]] = field(default_factory=dict[str, tuple[int, str]])
    current: dict[str, int] = field(default_factory=dict[str, int])
    """수렴한 현장의 중앙 current gen(`/fleet` 행에 들어간다)."""
    published: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    """`put`이 남긴 현장별 desired·`max_published_gen`."""
    put_status: int = 201
    put_product: str = ""
    """`put`이 desired에 남길 product(빈 값이면 세션 NEXT)."""
    now: datetime = datetime(2026, 11, 15, 3, 0, tzinfo=UTC)
    tick: float = 0.0
    skew: dict[str, float] = field(default_factory=dict[str, float])
    """현장별 VM 시계 - 가짜 호스트 시계(초, 기본 0)."""

    def row(self, site: str) -> dict[str, object]:
        base: dict[str, object] = {
            "site": site,
            "connection": "online",
            "last_heartbeat_seq": 3,
            "current": {"gen": self.current.get(site, fleet_session.C0_GEN)},
            "desired": None,
            "max_published_gen": 0,
        }
        return {**base, **self.published.get(site, {}), **self.fleet_rows.get(site, {})}

    def central(self, *args: str) -> dict[str, object]:
        command = args[0]
        self.events.append(
            "central " + " ".join(args[:3] if command in ("fleet", "put") else args[:1])
        )
        if command == "up":
            return {"ca_sha256": "AB:CD"}
        if command == "dump":
            return {"name": args[2], "bytes": 10, "seconds": 1.0, "fleet": self.dump_fleet}
        if command == "restore":
            return {**self.restore_reply, "seconds": 2.0}
        if command == "fleet":
            return {"status": 200, "body": [self.row(args[2])]}
        if command == "put":
            site, gen = args[2], int(args[4])
            if self.put_status == 201:
                pair = self.session.pair
                product = self.put_product or pair.releases[pair.next]
                self.published[site] = {
                    "desired": {"generation": gen, "product_version": product},
                    "max_published_gen": gen,
                }
            return {"status": self.put_status, "body": {"site": site, "generation": gen}}
        return {"ok": True}

    def remote(self) -> fleet_session.Remote:
        def restore_vm(run: g2.SiteRun, snapshot: str, counters: Mapping[str, int]) -> None:
            self.events.append(f"vm-restore {run.ctx.site.site_id} {snapshot}")
            run.ctx.nft_last = dict(counters)

        def restore_c0(run: g2.SiteRun) -> None:
            self.events.append(f"c0 {run.ctx.site.site_id}")

        def online(ctx: Context, host: Vm, ca: Path, sha: str) -> dict[str, object]:
            self.events.append(f"online {ctx.site.site_id}")
            return {"registered": ctx.site.site_id}

        def site_of(host: Vm) -> str:
            return next(s for s, r in self.session.sites.items() if r.ctx.site.ip == host.ip)

        def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
            site = site_of(host)
            self.events.append(f"converge {site} g{gen}")
            want, result = self.applied.get(site, (gen, "succeeded"))
            self.current[site] = want
            return {"updater": {"applied_gen": want, "last_result": result}}, 3.0

        def probe(host: Vm) -> dict[str, object]:
            gen = self.current.get(site_of(host), fleet_session.C0_GEN)
            return {"updater": {"applied_gen": gen, "last_result": "succeeded"}}

        def vm_epoch(host: Vm) -> float:
            return self.now.timestamp() + self.skew.get(site_of(host), 0.0)

        def checkpoint(vm: str, name: str) -> None:
            self.events.append(f"checkpoint {vm} {name}")

        def mark(ctx: Context, host: Vm) -> None:
            self.events.append(f"mark {ctx.site.site_id}")
            ctx.nft_last = {"ext_out_v4": 11}

        def clock() -> float:
            self.tick += 1.0
            return self.tick

        return fleet_session.Remote(
            central=self.central,
            checkpoint=checkpoint,
            restore_vm=restore_vm,
            restore_c0=restore_c0,
            online=online,
            probe=probe,
            converge=converge,
            wait_ready=lambda host: {},
            mark=mark,
            vm_epoch=vm_epoch,
            now=lambda: self.now,
            sleep=lambda s: None,
            clock=clock,
        )
