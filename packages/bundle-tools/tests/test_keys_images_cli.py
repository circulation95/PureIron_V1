"""키 형식(raw·PEM)·이미지 config digest·`pureiron-bundle` CLI."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from pureiron_bundle import keys
from pureiron_bundle.cli import main
from pureiron_bundle.errors import BundleRejected, RejectCode
from pureiron_bundle.images import ImageFormatError, check_loaded_image, config_digest_from_tar
from pureiron_bundle.sign import write_signed_bundle

HEX = "ab" * 32


def test_pem_export_matches_cryptography_and_round_trips() -> None:
    for _ in range(5):
        private = Ed25519PrivateKey.generate()
        raw = private.public_key().public_bytes_raw()
        reference = private.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
        pem = keys.export_pem(raw)
        assert pem.encode("ascii") == reference
        assert keys.import_pem(pem) == raw
        assert keys.import_pem(reference.decode("ascii")) == raw


@pytest.mark.parametrize(
    "text",
    [
        "not pem",
        "-----BEGIN PUBLIC KEY-----\n!!!\n-----END PUBLIC KEY-----\n",
        "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VuAyEA" + "A" * 43 + "=\n-----END PUBLIC KEY-----\n",
    ],
)
def test_import_pem_rejects_non_ed25519(text: str) -> None:
    with pytest.raises(keys.KeyFormatError):
        keys.import_pem(text)


def test_sign_verify_and_fingerprint() -> None:
    seed = keys.generate_seed()
    public = keys.public_key(seed)
    signature = keys.sign(seed, b"data")
    assert len(signature) == keys.SIGNATURE_LEN
    assert keys.verify(public, signature, b"data")
    assert not keys.verify(public, signature, b"datA")
    assert not keys.verify(public, signature[:63], b"data")
    assert not keys.verify(public[:31], signature, b"data")
    assert keys.fingerprint(public) == hashlib.sha256(public).hexdigest()


def test_private_key_is_written_once_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "root.key"
    keys.write_private_key(path, b"\x01" * 32)
    assert keys.read_private_key(path) == b"\x01" * 32
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        keys.write_private_key(path, b"\x02" * 32)
    assert keys.read_private_key(path) == b"\x01" * 32
    (tmp_path / "short.pub").write_bytes(b"\x00" * 31)
    with pytest.raises(keys.KeyFormatError):
        keys.read_public_key(tmp_path / "short.pub")


def _image(path: Path, manifest: object, *, gz: bool = False) -> Path:
    data = json.dumps(manifest).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(buffer.getvalue(), mtime=0) if gz else buffer.getvalue())
    return path


@pytest.mark.parametrize("config", [f"blobs/sha256/{HEX}", f"{HEX}.json"])
@pytest.mark.parametrize("gz", [False, True])
def test_config_digest_from_tar(tmp_path: Path, config: str, gz: bool) -> None:
    path = _image(tmp_path / "img.tar", [{"Config": config, "RepoTags": ["x"]}], gz=gz)
    assert config_digest_from_tar(path) == f"sha256:{HEX}"


@pytest.mark.parametrize(
    "manifest", [[], {"Config": "x"}, [{"Config": "blobs/sha256/zz"}], ["str"]]
)
def test_config_digest_rejects_bad_manifest(tmp_path: Path, manifest: object) -> None:
    with pytest.raises(ImageFormatError):
        config_digest_from_tar(_image(tmp_path / "img.tar", manifest))


def test_config_digest_rejects_non_tar(tmp_path: Path) -> None:
    path = tmp_path / "img.tar"
    path.write_bytes(b"\x00\x01garbage" * 100)
    with pytest.raises(ImageFormatError):
        config_digest_from_tar(path)


def test_check_loaded_image() -> None:
    check_loaded_image("core", f"sha256:{HEX}\n", f"sha256:{HEX}")
    with pytest.raises(BundleRejected) as info:
        check_loaded_image("core", "sha256:" + "0" * 64, f"sha256:{HEX}")
    assert info.value.code is RejectCode.E_DIGEST


def _manifest(digest: str) -> dict[str, object]:
    image = {"config_digest": digest, "tag": "pureiron/core:r0"}
    return {
        "format": 1,
        "kind": "site",
        "site_id": "site-a",
        "generation": 1,
        "created_at": "2026-10-04T00:00:00Z",
        "product_release": {
            "version": "2026.10.0",
            "min_updater": "0.1.0",
            "images": dict.fromkeys(("core", "collector", "web", "site-db"), image),
            "schema": {
                "target": 1,
                "app_expected": 1,
                "app_max_compatible": 2,
                "compat_app_min": "2026.10.0",
            },
        },
        "project": {"project_id": "acme", "version": "v1", "content_hash": "sha256:" + "0" * 64},
        "window": {
            "recurring": {"tz": "Asia/Seoul", "days": ["sat"], "start": "02:00", "end": "05:00"}
        },
        "trust_update": None,
        "fleet_commit": "abc123",
    }


def test_cli_keys_cert_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    keydir = tmp_path / "keys"
    assert main(["keys", "gen", "--out", str(keydir), "--name", "root"]) == 0
    assert main(["keys", "gen", "--out", str(keydir), "--name", "release"]) == 0
    root_pub = keys.read_public_key(keydir / "root.pub")
    assert main(["keys", "fingerprint", str(keydir / "root.pub")]) == 0
    assert main(["keys", "export-pem", str(keydir / "root.pub")]) == 0
    out = capsys.readouterr().out
    assert keys.fingerprint(root_pub) in out
    assert keys.export_pem(root_pub) in out

    certdir = tmp_path / "cert"
    issue = ["cert", "issue", "--root-key", str(keydir / "root.key")]
    issue += ["--release-pub", str(keydir / "release.pub"), "--serial", "1"]
    issue += ["--not-before", "2026-09-01T00:00:00Z", "--out", str(certdir)]
    assert main(issue) == 0
    cert = json.loads((certdir / "release-cert.json").read_bytes())
    assert cert["not_after"] == "2027-02-28T00:00:00Z"

    image = _image(tmp_path / "core.tar", [{"Config": f"blobs/sha256/{HEX}"}])
    payload = {f"images/{svc}.tar": image for svc in ("core", "collector", "web", "site-db")}
    bundle = tmp_path / "site.pib"
    write_signed_bundle(
        bundle,
        json.loads(json.dumps(_manifest(f"sha256:{HEX}"))),
        payload,
        release_seed=keys.read_private_key(keydir / "release.key"),
        cert_json=(certdir / "release-cert.json").read_bytes(),
        cert_sig=(certdir / "release-cert.sig").read_bytes(),
    )
    capsys.readouterr()
    verify = ["verify", str(bundle), "--root-pub", str(keydir / "root.pub")]
    assert main([*verify, "--site", "site-a", "--updater-version", "0.1.0"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["decision"], report["files"], report["cert_serial"]) == ("apply", 4, 1)
    assert main([*verify, "--site", "site-b"]) == 1
    assert capsys.readouterr().err.startswith("E_SITE:")
    assert main([*verify, "--site", "site-a", "--updater-version", "0.0.9"]) == 1
    assert capsys.readouterr().err.startswith("E_UPDATER:")
    assert main(["keys", "fingerprint", str(tmp_path / "missing.pub")]) == 2
