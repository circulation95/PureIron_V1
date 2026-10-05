"""e2e VM 재료(PD-24): golden VHDX, cloud-init NoCloud seed ISO, SSH 키.

    uv run python tests/e2e/infra/golden.py golden --image <cloudimg.img> --out <golden.vhdx>

- golden: Ubuntu 24.04 클라우드 이미지(qcow2)를 WSL의 `qemu-img`로 64 GiB 동적 VHDX로 바꾸고
  읽기 전용으로 둔다. 64 GiB인 이유: preflight가 `/var/lib/pureiron` 여유 ≥ 50 GiB를 요구한다
  (동적 디스크라 실제 사용분만 차지). VM은 이 위의 차분 디스크로만 만든다.
- seed ISO(label `cidata`): 사용자 `pureiron`(SSH 키, sudo), 내부 전용 vSwitch 고정 IP,
  OS의 외부 접속 시도(시간 동기·apt·snap·motd)를 끄고, 외부로 나가려는 패킷을 세는 nftables
  테이블 `inet e2e_audit`를 둔다. 기본 경로·DNS는 호스트(전달하지 않음)로 두어 외부 접속 *시도*가
  카운터에 잡히게 한다(경로가 없으면 패킷이 생기지 않아 0이 당연해진다).
"""

from __future__ import annotations

import argparse
import io
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pycdlib

GOLDEN_SIZE_GIB = 64
WSL_DISTRO = "Ubuntu"
QEMU_IMG = "$HOME/m2d-tools/qemu-img"


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


def nft_audit(cidr: str) -> str:
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
runcmd:
  - [systemctl, daemon-reload]
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


def wsl(script: str, *, timeout_s: float = 1800) -> str:
    result = subprocess.run(
        ["wsl", "-d", WSL_DISTRO, "--exec", "bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"wsl failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def build_golden(image: Path, out: Path, size_gib: int = GOLDEN_SIZE_GIB) -> Path:
    """qcow2 → raw(WSL ext4 임시) → 크기 확장 → 동적 VHDX. 이미 있으면 그대로 둔다."""
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_suffix(".tmp.vhdx")
    tmp_out.unlink(missing_ok=True)
    wsl(
        "set -euo pipefail; work=$(mktemp -d); trap 'rm -rf \"$work\"' EXIT; "
        f"{QEMU_IMG} convert -O raw '{wsl_path(image)}' \"$work/disk.raw\"; "
        f'{QEMU_IMG} resize -f raw "$work/disk.raw" {size_gib}G; '
        f"{QEMU_IMG} convert -O vhdx -o subformat=dynamic \"$work/disk.raw\" '{wsl_path(tmp_out)}'"
    )
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


def seed_files(hostname: str, ip: str, ssh_pub: str, net: Network) -> dict[str, bytes]:
    nft = "".join(f"      {line}\n" for line in nft_audit(net.cidr).splitlines())
    user_data = USER_DATA.format(hostname=hostname, ssh_pub=ssh_pub.strip(), nft=nft.rstrip("\n"))
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
