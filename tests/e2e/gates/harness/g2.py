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
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness import buildhost, golden, hyperv, judges, paths, release_pair, site, sites, trees
from harness.context import Context, E2EError, InfraError
from harness.release_pair import ReleasePair
from harness.site import Site
from pureiron_core.platform.migrate import load_catalog

RESULTS = paths.E2E / "results"
SIM_ADDRESS = "10.0.0.5"
"""fixture 장비 주소 — VM 안 dummy 인터페이스에 붙여 시뮬레이터가 답한다(E3)."""
READY_TIMEOUT_S = 300
LIVE_TIMEOUT_S = 300
"""c0 뒤 100태그 전부 GOOD·장비 connected까지 기다리는 시간(G2-1 "5분 안에")."""
GIT_TIMEOUT_S = 60
ITEM_G2_1 = "G2-1 install + 100 tags GOOD + device connected + no external"
SITE_ROLES: tuple[tuple[Site, str], ...] = ((sites.SITE_A, "base"), (sites.SITE_B, "next"))
"""현장 → 설치 release 역할(E2). site-a는 M6-4에서 BASE→HEAD 업그레이드 대상이라 BASE다."""


def site_release(pair: ReleasePair, site_id: str) -> str:
    """`site_id`가 g1에 설치하는 release 이름."""
    role = {s.site_id: r for s, r in SITE_ROLES}[site_id]
    return pair.base if role == "base" else pair.next


def results_dir(run_id: str) -> Path:
    return RESULTS / f"g2-{run_id}"


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


@dataclass
class Session:
    run_id: str
    head_sha: str
    pair: ReleasePair
    tree: Path
    simulator: Path
    sites: dict[str, SiteRun]

    @property
    def sim_image(self) -> str:
        return f"pureiron/simulator:{self.pair.next}-src"


def prepare(run_id: str, sha: str) -> Session:
    """WSL 동기화 → HEAD 빌드(NEXT 기록·시뮬레이터 tar) → 설치 번들 → 100태그 트리 → 현장별 g1
    번들. HEAD 빌드 실패는 `HeadBuildError`(제품)로 멈춘다."""
    out = paths.BUNDLES / run_id
    pair = repo_pair(sha)
    buildhost.sync_repo()
    next_record = buildhost.nightly_release(out / "nightly", pair, sha)
    base_path = paths.REPO / "deploy" / "releases" / f"{pair.base}.json"
    base_record: dict[str, object] = json.loads(base_path.read_text(encoding="utf-8"))
    judges.check_record(pair, pair.base, base_record)
    if next_record.get("version") != pair.releases[pair.next]:
        raise E2EError(f"{pair.next}: version {next_record.get('version')!r} != pair")
    digests = {
        pair.base: judges.digests_of(base_record),
        pair.next: judges.digests_of(next_record),
    }
    pib, fpr, boot = buildhost.install_bundle(out / "install", pair.releases[pair.base])
    tree = trees.g2_tree(out / "tree")
    runs: dict[str, SiteRun] = {}
    for base_site, _ in SITE_ROLES:
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
            ctx, 1, release, datetime.now(UTC) - timedelta(days=1), golden.wsl_path(tree)
        )
        runs[base_site.site_id] = SiteRun(ctx, release)
    return Session(run_id, sha, pair, tree, out / "nightly" / "simulator.tar", runs)


def bring_up(run: SiteRun, session: Session, ssh_pub: str) -> None:
    """VM 부팅(dummy 장비 주소) → 외부 접속 기준값 → 설치 runbook·g1 → 시뮬레이터 → ready → viewer
    → 체크포인트 c0."""
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
    )
    run.evidence["install_s"] = round(install_s, 1)
    site.start_simulator(host, session.simulator, session.sim_image)
    site.wait_ready(host, READY_TIMEOUT_S)
    run.evidence["viewer"] = site.add_viewer(host)
    site.nft_mark(ctx, host)
    ctx.nft_c0 = dict(ctx.nft_last)
    hyperv.checkpoint(ctx.site.vm_name, "c0")
    ctx.save()


def _guarded(run: SiteRun, session: Session, ssh_pub: str) -> None:
    """한 현장의 실패가 다른 현장을 멈추지 않게 예외를 그 현장의 결과로 둔다(그 현장 시험이 다시
    던진다)."""
    try:
        bring_up(run, session, ssh_pub)
    except Exception as exc:
        run.ctx.log(f"bring-up failed: {exc!r}")
        run.error = exc


def bring_up_all(session: Session, environ: dict[str, str] | None = None) -> None:
    """두 현장을 스레드로 동시에 올린다. `PUREIRON_G2_SERIAL=1`이면 차례로(메모리가 모자랄 때,
    R1)."""
    sites.check_host()
    golden.build_golden(sites.CLOUD_IMAGE, sites.GOLDEN)
    pub = golden.ensure_ssh_key(paths.SSH_DIR).read_text(encoding="utf-8")
    runs = list(session.sites.values())
    if (environ if environ is not None else dict(os.environ)).get("PUREIRON_G2_SERIAL") == "1":
        for run in runs:
            _guarded(run, session, pub)
        return
    with ThreadPoolExecutor(max_workers=len(runs)) as pool:
        for future in [pool.submit(_guarded, run, session, pub) for run in runs]:
            future.result()


def check_install(session: Session, site_id: str) -> dict[str, bool]:
    """G2-1 한 현장: 설치(ready·실행 이미지 == 그 현장 release digest·overlay2·classic) → 5분 안에
    100태그 전부 GOOD·장비 connected → 부팅 이후 외부 접속 0. 판정을 기록하고 돌려준다. 부팅·설치가
    실패했으면 그 예외를 다시 던진다."""
    run = session.sites[site_id]
    if run.error is not None:
        raise run.error
    ctx = run.ctx
    host = ctx.site.host()
    snap = site.probe(host)
    checks = judges.judge_installed(snap, ctx.digests[run.release])
    want = trees.g2_tag_paths()
    out, live, live_s = site.wait_live(
        lambda: site.live(host),
        lambda reply: judges.judge_live(reply, want, trees.SIM_DEVICE),
        LIVE_TIMEOUT_S,
    )
    checks |= live
    site.nft_mark(ctx, host)
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
        **run.evidence,
    )
    return checks
