"""번들 빌더 종단 테스트(docker 없이): 가짜 이미지 tar·임시 키·cert로 빌드 → verify_bundle."""

from __future__ import annotations

import hashlib
import io
import json
import platform
import shutil
import tarfile
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import build_bundle
import pytest
from build_bundle import (
    SIZE_LIMIT,
    SIZE_WARN,
    ReleaseRecord,
    compat_app_min,
    default_min_updater,
    main,
    size_gate,
    version_key,
)

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.images import config_digest_from_tar
from pureiron_bundle.trust import issue_cert
from pureiron_bundle.verify import GenDecision, SchemaAction, VerifyContext, verify_bundle
from pureiron_project.project import load_project

REPO = Path(__file__).resolve().parents[2]
ACME = REPO / "packages" / "project-tools" / "fixtures" / "acme"
SERVICES = ("core", "collector", "web", "site-db")
ROOT_SEED = bytes(range(32))
RELEASE_SEED = bytes(range(32, 64))
CREATED = "2026-10-04T01:00:00Z"
FLEET = """\
site: site-a
generation: 7
product: 2026.11.0
project: { repo: pureiron-project-acme, version: v3 }
window: { recurring: { tz: Asia/Seoul, days: [sat, sun], start: "02:00", end: "05:00" } }
"""


def fake_image(path: Path, name: str) -> str:
    blob = json.dumps({"architecture": "amd64", "name": name}).encode()
    digest = hashlib.sha256(blob).hexdigest()
    manifest = json.dumps([{"Config": f"blobs/sha256/{digest}", "RepoTags": [name]}]).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for member, data in ((f"blobs/sha256/{digest}", blob), ("manifest.json", manifest)):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return f"sha256:{digest}"


