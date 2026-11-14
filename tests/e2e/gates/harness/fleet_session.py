"""fleet 세션(M6-4a A4 — 계획 ★P1, ★P5 개정) — fleet 시험이 골랐을 때만 한 번 만드는 출발점 c1 쌍.

nightly는 GitHub을 거치지 않는다(★P5): 시나리오가 서명 번들을 중앙에 직접 PUT한다
(`fleet_scenarios`). 그래서 c1에는 게시가 없다 — 중앙 `max_published_gen`은 0, 현장 applied gen은
c0(g1, 폐쇄망 `apply`) 그대로다.

`prepare_c1`(세션 fixture `fleet_c1`): 새 중앙 up(`central.py up` — 새 DB·CA·토큰) → 두 현장 c0 복원
→ `site.site_online`(CA 신뢰·`updater.env`·등록·현장 token은 stdin) → 두 현장의 heartbeat가 중앙
`/fleet`에 보일 때까지(`heard`) → control-web(현장이 닿는 유일한 입구)을 멈추고 중앙 DB 덤프(그때
`/fleet` 요약 포함) → VM 체크포인트 c1 x2 → control-web 시작. 덤프가 먼저라 VM의 보고 seq가 중앙
수위 이상이다(복원 뒤 heartbeat가 history로 밀리지 않는다 — control-api `reports`).

`restore_c1`(fleet 시나리오의 시작): control-web 멈춤 → VM c1 복원(메모리 확보·카운터 기준,
`g2.restore_checkpoint`) → 중앙 restore(덤프 때 `/fleet`과 일치 확인) → control-web 시작 → 복원한
현장의 heartbeat 수위가 c1 값을 넘는지. control-web을 처음부터 끝까지 멈춰 두어 되돌리기 전의 VM이
복원된 중앙에, 복원된 VM이 되돌리기 전의 중앙에 닿지 않는다. VM만 되돌리면 중앙 desired가 남아
Updater가 앞 시나리오의 게시본을 스스로 적용한다(계획 F8).

c1(덤프 이름·체크포인트)은 이 pytest 프로세스가 만든 것만 쓴다 — infra 재시도 세션은 새 VM·새 중앙
위에 c1을 새로 만든다(`central.py up`이 지난 덤프를 지운다). 실패 분류: WSL·Hyper-V 호출과 대기
초과는 `InfraError`, 원격 제품 명령의 실패는 그 예외 그대로(분류는 `failures`).
"""

from __future__ import annotations

import secrets
import subprocess
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harness import buildhost, g2, golden, hyperv, judges, order, site
from harness.context import Context, InfraError, log, write_json
from harness.vm import Vm

C1 = "c1"
SITES = ("site-a", "site-b")
C0_GEN = 1
"""c0의 applied gen(설치 runbook의 g1) — 게시가 없으니 c1도 같다."""
SESSION_FILE = Path("fleet") / "session.json"
"""결과 디렉터리 안 fleet 세션 기록(비밀값 없음) — 재시도 전 `first/`로 옮긴다."""
CONVERGE_TIMEOUT_S = 600.0
FLEET_TIMEOUT_S = 120.0
"""적용 뒤 중앙 `/fleet` current가 따라오기까지(보고 한 번 + heartbeat 여유)."""
ONLINE_TIMEOUT_S = 180.0
"""온라인 전환 뒤 `heard`까지 — `buildhost.wait_fleet`의 기본 대기와 같다."""
HEARTBEAT_TIMEOUT_S = 90.0
"""복원 뒤 heartbeat가 다시 보이기까지 — poll 10초 x 3 + Updater 재시도·TLS 여유."""
POLL_S = 5.0


# ---------------------------------------------------------------- 원격 호출(주입)


@dataclass(frozen=True)
class Remote:
    """fleet 세션·시나리오가 부르는 WSL·Hyper-V·VM 조작. 기본값이 실제 호출이다."""

    central: Callable[..., dict[str, object]] = buildhost.central
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


