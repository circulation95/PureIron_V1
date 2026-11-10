"""e2e VM 재료(PD-24): golden VHDX, cloud-init NoCloud seed ISO, SSH 키.

    uv run python tests/e2e/gates/harness/golden.py golden --image <cloudimg.img> \\
        --out <golden.vhdx>

- golden: Ubuntu 24.04 클라우드 이미지(qcow2)를 WSL의 `qemu-img`로 64 GiB 동적 VHDX로 바꾸고
  읽기 전용으로 둔다. 64 GiB인 이유: preflight가 `/var/lib/pureiron` 여유 ≥ 50 GiB를 요구한다
  (동적 디스크라 실제 사용분만 차지). VM은 이 위의 차분 디스크로만 만든다.
- seed ISO(label `cidata`): 사용자 `pureiron`(SSH 키, sudo), 내부 전용 vSwitch 고정 IP,
  OS의 외부 접속 시도(시간 동기·apt·snap·motd)를 끄고, 외부로 나가려는 패킷을 세는 nftables
  테이블 `inet e2e_audit`를 둔다. 기본 경로·DNS는 호스트(전달하지 않음)로 두어 외부 접속 *시도*가
  카운터에 잡히게 한다(경로가 없으면 패킷이 생기지 않아 0이 당연해진다). 선언된 현장 장비
  끝점(`field`)으로의 전달은 `field_fwd`로 따로 센다. golden은 프로젝트 트리를 모르므로 끝점은
  호출자가 넘긴다.
- seed의 `dummy`(G2, 기본 꺼짐): VM 안에 가짜 장비 주소(fixture 장비 `10.0.0.5`)를 dummy
  인터페이스로 붙인다(systemd-networkd, 재부팅 뒤에도 남는다). 시뮬레이터(`site.start_simulator`,
  host network)가 그 주소의 modbus 포트에서 답한다. collector(compose bridge)가 그 주소로 보내는
  패킷은 VM 자신의 주소라 forward가 아니라 input으로 들어온다 — `ext_out_v4`(output)·`ext_fwd`·
  `field_fwd`(forward, eth0) 어느 카운터에도 잡히지 않는다. 시뮬레이터의 응답은 output으로 나가지만
  목적지가 docker bridge(`172.16.0.0/12`)라 `ext_out_v4` 제외 대상이다.
"""

from __future__ import annotations

import argparse
import io
import ipaddress
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pycdlib

GOLDEN_SIZE_GIB = 64
WSL_DISTRO = "Ubuntu"
QEMU_IMG = "$HOME/m2d-tools/qemu-img"


WSL_STARTED = "__pureiron_e2e_wsl_started__"
"""`wsl()`이 스크립트 앞에서 stderr로 내는 표지 — 이것이 보이면 wsl.exe가 bash를 띄운 것이다."""
WSL_INFRA_EXIT = 75
"""WSL 스크립트가 "실행 환경 탓"(Docker 무응답·BASE image store 없음·git 사본 실패)으로 멈출 때 내는
종료 코드(EX_TEMPFAIL, `bundles.INFRA_EXIT`와 같다)."""
WSL_SERVICE_ERROR = "Error code: Wsl/"
"""wsl.exe 자신의 오류 표지(예: `Wsl/Service/CreateInstance/.../0x8007274c`)."""
WSL_TERMINATED = "instance has terminated"
"""wsl.exe가 WSL VM이 도중에 죽었을 때 내는 문구(표지 뒤에도 나온다 — 리뷰 r2 L1)."""
BASH_MAX_EXIT = 255
"""bash(와 그 안의 스크립트)가 낼 수 있는 가장 큰 종료 코드 — 이보다 크거나 음수면 wsl.exe 자신의
코드다(예: 4294967295 = 0xFFFFFFFF)."""


class WslError(RuntimeError):
    """wsl.exe **호출 자체**의 실패(`infra`): bash가 뜨지 않았거나, wsl.exe가 자기 오류를 냈거나,
    스크립트가 `WSL_INFRA_EXIT`로 실행 환경 탓을 알렸다. `RuntimeError`를 잡던 호출자는 그대로
    잡는다."""


