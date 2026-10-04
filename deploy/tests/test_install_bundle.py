"""설치 번들: 빌더(build_install_bundle) → 부트스트랩(bootstrap-verify.sh) → preflight → install.sh.

셸 스크립트는 실제 bash로 돌리고, 호스트 명령(dpkg·systemctl·docker·ss·timedatectl…)은 PATH 앞의
스텁으로 대신한다. install.sh의 5a 재검증은 venv tarball 안의 스텁 `pureiron-site`가 이 저장소의
실제 Updater CLI를 실행하므로 bundle-tools 검증 경로를 그대로 탄다. 실제 호스트 설치는 M2d(VM).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from build_install_bundle import (
    BuildError,
    check_lf,
    deb_upstream_version,
    default_updater_version,
    load_debs,
    main,
    script_sources,
)
from images import bash_executable

from pureiron_bundle import keys
from pureiron_bundle.manifest import DOCKER_PACKAGES
from pureiron_bundle.pib import CERT_FILE, CERT_SIG
from pureiron_bundle.trust import issue_cert
from pureiron_bundle.verify import verify_install_bundle

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable).as_posix()
COMPOSE_DEB = "2.29.1-1~ubuntu.24.04~noble"
METADATA = ("manifest.json", "manifest.sig", "release-cert.json", "release-cert.sig")


@dataclass
class Built:
    pib: Path
    root_pub: bytes
    tmp: Path

    @property
    def fingerprint(self) -> str:
        return keys.fingerprint(self.root_pub)


def write_debs(directory: Path, compose_version: str = COMPOSE_DEB) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    listed: dict[str, dict[str, str]] = {}
    for package in DOCKER_PACKAGES:
        data = f"deb {package}".encode() * 50
        (directory / f"{package}.deb").write_bytes(data)
        version = compose_version if package == "docker-compose-plugin" else "5:28.3.3-1"
        listed[package] = {
            "version": version,
            "arch": "amd64",
            "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
        }
    (directory / "debs.json").write_text(json.dumps(listed), encoding="utf-8")


def write_updater_tar(directory: Path, version: str) -> Path:
    """`<ver>/bin/pureiron-site` = 이 저장소의 Updater CLI를 실행하는 스텁."""
    script = f'#!/usr/bin/env bash\nexec "{PYTHON}" -m pureiron_updater.cli "$@"\n'.encode()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"updater-{version}.tar.gz"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data, mode in (
            (version, None, 0o755),
            (f"{version}/bin", None, 0o755),
            (f"{version}/bin/pureiron-site", script, 0o755),
        ):
            info = tarfile.TarInfo(name)
            info.mode = mode
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    return path


def build_bundle(
    tmp: Path, *, compose_version: str = COMPOSE_DEB, updater_version: str | None = None
) -> Built:
    keydir = tmp / "keys"
    keydir.mkdir(parents=True)
    root_seed, release_seed = bytes(range(32)), bytes(range(32, 64))
    keys.write_private_key(keydir / "release.key", release_seed)
    root_pub = keys.public_key(root_seed)
    keys.write_public_key(keydir / "root.pub", root_pub)
    cert_json, cert_sig = issue_cert(
        root_seed, keys.public_key(release_seed), 1, datetime(2026, 9, 1, tzinfo=UTC)
    )
    (keydir / CERT_FILE).write_bytes(cert_json)
    (keydir / CERT_SIG).write_bytes(cert_sig)
    write_debs(tmp / "debs", compose_version)
    version = updater_version or default_updater_version()
    updater = write_updater_tar(tmp / "updater", version)
    code = main(
        [
            *("--updater-version", version),
            *("--release", "2026.10.0"),
            *("--updater-tar", str(updater)),
            *("--debs-dir", str(tmp / "debs")),
            *("--release-key", str(keydir / "release.key")),
            *("--cert-dir", str(keydir)),
            *("--root-pub", str(keydir / "root.pub")),
            *("--out", str(tmp / "out")),
            *("--created-at", "2026-10-05T01:00:00Z"),
        ]
    )
    assert code == 0
    return Built(tmp / "out" / "pureiron-install-2026.10.0.pib", root_pub, tmp)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Built:
    return build_bundle(tmp_path_factory.mktemp("install"))


def bash(
    script: Path, *args: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [bash_executable(), str(script), *args],
        cwd=cwd,
        env={**os.environ, "PUREIRON_PYTHON": PYTHON, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def operator_extract(pib: Path, work: Path) -> None:
    """runbook 1단계: 메타데이터·bootstrap/·keys/만 푼다."""
    work.mkdir(parents=True)
    with tarfile.open(pib) as tar:
        for member in tar.getmembers():
            name = member.name
            if name in METADATA or name.startswith(("bootstrap/", "keys/")):
                tar.extract(member, work, filter="data")


def bootstrap(
    built: Built, work: Path, *, fpr: str | None = None, pib: Path | None = None
) -> subprocess.CompletedProcess[str]:
    operator_extract(built.pib, work)
    return bash(
        work / "bootstrap" / "bootstrap-verify.sh",
        *("--root-fpr", fpr or built.fingerprint),
        *("--bundle", (pib or built.pib).as_posix()),
        *("--dest", (work / "dest").as_posix()),
        cwd=work,
    )


def tampered_copy(built: Built, target: Path, package: str = "docker-ce") -> Path:
    data = bytearray(built.pib.read_bytes())
    marker = f"deb {package}".encode()
    data[data.index(marker)] ^= 1
    target.write_bytes(bytes(data))
    return target


# ── 빌더 ─────────────────────────────────────────────────────────────────────


def test_builder_writes_verified_bundle_and_runbook(built: Built) -> None:
    result = verify_install_bundle(built.pib, built.root_pub)
    manifest = result.manifest
    assert manifest.docker_packages["docker-compose-plugin"] == COMPOSE_DEB
    assert manifest.compose_min == "2.17.0"
    assert manifest.updater_file == f"updater/updater-{default_updater_version()}.tar.gz"
    runbook = built.pib.with_name("pureiron-install-2026.10.0.runbook.txt").read_text("utf-8")
    script = (ROOT / "deploy" / "install" / "bootstrap-verify.sh").read_bytes()
    assert built.fingerprint in runbook
    assert hashlib.sha256(script).hexdigest() in runbook


def test_builder_is_deterministic(built: Built, tmp_path: Path) -> None:
    again = build_bundle(tmp_path)
    assert again.pib.read_bytes() == built.pib.read_bytes()


def test_old_compose_plugin_is_refused(tmp_path: Path) -> None:
    write_debs(tmp_path / "debs", "2.16.0-1~ubuntu.24.04~noble")
    with pytest.raises(BuildError, match=r"2\.17\.0"):
        load_debs(tmp_path / "debs")


def test_deb_checks(tmp_path: Path) -> None:
    write_debs(tmp_path / "debs")
    (tmp_path / "debs" / "docker-ce.deb").write_bytes(b"other")
    with pytest.raises(BuildError, match="sha256"):
        load_debs(tmp_path / "debs")
    assert deb_upstream_version("5:28.3.3-1~ubuntu.24.04~noble") == (28, 3, 3)
    with pytest.raises(BuildError):
        deb_upstream_version("unknown")


def test_scripts_are_lf_only(tmp_path: Path) -> None:
    check_lf(script_sources())
    bad = tmp_path / "x.sh"
    bad.write_bytes(b"echo hi\r\n")
    with pytest.raises(BuildError, match="CR"):
        check_lf({"install/x.sh": bad})


# ── bootstrap-verify.sh ──────────────────────────────────────────────────────


def test_bootstrap_verify_extracts_signed_files(built: Built, tmp_path: Path) -> None:
    run = bootstrap(built, tmp_path / "w")
    assert run.returncode == 0, run.stderr + run.stdout
    dest = tmp_path / "w" / "dest"
    assert (dest / "debs" / "docker-ce.deb").is_file()
    assert (dest / "install" / "install.sh").is_file()
    assert (dest / "manifest.json").is_file()


@pytest.mark.parametrize("case", ["fingerprint", "payload", "manifest", "cert"])
def test_bootstrap_verify_rejects(built: Built, tmp_path: Path, case: str) -> None:
    work = tmp_path / "w"
    if case == "fingerprint":
        run = bootstrap(built, work, fpr="0" * 64)
    elif case == "payload":
        run = bootstrap(built, work, pib=tampered_copy(built, tmp_path / "bad.pib"))
    else:
        operator_extract(built.pib, work)
        name = "manifest.json" if case == "manifest" else "release-cert.json"
        target = work / name
        target.write_bytes(target.read_bytes().replace(b'"format":1', b'"format": 1'))
        run = bash(
            work / "bootstrap" / "bootstrap-verify.sh",
            *("--root-fpr", built.fingerprint, "--bundle", built.pib.as_posix()),
            *("--dest", (work / "dest").as_posix()),
            cwd=work,
        )
    assert run.returncode == 1, run.stdout
    assert "FAIL" in run.stderr


def test_bootstrap_refuses_planted_files_before_running_python(
    built: Built, tmp_path: Path
) -> None:
    """보안 리뷰 C-1: 검증 전 추출물에 섞인 모듈(json.py 등)이 있으면 python 전에 멈춘다."""
    work = tmp_path / "w"
    operator_extract(built.pib, work)
    marker = tmp_path / "pwned"
    (work / "json.py").write_text(
        f"open({marker.as_posix()!r}, 'w').write('x')\n", encoding="utf-8"
    )
    run = bash(
        work / "bootstrap" / "bootstrap-verify.sh",
        *("--root-fpr", built.fingerprint, "--bundle", built.pib.as_posix()),
        *("--dest", (work / "dest").as_posix()),
        cwd=work,
    )
    assert run.returncode == 1
    assert "must contain only" in run.stderr
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink 생성에 관리자 권한이 필요(Windows)")
def test_bootstrap_refuses_symlinks(built: Built, tmp_path: Path) -> None:
    work = tmp_path / "w"
    operator_extract(built.pib, work)
    (work / "bootstrap" / "evil").symlink_to("..")
    run = bash(
        work / "bootstrap" / "bootstrap-verify.sh",
        *("--root-fpr", built.fingerprint, "--bundle", built.pib.as_posix()),
        *("--dest", (work / "dest").as_posix()),
        cwd=work,
    )
    assert run.returncode == 1
    assert "symbolic links" in run.stderr


# ── 호스트 스텁 ──────────────────────────────────────────────────────────────


@dataclass
class Host:
    bin: Path
    log: Path
    env: dict[str, str]


STUBS = {
    "uname": "echo x86_64",
    "df": 'echo "Avail"; echo "${STUB_DF_AVAIL:-100000000000000}"',
    "ss": 'if [ -n "${STUB_SS_LINE:-}" ]; then echo "$STUB_SS_LINE"; fi',
    "timedatectl": 'echo "${STUB_NTP:-yes}"',
    "dpkg-query": (
        'case "${@: -1}" in iptables|libseccomp2) echo "install ok installed" ;;'
        ' ${STUB_CONFLICT:-none}) echo "install ok installed" ;;'
        ' ${STUB_PREINSTALLED:-none}) echo "install ok installed ${STUB_PREINSTALLED_VERSION:-}" ;;'
        " *) exit 1 ;; esac"
    ),
    "dpkg": 'echo "dpkg $*" >> "$STUB_LOG"',
    "systemctl": (
        'echo "systemctl $*" >> "$STUB_LOG"; case "$1" in is-enabled|is-active) exit 1 ;; esac'
    ),
    "docker": (
        'case "$1 $2" in "info --format") echo "${STUB_DRIVER:-overlay2}" ;;'
        ' "compose version") echo "v${STUB_COMPOSE:-2.29.1}" ;; esac'
    ),
}


def make_host(tmp: Path) -> Host:
    stub_bin = tmp / "stub-bin"
    stub_bin.mkdir(parents=True)
    for name, body in STUBS.items():
        path = stub_bin / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8", newline="\n")
        path.chmod(0o755)
    python = stub_bin / "python3.12"
    python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
    python.chmod(0o755)
    os_release = tmp / "os-release"
    os_release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n', encoding="utf-8")
    log = tmp / "stub.log"
    log.touch()
    env = {
        "PATH": str(stub_bin) + os.pathsep + os.environ["PATH"],
        "PREFLIGHT_OS_RELEASE": os_release.as_posix(),
        "PREFLIGHT_PYTHON": python.as_posix(),
        "STUB_LOG": log.as_posix(),
    }
    return Host(stub_bin, log, env)


PREFLIGHT = ROOT / "deploy" / "install" / "preflight.sh"


@pytest.mark.parametrize(
    ("stub", "args", "ok"),
    [
        ({}, [], True),
        ({"STUB_NTP": "no"}, [], False),
        ({"STUB_NTP": "no"}, ["--offline-time-confirmed"], True),
        ({"STUB_SS_LINE": "LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*"}, [], False),
        ({"STUB_DF_AVAIL": "1000"}, [], False),
        ({"STUB_CONFLICT": "docker.io"}, [], False),
    ],
)
def test_preflight(tmp_path: Path, stub: dict[str, str], args: list[str], ok: bool) -> None:
    host = make_host(tmp_path)
    env = {**host.env, **stub}
    run = bash(PREFLIGHT, "--root", (tmp_path / "var").as_posix(), *args, cwd=tmp_path, env=env)
    assert (run.returncode == 0) is ok, run.stdout + run.stderr
    assert ("preflight: OK" in run.stdout) is ok


# ── install.sh ───────────────────────────────────────────────────────────────


@pytest.fixture
def staged(built: Built, tmp_path: Path) -> tuple[Path, Host, Path]:
    """bootstrap까지 마친 추출 디렉터리, 호스트 스텁, 설치 prefix."""
    run = bootstrap(built, tmp_path / "w")
    assert run.returncode == 0, run.stderr
    host = make_host(tmp_path)
    prefix = tmp_path / "root"
    host.env["PUREIRON_PREFIX"] = prefix.as_posix()
    return tmp_path / "w" / "dest", host, prefix


def install(
    dest: Path, host: Host, pib: Path, *extra: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return bash(
        dest / "install" / "install.sh",
        "--bundle",
        pib.as_posix(),
        *extra,
        cwd=dest,
        env={**host.env, **(env or {})},
    )


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh는 symlink가 필요(CI·VM: Linux)")
def test_install_sets_up_host_and_reverifies(built: Built, staged: tuple[Path, Host, Path]) -> None:
    dest, host, prefix = staged
    run = install(dest, host, built.pib)
    assert run.returncode == 0, run.stdout + run.stderr
    version = default_updater_version()
    assert (prefix / "etc/pureiron/root.pub").read_bytes() == built.root_pub
    assert (prefix / "etc/docker/daemon.json").read_bytes() == (
        ROOT / "deploy/install/daemon.json"
    ).read_bytes()
    assert os.readlink(prefix / "opt/pureiron/updater/current") == version
    assert (prefix / "etc/systemd/system/pureiron-updater.service").is_file()
    assert (prefix / "etc/pureiron/updater.env").is_file()
    secrets = prefix / "var/lib/pureiron/state/secrets"
    assert sorted(p.name for p in secrets.iterdir()) == ["db_password", "updater_token"]
    log = host.log.read_text(encoding="utf-8")
    assert "dpkg -i" in log
    assert "systemctl enable --now docker" in log
    assert "systemctl enable pureiron-updater" in log
    assert "dpkg -r" not in log
    assert "install: OK" in run.stdout
    # 같은 번들로 다시 돌려도(멱등) 성공하고 secret은 바뀌지 않는다.
    before = (secrets / "db_password").read_bytes()
    assert install(dest, host, built.pib).returncode == 0
    assert (secrets / "db_password").read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh는 symlink가 필요(CI·VM: Linux)")
@pytest.mark.parametrize("case", ["reverify", "compose", "store"])
def test_install_failure_rolls_back(
    built: Built, staged: tuple[Path, Host, Path], tmp_path: Path, case: str
) -> None:
    dest, host, prefix = staged
    pib, env = built.pib, {}
    if case == "reverify":
        pib = tampered_copy(built, tmp_path / "tampered.pib", "containerd.io")
    elif case == "compose":
        env = {"STUB_COMPOSE": "2.16.0"}
    else:
        env = {"STUB_DRIVER": "overlayfs"}
    run = install(dest, host, pib, env=env)
    assert run.returncode != 0, run.stdout
    assert "rolled back" in run.stderr
    log = host.log.read_text(encoding="utf-8")
    assert "dpkg -r docker-compose-plugin docker-ce docker-ce-cli containerd.io" in log
    assert "systemctl disable --now docker" in log
    for leftover in (
        "etc/pureiron/root.pub",
        "etc/docker/daemon.json",
        "opt/pureiron/updater/current",
        f"opt/pureiron/updater/{default_updater_version()}",
        "etc/systemd/system/pureiron-updater.service",
        "usr/local/bin/pureiron-site",
    ):
        assert not os.path.lexists(prefix / leftover), leftover


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh는 symlink가 필요(CI·VM: Linux)")
def test_install_refuses_a_different_trust_root(
    built: Built, staged: tuple[Path, Host, Path]
) -> None:
    dest, host, prefix = staged
    planted = prefix / "etc/pureiron/root.pub"
    planted.parent.mkdir(parents=True)
    planted.write_bytes(bytes(32))
    run = install(dest, host, built.pib)
    assert run.returncode != 0
    assert planted.read_bytes() == bytes(32)
    shutil.rmtree(prefix / "etc/pureiron")


LINUX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="install.sh는 symlink가 필요(CI·VM: Linux)"
)


@LINUX_ONLY
def test_preinstalled_docker_of_the_same_version_is_kept_on_rollback(
    built: Built, staged: tuple[Path, Host, Path]
) -> None:
    """코드 리뷰 M-4: 이번 실행 전부터 있던 패키지는 되돌릴 때 지우지 않는다."""
    dest, host, prefix = staged
    daemon = prefix / "etc/docker/daemon.json"
    daemon.parent.mkdir(parents=True)
    daemon.write_bytes((ROOT / "deploy/install/daemon.json").read_bytes())
    env = {
        "STUB_PREINSTALLED": "docker-ce",
        "STUB_PREINSTALLED_VERSION": "5:28.3.3-1",
        "STUB_COMPOSE": "2.16.0",
    }
    run = install(dest, host, built.pib, env=env)
    assert run.returncode != 0
    log = host.log.read_text(encoding="utf-8")
    assert "dpkg -r docker-compose-plugin docker-ce-cli containerd.io" in log
    assert "systemctl disable --now docker" not in log


@LINUX_ONLY
def test_preinstalled_docker_of_another_version_stops_before_changes(
    built: Built, staged: tuple[Path, Host, Path]
) -> None:
    dest, host, prefix = staged
    env = {"STUB_PREINSTALLED": "docker-ce", "STUB_PREINSTALLED_VERSION": "5:27.0.0-1"}
    run = install(dest, host, built.pib, env=env)
    assert run.returncode != 0
    assert "already installed" in run.stderr
    assert "dpkg" not in host.log.read_text(encoding="utf-8")
    assert not (prefix / "etc/docker/daemon.json").exists()


def stage_bundle(bundle: Built, work: Path) -> Path:
    run = bootstrap(bundle, work)
    assert run.returncode == 0, run.stderr
    return work / "dest"


@LINUX_ONLY
@pytest.mark.parametrize("fails", [False, True])
def test_updater_only_replaces_venv_and_unit_or_restores(
    built: Built, staged: tuple[Path, Host, Path], tmp_path: Path, fails: bool
) -> None:
    """코드 리뷰 M-5: Updater 교체는 새 unit으로 바꾸고, 재검증 실패 시 이전 current·unit 복원."""
    dest, host, prefix = staged
    assert install(dest, host, built.pib).returncode == 0
    old = default_updater_version()
    unit = prefix / "etc/systemd/system/pureiron-updater.service"
    unit.write_text(unit.read_text(encoding="utf-8") + "# old release\n", encoding="utf-8")
    old_unit = unit.read_bytes()
    newer = build_bundle(tmp_path / "v2", updater_version="9.9.9")
    new_dest = stage_bundle(newer, tmp_path / "w2")
    pib = tampered_copy(newer, tmp_path / "bad2.pib", "containerd.io") if fails else newer.pib
    run = install(new_dest, host, pib, "--updater-only")
    current = prefix / "opt/pureiron/updater/current"
    if fails:
        assert run.returncode != 0
        assert os.readlink(current) == old
        assert not (prefix / "opt/pureiron/updater/9.9.9").exists()
        assert unit.read_bytes() == old_unit
    else:
        assert run.returncode == 0, run.stdout + run.stderr
        assert os.readlink(current) == "9.9.9"
        assert unit.read_bytes() == (ROOT / "deploy/systemd/pureiron-updater.service").read_bytes()
    assert "dpkg -r" not in host.log.read_text(encoding="utf-8")


@LINUX_ONLY
def test_bootstrap_refuses_a_writable_metadata_directory(built: Built, tmp_path: Path) -> None:
    """보안 재검토 M-1: 남이 쓸 수 있는 디렉터리는 검증과 사용 사이에 바꿔치기될 수 있다."""
    work = tmp_path / "w"
    operator_extract(built.pib, work)
    (work / "manifest.json").chmod(0o666)
    run = bash(
        work / "bootstrap" / "bootstrap-verify.sh",
        *("--root-fpr", built.fingerprint, "--bundle", built.pib.as_posix()),
        *("--dest", (work.parent / "dest").as_posix()),
        cwd=work,
    )
    assert run.returncode == 1
    assert "not group/world-writable" in run.stderr


@LINUX_ONLY
def test_preinstalled_docker_without_classic_store_config_stops(
    built: Built, staged: tuple[Path, Host, Path]
) -> None:
    """재검토 LOW: 기존 Docker에 daemon.json을 새로 두면 저장소가 바뀐다 — 바꾸기 전에 멈춤."""
    dest, host, prefix = staged
    env = {"STUB_PREINSTALLED": "docker-ce", "STUB_PREINSTALLED_VERSION": "5:28.3.3-1"}
    run = install(dest, host, built.pib, env=env)
    assert run.returncode != 0
    assert "PD-19" in run.stderr
    assert "dpkg" not in host.log.read_text(encoding="utf-8")
    assert not (prefix / "etc/docker/daemon.json").exists()
