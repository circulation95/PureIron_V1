"""`.pib` 검증 단일 구현(ADR-023, P3, plan §6.2).

검사 순서는 계약으로 고정이다:
① 서명·cert(`E_SIG`/`E_CERT`) ② site_id(`E_SITE`) ③ generation(`E_GEN`)
④ payload sha256·누락·초과·이미지 config digest(`E_DIGEST`) ⑤ 스키마 envelope(`E_SCHEMA`)·
`min_updater`(`E_UPDATER`) ⑥ 드래프트 게이트(`E_DRAFT`).

메타데이터는 payload를 해시하기 전에 읽고 서명을 확인한다. payload는 해시만 하고, 이미지 tar는
sha256이 맞은 추출본에서만 해석한다(hash first, parse later). gen 판정이 NO_OP(이미 적용)이면
④~⑥은 하지 않는다 — 적용할 것이 없으므로 payload·스키마·드래프트는 판단 대상이 아니다.
판정 규칙(gen·스키마·드래프트·Updater 버전)은 Updater가 재검사에 쓰도록 순수 함수로 둔다.

설치 번들(`kind: install`)은 `verify_install_bundle`이 같은 ①(서명·cert)·④(payload) 코드로
검증한다. site·generation·스키마·드래프트는 해당이 없고, 대신 설치 배치(필수 payload)와
번들 안 `keys/root.pub`·`keys/root.pem`이 검증에 쓴 root 공개키와 같은지 확인한다.
"""

from __future__ import annotations

import io
import re
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO

from pureiron_bundle import keys
from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.images import ImageFormatError, config_digest_from_tar
from pureiron_bundle.manifest import (
    DOCKER_PACKAGES,
    MANIFEST_FILE,
    BundleKind,
    InstallManifest,
    Manifest,
    SchemaEnvelope,
    bundle_kind,
    parse_install_manifest,
    parse_manifest,
)
from pureiron_bundle.pib import (
    CERT_FILE,
    CERT_SIG,
    MANIFEST_SIG,
    TRUST_UPDATE_SIG,
    PayloadMember,
    PibReader,
    hash_stream,
    open_pib,
    safe_target,
)
from pureiron_bundle.trust import (
    ReleaseCert,
    TrustDecision,
    TrustState,
    check_validity,
    verify_cert,
    verify_manifest_signature,
)

_IMAGE_PAYLOAD = re.compile(r"images/([a-z][a-z0-9-]{0,31})\.tar(\.gz)?")
_SEMVER = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")
PART_SUFFIX = ".part"
ROOT_PUB_PAYLOAD = "keys/root.pub"
ROOT_PEM_PAYLOAD = "keys/root.pem"
INSTALL_LAYOUT = (
    "bootstrap/bootstrap-verify.sh",
    "install/daemon.json",
    "install/install.sh",
    "install/preflight.sh",
    "install/updater.env",
    "systemd/pureiron-updater.service",
    ROOT_PUB_PAYLOAD,
    ROOT_PEM_PAYLOAD,
)
"""설치 번들 필수 payload(+ `updater.file`, `debs/<패키지>.deb`)."""
_CAPTURE_LIMIT = 4096