class WslScriptError(RuntimeError):
    """WSL 안 스크립트가 0 아닌 코드로 끝났다(`product` — 호출된 빌드·번들 스크립트의 실패).
    `WslError`의 하위 클래스가 아니다(분류가 갈린다). `RuntimeError`를 잡던 호출자는 그대로
    잡는다."""

    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass(frozen=True, slots=True)
class Network:
    host_ip: str = "10.77.0.1"
    prefix: int = 24

    @property
    def cidr(self) -> str:
        octets = self.host_ip.split(".")
        return ".".join([*octets[:3], "0"]) + f"/{self.prefix}"


# 외부로 나가려는 패킷을 세는 규칙(이름 → nft match).
# 내부 망·loopback·docker bridge(172.16/12)·멀티캐스트(224/4, docker0 IGMP 등 링크 밖으로 안 나감)·
# 제한 broadcast는 제외한다.
OUT_RULES = (
    (
        "ext_out_v4",
        "ip daddr != {{ 127.0.0.0/8, {cidr}, 172.16.0.0/12, 224.0.0.0/4, 255.255.255.255 }}",
    ),
    ("dns_out", "meta l4proto {{ tcp, udp }} th dport 53 ip daddr != 127.0.0.0/8"),
    ("ext_out_v6", "ip6 daddr != {{ ::1, fe80::/10, ff00::/8 }}"),
)
FWD_RULES = (("ext_fwd", 'oifname "eth0"'),)
FIELD_RULE = "field_fwd"
"""선언된 현장 장비(host·TCP port)로 나가는 전달 — collector 폴링이라 외부 접속이 아니다."""

Endpoint = tuple[str, int]
"""(IPv4 주소, TCP port)."""


def field_rule(field: tuple[Endpoint, ...]) -> list[str]:
    """`field` 끝점으로의 eth0 전달을 `ext_fwd` 앞에서 세고 빠져나간다. 끝점이 없으면 규칙도
    없다."""
    if not field:
        return []
    targets = ", ".join(f"{host} . {port}" for host, port in field)
    return [
        # accept는 이 base chain(e2e_audit) 평가만 끝낸다 — docker 등 다른 체인은 그대로 거친다.
        f"        {FWD_RULES[0][1]} ip daddr . tcp dport {{ {targets} }} counter accept "
        f'comment "{FIELD_RULE}"'
    ]


def nft_audit(cidr: str, field: tuple[Endpoint, ...] = ()) -> str:
    def rules(items: tuple[tuple[str, str], ...]) -> list[str]:
        return [
            f'        {match.format(cidr=cidr)} counter log prefix "e2e-{name} " comment "{name}"'
            for name, match in items
        ]

    return "\n".join(
        [
            "table inet e2e_audit {",
            "    chain audit_out {",
            "        type filter hook output priority -10; policy accept;",
            *rules(OUT_RULES),
            "    }",
            "    chain audit_fwd {",  # `fwd`는 nft 예약어
            "        type filter hook forward priority -10; policy accept;",
            *field_rule(field),
            *rules(FWD_RULES),
            "    }",
            "}",
            "",
        ]
    )


