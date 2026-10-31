"""absorb·gate 테스트용 현장 문서: enrollment 봉투·`draft.tar`(Updater `enroll`·`export_draft`와
같은 형식 — 같은 `sign_document`·`pack_draft`)."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Mapping
from pathlib import Path

from pureiron_bundle import keys
from pureiron_bundle.pib import tar_info
from pureiron_bundle.sitesign import (
    Json,
    SitePurpose,
    dumps,
    pack_draft,
    sign_document,
    tree_digest,
)
from pureiron_project.canonical import dump
from pureiron_project.project import load_project


def enrollment(seed: bytes, site_id: str) -> bytes:
    public = keys.public_key(seed)
    payload: dict[str, Json] = {
        "site_id": site_id,
        "public_key": public.hex(),
        "fingerprint": keys.fingerprint(public),
        "next_report_seq": 1,
        "seq_pending": False,
        "created_at": "2026-10-30T00:00:00Z",
        "updater_version": "0.1.0",
    }
    return dumps(sign_document(seed, SitePurpose.ENROLLMENT, payload))


def tree_tar(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(files, key=lambda r: r.encode("utf-8")):
            tar.addfile(tar_info(rel, len(files[rel])), io.BytesIO(files[rel]))
    return buffer.getvalue()


def project_tar(root: Path) -> tuple[bytes, str]:
    """트리 디렉터리 → (core export와 같은 정규형 project.tar, content_hash)."""
    project = load_project(root)
    assert project.ok, project.problems
    files = {f.relpath: dump(f.data).encode("utf-8") for f in project.files}
    return tree_tar(files), project.content_hash()


def draft(
    seed: bytes,
    site_id: str,
    tree: bytes,
    content_hash: str,
    *,
    site_rev: int,
    base_site_rev: int,
    **overrides: Json,
) -> bytes:
    payload: dict[str, Json] = {
        "site_id": site_id,
        "site_rev": site_rev,
        "base_site_rev": base_site_rev,
        "content_hash": content_hash,
        "exported_at": "2026-10-30T00:00:00Z",
        "project_tar_sha256": tree_digest(tree),
        **overrides,
    }
    return pack_draft(dumps(sign_document(seed, SitePurpose.DRAFT, payload)), tree)


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }
