"""G2-7 폐쇄망(M6-4b-2 C2) — site-b c0 + 중앙 c1 DB 위에서 seq 정렬 → nft 차단·카운터 → USB 적용 →
`report export`를 USB로 → `POST /offline-imports` → `/fleet`·control-web 경로 판정. 가짜 VM은 현장
`report_seq` 카운터와 대기 보고를, 가짜 중앙은 종류별 수위(`judge_seq`: 수위보다 큰 seq만 반영)를
흉내 낸다. 변이 대상: 카운터 판정·ssh 예외·applied·이미지·반입 응답·`/fleet` 반영·Caddy 경로·
c1 중앙 restore·c0 VM·nft 정리·seq 정렬."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fleet_fakes import FakeRemote
from g2_fakes import digests, scenario_session
from harness import central, fleet_scenarios, fleet_session, g2, golden, judges, paths, site, usb
from harness.context import Context, E2EError, InfraError
from harness.vm import Vm

C1_MARKS = {"last_heartbeat_seq": 9, "last_report_seq": 2}
"""c1 덤프 때 site-b 수위 — c0에서 온라인이 되며 heartbeat 1, g1 보고 2, heartbeat 3..9."""


@dataclass
class AirgapVm:
    """폐쇄망 site-b c0 VM 가짜: `report_seq` 카운터 1, 대기 보고 = c0 g1 성공 1건. USB 마운트·
    apply·`report export`·`enroll --next-seq`를 흉내 낸다(`run`)."""

    release: str
    events: list[str]
    next_seq: int = 1
    pending: list[dict[str, object]] = field(
        default_factory=lambda: [{"to": 1, "result": "succeeded"}]
    )
    applied: int = 1
    image: str = ""
    mounted: str = ""
    usb_files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    export_rc: int = 0
    enroll_busy: int = 0
    """`enroll`이 Updater 잠금으로 exit 2를 낼 횟수(리뷰 M1)."""
    enroll_rc: int = 0
    kernel_log: list[str] = field(default_factory=list[str])

    def run(
        self, command: str, *, timeout_s: float = 600, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        out, rc = "", 0
        if "--next-seq" in command:
            self.events.append("vm enroll --next-seq")
            if self.enroll_busy:
                self.enroll_busy -= 1
                return subprocess.CompletedProcess[str]([], 2, "", "another updater is running")
            if self.enroll_rc:
                return subprocess.CompletedProcess[str]([], self.enroll_rc, "", "enroll refused")
            self.next_seq = max(self.next_seq, int(command.rsplit(" ", 1)[1]))
        elif "blkid" in command:
            out = "/dev/sdb\n"
        elif "mount -o ro" in command:
            self.events.append("vm mount ro")
            self.mounted = "ro"
        elif "remount,rw" in command:
            self.events.append("vm remount rw")
            self.mounted = "rw"
        elif "pureiron-site apply" in command:
            self.events.append("vm apply (usb)")
            assert self.mounted
            assert f"{usb.MOUNT}/" in command
            gen = int(command.rsplit("-g", maxsplit=1)[-1].split("-", maxsplit=1)[0])
            self.applied, self.image = gen, self.release
            self.pending.append({"to": gen, "result": "succeeded"})
        elif "report export" in command:
            self.events.append("vm report export (usb)")
            assert self.mounted == "rw"
            assert f"--out {usb.MOUNT}/{fleet_scenarios.REPORTS_DIR}" in command
            rc = self.export_rc
            if rc == 0:
                reports = [
                    {**r, "report_seq": self.next_seq + i} for i, r in enumerate(self.pending)
                ]
                self.next_seq += len(reports)
                first, last = reports[0]["report_seq"], reports[-1]["report_seq"]
                name = f"pureiron-reports-site-b-{first:06d}-{last:06d}.json"
                body = {"site_id": "site-b", "first_seq": first, "last_seq": last}
                self.usb_files[name] = json.dumps({**body, "reports": reports}).encode()
                self.pending = []
                out = f"{len(reports)} report(s) → {name}\n"
        elif "journalctl -k" in command:
            assert "grep -F 'e2e-airgap-'" in command
            out = "\n".join(self.kernel_log)
        elif "umount" in command:
            self.events.append("vm umount")
            self.mounted = ""
        return subprocess.CompletedProcess[str]([], rc, out, "" if rc == 0 else "export failed")


@dataclass
class Airgap:
    """가짜 중앙(`FakeRemote` + 종류별 수위 반입) + 가짜 폐쇄망 VM + 가짜 USB(WSL·Hyper-V)."""

    session: g2.Session
    fs: fleet_session.FleetSession
    fake: FakeRemote
    vm: AirgapVm
    events: list[str]
    scenario: fleet_scenarios.Scenario = field(init=False)
    counters: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(fleet_scenarios.AIRGAP_COUNTERS, 0)
    )
    marks: dict[str, int] = field(default_factory=lambda: dict(C1_MARKS))
    nft_scripts: list[str] = field(default_factory=list[str])
    lose_files: bool = False
    """꺼내기(debugfs rdump)가 아무것도 내지 못한다(리뷰 M3)."""


def airgap_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Airgap:
    monkeypatch.setattr(paths, "E2E", tmp_path / "e2e")
    monkeypatch.setattr(paths, "BUNDLES", tmp_path / "e2e" / "bundles")
    monkeypatch.setattr(g2, "RESULTS", tmp_path / "e2e" / "results")
    s = scenario_session(tmp_path)
    fs = fleet_session.FleetSession(
        run_id=s.run_id,
        c1_name="c1-x",
        ca=Path("ca"),
        ca_sha256="AB",
        c1_fleet={"site-a": dict(C1_MARKS), "site-b": dict(C1_MARKS)},
        c1_nft={"site-a": {}, "site-b": {}},
    )
    fake = FakeRemote(s)
    events = fake.events
    run = s.sites["site-b"]
    f = Airgap(s, fs, fake, AirgapVm(s.pair.next, events), events)
    f.vm.image = run.release
    current_seq = C1_MARKS["last_heartbeat_seq"]

    def probe(host: Vm) -> dict[str, object]:
        images = digests(f.vm.image)
        return {
            "updater": {"applied_gen": f.vm.applied, "last_result": "succeeded", "attempt": None},
            "containers": {svc: {"image_id": d, "running": True} for svc, d in images.items()},
            "readyz": {"status": 200},
        }

    def converge(host: Vm, gen: int) -> tuple[dict[str, object], float]:
        events.append(f"converge g{gen}")
        return probe(host), 20.0

    def nft(host: Vm, script: str) -> None:
        f.nft_scripts.append(script)
        clear = script == fleet_scenarios.AIRGAP_CLEAR
        events.append("nft clear" if clear else "nft airgap")

    def offline_import(path: str) -> dict[str, object]:
        """종류별 수위(control-api `judge_seq`): 보고 seq > 보고 수위면 반영, 아니면 이력."""
        nonlocal current_seq
        found = [
            p for p in (run.ctx.work / "airgap" / "reports").glob("*") if golden.wsl_path(p) == path
        ]
        doc = json.loads(found[0].read_bytes())
        applied = history = 0
        row = dict(fake.fleet_rows.get("site-b", {}))
        for report in sorted(doc["reports"], key=lambda r: r["report_seq"]):
            seq = report["report_seq"]
            if seq <= f.marks["last_report_seq"]:
                history += 1
                continue
            applied += 1
            f.marks["last_report_seq"] = seq
            row["last_attempt"] = {
                "source": "offline",
                "generation": report["to"],
                "result": report["result"],
                "report_seq": seq,
            }
            if report["result"] == "succeeded" and seq > current_seq:
                current_seq = seq
                row["current"] = {"gen": report["to"]}
        fake.fleet_rows["site-b"] = row
        body = {"applied_total": applied, "history_total": history}
        return {"status": 201, "body": body, "file": Path(path).name}

    def central_view(*args: str) -> dict[str, object]:
        if args[0] == "offline-import":
            events.append("central offline-import")
            return offline_import(args[2])
        return fake.central(*args)

    def wsl(script: str) -> str:
        if "mke2fs" in script:
            events.append("usb make")
            (paths.E2E / "usb" / script.rsplit("/", 1)[-1].strip("'\" ")).write_bytes(b"x")
        elif "rdump" in script:
            events.append("usb read")
            if f.lose_files:
                return ""  # debugfs: rdump가 실패해도 종료 코드 0
            out = run.ctx.work / "airgap" / "reports"
            out.mkdir(parents=True, exist_ok=True)
            for name, data in f.vm.usb_files.items():
                (out / name).write_bytes(data)
        return ""

    def attach(vm: str, vhdx: Path) -> None:
        events.append(f"attach {vm}")

    def detach(vm: str, vhdx: Path) -> int:
        events.append(f"detach {vm}")
        return 1

    def build(
        ctx: Context,
        gen: int,
        release: str,
        window: tuple[datetime, datetime],
        tree: Path,
        *,
        writes: bool = False,
    ) -> Path:
        events.append(f"build g{gen} {release}")
        return tmp_path / f"pureiron-site-b-g{gen}-{release}.pib"

    def shell(host: Vm) -> site.Shell:
        return f.vm

    def table_counters(host: Vm, table: str) -> dict[str, int]:
        return dict(f.counters) if table == fleet_scenarios.AIRGAP_NFT else {}

    ops = usb.Ops(wsl=wsl, attach=attach, detach=detach, clock=lambda: 0.0, sleep=lambda s: None)
    remote = dataclasses.replace(
        fake.remote(),
        central=central_view,
        probe=probe,
        converge=converge,
        nft=nft,
        shell=shell,
        usb_ops=ops,
        table_counters=table_counters,
    )
    f.scenario = fleet_scenarios.Scenario(remote, build=build)
    return f


def failed(checks: dict[str, bool]) -> list[str]:
    return sorted(k for k, ok in checks.items() if not ok)


def last_record(f: Airgap) -> dict[str, object]:
    state = json.loads(f.session.sites["site-b"].ctx.site.state_file.read_text("utf-8"))
    return cast(dict[str, object], state["results"][-1])


def test_airgap_applies_from_usb_exports_by_usb_and_imports_into_the_c1_central(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = airgap_flow(tmp_path, monkeypatch)
    checks = fleet_scenarios.airgap(f.session, f.fs, f.scenario)
    assert failed(checks) == []
    assert set(checks) == set(fleet_scenarios.G2_7_ITEMS)
    want = [
        *("central stop-web", "vm-restore site-b c0", "central restore", "central start-web"),
        "vm enroll --next-seq",
        "central fleet --site site-b",
        "nft airgap",
        "build g2 " + f.session.pair.next,
        *("usb make", "attach pureiron-site-b", "vm mount ro", "vm apply (usb)", "converge g2"),
        *("vm remount rw", "vm report export (usb)", "vm umount", "detach pureiron-site-b"),
        "usb read",
        "nft clear",
        "central offline-import",
        "central fleet --site site-b",
        "central fleet --ui",
    ]
    assert f.events == want
    assert not [e for e in f.events if e in ("central up", "central put")]
    assert f.vm.next_seq == 12  # 정렬 10 + 보고 2건(g1·g2)
    record = last_record(f)
    assert record["ok"] is True
    note = cast(dict[str, object], record["seq_alignment"])
    assert note["next_seq"] == 10
    assert note["source"] == "fleet/session.json c1_fleet[site-b] (central dump summary)"
    assert cast(dict[str, object], record["usb"])["deleted"] is True
    assert record["filter_cleared"] is True
    assert not list((tmp_path / "e2e" / "usb").glob("*.vhdx"))


def test_without_the_seq_alignment_the_import_is_history_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """정렬이 없으면(카운터 1부터) 보고 seq 1·2가 c1 보고 수위 2 이하라 이력으로만 — `/fleet`가
    바뀌지 않아 판정이 거짓이다(코디네이터 확정 A의 근거)."""
    f = airgap_flow(tmp_path, monkeypatch)

    def skipped(host: site.Shell, site_id: str, next_seq: int, **_: object) -> None:
        f.events.append("seq alignment skipped")

    monkeypatch.setattr(site, "align_seq", skipped)
    checks = fleet_scenarios.airgap(f.session, f.fs, f.scenario)
    assert failed(checks) == [
        "control_web_last_attempt",
        "fleet_current",
        "fleet_last_attempt",
        "import_applied",
    ]


def test_missing_c1_marks_stop_the_test_without_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = airgap_flow(tmp_path, monkeypatch)
    f.fs.c1_fleet["site-b"] = {"last_heartbeat_seq": 9}
    with pytest.raises(E2EError, match="no c1 seq marks"):
        fleet_scenarios.airgap(f.session, f.fs, f.scenario)
    assert "nft airgap" not in f.events


def test_a_failed_export_still_clears_the_filter_and_removes_the_usb_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = airgap_flow(tmp_path, monkeypatch)
    f.vm.export_rc = 1
    with pytest.raises(E2EError, match="report export exited 1"):
        fleet_scenarios.airgap(f.session, f.fs, f.scenario)
    assert f.events[-4:] == [
        "vm report export (usb)",
        "vm umount",
        "detach pureiron-site-b",
        "nft clear",
    ]
    assert not list((tmp_path / "e2e" / "usb").glob("*.vhdx"))


def test_any_packet_to_or_from_the_central_fails_g2_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = airgap_flow(tmp_path, monkeypatch)
    f.counters["airgap_out"] = 3
    f.vm.kernel_log = [
        "2026-11-18T03:01:00+00:00 site-b kernel: e2e-airgap-out IN= OUT=eth0 DPT=53"
    ]
    checks = fleet_scenarios.airgap(f.session, f.fs, f.scenario)
    assert failed(checks) == ["no_central_traffic"]
    assert last_record(f)["airgap_log"] == f.vm.kernel_log  # 출처 증거(nft log 줄)


def test_the_airgap_filter_lets_ssh_through_before_counting_everything_else() -> None:
    """체인마다 ssh(VM:22) accept가 카운터·drop 규칙보다 먼저다 — 빠지면 하네스 ssh도 막히고 세어져
    G2-7이 늘 실패한다. 표는 `e2e_fleet`·`e2e_audit`와 따로, 통째로 바꾼다."""
    script = fleet_scenarios.airgap_filter()
    assert script.startswith(fleet_scenarios.AIRGAP_CLEAR)
    assert "table inet e2e_airgap {" in script
    chains: dict[str, list[str]] = {}
    current = ""
    for line in script.splitlines():
        text = line.strip()
        if text.startswith("chain "):
            current = text.split()[1]
            chains[current] = []
        elif current and text and text != "}" and not text.startswith("type filter"):
            chains[current].append(text)
    assert chains == {
        "airgap_in": [
            "ip saddr 10.77.0.1 tcp dport 22 accept",
            "ip saddr 10.77.0.1 meta pkttype host counter "
            'log prefix "e2e-airgap-in " drop comment "airgap_in"',
        ],
        "airgap_out": [
            "ip daddr 10.77.0.1 tcp sport 22 accept",
            'ip daddr 10.77.0.1 counter log prefix "e2e-airgap-out " drop comment "airgap_out"',
        ],
        "airgap_fwd": [
            "ip saddr 10.77.0.1 counter "
            'log prefix "e2e-airgap-fwd-in " drop comment "airgap_fwd_in"',
            "ip daddr 10.77.0.1 counter "
            'log prefix "e2e-airgap-fwd-out " drop comment "airgap_fwd_out"',
        ],
    }
    hooks = [line.strip() for line in script.splitlines() if "type filter hook" in line]
    assert [h.split()[3] for h in hooks] == ["input", "output", "forward"]
    comments = {c.rsplit('"', 2)[1] for rules in chains.values() for c in rules if "comment" in c}
    assert comments == set(fleet_scenarios.AIRGAP_COUNTERS)


def airgap_judge(**change: object) -> dict[str, bool]:
    gen = 2
    snap: dict[str, object] = {
        "updater": {"applied_gen": gen, "last_result": "succeeded"},
        "containers": {s: {"image_id": d, "running": True} for s, d in digests("next").items()},
    }
    attempt = {"source": "offline", "generation": gen, "result": "succeeded"}
    row: dict[str, object] = {"site": "site-b", "last_attempt": attempt, "current": {"gen": gen}}
    args: dict[str, Any] = {
        "counters": dict.fromkeys(fleet_scenarios.AIRGAP_COUNTERS, 0),
        "snap": snap,
        "imports": [{"status": 201, "body": {"applied_total": 2, "history_total": 0}}],
        "row": row,
        "ui": {"status": 200, "body": [row], "via": "caddy-ui"},
    }
    args |= change
    return judges.judge_airgap(
        args["counters"],
        args["snap"],
        args["imports"],
        args["row"],
        args["ui"],
        site_id="site-b",
        gen=gen,
        next_digests=digests("next"),
        want_counters=fleet_scenarios.AIRGAP_COUNTERS,
    )


ATTEMPT_G1 = {"source": "offline", "generation": 1, "result": "succeeded"}


@pytest.mark.parametrize(
    ("change", "broken"),
    [
        (
            {
                "counters": {
                    "airgap_in": 0,
                    "airgap_out": 0,
                    "airgap_fwd_in": 1,
                    "airgap_fwd_out": 0,
                }
            },
            ["no_central_traffic"],
        ),
        ({"counters": {}}, ["no_central_traffic"]),
        (
            {
                "snap": {
                    "updater": {"applied_gen": 2, "last_result": "failed"},
                    "containers": {
                        s: {"image_id": d, "running": True} for s, d in digests("next").items()
                    },
                }
            },
            ["succeeded"],
        ),
        (
            {
                "snap": {
                    "updater": {"applied_gen": 1, "last_result": "succeeded"},
                    "containers": {
                        s: {"image_id": d, "running": True} for s, d in digests("next").items()
                    },
                }
            },
            ["applied_gen"],
        ),
        (
            {
                "snap": {
                    "updater": {"applied_gen": 2, "last_result": "succeeded"},
                    "containers": {
                        s: {"image_id": d, "running": True} for s, d in digests("base").items()
                    },
                }
            },
            ["images_next"],
        ),
        (
            {"imports": [{"status": 400, "body": {"detail": "bad"}}]},
            ["import_201", "import_applied"],
        ),
        ({"imports": []}, ["import_201", "import_applied"]),
        (
            {"imports": [{"status": 201, "body": {"applied_total": 0, "history_total": 2}}]},
            ["import_applied"],
        ),
        (
            {"row": {"site": "site-b", "last_attempt": ATTEMPT_G1, "current": {"gen": 2}}},
            ["fleet_last_attempt"],
        ),
        (
            {
                "row": {
                    "site": "site-b",
                    "last_attempt": {**ATTEMPT_G1, "generation": 2, "source": "online"},
                    "current": {"gen": 2},
                }
            },
            ["fleet_last_attempt"],
        ),
        (
            {
                "row": {
                    "site": "site-b",
                    "last_attempt": {**ATTEMPT_G1, "generation": 2},
                    "current": {"gen": 1},
                }
            },
            ["fleet_current"],
        ),
        (
            {"ui": {"status": 200, "body": [], "via": "control-api"}},
            ["control_web_last_attempt", "via_control_web"],
        ),
        (
            {"ui": {"status": 401, "body": "no", "via": "caddy-ui"}},
            ["control_web_last_attempt", "via_control_web"],
        ),
        (
            {
                "ui": {
                    "status": 200,
                    "body": [{"site": "site-b", "last_attempt": ATTEMPT_G1}],
                    "via": "caddy-ui",
                }
            },
            ["control_web_last_attempt"],
        ),
    ],
)
def test_airgap_judge_each_check_matters(change: dict[str, object], broken: list[str]) -> None:
    assert failed(airgap_judge()) == []
    assert failed(airgap_judge(**change)) == broken


def test_restore_airgap_takes_the_vm_to_c0_and_the_central_to_the_c1_dump(tmp_path: Path) -> None:
    s = scenario_session(tmp_path)
    fake = FakeRemote(s)
    fs = fleet_session.FleetSession(s.run_id, "c1-x", Path("ca"), "AB", c1_nft={"site-b": {}})
    out = fleet_session.restore_airgap(s, fs, "site-b", remote=fake.remote(), results=tmp_path)
    assert fake.events == [
        "central stop-web",
        "vm-restore site-b c0",
        "central restore",
        "central start-web",
    ]
    assert out["vm"] == "c0"
    assert fs.restores == 1
    assert s.sites["site-b"].ctx.nft_last == s.sites["site-b"].ctx.nft_c0
    fake.restore_reply = {"fleet_match": False}
    with pytest.raises(InfraError, match="does not match"):
        fleet_session.restore_airgap(s, fs, "site-b", remote=fake.remote(), results=tmp_path)
    assert fake.events[-1] == "central start-web"


def test_central_posts_the_report_bundle_with_the_admin_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`offline-import`: admin 토큰(비밀 파일)으로 `POST /offline-imports`, 본문은 export 파일
    그대로(JSON). 토큰은 응답·출력에 없다."""
    bundle = tmp_path / "pureiron-reports-site-b-000010-000011.json"
    bundle.write_bytes(b'{"payload": {"site_id": "site-b"}}')
    seen: dict[str, object] = {}

    class Response:
        status = 201

        def read(self) -> bytes:
            return b'{"import_id": 1, "site_id": "site-b", "applied_total": 2}'

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def urlopen(request: Any, **kwargs: object) -> Response:
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.get_header("Authorization")
        seen["type"] = request.get_header("Content-type")
        seen["body"] = request.data.read()
        return Response()

    def read_secret(name: str) -> str:
        return {"admin_token": "adm-secret-1"}[name]

    monkeypatch.setattr(central, "read_secret", read_secret)
    monkeypatch.setattr(central, "context", lambda: None)
    monkeypatch.setattr(central.urllib.request, "urlopen", urlopen)
    args = argparse.Namespace(command="offline-import", file=bundle)
    out = central.run_command(args)
    assert seen["url"] == "https://10.77.0.1:28443/api/v1/offline-imports"
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer adm-secret-1"
    assert seen["type"] == "application/json"
    assert seen["body"] == bundle.read_bytes()
    assert out["status"] == 201
    assert "adm-secret-1" not in json.dumps(out)