def release_record(directory: Path, store: Path, name: str, version: str, epoch: int) -> None:
    images: dict[str, dict[str, str]] = {}
    for svc in SERVICES:
        tag = f"pureiron/{svc}:{name}" if svc != "site-db" else "pureiron/site-db:2.30.2-pg16"
        images[svc] = {"tag": tag, "config_digest": fake_image(store / name / f"{svc}.tar", tag)}
    images["site-db"]["upstream"] = "timescale/timescaledb:2.30.2-pg16@sha256:" + "6" * 64
    record = {
        "name": name,
        "release": name,
        "version": version,
        "images": images,
        "schema": {"epoch": epoch, "app_expected": epoch, "app_max_compatible": epoch + 1},
    }
    (directory / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


class Setup:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.releases = tmp / "releases"
        self.releases.mkdir()
        self.store = tmp / "store"
        release_record(self.releases, self.store, "r0", "2026.10.0", 1)
        release_record(self.releases, self.store, "r1", "2026.11.0", 2)
        self.fleet = tmp / "site-a.yaml"
        self.fleet.write_text(FLEET, encoding="utf-8")
        self.project = tmp / "acme"
        shutil.copytree(ACME, self.project)
        self.keys = tmp / "keys"
        self.keys.mkdir()
        keys.write_private_key(self.keys / "release.key", RELEASE_SEED)
        cert_json, cert_sig = issue_cert(
            ROOT_SEED, keys.public_key(RELEASE_SEED), 1, datetime(2026, 9, 1, tzinfo=UTC)
        )
        (self.keys / "release-cert.json").write_bytes(cert_json)
        (self.keys / "release-cert.sig").write_bytes(cert_sig)
        keys.write_public_key(self.keys / "root.pub", keys.public_key(ROOT_SEED))
        self.out = tmp / "out"

    def args(self, **overrides: str) -> list[str]:
        values = {
            "--fleet-file": str(self.fleet),
            "--project-dir": str(self.project),
            "--release-key": str(self.keys / "release.key"),
            "--cert-dir": str(self.keys),
            "--root-pub": str(self.keys / "root.pub"),
            "--fleet-commit": "abc1234",
            "--out": str(self.out),
            "--releases": str(self.releases),
            "--store": str(self.store),
            "--created-at": CREATED,
        }
        values.update({f"--{k.replace('_', '-')}": v for k, v in overrides.items()})
        return [part for pair in values.items() for part in pair]

    @property
    def pib(self) -> Path:
        return self.out / "pureiron-site-a-g7.pib"


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def context(**overrides: object) -> VerifyContext:
    base = VerifyContext(
        root_pubkey=keys.public_key(ROOT_SEED), site_id="site-a", updater_version="0.1.0"
    )
    return replace(base, **overrides)


def test_build_then_verify_end_to_end(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(setup.args()) == 0
    assert capsys.readouterr().out.strip().endswith("pureiron-site-a-g7.pib")
    extract = setup.tmp / "staging"
    result = verify_bundle(setup.pib, context(schema_epoch=1), extract_to=extract)
    manifest = result.manifest
    assert result.generation is GenDecision.APPLY
    assert result.schema is not None
    assert (result.schema.action, result.schema.target) == (SchemaAction.MIGRATE, 2)
    assert manifest.generation == 7
    assert manifest.product_release.version == "2026.11.0"
    assert manifest.product_release.min_updater == default_min_updater()
    # r0(epoch 1)의 envelope [1, 2]가 epoch 2 스키마를 포함한다.
    assert manifest.product_release.schema.compat_app_min == "2026.10.0"
    assert manifest.project.content_hash == load_project(ACME).content_hash()
    assert manifest.project.absorbs == {"site-a": 3}
    assert manifest.fleet_commit == "abc1234"
    assert sorted(manifest.files) == [
        "compose/site.compose.yml",
        *(f"images/{svc}.tar.gz" for svc in sorted(SERVICES)),
        "project/project.tar",
    ]
    record = json.loads((setup.releases / "r1.json").read_text(encoding="utf-8"))
    for svc in SERVICES:
        digest = record["images"][svc]["config_digest"]
        assert manifest.product_release.images[svc].config_digest == digest
        assert config_digest_from_tar(extract / "images" / f"{svc}.tar.gz") == digest
    assert manifest.product_release.images["site-db"].upstream is not None
    with tarfile.open(extract / "project" / "project.tar") as tar:
        names = tar.getnames()
    assert names == sorted(names)
    assert "project.yaml" in names
    metrics_file = setup.out / "pureiron-site-a-g7.bundle-metrics.json"
    metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
    assert metrics["size_bytes"] == setup.pib.stat().st_size
    assert sorted(metrics["image_sizes"]) == sorted(SERVICES)
    assert metrics["build_s"] >= 0
    assert metrics["zlib"] == zlib.ZLIB_RUNTIME_VERSION
    assert metrics["python"] == platform.python_version()
    assert not list(setup.out.glob(".staging-*"))


def test_build_is_deterministic(setup: Setup) -> None:
    assert main(setup.args()) == 0
    first = setup.pib.read_bytes()
    assert main(setup.args()) == 0
    assert setup.pib.read_bytes() == first


def test_tampered_bundles_are_rejected(setup: Setup) -> None:
    assert main(setup.args()) == 0
    with tarfile.open(setup.pib) as tar:
        items: list[tuple[str, bytes]] = []
        for member in tar.getmembers():
            handle = tar.extractfile(member)
            items.append((member.name, b"" if handle is None else handle.read()))

    def write(name: str, replaced: dict[str, bytes]) -> Path:
        path = setup.tmp / name
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
            for member, data in items:
                body = replaced.get(member, data)
                info = tarfile.TarInfo(member)
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
        return path

    def code(path: Path, ctx: VerifyContext) -> RejectCode:
        with pytest.raises(BundleRejectedError) as info:
            verify_bundle(path, ctx)
        return info.value.code

    blobs = dict(items)
    project = blobs["project/project.tar"]
    flipped = write("flip.pib", {"project/project.tar": project[:-1] + bytes([project[-1] ^ 1])})
    assert code(flipped, context()) is RejectCode.E_DIGEST
    manifest = blobs["manifest.json"].replace(b'"generation":7', b'"generation":8')
    assert code(write("swap.pib", {"manifest.json": manifest}), context()) is RejectCode.E_SIG
    assert code(setup.pib, context(site_id="site-b")) is RejectCode.E_SITE


def test_missing_store_image_or_digest_mismatch_fails(setup: Setup) -> None:
    (setup.store / "r1" / "web.tar").unlink()
    assert main(setup.args()) == 1
    fake_image(setup.store / "r1" / "web.tar", "different")
    assert main(setup.args()) == 1
    assert not setup.pib.exists()


def test_self_verify_failure_removes_bundle(setup: Setup) -> None:
    keys.write_public_key(setup.keys / "other.pub", keys.public_key(bytes(range(64, 96))))
    assert main(setup.args(root_pub=str(setup.keys / "other.pub"))) == 1
    assert not setup.pib.exists()
    assert main(setup.args(min_updater="0.1")) == 1
    assert not setup.pib.exists()


def test_size_budget_failure_removes_bundle(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_bundle, "SIZE_LIMIT", 1)
    assert main(setup.args()) == 1
    assert "::error::" in capsys.readouterr().out
    assert not setup.pib.exists()
    assert not list(setup.out.glob("*.bundle-metrics.json"))


def test_malformed_release_record_is_a_build_error(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    record = json.loads((setup.releases / "r1.json").read_text(encoding="utf-8"))
    del record["schema"]["app_expected"]
    (setup.releases / "r1.json").write_text(json.dumps(record), encoding="utf-8")
    assert main(setup.args()) == 1
    err = capsys.readouterr().err
    assert "malformed release record" in err
    assert "Traceback" not in err
    record["schema"]["app_expected"] = 2
    del record["images"]["web"]["tag"]
    (setup.releases / "r1.json").write_text(json.dumps(record), encoding="utf-8")
    assert main(setup.args()) == 1
    assert "images.web" in capsys.readouterr().err


def test_unknown_product_or_invalid_fleet_fails(setup: Setup) -> None:
    setup.fleet.write_text(FLEET.replace("2026.11.0", "2027.1.0"), encoding="utf-8")
    assert main(setup.args()) == 1
    setup.fleet.write_text(FLEET.replace("generation: 7", "generation: 0"), encoding="utf-8")
    assert main(setup.args()) == 1


def test_compat_app_min_is_oldest_compatible_final_release() -> None:
    def rec(version: str, epoch: int) -> ReleaseRecord:
        return ReleaseRecord(version, version, {}, epoch, epoch, epoch + 1)

    r10, r11, r12 = rec("2026.10.0", 1), rec("2026.11.0", 2), rec("2026.12.0", 3)
    rc = rec("2026.9.0-rc.1", 2)
    future = rec("2027.1.0", 4)
    records = [r12, r10, rc, r11, future]
    assert compat_app_min(records, r12) == "2026.11.0"
    assert compat_app_min(records, r11) == "2026.10.0"
    assert compat_app_min(records, future) == "2026.12.0"
    first_rc = rec("2026.8.0-rc.1", 9)
    assert compat_app_min([first_rc], first_rc) == "2026.8.0-rc.1"
    # 같은 epoch의 정식이 이미 기록된 뒤 다시 빌드한 rc: 자기보다 새 버전을 고르지 않는다.
    rebuilt_rc = rec("2026.11.0-rc.2", 2)
    assert compat_app_min([*records, rebuilt_rc], rebuilt_rc) == "2026.10.0"
    late_rc = rec("2026.13.0-rc.1", 3)
    assert compat_app_min([r12, late_rc], late_rc) == "2026.12.0"
    assert version_key("2026.10.0") > version_key("2026.9.5")
    assert version_key("2026.10.0") > version_key("2026.10.0-rc.1")


def test_size_gate(capsys: pytest.CaptureFixture[str]) -> None:
    assert size_gate(SIZE_WARN) == 0
    assert capsys.readouterr().out == ""
    assert size_gate(SIZE_WARN + 1) == 0
    assert "::warning::" in capsys.readouterr().out
    assert size_gate(SIZE_LIMIT + 1) == 1
    assert "::error::" in capsys.readouterr().out


def test_r0_record_has_contract_version() -> None:
    record = json.loads((REPO / "deploy" / "releases" / "r0.json").read_text(encoding="utf-8"))
    assert record["version"] == "2026.10.0"
