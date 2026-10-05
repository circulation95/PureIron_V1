"""VM 쪽 crash 주입 감시자(M2d, plan M2 종료 조건). 현장 호스트에서 root로 실행한다.

    python3 crash_at.py --generation 5 --phase migrating --mode kill [--target service|cli]

Updater 상태 파일(`state/updater.json`, tmp → rename으로만 갱신)을 inotify로 지켜보다가 시도가
`(generation, phase)`에 들어간 순간 그 단계를 실행 중인 프로세스를 SIGSTOP으로 얼린다 — 그 phase를
벗어나지 못하게. 대상은 기본 서비스 주 프로세스, `--target cli`는 운영자 `pureiron-site apply`
(폐쇄망에서 `fetched`(번들 검증)를 실행하는 쪽). 이어서

- `--mode kill`: 서비스는 `systemctl kill -s KILL pureiron-updater`(cgroup 전체: 하위 docker compose
  포함) — `Restart=always`가 다시 띄우고 첫 tick의 `recover()`가 수렴시킨다. CLI는 SIGKILL.
- `--mode freeze`: `HIT`를 출력하고 끝낸다. 호스트가 이어서 `Restart-VM -Force`를 건다(전원 리셋).

phase가 매우 짧아(`backed_up` 등) 못 보고 지나치면 `MISSED <지나간 phase>`로 끝난다(종료 코드 3).
표준 라이브러리만 쓴다(현장 호스트의 /usr/bin/python3.12).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import select
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

STATE = Path("/var/lib/pureiron/state/updater.json")
SERVICE = "pureiron-updater"
IN_MOVED_TO = 0x00000080
IN_CLOSE_WRITE = 0x00000008
EVENT_HEADER = struct.Struct("iIII")
ORDER = (
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
TERMINAL = ("succeeded", "failed", "rejected", "failed_manual")


def read_attempt(path: Path) -> tuple[int | None, str | None]:
    """(attempt.generation, attempt.phase) — 시도가 없거나 읽을 수 없으면 (None, None)."""
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    attempt: object = cast(dict[str, object], data).get("attempt")
    if not isinstance(attempt, dict):
        return None, None
    generation = cast(dict[str, object], attempt).get("generation")
    phase = cast(dict[str, object], attempt).get("phase")
    return (
        generation if isinstance(generation, int) else None,
        phase if isinstance(phase, str) else None,
    )


def passed(target: str, phase: str) -> bool:
    """`phase`가 `target`을 이미 지나쳤는지(정상 경로 순서 기준, 종료 phase는 항상 지남)."""
    if phase in TERMINAL:
        return True
    if phase not in ORDER or target not in ORDER:
        return False
    return ORDER.index(phase) > ORDER.index(target)


def main_pid() -> int:
    out = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", SERVICE],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return int(out or "0")


def cli_pids() -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", "pureiron-site apply"], capture_output=True, text=True, check=False
    ).stdout
    return [int(pid) for pid in out.split() if int(pid) != os.getpid()]


class Inotify:
    def __init__(self, directory: Path) -> None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        mask = IN_MOVED_TO | IN_CLOSE_WRITE
        if libc.inotify_add_watch(self.fd, str(directory).encode(), mask) < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch")

    def wait(self, timeout_s: float) -> None:
        """이벤트가 오거나 시간이 지날 때까지 기다리고 쌓인 이벤트를 비운다."""
        ready, _, _ = select.select([self.fd], [], [], timeout_s)
        if ready:
            try:
                while os.read(self.fd, 4096):
                    pass
            except BlockingIOError:
                pass


def watch(generation: int, phase: str, timeout_s: float, state: Path) -> tuple[str, str | None]:
    """('HIT', phase) / ('MISSED', 지나간 phase) / ('TIMEOUT', 마지막 phase)."""
    notify = Inotify(state.parent)
    deadline = time.monotonic() + timeout_s
    last: str | None = None
    while time.monotonic() < deadline:
        gen, current = read_attempt(state)
        if gen == generation and current is not None:
            if current == phase:
                return "HIT", current
            if passed(phase, current):
                return "MISSED", current
            last = current
        # 이벤트를 놓쳐도(감시 시작 전 rename 등) 짧은 주기로 다시 읽는다.
        notify.wait(0.02)
    return "TIMEOUT", last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crash_at")
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--phase", choices=ORDER, required=True)
    parser.add_argument("--mode", choices=("kill", "freeze"), required=True)
    parser.add_argument("--target", choices=("service", "cli"), default="service")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--state", type=Path, default=STATE)
    args = parser.parse_args(argv)

    outcome, seen = watch(args.generation, args.phase, args.timeout, args.state)
    if outcome != "HIT":
        print(f"{outcome} {seen}", flush=True)
        return 3 if outcome == "MISSED" else 4
    pids = [main_pid()] if args.target == "service" else cli_pids()
    pids = [pid for pid in pids if pid > 0]
    if args.target == "service" and pids:
        # cgroup 전체(하위 docker compose 등)를 얼린다 — 주 프로세스만 얼리면 자식이
        # phase를 이어 간다.
        subprocess.run(["systemctl", "kill", "-s", "SIGSTOP", SERVICE], check=True)
    for pid in pids:
        os.kill(pid, signal.SIGSTOP)
    hit_at = time.time()
    if args.mode == "kill":
        if args.target == "service":
            subprocess.run(["systemctl", "kill", "-s", "KILL", SERVICE], check=True)
        else:
            for pid in pids:
                os.kill(pid, signal.SIGKILL)
    print(f"HIT {seen} pids={pids} at={hit_at:.3f}", flush=True)
    return 0 if pids else 5


if __name__ == "__main__":
    sys.exit(main())
