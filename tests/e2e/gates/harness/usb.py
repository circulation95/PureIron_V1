"""USB 대용 디스크(M6-4b-2 ★P4·G2-7) — 현장 운영자가 USB로 번들을 들고 오고 보고 묶음을 들고 나가는
길을 ext4 VHDX로 흉내 낸다(M6-0 탐침 ②: exFAT은 golden 커널에 모듈이 없어 ext4).

한 번 쓰는 순서:
1. `image` — WSL에서 빈 raw 파일에 `mke2fs -t ext4 -L <라벨> -d <파일 디렉터리>` → `qemu-img
   convert -O vhdx`(동적)로 `<E2E>/usb/<run>-<현장>-<용도>-<임의 hex>.vhdx`를 만든다. with 블록이
   끝나면(예외 경로 포함) VHDX를 지운다.
2. `attached` — Hyper-V SCSI hot-add(`hyperv.attach_disk`) → VM에서 SCSI 다시 읽기·라벨로 장치
   찾기(`blkid -t LABEL=…`) → `MOUNT`에 ro 마운트 → (블록 본문: 마운트 경로를 쓴다) → umount ·
   장치 삭제 → hot-remove. 예외 경로에서도 떼고, 그때 떼기 실패는 로그만 남긴다(원래 예외의 분류를
   바꾸지 않게).
3. `read_dir` — 뗀 VHDX를 WSL에서 raw로 바꿔 `debugfs rdump`로 디렉터리를 꺼낸다(VM이 쓴 보고
   묶음, G2-7). 루트 권한·마운트가 없어도 된다.

실패 분류: 만들기·붙이기·장치 찾기·마운트·떼기·꺼내기·지우기는 시험 배선이라 모두 `InfraError`·
`HyperVError`(infra)다. 마운트 경로 위에서 도는 제품 명령(설치·apply·report export)의 실패는 부른
쪽 규칙 그대로다. 기록(`record`)에 시각(UTC ISO)·장치·VHDX 이름을 남긴다(비밀값 없음).
"""

from __future__ import annotations

import secrets
import shlex
import subprocess
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness import golden, hyperv, paths
from harness.context import InfraError, log
from harness.site import Shell, as_root
from harness.vm import REMOTE_ERRORS

INSTALL_SITE = "site-b"
"""G2-1 설치 번들을 USB 대용 디스크로 받는 현장(★P4). 다른 현장은 ssh(scp) 그대로."""
SIZE_GIB = 4
"""raw·VHDX 크기 — 설치 번들 약 0.7 GB + g1 번들 약 0.7 GB에 여유. 동적 VHDX·sparse raw라 쓴 만큼만
차지한다."""
MOUNT = "/media/e2e-usb"
"""VM 마운트 지점(현장 README의 `/media/usb`와 같은 자리, 이름만 시험용)."""
MOUNT_OPTIONS = "ro,nosuid,nodev,noexec"
"""받는 매체는 읽기만 한다 — 설치·apply는 번들을 읽고 VM 디스크에 사본·추출물을 둔다."""
FIND_TIMEOUT_S = 60.0
"""hot-add 뒤 VM이 장치를 보기까지(M6-0 실측: 바로 `sdb`)."""
FIND_POLL_S = 2.0
WSL_TIMEOUT_S = 900.0
RESCAN = (
    "for s in /sys/class/scsi_host/host*/scan; do echo '- - -' > \"$s\"; done; "
    "udevadm settle -t 10 || true; "
)
"""hot-add한 SCSI 디스크를 바로 보게 다시 읽는다(hv_storvsc는 보통 스스로 알지만 확실히)."""


def usb_dir() -> Path:
    """USB 대용 VHDX를 두는 곳 — 부를 때 `paths.E2E`를 읽는다(단위 시험이 바꾼다)."""
    return paths.E2E / "usb"


@dataclass(frozen=True)
class Ops:
    """WSL·Hyper-V·시계 — 기본값이 실제 호출이다."""

    wsl: Callable[[str], str] = field(
        default=lambda script: golden.wsl(script, timeout_s=WSL_TIMEOUT_S)
    )
    attach: Callable[[str, Path], None] = hyperv.attach_disk
    detach: Callable[[str, Path], int] = hyperv.detach_disk
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Stick:
    vhdx: Path
    label: str
    """ext4 라벨(16자 이하) — VM에서 장치를 찾는 열쇠. 파일 이름의 임의 hex와 같다."""


