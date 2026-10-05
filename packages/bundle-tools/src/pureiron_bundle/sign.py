"""서명 번들 생성(CI·빌더 쪽). 검증(`verify.py`)과 같은 형식 코드(`pib`·`manifest`·`trust`)를 쓴다.

manifest 문서(`files` 제외)와 payload 파일을 받아 각 파일 sha256을 스트리밍으로 계산해 `files`를
채우고, 계약 검증 → JCS 바이트 → release key 서명 → 결정적 `.pib` 쓰기 순으로 진행한다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from pureiron_bundle import keys
from pureiron_bundle.manifest import (
    MANIFEST_FILE,
    BundleKind,
    Json,
    SchemaName,
    manifest_bytes,
    validate,
)
from pureiron_bundle.pib import (
    CERT_FILE,
    CERT_SIG,
    CHUNK,
    MANIFEST_SIG,
    TRUST_UPDATE_SIG,
    check_payload_names,
    write_pib,
)
from pureiron_bundle.trust import parse_cert


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def write_signed_bundle(
    out: Path,
    document: Mapping[str, Json],
    payload: Mapping[str, Path],
    *,
    release_seed: bytes,
    cert_json: bytes,
    cert_sig: bytes,
    trust_update_sig: bytes | None = None,
) -> bytes:
    """`.pib`를 쓰고 서명한 manifest 바이트를 돌려준다.

    `document`에 `files`가 있으면 무시하고 payload에서 다시 계산한다. release key가 cert의
    `release_pubkey`와 다르거나 manifest가 계약에 맞지 않으면 쓰기 전에 `ValueError`.
    """
    cert = parse_cert(cert_json)
    if keys.public_key(release_seed) != cert.release_pubkey:
        raise ValueError("release key does not match release-cert.json release_pubkey")
    check_payload_names(payload)
    files: dict[str, Json] = {name: file_sha256(path) for name, path in payload.items()}
    full: dict[str, Json] = {**document, "files": files}
    schema = (
        SchemaName.INSTALL_MANIFEST
        if full.get("kind") == BundleKind.INSTALL
        else SchemaName.MANIFEST
    )
    problems = validate(schema, full)
    if problems:
        raise ValueError(f"{MANIFEST_FILE} violates the contract: " + "; ".join(problems))
    data = manifest_bytes(full)
    metadata = {
        MANIFEST_FILE: data,
        MANIFEST_SIG: keys.sign(release_seed, data),
        CERT_FILE: cert_json,
        CERT_SIG: cert_sig,
    }
    if trust_update_sig is not None:
        metadata[TRUST_UPDATE_SIG] = trust_update_sig
    write_pib(out, metadata, payload)
    return data