def test_busy_retry_waits_out_the_updater_lock() -> None:
    codes = [2, 2, 0]
    seen: list[str] = []

    class Host:
        def run(
            self,
            command: str,
            *,
            timeout_s: float = 600,
            check: bool = True,
            input: str | None = None,
        ) -> subprocess.CompletedProcess[str]:
            seen.append(command)
            return subprocess.CompletedProcess[str]([], codes.pop(0), "", "")

    done = site.busy_retry(Host(), "sudo pureiron-site report export --out x", sleep=lambda s: None)
    assert done.returncode == 0
    assert len(seen) == 3


def test_a_busy_updater_lock_on_the_seq_alignment_is_waited_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`enroll`도 Updater 잠금을 잡는다 — exit 2면 다시 묻는다(리뷰 M1)."""
    f = airgap_flow(tmp_path, monkeypatch)
    f.vm.enroll_busy = 1
    checks = fleet_scenarios.airgap(f.session, f.fs, f.scenario)
    assert failed(checks) == []
    assert f.events.count("vm enroll --next-seq") == 2


def test_a_refused_seq_alignment_stops_with_the_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = airgap_flow(tmp_path, monkeypatch)
    f.vm.enroll_rc = 1
    with pytest.raises(E2EError, match="enroll --next-seq 10 exited 1: enroll refused"):
        fleet_scenarios.airgap(f.session, f.fs, f.scenario)
    assert "nft airgap" not in f.events


def test_bundles_that_do_not_come_off_the_usb_disk_are_infra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """export가 묶음을 썼는데 꺼낸 파일 수가 다르면 꺼내기(시험 배선) 실패 — infra, 반입 0건을
    제품 실패로 보지 않는다(리뷰 M3). 필터·USB는 치운다."""
    f = airgap_flow(tmp_path, monkeypatch)
    f.lose_files = True
    with pytest.raises(InfraError, match=r"not read: report export wrote 1 bundle\(s\), 0 came"):
        fleet_scenarios.airgap(f.session, f.fs, f.scenario)
    assert f.events[-2:] == ["usb read", "nft clear"]
    assert "central offline-import" not in f.events
    assert not list((tmp_path / "e2e" / "usb").glob("*.vhdx"))
