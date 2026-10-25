"""현장 드래프트 파일 `pureiron-site project export --out draft.tar`(plan M5-2 P6, D9).

순서: 등록(`enroll`) 확인 → 적용된 generation의 core 이미지로 `project export --draft`(쓰기 mount,
한 스냅샷의 유효 트리 + `draft-meta.json`) → payload `{site_id, site_rev, base_site_rev,
content_hash, exported_at, project_tar_sha256}`를 site key로 서명(purpose `site-draft`) →
`draft.tar` = `draft.json` + `project.tar`(결정적 tar, `pureiron_bundle.sitesign.pack_draft`).
벤더는 등록된 site 공개키로 `open_draft` 검증 뒤 absorb 한다(M5-3).

- 등록 전·키 불일치·적용된 generation 없음은 core를 부르기 전에 거부한다.
- core 출력 디렉터리는 컨테이너가 쓴 것이라 백업과 같은 규칙으로 읽는다(`backup.read_regular`:
  링크를 따르지 않는 일반 파일만, 정확히 두 파일). 끝나면 지운다.
- `site_rev == base_site_rev`(흡수할 드래프트 없음)여도 파일은 만든다 — 호출자가 안내한다.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from pureiron_bundle import keys
from pureiron_bundle.manifest import Json
from pureiron_bundle.sitesign import SitePurpose, dumps, pack_draft, sign_document, tree_digest
from pureiron_bundle.trust import format_timestamp
from pureiron_updater.backup import BackupError, read_regular, restrict
from pureiron_updater.compose import target_dir
from pureiron_updater.enroll import IDENTITY_FILE, EnrollError, load_identity
from pureiron_updater.ports import CommandError, Compose
from pureiron_updater.settings import Settings
from pureiron_updater.state import StateStore, atomic_write

WORK_DIR = "draft-export"
"""`state/` 아래 core 출력 디렉터리(컨테이너 uid 소유 0700, 끝나면 지운다)."""
TREE_FILE = "project.tar"
META_FILE = "draft-meta.json"
_CONTAINER_OUT = "/run/pureiron-draft-out"


class DraftError(Exception):
    """드래프트 파일을 만들지 못했다(core 실패·출력 형식 위반). 아무 파일도 남기지 않는다."""


@dataclass(frozen=True, slots=True)
class DraftExport:
    path: Path
    payload: dict[str, Json]

    @property
    def has_drafts(self) -> bool:
        return self.payload["site_rev"] != self.payload["base_site_rev"]


def _site_seed(settings: Settings) -> tuple[str, bytes]:
    identity = load_identity(settings.state_dir)
    if identity is None:
        raise EnrollError("not enrolled (run: pureiron-site enroll --site <id>)")
    seed = keys.read_private_key(settings.site_key_file)
    if keys.fingerprint(keys.public_key(seed)) != identity.fingerprint:
        raise EnrollError(f"{settings.site_key_file} does not match {IDENTITY_FILE}")
    return identity.site_id, seed


def _read_output(work: Path) -> tuple[dict[str, object], bytes]:
    names = sorted(entry.name for entry in os.scandir(work))
    if names != sorted((META_FILE, TREE_FILE)):
        raise DraftError(f"project export --draft wrote {names}, expected {META_FILE}, {TREE_FILE}")
    try:
        meta: object = json.loads(read_regular(work / META_FILE))
        tree = read_regular(work / TREE_FILE)
    except (BackupError, ValueError) as exc:
        raise DraftError(f"project export --draft output is unreadable: {exc}") from exc
    if not isinstance(meta, dict):
        raise DraftError(f"{META_FILE} is not a JSON object")
    return cast(dict[str, object], meta), tree


def _payload(site_id: str, meta: dict[str, object], tree: bytes, now: datetime) -> dict[str, Json]:
    site_rev, base, content_hash = (
        meta.get("site_rev"),
        meta.get("base_site_rev"),
        meta.get("content_hash"),
    )
    if type(site_rev) is not int or type(base) is not int or not isinstance(content_hash, str):
        raise DraftError(f"{META_FILE} is malformed: {meta!r}")
    return {
        "site_id": site_id,
        "site_rev": site_rev,
        "base_site_rev": base,
        "content_hash": content_hash,
        "exported_at": format_timestamp(now),
        "project_tar_sha256": tree_digest(tree),
    }


def export_draft(settings: Settings, compose: Compose, out: Path, *, now: datetime) -> DraftExport:
    site_id, seed = _site_seed(settings)
    state = StateStore(settings.state_dir).load()
    if state.applied_gen is None:
        raise DraftError("no applied generation yet — nothing to export")
    work = settings.state_dir / WORK_DIR
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        restrict(work, 0o700, settings.secret_uid)
        try:
            compose.run_core(
                target_dir(settings.compose_dir, state.applied_gen),
                ["project", "export", "--draft", "--out", _CONTAINER_OUT],
                writable={work: _CONTAINER_OUT},
            )
        except CommandError as exc:
            raise DraftError(f"core project export --draft failed: {exc}") from exc
        meta, tree = _read_output(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    payload = _payload(site_id, meta, tree, now)
    envelope = dumps(sign_document(seed, SitePurpose.DRAFT, payload))
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, pack_draft(envelope, tree))
    return DraftExport(out, payload)