def central(remote: Remote, *args: str) -> dict[str, object]:
    """`central.py` 한 번 — WSL·스크립트 실패와 JSON 아님은 `InfraError`(중앙은 실행 환경)."""
    try:
        return remote.central(*args)
    except (golden.WslError, golden.WslScriptError, ValueError, IndexError) as exc:
        raise InfraError(f"central.py {' '.join(args[:1])} failed: {exc}") from exc


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


# ---------------------------------------------------------------- 세션 기록


@dataclass
class FleetSession:
    run_id: str
    c1_name: str
    """이 프로세스가 만든 덤프·c1 이름(`c1-<run>-<임의 8 hex>`) — 다른 세션 것을 쓰지 않는다."""
    ca: Path
    ca_sha256: str
    c1_fleet: dict[str, dict[str, object]] = field(default_factory=dict[str, dict[str, object]])
    """덤프 때 `/fleet` 요약(`central.fleet_view`)."""
    c1_nft: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    """c1 체크포인트 때 현장별 외부 접속 카운터 — 복원 뒤 기준."""
    evidence: dict[str, object] = field(default_factory=dict[str, object])
    restores: int = 0

    def save(self, results: Path) -> None:
        write_json(
            results / SESSION_FILE,
            {
                "run_id": self.run_id,
                "c1_name": self.c1_name,
                "ca_sha256": self.ca_sha256,
                "c1_fleet": self.c1_fleet,
                "restores": self.restores,
                "evidence": self.evidence,
            },
        )


def new_c1_name(run_id: str) -> str:
    return f"{C1}-{g2.check_run_id(run_id)}-{secrets.token_hex(4)}"


# ---------------------------------------------------------------- c1 쌍(★P1)


def heard(row: dict[str, object]) -> bool:
    """온라인 전환 뒤 `/fleet` 행이 다 찼는가: online·heartbeat 수위 > 0·current = c0 gen.
    online만 기다리면 이르다 — c0 g1의 성공 보고(첫 heartbeat 다음 seq)가 current를 null로 덮고 다음
    heartbeat가 다시 채운다(run.py `online_poll`과 같은 조건)."""
    return (
        row.get("connection") == "online"
        and judges.as_int(row.get("last_heartbeat_seq")) > 0
        and judges.sub(row, "current").get("gen") == C0_GEN
    )


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
    session: g2.Session, *, remote: Remote | None = None, results: Path | None = None
) -> FleetSession:
    """모듈 설명의 c1 준비. 실패하면 중앙을 내리고 다시 던진다."""
    remote = remote or Remote()
    out = results or g2.results_dir(session.run_id)
    started = remote.clock()
    ca = g2.bundles_dir(session.run_id) / "fleet" / "central-root.crt"
    status = central(remote, "up", "--ca-out", golden.wsl_path(ca))
    try:
        ca_sha = str(status.get("ca_sha256") or "")
        if not ca_sha:
            raise InfraError("central.py up returned no CA fingerprint")
        fs = FleetSession(session.run_id, new_c1_name(session.run_id), ca, ca_sha)
        online: dict[str, object] = {}
        for site_id in SITES:
            run = g2.site_up(session, site_id)
            log(f"[fleet] {site_id}: c0 -> online")
            remote.restore_c0(run)
            online[site_id] = remote.online(run.ctx, run.ctx.site.host(), ca, ca_sha)
        for site_id in SITES:
            wait_row(remote, site_id, heard, ONLINE_TIMEOUT_S)
        fs.evidence["online"] = online
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


def restore_c1(
    session: g2.Session,
    fs: FleetSession,
    sites: Collection[str],
    *,
    remote: Remote | None = None,
    results: Path | None = None,
) -> dict[str, object]:
    """fleet 시나리오의 시작(모듈 설명). `sites`의 VM과 중앙을 함께 되돌린다."""
    remote = remote or Remote()
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
    return {"central": {k: restored.get(k) for k in ("seconds", "fleet_match")}}


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