def new_stick(run_id: str, site_id: str, purpose: str) -> Stick:
    token = secrets.token_hex(4)
    return Stick(usb_dir() / f"{run_id}-{site_id}-{purpose}-{token}.vhdx", f"e2e-{token}")


def stamp(ops: Ops) -> str:
    return ops.now().isoformat(timespec="seconds")


def make_script(stick: Stick, files: Sequence[Path]) -> str:
    """WSL bash: 파일을 임시 디렉터리에 모아 ext4 raw → 동적 VHDX."""
    sources = " ".join(shlex.quote(golden.wsl_path(f)) for f in files)
    return (
        "set -euo pipefail; work=$(mktemp -d); trap 'rm -rf \"$work\"' EXIT; "
        f'mkdir "$work/root"; cp {sources} "$work/root/"; '
        f'truncate -s {SIZE_GIB}G "$work/usb.raw"; '
        f'mke2fs -q -t ext4 -L {stick.label} -d "$work/root" "$work/usb.raw"; '
        f'{golden.QEMU_IMG} convert -O vhdx -o subformat=dynamic "$work/usb.raw" '
        f"{shlex.quote(golden.wsl_path(stick.vhdx))}"
    )


@contextmanager
def image(
    run_id: str,
    site_id: str,
    purpose: str,
    files: Sequence[Path],
    record: dict[str, object],
    *,
    ops: Ops | None = None,
) -> Generator[Stick]:
    """`files`를 담은 USB 대용 VHDX(모듈 설명 1). 끝에(예외 경로 포함) VHDX를 지우고 `record`에
    `deleted`를 남긴다. 지우기 실패는 정상 경로면 `InfraError`, 예외 경로면 로그만."""
    ops = ops or Ops()
    stick = new_stick(run_id, site_id, purpose)
    stick.vhdx.parent.mkdir(parents=True, exist_ok=True)
    record |= {"vhdx": stick.vhdx.name, "label": stick.label}
    started = ops.clock()
    try:
        try:
            ops.wsl(make_script(stick, files))
        except (golden.WslError, golden.WslScriptError) as exc:
            raise InfraError(f"USB image {stick.vhdx.name} not made: {exc}") from exc
        record["make_s"] = round(ops.clock() - started, 1)
        yield stick
    except BaseException:
        delete(stick, record, quiet=True)
        raise
    delete(stick, record, quiet=False)


def delete(stick: Stick, record: dict[str, object], *, quiet: bool) -> None:
    try:
        stick.vhdx.unlink(missing_ok=True)
    except OSError as exc:
        record["deleted"] = False
        if not quiet:
            raise InfraError(f"USB image {stick.vhdx} not deleted: {exc}") from exc
        log(f"[usb] {stick.vhdx.name} not deleted: {exc}")
        return
    record["deleted"] = True


def find_device(host: Shell, label: str, ops: Ops) -> str:
    """라벨 `label`의 블록 장치(`/dev/sdX`). `FIND_TIMEOUT_S` 안에 없으면 `InfraError`."""
    deadline = ops.clock() + FIND_TIMEOUT_S
    while True:
        try:
            out = host.run(
                as_root(RESCAN + f"blkid -c /dev/null -o device -t LABEL={label}"),
                check=False,
                timeout_s=60,
            ).stdout
        except (*REMOTE_ERRORS, subprocess.TimeoutExpired):
            out = ""
        devices = [line.strip() for line in out.splitlines() if line.strip().startswith("/dev/")]
        if devices:
            return devices[0]
        if ops.clock() >= deadline:
            raise InfraError(f"USB disk {label} not seen in the VM within {FIND_TIMEOUT_S:.0f}s")
        ops.sleep(FIND_POLL_S)


def vm_run(host: Shell, command: str, what: str) -> None:
    """VM 배선 명령(마운트 등) — 실패는 `InfraError`."""
    try:
        host.run(command, timeout_s=120)
    except (*REMOTE_ERRORS, subprocess.TimeoutExpired) as exc:
        raise InfraError(f"USB {what} failed: {exc}") from exc


def remount_rw(host: Shell) -> None:
    """같은 디스크를 쓰기로 다시 마운트한다(G2-7 — 보고 묶음을 USB에 쓴다)."""
    vm_run(host, f"sudo mount -o remount,rw {MOUNT}", "remount rw")


