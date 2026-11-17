"""USB 대용 디스크(M6-4b-2 ★P4) — site-b G2-1 설치가 USB로 오고(만들기 → hot-add → 라벨로 장치
찾기 → ro 마운트 → 같은 runbook → umount → hot-remove → VHDX 삭제), 예외 경로에서도 떼고 지우며,
preflight가 남은 것을 치운다. 변이 대상: site-b가 ssh로 돌아감 · 떼기 생략(정상·예외) · VHDX
삭제 생략."""

from __future__ import annotations

import dataclasses
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from g2_fakes import session
from harness import g2, golden, hyperv, judges, paths, preinstall, usb
from harness.context import InfraError
from harness.vm import RemoteCommandError, Vm


@dataclass
class FakeShell:
    """VM 셸 가짜 — 명령을 `events`에, 라벨 찾기(`blkid`)는 `seen_after`번째부터 장치를 낸다."""

    events: list[str]
    device: str = "/dev/sdb"
    seen_after: int = 1
    asked: int = 0

    def run(
        self, command: str, *, timeout_s: float = 600, check: bool = True, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        out = ""
        if "blkid" in command:
            self.asked += 1
            self.events.append("vm find")
            if self.seen_after and self.asked >= self.seen_after:
                out = self.device + "\n"
        elif "mount -o ro" in command:
            self.events.append("vm mount ro")
        elif "umount" in command:
            self.events.append("vm umount")
        else:
            self.events.append(f"vm {command}")
        return subprocess.CompletedProcess[str]([], 0, out, "")

    def put(self, local: Path, remote: str, *, timeout_s: float = 1800) -> None:
        self.events.append(f"scp {local.name}")


@dataclass
class FakeOps:
    """WSL(VHDX 파일을 만든다)·Hyper-V 붙이기/떼기 가짜."""

    events: list[str]
    attached: list[Path] = field(default_factory=list[Path])
    detach_error: bool = False
    scripts: list[str] = field(default_factory=list[str])

    def ops(self) -> usb.Ops:
        def wsl(script: str) -> str:
            self.scripts.append(script)
            self.events.append("wsl make")
            name = script.rsplit("/", 1)[-1].strip("'\" ")  # 끝 인자 = VHDX(WSL 경로)
            (paths.E2E / "usb" / name).write_bytes(b"vhdx")
            return ""

        def attach(vm: str, vhdx: Path) -> None:
            self.events.append(f"attach {vm}")
            self.attached.append(vhdx)

        def detach(vm: str, vhdx: Path) -> int:
            self.events.append(f"detach {vm}")
            if self.detach_error:
                raise hyperv.HyperVError("Remove-VMHardDiskDrive failed")
            return 1 if vhdx in self.attached else 0

        return usb.Ops(
            wsl=wsl,
            attach=attach,
            detach=detach,
            now=lambda: datetime(2026, 11, 18, 3, 0, tzinfo=UTC),
            clock=lambda: 0.0,
            sleep=lambda s: None,
        )


@pytest.fixture
def e2e_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "e2e"
    (root / "usb").mkdir(parents=True)
    monkeypatch.setattr(paths, "E2E", root)
    return root


def install_once(
    tmp_path: Path,
    site_id: str,
    *,
    fail: BaseException | None = None,
    detach_error: bool = False,
    seen_after: int = 1,
    clock: Callable[[], float] = lambda: 0.0,
) -> tuple[list[str], list[dict[str, object]], g2.SiteRun, BaseException | None]:
    """`g2.install`을 가짜 VM·WSL·Hyper-V로 한 번. (사건, runbook 호출 인자, SiteRun, 예외)."""
    run = session(tmp_path).sites[site_id]
    run.ctx.install_pib = str(tmp_path / "pureiron-install-r5.pib")
    events: list[str] = []
    calls: list[dict[str, object]] = []
    shell = FakeShell(events, seen_after=seen_after)
    ops = dataclasses.replace(FakeOps(events, detach_error=detach_error).ops(), clock=clock)

    def runbook(host: object, *bundles: Path, **kwargs: object) -> float:
        events.append("runbook")
        calls.append({"bundles": [b.name for b in bundles], **kwargs})
        if fail is not None:
            raise fail
        return 12.0

    raised: BaseException | None = None
    try:
        g2.install(run, shell, runbook=runbook, ops=ops)
    except (RemoteCommandError, InfraError, hyperv.HyperVError) as exc:
        raised = exc
    return events, calls, run, raised


def usb_record(run: g2.SiteRun) -> dict[str, object]:
    record = run.evidence["usb"]
    assert isinstance(record, dict)
    return cast(dict[str, object], record)


def test_site_b_installs_from_the_usb_disk_and_removes_it(e2e_root: Path, tmp_path: Path) -> None:
    """★P4: site-b는 VHDX 만들기 → hot-add → 장치 찾기 → ro 마운트 → 같은 runbook(마운트 경로,
    scp 없음) → umount → hot-remove → VHDX 삭제. 매체·시각이 G2-1 기록(evidence)에 남는다."""
    events, calls, run, raised = install_once(tmp_path, "site-b")
    assert raised is None
    assert events == [
        "wsl make",
        "attach pureiron-site-b",
        "vm find",
        "vm mount ro",
        "runbook",
        "vm umount",
        "detach pureiron-site-b",
    ]
    assert calls[0]["bundle_dir"] == usb.MOUNT
    assert calls[0]["bundles"] == [
        "pureiron-install-r5.pib",
        f"pureiron-site-b-g1-{run.release}.pib",
    ]
    record = usb_record(run)
    assert run.evidence["media"] == "usb"
    assert {"hot_added_at", "mounted_at", "removed_at"} <= set(record)
    assert record["device"] == "/dev/sdb"
    assert record["removed"] is True
    assert record["deleted"] is True
    assert not list((e2e_root / "usb").glob("*.vhdx"))
    assert judges.judge_install_media(run.evidence, "usb") == {
        "install_media": True,
        "usb_mounted": True,
        "usb_removed": True,
    }


def test_site_a_installs_over_ssh(e2e_root: Path, tmp_path: Path) -> None:
    events, calls, run, raised = install_once(tmp_path, "site-a")
    assert raised is None
    assert events == ["runbook"]
    assert calls[0]["bundle_dir"] is None
    assert run.evidence["media"] == "ssh"
    assert judges.judge_install_media(run.evidence, "ssh") == {"install_media": True}


def test_a_failed_install_still_detaches_and_deletes_the_usb_disk(
    e2e_root: Path, tmp_path: Path
) -> None:
    """설치 명령이 실패해도(제품 — 분류 그대로) umount·hot-remove·VHDX 삭제를 한다."""
    broken = RemoteCommandError("install.sh exited 1", 1)
    events, _, run, raised = install_once(tmp_path, "site-b", fail=broken)
    assert raised is broken
    assert events[-2:] == ["vm umount", "detach pureiron-site-b"]
    record = usb_record(run)
    assert record["removed"] is True
    assert record["deleted"] is True
    assert not list((e2e_root / "usb").glob("*.vhdx"))


def test_a_detach_failure_does_not_replace_the_product_failure(
    e2e_root: Path, tmp_path: Path
) -> None:
    """예외 경로의 떼기 실패는 로그만 — 원래 제품 실패가 infra로 바뀌지 않는다(재시도로 숨지
    않게). 판정은 거짓(`usb_removed`)."""
    broken = RemoteCommandError("install.sh exited 1", 1)
    _, _, run, raised = install_once(tmp_path, "site-b", fail=broken, detach_error=True)
    assert raised is broken
    assert usb_record(run)["removed"] is False
    assert judges.judge_install_media(run.evidence, "usb")["usb_removed"] is False


def test_a_detach_failure_after_a_good_install_is_infra(e2e_root: Path, tmp_path: Path) -> None:
    _, _, run, raised = install_once(tmp_path, "site-b", detach_error=True)
    assert isinstance(raised, hyperv.HyperVError)
    assert usb_record(run)["removed"] is False
    assert "deleted" in usb_record(run)  # 지우기는 시도했다


def test_a_disk_the_vm_never_sees_is_infra_and_is_still_removed(
    e2e_root: Path, tmp_path: Path
) -> None:
    ticks = iter(range(0, 1000, 10))
    events, calls, _, raised = install_once(
        tmp_path, "site-b", seen_after=0, clock=lambda: float(next(ticks))
    )
    assert isinstance(raised, InfraError)
    assert "not seen" in str(raised)
    assert not calls
    assert events[-2:] == ["vm umount", "detach pureiron-site-b"]
    assert not list((e2e_root / "usb").glob("*.vhdx"))


def test_a_usb_image_that_cannot_be_made_is_infra(e2e_root: Path) -> None:
    def wsl(script: str) -> str:
        raise golden.WslScriptError("mke2fs: not found", 127)

    record: dict[str, object] = {}
    with (
        pytest.raises(InfraError, match="not made"),
        usb.image("20261118T030000Z", "site-b", "install", [], record, ops=usb.Ops(wsl=wsl)),
    ):
        pass
    assert record["deleted"] is True


def test_the_usb_image_is_ext4_with_a_label_and_a_dynamic_vhdx(tmp_path: Path) -> None:
    stick = usb.Stick(tmp_path / "x.vhdx", "e2e-0011aabb")
    script = usb.make_script(stick, [tmp_path / "a.pib", tmp_path / "b.pib"])
    assert "mke2fs -q -t ext4 -L e2e-0011aabb -d" in script
    assert "convert -O vhdx -o subformat=dynamic" in script
    assert golden.wsl_path(tmp_path / "a.pib") in script
    assert script.endswith(golden.wsl_path(stick.vhdx))
    read = usb.read_script(stick, "reports", tmp_path / "out")
    assert "convert -O raw" in read
    assert f"rdump /reports {golden.wsl_path(tmp_path / 'out')}" in read


def test_new_sticks_are_per_run_and_unique(e2e_root: Path) -> None:
    one = usb.new_stick("20261118T030000Z", "site-b", "install")
    two = usb.new_stick("20261118T030000Z", "site-b", "install")
    assert one.vhdx.parent == e2e_root / "usb"
    assert one.vhdx.name.startswith("20261118T030000Z-site-b-install-")
    assert one.vhdx != two.vhdx
    assert one.label != two.label
    assert len(one.label) <= 16  # ext4 라벨 한도


def test_the_hot_add_and_remove_cmdlets(monkeypatch: pytest.MonkeyPatch) -> None:
    scripts: list[str] = []

    def run_ps(script: str, *, timeout_s: float = 300) -> str:
        scripts.append(script)
        return "1"

    monkeypatch.setattr(hyperv, "run_ps", run_ps)
    vhdx = Path("C:/PureIron_e2e/usb/x.vhdx")
    hyperv.attach_disk("pureiron-site-b", vhdx)
    assert hyperv.detach_disk("pureiron-site-b", vhdx) == 1
    assert hyperv.detach_under(Path("C:/PureIron_e2e/usb")) == ["1"]
    quoted = hyperv.ps_quote(str(vhdx))
    assert scripts[0] == (
        f"Add-VMHardDiskDrive -VMName 'pureiron-site-b' -ControllerType SCSI -Path {quoted}"
    )
    assert "Get-VMHardDiskDrive -VMName 'pureiron-site-b'" in scripts[1]
    assert f"$_.Path -eq {quoted}" in scripts[1]
    assert "Remove-VMHardDiskDrive" in scripts[1]
    assert "Get-VM | Get-VMHardDiskDrive" in scripts[2]
    assert "Remove-VMHardDiskDrive" in scripts[2]


def test_preflight_removes_usb_leftovers(e2e_root: Path) -> None:
    (e2e_root / "usb" / "old-site-b-install-aa.vhdx").write_bytes(b"x")
    (e2e_root / "usb" / "keep.txt").write_text("x", "utf-8")

    def detach_under(folder: Path) -> list[str]:
        assert folder == e2e_root / "usb"
        return ["pureiron-site-b old.vhdx"]

    assert usb.remove_leftovers(detach_under=detach_under) == [
        "usb detached pureiron-site-b old.vhdx",
        "usb removed old-site-b-install-aa.vhdx",
    ]
    assert [p.name for p in (e2e_root / "usb").iterdir()] == ["keep.txt"]

    def broken(folder: Path) -> list[str]:
        raise hyperv.HyperVError("Get-VM failed")

    assert usb.remove_leftovers(detach_under=broken) == [
        "usb detach (leftover) failed: Get-VM failed"
    ]


def test_the_preinstall_probes_copy_the_bundle_from_the_usb_mount(tmp_path: Path) -> None:
    run = session(tmp_path).sites["site-b"]
    run.ctx.install_pib = str(tmp_path / "pureiron-install-r5.pib")
    seen: list[str] = []

    def probes(host: preinstall.Shell, book: preinstall.Runbook) -> preinstall.Found:
        seen.append(book.install_remote)
        return {}, {}

    host = Vm("10.77.0.11", tmp_path / "k")
    hook = g2.preinstall_hook(run, host, probes=probes, bundle_dir=usb.MOUNT)
    assert hook is not None
    hook()
    assert seen == [f"{usb.MOUNT}/pureiron-install-r5.pib"]


@pytest.mark.parametrize("site_id", ["site-a", "site-b"])
def test_the_install_media_judge_each_check_matters(site_id: str) -> None:
    want = g2.install_media(site_id)
    assert want == ("usb" if site_id == "site-b" else "ssh")
    record: dict[str, object] = {
        "hot_added_at": "a",
        "mounted_at": "b",
        "removed": True,
        "deleted": True,
    }
    good: dict[str, object] = {"media": want, "usb": record}
    assert all(judges.judge_install_media(good, want).values())
    other = "ssh" if want == "usb" else "usb"
    assert judges.judge_install_media({**good, "media": other}, want)["install_media"] is False
    if want == "usb":
        for key, value in (("removed", False), ("deleted", False), ("mounted_at", None)):
            checks = judges.judge_install_media({**good, "usb": {**record, key: value}}, want)
            assert not all(checks.values()), key
