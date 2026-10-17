"""M2d Hyper-V VM 시나리오(plan M2 종료 조건, PD-24). Windows 호스트에서 실행한다.

    uv run python tests/e2e/m2d/run.py all                # 처음부터 끝까지
    uv run python tests/e2e/m2d/run.py all --from crash   # 앞 단계 결과(context)를 이어서
    uv run python tests/e2e/m2d/run.py destroy            # VM·차분 디스크 삭제(golden은 둔다)

release 쌍은 `release_pair.py`(`BASE`→`NEXT`, 헬스 실패 `FAULT`)가 정한다 — S4: r2(epoch 3)→r3
(epoch 4), fault r3'(`r3f`). 아래의 BASE·NEXT·FAULT는 그 값이다.

단계(`STEPS`):
1. `prepare` — WSL runner의 저장소 사본에서 FAULT 기록, 설치 번들(실빌드: deb 수집·Updater
   venv), g1(BASE) 현장 번들. release 기록의 epoch가 `EPOCHS`와 다르면 멈춘다.
2. `vm` — golden VHDX 위 차분 디스크 + seed ISO로 `pureiron-site-a`(2 vCPU/4 GiB, 내부 전용 망).
3. `install` — runbook 2단계(전용 디렉터리에 메타데이터만 추출 → 지문·sha256 대조 →
   `bootstrap-verify.sh`) → `install.sh --offline-time-confirmed` → enroll → g1 apply → 서비스 시작.
   `/readyz` 200, classic image store(overlay2), 이미지 `.Id` == BASE config digest. 압축 chunk가
   있는 이력을 넣고(DB epoch == BASE epoch·BASE 산출물 있음·NEXT 테이블 없음 확인) 체크포인트
   `c0`를 만든다.
4. `scenarios` — `c0`에서 거부(다른 현장 `E_SITE`·변조 `E_DIGEST`) exit 1과 같은 gen 재빌드 no-op
   exit 0(R-7 멱등), 모두 core `StartedAt`·실행 이미지 불변. `c0`에서 BASE→NEXT(압축 DB 위 NEXT
   epoch migrate) 성공 + NEXT 스키마 확인 → 그 위에서 낮은 gen `E_GEN` 거부. `c0`에서 FAULT →
   5분 안에 BASE digest 복귀 + `failed`.
5. `crash` — phase마다 `systemctl kill -s KILL pureiron-updater`와 `Restart-VM -Force`를 1회씩.
   서비스가 그 시도를 실행하도록 창이 조금 뒤에 열리는 번들을 CLI로 스테이징하고(`verified` 대기),
   VM 쪽 `crash_at.py`가 해당 phase 진입 순간 프로세스를 얼린 뒤 kill/리셋한다. `migrating`은
   실제 마이그레이션이 일어나도록 `c0`에서 BASE→NEXT, `rolling_back`은 FAULT로, 나머지는
   NEXT↔BASE를 오간다. `fetched`(번들 검증)는 CLI를 대상으로. 매번 10분 안에 `succeeded`
   (rolling_back은 `failed`) + 실행 이미지 == 기대 release + 편집 잠금 해제로 수렴해야 한다.
6. `report` — nftables 외부 접속 카운터 0 확인, `bundle-metrics.json`, `results.json`(빠진 항목은
   `missing`).

결과: `artifacts/e2e/m2d-<시각>/`(gitignore). 진행 상태는 `C:\\PureIron_e2e\\state.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))

import golden
import hyperv
from release_pair import (
    BASE,
    BASE_COLUMNS,
    CAGGS,
    EPOCHS,
    FAULT,
    NEXT,
    NEXT_INDEXES,
    NEXT_TABLES,
    RELEASES,
)
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
INSTALL_RELEASE = RELEASES[BASE]
"""설치 번들 manifest `release`(이미지는 싣지 않는다 — 실행 release는 g1 현장 번들이 정한다)."""
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

# 결과 항목 이름 — release가 들어가는 것은 쌍에서 만든다. 전체 목록은 `expected_items()`.
ITEM_VM_BOOT = "vm boot (cloud-init, internal-only switch)"
ITEM_OS_QUIET = "OS quiet before install (nftables baseline)"
ITEM_INSTALL = f"install bundle bootstrap + g1({BASE}) on offline VM"
ITEM_REJECT_SITE, ITEM_REJECT_TAMPERED = "reject site", "reject tampered"
ITEM_SAME_GEN = "same gen rebuild → no-op"
ITEM_UPGRADE = f"{BASE}→{NEXT} on compressed epoch-{EPOCHS[BASE]} DB"
ITEM_SCHEMA = f"{NEXT} schema after upgrade"
ITEM_LOWER_GEN = "reject lower gen"
ITEM_FAULT = f"{NEXT}' health failure → rollback"
ITEM_ROLLBACK_TIME = f"rollback within 5 min to {BASE} digests"
ITEM_NO_EXTERNAL = "no external connection attempts since install (nftables, accumulated)"


class E2EError(RuntimeError):
    pass


@dataclass(frozen=True)
class CrashCase:
    phase: str
    mode: str
    """`kill`(서비스 SIGKILL) 또는 `reset`(VM 전원 리셋)."""
    release: str
    prev: str
    """주입 전 실행 중인 release — 실패(FAULT)면 이것으로 돌아가야 한다."""
    from_c0: bool = False
    """`c0`(BASE epoch, 압축 이력)로 복원한 뒤 주입한다 — 실제 마이그레이션이 일어나는 migrating."""

    @property
    def name(self) -> str:
        return f"crash {self.mode} @ {self.phase}"


def crash_plan() -> list[CrashCase]:
    """crash 주입 순서. migrating은 c0에서 BASE→NEXT(두 번 모두 복원 후), rolling_back은 FAULT,
    나머지 phase는 직전 결과에서 NEXT↔BASE를 번갈아 간다(c0 이후 첫 대상 = NEXT 위에서 BASE)."""
    cases = [CrashCase("migrating", mode, NEXT, BASE, from_c0=True) for mode in ("kill", "reset")]
    current = NEXT
    for phase in CRASH_PHASES:
        if phase == "migrating":
            continue
        for mode in ("kill", "reset"):
            if phase == "rolling_back":
                cases.append(CrashCase(phase, mode, FAULT, current))
            else:
                target = BASE if current == NEXT else NEXT
                cases.append(CrashCase(phase, mode, target, current))
                current = target
    return cases


def expected_items() -> list[str]:
    """`all` 한 번이 남기는 결과 항목 이름(30개, 순서대로) — 실측끼리 30/30으로 비교한다."""
    return [
        ITEM_VM_BOOT,
        ITEM_OS_QUIET,
        ITEM_INSTALL,
        ITEM_REJECT_SITE,
        ITEM_REJECT_TAMPERED,
        ITEM_SAME_GEN,
        ITEM_UPGRADE,
        ITEM_SCHEMA,
        ITEM_LOWER_GEN,
        ITEM_FAULT,
        ITEM_ROLLBACK_TIME,
        *(case.name for case in crash_plan()),
        ITEM_NO_EXTERNAL,
    ]


@dataclass
class Context:
    run_id: str
    artifacts: str
    install_pib: str = ""
    runbook_fpr: str = ""
    runbook_bootstrap: str = ""
    digests: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    next_gen: int = 2
    current: str = BASE
    results: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    bundles: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    nft_before: dict[str, int] = field(default_factory=dict[str, int])
    nft_total: dict[str, int] = field(default_factory=dict[str, int])
    """VM 부팅 이후 누적 외부 접속 시도 — 복원·리셋으로 카운터가 되돌아가도 잃지 않게
    구간마다 더한다."""
    nft_last: dict[str, int] = field(default_factory=dict[str, int])
    nft_c0: dict[str, int] = field(default_factory=dict[str, int])
    nft_mark_at: int = 0
    """마지막 표시의 VM 시각(epoch s) — resolved 로그를 이 뒤부터 읽는다."""
    dns_lookups: dict[str, int] = field(default_factory=dict[str, int])
    """resolved가 조회한 이름별 횟수(로컬 호스트 이름 제외) — dns_out 출처 증거."""
    seed_rows: int = 0

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), "utf-8")

    @classmethod
    def load(cls) -> Context:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

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
    built = out / f"pureiron-{SITE}-g{gen}.pib"
    metrics = json.loads(built.with_name(built.stem + ".bundle-metrics.json").read_text("utf-8"))
    # 같은 gen의 다른 release(설치 g1=BASE, 시나리오 g1=NEXT)가 서로 덮어쓰지 않게
    # 이름에 release를 둔다.
    pib = built.replace(out / f"pureiron-{SITE}-g{gen}-{release}.pib")
    entry: dict[str, object] = {"gen": gen, "release": release, "file": pib.name, **metrics}
    ctx.bundles = [b for b in ctx.bundles if b.get("file") != pib.name] + [entry]
    ctx.save()
    return pib


def site_bundle(ctx: Context, gen: int, release: str) -> Path:
    return BUNDLES / ctx.run_id / f"pureiron-{SITE}-g{gen}-{release}.pib"


def digests_of(release_json: dict[str, object]) -> dict[str, str]:
    images = cast(dict[str, dict[str, str]], release_json["images"])
    return {svc: images[svc]["config_digest"] for svc in APP_SERVICES}


def check_record(name: str, record: dict[str, object]) -> None:
    """release 기록이 쌍의 기대(달력 버전·스키마 epoch·앱 envelope [epoch, epoch + 1])와 같은지 —
    다르면 잘못된 기반에서 30개를 돌리게 되므로 prepare에서 멈춘다. envelope는 롤백 판정(BASE
    앱이 NEXT epoch DB에서 ready)의 전제다."""
    schema = cast(dict[str, object], record.get("schema") or {})
    epoch = EPOCHS[name]
    got = (
        record.get("version"),
        schema.get("epoch"),
        schema.get("app_expected"),
        schema.get("app_max_compatible"),
    )
    want = (RELEASES[name], epoch, epoch, epoch + 1)
    if got != want:
        raise E2EError(
            f"{name}.json: (version, epoch, app_expected, app_max_compatible) {got} != {want}"
        )


def step_prepare(ctx: Context) -> None:
    log("prepare: WSL repo sync")
    sync_repo()
    log(f"prepare: fault release {FAULT} (from {NEXT})")
    wsl(f"uv run --no-sync python tests/e2e/m2d/bundles.py fault-release --work {WSL_HOME}/work")
    ctx.digests = {}
    for name in RELEASES:
        record = json.loads(wsl(f"cat {WSL_HOME}/work/releases/{name}.json"))
        check_record(name, record)
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
    log(f"prepare: g1 ({BASE}) site bundle")
    build_site_bundle(ctx, 1, BASE, datetime.now(UTC) - timedelta(days=1))
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
    ctx.nft_last = dict(ctx.nft_before)
    ctx.nft_total = dict.fromkeys(ctx.nft_before, 0)
    ctx.nft_mark_at, boot_lookups = resolved_lookups(host, 0)
    ctx.dns_lookups = {}
    ctx.record(ITEM_VM_BOOT, True, ssh_after_s=round(waited, 1))
    ctx.record(
        ITEM_OS_QUIET,
        not any(ctx.nft_before.values()),
        baseline=ctx.nft_before,
        dns_lookups=boot_lookups,
    )


def nft_counters(host: Vm) -> dict[str, int]:
    out = host.run("sudo nft -j list table inet e2e_audit").stdout
    counters: dict[str, int] = {}
    for entry in cast(list[dict[str, object]], json.loads(out)["nftables"]):
        rule = cast(dict[str, object] | None, entry.get("rule"))
        if rule is None:
            continue
        for expr in cast(list[dict[str, object]], rule.get("expr", [])):
            counter = cast(dict[str, int] | None, expr.get("counter"))
            if counter is not None:
                counters[str(rule.get("comment"))] = counter["packets"]
    return counters


def nft_accumulate(
    total: dict[str, int], last: dict[str, int], now: dict[str, int]
) -> dict[str, int]:
    """`last` 이후 늘어난 만큼 `total`에 더한 새 합계. 값이 줄었으면(표시 없이 재부팅)
    0부터 센다."""
    merged = dict(total)
    for key, value in now.items():
        before = last.get(key, 0)
        merged[key] = merged.get(key, 0) + (value - before if value >= before else value)
    return merged


LOOKUP = re.compile(r"Looking up RR for (\S+) IN \S+")


def dns_names(log: str) -> dict[str, int]:
    """resolved 디버그 로그의 조회 이름별 횟수(로컬에서 답하는 호스트 이름 제외)."""
    counts: dict[str, int] = {}
    for found in LOOKUP.findall(log):
        name = found.rstrip(".")
        if name not in (SITE, "localhost"):
            counts[name] = counts.get(name, 0) + 1
    return counts


def resolved_lookups(host: Vm, since: int) -> tuple[int, dict[str, int]]:
    """(지금 VM 시각, `since` 이후 resolved가 조회한 이름별 횟수). 0이면 부팅부터."""
    flag = f"--since @{since} " if since else "-b "
    out = host.run(
        f"date +%s; sudo journalctl -u systemd-resolved {flag}--no-pager -o cat", check=False
    ).stdout
    first, _, log = out.partition("\n")
    return as_int(first.strip()), dns_names(log)


def nft_mark(ctx: Context, host: Vm) -> None:
    """마지막 표시 이후의 외부 접속 시도를 누적한다. 체크포인트 복원·전원 리셋은 카운터를
    되돌리므로 그 직전에 표시하고, 직후 기준을 c0 값·0으로 다시 잡는다. 그동안 resolved가
    조회한 이름도 모은다(출처 증거)."""
    now = nft_counters(host)
    ctx.nft_total = nft_accumulate(ctx.nft_total, ctx.nft_last, now)
    ctx.nft_last = now
    ctx.nft_mark_at, names = resolved_lookups(host, ctx.nft_mark_at)
    for name, count in names.items():
        ctx.dns_lookups[name] = ctx.dns_lookups.get(name, 0) + count
    ctx.save()


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


def as_int(value: object) -> int:
    """probe 값(문자열 숫자, 실패 시 `{"error": ...}`)을 정수로 — 못 읽으면 -1."""
    try:
        return int(str(value))
    except ValueError:
        return -1


def judge_attempt(
    snap: dict[str, object],
    report: dict[str, object],
    *,
    expect: str,
    want_digests: dict[str, str],
    want_applied: int | None,
) -> dict[str, bool]:
    """끝난 시도 판정 항목(모두 참이어야 통과). `last_result`는 `failed_manual`도 `failed`로
    접으므로(metrics.result_of) 자동 롤백 실패는 경보·보고의 `failed_manual`로 따로 본다."""
    state = updater_state(snap)
    return {
        "result": state.get("last_result") == expect,
        "no_manual_alarm": state.get("alarm") is None and report.get("failed_manual") is False,
        "applied_gen": state.get("applied_gen") == want_applied,
        "images_match": running_digests(snap) == want_digests,
        "edit_unlocked": edit_unlocked(snap),
    }


# ---------------------------------------------------------------- 설치


def step_install(ctx: Context) -> None:
    host = vm()
    install = Path(ctx.install_pib)
    g1 = site_bundle(ctx, 1, BASE)
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
    digests_ok = running_digests(snap) == ctx.digests[BASE]
    installed = ready(snap) and digests_ok and docker.get("driver") == "overlay2" and classic
    c0: dict[str, object] = {}
    if installed:  # 이력 시드 → c0 기반(BASE epoch + 압축 chunk 이력) 확인
        seed_history(host)
        c0 = db_of(probe(host))
        ctx.seed_rows = as_int(c0.get("tag_history_rows"))
    c0_checks = judge_c0(c0)
    ok = installed and all(c0_checks.values())
    ctx.record(
        ITEM_INSTALL,
        ok,
        install_s=round(install_s, 1),
        storage_driver=docker.get("driver"),
        classic_store=classic,
        release=BASE,
        images_match=digests_ok,
        readyz=cast(dict[str, object], snap.get("readyz") or {}).get("status"),
        c0_checks=c0_checks,
        c0_db=c0,
    )
    if not ok:  # 잘못된 기반 위에 c0를 찍고 이어 가지 않는다
        raise E2EError("install failed — see results")
    host.run("sudo rm -f " + f"{INCOMING}/{install.name} {INCOMING}/{g1.name}")
    nft_mark(ctx, host)
    ctx.nft_c0 = dict(ctx.nft_last)
    hyperv.checkpoint(VM_NAME, "c0")
    ctx.current = BASE
    ctx.save()


def db_of(snap: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], snap.get("db") or {})


def names(db: dict[str, object], key: str) -> frozenset[str]:
    """probe의 쉼표 구분 이름 목록을 집합으로. 못 읽었으면(`{"error": ...}`) 빈 집합."""
    value = db.get(key)
    return frozenset(n for n in value.split(",") if n) if isinstance(value, str) else frozenset()


def judge_c0(db: dict[str, object]) -> dict[str, bool]:
    """체크포인트 c0 기반: BASE epoch와 그 산출물, 압축 chunk가 있는 이력, NEXT 산출물 없음 —
    업그레이드가 압축 DB 위에서 실제 마이그레이션을 하고 이력 보존(`rows_kept`)을 잴 수 있어야
    한다."""
    tables = names(db, "tables")
    return {
        "epoch": db.get("epoch") == str(EPOCHS[BASE]),
        "base_schema": names(db, "caggs") == CAGGS
        and names(db, "realtime_caggs") == CAGGS
        and names(db, "columns") >= BASE_COLUMNS,
        "next_absent": bool(tables) and tables.isdisjoint(NEXT_TABLES),
        "compressed_chunks": as_int(db.get("compressed_chunks")) > 0,
        "seed_rows": as_int(db.get("tag_history_rows")) > 0,
    }


def judge_next_schema(db: dict[str, object]) -> dict[str, bool]:
    """업그레이드 뒤 NEXT(r3) 스키마: epoch 4, 0010~0012 테이블·인덱스, BASE 산출물 유지(1분·1시간
    CAGG와 실시간 집계, 0008·0009 열), 압축 chunk 유지."""
    return {
        "epoch": db.get("epoch") == str(EPOCHS[NEXT]),
        "next_tables": names(db, "tables") >= NEXT_TABLES,
        "next_indexes": names(db, "indexes") >= NEXT_INDEXES,
        "caggs": names(db, "caggs") == CAGGS,
        "realtime_caggs": names(db, "realtime_caggs") == CAGGS,
        "base_columns": names(db, "columns") >= BASE_COLUMNS,
        "compressed_chunks": as_int(db.get("compressed_chunks")) > 0,
    }


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
    with pib.open("rb") as f:
        sha12 = hashlib.file_digest(f, "sha256").hexdigest()[:12]
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
    applied_before = updater_state(probe(host)).get("applied_gen")
    started = time.monotonic()
    stage(host, pib, check=False)
    snap, waited = converge(host, gen)
    took = time.monotonic() - started
    state, report = updater_state(snap), report_for(host, pib)
    want = ctx.digests[release if expect == "succeeded" else prev]
    checks = judge_attempt(
        snap,
        report,
        expect=expect,
        want_digests=want,
        want_applied=gen if expect == "succeeded" else cast(int | None, applied_before),
    )
    rows = as_int(db_of(snap).get("tag_history_rows"))
    checks["rows_kept"] = rows >= ctx.seed_rows > 0
    ok = all(checks.values())
    detail: dict[str, object] = {
        "gen": gen,
        "release": release,
        "result": state.get("last_result"),
        "code": report.get("code"),
        "seconds": round(took, 1),
        "checks": checks,
        "alarm": state.get("alarm"),
        "images_match": checks["images_match"],
        "epoch": db_of(snap).get("epoch"),
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


def restore_c0(ctx: Context) -> None:
    nft_mark(ctx, vm())
    reclaim_host_memory()
    hyperv.restore(VM_NAME, "c0")
    ctx.nft_last = dict(ctx.nft_c0)  # 복원은 카운터(메모리)도 c0 시점으로 되돌린다
    ctx.save()
    vm().wait_ssh()


def step_scenarios(ctx: Context) -> None:
    gen = ctx.next_gen
    # 거부 코드(VM 수동 1회): 다른 현장, 변조 — c0(applied g1 = BASE) 위에서
    other = BUNDLES / ctx.run_id / f"pureiron-site-b-g{gen}.pib"
    wsl(
        f"uv run --no-sync python tests/e2e/m2d/bundles.py site --work {WSL_HOME}/work "
        f"--out '{golden.wsl_path(other.parent)}' --gen {gen} --product {RELEASES[NEXT]} "
        "--window-start 2026-01-01T00:00:00Z --window-end 2027-01-01T00:00:00Z --site site-b"
    )
    # 같은 gen(1) 재빌드(NEXT) — 내용이 달라도 gen 규칙상 멱등 no-op(R-7).
    same = build_site_bundle(ctx, 1, NEXT, datetime.now(UTC) - timedelta(days=1))
    tampered = same.with_name(f"pureiron-{SITE}-g{gen}-tampered.pib")
    data = bytearray(build_site_bundle(ctx, gen, NEXT, datetime.now(UTC)).read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered.write_bytes(bytes(data))
    restore_c0(ctx)
    host = vm()
    for name, pib, code in (
        (ITEM_REJECT_SITE, other, "E_SITE"),
        (ITEM_REJECT_TAMPERED, tampered, "E_DIGEST"),
    ):
        stage_untouched(ctx, name, host, pib, want_exit=1, want_code=code, want=BASE)
    stage_untouched(ctx, ITEM_SAME_GEN, host, same, want_exit=0, want_code=None, want=BASE)

    # c0(BASE epoch, 압축 chunk)에서 BASE→NEXT — 압축 DB 위에서 NEXT 마이그레이션이 실제로 돈다
    restore_c0(ctx)
    up = apply_and_check(ctx, ITEM_UPGRADE, gen, NEXT, expect="succeeded", prev=BASE)
    db = db_of(cast(dict[str, object], up["snapshot"]))
    schema_checks = judge_next_schema(db)
    ctx.record(ITEM_SCHEMA, all(schema_checks.values()), checks=schema_checks, db=db)
    # applied g2 위에서 g1 → E_GEN(낮은 gen). g2보다 뒤에 빌드해야 한다 — 먼저 만든 번들은
    # cert 단계의 `max_seen_created_at` 역행(E_CERT)에 먼저 걸린다(첫 실측).
    lower = build_site_bundle(ctx, 1, NEXT, datetime.now(UTC) - timedelta(days=1))
    stage_untouched(ctx, ITEM_LOWER_GEN, vm(), lower, want_exit=1, want_code="E_GEN", want=NEXT)
    # c0에서 FAULT → 5분 안에 BASE 복귀 + failed
    restore_c0(ctx)
    rb = apply_and_check(ctx, ITEM_FAULT, gen, FAULT, expect="failed", prev=BASE)
    ctx.record(
        ITEM_ROLLBACK_TIME,
        bool(rb["images_match"]) and float(str(rb["seconds"])) <= ROLLBACK_LIMIT_S,
        seconds=rb["seconds"],
        code=rb["code"],
    )
    ctx.next_gen = gen + 1
    ctx.save()


# ---------------------------------------------------------------- crash 주입


def boot_id(host: Vm) -> str:
    return host.run("cat /proc/sys/kernel/random/boot_id").stdout.strip()


def service_restarts(host: Vm) -> int:
    out = host.run("systemctl show -p NRestarts --value pureiron-updater").stdout
    return as_int(out.strip())


STAGE_TRIES = 30
TAIL_CHARS = 800


def stage_for_window(
    apply: Callable[[], subprocess.CompletedProcess[str]],
    open_at: datetime,
    *,
    watcher_alive: Callable[[], bool],
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    pause_s: float = 2,
) -> tuple[bool, dict[str, object]]:
    """창(`open_at`) 전에 CLI 스테이징을 끝냈는지와 그 진단 기록. 서비스 tick이 잠금을 잡고 있으면
    `apply`가 exit 2라 잠시 뒤 다시 한다. 판정: 마지막 시도가 exit 0이고 끝난 시각이 `open_at`
    전. 기록: 시도마다 rc·걸린 초·감시자 ssh가 아직 살아 있는지, 마지막 rc·시도 횟수·`open_at`
    까지 남은 초(음수 = 창 뒤), 판정이 거짓이면 stdout·stderr 끝부분."""
    tries: list[dict[str, object]] = []
    result = subprocess.CompletedProcess[str]([], 2, "", "")
    for _ in range(STAGE_TRIES):
        started = time.monotonic()
        result = apply()
        tries.append(
            {
                "rc": result.returncode,
                "s": round(time.monotonic() - started, 1),
                "watcher_alive": watcher_alive(),
            }
        )
        if result.returncode != 2:
            break
        time.sleep(pause_s)
    margin_s = (open_at - now()).total_seconds()
    ok = result.returncode == 0 and margin_s > 0
    info: dict[str, object] = {
        "rc": result.returncode,
        "tries": len(tries),
        "margin_s": round(margin_s, 1),
        "attempts": tries,
    }
    if not ok:
        info["stdout_tail"] = (result.stdout or "")[-TAIL_CHARS:]
        info["stderr_tail"] = (result.stderr or "")[-TAIL_CHARS:]
    return ok, info


def crash_once(ctx: Context, case: CrashCase, *, gen: int) -> None:
    host = vm()
    phase, mode, release = case.phase, case.mode, case.release
    expect = "failed" if release == FAULT else "succeeded"
    # 창은 번들에 서명되므로 빌드 전에 정한다. service 대상이면 CLI 스테이징이 창 전에 끝났는지
    # 확인한다 — 아니면 CLI가 phase를 직접 실행해 쉬고 있는 서비스를 죽이는 거짓 통과가 된다.
    open_at = datetime.now(UTC) + timedelta(seconds=WINDOW_LEAD_S)
    pib = build_site_bundle(ctx, gen, release, open_at)
    host.put(pib, f"{INCOMING}/{pib.name}")
    applied_before = updater_state(probe(host)).get("applied_gen")
    boot_before, restarts_before = boot_id(host), service_restarts(host)
    target = "cli" if phase == "fetched" else "service"
    watch_mode = "kill" if mode == "kill" else "freeze"
    watcher = host.start(
        f"sudo python3 {REMOTE}/crash_at.py --generation {gen} --phase {phase} "
        f"--mode {watch_mode} --target {target} --timeout {WINDOW_LEAD_S + 600}"
    )
    time.sleep(2)
    apply_cmd = f"sudo pureiron-site apply {INCOMING}/{pib.name}"
    staged_in_time: bool | None = None
    stage_proc: subprocess.Popen[str] | None = None
    stage_info: dict[str, object] = {"target": target, "ssh": shutil.which("ssh")}
    if target == "cli":
        stage_proc = host.start(apply_cmd, capture=False)
    else:
        staged_in_time, info = stage_for_window(
            lambda: host.run(apply_cmd, check=False),
            open_at,
            watcher_alive=lambda: watcher.poll() is None,
        )
        stage_info.update(info)
    line = watcher.stdout.readline().strip() if watcher.stdout else ""
    # 감시자는 HIT를 출력하고 곧 끝난다 — 리셋이 ssh를 끊기 전에 종료 코드를 받는다(rc 5 = 얼릴
    # 프로세스 없음).
    watcher_rc = watcher.wait(timeout=60)
    hit = line.startswith("HIT") and watcher_rc == 0
    if hit and mode == "reset":
        nft_mark(ctx, host)  # 리셋은 카운터를 0으로 되돌린다
        hyperv.restart_force(VM_NAME)
        ctx.nft_last = {}
    if stage_proc is not None:
        stage_info["rc"] = stage_proc.wait(timeout=1800)  # CLI 대상은 감시자가 죽인다
    snap, waited = converge(vm(), gen)
    report = report_for(vm(), pib)
    want = ctx.digests[release if expect == "succeeded" else case.prev]
    checks = judge_attempt(
        snap,
        report,
        expect=expect,
        want_digests=want,
        want_applied=gen if expect == "succeeded" else cast(int | None, applied_before),
    )
    checks["hit"] = hit
    if staged_in_time is not None:
        checks["staged_before_window"] = staged_in_time
    # 주입 효과: 리셋이면 boot id가 바뀌고, 서비스 kill이면 systemd 재시작 횟수가 는다.
    if mode == "reset":
        checks["rebooted"] = boot_id(vm()) != boot_before
    elif target == "service":
        checks["service_restarted"] = service_restarts(vm()) > restarts_before
    rows = as_int(db_of(snap).get("tag_history_rows"))
    checks["rows_kept"] = rows >= ctx.seed_rows > 0
    ctx.record(
        case.name,
        all(checks.values()),
        gen=gen,
        release=release,
        watcher=line,
        result=updater_state(snap).get("last_result"),
        code=report.get("code"),
        converge_s=round(waited, 1),
        checks=checks,
        stage=stage_info,
        alarm=updater_state(snap).get("alarm"),
        logs=report.get("logs") if not all(checks.values()) else None,
    )
    vm().run(f"sudo rm -f {INCOMING}/{pib.name}", check=False)


def step_crash(ctx: Context) -> None:
    # migrating은 c0(BASE epoch)로 복원해 같은 gen으로 두 번, 나머지는 하나씩 gen을 올린다.
    c0_gen = ctx.next_gen
    gen = c0_gen + 1
    plan = crash_plan()
    for case in plan:
        if case.from_c0:
            restore_c0(ctx)
            crash_once(ctx, case, gen=c0_gen)
        else:
            crash_once(ctx, case, gen=gen)
            gen += 1
    last = plan[-1]
    ctx.next_gen, ctx.current = gen, last.prev if last.release == FAULT else last.release
    ctx.save()


# ---------------------------------------------------------------- 보고


def step_report(ctx: Context) -> None:
    nft_mark(ctx, vm())
    ctx.record(
        ITEM_NO_EXTERNAL,
        not any(ctx.nft_total.values()),
        baseline=ctx.nft_before,
        total=ctx.nft_total,
        dns_lookups=ctx.dns_lookups,
    )
    ctx.out.mkdir(parents=True, exist_ok=True)
    metrics = ctx.out / "bundle-metrics.json"
    metrics.write_text(json.dumps(ctx.bundles, indent=2, ensure_ascii=False) + "\n", "utf-8")
    failed = [r["name"] for r in ctx.results if not r["ok"]]
    seen = {r["name"] for r in ctx.results}
    summary = {
        "run_id": ctx.run_id,
        "pair": f"{BASE}→{NEXT} (fault {FAULT})",
        "passed": len(ctx.results) - len(failed),
        "expected": len(expected_items()),
        "failed": failed,
        "missing": [name for name in expected_items() if name not in seen],
    }
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
