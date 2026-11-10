"""nightly preflight(M6-2 E8·D12, M6-0 발견 2) — 측정, 판단(순수), 조치.

판단 기준(`Limits`, 환경변수로 덮어쓸 수 있다 — 리허설에서 일부러 infra 실패를 낼 때):
Windows 가용 메모리 ≥ `PUREIRON_G2_MIN_MEM_GB`(8), C: 여유 ≥ `PUREIRON_G2_MIN_DISK_GB`(40),
WSL이 응답(시간 제한 있는 echo), WSL Docker 동작, 진행 중인 foundation-ci 없음(최대
`PUREIRON_G2_CI_WAIT_S` 1800초 기다린다). 하나라도 어기면 `infra`로 멈춘다.

조치(판단만 하는 모드에서는 하지 않는다): WSL이 응답하지 않으면 `docker desktop stop` →
`wsl --shutdown` → `docker desktop start` 뒤 1회 다시 잰다(조치를 기록한다 — 응답할 때는 재시작하지
않는다, 돌고 있는 g1 job을 끊지 않게). 남은 e2e VM, 14일 넘은 결과, 7일 넘은 임시 브랜치를 지운다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness import golden, hyperv
from harness.context import E2EError
from harness.github import TEMP_BRANCH_PREFIX, GitHub, GitHubError
from harness.site import Site

GIB = 1024**3
WSL_ECHO_TIMEOUT_S = 30
DOCKER_TIMEOUT_S = 60
PS_TIMEOUT_S = 60
DESKTOP_TIMEOUT_S = 300
CI_POLL_S = 60
RESULTS_KEEP_DAYS = 14
BRANCH_KEEP_DAYS = 7


@dataclass(frozen=True)
class Limits:
    min_mem_gb: float = 8.0
    min_disk_gb: float = 40.0
    ci_wait_s: float = 1800.0

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
        )


@dataclass(frozen=True)
class Measure:
    mem_gb: float | None
    disk_gb: float | None
    wsl_ok: bool
    docker_ok: bool
    ci_running: int
    notes: tuple[str, ...] = field(default=())


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


def ci_running() -> tuple[int, str | None]:
    """진행 중·대기 중인 foundation-ci run 수. gh로 못 읽으면 (0, 이유) — 그것만으로 멈추지
    않는다."""
    count = 0
    for status in ("in_progress", "queued"):
        done = _quiet(
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
            return 0, "foundation-ci status unavailable (gh run list failed)"
        count += len(json.loads(done.stdout or "[]"))
    return count, None


def measure(e2e_root: Path) -> Measure:
    ci, note = ci_running()
    return Measure(
        mem_gb=windows_mem_gb(),
        disk_gb=disk_gb(e2e_root),
        wsl_ok=wsl_ok(),
        docker_ok=docker_ok(),
        ci_running=ci,
        notes=() if note is None else (note,),
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


def remove_vms(site_list: list[Site]) -> list[str]:
    removed: list[str] = []
    for one in site_list:
        if hyperv.vm_state(one.vm_name) is not None:
            hyperv.remove(one.vm_name, one.vm_dir / "disk.vhdx")
            removed.append(one.vm_name)
    return removed


def prune_results(results: Path, now: float) -> list[str]:
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

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
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
    cleanup: Callable[[], list[str]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome:
    """조치가 있는 preflight: WSL 무응답이면 복구 뒤 1회 다시 재고, foundation-ci가 돌면 최대
    `limits.ci_wait_s` 기다리고, 통과하면 정리(`cleanup`)한다."""
    actions: list[str] = []
    seen = measure_now()
    if not seen.wsl_ok:
        actions += ["wsl not responding: restart", *recover()]
        seen = measure_now()
    started = clock()
    while seen.ci_running and clock() - started < limits.ci_wait_s:
        actions.append(f"waiting for {seen.ci_running} foundation-ci run(s)")
        sleep(CI_POLL_S)
        seen = measure_now()
    problems = judge(seen, limits)
    if not problems and cleanup is not None:
        actions += cleanup()
    return Outcome(seen, limits, problems, actions)


def judge_only(limits: Limits, measure_now: Callable[[], Measure]) -> Outcome:
    """측정·판단만 한다(복구·대기·정리 없음)."""
    seen = measure_now()
    return Outcome(seen, limits, judge(seen, limits), [])


def now_utc() -> datetime:
    return datetime.now(UTC)
