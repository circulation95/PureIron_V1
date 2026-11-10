"""nightly preflight(M6-2 E8·D12, M6-0 발견 2) — 측정, 판단(순수), 조치.

판단 기준(`Limits`, 환경변수로 덮어쓸 수 있다 — 리허설에서 일부러 infra 실패를 낼 때):
Windows 가용 메모리 ≥ `PUREIRON_G2_MIN_MEM_GB`(8), C: 여유 ≥ `PUREIRON_G2_MIN_DISK_GB`(40),
WSL이 응답(시간 제한 있는 echo), WSL Docker 동작, 진행 중인 foundation-ci 없음(최대
`PUREIRON_G2_CI_WAIT_S` 1800초 기다린다). GitHub Actions 안(`GITHUB_ACTIONS=true`)에서는
foundation-ci 상태를 읽지 못하는 것(`gh run list` 실패)도 어긴 것으로 친다 — 그 확인이 꺼진 채로
도는 것을 조용히 넘기지 않게(로컬에서는 메모만 남긴다). 하나라도 어기면 `infra`로 멈춘다.

조치(판단만 하는 모드에서는 하지 않는다): **foundation-ci 대기를 먼저** 한다. 그 뒤에도 WSL echo가
답하지 않으면 `WSL_ECHO_TRIES`번까지 다시 묻고, 그래도 무응답이며 foundation-ci가 돌지 않는 것이
확인될 때만 `docker desktop stop` → `wsl --shutdown` → `docker desktop start` 뒤 다시 잰다(같은 PC의
g1 job은 메모리가 빠듯할 때 WSL을 느리게 만든다 — 느린 것을 죽은 것으로 보고 살아 있는 job을 끊지
않게). CI가 돌거나 상태를 모르면 재시작하지 않고 `infra`로 멈춘다. 그 뒤 메모리가 기준 미만이고
WSL이 답하면 WSL 페이지 캐시를 비우고(`buildhost.reclaim_host_memory`) 최대 `RECLAIM_WAIT_S` 동안
`RECLAIM_POLL_S`마다 다시 잰다 — WSL은 캐시를 Windows에 늦게 돌려준다(VM 실측: vmmemWSL 7.5 GB·
가용 4.6 GB가 회수 뒤 약 1분에 3.5 GB·6.3 GB). 끝까지 기준 미만이면 `infra`. 판단만 하는 모드는
회수하지 않고, 대신 WSL 쪽 수치(vmmemWSL 크기·WSL 페이지 캐시)와 "회수하면 달라질 수 있음" 메모를
남긴다. 남은 e2e VM, 14일 넘은 결과, 7일 넘은 임시 브랜치를 지운다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from harness import buildhost, golden, hyperv
from harness.context import E2EError
from harness.github import TEMP_BRANCH_PREFIX, GitHub, GitHubError
from harness.site import Site

GIB = 1024**3
WSL_ECHO_TIMEOUT_S = 30
WSL_ECHO_TRIES = 3
"""WSL 재시작 전에 echo를 물어보는 횟수(처음 측정 포함)."""
WSL_ECHO_PAUSE_S = 15
DOCKER_TIMEOUT_S = 60
PS_TIMEOUT_S = 60
DESKTOP_TIMEOUT_S = 300
CI_POLL_S = 60
RECLAIM_WAIT_S = 120
"""WSL 페이지 캐시를 비운 뒤 Windows 가용 메모리가 기준을 넘기를 기다리는 최대 시간(실측 약 1분)."""
RECLAIM_POLL_S = 10
WSL_MEMINFO_TIMEOUT_S = 30
RESULTS_KEEP_DAYS = 14
BRANCH_KEEP_DAYS = 7
CI_UNAVAILABLE_NOTE = "foundation-ci status unavailable (gh run list failed)"
CI_UNKNOWN_PROBLEM = (
    "foundation-ci status unavailable in GitHub Actions (gh run list failed - "
    "check the workflow permission actions: read)"
)


@dataclass(frozen=True)
class Limits:
    min_mem_gb: float = 8.0
    min_disk_gb: float = 40.0
    ci_wait_s: float = 1800.0
    require_ci_status: bool = False
    """foundation-ci 상태를 못 읽으면 어긴 것으로 친다(GitHub Actions 안, `GITHUB_ACTIONS=true`)."""

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Limits:
        def number(key: str, default: float) -> float:
            raw = environ.get(key)
            if raw is None or raw == "":
                return default
            try:
                value = float(raw)
            except ValueError:
                value = -1.0
            if value < 0:
                raise E2EError(f"{key} must be a number >= 0, got {raw!r}")
            return value

        return cls(
            min_mem_gb=number("PUREIRON_G2_MIN_MEM_GB", cls.min_mem_gb),
            min_disk_gb=number("PUREIRON_G2_MIN_DISK_GB", cls.min_disk_gb),
            ci_wait_s=number("PUREIRON_G2_CI_WAIT_S", cls.ci_wait_s),
            require_ci_status=environ.get("GITHUB_ACTIONS") == "true",
        )


@dataclass(frozen=True)
class Measure:
    mem_gb: float | None
    disk_gb: float | None
    wsl_ok: bool
    docker_ok: bool
    ci_running: int
    notes: tuple[str, ...] = field(default=())
    ci_known: bool = True
    """foundation-ci 상태를 읽었는가(`gh run list` 성공 + JSON) — 거짓이면 `ci_running`은 0이다."""
    wsl_vmmem_gb: float | None = None
    """Windows에서 본 WSL VM(`vmmemWSL` 작업 집합) 크기 — 판단에는 쓰지 않는다(회수 여지 참고)."""
    wsl_cache_gb: float | None = None
    """WSL 안의 페이지 캐시(`/proc/meminfo` Buffers + Cached) — 회수하면 Windows로 돌아올 수
    있는 몫."""


def judge(measure: Measure, limits: Limits) -> list[str]:
    """어긴 기준(영어 한 줄씩). 빈 목록이면 통과다. 재지 못한 값(None)은 어긴 것으로 친다."""
    problems: list[str] = []
    if measure.mem_gb is None or measure.mem_gb < limits.min_mem_gb:
        problems.append(f"windows available memory {measure.mem_gb} GB < {limits.min_mem_gb} GB")
    if measure.disk_gb is None or measure.disk_gb < limits.min_disk_gb:
        problems.append(f"C: free {measure.disk_gb} GB < {limits.min_disk_gb} GB")
    if not measure.wsl_ok:
        problems.append("WSL is not responding")
    if not measure.docker_ok:
        problems.append("docker (WSL) is not responding")
    if measure.ci_running:
        problems.append(f"{measure.ci_running} foundation-ci run(s) still in progress")
    if not measure.ci_known and limits.require_ci_status:
        problems.append(CI_UNKNOWN_PROBLEM)
    return problems


def stale(entries: Mapping[str, float], now: float, keep_days: int) -> list[str]:
    """`entries`(이름 → mtime epoch s) 가운데 `keep_days`일보다 오래된 것(정렬)."""
    cutoff = now - keep_days * 86400
    return sorted(name for name, mtime in entries.items() if mtime < cutoff)


# ---------------------------------------------------------------- 측정(Windows 호스트)


def _quiet(cmd: list[str], timeout_s: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def windows_mem_gb() -> float | None:
    done = _quiet(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory",
        ],
        PS_TIMEOUT_S,
    )
    raw = done.stdout.strip() if done is not None and done.returncode == 0 else ""
    return round(int(raw) * 1024 / GIB, 1) if raw.isdecimal() else None


def wsl_vmmem_gb() -> float | None:
    """`vmmemWSL` 프로세스의 작업 집합(GB). 프로세스가 없거나 못 읽으면 None."""
    done = _quiet(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-Process -Name vmmemWSL -ErrorAction SilentlyContinue"
            " | Measure-Object -Property WorkingSet64 -Sum).Sum",
        ],
        PS_TIMEOUT_S,
    )
    raw = done.stdout.strip() if done is not None and done.returncode == 0 else ""
    return round(int(raw) / GIB, 1) if raw.isdecimal() and int(raw) > 0 else None


def meminfo_cache_gb(meminfo: str) -> float | None:
    """`/proc/meminfo` 본문의 Buffers + Cached(GB). 둘 중 하나라도 없으면 None."""
    kib: dict[str, int] = {}
    for line in meminfo.splitlines():
        name, _, rest = line.partition(":")
        value = rest.split()
        if name in ("Buffers", "Cached") and value and value[0].isdecimal():
            kib[name] = int(value[0])
    if len(kib) != 2:
        return None
    return round(sum(kib.values()) * 1024 / GIB, 1)


def wsl_cache_gb() -> float | None:
    done = _quiet(
        ["wsl", "-d", golden.WSL_DISTRO, "--exec", "cat", "/proc/meminfo"], WSL_MEMINFO_TIMEOUT_S
    )
    return meminfo_cache_gb(done.stdout) if done is not None and done.returncode == 0 else None


def disk_gb(path: Path) -> float | None:
    try:
        return round(shutil.disk_usage(path.anchor or str(path)).free / GIB, 1)
    except OSError:
        return None


def wsl_ok() -> bool:
    done = _quiet(["wsl", "-d", golden.WSL_DISTRO, "--exec", "echo", "ok"], WSL_ECHO_TIMEOUT_S)
    return done is not None and done.returncode == 0 and done.stdout.strip() == "ok"


def docker_ok() -> bool:
    """WSL에서 본 Docker(번들·이미지 빌드가 쓰는 쪽). 빌드와 같은 셸 경로(`bash -c`)로 부른다 —
    Docker Desktop WSL 통합의 `docker`는 Desktop이 떠 있을 때만 있다."""
    done = _quiet(
        [
            "wsl",
            "-d",
            golden.WSL_DISTRO,
            "--exec",
            "bash",
            "-c",
            "docker info --format '{{.ServerVersion}}'",
        ],
        DOCKER_TIMEOUT_S,
    )
    return done is not None and done.returncode == 0 and bool(done.stdout.strip())


Runner = Callable[[list[str], float], subprocess.CompletedProcess[str] | None]


def ci_running(run: Runner = _quiet) -> tuple[int, str | None]:
    """진행 중·대기 중인 foundation-ci run 수. gh로 못 읽으면(실패·JSON 아님) (0, 이유) — 판단은
    `judge`가 `Limits.require_ci_status`로 한다."""
    count = 0
    for status in ("in_progress", "queued"):
        done = run(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "foundation-ci.yml",
                "--status",
                status,
                "--json",
                "databaseId",
            ],
            PS_TIMEOUT_S,
        )
        if done is None or done.returncode != 0:
            return 0, CI_UNAVAILABLE_NOTE
        try:
            runs: object = json.loads(done.stdout or "[]")
        except ValueError:
            return 0, "foundation-ci status unavailable (gh run list output is not JSON)"
        count += len(cast(list[object], runs)) if isinstance(runs, list) else 0
    return count, None


def measure(e2e_root: Path) -> Measure:
    ci, note = ci_running()
    alive = wsl_ok()
    return Measure(
        mem_gb=windows_mem_gb(),
        disk_gb=disk_gb(e2e_root),
        wsl_ok=alive,
        docker_ok=docker_ok(),
        ci_running=ci,
        notes=() if note is None else (note,),
        ci_known=note is None,
        wsl_vmmem_gb=wsl_vmmem_gb(),
        wsl_cache_gb=wsl_cache_gb() if alive else None,
    )


def mem_short(measure: Measure, limits: Limits) -> bool:
    """Windows 가용 메모리가 기준 미만인가(재지 못한 값도 미만으로 친다 — `judge`와 같다)."""
    return measure.mem_gb is None or measure.mem_gb < limits.min_mem_gb


def reclaim_hint(measure: Measure, limits: Limits) -> str | None:
    """판단만 하는 모드의 메모: 메모리가 모자라도 WSL이 답하면 조치 있는 preflight는 먼저
    회수한다."""
    if not mem_short(measure, limits) or not measure.wsl_ok:
        return None
    return (
        f"windows available memory {measure.mem_gb} GB < {limits.min_mem_gb} GB, but WSL holds "
        f"{measure.wsl_vmmem_gb} GB (vmmemWSL, page cache {measure.wsl_cache_gb} GB): judge-only "
        "does not reclaim it - a full preflight reclaims it first and may pass"
    )


# ---------------------------------------------------------------- 조치


def recover_wsl() -> list[str]:
    """Docker Desktop 중지 → `wsl --shutdown` → Docker Desktop 시작. 실행한 조치(결과 포함)."""
    actions: list[str] = []
    for cmd, timeout_s in (
        (["docker", "desktop", "stop"], DESKTOP_TIMEOUT_S),
        (["wsl", "--shutdown"], DESKTOP_TIMEOUT_S),
        (["docker", "desktop", "start"], DESKTOP_TIMEOUT_S),
    ):
        done = _quiet(cmd, timeout_s)
        actions.append(f"{' '.join(cmd)} -> {'timeout' if done is None else done.returncode}")
    return actions


def wsl_answers(
    alive: Callable[[], bool], sleep: Callable[[float], None], tries: int = WSL_ECHO_TRIES
) -> bool:
    """WSL echo를 `tries`번까지 묻는다(사이에 `WSL_ECHO_PAUSE_S` 쉰다)."""
    for attempt in range(tries):
        if attempt:
            sleep(WSL_ECHO_PAUSE_S)
        if alive():
            return True
    return False


def revive_wsl(
    ci: Callable[[], tuple[int, bool]],
    *,
    alive: Callable[[], bool],
    restart: Callable[[], list[str]],
    sleep: Callable[[float], None],
    tries: int = WSL_ECHO_TRIES,
) -> tuple[bool, list[str]]:
    """WSL이 끝내 답하지 않을 때만, 그리고 foundation-ci가 돌지 않는 것이 확인될 때만(`ci`: 진행
    중 run 수, 상태를 읽었는가) 재시작한다. (WSL을 쓸 수 있다고 보는가, 조치) — 재시작했으면
    참(다음 측정·실행이 확인한다), CI 때문에 재시작하지 않았으면 거짓."""
    if wsl_answers(alive, sleep, tries):
        return True, []
    ci_running, ci_known = ci()
    if ci_running:
        return False, [
            f"wsl not responding, {ci_running} foundation-ci run(s) in progress: no restart"
        ]
    if not ci_known:
        return False, ["wsl not responding, foundation-ci status unknown: no restart"]
    return True, ["wsl not responding: restart", *restart()]


def reclaim_memory(
    min_gb: float,
    before: float | None,
    *,
    mem_now: Callable[[], float | None] | None = None,
    reclaim: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[float | None, list[str]]:
    """WSL 페이지 캐시를 비우고(`reclaim`) Windows 가용 메모리가 `min_gb` 이상이 될 때까지 최대
    `RECLAIM_WAIT_S` 동안 `RECLAIM_POLL_S`마다 다시 잰다. (마지막 측정값, 조치 — 회수 전·후 GB).
    `mem_now`·`reclaim`을 주지 않으면 `windows_mem_gb`·`buildhost.reclaim_host_memory`를 부를 때
    모듈 속성으로 찾는다(단위 시험이 바꿔 끼운다)."""
    (reclaim or buildhost.reclaim_host_memory)()
    read = mem_now or windows_mem_gb
    started = clock()
    after = read()
    while (after is None or after < min_gb) and clock() - started < RECLAIM_WAIT_S:
        sleep(RECLAIM_POLL_S)
        after = read()
    waited = round(clock() - started)
    return after, [
        f"reclaimed WSL page cache: windows available memory {before} -> {after} GB "
        f"(need {min_gb} GB, waited {waited}s)"
    ]


def remove_vms(site_list: list[Site]) -> list[str]:
    removed: list[str] = []
    for one in site_list:
        if hyperv.vm_state(one.vm_name) is not None:
            hyperv.remove(one.vm_name, one.vm_dir / "disk.vhdx")
            removed.append(one.vm_name)
    return removed


def prune_results(results: Path, now: float) -> list[str]:
    """`results` 바로 아래 `g2-*` 디렉터리 가운데 `RESULTS_KEEP_DAYS`일보다 오래된 것을 지운다(다른
    이름·파일은 건드리지 않는다)."""
    if not results.is_dir():
        return []
    entries = {d.name: d.stat().st_mtime for d in results.glob("g2-*") if d.is_dir()}
    old = stale(entries, now, RESULTS_KEEP_DAYS)
    for name in old:
        shutil.rmtree(results / name, ignore_errors=True)
    return old


def prune_branches(gh: GitHub, now: datetime) -> list[str]:
    try:
        branches = gh.temp_branches(TEMP_BRANCH_PREFIX)
    except GitHubError as exc:
        return [f"temp branches unavailable: {exc}"]
    cutoff = now - timedelta(days=BRANCH_KEEP_DAYS)
    old = sorted(name for name, stamp in branches if stamp < cutoff)
    for name in old:
        gh.delete_branch(name)
    return old


@dataclass(frozen=True)
class Outcome:
    measure: Measure
    limits: Limits
    problems: list[str]
    actions: list[str]

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def warnings(self) -> list[str]:
        """판정을 막지는 않았지만 눈에 띄게 남길 측정 메모(예: foundation-ci 상태를 못 읽음)."""
        return list(self.measure.notes)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "warnings": self.warnings,
            "measure": asdict(self.measure),
            "limits": asdict(self.limits),
            "problems": self.problems,
            "actions": self.actions,
        }


def run(
    limits: Limits,
    *,
    measure_now: Callable[[], Measure],
    recover: Callable[[], list[str]] = recover_wsl,
    wsl_alive: Callable[[], bool] = wsl_ok,
    cleanup: Callable[[], list[str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    reclaim: Callable[[], None] | None = None,
    mem_now: Callable[[], float | None] | None = None,
) -> Outcome:
    """조치가 있는 preflight: foundation-ci가 돌면 최대 `limits.ci_wait_s` 기다리고, 그 뒤에도 WSL이
    답하지 않으면 `revive_wsl`(CI가 없을 때만 재시작) 뒤 다시 재고, 메모리가 모자라면(WSL이 답할
    때만) `reclaim_memory`로 WSL 캐시를 돌려받으며 다시 재고, 통과하면 정리(`cleanup`)한다."""
    actions: list[str] = []
    seen = measure_now()
    started = clock()
    while seen.ci_running and clock() - started < limits.ci_wait_s:
        actions.append(f"waiting for {seen.ci_running} foundation-ci run(s)")
        sleep(CI_POLL_S)
        seen = measure_now()
    if not seen.wsl_ok:
        # 측정이 이미 1번 물었다 — 남은 횟수만 더 묻는다.
        last = seen
        _, revived = revive_wsl(
            lambda: (last.ci_running, last.ci_known),
            alive=wsl_alive,
            restart=recover,
            sleep=sleep,
            tries=WSL_ECHO_TRIES - 1,
        )
        actions += revived
        seen = measure_now()
    if seen.wsl_ok and mem_short(seen, limits):
        mem_gb, freed = reclaim_memory(
            limits.min_mem_gb,
            seen.mem_gb,
            mem_now=mem_now,
            reclaim=reclaim,
            sleep=sleep,
            clock=clock,
        )
        actions += freed
        seen = replace(seen, mem_gb=mem_gb)
    problems = judge(seen, limits)
    if not problems and cleanup is not None:
        actions += cleanup()
    return Outcome(seen, limits, problems, actions)


def judge_only(limits: Limits, measure_now: Callable[[], Measure]) -> Outcome:
    """측정·판단만 한다(복구·대기·회수·정리 없음). 메모리가 모자라도 WSL이 답하면 "회수하면 달라질
    수 있음" 메모(`reclaim_hint`)를 `warnings`에 남긴다."""
    seen = measure_now()
    hint = reclaim_hint(seen, limits)
    if hint is not None:
        seen = replace(seen, notes=(*seen.notes, hint))
    return Outcome(seen, limits, judge(seen, limits), [])


def now_utc() -> datetime:
    return datetime.now(UTC)
