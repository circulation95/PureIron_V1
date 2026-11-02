"""manifest.json 해석·계약 검증(plan §6.2, `packages/contracts/*manifest.schema.json`).

`.pib`는 두 종류다: 현장 번들(`kind: site`)과 설치 번들(`kind: install`, M2c). 같은 형식·서명
체인이고 manifest 계약만 다르다. 서명 확인 뒤 `bundle_kind`로 종류를 보고 맞는 계약으로 해석한다.

스키마 정본은 `packages/contracts/`이고, 패키지 안 `schema/*.schema.json`은
`scripts/g1.sh contracts-gen`이 복사한 생성물이다(contracts-drift가 동기 여부를 검사).
manifest의 `$ref: fleet-site.schema.json#…`는 패키지 안 사본으로 오프라인 해석한다.

빌더는 manifest를 JCS(RFC 8785) 바이트로 쓰고 그 바이트에 서명한다. 검증은 서명된 바이트를
그대로 확인한 뒤에만 해석한다(재직렬화하지 않는다).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any, Protocol, cast

import rfc8785
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from pureiron_bundle.errors import BundleRejectedError, RejectCode
from pureiron_bundle.trust import loads_strict, parse_timestamp

type Json = bool | int | float | str | list[Json] | dict[str, Json] | None

MANIFEST_FILE = "manifest.json"
SITE_IMAGES = ("core", "collector", "web", "site-db")
DOCKER_PACKAGES = ("containerd.io", "docker-ce", "docker-ce-cli", "docker-compose-plugin")


class BundleKind(StrEnum):
    SITE = "site"
    INSTALL = "install"


class SchemaName(StrEnum):
    MANIFEST = "manifest.schema.json"
    INSTALL_MANIFEST = "install-manifest.schema.json"
    FLEET_SITE = "fleet-site.schema.json"


class _Validator(Protocol):
    def iter_errors(self, instance: object) -> Iterator[_ValidationError]: ...


class _ValidationError(Protocol):
    @property
    def message(self) -> str: ...

    @property
    def json_path(self) -> str: ...


@cache
def contract_schema(name: SchemaName) -> Mapping[str, object]:
    text = resources.files("pureiron_bundle").joinpath(f"schema/{name.value}").read_text("utf-8")
    return cast(Mapping[str, object], json.loads(text))


@cache
def _validator(name: SchemaName) -> _Validator:
    registry = Registry[Any]()
    for each in SchemaName:
        schema = contract_schema(each)
        registry = registry.with_resource(str(schema["$id"]), Resource.from_contents(schema))
    return cast(_Validator, Draft202012Validator(contract_schema(name), registry=registry))


def _control_chars(value: object, path: str) -> Iterator[str]:
    """제어 문자가 든 문자열·키. JSON Schema `pattern`은 `re.search`라 `…$`가 끝 줄바꿈을
    허용하므로(예: `"site-a\\n"`), 계약 문서에서는 제어 문자 자체를 거부한다."""
    if isinstance(value, str):
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            yield f"{path}: control characters are not allowed"
    elif isinstance(value, dict):
        for key, item in cast(dict[str, object], value).items():
            yield from _control_chars(key, f"{path}.<key>")
            yield from _control_chars(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(cast(list[object], value)):
            yield from _control_chars(item, f"{path}[{i}]")


def validate(name: SchemaName, document: object) -> list[str]:
    """스키마 위반·제어 문자 메시지(`<json path>: <message>`), 정렬."""
    problems = {f"{e.json_path}: {e.message}" for e in _validator(name).iter_errors(document)}
    problems.update(_control_chars(document, "$"))
    return sorted(problems)


@cache
def file_path_pattern() -> re.Pattern[str]:
    """manifest `files` 키(번들 안 payload 경로) 패턴 — 계약 스키마에서 읽어 동기화를 유지한다."""
    schema = contract_schema(SchemaName.MANIFEST)
    properties = cast(Mapping[str, Mapping[str, Mapping[str, str]]], schema["properties"])
    return re.compile(properties["files"]["propertyNames"]["pattern"])


def path_collision(names: Iterable[str]) -> str | None:
    """추출 시 충돌하는 경로 쌍 설명(없으면 None).

    한 경로가 다른 경로의 디렉터리 접두(`a`와 `a/b`)이거나, 대소문자를 무시하면 같은
    경로(`A`와 `a`)면 대소문자 무시 파일시스템·디렉터리 생성에서 서로 덮어쓴다.
    """
    folded: dict[str, str] = {}
    for name in names:
        key = name.casefold()
        if key in folded:
            return f"payload paths {folded[key]!r} and {name!r} collide (case-insensitive)"
        folded[key] = name
    for key, name in folded.items():
        parts = key.split("/")
        for i in range(1, len(parts)):
            prefix = "/".join(parts[:i])
            if prefix in folded:
                return f"payload path {folded[prefix]!r} is a directory prefix of {name!r}"
    return None


@cache
def release_version_pattern() -> re.Pattern[str]:
    """제품 달력 버전(`YYYY.M.N`) 패턴 — fleet-site 계약 `$defs/release`."""
    schema = contract_schema(SchemaName.FLEET_SITE)
    defs = cast(Mapping[str, Mapping[str, str]], schema["$defs"])
    return re.compile(defs["release"]["pattern"])


@dataclass(frozen=True, slots=True)
class ImageRef:
    config_digest: str
    tag: str
    registry_digest: str | None = None
    upstream: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaEnvelope:
    target: int
    app_expected: int
    app_max_compatible: int
    compat_app_min: str


@dataclass(frozen=True, slots=True)
class ProductRelease:
    version: str
    min_updater: str
    images: Mapping[str, ImageRef]
    schema: SchemaEnvelope


@dataclass(frozen=True, slots=True)
class ProjectRef:
    project_id: str
    version: str
    content_hash: str
    absorbs: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class SiteConfig:
    """manifest `site_config`(M4-1 C2, 선택 — 없으면 기본값). 서명 대상(현장 파일로 못 바꾼다)."""

    writes_enabled: bool = False
    """core 쓰기 스위치(`PUREIRON_WRITES_ENABLED`). Updater가 generation `.env`에 늘 쓴다."""


@dataclass(frozen=True, slots=True)
class Manifest:
    site_id: str
    generation: int
    created_at: datetime
    product_release: ProductRelease
    project: ProjectRef
    window: Json
    files: Mapping[str, str]
    trust_update: int | None
    fleet_commit: str
    document: Mapping[str, Json]
    """계약 검증을 통과한 원본 문서(보고·저장용)."""
    site_config: SiteConfig = SiteConfig()


@dataclass(frozen=True, slots=True)
class InstallManifest:
    """설치 번들 manifest(`kind: install`)."""

    release: str
    created_at: datetime
    updater_version: str
    updater_file: str
    docker_packages: Mapping[str, str]
    compose_min: str
    files: Mapping[str, str]
    trust_update: int | None
    document: Mapping[str, Json]


def manifest_bytes(document: Mapping[str, Json]) -> bytes:
    """서명 대상 바이트 = JCS(RFC 8785)."""
    return rfc8785.dumps(dict(document))


def _image(data: Mapping[str, str]) -> ImageRef:
    return ImageRef(
        config_digest=data["config_digest"],
        tag=data["tag"],
        registry_digest=data.get("registry_digest"),
        upstream=data.get("upstream"),
    )


def _format_error(detail: str) -> BundleRejectedError:
    return BundleRejectedError(RejectCode.E_FORMAT, f"{MANIFEST_FILE}: {detail}")


def _load_json(data: bytes) -> object:
    try:
        return loads_strict(data)
    except (UnicodeDecodeError, ValueError) as exc:
        raise _format_error(f"invalid JSON ({exc})") from exc


def bundle_kind(data: bytes) -> BundleKind:
    """서명 확인이 끝난 manifest 바이트의 `kind`(계약 검증 전 분기용). 모르는 값은 `E_FORMAT`."""
    document = _load_json(data)
    kind = cast(dict[str, object], document).get("kind") if isinstance(document, dict) else None
    try:
        return BundleKind(str(kind))
    except ValueError as exc:
        raise _format_error(f"unknown bundle kind {kind!r}") from exc


def _checked_document(data: bytes, schema: SchemaName) -> tuple[dict[str, Json], datetime]:
    """계약 검증 + payload 경로 충돌 + created_at 해석."""
    document = _load_json(data)
    problems = validate(schema, document)
    if problems:
        raise _format_error("; ".join(problems))
    doc = cast(dict[str, Json], document)
    collision = path_collision(cast(dict[str, str], doc["files"]))
    if collision:
        raise _format_error(collision)
    try:
        created_at = parse_timestamp(cast(str, doc["created_at"]))
    except ValueError as exc:
        raise _format_error(f"created_at {exc}") from exc
    return doc, created_at


def _trust_update(doc: Mapping[str, Json]) -> int | None:
    trust_update = cast(dict[str, int] | None, doc.get("trust_update"))
    return None if trust_update is None else trust_update["min_cert_serial"]


def parse_install_manifest(data: bytes) -> InstallManifest:
    """서명 확인이 끝난 설치 번들 manifest 바이트 → 계약 검증 → 타입 접근자. 위반은 `E_FORMAT`."""
    doc, created_at = _checked_document(data, SchemaName.INSTALL_MANIFEST)
    updater = cast(dict[str, str], doc["updater"])
    docker = cast(dict[str, Json], doc["docker"])
    return InstallManifest(
        release=cast(str, doc["release"]),
        created_at=created_at,
        updater_version=updater["version"],
        updater_file=updater["file"],
        docker_packages=cast(dict[str, str], docker["packages"]),
        compose_min=cast(str, docker["compose_min"]),
        files=cast(dict[str, str], doc["files"]),
        trust_update=_trust_update(doc),
        document=doc,
    )


def parse_manifest(data: bytes) -> Manifest:
    """서명 확인이 끝난 manifest 바이트 → 계약 검증 → 타입 접근자. 위반은 `E_FORMAT`."""
    doc, created_at = _checked_document(data, SchemaName.MANIFEST)
    release = cast(dict[str, Json], doc["product_release"])
    project = cast(dict[str, Json], doc["project"])
    schema = cast(dict[str, Json], release["schema"])
    images = cast(dict[str, dict[str, str]], release["images"])
    return Manifest(
        site_id=cast(str, doc["site_id"]),
        generation=cast(int, doc["generation"]),
        created_at=created_at,
        product_release=ProductRelease(
            version=cast(str, release["version"]),
            min_updater=cast(str, release["min_updater"]),
            images={name: _image(image) for name, image in images.items()},
            schema=SchemaEnvelope(
                target=cast(int, schema["target"]),
                app_expected=cast(int, schema["app_expected"]),
                app_max_compatible=cast(int, schema["app_max_compatible"]),
                compat_app_min=cast(str, schema["compat_app_min"]),
            ),
        ),
        project=ProjectRef(
            project_id=cast(str, project["project_id"]),
            version=cast(str, project["version"]),
            content_hash=cast(str, project["content_hash"]),
            absorbs=cast(dict[str, int], project.get("absorbs", {})),
        ),
        window=doc["window"],
        files=cast(dict[str, str], doc["files"]),
        trust_update=_trust_update(doc),
        fleet_commit=cast(str, doc["fleet_commit"]),
        document=doc,
        site_config=_site_config(doc),
    )


def _site_config(doc: Mapping[str, Json]) -> SiteConfig:
    config = cast(dict[str, Json] | None, doc.get("site_config"))
    return (
        SiteConfig()
        if config is None
        else SiteConfig(writes_enabled=config["writes_enabled"] is True)
    )
