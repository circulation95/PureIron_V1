"""publish 호스트 동기화(M6-4a A2·D11) — WSL 쪽 `publish_host.py`를 tmp 경로로만 돌린다(실제
`/etc/pureiron`·`/srv/pureiron`·`update-ca-certificates`·`gh secret set`은 부르지 않는다). Windows
쪽 `fleet_session.sync_publish_host`는 가짜 root WSL·GitHub로 본다."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fleet_fakes import FakeGitHub, FakeRemote, repo
from g2_fakes import session
from harness import fleet, fleet_session, publish_host
from harness.context import InfraError
from harness.golden import WslScriptError


def pem(tmp_path: Path) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "e2e root")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "central-root.crt"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


class Cmd:
    """`run_cmd` 가짜 — 명령과 stdin을 남기고 실제로는 tmp 안 파일 조작만 흉내 낸다."""

    def __init__(self, rc: int = 0) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.rc = rc

    def __call__(
        self, cmd: Sequence[str], *, stdin: str | None = None, timeout_s: float = 0
    ) -> subprocess.CompletedProcess[str]:
        args = list(cmd)
        self.calls.append((args, stdin))
        if args[0] == "install" and self.rc == 0:
            Path(args[-1]).write_text(Path(args[-2]).read_text() if args[-2] != "/dev/null" else "")
            Path(args[-1]).chmod(int(args[args.index("-m") + 1], 8))
        if args[:2] == ["sh", "-c"] and self.rc == 0:
            Path(args[-1]).write_text(stdin or "")
        return subprocess.CompletedProcess[str](args, self.rc, "", "denied" if self.rc else "")


# ---------------------------------------------------------------- trust


def test_trust_checks_the_fingerprint_then_installs_into_the_os_store(tmp_path: Path) -> None:
    ca = pem(tmp_path)
    want = publish_host.ca_fingerprint(ca.read_text("ascii"))
    cmd, probed = Cmd(), list[str]()

    def probe(url: str) -> int:
        probed.append(url)
        return 200

    done = publish_host.trust(
        ca,
        want.lower(),
        target=tmp_path / "store.crt",
        check_url="https://10.77.0.1:28443/api/v1/healthz",
        run=cmd,
        probe=probe,
    )
    assert [c[0][0] for c in cmd.calls] == ["install", "update-ca-certificates"]
    assert (tmp_path / "store.crt").read_text() == ca.read_text()
    assert done["healthz"] == 200
    assert probed == ["https://10.77.0.1:28443/api/v1/healthz"]


def test_trust_refuses_another_ca(tmp_path: Path) -> None:
    cmd = Cmd()
    with pytest.raises(publish_host.HostError, match="fingerprint"):
        publish_host.trust(pem(tmp_path), "00:11", target=tmp_path / "x", run=cmd)
    assert cmd.calls == []


def test_trust_fails_when_the_os_store_does_not_verify(tmp_path: Path) -> None:
    ca = pem(tmp_path)
    want = publish_host.ca_fingerprint(ca.read_text("ascii"))
    with pytest.raises(publish_host.HostError, match="OS trust store"):
        publish_host.trust(
            ca,
            want,
            target=tmp_path / "x",
            update=False,
            check_url="u",
            run=Cmd(),
            probe=lambda u: 0,
        )


# ---------------------------------------------------------------- tokens


def test_tokens_are_created_0600_first_and_written_through_stdin(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "ci_token").write_text("ci-SECRET\n")
    (secrets / "publish_viewer_token").write_text("viewer-SECRET\n")
    dest = tmp_path / "etc"
    dest.mkdir()
    cmd = Cmd()
    done = publish_host.tokens(secrets, dest=dest, owner="pureiron-publish", run=cmd)
    assert (dest / "ci_token").read_text() == "ci-SECRET"
    assert (dest / "viewer_token").read_text() == "viewer-SECRET"
    if os.name != "nt":
        assert stat.S_IMODE((dest / "ci_token").stat().st_mode) == 0o600
    creates = [c for c in cmd.calls if c[0][0] == "install"]
    assert creates[0][0] == [
        *("install", "-m", "0600", "-o", "pureiron-publish", "-g", "pureiron-publish"),
        *("/dev/null", str(dest / "ci_token")),
    ]
    argv = " ".join(" ".join(c[0]) for c in cmd.calls)
    assert "SECRET" not in argv
    assert [c[1] for c in cmd.calls if c[0][0] == "sh"] == ["ci-SECRET", "viewer-SECRET"]
    assert "SECRET" not in str(done)


def test_an_empty_token_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "ci_token").write_text("\n")
    with pytest.raises(publish_host.HostError, match="empty"):
        publish_host.tokens(tmp_path, dest=tmp_path, run=Cmd())


# ---------------------------------------------------------------- images


def nightly_store(tmp_path: Path) -> Path:
    store = tmp_path / "work" / "store"
    real = tmp_path / "real" / "r5"
    real.mkdir(parents=True)
    (real / "site-db.tar").write_bytes(b"db")
    for name in ("n1a2b3c4", "n1a2b3c4h", "n1a2b3c4m"):
        (store / name).mkdir(parents=True)
        for svc in ("core", "web"):
            tar = store / name / f"{svc}.tar"
            tar.write_bytes(f"{name}-{svc}".encode())
            tar.chmod(0o600)
        if os.name != "nt":
            (store / name / "site-db.tar").symlink_to(real / "site-db.tar")
    return store


def test_images_are_hardlinked_readable_and_old_nightlies_pruned(tmp_path: Path) -> None:
    store, dest = nightly_store(tmp_path), tmp_path / "srv"
    for old in ("r5", "nffffff1", "nffffff1h", "neeeeee2", "nddddd03", "nddddd03m"):
        (dest / old).mkdir(parents=True)
        os.utime(dest / old, (1, 1 if old.startswith("nd") else 2))
    names = ["n1a2b3c4", "n1a2b3c4h", "n1a2b3c4m"]
    done = publish_host.link_images(store, names, dest, keep=3)
    for name in names:
        core = dest / name / "core.tar"
        assert core.read_bytes() == f"{name}-core".encode()
        assert os.path.samefile(core, store / name / "core.tar")  # 같은 ext4 — 복사 아님
        if os.name != "nt":
            assert stat.S_IMODE(core.stat().st_mode) == 0o644
            assert (dest / name / "site-db.tar").read_bytes() == b"db"
    assert done["pruned"] == ["nddddd03", "nddddd03m"]  # 가장 오래된 묶음 — NEXT + 2묶음이 남는다
    assert (dest / "r5").is_dir()  # release는 건드리지 않는다
    again = publish_host.link_images(store, names, dest, keep=3)
    assert set(linked(again)["n1a2b3c4"].values()) == {"same"}


def linked(done: dict[str, object]) -> dict[str, dict[str, str]]:
    return cast(dict[str, dict[str, str]], done["linked"])


def test_images_fall_back_to_copy_across_filesystems(tmp_path: Path) -> None:
    store, dest = nightly_store(tmp_path), tmp_path / "srv"

    def cross_device(src: Path, dst: Path) -> None:
        raise OSError(18, "Invalid cross-device link")

    done = publish_host.link_images(store, ["n1a2b3c4"], dest, link=cross_device)
    assert set(linked(done)["n1a2b3c4"].values()) == {"copy"}
    assert (dest / "n1a2b3c4" / "web.tar").read_bytes() == b"n1a2b3c4-web"


def test_images_refuse_a_release_name_or_another_group(tmp_path: Path) -> None:
    store = nightly_store(tmp_path)
    with pytest.raises(publish_host.HostError):
        publish_host.link_images(store, ["r5"], tmp_path / "srv")
    with pytest.raises(publish_host.HostError, match="not in the"):
        publish_host.link_images(store, ["n1a2b3c4", "n9999999h"], tmp_path / "srv")


# ---------------------------------------------------------------- chain·trust-base


def test_chain_reads_last_approved(tmp_path: Path) -> None:
    path = publish_host.chain_file(fleet.FLEET_REPO, tmp_path)
    assert publish_host.chain(fleet.FLEET_REPO, root=tmp_path)["last_approved"] is None
    path.parent.mkdir(parents=True)
    path.write_text("a" * 40 + "\n")
    assert publish_host.chain(fleet.FLEET_REPO, root=tmp_path)["last_approved"] == "a" * 40
    path.write_text("junk")
    with pytest.raises(publish_host.HostError):
        publish_host.chain(fleet.FLEET_REPO, root=tmp_path)


def test_trust_base_runs_as_the_publish_user_with_the_runner_tool() -> None:
    cmd = publish_host.trust_base_command(fleet.FLEET_REPO, "b" * 40, "g2-nightly R: reason")
    assert cmd[:4] == ["runuser", "-u", "pureiron-publish", "--"]
    assert cmd[4].endswith("pureiron-v1/.venv/bin/python")
    assert cmd[6:] == [
        *("trust-base", "--fleet-repo", fleet.FLEET_REPO, "--sha", "b" * 40),
        *("--reason", "g2-nightly R: reason"),
    ]
    with pytest.raises(publish_host.HostError):
        publish_host.trust_base_command(fleet.FLEET_REPO, "main", "x")


# ---------------------------------------------------------------- Windows 쪽 sync


def test_sync_runs_every_host_step_as_root_and_sets_the_secret_by_stdin(tmp_path: Path) -> None:
    s = session(tmp_path)
    fake = FakeRemote(s)
    gh = FakeGitHub()
    done = fleet_session.sync_publish_host(s, "AB:CD", fake.remote(), repo(gh))
    assert [e for e in fake.events if e.startswith("host")] == [
        "host trust",
        "host tokens",
        "host images",
    ]
    assert gh.secrets[(fleet.FLEET_REPO, "PUREIRON_FLEET_VIEWER_TOKEN")] == "viewer-token-SECRET"
    assert "viewer-token-SECRET" not in gh.argv_and_stdin().replace("viewer-token-SECRET", "", 1)
    assert "SECRET" not in str(done)


def test_a_host_step_failure_is_infra(tmp_path: Path) -> None:
    s = session(tmp_path)
    fake = FakeRemote(s)
    remote = fake.remote()

    def broken(script: str, stdin: str | None = None) -> str:
        raise WslScriptError("publish_host trust: CA fingerprint mismatch", 1)

    with pytest.raises(InfraError, match=r"publish_host\.py trust"):
        fleet_session.sync_publish_host(
            s, "AB:CD", dataclasses.replace(remote, wsl_root=broken), repo(FakeGitHub())
        )