USER_DATA = """\
#cloud-config
hostname: {hostname}
users:
  - name: pureiron
    groups: [sudo]
    shell: /bin/bash
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    lock_passwd: true
    ssh_authorized_keys:
      - {ssh_pub}
ssh_pwauth: false
package_update: false
package_upgrade: false
# network-online 전에 끈다 — runcmd에서 끄면 pollinate(entropy.ubuntu.com)·snapd(api.snapcraft.io)가
# 이미 DNS를 질의한 뒤다(첫 실측 dns_out 36).
bootcmd:
  - [systemctl, mask, --now, pollinate.service, snapd.service, snapd.socket,
     snapd.seeded.service, systemd-timesyncd.service]
  # SSH 로그인마다 update-motd의 릴리스 업그레이드 확인이 changelogs.ubuntu.com을 조회했다
  # (실측 dns_lookups). sshd가 뜨기 전에 끈다.
  - [sed, -i, "s/^Prompt=.*/Prompt=never/", /etc/update-manager/release-upgrades]
  - [chmod, -x, /etc/update-motd.d/91-release-upgrade]
write_files:
  - path: /etc/e2e-audit.nft
    permissions: "0644"
    content: |
{nft}
  - path: /etc/systemd/system/e2e-audit.service
    permissions: "0644"
    content: |
      [Unit]
      Description=e2e external-traffic audit counters
      DefaultDependencies=no
      Before=network-pre.target
      Wants=network-pre.target
      [Service]
      Type=oneshot
      RemainAfterExit=yes
      ExecStart=/usr/sbin/nft -f /etc/e2e-audit.nft
      [Install]
      WantedBy=multi-user.target
  # 외부 DNS 패킷은 모두 systemd-resolved가 보낸다(컨테이너 질의도 Docker 내장 DNS → resolved).
  # 디버그 로그의 `Looking up RR for <이름>`이 dns_out 카운터의 출처 증거가 된다.
  - path: /etc/systemd/system/systemd-resolved.service.d/e2e-debug.conf
    permissions: "0644"
    content: |
      [Service]
      Environment=SYSTEMD_LOG_LEVEL=debug
runcmd:
  - [systemctl, daemon-reload]
  - [systemctl, restart, systemd-resolved.service]
  - [systemctl, enable, --now, e2e-audit.service]
  - [systemctl, disable, --now, apt-daily.timer, apt-daily-upgrade.timer, motd-news.timer,
     fwupd-refresh.timer]
  - [sh, -c, "pro config set apt_news=false || true"]
  - [touch, /var/lib/e2e-ready]
"""

NETWORK_CONFIG = """\
version: 2
ethernets:
  eth0:
    match:
      driver: hv_netvsc
    set-name: eth0
    addresses: [{ip}/{prefix}]
    routes:
      - to: default
        via: {host_ip}
    nameservers:
      addresses: [{host_ip}]
"""


def wsl_path(path: Path) -> str:
    """`C:\\a\\b` → `/mnt/c/a/b`."""
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    return str(PurePosixPath("/mnt", drive, *resolved.parts[1:]))


def wsl_failure(returncode: int, stderr: str) -> RuntimeError | None:
    """`wsl()` 결과의 실패 종류(성공이면 None). 표지가 없거나(bash가 뜨지 않음) wsl.exe 오류
    표지·"instance has terminated"가 있거나, 종료 코드가 bash 범위(0~255) 밖이거나(wsl.exe 자신의
    코드 — 스크립트 도중 WSL VM이 죽음), `WSL_INFRA_EXIT`면 `WslError`, 그 밖의 0 아닌 코드는
    `WslScriptError`다. wsl.exe 자신의 메시지는 UTF-16일 수 있어 NUL을 지우고 본다."""
    if returncode == 0:
        return None
    plain = stderr.replace("\x00", "")
    started = WSL_STARTED in plain
    text = plain.replace(WSL_STARTED + "\n", "").replace(WSL_STARTED, "").strip()
    message = f"wsl failed ({returncode}): {text}"
    wsl_exe = WSL_SERVICE_ERROR in plain or WSL_TERMINATED in plain
    outside_bash = returncode < 0 or returncode > BASH_MAX_EXIT
    if not started or wsl_exe or outside_bash or returncode == WSL_INFRA_EXIT:
        return WslError(message)
    return WslScriptError(f"wsl script failed ({returncode}): {text}", returncode)


