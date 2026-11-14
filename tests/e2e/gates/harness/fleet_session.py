"""fleet 세션(M6-4a A4 — 계획 ★P1, A2·A3·A5를 묶는다) — fleet 시험이 골랐을 때만 한 번 만든다.

`prepare_c1`(세션 fixture `fleet_c1`):
① 도구 브랜치(★P2, `toolbranch`): V1 `e2e/nightly-tool`을 HEAD + `deploy/releases/<NEXT·FAULT
2종>.json` 커밋으로 force 갱신(git data API, circulation95 keyring — Actions `GITHUB_TOKEN`은
workflow 파일이 바뀐 커밋을 담은 ref 갱신을 거부한다). fleet-test workflow는 `@e2e/nightly-tool`을
본다.
② 새 중앙 up(`central.py up` — 새 DB·CA·토큰) → publish 호스트 동기화(`sync_publish_host`: CA 신뢰·
   ci/viewer 토큰 파일·NEXT·FAULT 이미지 hardlink는 `wsl -u root`로 `publish_host.py`, fleet-test
   secret `PUREIRON_FLEET_VIEWER_TOKEN`은 `gh secret set`의 stdin으로).
③ fleet-test 정리·사슬 대조(P8 `baseline.clear_leftovers`·`reconcile`): 사람이 연 PR이 있으면
`InfraError`(건너뜀), 남은 bot PR·`g2/*` 브랜치는 닫고 지운다, 사슬 `last_approved` ≠ main이면
되돌림 또는 trust-base.
④ acme-test G2 태그(★P3 `baseline.ensure_project_tag`): G2 트리(absorbs 비움)의 content_hash로 태그
   `v1<15자리>`, 없으면 브랜치 `g2/base`에 그 트리만 담은 커밋 + 태그(main은 건드리지 않는다).
⑤ 두 현장 c0 복원 → `site.site_online`(CA·`updater.env`·등록·token) → 기준선 PR(P2
`baseline.baseline`: site-a gen+1 BASE·site-b gen+1(없으면 1) NEXT, 같은 G2 태그, 늘 열린 1회 창) →
승인 → merge → publish가 새 중앙에 둘 다 게시 → 두 현장 적용·중앙 current 수렴.
⑥ c1 쌍: control-web(현장이 닿는 유일한 입구)을 멈추고 → 중앙 DB 덤프(그때 `/fleet` 요약 포함) → VM
   체크포인트 c1 x2 → control-web 시작. 덤프가 먼저라 VM의 보고 seq ≥ 중앙 수위다(복원 뒤
   heartbeat가 history로 밀리지 않는다).

`restore_c1`(fleet 시나리오의 시작): 사슬·main 대조(어긋나면 ③의 복구) → control-web 멈춤 → VM c1
복원(메모리 확보 포함, `g2.restore_checkpoint`) → 중앙 restore(덤프 때 `/fleet`과 일치 확인) →
control-web 시작 → 복원한 현장의 heartbeat가 c1 수위를 넘는지. control-web을 처음부터 끝까지 멈춰
두어 되돌리기 전의 VM이 복원된 중앙에, 복원된 VM이 되돌리기 전의 중앙에 닿지 않는다(F8).

c1(덤프 이름·체크포인트)은 이 pytest 프로세스가 만든 것만 쓴다 — infra 재시도 세션은 새 VM·새 중앙
위에 c1을 새로 만든다(`central.py up`이 지난 덤프를 지운다). 실패 분류: GitHub·WSL·Hyper-V 호출과
대기 초과는 `InfraError`, 기준선 publish(승인 판정·빌드·PUT)·적용 실패는 제품(`E2EError`).
"""

from __future__ import annotations

import json
import secrets
import shlex
import subprocess
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harness import baseline, buildhost, fleet, g2, golden, hyperv, judges, order, site, toolbranch
from harness.baseline import SITES
from harness.context import Context, E2EError, InfraError, log, write_json
from harness.fleet import Gh, Repo
from harness.vm import Vm

