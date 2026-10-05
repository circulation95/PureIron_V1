"""설치 번들(`pureiron-install-<release>.pib`) 빌더(plan M2 설치 번들 B3).

    bash deploy/install/debs/fetch_debs.sh out/debs
    bash deploy/build/build_updater.sh out/updater
    uv run python deploy/bundle/build_install_bundle.py --release 2026.10.0 \\
        --updater-tar out/updater/updater-0.1.0.tar.gz --debs-dir out/debs \\
        --release-key ~/pureiron/keys/test/release.key --cert-dir ~/pureiron/keys/test \\
        --root-pub ~/pureiron/keys/test/test-root.pub --out out/bundles

payload(서명된 `files`, 계약 `install-manifest.schema.json`):
- `bootstrap/bootstrap-verify.sh`, `install/{install.sh,preflight.sh,daemon.json,updater.env}`,
  `systemd/pureiron-updater.service` — 저장소 `deploy/install`·`deploy/systemd`
  (LF만, CR이 있으면 실패)
- `keys/root.pub`(raw 32B), `keys/root.pem`(`keys.export_pem`으로 결정적 생성)
- `updater/updater-<ver>.tar.gz` — `build_updater.sh` 결과, 최상위 `<ver>/`,
  `<ver>/bin/pureiron-site` 포함
- `debs/<패키지>.deb` — `fetch_debs.sh` 결과. `debs.json`의 sha256과 같아야 하고, 패키지 집합은
  `DOCKER_PACKAGES`, 아키텍처 amd64, docker-compose-plugin 버전 ≥ `COMPOSE_MIN`.

쓴 뒤 `verify_install_bundle`(현장 5a와 같은 코드)로 자체 검증한다. 함께 쓰는
`<pib 이름>.runbook.txt`에는 운영자가 종이 runbook에 옮겨 적을 **root 지문**과
**bootstrap-verify.sh sha256**, 스크립트를 믿지 않을 때 손으로 칠 원시 openssl 명령이 들어 있다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tarfile
import tempfile
import time
import tomllib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError
from pureiron_bundle.manifest import DOCKER_PACKAGES, Json, release_version_pattern
from pureiron_bundle.pib import CERT_FILE, CERT_SIG
from pureiron_bundle.sign import file_sha256, write_signed_bundle
from pureiron_bundle.trust import format_timestamp, parse_timestamp
from pureiron_bundle.verify import ROOT_PEM_PAYLOAD, ROOT_PUB_PAYLOAD, verify_install_bundle

ROOT = Path(__file__).resolve().parents[2]
UPDATER_PYPROJECT = ROOT / "services" / "updater" / "pyproject.toml"
COMPOSE_MIN = "2.17.0"
"""Updater가 쓰는 `docker compose up --wait-timeout`이 들어온 버전(compose#10269)."""
DEB_ARCH = "amd64"
_UPDATER_TAR = re.compile(r"updater-([0-9]+\.[0-9]+\.[0-9]+)\.tar\.gz")
_DEB_VERSION = re.compile(r"(?:[0-9]+:)?([0-9]+)\.([0-9]+)\.([0-9]+)")


class BuildError(RuntimeError):
    pass


def script_sources(root: Path = ROOT) -> dict[str, Path]:
    """저장소의 설치 스크립트 → 번들 경로."""
    install = root / "deploy" / "install"
    return {
        "bootstrap/bootstrap-verify.sh": install / "bootstrap-verify.sh",
        "install/daemon.json": install / "daemon.json",
        "install/install.sh": install / "install.sh",
        "install/preflight.sh": install / "preflight.sh",
        "install/updater.env": install / "updater.env",
        "systemd/pureiron-updater.service": root
        / "deploy"
        / "systemd"
        / "pureiron-updater.service",
    }


def check_lf(sources: Mapping[str, Path]) -> None:
    """현장 bash·systemd가 읽는 파일에 CR이 섞이면(Windows 체크아웃) 설치가 깨진다."""
    for name, path in sources.items():
        if b"\r" in path.read_bytes():
            raise BuildError(f"{path} ({name}) contains CR line endings — must be LF only")


def deb_upstream_version(version: str) -> tuple[int, int, int]:
    """deb 버전(`[epoch:]X.Y.Z-…`)의 upstream X.Y.Z."""
    match = _DEB_VERSION.match(version)
    if match is None:
        raise BuildError(f"cannot read an X.Y.Z version from deb version {version!r}")
    return int(match[1]), int(match[2]), int(match[3])


def load_debs(directory: Path) -> tuple[dict[str, Path], dict[str, str]]:
    """`debs.json` + `<패키지>.deb` → (payload, 패키지 → 버전).

    패키지 집합·sha256·arch·compose 최소 버전을 검사한다.
    """
    try:
        listed = cast(
            dict[str, dict[str, str]],
            json.loads((directory / "debs.json").read_text(encoding="utf-8")),
        )
    except (OSError, ValueError) as exc:
        raise BuildError(f"{directory / 'debs.json'}: {exc}") from exc
    if set(listed) != set(DOCKER_PACKAGES):
        raise BuildError(f"debs.json packages {sorted(listed)} != {sorted(DOCKER_PACKAGES)}")
    payload: dict[str, Path] = {}
    versions: dict[str, str] = {}
    for package, info in sorted(listed.items()):
        path = directory / f"{package}.deb"
        if not path.is_file():
            raise BuildError(f"{path} missing (fetch_debs.sh)")
        if info.get("arch") != DEB_ARCH:
            raise BuildError(f"{package}: arch {info.get('arch')!r} != {DEB_ARCH}")
        actual = file_sha256(path)
        if actual != info.get("sha256"):
            raise BuildError(f"{package}.deb: sha256 {actual} != debs.json {info.get('sha256')}")
        payload[f"debs/{package}.deb"] = path
        versions[package] = info["version"]
    compose = deb_upstream_version(versions["docker-compose-plugin"])
    minimum = tuple(int(part) for part in COMPOSE_MIN.split("."))
    if compose < minimum:
        raise BuildError(
            f"docker-compose-plugin {versions['docker-compose-plugin']} < required {COMPOSE_MIN}"
        )
    return payload, versions


def default_updater_version() -> str:
    data = tomllib.loads(UPDATER_PYPROJECT.read_text(encoding="utf-8"))
    return str(cast(dict[str, object], data["project"])["version"])


def check_updater_tar(path: Path, version: str) -> None:
    match = _UPDATER_TAR.fullmatch(path.name)
    if match is None or match[1] != version:
        raise BuildError(f"{path.name}: expected updater-{version}.tar.gz")
    try:
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
    except (OSError, tarfile.TarError) as exc:
        raise BuildError(f"{path}: {exc}") from exc
    outside = [n for n in names if n != version and not n.startswith(f"{version}/")]
    if outside:
        raise BuildError(f"{path.name}: entries outside {version}/: {outside[:5]}")
    if f"{version}/bin/pureiron-site" not in names:
        raise BuildError(f"{path.name}: {version}/bin/pureiron-site missing")


def runbook(pib: Path, root_pub: bytes, bootstrap_sha256: str) -> str:
    fingerprint = keys.fingerprint(root_pub)
    name = pib.name
    return "\n".join(
        [
            f"PureIron 설치 번들 runbook — {name}",
            "",
            f"root 지문(sha256 of keys/root.pub): {fingerprint}",
            f"bootstrap/bootstrap-verify.sh sha256: {bootstrap_sha256.removeprefix('sha256:')}",
            "",
            f"0) BUNDLE=/root/{name}   # 번들을 복사한 절대 경로",
            "1) 메타데이터 풀기 — 전용 빈 디렉터리에서, 이름을 지정한 파일만",
            "   install -d -m 700 /root/pib-meta && cd /root/pib-meta",
            '   tar --force-local --no-same-owner --no-same-permissions -xf "$BUNDLE" \\',
            "       manifest.json manifest.sig release-cert.json release-cert.sig \\",
            "       keys/root.pub keys/root.pem bootstrap/bootstrap-verify.sh",
            "2) 스크립트 대조(위 sha256과 같아야 실행)",
            "   sha256sum bootstrap/bootstrap-verify.sh",
            "3) 검증·추출",
            f"   sudo bash bootstrap/bootstrap-verify.sh --root-fpr {fingerprint} "
            '--bundle "$BUNDLE" --dest /root/pureiron-install',
            "4) 설치",
            '   sudo bash /root/pureiron-install/install/install.sh --bundle "$BUNDLE"',
            "",
            "부록 — 스크립트를 믿지 않을 때 원시 명령",
            "   (root 지문을 먼저 sha256sum keys/root.pub로 대조):",
            "   openssl pkeyutl -verify -pubin -inkey keys/root.pem -rawin "
            "-in release-cert.json -sigfile release-cert.sig",
            "   (release-cert.json의 release_pubkey hex 앞에 302a300506032b6570032100을 붙여 "
            "base64 → release.pem)",
            "   openssl pkeyutl -verify -pubin -inkey release.pem -rawin "
            "-in manifest.json -sigfile manifest.sig",
            "",
        ]
    )


def build(args: argparse.Namespace) -> int:
    started = time.monotonic()
    release: str = args.release
    if not release_version_pattern().fullmatch(release):
        raise BuildError(f"--release {release!r} is not YYYY.M.N")
    root_pub = keys.read_public_key(args.root_pub)
    version: str = args.updater_version or default_updater_version()
    updater_tar: Path = args.updater_tar
    check_updater_tar(updater_tar, version)
    debs, deb_versions = load_debs(args.debs_dir)
    sources = script_sources()
    check_lf(sources)

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    pib = out / f"pureiron-install-{release}.pib"
    with tempfile.TemporaryDirectory(dir=out, prefix=".staging-") as tmp:
        root_pem = Path(tmp) / "root.pem"
        root_pem.write_text(keys.export_pem(root_pub), encoding="ascii", newline="\n")
        payload: dict[str, Path] = {
            **sources,
            **debs,
            ROOT_PUB_PAYLOAD: args.root_pub,
            ROOT_PEM_PAYLOAD: root_pem,
            f"updater/{updater_tar.name}": updater_tar,
        }
        created_at = parse_timestamp(args.created_at) if args.created_at else datetime.now(UTC)
        document: dict[str, Json] = {
            "format": 1,
            "kind": "install",
            "release": release,
            "created_at": format_timestamp(created_at),
            "updater": {"version": version, "file": f"updater/{updater_tar.name}"},
            "docker": {"packages": dict(deb_versions), "compose_min": COMPOSE_MIN},
            "trust_update": None,
        }
        cert_dir: Path = args.cert_dir
        write_signed_bundle(
            pib,
            document,
            payload,
            release_seed=keys.read_private_key(args.release_key),
            cert_json=(cert_dir / CERT_FILE).read_bytes(),
            cert_sig=(cert_dir / CERT_SIG).read_bytes(),
        )
    try:
        verify_install_bundle(pib, root_pub)
    except BaseException:
        pib.unlink(missing_ok=True)
        raise
    bootstrap_sha = file_sha256(sources["bootstrap/bootstrap-verify.sh"])
    text = runbook(pib, root_pub, bootstrap_sha)
    pib.with_name(pib.stem + ".runbook.txt").write_text(text, encoding="utf-8", newline="\n")
    metrics = {"size_bytes": pib.stat().st_size, "build_s": round(time.monotonic() - started, 3)}
    print(json.dumps(metrics, sort_keys=True))
    print(pib)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="build_install_bundle")
    p.add_argument("--release", required=True, help="제품 release 버전(YYYY.M.N)")
    p.add_argument("--updater-tar", type=Path, required=True, help="build_updater.sh 결과")
    p.add_argument("--debs-dir", type=Path, required=True, help="fetch_debs.sh 결과(debs.json)")
    p.add_argument("--release-key", type=Path, required=True, help="release key raw 32B seed")
    p.add_argument("--cert-dir", type=Path, required=True, help="release-cert.json/.sig 위치")
    p.add_argument("--root-pub", type=Path, required=True, help="현장에 심을 root 공개키(raw)")
    p.add_argument("--out", type=Path, required=True, help="출력 디렉터리")
    p.add_argument("--updater-version", default=None, help="기본: services/updater 버전")
    p.add_argument("--created-at", default=None, help="RFC 3339(기본: 지금, 테스트용)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return build(args)
    except (BuildError, BundleRejectedError, ValueError, OSError) as exc:
        print(f"::error::build_install_bundle: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
