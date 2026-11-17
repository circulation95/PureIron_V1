"""e2e VM 접속(Windows OpenSSH `ssh`/`scp`, 내부 전용 vSwitch). 사용자 `pureiron`(sudo NOPASSWD).

호스트 키는 VM을 만들 때마다 바뀌므로 known_hosts에 남기지 않는다(e2e 전용 망, 키 고정 안 함).

실패는 둘로 가른다(G2 실패 분류 E8): ssh·scp **호출 자체**의 실패(연결·인증·전송, OpenSSH 종료
코드 255, SSH 대기 시간 초과)는 `VmError`(`infra`)이고, 연결은 됐는데 **원격 명령**이 0 아닌 코드로
끝나면 `RemoteCommandError`(`E2EError` 하위, `product`)다. 원격 명령이 스스로 255로 끝나면 ssh
실패와 구분되지 않아 `VmError`로 간다(한계).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harness.context import E2EError

USER = "pureiron"
NULL_DEVICE = "NUL"
SSH_FAILED = 255
"""OpenSSH 클라이언트가 자기 실패(연결·인증·세션 끊김)에 내는 종료 코드."""


class VmError(RuntimeError):
    """ssh·scp 호출 자체의 실패(`infra`)."""


class RemoteCommandError(E2EError):
    """원격 명령이 0 아닌 코드(255 제외)로 끝났다 — 호출된 제품 스크립트의 실패(`product`)."""

    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


REMOTE_ERRORS: tuple[type[RuntimeError], ...] = (VmError, RemoteCommandError)
"""`Vm.run`(check=True)이 던지는 예외 전부 — 원격 읽기 실패를 "빈 응답"으로 치는 대기 루프용."""


def remote_failure(
    command: str, result: subprocess.CompletedProcess[str]
) -> VmError | RemoteCommandError | None:
    """ssh 결과의 실패 종류(성공이면 None): 255는 `VmError`, 그 밖의 0 아닌 코드는
    `RemoteCommandError`."""
    if result.returncode == 0:
        return None
    message = (
        f"ssh `{command[:120]}` exited {result.returncode}: "
        f"{(result.stderr or result.stdout or '').strip()[-1500:]}"
    )
    if result.returncode == SSH_FAILED:
        return VmError(message)
    return RemoteCommandError(message, result.returncode)


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
        self,
        command: str,
        *,
        timeout_s: float = 600,
        check: bool = True,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """원격 셸 명령(`sudo`가 필요하면 명령에 직접 쓴다). `input`은 원격 명령의 stdin으로만
        넘긴다 — 비밀값(현장 token)을 명령 줄·파일에 남기지 않을 때 쓴다. 실패 메시지에는 명령과
        stderr만 실린다. `check`이면 종료 코드 255는 `VmError`(ssh 실패), 그 밖의 0 아닌 코드는
        `RemoteCommandError`(원격 명령 실패)다. `check=False`이면 코드와 상관없이 결과를
        돌려준다.

        `input`은 UTF-8 바이트 그대로 보낸다(M6-4b-1 fix r2): 텍스트 모드 stdin은 Windows에서
        `\\n`을 `\\r\\n`으로 바꿔 원격 `nft -f -`가 `\\r`에서 문법 오류를 냈다(실측 run
        44020762321). stdout·stderr는 UTF-8(`errors="replace"`)로 읽어 문자열로 돌려준다."""
        raw = subprocess.run(
            [_tool("ssh"), *self._opts(), f"{USER}@{self.ip}", command],
            stdin=subprocess.DEVNULL if input is None else None,
            input=None if input is None else input.encode("utf-8"),
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        result = subprocess.CompletedProcess[str](
            raw.args,
            raw.returncode,
            raw.stdout.decode("utf-8", "replace"),
            raw.stderr.decode("utf-8", "replace"),
        )
        failure = remote_failure(command, result) if check else None
        if failure is not None:
            raise failure
        return result

    def start(self, command: str, *, capture: bool = True) -> subprocess.Popen[str]:
        """오래 도는 원격 명령(출력 줄을 읽으며 기다린다 — crash 감시자). 출력을 읽지 않을 명령은
        `capture=False` — 읽지 않는 파이프가 차면 원격 쪽 write까지 멈춘다.

        ssh는 모두 stdin을 닫고 띄운다(`ssh -n`과 같음) — 콘솔에서 띄운 실행이면 동시에 도는
        ssh(감시자와 apply)가 같은 콘솔 stdin을 물려받는다(S4 1차 실측 crash 14건)."""
        out = subprocess.PIPE if capture else subprocess.DEVNULL
        return subprocess.Popen(
            [_tool("ssh"), *self._opts(), f"{USER}@{self.ip}", command],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def put(self, local: Path, remote: str, *, timeout_s: float = 1800) -> None:
        result = subprocess.run(
            [_tool("scp"), *self._opts(), "-q", str(local), f"{USER}@{self.ip}:{remote}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if result.returncode != 0:
            raise VmError(f"scp {local.name} failed: {result.stderr.strip()}")

    def get(self, remote: str, local: Path, *, timeout_s: float = 600) -> None:
        """VM 파일을 호스트로(사용자 `pureiron`이 읽을 수 있어야 한다)."""
        local.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [_tool("scp"), *self._opts(), "-q", f"{USER}@{self.ip}:{remote}", str(local)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if result.returncode != 0:
            raise VmError(f"scp {remote} failed: {result.stderr.strip()}")

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