C1 = "c1"
VIEWER_SECRET = "PUREIRON_FLEET_VIEWER_TOKEN"
SESSION_FILE = Path("fleet") / "session.json"
"""결과 디렉터리 안 fleet 세션 기록(비밀값 없음) — 재시도 전 `first/`로 옮긴다."""
PUBLISH_HOST_PY = "tests/e2e/gates/harness/publish_host.py"
ROOT_PYTHON = "cd /home/shwang/m2d-e2e/repo && PYTHONDONTWRITEBYTECODE=1 .venv/bin/python"
"""root 셸에서 저장소 사본 venv를 직접 부른다(`uv` 없이, 바이트코드를 쓰지 않는다)."""
CENTRAL_WORK = "/home/shwang/pureiron-e2e-central"
NIGHTLY_STORE = "/home/shwang/m2d-e2e/work-nightly/store"
CONVERGE_TIMEOUT_S = 600.0
FLEET_TIMEOUT_S = 120.0
HEARTBEAT_TIMEOUT_S = 90.0
"""복원 뒤 heartbeat가 다시 보이기까지 — poll 10초 x 3 + Updater 재시도·TLS 여유."""
POLL_S = 5.0
WSL_TIMEOUT_S = 600.0
# ---------------------------------------------------------------- 원격 호출(주입)


def _wsl_root(script: str, stdin: str | None = None) -> str:
    return golden.wsl(script, timeout_s=WSL_TIMEOUT_S, user="root", stdin=stdin)


@dataclass(frozen=True)
class Remote:
    """fleet 세션이 부르는 WSL·Hyper-V·VM 조작. 기본값이 실제 호출이다."""

    central: Callable[..., dict[str, object]] = buildhost.central
    wsl: Callable[..., str] = buildhost.wsl
    wsl_root: Callable[..., str] = _wsl_root
    checkpoint: Callable[[str, str], None] = hyperv.checkpoint
    restore_vm: Callable[[g2.SiteRun, str, Mapping[str, int]], object] = g2.restore_checkpoint
    restore_c0: Callable[[g2.SiteRun], object] = g2.restore_c0
    online: Callable[[Context, Vm, Path, str], dict[str, object]] = site.site_online
    probe: Callable[[Vm], dict[str, object]] = site.probe
    converge: Callable[[Vm, int], tuple[dict[str, object], float]] = field(
        default=lambda host, gen: site.converge(host, gen, CONVERGE_TIMEOUT_S)
    )
    wait_ready: Callable[[Vm], dict[str, object]] = field(
        default=lambda host: site.wait_ready(host, g2.READY_TIMEOUT_S)
    )
    mark: Callable[[Context, Vm], None] = site.nft_mark
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic


@dataclass
class Repos:
    fleet: Repo
    project: Repo
    v1: Repo

    @classmethod
    def default(cls) -> Repos:
        gh = Gh()
        return cls(
            Repo(gh, fleet.FLEET_REPO), Repo(gh, fleet.PROJECT_REPO), Repo(gh, fleet.V1_REPO)
        )


def central(remote: Remote, *args: str) -> dict[str, object]:
    """`central.py` 한 번 — WSL·스크립트 실패와 JSON 아님은 `InfraError`(중앙은 실행 환경)."""
    try:
        return remote.central(*args)
    except (golden.WslError, golden.WslScriptError, ValueError, IndexError) as exc:
        raise InfraError(f"central.py {' '.join(args[:1])} failed: {exc}") from exc


def host_json(remote: Remote, command: str) -> dict[str, object]:
    """root 셸에서 `publish_host.py <command>`(JSON 한 줄). 실패는 `InfraError`."""
    try:
        raw = remote.wsl_root(f"{ROOT_PYTHON} {PUBLISH_HOST_PY} {command}")
        return cast(dict[str, object], json.loads(raw.strip().splitlines()[-1]))
    except (golden.WslError, golden.WslScriptError, ValueError, IndexError) as exc:
        raise InfraError(f"publish_host.py {command.split(maxsplit=1)[0]} failed: {exc}") from exc