def wsl(
    script: str,
    *,
    timeout_s: float = 1800,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """WSL bash로 `script`를 돌리고 stdout을 돌려준다. 실패 종류는 `wsl_failure`, 시간 초과는
    `subprocess.TimeoutExpired`(그대로 전파). `runner`는 단위 시험이 바꿔 끼운다."""
    result = runner(
        ["wsl", "-d", WSL_DISTRO, "--exec", "bash", "-c", f"echo {WSL_STARTED} >&2; {script}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    failure = wsl_failure(result.returncode, result.stderr or "")
    if failure is not None:
        raise failure
    return result.stdout


def build_golden(image: Path, out: Path, size_gib: int = GOLDEN_SIZE_GIB) -> Path:
    """qcow2 → raw(WSL ext4 임시) → 크기 확장 → 동적 VHDX. 이미 있으면 그대로 둔다."""
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_suffix(".tmp.vhdx")
    tmp_out.unlink(missing_ok=True)
    try:
        wsl(
            "set -euo pipefail; work=$(mktemp -d); trap 'rm -rf \"$work\"' EXIT; "
            f"{QEMU_IMG} convert -O raw '{wsl_path(image)}' \"$work/disk.raw\"; "
            f'{QEMU_IMG} resize -f raw "$work/disk.raw" {size_gib}G; '
            f'{QEMU_IMG} convert -O vhdx -o subformat=dynamic "$work/disk.raw" '
            f"'{wsl_path(tmp_out)}'"
        )
    except WslScriptError as exc:  # 클라우드 이미지 변환은 제품 코드가 아니다 — 실행 환경
        raise WslError(f"golden image build failed: {exc}") from exc
    tmp_out.replace(out)
    out.chmod(stat.S_IREAD)  # golden은 읽기 전용(차분 디스크의 부모)
    return out


def ensure_ssh_key(directory: Path) -> Path:
    """e2e 전용 ed25519 키(비밀번호 없음). 공개키 경로를 돌려준다."""
    key = directory / "id_ed25519"
    if not key.exists():
        directory.mkdir(parents=True, exist_ok=True)
        ssh_keygen = shutil.which("ssh-keygen")
        if ssh_keygen is None:
            raise RuntimeError("ssh-keygen not found (Windows OpenSSH client)")
        subprocess.run(
            [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", "pureiron-e2e", "-f", str(key)],
            check=True,
        )
    return key.with_suffix(".pub")


DUMMY_FILES = """\
  - path: /etc/systemd/network/10-e2e-dummy.netdev
    permissions: "0644"
    content: |
      [NetDev]
      Name={name}
      Kind=dummy
  - path: /etc/systemd/network/10-e2e-dummy.network
    permissions: "0644"
    content: |
      [Match]
      Name={name}
      [Network]
      Address={address}/32
"""
"""seed `write_files`에 더하는 dummy 인터페이스(systemd-networkd — 재부팅 뒤에도 남는다)."""
DUMMY_IFACE = "e2edummy0"
DUMMY_RUNCMD = "  - [networkctl, reload]\n"
"""write_files는 networkd가 뜬 뒤에 쓰이므로 첫 부팅에서는 다시 읽게 한다(다음 부팅부터는 기동 때
읽는다)."""


def seed_files(
    hostname: str,
    ip: str,
    ssh_pub: str,
    net: Network,
    field: tuple[Endpoint, ...] = (),
    *,
    dummy: str | None = None,
) -> dict[str, bytes]:
    """`dummy`(IPv4 주소)를 주면 VM 안에 그 주소의 dummy 인터페이스를 둔다(G2 시뮬레이터). 기본은
    없음 — M2d `run.py`의 seed는 바이트까지 그대로다."""
    nft = "".join(f"      {line}\n" for line in nft_audit(net.cidr, field).splitlines())
    user_data = USER_DATA.format(hostname=hostname, ssh_pub=ssh_pub.strip(), nft=nft.rstrip("\n"))
    if dummy is not None:
        address = str(ipaddress.IPv4Address(dummy))
        files = DUMMY_FILES.format(name=DUMMY_IFACE, address=address)
        user_data = user_data.replace("runcmd:\n", files + "runcmd:\n" + DUMMY_RUNCMD, 1)
    network = NETWORK_CONFIG.format(ip=ip, prefix=net.prefix, host_ip=net.host_ip)
    meta = f"instance-id: {hostname}\nlocal-hostname: {hostname}\n"
    return {
        "user-data": user_data.encode(),
        "meta-data": meta.encode(),
        "network-config": network.encode(),
    }


def write_seed_iso(out: Path, files: dict[str, bytes]) -> Path:
    """NoCloud seed(volume label `cidata`, Joliet + Rock Ridge로 소문자·`-` 이름 보존)."""
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="cidata")
    for index, (name, data) in enumerate(sorted(files.items())):
        iso.add_fp(
            io.BytesIO(data),
            len(data),
            f"/FILE{index};1",
            rr_name=name,
            joliet_path=f"/{name}",
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    iso.write(str(out))
    iso.close()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="golden")
    sub = parser.add_subparsers(dest="command", required=True)
    golden = sub.add_parser("golden")
    golden.add_argument("--image", type=Path, required=True)
    golden.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "golden":
        print(build_golden(args.image, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
