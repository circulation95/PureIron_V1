"""Hyper-V 조작 래퍼(PD-24): Windows 호스트의 PowerShell Hyper-V 모듈을 부른다.

e2e 시나리오는 VM을 이 모듈로만 다룬다 — golden VHDX(읽기 전용) 위 차분 디스크로 VM을 만들고,
호스트 재부팅 주입은 `Restart-VM -Force`(전원 리셋)다. 현재 사용자가 Hyper-V Administrators
그룹이어야 한다(`setup-host.ps1`).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# '시간 동기화' 통합 서비스(Id 끝 GUID, 언어와 무관).
TIME_SYNC_ID = "2497F4DE-E9FA-4204-80E4-4B75C46419C0"


class HyperVError(RuntimeError):
    pass


def ps_quote(value: str) -> str:
    """PowerShell 작은따옴표 리터럴(내부 `'`는 `''`)."""
    return "'" + value.replace("'", "''") + "'"


def run_ps(script: str, *, timeout_s: float = 300) -> str:
    """오류는 `$ErrorActionPreference='Stop'`의 종결 오류로만 판정한다. `-Command`의 종료 코드는
    마지막 `$?`를 따르므로 `-ErrorAction SilentlyContinue`로 넘긴 '없음'도 1이 된다 → 끝에 `exit 0`.
    """
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script + "; exit 0",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip())[-1500:]
        raise HyperVError(f"powershell failed ({result.returncode}): {detail}")
    return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class VmSpec:
    name: str
    disk: Path
    seed_iso: Path
    switch: str
    cpus: int = 2
    memory_gib: int = 4


def available() -> bool:
    try:
        run_ps("Get-VM | Out-Null")
    except HyperVError:
        return False
    return True


def switch_exists(name: str) -> bool:
    out = run_ps(f"@(Get-VMSwitch -Name {ps_quote(name)} -ErrorAction SilentlyContinue).Count")
    return out == "1"


def vm_state(name: str) -> str | None:
    out = run_ps(
        f"$vm = Get-VM -Name {ps_quote(name)} -ErrorAction SilentlyContinue; "
        "if ($vm) { $vm.State.ToString() }"
    )
    return out or None


def create_vm(spec: VmSpec, parent_vhdx: Path) -> None:
    """golden 위 차분 디스크 + cloud-init seed ISO로 Gen2 VM을 만든다(시작은 하지 않음)."""
    if vm_state(spec.name) is not None:
        raise HyperVError(f"VM {spec.name} already exists")
    spec.disk.parent.mkdir(parents=True, exist_ok=True)
    name, disk, iso = ps_quote(spec.name), ps_quote(str(spec.disk)), ps_quote(str(spec.seed_iso))
    run_ps(
        f"New-VHD -Path {disk} -ParentPath {ps_quote(str(parent_vhdx))} -Differencing | Out-Null; "
        f"New-VM -Name {name} -Generation 2 -MemoryStartupBytes {spec.memory_gib}GB "
        f"-VHDPath {disk} -SwitchName {ps_quote(spec.switch)} | Out-Null; "
        f"Set-VM -Name {name} -ProcessorCount {spec.cpus} -StaticMemory "
        "-AutomaticCheckpointsEnabled $false -AutomaticStartAction Nothing "
        "-AutomaticStopAction TurnOff -CheckpointType Standard; "
        f"Set-VMFirmware -VMName {name} -SecureBootTemplate MicrosoftUEFICertificateAuthority; "
        f"Add-VMDvdDrive -VMName {name} -Path {iso}; "
        # 이름은 OS 언어로 지역화된다(예: '시간 동기화') → 서비스 ID로 고른다.
        f"Get-VMIntegrationService -VMName {name} | "
        f"Where-Object {{ $_.Id -like '*\\{TIME_SYNC_ID}' }} | Enable-VMIntegrationService"
    )


def start(name: str) -> None:
    run_ps(f"Start-VM -Name {ps_quote(name)}")


def restart_force(name: str) -> None:
    """전원 리셋(게스트 종료 절차 없음) — 호스트 재부팅 주입(PD-24)."""
    run_ps(f"Restart-VM -Name {ps_quote(name)} -Force")


def checkpoint(name: str, snapshot: str) -> None:
    """메모리 포함(Standard) 체크포인트 — 같은 이름이 있으면 바꾼다."""
    vm, snap = ps_quote(name), ps_quote(snapshot)
    run_ps(
        f"Get-VMSnapshot -VMName {vm} -Name {snap} -ErrorAction SilentlyContinue | "
        "Remove-VMSnapshot; "
        f"Checkpoint-VM -Name {vm} -SnapshotName {snap}",
        timeout_s=900,
    )


def restore(name: str, snapshot: str) -> None:
    """체크포인트로 되돌리고 다시 실행한다(시계는 시간 동기화 통합 서비스가 맞춘다)."""
    vm, snap = ps_quote(name), ps_quote(snapshot)
    run_ps(
        f"Restore-VMSnapshot -VMName {vm} -Name {snap} -Confirm:$false; Start-VM -Name {vm}",
        timeout_s=900,
    )


def turn_off(name: str) -> None:
    run_ps(f"Stop-VM -Name {ps_quote(name)} -TurnOff -Force")


def remove(name: str, disk: Path) -> None:
    """VM과 차분 디스크를 지운다(golden은 그대로)."""
    if vm_state(name) is not None:
        quoted = ps_quote(name)
        run_ps(
            f"$vm = Get-VM -Name {quoted}; "
            f"if ($vm.State -ne 'Off') {{ Stop-VM -Name {quoted} -TurnOff -Force }}; "
            f"Remove-VM -Name {quoted} -Force"
        )
    disk.unlink(missing_ok=True)
