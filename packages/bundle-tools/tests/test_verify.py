"""verify_bundle 종단 테스트 — 거부 코드마다 1건 이상, 신뢰 상태·trust_update·추출."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejected, RejectCode
from pureiron_bundle.manifest import Json, manifest_bytes
from pureiron_bundle.sign import write_signed_bundle
from pureiron_bundle.trust import TrustState, format_timestamp, issue_cert, sign_trust_update
from pureiron_bundle.verify import (
    AttemptResult,
    DraftState,
    GenDecision,
    GenState,
    SchemaAction,
    VerifyContext,
    verify_bundle,
)

ROOT_SEED = bytes(range(32))
RELEASE_SEED = bytes(range(32, 64))
OTHER_SEED = bytes(range(64, 96))
ROOT_PUB = keys.public_key(ROOT_SEED)
NOT_BEFORE = datetime(2026, 9, 1, tzinfo=UTC)
CREATED = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
SERVICES = ("core", "collector", "web", "site-db")


def image_tar(path: Path, seed: str, *, gz: bool = True) -> str:
    """docker save 형식의 작은 가짜 이미지 tar. config digest를 돌려준다."""
    blob = json.dumps({"seed": seed}).encode()
    digest = hashlib.sha256(blob).hexdigest()
    manifest = json.dumps([{"Config": f"blobs/sha256/{digest}", "RepoTags": [seed]}]).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in ((f"blobs/sha256/{digest}", blob), ("manifest.json", manifest)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    raw = buffer.getvalue()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(raw, mtime=0) if gz else raw)
    return f"sha256:{digest}"


def cert(serial: int = 1, signer: bytes = ROOT_SEED, days: int = 180) -> tuple[bytes, bytes]:
    cert_json, _ = issue_cert(ROOT_SEED, keys.public_key(RELEASE_SEED), serial, NOT_BEFORE, days)
    return cert_json, keys.sign(signer, cert_json)


class Kit:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.payload: dict[str, Path] = {}
        self.digests: dict[str, str] = {}
        for svc in SERVICES:
            name = f"images/{svc}.tar.gz"
            self.digests[svc] = image_tar(tmp / "src" / name, svc)
            self.payload[name] = tmp / "src" / name
        project = tmp / "src" / "project" / "project.tar"
        project.parent.mkdir(parents=True)
        project.write_bytes(b"project-bytes" * 100)
        self.payload["project/project.tar"] = project

    def document(self, **overrides: Json) -> dict[str, Json]:
        images: dict[str, Json] = {
            svc: {"config_digest": self.digests[svc], "tag": f"pureiron/{svc}:r0"}
            for svc in SERVICES
        }
        doc: dict[str, Json] = {
            "format": 1,
            "kind": "site",
            "site_id": "site-a",
            "generation": 5,
            "created_at": format_timestamp(CREATED),
            "product_release": {
                "version": "2026.10.0",
                "min_updater": "0.1.0",
                "images": images,
                "schema": {
                    "target": 2,
                    "app_expected": 2,
                    "app_max_compatible": 3,
                    "compat_app_min": "2026.10.0",
                },
            },
            "project": {
                "project_id": "acme",
                "version": "v3",
                "content_hash": "sha256:" + "0" * 64,
                "absorbs": {"site-a": 3},
            },
            "window": {
                "once": {"start": "2026-10-04T10:00:00+09:00", "end": "2026-10-04T11:00:00+09:00"}
            },
            "trust_update": None,
            "fleet_commit": "abc123",
        }
        doc.update(overrides)
        return doc

    def build(
        self,
        name: str = "bundle.pib",
        *,
        document: dict[str, Json] | None = None,
        certificate: tuple[bytes, bytes] | None = None,
        trust_update_sig: bytes | None = None,
        payload: dict[str, Path] | None = None,
    ) -> Path:
        cert_json, cert_sig = certificate or cert()
        out = self.tmp / name
        write_signed_bundle(
            out,
            document or self.document(),
            self.payload if payload is None else payload,
            release_seed=RELEASE_SEED,
            cert_json=cert_json,
            cert_sig=cert_sig,
            trust_update_sig=trust_update_sig,
        )
        return out


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


def ctx(**overrides: object) -> VerifyContext:
    base = VerifyContext(root_pubkey=ROOT_PUB, site_id="site-a", updater_version="0.1.0")
    return replace(base, **overrides)


def members(path: Path) -> list[tuple[str, bytes]]:
    with tarfile.open(path) as tar:
        out: list[tuple[str, bytes]] = []
        for member in tar.getmembers():
            handle = tar.extractfile(member)
            out.append((member.name, b"" if handle is None else handle.read()))
        return out


def write_members(path: Path, items: list[tuple[str, bytes]]) -> Path:
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in items:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def reject(path: Path, context: VerifyContext | None = None, **kw: Path) -> RejectCode:
    with pytest.raises(BundleRejected) as info:
        verify_bundle(path, context or ctx(), **kw)
    return info.value.code


def test_valid_bundle_fresh_install(kit: Kit) -> None:
    result = verify_bundle(kit.build(), ctx())
    assert result.generation is GenDecision.APPLY
    assert result.schema is not None
    assert result.schema.action is SchemaAction.MIGRATE
    assert (result.schema.current, result.schema.target) == (0, 2)
    assert sorted(result.files) == sorted(kit.payload)
    assert result.trust.state == TrustState(0, CREATED)
    assert not result.trust.trust_update_applied
    assert result.manifest.product_release.images["core"].config_digest == kit.digests["core"]
    assert result.verify_s >= 0


def test_writer_is_deterministic(kit: Kit) -> None:
    first = kit.build("a.pib").read_bytes()
    second = kit.build("b.pib").read_bytes()
    assert first == second
    names = [name for name, _ in members(kit.tmp / "a.pib")]
    assert names[:4] == ["manifest.json", "manifest.sig", "release-cert.json", "release-cert.sig"]
    assert names[4:] == sorted(kit.payload)
    with tarfile.open(kit.tmp / "a.pib") as tar:
        for info in tar.getmembers():
            assert (info.mtime, info.uid, info.gid, info.uname, info.gname, info.mode) == (
                0,
                0,
                0,
                "",
                "",
                0o644,
            )


def test_manifest_bytes_are_jcs(kit: Kit) -> None:
    path = kit.build()
    data = dict(members(path))["manifest.json"]
    assert data == manifest_bytes(json.loads(data))


# ① 서명·cert -----------------------------------------------------------------


def test_swapped_manifest_is_e_sig(kit: Kit) -> None:
    good = kit.build("good.pib")
    other = kit.build("other.pib", document=kit.document(generation=6))
    items = members(good)
    items[0] = ("manifest.json", dict(members(other))["manifest.json"])
    assert reject(write_members(kit.tmp / "swapped.pib", items)) is RejectCode.E_SIG


def test_cert_signed_by_release_key_is_e_sig(kit: Kit) -> None:
    path = kit.build(certificate=cert(signer=RELEASE_SEED))
    assert reject(path) is RejectCode.E_SIG


def test_unknown_root_is_e_sig(kit: Kit) -> None:
    path = kit.build()
    assert reject(path, ctx(root_pubkey=keys.public_key(OTHER_SEED))) is RejectCode.E_SIG


@pytest.mark.parametrize(
    "created_at",
    [NOT_BEFORE - timedelta(seconds=1), NOT_BEFORE + timedelta(days=180, seconds=1)],
)
def test_cert_validity_uses_signed_created_at(kit: Kit, created_at: datetime) -> None:
    path = kit.build(document=kit.document(created_at=format_timestamp(created_at)))
    assert reject(path) is RejectCode.E_CERT


def test_cert_boundaries_are_inclusive(kit: Kit) -> None:
    for i, stamp in enumerate((NOT_BEFORE, NOT_BEFORE + timedelta(days=180))):
        path = kit.build(f"b{i}.pib", document=kit.document(created_at=format_timestamp(stamp)))
        assert verify_bundle(path, ctx()).trust.state.max_seen_created_at == stamp


def test_created_at_regression_is_e_cert(kit: Kit) -> None:
    path = kit.build()
    later = TrustState(max_seen_created_at=CREATED + timedelta(seconds=1))
    assert reject(path, ctx(trust=later)) is RejectCode.E_CERT
    same = verify_bundle(path, ctx(trust=TrustState(max_seen_created_at=CREATED)))
    assert same.trust.state.max_seen_created_at == CREATED


def test_serial_below_floor_is_e_cert(kit: Kit) -> None:
    path = kit.build(certificate=cert(serial=1))
    assert reject(path, ctx(trust=TrustState(min_cert_serial=2))) is RejectCode.E_CERT


def test_valid_trust_update_raises_floor(kit: Kit) -> None:
    path = kit.build(
        document=kit.document(trust_update={"min_cert_serial": 2}),
        certificate=cert(serial=2),
        trust_update_sig=sign_trust_update(ROOT_SEED, 2),
    )
    result = verify_bundle(path, ctx(trust=TrustState(min_cert_serial=1)))
    assert result.trust.trust_update_applied
    assert result.trust.state.min_cert_serial == 2


def test_trust_update_floor_applies_to_own_cert(kit: Kit) -> None:
    path = kit.build(
        document=kit.document(trust_update={"min_cert_serial": 3}),
        certificate=cert(serial=2),
        trust_update_sig=sign_trust_update(ROOT_SEED, 3),
    )
    assert reject(path) is RejectCode.E_CERT


def test_trust_update_never_lowers_floor(kit: Kit) -> None:
    path = kit.build(
        document=kit.document(trust_update={"min_cert_serial": 1}),
        certificate=cert(serial=5),
        trust_update_sig=sign_trust_update(ROOT_SEED, 1),
    )
    result = verify_bundle(path, ctx(trust=TrustState(min_cert_serial=4)))
    assert result.trust.state.min_cert_serial == 4


@pytest.mark.parametrize("signer", [None, RELEASE_SEED, "wrong-value"])
def test_trust_update_without_valid_root_signature_is_ignored(
    kit: Kit, signer: bytes | str | None
) -> None:
    sig: bytes | None = None
    if isinstance(signer, bytes):
        sig = sign_trust_update(signer, 3)
    elif signer == "wrong-value":
        sig = sign_trust_update(ROOT_SEED, 9)
    path = kit.build(
        document=kit.document(trust_update={"min_cert_serial": 3}),
        certificate=cert(serial=2),
        trust_update_sig=sig,
    )
    result = verify_bundle(path, ctx(trust=TrustState(min_cert_serial=1)))
    assert not result.trust.trust_update_applied
    assert result.trust.state.min_cert_serial == 1


# ②③ site·generation -------------------------------------------------------------


def test_wrong_site_is_e_site(kit: Kit) -> None:
    assert reject(kit.build(), ctx(site_id="site-b")) is RejectCode.E_SITE


def test_old_generation_is_e_gen(kit: Kit) -> None:
    state = GenState(applied_gen=6, last_attempted_gen=6, last_result=AttemptResult.SUCCEEDED)
    assert reject(kit.build(), ctx(generation=state)) is RejectCode.E_GEN


def test_applied_generation_is_no_op_without_payload_checks(kit: Kit) -> None:
    path = kit.build()
    items = members(path)
    items[-1] = (items[-1][0], b"tampered")
    tampered = write_members(kit.tmp / "t.pib", items)
    state = GenState(applied_gen=5, last_attempted_gen=5, last_result=AttemptResult.SUCCEEDED)
    result = verify_bundle(tampered, ctx(generation=state, draft=DraftState(9, 1)))
    assert result.generation is GenDecision.NO_OP
    assert result.schema is None
    assert result.files == {}


# ④ payload --------------------------------------------------------------------


def test_flipped_payload_byte_is_e_digest(kit: Kit) -> None:
    items = members(kit.build())
    name, data = items[-1]
    items[-1] = (name, bytes([data[0] ^ 1]) + data[1:])
    assert reject(write_members(kit.tmp / "flip.pib", items)) is RejectCode.E_DIGEST


def test_extra_payload_is_e_digest(kit: Kit) -> None:
    items = [*members(kit.build()), ("zz/extra.bin", b"x")]
    assert reject(write_members(kit.tmp / "extra.pib", items)) is RejectCode.E_DIGEST


def test_missing_payload_is_e_digest(kit: Kit) -> None:
    items = members(kit.build())[:-1]
    assert reject(write_members(kit.tmp / "missing.pib", items)) is RejectCode.E_DIGEST


def test_image_config_digest_mismatch_is_e_digest(kit: Kit) -> None:
    doc = kit.document()
    release = doc["product_release"]
    assert isinstance(release, dict)
    images = release["images"]
    assert isinstance(images, dict)
    images["web"] = {"config_digest": "sha256:" + "f" * 64, "tag": "pureiron/web:r0"}
    assert reject(kit.build(document=doc)) is RejectCode.E_DIGEST


def test_image_without_payload_is_e_digest(kit: Kit) -> None:
    payload = {k: v for k, v in kit.payload.items() if k != "images/web.tar.gz"}
    assert reject(kit.build(payload=payload)) is RejectCode.E_DIGEST


def test_uncompressed_image_tar_is_accepted(kit: Kit) -> None:
    payload = {k: v for k, v in kit.payload.items() if k != "images/web.tar.gz"}
    plain = kit.tmp / "src" / "images" / "web.tar"
    assert image_tar(plain, "web", gz=False) == kit.digests["web"]
    payload["images/web.tar"] = plain
    assert verify_bundle(kit.build(payload=payload), ctx()).generation is GenDecision.APPLY


# ⑤⑥ 스키마·Updater·드래프트 -------------------------------------------------------


def test_schema_jump_is_e_schema(kit: Kit) -> None:
    assert reject(kit.build(), ctx(schema_epoch=0)) is RejectCode.E_SCHEMA


def test_min_updater_is_compared_as_semver(kit: Kit) -> None:
    release = kit.document()["product_release"]
    assert isinstance(release, dict)
    path = kit.build(document=kit.document(product_release={**release, "min_updater": "0.10.0"}))
    assert reject(path, ctx(updater_version="0.9.0")) is RejectCode.E_UPDATER
    assert verify_bundle(path, ctx(updater_version="0.10.0")).generation is GenDecision.APPLY
    assert verify_bundle(path, ctx(updater_version=None)).generation is GenDecision.APPLY


def test_unabsorbed_drafts_are_e_draft(kit: Kit) -> None:
    assert reject(kit.build(), ctx(draft=DraftState(site_rev=4, base_site_rev=4))) is (
        RejectCode.E_DRAFT
    )
    ok = verify_bundle(kit.build("ok.pib"), ctx(draft=DraftState(site_rev=3, base_site_rev=1)))
    assert ok.generation is GenDecision.APPLY


def test_signed_manifest_violating_contract_is_e_format(kit: Kit) -> None:
    items = members(kit.build())
    bad = manifest_bytes({"format": 1, "kind": "site"})
    items[0] = ("manifest.json", bad)
    items[1] = ("manifest.sig", keys.sign(RELEASE_SEED, bad))
    assert reject(write_members(kit.tmp / "bad.pib", items)) is RejectCode.E_FORMAT


# 추출 ---------------------------------------------------------------------------


def test_extract_to_writes_only_payload_under_directory(kit: Kit) -> None:
    target = kit.tmp / "staging"
    before = {p for p in kit.tmp.rglob("*") if not p.is_relative_to(target)}
    verify_bundle(kit.build(), ctx(), extract_to=target)
    written = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert written == sorted(kit.payload)
    for name, source in kit.payload.items():
        assert (target / name).read_bytes() == source.read_bytes()
    after = {p for p in kit.tmp.rglob("*") if not p.is_relative_to(target)}
    assert after - before == {kit.tmp / "bundle.pib"}


def test_extract_to_must_be_empty(kit: Kit) -> None:
    target = kit.tmp / "staging"
    target.mkdir()
    (target / "old").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        verify_bundle(kit.build(), ctx(), extract_to=target)


def test_traversal_member_is_rejected_before_writing(kit: Kit) -> None:
    items = [*members(kit.build()), ("../escape.bin", b"x")]
    target = kit.tmp / "inner" / "staging"
    assert reject(write_members(kit.tmp / "t.pib", items), extract_to=target) is (
        RejectCode.E_FORMAT
    )
    assert not (kit.tmp / "inner" / "escape.bin").exists()
    assert not (kit.tmp / "escape.bin").exists()
