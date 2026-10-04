"""`.pib` 검증 단일 구현(ADR-023, P3, plan §6.2).

검사 순서는 계약으로 고정이다:
① 서명·cert(`E_SIG`/`E_CERT`) ② site_id(`E_SITE`) ③ generation(`E_GEN`)
④ payload sha256·누락·초과·이미지 config digest(`E_DIGEST`) ⑤ 스키마 envelope(`E_SCHEMA`)·
`min_updater`(`E_UPDATER`) ⑥ 드래프트 게이트(`E_DRAFT`).

메타데이터는 payload를 해시하기 전에 읽고 서명을 확인한다. gen 판정이 NO_OP(이미 적용)이면
④~⑥은 하지 않는다 — 적용할 것이 없으므로 payload·스키마·드래프트는 판단 대상이 아니다.
판정 규칙(gen·스키마·드래프트·Updater 버전)은 Updater가 재검사에 쓰도록 순수 함수로 둔다.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pureiron_bundle.errors import BundleRejected, RejectCode
from pureiron_bundle.images import ImageFormatError, config_digest_from_stream
from pureiron_bundle.manifest import MANIFEST_FILE, Manifest, SchemaEnvelope, parse_manifest
from pureiron_bundle.pib import (
    CERT_FILE,
    CERT_SIG,
    MANIFEST_SIG,
    TRUST_UPDATE_SIG,
    HashingReader,
    PibReader,
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

_IMAGE_PAYLOAD = re.compile(r"^images/([a-z][a-z0-9-]{0,31})\.tar(\.gz)?$")
_SEMVER = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")


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


def decide_generation(gen: int, state: GenState) -> GenDecision:
    """plan M2 gen 규칙. 거부는 `E_GEN`."""
    known = [g for g in (state.applied_gen, state.last_attempted_gen) if g is not None]
    if known and gen < max(known):
        raise BundleRejected(RejectCode.E_GEN, f"generation {gen} < {max(known)}")
    if gen == state.applied_gen:
        return GenDecision.NO_OP
    if gen == state.last_attempted_gen:
        if state.last_result is AttemptResult.PENDING:
            return GenDecision.RESUME
        raise BundleRejected(
            RejectCode.E_GEN,
            f"generation {gen} already attempted ({state.last_result}) — not retried",
        )
    return GenDecision.APPLY


def decide_schema(envelope: SchemaEnvelope, current: int | None) -> SchemaDecision:
    """plan §6.6 Updater 판정. 거부는 `E_SCHEMA`."""
    expected, max_compatible = envelope.app_expected, envelope.app_max_compatible
    if max_compatible != expected + 1 or envelope.target != expected:
        raise BundleRejected(
            RejectCode.E_SCHEMA,
            f"inconsistent schema envelope target={envelope.target} "
            f"app_expected={expected} app_max_compatible={max_compatible}",
        )
    if current is None:
        return SchemaDecision(SchemaAction.MIGRATE, 0, envelope.target)
    if current > max_compatible:
        raise BundleRejected(
            RejectCode.E_SCHEMA, f"schema epoch {current} > app_max_compatible {max_compatible}"
        )
    if expected - current > 1:
        raise BundleRejected(
            RejectCode.E_SCHEMA,
            f"schema epoch {current} → {expected} skips epochs (apply stepwise releases)",
        )
    if expected - current == 1:
        return SchemaDecision(SchemaAction.MIGRATE, current, envelope.target)
    return SchemaDecision(SchemaAction.NO_MIGRATE, current, None)


def parse_semver(text: str) -> tuple[int, int, int]:
    match = _SEMVER.match(text)
    if match is None:
        raise ValueError(f"not a MAJOR.MINOR.PATCH version: {text!r}")
    return int(match[1]), int(match[2]), int(match[3])


def check_updater_version(min_updater: str, updater_version: str) -> None:
    if parse_semver(min_updater) > parse_semver(updater_version):
        raise BundleRejected(
            RejectCode.E_UPDATER, f"bundle needs updater >= {min_updater}, have {updater_version}"
        )


def check_draft_gate(site_id: str, absorbs: Mapping[str, int], draft: DraftState | None) -> None:
    """드래프트 게이트(data-contracts). Updater가 lock 시점에 같은 함수로 재검사한다."""
    if draft is None:
        return
    absorbed = absorbs.get(site_id)
    if absorbed is not None:
        if draft.site_rev > absorbed:
            raise BundleRejected(
                RejectCode.E_DRAFT,
                f"site_rev {draft.site_rev} has drafts not absorbed (absorbs {absorbed})",
            )
    elif draft.site_rev != draft.base_site_rev:
        raise BundleRejected(
            RejectCode.E_DRAFT,
            f"site_rev {draft.site_rev} != base_site_rev {draft.base_site_rev} "
            "(unabsorbed site drafts)",
        )


def _image_payloads(manifest: Manifest) -> dict[str, str]:
    """payload 경로 → 서비스. 이미지마다 payload가 정확히 하나여야 한다(`E_DIGEST`)."""
    found: dict[str, str] = {}
    for name in manifest.files:
        match = _IMAGE_PAYLOAD.match(name)
        if match is not None:
            found[name] = match[1]
    images = manifest.product_release.images
    for name, service in found.items():
        if service not in images:
            raise BundleRejected(
                RejectCode.E_DIGEST, f"{name}: no product_release.images entry for {service!r}"
            )
    for service in images:
        count = sum(1 for svc in found.values() if svc == service)
        if count != 1:
            raise BundleRejected(
                RejectCode.E_DIGEST, f"image {service!r} needs exactly one payload, found {count}"
            )
    return found


def _check_payload(
    reader: PibReader, manifest: Manifest, extract_to: Path | None
) -> dict[str, str]:
    image_payloads = _image_payloads(manifest)
    expected = dict(manifest.files)
    verified: dict[str, str] = {}
    for member in reader.iter_payload():
        want = expected.pop(member.name, None)
        if want is None:
            raise BundleRejected(RejectCode.E_DIGEST, f"{member.name}: not listed in manifest")
        sink = None
        if extract_to is not None:
            try:
                sink = safe_target(extract_to, member.name).open("xb")
            except FileExistsError as exc:
                raise BundleRejected(
                    RejectCode.E_FORMAT, f"{member.name}: path collides on extraction"
                ) from exc
        service = image_payloads.get(member.name)
        digest: str | None = None
        problem = ""
        try:
            hashing = HashingReader(member.stream, sink)
            if service is not None:
                try:
                    digest = config_digest_from_stream(hashing, member.name)
                except ImageFormatError as exc:
                    problem = str(exc)
            actual = hashing.drain()
        finally:
            if sink is not None:
                sink.close()
        if actual != want:
            raise BundleRejected(RejectCode.E_DIGEST, f"{member.name}: sha256 {actual} != {want}")
        if service is not None:
            recorded = manifest.product_release.images[service].config_digest
            if digest != recorded:
                raise BundleRejected(
                    RejectCode.E_DIGEST,
                    f"{member.name}: config digest {digest or problem} != manifest {recorded}",
                )
        verified[member.name] = actual
    if expected:
        raise BundleRejected(RejectCode.E_DIGEST, f"missing payload files {sorted(expected)}")
    return verified


def _prepare_extract_dir(extract_to: Path) -> None:
    extract_to.mkdir(parents=True, exist_ok=True)
    if any(extract_to.iterdir()):
        raise ValueError(f"extract directory must be empty: {extract_to}")


def verify_bundle(
    path: Path, ctx: VerifyContext, *, extract_to: Path | None = None
) -> VerifiedBundle:
    """`.pib`를 계약 순서대로 검증한다. 실패는 `BundleRejected`.

    `extract_to`(비어 있는 디렉터리)를 주면 payload를 해시하면서 그 아래에만 풀어 쓴다.
    거부되면 풀린 파일은 신뢰할 수 없으므로 호출자가 디렉터리를 지운다.
    """
    started = time.monotonic()
    if extract_to is not None:
        _prepare_extract_dir(extract_to)
    with open_pib(path) as reader:
        meta = reader.read_metadata()
        # ① root → cert → manifest, cert 유효성은 서명된 created_at 기준
        cert = verify_cert(ctx.root_pubkey, meta[CERT_FILE], meta[CERT_SIG])
        verify_manifest_signature(cert, meta[MANIFEST_FILE], meta[MANIFEST_SIG])
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
            raise BundleRejected(
                RejectCode.E_SITE, f"bundle is for site {manifest.site_id!r}, not {ctx.site_id!r}"
            )
        # ③ generation
        generation = decide_generation(manifest.generation, ctx.generation)
        if generation is GenDecision.NO_OP:
            return VerifiedBundle(
                manifest, cert, generation, None, decision, {}, time.monotonic() - started
            )
        # ④ payload sha256 + 이미지 config digest
        files = _check_payload(reader, manifest, extract_to)
    # ⑤ 스키마 envelope + Updater 버전
    schema = decide_schema(manifest.product_release.schema, ctx.schema_epoch)
    if ctx.updater_version is not None:
        check_updater_version(manifest.product_release.min_updater, ctx.updater_version)
    # ⑥ 드래프트 게이트
    check_draft_gate(manifest.site_id, manifest.project.absorbs, ctx.draft)
    return VerifiedBundle(
        manifest, cert, generation, schema, decision, files, time.monotonic() - started
    )
