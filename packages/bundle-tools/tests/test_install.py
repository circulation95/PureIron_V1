"""설치 번들(`kind: install`) 검증과 site key 서명 문서."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pureiron_bundle import keys
from pureiron_bundle.cli import main
from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.manifest import DOCKER_PACKAGES, Json
from pureiron_bundle.sign import write_signed_bundle
from pureiron_bundle.sitesign import SignedDocumentError, SitePurpose, open_document, sign_document
from pureiron_bundle.trust import TrustState, format_timestamp, issue_cert
from pureiron_bundle.verify import (
    INSTALL_LAYOUT,
    ROOT_PEM_PAYLOAD,
    ROOT_PUB_PAYLOAD,
    VerifyContext,
    verify_bundle,
    verify_install_bundle,
)

ROOT_SEED = bytes(range(32))
RELEASE_SEED = bytes(range(32, 64))
OTHER_SEED = bytes(range(64, 96))
ROOT_PUB = keys.public_key(ROOT_SEED)
CREATED = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)
UPDATER_FILE = "updater/updater-0.2.0.tar.gz"


class InstallKit:
    def __init__(self, tmp: Path, root_pub: bytes = ROOT_PUB) -> None:
        self.tmp = tmp
        self.src = tmp / "src"
        self.payload: dict[str, Path] = {}
        for name in INSTALL_LAYOUT:
            if name == ROOT_PUB_PAYLOAD:
                self.put(name, root_pub)
            elif name == ROOT_PEM_PAYLOAD:
                self.put(name, keys.export_pem(root_pub).encode())
            else:
                self.put(name, f"#!/bin/sh\n# {name}\n".encode())
        self.put(UPDATER_FILE, b"venv" * 1000)
        for package in DOCKER_PACKAGES:
            self.put(f"debs/{package}.deb", f"deb {package}".encode())

    def put(self, name: str, data: bytes) -> None:
        path = self.src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.payload[name] = path

    def document(self, **overrides: Json) -> dict[str, Json]:
        doc: dict[str, Json] = {
            "format": 1,
            "kind": "install",
            "release": "2026.10.0",
            "created_at": format_timestamp(CREATED),
            "updater": {"version": "0.2.0", "file": UPDATER_FILE},
            "docker": {
                "packages": dict.fromkeys(DOCKER_PACKAGES, "1.0-1"),
                "compose_min": "2.17.0",
            },
            "trust_update": None,
        }
        doc.update(overrides)
        return doc

    def build(self, *, document: dict[str, Json] | None = None) -> Path:
        cert_json, cert_sig = issue_cert(
            ROOT_SEED, keys.public_key(RELEASE_SEED), 1, datetime(2026, 9, 1, tzinfo=UTC)
        )
        out = self.tmp / "pureiron-install-2026.10.0.pib"
        write_signed_bundle(
            out,
            document or self.document(),
            self.payload,
            release_seed=RELEASE_SEED,
            cert_json=cert_json,
            cert_sig=cert_sig,
        )
        return out


@pytest.fixture
def kit(tmp_path: Path) -> InstallKit:
    return InstallKit(tmp_path)


def test_install_bundle_verifies_and_extracts(kit: InstallKit, tmp_path: Path) -> None:
    out = tmp_path / "x"
    result = verify_install_bundle(kit.build(), ROOT_PUB, extract_to=out)
    assert result.manifest.updater_version == "0.2.0"
    assert result.manifest.compose_min == "2.17.0"
    assert set(result.files) == set(kit.payload)
    assert (out / "keys" / "root.pub").read_bytes() == ROOT_PUB
    assert (out / "debs" / "docker-ce.deb").read_bytes() == b"deb docker-ce"
    assert result.trust.state.max_seen_created_at == CREATED


def test_install_bundle_respects_trust_state(kit: InstallKit) -> None:
    later = TrustState(max_seen_created_at=datetime(2026, 11, 1, tzinfo=UTC))
    with pytest.raises(BundleRejectedError) as err:
        verify_install_bundle(kit.build(), ROOT_PUB, later)
    assert err.value.code is RejectCode.E_CERT


def test_kinds_are_not_interchangeable(kit: InstallKit) -> None:
    pib = kit.build()
    with pytest.raises(BundleRejectedError) as err:
        verify_bundle(pib, VerifyContext(root_pubkey=ROOT_PUB, site_id="site-a"))
    assert err.value.code is RejectCode.E_FORMAT
    assert "expected a site bundle" in err.value.detail


def test_wrong_root_rejected(kit: InstallKit) -> None:
    with pytest.raises(BundleRejectedError) as err:
        verify_install_bundle(kit.build(), keys.public_key(OTHER_SEED))
    assert err.value.code is RejectCode.E_SIG


@pytest.mark.parametrize("name", [ROOT_PUB_PAYLOAD, ROOT_PEM_PAYLOAD])
def test_bundle_must_plant_the_verifying_root(tmp_path: Path, name: str) -> None:
    other = keys.public_key(OTHER_SEED)
    kit = InstallKit(tmp_path)
    kit.put(name, other if name == ROOT_PUB_PAYLOAD else keys.export_pem(other).encode())
    with pytest.raises(BundleRejectedError) as err:
        verify_install_bundle(kit.build(), ROOT_PUB)
    assert err.value.code is RejectCode.E_SIG


def test_missing_layout_member_rejected(kit: InstallKit) -> None:
    del kit.payload["install/preflight.sh"]
    with pytest.raises(BundleRejectedError) as err:
        verify_install_bundle(kit.build(), ROOT_PUB)
    assert err.value.code is RejectCode.E_FORMAT
    assert "install/preflight.sh" in err.value.detail


def test_debs_must_match_package_list(kit: InstallKit) -> None:
    kit.put("debs/extra.deb", b"x")
    with pytest.raises(BundleRejectedError) as err:
        verify_install_bundle(kit.build(), ROOT_PUB)
    assert err.value.code is RejectCode.E_FORMAT


def test_tampered_payload_rejected_and_extract_cleared(kit: InstallKit, tmp_path: Path) -> None:
    pib = kit.build()
    data = pib.read_bytes()
    marker = b"# install/install.sh"
    pib.write_bytes(data.replace(marker, b"# install/evil!!.sh"))
    out = tmp_path / "x"
    with pytest.raises(BundleRejectedError) as err:
        verify_install_bundle(pib, ROOT_PUB, extract_to=out)
    assert err.value.code is RejectCode.E_DIGEST
    assert not any(out.iterdir())


def test_cli_verify_install(
    kit: InstallKit, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root_pub = tmp_path / "root.pub"
    root_pub.write_bytes(ROOT_PUB)
    assert main(["verify", str(kit.build()), "--root-pub", str(root_pub), "--kind", "install"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "install"
    assert out["updater"] == "0.2.0"
    assert main(["verify", str(kit.build()), "--root-pub", str(root_pub)]) == 2


def test_site_document_round_trip_and_domain_separation() -> None:
    seed = bytes(range(100, 132))
    public = keys.public_key(seed)
    payload: dict[str, Json] = {"site_id": "site-a", "public_key": public.hex(), "x": 1.5}
    data = json.dumps(sign_document(seed, SitePurpose.ENROLLMENT, payload)).encode()
    assert open_document(data, SitePurpose.ENROLLMENT) == payload
    assert open_document(data, SitePurpose.ENROLLMENT, public) == payload
    with pytest.raises(SignedDocumentError):
        open_document(data, SitePurpose.REPORTS, public)
    tampered = json.loads(data)
    tampered["payload"]["site_id"] = "site-b"
    with pytest.raises(SignedDocumentError):
        open_document(json.dumps(tampered).encode(), SitePurpose.ENROLLMENT)
    with pytest.raises(SignedDocumentError):
        open_document(data, SitePurpose.ENROLLMENT, keys.public_key(OTHER_SEED))


def test_reports_need_the_registered_key() -> None:
    """리뷰: 자기 키(payload.public_key) 검증은 enrollment에서만 — 보고는 등록 키가 필요하다."""
    seed = bytes(range(100, 132))
    payload: dict[str, Json] = {"public_key": keys.public_key(seed).hex(), "reports": []}
    data = json.dumps(sign_document(seed, SitePurpose.REPORTS, payload)).encode()
    with pytest.raises(SignedDocumentError, match="registered"):
        open_document(data, SitePurpose.REPORTS)


def test_oversized_key_member_rejected(tmp_path: Path) -> None:
    kit = InstallKit(tmp_path)
    kit.put(ROOT_PEM_PAYLOAD, keys.export_pem(ROOT_PUB).encode() + b"#" * 5000)
    with pytest.raises(BundleRejectedError) as err:
        verify_install_bundle(kit.build(), ROOT_PUB)
    assert err.value.code is RejectCode.E_FORMAT