# ---------------------------------------------------------------- 세션 기록


@dataclass
class FleetSession:
    run_id: str
    c1_name: str
    """이 프로세스가 만든 덤프·c1 이름(`c1-<run>-<임의 8 hex>`) — 다른 세션 것을 쓰지 않는다."""
    ca: Path
    ca_sha256: str
    project_version: str
    baseline_sha: str
    """기준선 PR의 merge 커밋(= c1 때 main·`last_approved`)."""
    gens: dict[str, int]
    """c1 때 현장별 fleet 파일 gen(= 중앙 `max_published_gen`)."""
    c1_fleet: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    """덤프 때 `/fleet` 요약(`central.fleet_view`)."""
    c1_nft: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    """c1 체크포인트 때 현장별 외부 접속 카운터 — 복원 뒤 기준."""
    evidence: dict[str, object] = field(default_factory=dict[str, object])
    chain: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    """사슬 대조 기록(세션 시작·복원마다) — `summary.json`의 `fleet`에 남는다."""
    restores: int = 0

    def save(self, results: Path) -> None:
        write_json(
            results / SESSION_FILE,
            {
                "run_id": self.run_id,
                "c1_name": self.c1_name,
                "ca_sha256": self.ca_sha256,
                "project_version": self.project_version,
                "baseline_sha": self.baseline_sha,
                "gens": self.gens,
                "c1_fleet": self.c1_fleet,
                "chain": self.chain,
                "restores": self.restores,
                "evidence": self.evidence,
            },
        )


def new_c1_name(run_id: str) -> str:
    return f"{C1}-{g2.check_run_id(run_id)}-{secrets.token_hex(4)}"


# ---------------------------------------------------------------- ② publish 호스트(D11)


def sync_publish_host(
    session: g2.Session, ca_sha256: str, remote: Remote, fleet_repo: Repo
) -> dict[str, object]:
    pair = session.pair
    trusted = host_json(
        remote,
        f"trust --ca {CENTRAL_WORK}/central-root.crt --sha256 {shlex.quote(ca_sha256)} "
        f"--check-url {site.CENTRAL_URL}/healthz",
    )
    tokens = host_json(remote, f"tokens --secrets {CENTRAL_WORK}/secrets")
    names = ",".join((pair.next, pair.fault, pair.fault_migrate))
    images = host_json(remote, f"images --store {NIGHTLY_STORE} --names {names}")
    try:
        token = remote.wsl(f"uv run --no-sync python {buildhost.CENTRAL_PY} publish-viewer-token")
    except (golden.WslError, golden.WslScriptError) as exc:
        raise InfraError(f"central.py publish-viewer-token failed: {exc}") from exc
    fleet_repo.set_secret(VIEWER_SECRET, token.strip())
    del token
    return {"trust": trusted, "tokens": tokens, "images": images, "secret": VIEWER_SECRET}


# ---------------------------------------------------------------- ③ 사슬(P8 — 판단은 `baseline`)


def read_chain(remote: Remote) -> str | None:
    found = host_json(remote, f"chain --repo {fleet.FLEET_REPO}").get("last_approved")
    return found if isinstance(found, str) else None


def trust_base(remote: Remote, sha: str, reason: str) -> dict[str, object]:
    return host_json(
        remote,
        f"trust-base --repo {fleet.FLEET_REPO} --sha {sha} --reason {shlex.quote(reason)}",
    )


def reconcile(fleet_repo: Repo, remote: Remote, run_id: str) -> dict[str, object]:
    """`baseline.reconcile`에 publish 호스트 사슬 읽기·trust-base(root WSL)를 묶는다."""
    return baseline.reconcile(
        fleet_repo,
        lambda: read_chain(remote),
        lambda sha, reason: trust_base(remote, sha, reason),
        run_id,
    )


# ---------------------------------------------------------------- ⑤ 기준선 수렴(P2)


