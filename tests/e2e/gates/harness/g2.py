"""G2 nightly 세션(M6-2) — HEAD 빌드·번들(`prepare`) → 현장 VM 2대 부팅·설치·시뮬레이터(동시) →
체크포인트 `c0`, 그리고 G2-1 판정(`check_install`).

현장과 설치 release(E2): site-a = BASE(태그가 있는 최신 r*), site-b = NEXT(HEAD 빌드). 두 현장 모두
같은 100태그 트리(`trees.g2_tree`)를 g1에 싣는다. 결과·상태 파일은
`<E2E>/results/g2-<run>/<site>/`, 번들은 `<E2E>/bundles/<run>/`(현장별 하위 디렉터리)에 둔다.
현장 하나의 부팅·설치가 실패해도 다른 현장은 계속한다 — 예외는 `SiteRun.error`에 두고 그 현장의
시험이 다시 던진다(분류는 예외 종류, `failures`).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from harness import (
    buildhost,
    bundles,
    golden,
    hyperv,
    judges,
    paths,
    preflight,
    preinstall,
    release_pair,
    site,
    sites,
    trees,
)
from harness.context import Context, E2EError, InfraError
from harness.release_pair import ReleasePair
from harness.site import Site
from harness.vm import Vm
from pureiron_core.platform.migrate import load_catalog

RESULTS = paths.E2E / "results"
SIM_ADDRESS = "10.0.0.5"
"""fixture 장비 주소 — VM 안 dummy 인터페이스에 붙여 시뮬레이터가 답한다(E3)."""
READY_TIMEOUT_S = 300
LIVE_TIMEOUT_S = 300
"""c0 뒤 100태그 전부 GOOD·장비 connected까지 기다리는 시간(G2-1 "5분 안에")."""
GIT_TIMEOUT_S = 60
ITEM_G2_1 = "G2-1 install + 100 tags GOOD + device connected + no external"
VM_START_HEADROOM_GB = 2.0
"""VM을 시작하기 직전 Windows 가용 메모리에 VM 메모리 합 말고 더 있어야 하는 몫(`vm_start_need_gb`).
M6-2a 실측(2 GiB VM 2대, 가용 6.3 GB에서 시작 → VM 뒤 약 2.3 GB)에서 실행 중 최저가 1.25 GB였고(약
1 GB가 더 줄었다) 1 GB 아래에서는 WSL이 0x8007274c로 멈춘다 — VM을 띄운 뒤 2 GB가 남아야 실행 중에도
1 GB 근처를 지킨다. 3 GiB VM 2대면 3 * 2 + 2 = 8 GB로 preflight 기준(`Limits.min_mem_gb`)과 같다."""
VM_MEMORY_FILE = "vm-memory.json"
"""현장 결과 디렉터리에 남기는 VM 시작 직전 메모리 확인 기록(`make_room`)."""
RESTORE_MEMORY_FILE = "restore-memory.json"
"""현장 결과 디렉터리에 남기는 c0 복원 직전 메모리 확인 기록(`restore_c0`, 복원마다 한 항목)."""
ITEM_NO_EXTERNAL = "G2 no external since boot (all scenarios)"
EXPECTED_SCHEMA_FILE = "expected-schema.json"
"""결과 디렉터리에 남기는 D9 기대 스키마(`buildhost.expected_schema`)."""
SITE_ROLES: tuple[tuple[Site, str], ...] = ((sites.SITE_A, "base"), (sites.SITE_B, "next"))
"""현장 → 설치 release 역할(E2). site-a는 M6-4에서 BASE→HEAD 업그레이드 대상이라 BASE다."""


def site_release(pair: ReleasePair, site_id: str) -> str:
    """`site_id`가 g1에 설치하는 release 이름."""
    role = {s.site_id: r for s, r in SITE_ROLES}[site_id]
    return pair.base if role == "base" else pair.next


RUN_ID = re.compile(r"\d{8}T\d{6}Z")
"""run id 형식(`new_run_id`) — 사람이 CLI·환경변수로 넣는 값이 경로를 벗어나지 않게(리뷰 L7)."""


def check_run_id(run_id: str) -> str:
    if RUN_ID.fullmatch(run_id) is None:
        raise E2EError(f"run id must look like 20261110T180000Z, got {run_id!r}")
    return run_id


def results_dir(run_id: str) -> Path:
    return RESULTS / f"g2-{check_run_id(run_id)}"


def bundles_dir(run_id: str) -> Path:
    """이 run의 번들 디렉터리(`<BUNDLES>/<run>`) — 현장별 하위 디렉터리는 `Context.work`."""
    return paths.BUNDLES / check_run_id(run_id)


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _git(*args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(paths.REPO), *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=GIT_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise InfraError(f"git {' '.join(args)} failed: {exc}") from exc
    return done.stdout


def head_sha() -> str:
    return _git("rev-parse", "HEAD").strip()


def repo_pair(sha: str) -> ReleasePair:
    """저장소 `deploy/releases/r*.json`·git 태그 `r*`·HEAD migration epoch로 nightly 쌍을 만든다."""
    records: dict[str, dict[str, object]] = {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((paths.REPO / "deploy" / "releases").glob("r*.json"))
    }
    tags = set(_git("tag", "--list", "r*").split())
    epoch = load_catalog(paths.REPO / "services" / "core" / "migrations").app_expected
    return release_pair.nightly(records, tags, sha, epoch)


@dataclass
class SiteRun:
    ctx: Context
    release: str
    error: BaseException | None = None
    evidence: dict[str, object] = field(default_factory=dict[str, object])
    """부팅·설치 단계의 측정값(ssh 대기·설치 초·viewer 생성 결과 — 비밀값 없음)."""
    c0_at: float | None = None
    """체크포인트 c0를 만든 시각(`time.monotonic`) — G2-1 "c0 뒤 5분"의 기준(M6-2a 리뷰 L3)."""
    live: LiveWatch | None = None
    """c0 직후부터 `bring_up` 안에서 지켜본 live 결과(`watch_live`) — 판정(`check_install`)이 다른
    현장 부팅 뒤에 늦게 시작해도 "c0 뒤 5분 안에"를 증명한다(리뷰 r2 L3, 직렬 모드 포함)."""
    c0_db: dict[str, object] = field(default_factory=dict[str, object])
    """체크포인트 c0 직전(이력 시드 뒤) probe `db` — c0 기반 판정(`judges.judge_c0_base`)."""
    unmarked_restores: int = 0
    """외부 접속 카운터를 표시하지 못하고(VM이 꺼져 있었다) 복원한 횟수 — 그 구간의 카운터는
    잃었으므로 맨 끝 판정(`check_no_external`)이 거짓이 된다."""
    preinstall: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    """G2-5 설치 전 변조 탐침 결과(`preinstall.run_probes`, site-b만) — 판정은 그 시험이 한다."""
    preinstall_errors: dict[str, BaseException] = field(default_factory=dict[str, BaseException])
    """탐침마다의 예외 — 그 시험이 다시 던진다(실제 설치는 막지 않는다)."""


@dataclass(frozen=True)
class LiveWatch:
    reply: dict[str, object]
    checks: dict[str, bool]
    seconds: float
    """c0부터 판정이 모두 참이 될 때까지(못 되면 마지막으로 읽은 때까지) 걸린 초."""


@dataclass
class Session:
    run_id: str
    head_sha: str
    pair: ReleasePair
    tree: Path
    """G2 100태그 트리(c0 g1 — `trees.g2_tree`)."""
    simulator: Path
    sites: dict[str, SiteRun]
    expected_schema: dict[str, object] = field(default_factory=dict[str, object])
    """D9 기대 스키마 — WSL의 빈 DB에 NEXT를 migrate한 뒤 probe와 같은 SQL로 읽은 이름 집합
    5종·epoch(`EXPECTED_SCHEMA_FILE`)."""
    release_trees: dict[str, Path] = field(default_factory=dict[str, Path])
    """NEXT·FAULT 2종의 번들 트리 — G2 트리에서 태그 `pair.tree_tag`의 description만 바꾼 것
    (`bundles.release_tree(source=tree)`). 판정 기대값(`trees.tree_expect`)도 같은 디렉터리에서
    읽는다."""

    def tree_of(self, release: str) -> Path:
        """`release` 번들이 싣는 트리 — BASE(c0 g1)는 G2 트리, 나머지는 `release_trees`."""
        return self.tree if release == self.pair.base else self.release_trees[release]

    @property
    def sim_image(self) -> str:
        return f"pureiron/simulator:{self.pair.next}-src"


def prepare(run_id: str, sha: str, only: Collection[str] | None = None) -> Session:
    """WSL 동기화 → HEAD 빌드(NEXT 기록·시뮬레이터 tar) → FAULT 2종(NEXT 파생, 기록·digest 확인)
    → 설치 번들 → 100태그 트리와 NEXT·FAULT 트리 → 현장별 `Context`·g1 번들. `only`(현장 id)를 주면
    그 현장만 `Context`·g1 번들을 만든다(재시도 세션이 고르지 않은 현장의 빈 `state.json`을 만들지
    않게, M6-3b 리뷰 L6) — 나머지 현장은 `Session.sites`에 없다(`site_up`이 `E2EError`). HEAD 빌드
    실패는 `HeadBuildError`(제품)로 멈춘다."""
    out = bundles_dir(run_id)
    pair = repo_pair(sha)
    buildhost.sync_repo()
    next_record = buildhost.nightly_release(out / "nightly", pair, sha)
    base_path = paths.REPO / "deploy" / "releases" / f"{pair.base}.json"
    base_record: dict[str, object] = json.loads(base_path.read_text(encoding="utf-8"))
    judges.check_record(pair, pair.base, base_record)
    if next_record.get("version") != pair.releases[pair.next]:
        raise E2EError(f"{pair.next}: version {next_record.get('version')!r} != pair")
    faults = buildhost.nightly_faults(pair, next_record)
    expected = buildhost.expected_schema(results_dir(run_id) / EXPECTED_SCHEMA_FILE, pair)
    for name, record in faults.items():
        judges.check_record(pair, name, record)
    digests = {
        pair.base: judges.digests_of(base_record),
        pair.next: judges.digests_of(next_record),
        **{name: judges.digests_of(record) for name, record in faults.items()},
    }
    pib, fpr, boot = buildhost.install_bundle(
        out / "install", pair.releases[pair.base], work=buildhost.NIGHTLY_WORK
    )
    tree = trees.g2_tree(out / "tree")
    release_trees = {
        name: bundles.release_tree(out / "trees" / name, name, pair, source=tree)
        for name in (pair.next, pair.fault, pair.fault_migrate)
    }
    runs: dict[str, SiteRun] = {}
    for base_site, _ in SITE_ROLES:
        if only is not None and base_site.site_id not in only:
            continue
        release = site_release(pair, base_site.site_id)
        state = results_dir(run_id) / base_site.site_id / "state.json"
        ctx = Context(
            run_id=run_id,
            artifacts=str(state.parent),
            install_pib=str(pib),
            runbook_fpr=fpr,
            runbook_bootstrap=boot,
            digests=digests,
            current=release,
            site=dataclasses.replace(base_site, state_file=state),
            pair=pair,
        )
        ctx.log(f"g1 {release} bundle (G2 tree)")
        buildhost.build_site_bundle(
            ctx,
            1,
            release,
            datetime.now(UTC) - timedelta(days=1),
            golden.wsl_path(tree),
            work=buildhost.NIGHTLY_WORK,
        )
        runs[base_site.site_id] = SiteRun(ctx, release)
    return Session(
        run_id,
        sha,
        pair,
        tree,
        out / "nightly" / "simulator.tar",
        runs,
        expected_schema=expected,
        release_trees=release_trees,
    )


BringUp = Callable[["SiteRun", "Session", str], None]
MakeRoom = Callable[[list["SiteRun"], Mapping[str, str]], "MemoryGate"]


def vm_start_need_gb(vms: int, environ: Mapping[str, str]) -> float:
    """VM `vms`대를 지금 시작하는 데 필요한 Windows 가용 메모리(GB) — VM 메모리
    (`sites.vm_memory_gib`) * 대수 + `VM_START_HEADROOM_GB`."""
    return vms * sites.vm_memory_gib(environ) + VM_START_HEADROOM_GB


@dataclass(frozen=True)
class MemoryGate:
    """VM 시작·체크포인트 복원 직전의 메모리 확보 결과(`secure_memory`)."""

    need_gb: float
    before_gb: float | None
    """회수 전 Windows 가용 메모리(GB, 못 읽으면 None)."""
    after_gb: float | None
    """회수·대기 뒤 마지막 측정값."""
    notes: list[str]

    @property
    def ok(self) -> bool:
        return self.after_gb is not None and self.after_gb >= self.need_gb

    def as_dict(self, **extra: object) -> dict[str, object]:
        return {
            **extra,
            "need_gb": self.need_gb,
            "before_gb": self.before_gb,
            "after_gb": self.after_gb,
            "ok": self.ok,
            "notes": self.notes,
        }


def secure_memory(
    need: float,
    *,
    mem_now: Callable[[], float | None] | None = None,
    reclaim: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MemoryGate:
    """WSL 페이지 캐시를 회수하고 Windows 가용 메모리가 `need` GB 이상이 될 때까지 기다린다
    (`preflight.reclaim_memory` — preflight와 같은 회수·대기). `mem_now`·`reclaim`이 없으면
    preflight 기본값(부를 때 모듈 속성으로 찾는다 — 단위 시험이 바꾼다)."""
    read = mem_now or preflight.windows_mem_gb
    before = read()
    after, notes = preflight.reclaim_memory(
        need, before, mem_now=read, reclaim=reclaim, sleep=sleep, clock=clock
    )
    return MemoryGate(need, before, after, notes)


def make_room(
    runs: list[SiteRun],
    environ: Mapping[str, str],
    *,
    mem_now: Callable[[], float | None] | None = None,
    reclaim: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MemoryGate:
    """`runs`의 VM을 시작하기 직전에 `secure_memory`로 Windows 가용 메모리가
    `vm_start_need_gb(len(runs))` 이상이 되게 한다. preflight의 측정은 HEAD 빌드·번들 전 값이라 그
    사이 WSL 캐시가 다시 찬다(M6-2b 리허설 R1: 두 번째 VM이 0x800705AA). 회수 전·후 값과 기다린
    시간은 현장 로그와 결과 디렉터리의 `VM_MEMORY_FILE`(확인마다 한 항목)에 남긴다. 모자랄 때 VM을
    시작하지 않고 현장 오류를 두는 것은 부른 쪽(`bring_up_all`)이 한다."""
    need = vm_start_need_gb(len(runs), environ)
    gate = secure_memory(need, mem_now=mem_now, reclaim=reclaim, sleep=sleep, clock=clock)
    record = gate.as_dict(sites=[run.ctx.site.site_id for run in runs])
    for run in runs:
        for note in gate.notes:
            run.ctx.log(note)
        _append_json(run.ctx.out / VM_MEMORY_FILE, record)
    return gate


def refuse_start(runs: list[SiteRun], gate: MemoryGate) -> None:
    """메모리가 모자라 VM을 시작하지 않는 현장마다 **새** `InfraError`를 둔다(메시지는 같다 — 한
    객체를 나눠 쓰면 pytest가 두 시험에서 같은 예외의 traceback을 겹쳐 쌓는다, M6-2b B-1)."""
    message = (
        f"windows available memory {gate.after_gb} GB < {gate.need_gb} GB needed to start "
        f"{len(runs)} VM(s) (VM memory + {VM_START_HEADROOM_GB} GB headroom) after reclaiming "
        "WSL page cache"
    )
    for run in runs:
        run.ctx.log(f"vm not started: {message}")
        run.error = InfraError(message)


def bring_up(run: SiteRun, session: Session, ssh_pub: str) -> None:
    """VM 부팅(dummy 장비 주소) → 외부 접속 기준값 → 설치 runbook·g1 → 시뮬레이터 → ready → 이력
    시드(압축 chunk, `site.seed_history`) → c0 기반 측정(`run.c0_db`, `Context.seed_rows`) → viewer
    → 체크포인트 c0. c0 기반 판정은 G2-1(`check_install`)이 한다."""
    ctx = run.ctx
    host = ctx.site.host()
    files = golden.seed_files(
        ctx.site.site_id,
        ctx.site.ip,
        ssh_pub,
        golden.Network(),
        field=trees.field_endpoints(session.tree),
        dummy=SIM_ADDRESS,
    )
    ctx.log("vm boot")
    run.evidence["ssh_after_s"] = round(
        sites.boot_vm(ctx.site, sites.site_vm_spec(ctx.site, os.environ), files), 1
    )
    ctx.nft_before = site.nft_counters(host)
    ctx.nft_last = dict(ctx.nft_before)
    ctx.nft_total = dict.fromkeys(ctx.nft_before, 0)
    ctx.nft_mark_at, _ = site.resolved_lookups(host, 0, ctx.site.site_id)
    ctx.log(f"install + g1 {run.release}")
    install_s = site.install_runbook(
        host,
        Path(ctx.install_pib),
        buildhost.site_bundle(ctx, 1, run.release),
        site_id=ctx.site.site_id,
        runbook_fpr=ctx.runbook_fpr,
        runbook_bootstrap=ctx.runbook_bootstrap,
        after_upload=preinstall_hook(run, host),
    )
    run.evidence["install_s"] = round(install_s, 1)
    site.start_simulator(host, session.simulator, session.sim_image)
    site.wait_ready(host, READY_TIMEOUT_S)
    ctx.log("seed history (compressed chunks)")
    site.seed_history(host)
    run.c0_db = judges.db_of(site.probe(host))
    ctx.seed_rows = judges.as_int(run.c0_db.get("tag_history_rows"))
    run.evidence["viewer"] = site.add_viewer(host)
    site.nft_mark(ctx, host)
    ctx.nft_c0 = dict(ctx.nft_last)
    hyperv.checkpoint(ctx.site.vm_name, "c0")
    run.c0_at = time.monotonic()
    ctx.save()
    run.live = watch_live(run)


def preinstall_hook(
    run: SiteRun, host: preinstall.Shell, *, probes: preinstall.Probes = preinstall.run_probes
) -> Callable[[], None] | None:
    """`preinstall.PREINSTALL_SITE`(site-b)이면 설치 번들을 올린 직후 부를 탐침(결과·예외를
    `run.preinstall`·`preinstall_errors`에 두고 설치를 계속한다), 다른 현장이면 None."""
    ctx = run.ctx
    if ctx.site.site_id != preinstall.PREINSTALL_SITE:
        return None
    book = preinstall.Runbook(
        f"{site.INCOMING}/{Path(ctx.install_pib).name}", ctx.runbook_fpr, ctx.runbook_bootstrap
    )

    def hook() -> None:
        ctx.log("pre-install tamper probes (copies of the install bundle)")
        run.preinstall, run.preinstall_errors = probes(host, book)
        errors = run.preinstall_errors
        for name, exc in errors.items():
            ctx.log(f"pre-install probe {name} failed: {exc!r}")

    return hook


def watch_live(
    run: SiteRun,
    *,
    live: Callable[[Vm], dict[str, object]] = site.live,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> LiveWatch:
    """c0(`run.c0_at`)부터 `LIVE_TIMEOUT_S` 안에 100태그 전부 GOOD·장비 connected가 되는지 지켜보고
    돌려준다. `bring_up`이 c0 직후에 불러 `run.live`에 남긴다."""
    host = run.ctx.site.host()
    want = trees.g2_tag_paths()
    reply, checks, seconds = site.wait_live(
        lambda: live(host),
        lambda got: judges.judge_live(got, want, trees.SIM_DEVICE),
        LIVE_TIMEOUT_S,
        clock=clock,
        sleep=sleep,
        started=run.c0_at,
    )
    return LiveWatch(reply, checks, seconds)


def _guarded(run: SiteRun, session: Session, ssh_pub: str, up: BringUp = bring_up) -> None:
    """한 현장의 실패가 다른 현장을 멈추지 않게 예외를 그 현장의 결과로 둔다(그 현장 시험이 다시
    던진다)."""
    try:
        up(run, session, ssh_pub)
    except Exception as exc:
        run.ctx.log(f"bring-up failed: {exc!r}")
        run.error = exc


def host_key() -> str:
    """Hyper-V 호스트 점검 → golden VHDX → e2e SSH 공개키."""
    sites.check_host()
    golden.build_golden(sites.CLOUD_IMAGE, sites.GOLDEN)
    return golden.ensure_ssh_key(paths.SSH_DIR).read_text(encoding="utf-8")


def selected_runs(session: Session, only: Collection[str] | None) -> list[SiteRun]:
    """`only`(없으면 전부)의 `SiteRun` — 고르지 않은 현장에는 `E2EError`를 둔다."""
    runs: list[SiteRun] = []
    for site_id, run in session.sites.items():
        if only is None or site_id in only:
            runs.append(run)
        else:
            run.error = E2EError(f"{site_id} not brought up: no selected test uses it")
    return runs


def bring_up_all(
    session: Session,
    environ: dict[str, str] | None = None,
    *,
    up: BringUp = bring_up,
    prepare_host: Callable[[], str] = host_key,
    room: MakeRoom = make_room,
    only: Collection[str] | None = None,
) -> None:
    """두 현장을 스레드로 동시에 올린다. VM을 시작하기 전에 `room`(`make_room`)으로 메모리를
    확보한다 — 동시 모드는 두 VM 몫으로 스레드를 띄우기 전에 한 번. 그 몫이 끝내 모자라도 VM 한
    대 몫(`vm_start_need_gb(1)`)은 되면 **직렬로 자동 전환**한다(B-3): 한 대 몫을 다시 재서
    띄우고, 다음 VM도 다시 잰다(앞 VM이 메모리를 잡은 뒤의 값). `PUREIRON_G2_SERIAL=1`이면 처음부터
    VM마다 그 몫으로 차례로(R1). 모자란 현장은 올리지 않는다(`SiteRun.error` = 현장마다 새
    `InfraError`). 어떻게 올렸는지는 현장 증거 `boot`에 남는다(G2-1 기록). `only`(현장 id)를 주면
    그 현장만 올린다 — 나머지는 `E2EError`를 두어 그 현장 시험이 돌면 바로 드러난다(3a 리뷰 L3:
    재시도·일부만 고른 실행이 쓰지 않는 VM을 띄우지 않게, `order.sites_needed`)."""
    runs = selected_runs(session, only)
    if not runs:
        return
    pub = prepare_host()
    env = environ if environ is not None else dict(os.environ)
    mode = "serial (PUREIRON_G2_SERIAL=1)"
    if env.get("PUREIRON_G2_SERIAL") != "1":
        gate = room(runs, env)
        if gate.ok:
            for run in runs:
                run.evidence["boot"] = "concurrent"
            with ThreadPoolExecutor(max_workers=len(runs)) as pool:
                for future in [pool.submit(_guarded, run, session, pub, up) for run in runs]:
                    future.result()
            return
        if gate.after_gb is None or gate.after_gb < vm_start_need_gb(1, env):
            refuse_start(runs, gate)
            return
        mode = f"serial (auto: {gate.after_gb} GB < {gate.need_gb} GB for {len(runs)} VMs)"
        for run in runs:
            run.ctx.log(f"boot {mode}")
    for run in runs:
        run.evidence["boot"] = mode
        gate = room([run], env)
        if gate.ok:
            _guarded(run, session, pub, up)
        else:
            refuse_start([run], gate)


def check_install(
    session: Session,
    site_id: str,
    *,
    probe: Callable[[Vm], dict[str, object]] = site.probe,
    live: Callable[[Vm], dict[str, object]] = site.live,
    mark: Callable[[Context, Vm], None] = site.nft_mark,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, bool]:
    """G2-1 한 현장: 설치(ready·실행 이미지 == 그 현장 release digest·overlay2·classic) · c0
    기반(이력 시드 뒤 압축 chunk·행 > 0·DB epoch == 그 현장 release epoch,
    `judges.judge_c0_base`) → c0 뒤 5분 안에 100태그 전부 GOOD·장비 connected → 부팅 이후 외부
    접속 0. 판정을 기록하고 돌려준다.
    "5분 안에"는 `bring_up`이 c0 직후부터 지켜본 결과(`run.live`)로 판정하고(`live_in_time`: c0부터
    `LIVE_TIMEOUT_S` 이내), 그것이 없을 때만 지금부터 지켜본다(그때 c0 + 300초가 이미 지났으면
    `live_in_time`이 거짓). 부팅·설치가 실패했으면 그 예외를 다시 던진다. VM 조작(`probe`·`live`·
    `mark`)과 시계는 단위 시험이 가짜로 바꾼다."""
    run = site_up(session, site_id)
    ctx = run.ctx
    host = ctx.site.host()
    snap = probe(host)
    checks = judges.judge_installed(snap, ctx.digests[run.release])
    checks |= judges.judge_c0_base(run.c0_db, session.pair.epochs[run.release])
    fresh = run.release == session.pair.next
    if fresh:  # D9 ⓒ: 새 설치(NEXT) c0의 이름 집합 5종 == 기대 스키마
        checks |= judges.judge_schema_equal(run.c0_db, session.expected_schema)
    want = trees.g2_tag_paths()
    watch = run.live or watch_live(run, live=live, clock=clock, sleep=sleep)
    out, live_s = watch.reply, watch.seconds
    checks |= watch.checks
    checks["live_in_time"] = live_s <= LIVE_TIMEOUT_S
    mark(ctx, host)
    checks["no_external"] = judges.no_external(ctx.nft_total)
    tags_body = judges.reply(out, "tags")[1]
    ctx.record(
        ITEM_G2_1,
        all(checks.values()),
        release=run.release,
        checks=checks,
        live_s=live_s,
        tags_not_good=judges.tags_not_good(tags_body, want)[:20],
        devices=judges.reply(out, "devices")[1],
        nft_total=ctx.nft_total,
        field_fwd=ctx.nft_total.get(golden.FIELD_RULE, 0),
        dns_lookups=ctx.dns_lookups,
        c0_db={k: run.c0_db.get(k) for k in ("epoch", "compressed_chunks", *judges.ROW_KEYS)},
        schema_diff=judges.schema_diff(run.c0_db, session.expected_schema) if fresh else None,
        **run.evidence,
    )
    return checks


# ---------------------------------------------------------------- 시나리오 공용(M6-3)


def restore_need_gb(running: bool, environ: Mapping[str, str]) -> float:
    """c0 복원 직전에 있어야 하는 Windows 가용 메모리(GB). 복원 대상 VM이 켜져 있으면
    `Restore-VMSnapshot`이 그 VM을 먼저 끄고(메모리 반환) `Start-VM`이 같은 양을 다시 잡으므로 VM
    몫은 이미 쓰고 있는 셈이다 — 실행 중 여유 `VM_START_HEADROOM_GB`만 요구한다(VM을 띄운 뒤 2 GB가
    남아야 실행 중 WSL 빌드로 줄어도 1 GB 근처를 지킨다, `VM_START_HEADROOM_GB` 근거). 꺼져 있으면
    새로 시작하는 것과 같아 VM 한 대 몫(`vm_start_need_gb(1)`)이다."""
    return VM_START_HEADROOM_GB if running else vm_start_need_gb(1, environ)


def _append_json(path: Path, entry: dict[str, object]) -> None:
    """`path`(JSON 배열)에 `entry`를 하나 더한다. 없거나 깨졌으면 새 배열."""
    try:
        old: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = []
    entries = cast(list[object], old) if isinstance(old, list) else []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([*entries, entry], indent=2), "utf-8")


def restore_c0(
    run: SiteRun,
    environ: Mapping[str, str] | None = None,
    *,
    state: Callable[[str], str | None] = hyperv.vm_state,
    mark: Callable[[Context, Vm], None] = site.nft_mark,
    restore: Callable[[str, str], None] = hyperv.restore,
    wait_ssh: Callable[[Vm], object] = lambda host: host.wait_ssh(),
    mem_now: Callable[[], float | None] | None = None,
    reclaim: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MemoryGate:
    """G2 시나리오의 시작: 그 현장 VM을 체크포인트 c0로 되돌린다(`restore_checkpoint`, 카운터 기준 =
    c0 값 `ctx.nft_c0`). `run.py`의 `site.restore_c0`(회수만 하고 기다리지 않는다)는 그대로다."""
    return restore_checkpoint(
        run,
        "c0",
        run.ctx.nft_c0,
        environ,
        state=state,
        mark=mark,
        restore=restore,
        wait_ssh=wait_ssh,
        mem_now=mem_now,
        reclaim=reclaim,
        sleep=sleep,
        clock=clock,
    )


def restore_checkpoint(
    run: SiteRun,
    snapshot: str,
    counters: Mapping[str, int],
    environ: Mapping[str, str] | None = None,
    *,
    state: Callable[[str], str | None] = hyperv.vm_state,
    mark: Callable[[Context, Vm], None] = site.nft_mark,
    restore: Callable[[str, str], None] = hyperv.restore,
    wait_ssh: Callable[[Vm], object] = lambda host: host.wait_ssh(),
    mem_now: Callable[[], float | None] | None = None,
    reclaim: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> MemoryGate:
    """현장 VM을 체크포인트 `snapshot`(c0·c1)으로 되돌린다. 순서: 외부 접속 카운터 표시(복원이
    카운터를 그 체크포인트 값으로 되돌리므로 그 전 구간을 누적 — VM이 꺼져 있으면 표시할 수 없어
    `unmarked_restores`를 올린다) → 메모리 확보(`restore_need_gb`, `secure_memory`) → 복원 →
    카운터 기준을 체크포인트 때 값(`counters`)으로 → ssh 대기. 메모리가 끝까지 모자라면 복원하지
    않고 `InfraError`(현장마다 새 객체). 회수 전·후 값은 현장 로그와 `RESTORE_MEMORY_FILE`에
    남긴다. Hyper-V·ssh·메모리 조작은 단위 시험이 가짜로 바꾼다."""
    ctx = run.ctx
    host = ctx.site.host()
    env = os.environ if environ is None else environ
    vm_state = state(ctx.site.vm_name)
    running = vm_state == "Running"
    if running:
        mark(ctx, host)
    else:
        run.unmarked_restores += 1
        ctx.log(f"vm {vm_state}: external-access counters since the last mark are lost")
    gate = secure_memory(
        restore_need_gb(running, env), mem_now=mem_now, reclaim=reclaim, sleep=sleep, clock=clock
    )
    for note in gate.notes:
        ctx.log(note)
    _append_json(ctx.out / RESTORE_MEMORY_FILE, gate.as_dict(vm_state=vm_state, snapshot=snapshot))
    if not gate.ok:
        raise InfraError(
            f"windows available memory {gate.after_gb} GB < {gate.need_gb} GB needed to restore "
            f"{ctx.site.vm_name} (vm {vm_state}) to {snapshot} after reclaiming WSL page cache"
        )
    ctx.log(f"restore {snapshot}")
    restore(ctx.site.vm_name, snapshot)
    ctx.nft_last = dict(counters)  # 복원은 카운터(메모리)도 체크포인트 시점으로 되돌린다
    ctx.save()
    wait_ssh(host)
    return gate


def site_up(session: Session, site_id: str) -> SiteRun:
    """`site_id`의 `SiteRun` — 부팅·설치가 실패한 현장이면 그 예외를 다시 던진다(분류는 예외
    종류). 이 세션이 준비하지 않은 현장(`prepare(only=)`)이면 `E2EError`."""
    run = session.sites.get(site_id)
    if run is None:
        raise E2EError(f"{site_id} not prepared: no selected test uses it")
    if run.error is not None:
        raise run.error
    return run


def check_no_external(
    session: Session,
    site_id: str,
    *,
    mark: Callable[[Context, Vm], None] = site.nft_mark,
) -> dict[str, bool]:
    """맨 끝 시험: 부팅 이후 모든 시나리오를 거친 누적 외부 접속 카운터(`ctx.nft_total` — 복원
    직전마다 표시해 더한다)가 0이고, 카운터를 잃은 복원이 없다. 판정을 기록하고 돌려준다."""
    run = site_up(session, site_id)
    ctx = run.ctx
    mark(ctx, ctx.site.host())
    checks = {
        "no_external": judges.no_external(ctx.nft_total),
        "every_interval_counted": run.unmarked_restores == 0,
    }
    ctx.record(
        ITEM_NO_EXTERNAL,
        all(checks.values()),
        checks=checks,
        nft_total=ctx.nft_total,
        field_fwd=ctx.nft_total.get(golden.FIELD_RULE, 0),
        dns_lookups=ctx.dns_lookups,
        unmarked_restores=run.unmarked_restores,
    )
    return checks