class AttemptResult(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class GenDecision(StrEnum):
    APPLY = "apply"
    NO_OP = "no_op"
    """이미 적용된 generation — 성공, 할 일 없음."""
    RESUME = "resume"
    """같은 generation 시도가 `pending`으로 끊김 — 이어서 진행."""


class SchemaAction(StrEnum):
    MIGRATE = "migrate"
    NO_MIGRATE = "no_migrate"


@dataclass(frozen=True, slots=True)
class GenState:
    """Updater 상태 파일의 generation 부분. 새 설치 = 모두 None."""

    applied_gen: int | None = None
    last_attempted_gen: int | None = None
    last_result: AttemptResult | None = None


@dataclass(frozen=True, slots=True)
class DraftState:
    """현장 `project_state`의 드래프트 리비전."""

    site_rev: int
    base_site_rev: int


@dataclass(frozen=True, slots=True)
class SchemaDecision:
    action: SchemaAction
    current: int
    """판정에 쓴 현재 epoch(새 설치는 0)."""
    target: int | None
    """MIGRATE일 때 `migrate up --to` 값."""


@dataclass(frozen=True, slots=True)
class VerifyContext:
    root_pubkey: bytes
    site_id: str
    updater_version: str | None = None
    """None이면 `min_updater` 검사를 건너뛴다(Updater 밖 검증 — CI 게시 검사 등)."""
    trust: TrustState = field(default_factory=TrustState)
    generation: GenState = field(default_factory=GenState)
    schema_epoch: int | None = None
    """현재 DB 스키마 epoch. None = 새 설치(0부터 migrate)."""
    draft: DraftState | None = None
    """None = project_state 없음(새 설치) → 드래프트 게이트 생략."""


@dataclass(frozen=True, slots=True)
class VerifiedBundle:
    manifest: Manifest
    cert: ReleaseCert
    generation: GenDecision
    schema: SchemaDecision | None
    """NO_OP이면 None(판정하지 않음)."""
    trust: TrustDecision
    files: Mapping[str, str]
    """확인된 payload 경로 → sha256(NO_OP이면 비어 있음)."""
    verify_s: float


@dataclass(frozen=True, slots=True)
class VerifiedInstall:
    manifest: InstallManifest
    cert: ReleaseCert
    trust: TrustDecision
    files: Mapping[str, str]
    verify_s: float


def decide_generation(gen: int, state: GenState) -> GenDecision:
    """plan M2 gen 규칙. 거부는 `E_GEN`."""
    known = [g for g in (state.applied_gen, state.last_attempted_gen) if g is not None]
    if known and gen < max(known):
        raise BundleRejectedError(RejectCode.E_GEN, f"generation {gen} < {max(known)}")
    if gen == state.applied_gen:
        return GenDecision.NO_OP
    if gen == state.last_attempted_gen:
        if state.last_result is AttemptResult.PENDING:
            return GenDecision.RESUME
        raise BundleRejectedError(
            RejectCode.E_GEN,
            f"generation {gen} already attempted ({state.last_result}) — not retried",
        )
    return GenDecision.APPLY


def decide_schema(envelope: SchemaEnvelope, current: int | None) -> SchemaDecision:
    """plan §6.6 Updater 판정. 거부는 `E_SCHEMA`."""
    expected, max_compatible = envelope.app_expected, envelope.app_max_compatible
    if max_compatible != expected + 1 or envelope.target != expected:
        raise BundleRejectedError(
            RejectCode.E_SCHEMA,
            f"inconsistent schema envelope target={envelope.target} "
            f"app_expected={expected} app_max_compatible={max_compatible}",
        )
    if current is None:
        return SchemaDecision(SchemaAction.MIGRATE, 0, envelope.target)
    if current > max_compatible:
        raise BundleRejectedError(
            RejectCode.E_SCHEMA, f"schema epoch {current} > app_max_compatible {max_compatible}"
        )
    if expected - current > 1:
        raise BundleRejectedError(
            RejectCode.E_SCHEMA,
            f"schema epoch {current} → {expected} skips epochs (apply stepwise releases)",
        )
    if expected - current == 1:
        return SchemaDecision(SchemaAction.MIGRATE, current, envelope.target)
    return SchemaDecision(SchemaAction.NO_MIGRATE, current, None)


def parse_semver(text: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(text)
    if match is None:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {text!r}")
    return int(match[1]), int(match[2]), int(match[3])


def check_updater_version(min_updater: str, updater_version: str) -> None:
    if parse_semver(min_updater) > parse_semver(updater_version):
        raise BundleRejectedError(
            RejectCode.E_UPDATER, f"bundle needs updater >= {min_updater}, have {updater_version}"
        )


def check_draft_gate(site_id: str, absorbs: Mapping[str, int], draft: DraftState | None) -> None:
    """드래프트 게이트(data-contracts). Updater가 lock 시점에 같은 함수로 재검사한다."""
    if draft is None:
        return
    absorbed = absorbs.get(site_id)
    if absorbed is not None:
        if draft.site_rev > absorbed:
            raise BundleRejectedError(
                RejectCode.E_DRAFT,
                f"site_rev {draft.site_rev} has drafts not absorbed (absorbs {absorbed})",
            )
    elif draft.site_rev != draft.base_site_rev:
        raise BundleRejectedError(
            RejectCode.E_DRAFT,
            f"site_rev {draft.site_rev} != base_site_rev {draft.base_site_rev} "
            "(unabsorbed site drafts)",
        )


def _image_payloads(manifest: Manifest) -> dict[str, str]:
    """payload 경로 → 서비스. 이미지마다 payload가 정확히 하나여야 한다(`E_DIGEST`)."""
    found: dict[str, str] = {}
    for name in manifest.files:
        match = _IMAGE_PAYLOAD.fullmatch(name)
        if match is not None:
            found[name] = match[1]
    images = manifest.product_release.images
    for name, service in found.items():
        if service not in images:
            raise BundleRejectedError(
                RejectCode.E_DIGEST, f"{name}: no product_release.images entry for {service!r}"
            )
    for service in images:
        count = sum(1 for svc in found.values() if svc == service)
        if count != 1:
            raise BundleRejectedError(
                RejectCode.E_DIGEST, f"image {service!r} needs exactly one payload, found {count}"
            )
    return found


def _extract_member(extract_to: Path, member: PayloadMember, source: IO[bytes]) -> str:
    """`<name>.part`에 쓰며 해시하고, 호출자가 sha256을 확인한 뒤 이름을 바꾼다."""
    try:
        part = safe_target(extract_to, member.name + PART_SUFFIX)
        sink = part.open("xb")
    except OSError as exc:
        raise BundleRejectedError(
            RejectCode.E_FORMAT, f"{member.name}: cannot extract ({exc})"
        ) from exc
    with sink:
        return hash_stream(source, sink)


def _check_payload(
    reader: PibReader,
    files: Mapping[str, str],
    extract_to: Path | None,
    capture: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], dict[str, bytes]]:
    """payload sha256·누락·초과. 여기서는 해시만 하고 내용(이미지 tar)은 해석하지 않는다.

    `capture`의 작은 멤버(≤ 4 KiB)는 바이트도 돌려준다 — 호출자는 sha256이 맞은 뒤에만 해석한다.
    """
    expected = dict(files)
    verified: dict[str, str] = {}
    captured: dict[str, bytes] = {}
    for member in reader.iter_payload():
        want = expected.pop(member.name, None)
        if want is None:
            raise BundleRejectedError(RejectCode.E_DIGEST, f"{member.name}: not listed in manifest")
        source: IO[bytes] = member.stream
        if member.name in capture:
            if member.size > _CAPTURE_LIMIT:
                raise BundleRejectedError(
                    RejectCode.E_FORMAT, f"{member.name}: larger than {_CAPTURE_LIMIT} bytes"
                )
            captured[member.name] = member.stream.read()
            source = io.BytesIO(captured[member.name])
        if extract_to is None:
            actual = hash_stream(source)
        else:
            actual = _extract_member(extract_to, member, source)
        if actual != want:
            raise BundleRejectedError(
                RejectCode.E_DIGEST, f"{member.name}: sha256 {actual} != {want}"
            )
        if extract_to is not None:
            final = extract_to.joinpath(*member.name.split("/"))
            try:
                final.with_name(final.name + PART_SUFFIX).rename(final)
            except OSError as exc:
                raise BundleRejectedError(
                    RejectCode.E_FORMAT, f"{member.name}: cannot extract ({exc})"
                ) from exc
        verified[member.name] = actual
    if expected:
        raise BundleRejectedError(RejectCode.E_DIGEST, f"missing payload files {sorted(expected)}")
    return verified, captured


def _check_image_digests(manifest: Manifest, images: Mapping[str, str], extract_to: Path) -> None:
    """sha256이 확인된(서명된) 추출본에서만 이미지 config digest를 읽는다."""
    for name, service in sorted(images.items()):
        recorded = manifest.product_release.images[service].config_digest
        try:
            actual = config_digest_from_tar(extract_to.joinpath(*name.split("/")))
        except ImageFormatError as exc:
            raise BundleRejectedError(RejectCode.E_DIGEST, str(exc)) from exc
        if actual != recorded:
            raise BundleRejectedError(
                RejectCode.E_DIGEST, f"{name}: config digest {actual} != manifest {recorded}"
            )


def _prepare_extract_dir(extract_to: Path) -> None:
    if extract_to.is_symlink():
        raise ValueError(f"extract directory must not be a symlink: {extract_to}")
    extract_to.mkdir(parents=True, exist_ok=True)
    if any(extract_to.iterdir()):
        raise ValueError(f"extract directory must be empty: {extract_to}")


def _clear_dir(directory: Path) -> None:
    """거부된 번들의 추출물을 지운다(시작 시 비어 있음을 확인한 디렉터리)."""
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _signed_metadata(
    reader: PibReader, root_pubkey: bytes, kind: BundleKind
) -> tuple[dict[str, bytes], ReleaseCert]:
    """① root → cert → manifest 서명(서명 확인 전에는 아무것도 해석하지 않는다) → 종류 확인."""
    meta = reader.read_metadata()
    cert = verify_cert(root_pubkey, meta[CERT_FILE], meta[CERT_SIG])
    verify_manifest_signature(cert, meta[MANIFEST_FILE], meta[MANIFEST_SIG])
    found = bundle_kind(meta[MANIFEST_FILE])
    if found is not kind:
        raise BundleRejectedError(
            RejectCode.E_FORMAT, f"{MANIFEST_FILE}: expected a {kind} bundle, got kind {found}"
        )
    return meta, cert


def _verify(path: Path, ctx: VerifyContext, extract_to: Path | None) -> VerifiedBundle:
    started = time.monotonic()
    with open_pib(path) as reader:
        meta, cert = _signed_metadata(reader, ctx.root_pubkey, BundleKind.SITE)
        manifest = parse_manifest(meta[MANIFEST_FILE])
        decision = check_validity(
            root_pubkey=ctx.root_pubkey,
            cert=cert,
            created_at=manifest.created_at,
            state=ctx.trust,
            trust_update=manifest.trust_update,
            trust_update_sig=meta.get(TRUST_UPDATE_SIG),
        )
        # ② site
        if manifest.site_id != ctx.site_id:
            raise BundleRejectedError(
                RejectCode.E_SITE, f"bundle is for site {manifest.site_id!r}, not {ctx.site_id!r}"
            )
        # ③ generation
        generation = decide_generation(manifest.generation, ctx.generation)
        if generation is GenDecision.NO_OP:
            return VerifiedBundle(
                manifest, cert, generation, None, decision, {}, time.monotonic() - started
            )
        # ④ payload sha256(해시 먼저) → 추출본의 이미지 config digest(해석은 나중)
        images = _image_payloads(manifest)
        files, _ = _check_payload(reader, manifest.files, extract_to)
    if extract_to is not None:
        _check_image_digests(manifest, images, extract_to)
    # ⑤ 스키마 envelope + Updater 버전
    schema = decide_schema(manifest.product_release.schema, ctx.schema_epoch)
    if ctx.updater_version is not None:
        check_updater_version(manifest.product_release.min_updater, ctx.updater_version)
    # ⑥ 드래프트 게이트
    check_draft_gate(manifest.site_id, manifest.project.absorbs, ctx.draft)
    return VerifiedBundle(
        manifest, cert, generation, schema, decision, files, time.monotonic() - started
    )


def _deb_package(name: str) -> str | None:
    if name.startswith("debs/") and name.endswith(".deb"):
        return name[len("debs/") : -len(".deb")]
    return None


def _check_install_layout(manifest: InstallManifest) -> None:
    """설치 배치: 필수 payload·Updater tarball·Docker deb가 서명된 `files`에 있는지(`E_FORMAT`)."""
    required = (*INSTALL_LAYOUT, manifest.updater_file, *(f"debs/{p}.deb" for p in DOCKER_PACKAGES))
    missing = [name for name in required if name not in manifest.files]
    if missing:
        raise BundleRejectedError(
            RejectCode.E_FORMAT, f"{MANIFEST_FILE}: install bundle lacks payload {missing}"
        )
    packages = {_deb_package(name) for name in manifest.files} - {None}
    if packages != set(manifest.docker_packages):
        raise BundleRejectedError(
            RejectCode.E_FORMAT,
            f"{MANIFEST_FILE}: debs/*.deb {sorted(p for p in packages if p)} != "
            f"docker.packages {sorted(manifest.docker_packages)}",
        )


def _check_install_keys(root_pubkey: bytes, captured: Mapping[str, bytes]) -> None:
    """번들이 심을 root 공개키(raw·PEM)가 이 번들을 검증한 root와 같아야 한다(`E_SIG`)."""
    try:
        pem = keys.import_pem(captured[ROOT_PEM_PAYLOAD].decode("ascii"))
    except (UnicodeDecodeError, keys.KeyFormatError) as exc:
        raise BundleRejectedError(RejectCode.E_FORMAT, f"{ROOT_PEM_PAYLOAD}: {exc}") from exc
    for name, value in ((ROOT_PUB_PAYLOAD, captured[ROOT_PUB_PAYLOAD]), (ROOT_PEM_PAYLOAD, pem)):
        if value != root_pubkey:
            raise BundleRejectedError(
                RejectCode.E_SIG, f"{name} is not the root key that verified this bundle"
            )


def _verify_install(
    path: Path, root_pubkey: bytes, trust: TrustState, extract_to: Path | None
) -> VerifiedInstall:
    started = time.monotonic()
    with open_pib(path) as reader:
        meta, cert = _signed_metadata(reader, root_pubkey, BundleKind.INSTALL)
        manifest = parse_install_manifest(meta[MANIFEST_FILE])
        decision = check_validity(
            root_pubkey=root_pubkey,
            cert=cert,
            created_at=manifest.created_at,
            state=trust,
            trust_update=manifest.trust_update,
            trust_update_sig=meta.get(TRUST_UPDATE_SIG),
        )
        _check_install_layout(manifest)
        files, captured = _check_payload(
            reader, manifest.files, extract_to, frozenset({ROOT_PUB_PAYLOAD, ROOT_PEM_PAYLOAD})
        )
    _check_install_keys(root_pubkey, captured)
    return VerifiedInstall(manifest, cert, decision, files, time.monotonic() - started)


def _with_extract_dir[T](extract_to: Path | None, run: Callable[[Path | None], T]) -> T:
    """`extract_to`를 준비(비어 있음·링크 아님)하고, 거부·예외 시 내용을 지운다."""
    if extract_to is None:
        return run(None)
    _prepare_extract_dir(extract_to)
    try:
        return run(extract_to)
    except BaseException:
        _clear_dir(extract_to)
        raise


def verify_install_bundle(
    path: Path,
    root_pubkey: bytes,
    trust: TrustState | None = None,
    *,
    extract_to: Path | None = None,
) -> VerifiedInstall:
    """설치 번들(`kind: install`) 검증: ① 서명·cert(`trust` 기준, 기본 = 새 호스트) → 설치 배치
    → ④ payload sha256 → 번들 root 키(raw·PEM) == 검증 root. 실패는 `BundleRejectedError`.

    `extract_to`의 의미·정리 규칙은 `verify_bundle`과 같다.
    """
    state = trust or TrustState()
    return _with_extract_dir(extract_to, lambda d: _verify_install(path, root_pubkey, state, d))


def verify_bundle(
    path: Path, ctx: VerifyContext, *, extract_to: Path | None = None
) -> VerifiedBundle:
    """`.pib`를 계약 순서대로 검증한다. 실패는 `BundleRejectedError`.

    `extract_to`(비어 있는 디렉터리, 링크 불가)를 주면 payload를 해시하며 그 아래에만 풀어 쓰고
    (`<name>.part` → sha256 확인 후 이름 변경), 이미지 config digest를 추출본에서 확인한다.
    거부·예외 시 `extract_to` 내용을 모두 지운다. 적용(`docker load` 등)은 반드시 이 추출본으로
    하고 `.pib`를 다시 열지 않는다(검증과 적용 사이 바꿔치기 방지).

    `extract_to` 없이 부르면 이미지 tar는 해석하지 않는다 — 그때 이미지 신원 확인은 Updater의
    load 후 `check_loaded_image`(`.Id` 대조)뿐이다.
    """
    if ctx.updater_version is not None:
        parse_semver(ctx.updater_version)
    return _with_extract_dir(extract_to, lambda d: _verify(path, ctx, d))