def wait_row(
    remote: Remote, site_id: str, want: Callable[[dict[str, object]], bool], timeout_s: float
) -> dict[str, object]:
    """`/fleet` `site_id` 행이 `want`를 만족할 때까지. 시간이 다 되면 `InfraError`."""
    deadline = remote.clock() + timeout_s
    row: dict[str, object] = {}
    while True:
        try:
            row = judges.fleet_row(remote.central("fleet", "--site", site_id), site_id)
        except (golden.WslError, golden.WslScriptError, ValueError, subprocess.TimeoutExpired):
            row = {}
        if row and want(row):
            return row
        if remote.clock() >= deadline:
            raise InfraError(f"/fleet {site_id} not as expected within {timeout_s:.0f}s: {row}")
        remote.sleep(POLL_S)


def converge_baseline(
    session: g2.Session, gens: Mapping[str, int], remote: Remote
) -> dict[str, object]:
    """두 현장이 기준선 gen을 받아 적용(succeeded)하고 중앙 current가 그 gen이 될 때까지. 적용
    실패는 제품."""
    seen: dict[str, object] = {}
    for site_id, gen in gens.items():
        run = g2.site_up(session, site_id)
        snap, waited = remote.converge(run.ctx.site.host(), gen)
        state = judges.updater_state(snap)
        if state.get("applied_gen") != gen or state.get("last_result") != "succeeded":
            raise E2EError(f"{site_id} baseline g{gen} not applied: {state}")
        wait_row(
            remote,
            site_id,
            lambda r, g=gen: judges.sub(r, "current").get("gen") == g,
            FLEET_TIMEOUT_S,
        )
        seen[site_id] = {"gen": gen, "converge_s": round(waited, 1)}
    return seen


# ---------------------------------------------------------------- ⑥ c1 쌍(★P1)


def take_c1(session: g2.Session, fs: FleetSession, remote: Remote) -> None:
    central(remote, "stop-web")
    try:
        dumped = central(remote, "dump", "--name", fs.c1_name)
        fs.c1_fleet = cast(dict[str, dict[str, object]], dumped.get("fleet") or {})
        fs.evidence["dump"] = {k: dumped.get(k) for k in ("bytes", "seconds")}
        for site_id in SITES:
            run = g2.site_up(session, site_id)
            remote.mark(run.ctx, run.ctx.site.host())
            remote.checkpoint(run.ctx.site.vm_name, C1)
            fs.c1_nft[site_id] = dict(run.ctx.nft_last)
    finally:
        central(remote, "start-web")


def prepare_c1(
    session: g2.Session,
    *,
    remote: Remote | None = None,
    repos: Repos | None = None,
    results: Path | None = None,
) -> FleetSession:
    """모듈 설명 ①~⑥. 실패하면 중앙을 내리고 다시 던진다."""
    remote = remote or Remote()
    repos = repos or Repos.default()
    out = results or g2.results_dir(session.run_id)
    started = remote.clock()
    evidence: dict[str, object] = {}
    log("[fleet] tool branch")
    evidence["tool"] = toolbranch.update_tool_branch(
        repos.v1, session.head_sha, session.records, session.run_id
    )
    ca = g2.bundles_dir(session.run_id) / "fleet" / "central-root.crt"
    status = central(remote, "up", "--ca-out", golden.wsl_path(ca))
    try:
        ca_sha = str(status.get("ca_sha256") or "")
        if not ca_sha:
            raise InfraError("central.py up returned no CA fingerprint")
        log("[fleet] publish host + fleet-test cleanup")
        evidence["publish_host"] = sync_publish_host(session, ca_sha, remote, repos.fleet)
        evidence["leftovers"] = baseline.clear_leftovers(repos.fleet)
        chain = [reconcile(repos.fleet, remote, session.run_id)]
        tag_dir = g2.bundles_dir(session.run_id) / "fleet" / "tag-tree"
        tag = baseline.ensure_project_tag(
            repos.project, baseline.tag_tree(session.tree, tag_dir), session.run_id
        )
        evidence["project_tag"] = tag
        online: dict[str, object] = {}
        for site_id in SITES:
            run = g2.site_up(session, site_id)
            remote.restore_c0(run)
            online[site_id] = remote.online(run.ctx, run.ctx.site.host(), ca, ca_sha)
        evidence["online"] = online
        log("[fleet] baseline PR")
        gens, merged = baseline.baseline(session, repos.fleet, str(tag["version"]), remote.now())
        evidence["baseline"] = {
            "pull": merged.pull.number,
            "merge": merged.sha,
            "run": merged.run.id,
            "published": merged.outcome.published,
        }
        evidence["converge"] = converge_baseline(session, gens, remote)
        fs = FleetSession(
            run_id=session.run_id,
            c1_name=new_c1_name(session.run_id),
            ca=ca,
            ca_sha256=ca_sha,
            project_version=str(tag["version"]),
            baseline_sha=merged.sha,
            gens=gens,
            evidence=evidence,
            chain=chain,
        )
        take_c1(session, fs, remote)
        fs.evidence["prepare_s"] = round(remote.clock() - started, 1)
        fs.save(out)
        return fs
    except BaseException:
        close(remote)
        raise