@contextmanager
def attached(
    host: Shell, vm_name: str, stick: Stick, record: dict[str, object], *, ops: Ops | None = None
) -> Generator[str]:
    """hot-add → 장치 찾기 → ro 마운트 → (마운트 경로) → umount → hot-remove(모듈 설명 2)."""
    ops = ops or Ops()
    ops.attach(vm_name, stick.vhdx)
    record["hot_added_at"] = stamp(ops)
    try:
        device = find_device(host, stick.label, ops)
        record["device"] = device
        vm_run(
            host,
            as_root(f"install -d -m 0755 {MOUNT} && mount -o {MOUNT_OPTIONS} {device} {MOUNT}"),
            "mount",
        )
        record["mounted_at"] = stamp(ops)
        yield MOUNT
    except BaseException:
        release(host, vm_name, stick, record, ops, quiet=True)
        raise
    release(host, vm_name, stick, record, ops, quiet=False)


def release(
    host: Shell, vm_name: str, stick: Stick, record: dict[str, object], ops: Ops, *, quiet: bool
) -> None:
    """VM에서 umount·장치 삭제(실패해도 계속 — 뗄 수는 있다) → hot-remove. 떼기 실패는 `quiet`면
    로그만(시간 초과 포함 — 원래 예외의 분류를 바꾸지 않게), 아니면 그대로 올린다(infra)."""
    device = str(record.get("device") or "")
    name = device.removeprefix("/dev/")
    drop = f"echo 1 > /sys/block/{name}/device/delete; " if name.isalnum() else ""
    script = f"sync; umount {MOUNT}; rmdir {MOUNT}; {drop}true"
    try:
        host.run(as_root(script), check=False, timeout_s=120)
    except (*REMOTE_ERRORS, subprocess.TimeoutExpired) as exc:
        log(f"[usb] umount {stick.label} failed: {exc}")
    try:
        removed = ops.detach(vm_name, stick.vhdx)
    except (hyperv.HyperVError, subprocess.TimeoutExpired) as exc:  # 시간 초과도(리뷰 M2)
        record["removed"] = False
        if not quiet:
            raise
        log(f"[usb] {stick.vhdx.name} not detached from {vm_name}: {exc}")
        return
    record["removed"] = removed >= 1
    record["removed_at"] = stamp(ops)


def read_script(stick: Stick, subdir: str, out: Path) -> str:
    """WSL bash: 뗀 VHDX를 사본에서 열어(로그가 남았으면 `check -r all`이 되감는다) raw로 →
    `debugfs rdump /<subdir>` → `out`(Windows 디렉터리의 WSL 경로)."""
    return (
        "set -euo pipefail; work=$(mktemp -d); trap 'rm -rf \"$work\"' EXIT; "
        f'cp {shlex.quote(golden.wsl_path(stick.vhdx))} "$work/usb.vhdx"; '
        f'{golden.QEMU_IMG} check -r all "$work/usb.vhdx" >/dev/null 2>&1 || true; '
        f'{golden.QEMU_IMG} convert -O raw "$work/usb.vhdx" "$work/usb.raw"; '
        f"mkdir -p {shlex.quote(golden.wsl_path(out))}; "
        f"debugfs -R {shlex.quote(f'rdump /{subdir} {golden.wsl_path(out)}')} "
        '"$work/usb.raw" 2>&1'
    )


def read_dir(stick: Stick, subdir: str, out: Path, *, ops: Ops | None = None) -> list[Path]:
    """뗀 디스크의 `/<subdir>` 파일을 `out/<subdir>/`로 꺼내 이름 순으로 돌려준다(모듈 설명 3)."""
    ops = ops or Ops()
    out.mkdir(parents=True, exist_ok=True)
    try:
        ops.wsl(read_script(stick, subdir, out))
    except (golden.WslError, golden.WslScriptError) as exc:
        raise InfraError(f"USB image {stick.vhdx.name} not read: {exc}") from exc
    return sorted(p for p in (out / subdir).glob("*") if p.is_file())


def remove_leftovers(
    *, detach_under: Callable[[Path], list[str]] = hyperv.detach_under
) -> list[str]:
    """preflight 정리: 지난 run이 남긴 USB 대용 디스크 — 어느 VM에 붙어 있으면 떼고, VHDX를 지운다.
    실패는 정리 기록에만 남긴다(그 밤을 막지 않는다)."""
    folder = usb_dir()
    actions: list[str] = []
    try:
        actions += [f"usb detached {line}" for line in detach_under(folder)]
    except hyperv.HyperVError as exc:
        actions.append(f"usb detach (leftover) failed: {exc}")
    for vhdx in sorted(folder.glob("*.vhdx")) if folder.is_dir() else []:
        try:
            vhdx.unlink()
            actions.append(f"usb removed {vhdx.name}")
        except OSError as exc:
            actions.append(f"usb {vhdx.name} not removed: {exc}")
    return actions
