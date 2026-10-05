"""e2e VM 접속(Windows OpenSSH `ssh`/`scp`, 내부 전용 vSwitch). 사용자 `pureiron`(sudo NOPASSWD).

호스트 키는 VM을 만들 때마다 바뀌므로 known_hosts에 남기지 않는다(e2e 전용 망, 키 고정 안 함).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

USER = "pureiron"
NULL_DEVICE = "NUL"


class VmError(RuntimeError):
    pass


def _tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise VmError(f"{name} not found (Windows OpenSSH client)")
    return found


@dataclass(frozen=True, slots=True)
class Vm:
    ip: str
    key: Path

    def _opts(self) -> list[str]:
        return [
            "-i",
            str(self.key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"UserKnownHostsFile={NULL_DEVICE}",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=10",
        ]

    def run(
        self, command: str, *, timeout_s: float = 600, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """원격 셸 명령(`sudo`가 필요하면 명령에 직접 쓴다)."""
        result = subprocess.run(
            [_tool("ssh"), *self._opts(), f"{USER}@{self.ip}", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
        if check and result.returncode != 0:
            raise VmError(
                f"ssh `{command[:120]}` exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[-1500:]}"
            )
        return result

    def start(self, command: str, *, capture: bool = True) -> subprocess.Popen[str]:
        """오래 도는 원격 명령(출력 줄을 읽으며 기다린다 — crash 감시자). 출력을 읽지 않을 명령은
        `capture=False` — 읽지 않는 파이프가 차면 원격 쪽 write까지 멈춘다."""
        out = subprocess.PIPE if capture else subprocess.DEVNULL
        return subprocess.Popen(
            [_tool("ssh"), *self._opts(), f"{USER}@{self.ip}", command],
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def put(self, local: Path, remote: str, *, timeout_s: float = 1800) -> None:
        result = subprocess.run(
            [_tool("scp"), *self._opts(), "-q", str(local), f"{USER}@{self.ip}:{remote}"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if result.returncode != 0:
            raise VmError(f"scp {local.name} failed: {result.stderr.strip()}")

    def reachable(self) -> bool:
        try:
            return self.run("true", timeout_s=15, check=False).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def wait_ssh(self, timeout_s: float = 600) -> float:
        """SSH가 열릴 때까지 기다리고 걸린 초를 돌려준다."""
        started = time.monotonic()
        while time.monotonic() - started < timeout_s:
            if self.reachable():
                return time.monotonic() - started
            time.sleep(3)
        raise VmError(f"{self.ip} not reachable over SSH in {timeout_s}s")

    def json(self, command: str, *, timeout_s: float = 120) -> dict[str, object]:
        out = self.run(command, timeout_s=timeout_s).stdout
        return cast(dict[str, object], json.loads(out))