def close(remote: Remote | None = None) -> None:
    """세션 끝(실패해도): 중앙을 내린다. c1 체크포인트는 VM과 함께 지워진다(다음 preflight)."""
    try:
        (remote or Remote()).central("down")
    except (golden.WslError, golden.WslScriptError, ValueError, subprocess.TimeoutExpired) as exc:
        log(f"[fleet] central down failed: {exc}")


def check_chain(fs: FleetSession, repos: Repos, remote: Remote) -> dict[str, object]:
    """복원마다 사슬·main 대조(어긋나면 `reconcile`의 복구) — 앞 시나리오의 되돌림 실패가 다음
    시나리오로 번지지 않게."""
    note = reconcile(repos.fleet, remote, fs.run_id)
    fs.chain.append(note)
    return note


def restore_c1(
    session: g2.Session,
    fs: FleetSession,
    sites: Collection[str],
    *,
    remote: Remote | None = None,
    repos: Repos | None = None,
    results: Path | None = None,
) -> dict[str, object]:
    """fleet 시나리오의 시작(모듈 설명). `sites`가 비면 중앙만 되돌린다(`E_APPROVAL`)."""
    remote = remote or Remote()
    repos = repos or Repos.default()
    chain = check_chain(fs, repos, remote)
    runs = [g2.site_up(session, s) for s in sites]
    central(remote, "stop-web")
    try:
        for run in runs:
            remote.restore_vm(run, C1, fs.c1_nft[run.ctx.site.site_id])
        restored = central(remote, "restore", "--name", fs.c1_name)
    finally:
        central(remote, "start-web")
    if restored.get("fleet_match") is not True:
        raise InfraError(f"central restore {fs.c1_name}: /fleet does not match the dump")
    for run in runs:
        site_id = run.ctx.site.site_id
        mark = judges.as_int(fs.c1_fleet.get(site_id, {}).get("last_heartbeat_seq"))
        wait_row(
            remote,
            site_id,
            lambda r, m=mark: judges.as_int(r.get("last_heartbeat_seq")) > m,
            HEARTBEAT_TIMEOUT_S,
        )
        remote.wait_ready(run.ctx.site.host())
    fs.restores += 1
    fs.save(results or g2.results_dir(session.run_id))
    return {"chain": chain, "central": {k: restored.get(k) for k in ("seconds", "fleet_match")}}


def open_fleet(
    session: g2.Session,
    selected: Sequence[str],
    *,
    prepare: Callable[[g2.Session], FleetSession] = prepare_c1,
) -> FleetSession | None:
    """고른 시험(`<파일>::<이름>`)에 fleet 묶음이 있을 때만 c1을 만든다(`order.needs_central`) — c0
    시나리오만 고른 실행(평일 재시도 등)은 중앙을 띄우지 않는다."""
    if not order.needs_central(selected):
        return None
    return prepare(session)
