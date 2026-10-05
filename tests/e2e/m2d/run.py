"""M2d Hyper-V VM 시나리오(plan M2 종료 조건, PD-24). Windows 호스트에서 실행한다.

    uv run python tests/e2e/m2d/run.py all                # 처음부터 끝까지
    uv run python tests/e2e/m2d/run.py all --from crash   # 앞 단계 결과(context)를 이어서
    uv run python tests/e2e/m2d/run.py destroy            # VM·차분 디스크 삭제(golden은 둔다)

단계(`STEPS`):
1. `prepare` — WSL runner의 저장소 사본에서 r1'(`r1f`) 기록, 설치 번들(실빌드: deb 수집·Updater
   venv), g1(r0) 현장 번들.
2. `vm` — golden VHDX 위 차분 디스크 + seed ISO로 `pureiron-site-a`(2 vCPU/4 GiB, 내부 전용 망).
3. `install` — runbook 2단계(전용 디렉터리에 메타데이터만 추출 → 지문·sha256 대조 →
   `bootstrap-verify.sh`) → `install.sh --offline-time-confirmed` → enroll → g1 apply → 서비스 시작.
   `/readyz` 200, classic image store(overlay2), 이미지 `.Id` == r0 config digest. 압축 chunk가 있는
   이력을 넣고 체크포인트 `c0`(epoch 1)를 만든다.
4. `scenarios` — `c0`에서 거부(다른 현장 `E_SITE`·변조 `E_DIGEST`) exit 1과 같은 gen 재빌드 no-op
   exit 0(R-7 멱등), 모두 core `StartedAt`·실행 이미지 불변. `c0`에서 r0→r1(압축 DB 위 epoch 2
   migrate) 성공 → 그 위에서 낮은 gen `E_GEN` 거부. `c0`에서 r1' → 5분 안에 r0 digest 복귀 +
   `failed`.
5. `crash` — phase마다 `systemctl kill -s KILL pureiron-updater`와 `Restart-VM -Force`를 1회씩.
   서비스가 그 시도를 실행하도록 창이 조금 뒤에 열리는 번들을 CLI로 스테이징하고(`verified` 대기),
   VM 쪽 `crash_at.py`가 해당 phase 진입 순간 프로세스를 얼린 뒤 kill/리셋한다. `migrating`은
   실제 마이그레이션이 일어나도록 `c0`에서, `rolling_back`은 r1'로, `fetched`(번들 검증)는 CLI를
   대상으로. 매번 10분 안에 `succeeded`(rolling_back은 `failed`) + 실행 이미지 == 기대 release +
   편집 잠금 해제로 수렴해야 한다.
6. `report` — nftables 외부 접속 카운터 0 확인, `bundle-metrics.json`, `results.json`.

결과: `artifacts/e2e/m2d-<시각>/`(gitignore). 진행 상태는 `C:\\PureIron_e2e\\state.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))

import golden
import hyperv
from vm import Vm, VmError

REPO = Path(__file__).resolve().parents[3]
E2E = Path(os.environ.get("PUREIRON_E2E_ROOT", r"C:\PureIron_e2e"))
STATE_FILE = E2E / "state.json"
GOLDEN = E2E / "golden" / "ubuntu-24.04-64g.vhdx"
CLOUD_IMAGE = E2E / "images" / "ubuntu-24.04-server-cloudimg-amd64.img"
BUNDLES = E2E / "bundles"
VM_NAME, VM_IP = "pureiron-site-a", "10.77.0.10"
# setup-host.ps1 -Switch 와 같은 이름(기본 pureiron-int).
SWITCH = os.environ.get("PUREIRON_E2E_SWITCH", "pureiron-int")
SITE = "site-a"
WSL_HOME = "$HOME/m2d-e2e"
INSTALL_RELEASE = "2026.10.1"
RELEASES = {"r0": "2026.10.0", "r1": "2026.10.1", "r1f": "2026.10.1-fault1"}
APP_SERVICES = ("core", "collector", "web", "site-db")
REMOTE = "/home/pureiron/e2e"
INCOMING = "/home/pureiron/incoming"
WINDOW_LEAD_S = 150
CONVERGE_S = 600
ROLLBACK_LIMIT_S = 300
CRASH_PHASES = (
    "fetched",
    "verified",
    "committed",
    "locked",
    "backed_up",
    "migrating",
    "switching",
    "checking",
    "rolling_back",
)
STEPS = ("prepare", "vm", "install", "scenarios", "crash", "report")


class E2EError(RuntimeError):
    pass


@dataclass
class Context:
    run_id: str
    artifacts: str
    install_pib: str = ""
    runbook_fpr: str = ""
    runbook_bootstrap: str = ""
    digests: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    next_gen: int = 2
    current: str = "r0"
    results: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    bundles: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    nft_before: dict[str, object] = field(default_factory=dict[str, object])

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), "utf-8")

    @classmethod
    def load(cls) -> Context:
        return cls(**json.loads(STATE_FILE.read_text(encoding="utf-8")))

    @property
    def out(self) -> Path:
        return Path(self.artifacts)

    def record(self, name: str, ok: bool, **detail: object) -> None:
        entry: dict[str, object] = {"name": name, "ok": ok, **detail}
        # 단계를 다시 돌리면(`run.py <step>`) 같은 항목은 마지막 결과로 바꾼다.
        self.results = [r for r in self.results if r["name"] != name] + [entry]
        self.save()
        print(
            f"[{'PASS' if ok else 'FAIL'}] {name} {json.dumps(detail, ensure_ascii=False)}",
            flush=True,
        )


def log(message: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {message}", flush=True)


def vm() -> Vm:
    return Vm(VM_IP, E2E / "ssh" / "id_ed25519")


# ---------------------------------------------------------------- WSL(빌드 호스트)


def wsl(script: str, *, timeout_s: float = 3600) -> str:
    epoch = subprocess.run(
        ["git", "-C", str(REPO), "log", "-1", "--format=%ct"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    prologue = (
        "set -euo pipefail; export PATH=$HOME/.local/bin:$PATH; "
        f"export SOURCE_DATE_EPOCH={epoch}; cd {WSL_HOME}/repo; "
    )
    return golden.wsl(prologue + script, timeout_s=timeout_s)


def sync_repo() -> None:
    golden.wsl(
        f"set -euo pipefail; export PATH=$HOME/.local/bin:$PATH; mkdir -p {WSL_HOME}/repo; "
        f"rsync -a --delete --exclude .git --exclude .venv --exclude node_modules "
        f"--exclude __pycache__ --exclude artifacts '{golden.wsl_path(REPO)}/' {WSL_HOME}/repo/; "
        f"cd {WSL_HOME}/repo && uv sync --all-packages --locked -q",
        timeout_s=1800,
    )


def build_site_bundle(ctx: Context, gen: int, release: str, window_open_at: datetime) -> Path:
    out = BUNDLES / ctx.run_id
    start = window_open_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (window_open_at + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    wsl(
        f"uv run --no-sync python tests/e2e/m2d/bundles.py site --work {WSL_HOME}/work "
        f"--out '{golden.wsl_path(out)}' --gen {gen} --product {RELEASES[release]} "
        f"--window-start {start} --window-end {end} --site {SITE}"
    )
    pib = out / f"pureiron-{SITE}-g{gen}.pib"
    metrics = json.loads(pib.with_name(pib.stem + ".bundle-metrics.json").read_text("utf-8"))
    ctx.bundles.append({"gen": gen, "release": release, "file": pib.name, **metrics})
    ctx.save()
    return pib


def digests_of(release_json: dict[str, object]) -> dict[str, str]:
    images = cast(dict[str, dict[str, str]], release_json["images"])
    return {svc: images[svc]["config_digest"] for svc in APP_SERVICES}


def step_prepare(ctx: Context) -> None:
    log("prepare: WSL repo sync")
    sync_repo()
    log("prepare: fault release r1f")
    wsl(f"uv run --no-sync python tests/e2e/m2d/bundles.py fault-release --work {WSL_HOME}/work")
    for name in RELEASES:
        record = json.loads(wsl(f"cat {WSL_HOME}/work/releases/{name}.json"))
        ctx.digests[name] = digests_of(record)
    log("prepare: install bundle (fetch_debs, build_updater, build_install_bundle)")
    out = BUNDLES / ctx.run_id
    wsl(
        f"uv run --no-sync python tests/e2e/m2d/bundles.py install --work {WSL_HOME}/work "
        f"--out '{golden.wsl_path(out)}' --release {INSTALL_RELEASE}",
        timeout_s=3600,
    )
    pib = out / f"pureiron-install-{INSTALL_RELEASE}.pib"
    runbook = pib.with_name(pib.stem + ".runbook.txt").read_text(encoding="utf-8")
    fpr = re.search(r"root 지문[^:]*:\s*([0-9a-f]{64})", runbook)
    boot = re.search(r"bootstrap-verify\.sh sha256:\s*([0-9a-f]{64})", runbook)
    if fpr is None or boot is None:
        raise E2EError(f"runbook values not found in {pib.name}.runbook.txt")
    ctx.install_pib, ctx.runbook_fpr, ctx.runbook_bootstrap = str(pib), fpr[1], boot[1]
    ctx.bundles.append({"kind": "install", "file": pib.name, "size_bytes": pib.stat().st_size})
    log("prepare: g1 (r0) site bundle")
    build_site_bundle(ctx, 1, "r0", datetime.now(UTC) - timedelta(days=1))
    ctx.save()


# ---------------------------------------------------------------- VM


def step_vm(ctx: Context) -> None:
    if not hyperv.available():
        raise E2EError("Hyper-V not available for this user (run tests/e2e/infra/setup-host.ps1)")
    if not hyperv.switch_exists(SWITCH):
        raise E2EError(f"vSwitch {SWITCH} missing (run tests/e2e/infra/setup-host.ps1)")
    golden.build_golden(CLOUD_IMAGE, GOLDEN)
    pub = golden.ensure_ssh_key(E2E / "ssh").read_text(encoding="utf-8")
    vm_dir = E2E / "vms" / VM_NAME
    hyperv.remove(VM_NAME, vm_dir / "disk.vhdx")
    seed = golden.write_seed_iso(
        vm_dir / "seed.iso", golden.seed_files("site-a", VM_IP, pub, golden.Network())
    )
    hyperv.create_vm(hyperv.VmSpec(VM_NAME, vm_dir / "disk.vhdx", seed, SWITCH), GOLDEN)
    reclaim_host_memory()
    hyperv.start(VM_NAME)
    host = vm()
    waited = host.wait_ssh(timeout_s=900)
    host.run("cloud-init status --wait >/dev/null; test -f /var/lib/e2e-ready", timeout_s=900)
    host.run(f"mkdir -p {REMOTE} {INCOMING}")
    for script in ("crash_at.py", "probe.py"):
        host.put(REPO / "tests" / "e2e" / "site" / script, f"{REMOTE}/{script}")
    ctx.nft_before = nft_counters(host)
    ctx.record("vm boot (cloud-init, internal-only switch)", True, ssh_after_s=round(waited, 1))


def nft_counters(host: Vm) -> dict[str, object]:
    out = host.run("sudo nft -j list table inet e2e_audit").stdout
    counters: dict[str, object] = {}
    for entry in cast(list[dict[str, object]], json.loads(out)["nftables"]):
        rule = cast(dict[str, object] | None, entry.get("rule"))
        if rule is None:
            continue
        for expr in cast(list[dict[str, object]], rule.get("expr", [])):
            counter = cast(dict[str, int] | None, expr.get("counter"))
            if counter is not None:
                counters[str(rule.get("comment"))] = counter["packets"]
    return counters


def reclaim_host_memory() -> None:
    """WSL(번들 빌드·Docker Desktop)의 페이지 캐시를 비워 VM 시작·복원에 쓸 호스트 메모리를
    돌려받는다. WSL VM은 캐시를 Windows에 늦게 돌려줘 4 GiB VM 복원이 0x8007000E로
    실패했다(첫 실측)."""
    subprocess.run(
        [
            "wsl",
            "-d",
            golden.WSL_DISTRO,
            "-u",
            "root",
            "--exec",
            "sh",
            "-c",
            "sync; echo 3 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory",
        ],
        capture_output=True,
        timeout=120,
        check=False,
    )


def as_root(script: str) -> str:
    """원격 root 셸에서 `script` 전체를 실행한다(`cd` 등이 sudo 밖에서 돌지 않게)."""
    return f"sudo bash -c {shlex.quote(script)}"


def probe(host: Vm) -> dict[str, object]:
    return host.json(f"sudo python3 {REMOTE}/probe.py")


def running_digests(snapshot: dict[str, object]) -> dict[str, str]:
    containers = cast(dict[str, dict[str, object]], snapshot.get("containers") or {})
    return {
        svc: str(info.get("image_id"))
        for svc, info in containers.items()
        if svc in APP_SERVICES and info.get("running")
    }


def updater_state(snapshot: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], snapshot.get("updater") or {})


def ready(snapshot: dict[str, object]) -> bool:
    readyz = cast(dict[str, object], snapshot.get("readyz") or {})
    return readyz.get("status") == 200


# ---------------------------------------------------------------- 설치


def step_install(ctx: Context) -> None:
    host = vm()
    install = Path(ctx.install_pib)
    g1 = BUNDLES / ctx.run_id / f"pureiron-{SITE}-g1.pib"
    for bundle in (install, g1):
        host.put(bundle, f"{INCOMING}/{bundle.name}")
    remote_install = f"{INCOMING}/{install.name}"
    meta = "manifest.json manifest.sig release-cert.json release-cert.sig keys/root.pub "
    meta += "keys/root.pem bootstrap/bootstrap-verify.sh"
    started = time.monotonic()
    # runbook 2단계는 root 셸에서 친다(/root는 700, bootstrap-verify ⓪은 추출물이
    # 실행 사용자 소유인지 본다).
    out = host.run(
        as_root(
            "install -d -m 700 /root/pib-meta && cd /root/pib-meta && "
            "tar --force-local --no-same-owner --no-same-permissions "
            f"-xf {remote_install} {meta} && sha256sum bootstrap/bootstrap-verify.sh"
        )
    ).stdout
    if not out.startswith(ctx.runbook_bootstrap):
        raise E2EError(f"bootstrap-verify.sh sha256 mismatch: {out.strip()}")
    host.run(
        as_root(
            "cd /root/pib-meta && bash bootstrap/bootstrap-verify.sh "
            f"--root-fpr {ctx.runbook_fpr} --bundle {remote_install} "
            "--dest /root/pureiron-install"
        ),
        timeout_s=900,
    )
    host.run(
        f"sudo bash /root/pureiron-install/install/install.sh --bundle {remote_install} "
        "--offline-time-confirmed",
        timeout_s=1800,
    )
    host.run(
        "echo PUREIRON_POLL_INTERVAL_S=10 | sudo tee -a /etc/pureiron/updater.env >/dev/null && "
        f"sudo pureiron-site enroll --site {SITE}"
    )
    host.run(f"sudo pureiron-site apply {INCOMING}/{g1.name}", timeout_s=1800)
    host.run("sudo systemctl start pureiron-updater")
    install_s = time.monotonic() - started
    snap = wait_ready(host, 300)
    docker = cast(dict[str, object], snap.get("docker") or {})
    classic = "containerd" not in json.dumps(docker.get("driver_status"))
    digests_ok = running_digests(snap) == ctx.digests["r0"]
    ctx.record(
        "install bundle bootstrap + g1(r0) on offline VM",
        ready(snap) and digests_ok and docker.get("driver") == "overlay2" and classic,
        install_s=round(install_s, 1),
        storage_driver=docker.get("driver"),
        classic_store=classic,
        images_match_r0=digests_ok,
        readyz=cast(dict[str, object], snap.get("readyz") or {}).get("status"),
    )
    seed_history(host)
    host.run("sudo rm -f " + f"{INCOMING}/{install.name} {INCOMING}/{g1.name}")
    hyperv.checkpoint(VM_NAME, "c0")
    ctx.current = "r0"
    ctx.save()


SEED_SQL = (
    "INSERT INTO devices (id, protocol) VALUES ('e2e-dev', 'modbus_tcp') ON CONFLICT DO NOTHING; "
    "INSERT INTO tags (path, device_id) VALUES ('e2e/tag', 'e2e-dev') ON CONFLICT DO NOTHING; "
    "INSERT INTO tag_history (time, tag_id, value_num, quality) "
    "SELECT ts, (SELECT id FROM tags WHERE path = 'e2e/tag'), extract(epoch FROM ts), 1 "
    "FROM generate_series(now() - interval '5 days', now() - interval '1 hour', "
    "interval '1 minute') ts ON CONFLICT DO NOTHING; "
    "SELECT count(compress_chunk(c, if_not_compressed => true)) "
    "FROM show_chunks('tag_history', older_than => interval '2 days') c;"
)


def seed_history(host: Vm) -> None:
    host.run(
        "cid=$(sudo docker ps -q --filter label=com.docker.compose.project=pureiron "
        "--filter label=com.docker.compose.service=site-db) && "
        f'sudo docker exec "$cid" psql -v ON_ERROR_STOP=1 -U pureiron -d pureiron -c "{SEED_SQL}"'
    )


def wait_ready(host: Vm, timeout_s: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    snap: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            snap = probe(host)
        except (VmError, subprocess.TimeoutExpired):
            time.sleep(5)
            continue
        if ready(snap):
            return snap
        time.sleep(5)
    return snap


# ---------------------------------------------------------------- 적용·수렴


def stage(host: Vm, pib: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    host.put(pib, f"{INCOMING}/{pib.name}")
    result = subprocess.CompletedProcess[str]([], 2, "", "busy")
    for _ in range(30):  # 서비스 tick이 잠금을 잡고 있으면 exit 2 — 잠시 뒤 다시
        result = host.run(f"sudo pureiron-site apply {INCOMING}/{pib.name}", check=False)
        if result.returncode != 2:
            break
        time.sleep(2)
    if check and result.returncode != 0:
        raise E2EError(f"apply {pib.name} exited {result.returncode}: {result.stderr[-800:]}")
    return result


def converge(host: Vm, gen: int, timeout_s: float = CONVERGE_S) -> tuple[dict[str, object], float]:
    """시도가 끝나(attempt 없음) `last_attempted_gen == gen`이고 ready가 될 때까지."""
    started = time.monotonic()
    snap: dict[str, object] = {}
    while time.monotonic() - started < timeout_s:
        try:
            snap = probe(host)
        except (VmError, subprocess.TimeoutExpired):
            time.sleep(5)
            continue
        state = updater_state(snap)
        done = state.get("attempt") is None and state.get("last_attempted_gen") == gen
        if done and state.get("last_result") in ("succeeded", "failed") and ready(snap):
            return snap, time.monotonic() - started
        time.sleep(5)
    return snap, time.monotonic() - started


def report_for(host: Vm, pib: Path) -> dict[str, object]:
    """그 번들의 최근 보고. 파일 이름 `<started>-g<gen>-<sha256 12자>.json`에서 sha로 찾는다 —
    커밋 전 거부는 gen을 믿지 않아 `g unknown`이므로 gen으로는 못 찾는다."""
    sha12 = hashlib.sha256(pib.read_bytes()).hexdigest()[:12]
    out = host.run(
        f"sudo sh -c 'ls -t /var/lib/pureiron/state/reports/*-{sha12}.json "
        f"/var/lib/pureiron/state/reports/exported/*-{sha12}.json 2>/dev/null | head -1 | "
        "xargs -r cat'",
        check=False,
    ).stdout
    return cast(dict[str, object], json.loads(out)) if out.strip() else {}


def edit_unlocked(snap: dict[str, object]) -> bool:
    project = cast(dict[str, object], snap.get("project") or {})
    body = cast(dict[str, object], project.get("body") or {})
    return project.get("status") == 200 and body.get("edit_locked") is False


def core_started_at(snap: dict[str, object]) -> object:
    containers = cast(dict[str, dict[str, object]], snap.get("containers") or {})
    return containers.get("core", {}).get("started_at")


def apply_and_check(
    ctx: Context, name: str, gen: int, release: str, *, expect: str, prev: str
) -> dict[str, object]:
    host = vm()
    pib = build_site_bundle(ctx, gen, release, datetime.now(UTC) - timedelta(minutes=5))
    started = time.monotonic()
    stage(host, pib, check=False)
    snap, waited = converge(host, gen)
    took = time.monotonic() - started
    state, report = updater_state(snap), report_for(host, pib)
    want = ctx.digests[release if expect == "succeeded" else prev]
    ok = state.get("last_result") == expect and running_digests(snap) == want
    detail: dict[str, object] = {
        "gen": gen,
        "release": release,
        "result": state.get("last_result"),
        "code": report.get("code"),
        "seconds": round(took, 1),
        "images_match": running_digests(snap) == want,
        "epoch": cast(dict[str, object], snap.get("db") or {}).get("epoch"),
        "verify_s": report.get("verify_s"),
        "load_s": report.get("load_s"),
        "phase_durations_s": report.get("phase_durations_s"),
    }
    ctx.record(name, ok, **detail)
    host.run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)
    return {**detail, "snapshot": snap, "converge_s": waited}


def stage_untouched(
    ctx: Context,
    name: str,
    host: Vm,
    pib: Path,
    *,
    want_exit: int,
    want_code: str | None,
    want: str,
) -> None:
    """적용되면 안 되는 번들: CLI 종료 코드·거부 코드, core `StartedAt`·실행 이미지 불변."""
    before = probe(host)
    result = stage(host, pib, check=False)
    after = probe(host)
    unchanged = core_started_at(after) == core_started_at(before)
    images_ok = running_digests(after) == ctx.digests[want]
    code = report_for(host, pib).get("code")
    ctx.record(
        name,
        result.returncode == want_exit and code == want_code and unchanged and images_ok,
        exit=result.returncode,
        code=code,
        core_started_at_unchanged=unchanged,
        images_match=images_ok,
    )
    host.run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)


def restore_c0() -> None:
    reclaim_host_memory()
    hyperv.restore(VM_NAME, "c0")
    vm().wait_ssh()


def step_scenarios(ctx: Context) -> None:
    gen = ctx.next_gen
    # 거부 코드(VM 수동 1회): 다른 현장, 변조 — c0(applied g1 = r0) 위에서
    other = BUNDLES / ctx.run_id / f"pureiron-site-b-g{gen}.pib"
    wsl(
        f"uv run --no-sync python tests/e2e/m2d/bundles.py site --work {WSL_HOME}/work "
        f"--out '{golden.wsl_path(other.parent)}' --gen {gen} --product {RELEASES['r1']} "
        "--window-start 2026-01-01T00:00:00Z --window-end 2027-01-01T00:00:00Z --site site-b"
    )
    # 같은 gen(1) 재빌드 — 내용이 달라도 gen 규칙상 멱등 no-op(R-7).
    same = build_site_bundle(ctx, 1, "r1", datetime.now(UTC) - timedelta(days=1))
    same = same.replace(same.with_name(f"pureiron-{SITE}-g1-r1.pib"))
    tampered = same.with_name("tampered.pib")
    data = bytearray(build_site_bundle(ctx, gen, "r1", datetime.now(UTC)).read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))
    restore_c0()
    host = vm()
    for label, pib, code in (("site", other, "E_SITE"), ("tampered", tampered, "E_DIGEST")):
        stage_untouched(ctx, f"reject {label}", host, pib, want_exit=1, want_code=code, want="r0")
    stage_untouched(
        ctx, "same gen rebuild → no-op", host, same, want_exit=0, want_code=None, want="r0"
    )

    # c0(epoch 1, 압축 chunk)에서 r0→r1
    restore_c0()
    up = apply_and_check(
        ctx, "r0→r1 on compressed epoch-1 DB", gen, "r1", expect="succeeded", prev="r0"
    )
    db = cast(dict[str, object], cast(dict[str, object], up["snapshot"]).get("db") or {})
    ctx.record(
        "r1 schema after upgrade",
        db.get("epoch") == "2"
        and "tag_history_1h" in str(db.get("caggs"))
        and int(str(db.get("compressed_chunks") or 0)) > 0,
        db=db,
    )
    # applied g2 위에서 g1 → E_GEN(낮은 gen). g2보다 뒤에 빌드해야 한다 — 먼저 만든 번들은
    # cert 단계의 `max_seen_created_at` 역행(E_CERT)에 먼저 걸린다(첫 실측).
    lower = build_site_bundle(ctx, 1, "r1", datetime.now(UTC) - timedelta(days=1))
    stage_untouched(ctx, "reject lower gen", vm(), lower, want_exit=1, want_code="E_GEN", want="r1")
    # c0에서 r1' → 5분 안에 r0 복귀 + failed
    restore_c0()
    rb = apply_and_check(
        ctx, "r1' health failure → rollback", gen, "r1f", expect="failed", prev="r0"
    )
    ctx.record(
        "rollback within 5 min to r0 digests",
        bool(rb["images_match"]) and float(str(rb["seconds"])) <= ROLLBACK_LIMIT_S,
        seconds=rb["seconds"],
        code=rb["code"],
    )
    ctx.next_gen = gen + 1
    ctx.save()


# ---------------------------------------------------------------- crash 주입


def crash_once(ctx: Context, phase: str, mode: str, *, gen: int, release: str, prev: str) -> None:
    host = vm()
    expect = "failed" if release == "r1f" else "succeeded"
    open_at = datetime.now(UTC) + timedelta(seconds=WINDOW_LEAD_S)
    pib = build_site_bundle(ctx, gen, release, open_at)
    host.put(pib, f"{INCOMING}/{pib.name}")
    target = "cli" if phase == "fetched" else "service"
    watch_mode = "kill" if mode == "kill" else "freeze"
    watcher = host.start(
        f"sudo python3 {REMOTE}/crash_at.py --generation {gen} --phase {phase} "
        f"--mode {watch_mode} --target {target} --timeout {WINDOW_LEAD_S + 600}"
    )
    time.sleep(2)
    stage_proc = host.start(f"sudo pureiron-site apply {INCOMING}/{pib.name}")
    line = watcher.stdout.readline().strip() if watcher.stdout else ""
    hit = line.startswith("HIT")
    if hit and mode == "reset":
        reclaim_host_memory()
        hyperv.restart_force(VM_NAME)
    stage_proc.wait(timeout=1800)
    watcher.wait(timeout=60)
    # fetched(CLI 검증 중) crash 뒤에는 서비스가 같은 시도를 이어받는다(재부팅이면 시작 시 recover).
    snap, waited = converge(vm(), gen)
    state, report = updater_state(snap), report_for(vm(), pib)
    want = ctx.digests[release if expect == "succeeded" else prev]
    unlocked = edit_unlocked(snap)
    ok = hit and state.get("last_result") == expect and running_digests(snap) == want and unlocked
    ctx.record(
        f"crash {mode} @ {phase}",
        ok,
        gen=gen,
        release=release,
        watcher=line,
        result=state.get("last_result"),
        code=report.get("code"),
        converge_s=round(waited, 1),
        images_match=running_digests(snap) == want,
        edit_unlocked=unlocked,
        alarm=state.get("alarm"),
    )
    vm().run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)


def step_crash(ctx: Context) -> None:
    gen = ctx.next_gen
    # migrating: 실제 마이그레이션이 일어나도록 c0(epoch 1)에서 r0→r1
    for mode in ("kill", "reset"):
        restore_c0()
        crash_once(ctx, "migrating", mode, gen=gen, release="r1", prev="r0")
    gen += 1
    current = "r1"
    for phase in CRASH_PHASES:
        if phase == "migrating":
            continue
        for mode in ("kill", "reset"):
            if phase == "rolling_back":
                crash_once(ctx, phase, mode, gen=gen, release="r1f", prev=current)
            else:
                target = "r0" if current == "r1" else "r1"
                crash_once(ctx, phase, mode, gen=gen, release=target, prev=current)
                current = target
            gen += 1
    ctx.next_gen, ctx.current = gen, current
    ctx.save()


# ---------------------------------------------------------------- 보고


def step_report(ctx: Context) -> None:
    host = vm()
    nft_after = nft_counters(host)
    external = {k: v for k, v in nft_after.items() if v}
    ctx.record(
        "no external connection attempts (nftables counters)",
        not external,
        before=ctx.nft_before,
        after=nft_after,
    )
    ctx.out.mkdir(parents=True, exist_ok=True)
    metrics = ctx.out / "bundle-metrics.json"
    metrics.write_text(json.dumps(ctx.bundles, indent=2, ensure_ascii=False) + "\n", "utf-8")
    failed = [r["name"] for r in ctx.results if not r["ok"]]
    summary = {"run_id": ctx.run_id, "passed": len(ctx.results) - len(failed), "failed": failed}
    (ctx.out / "results.json").write_text(
        json.dumps({**summary, "results": ctx.results}, indent=2, ensure_ascii=False) + "\n",
        "utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


RUNNERS = {
    "prepare": step_prepare,
    "vm": step_vm,
    "install": step_install,
    "scenarios": step_scenarios,
    "crash": step_crash,
    "report": step_report,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="m2d")
    parser.add_argument("command", choices=("all", "destroy", *STEPS))
    parser.add_argument("--from", dest="start", choices=STEPS, default=None)
    args = parser.parse_args(argv)
    if args.command == "destroy":
        hyperv.remove(VM_NAME, E2E / "vms" / VM_NAME / "disk.vhdx")
        return 0
    fresh = args.command == "prepare" or (args.command == "all" and args.start is None)
    if fresh:  # prepare는 새 실행(run id·결과)을 시작한다
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        ctx = Context(run_id=run_id, artifacts=str(REPO / "artifacts" / "e2e" / f"m2d-{run_id}"))
        ctx.save()
    else:
        ctx = Context.load()
    steps = STEPS if args.command == "all" else (args.command,)
    if args.start is not None:
        steps = STEPS[STEPS.index(args.start) :]
    for step in steps:
        log(f"== {step}")
        RUNNERS[step](ctx)
    failed = [r for r in ctx.results if not r["ok"]]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
