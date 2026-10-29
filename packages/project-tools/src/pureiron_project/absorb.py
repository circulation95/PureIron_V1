"""`pureiron-project absorb` — 현장 드래프트(`draft.tar`)를 고객 repo 트리에 회수(M5-3a, ADR-025).

순서(전부 아니면 전무 — 거부되면 repo는 한 바이트도 바뀌지 않는다):

1. 현장 키: `--site-key <site-enrollment.json>`(Updater `enroll`이 쓴 자기 서명 봉투)를 검증하고
   payload `site_id`가 `--site`와 같은지 본다. 자기 서명이라 신뢰 근거는 파일 출처(설치 때 벤더가
   받아 둔 파일)다 — 공개키 fingerprint를 출력해 사람이 설치 기록과 대조한다(M4 fleet 등록부).
2. `open_draft`(서명·purpose·site_id·`project_tar_sha256`) → `project.tar`를 tarsafe로 읽어
   `check_texts`로 트리 검증 → 다시 계산한 content_hash가 payload 값과 같아야 한다.
3. repo 트리 검증, `project_id` 일치, absorbs 리비전 검사(`--rev` ≤ 서명된 site_rev, 퇴행 금지).
4. 필드 병합 계획(`merge.plan_merge`, 순수 함수): 드래프트의 알람·태그에서 현장 PATCH 허용
   필드만 꺼내 repo의 같은 `id`·`path` 항목에 덮는다. 드래프트에 없는 선택 필드 = 기본값(repo에서
   제거). 그 밖의 필드·repo에만 있는 항목(새 태그·주소·장비 등)은 그대로 둔다.
5. 바뀐 파일 + `project.yaml absorbs.<site> = N`을 정규형으로 쓴 결과 트리를 먼저 검증하고 쓴 뒤,
   디스크에서 다시 `load_project`로 확인한다(실패하면 원래 바이트로 되돌린다).

종료 코드 구분(K4): 1~3의 검증 실패는 `AbsorbError`(exit 1), 드래프트 항목이 repo에 없음·
`--rev` 초과·absorbs 퇴행은 사람이 판단할 충돌이라 `AbsorbConflictError`(exit 2).
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pureiron_bundle import keys
from pureiron_bundle.sitesign import SignedDocumentError, SitePurpose, open_document, open_draft
from pureiron_bundle.tarsafe import PARSER_EXHAUSTION, open_tar_stream
from pureiron_project.canonical import Json, dump, jcs
from pureiron_project.merge import FieldChange, plan_merge
from pureiron_project.project import (
    CONTENT_DIRS,
    PROJECT_FILE,
    Project,
    RelPathError,
    check_texts,
    load_project,
    normalize_relpath,
)
from pureiron_project.schema import Kind

ABSORBS = "absorbs"
TREE_LIMIT_FILES = 10_000
"""core `apply.TREE_LIMIT_FILES`와 같은 상한(바이트 상한은 `open_draft`의 드래프트 상한)."""
_DRAFT_ROOT = Path("<draft>")


class AbsorbError(Exception):
    """검증 실패(exit 1): 키·서명·tar·트리·hash·project_id."""


class AbsorbConflictError(Exception):
    """사람이 판단할 충돌(exit 2): repo에 없는 드래프트 항목·`--rev` 초과·absorbs 퇴행."""


# ── 드래프트 검증 ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SiteDraft:
    """검증된 현장 드래프트."""

    site_id: str
    fingerprint: str
    site_rev: int
    base_site_rev: int
    content_hash: str
    sha256: str
    """`draft.tar` 파일 바이트의 sha256(hex) — 커밋 trailer에 남긴다."""
    tree: Project
    """드래프트 유효 트리(검증 통과, `root`는 가상 경로)."""


def site_public_key(enrollment: bytes, site_id: str) -> tuple[bytes, str]:
    """enrollment 봉투(자기 서명) → (공개키, fingerprint). 현장이 다르면 거부."""
    try:
        payload = open_document(enrollment, SitePurpose.ENROLLMENT)
    except SignedDocumentError as exc:
        raise AbsorbError(f"site enrollment: {exc}") from exc
    if payload.get("site_id") != site_id:
        raise AbsorbError(
            f"site enrollment is for site {payload.get('site_id')!r}, not {site_id!r}"
        )
    # open_document가 public_key(hex, 32B)를 이미 확인했다.
    public = bytes.fromhex(cast(str, payload["public_key"]))
    fingerprint = keys.fingerprint(public)
    if payload.get("fingerprint") != fingerprint:
        raise AbsorbError("site enrollment fingerprint does not match its public_key")
    return public, fingerprint


def _kind_of(rel: str) -> Kind | None:
    if rel == PROJECT_FILE:
        return Kind.PROJECT
    directory, _, name = rel.partition("/")
    for content_dir, kind in CONTENT_DIRS:
        if directory == content_dir and "/" not in name and name.endswith(".yaml"):
            return kind
    return None


def _tree_member(member: tarfile.TarInfo, seen: Mapping[str, object]) -> tuple[str, Kind]:
    name = member.name
    if not member.isreg():
        raise AbsorbError(f"draft project.tar entry {name!r}: only regular files are allowed")
    try:
        rel = normalize_relpath(name)
    except RelPathError as exc:
        raise AbsorbError(f"draft project.tar entry {name!r}: {exc}") from None
    kind = _kind_of(rel)
    if rel != name or kind is None:
        raise AbsorbError(f"draft project.tar entry {name!r}: not a project file path")
    if rel in seen:
        raise AbsorbError(f"draft project.tar entry {name!r}: duplicate path")
    if len(seen) >= TREE_LIMIT_FILES:
        raise AbsorbError(f"draft project.tar has more than {TREE_LIMIT_FILES} files")
    return rel, kind


def read_tree(project_tar: bytes) -> list[tuple[str, Kind, str]]:
    """드래프트 `project.tar` → (상대 경로, 종류, 텍스트). 프로젝트 파일이 아닌 항목은 거부."""
    texts: dict[str, tuple[Kind, str]] = {}
    try:
        with open_tar_stream(io.BytesIO(project_tar)) as tar:
            for member in tar:
                rel, kind = _tree_member(member, texts)
                handle = tar.extractfile(member)
                if handle is None:
                    raise AbsorbError(f"draft project.tar entry {rel!r}: unreadable")
                texts[rel] = (kind, handle.read().decode("utf-8"))
    except AbsorbError:
        raise
    except UnicodeDecodeError as exc:
        raise AbsorbError(f"draft project.tar: file is not UTF-8 ({exc})") from exc
    except (tarfile.TarError, ValueError, *PARSER_EXHAUSTION) as exc:
        raise AbsorbError(f"draft project.tar is not a valid tar ({exc})") from exc
    return [(rel, kind, text) for rel, (kind, text) in texts.items()]


def open_site_draft(draft: bytes, enrollment: bytes, site_id: str) -> SiteDraft:
    """K1: enrollment로 얻은 현장 키로 `open_draft` → 트리 검증 → content_hash 재계산 일치."""
    public, fingerprint = site_public_key(enrollment, site_id)
    try:
        payload, project_tar = open_draft(draft, public, site_id=site_id)
    except SignedDocumentError as exc:
        raise AbsorbError(f"draft: {exc}") from exc
    tree = check_texts(_DRAFT_ROOT, read_tree(project_tar))
    if not tree.ok:
        raise AbsorbError("draft tree is invalid: " + "; ".join(tree.problems))
    if tree.content_hash() != payload["content_hash"]:
        raise AbsorbError(
            f"draft tree hash {tree.content_hash()} != payload.content_hash "
            f"{payload['content_hash']}"
        )
    return SiteDraft(
        site_id=site_id,
        fingerprint=fingerprint,
        site_rev=cast(int, payload["site_rev"]),
        base_site_rev=cast(int, payload["base_site_rev"]),
        content_hash=cast(str, payload["content_hash"]),
        sha256=hashlib.sha256(draft).hexdigest(),
        tree=tree,
    )


# ── repo 반영 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AbsorbResult:
    draft: SiteDraft
    changes: tuple[FieldChange, ...]
    written: tuple[str, ...]
    absorbs_before: int | None
    absorbs_after: int
    content_hash: str
    """회수 뒤 repo 트리 content_hash."""

    @property
    def trailer(self) -> str:
        draft = self.draft
        return f"Absorbs-Draft: {draft.site_id} rev={self.absorbs_after} sha256={draft.sha256}"


def _data(project: Project) -> dict[str, Json]:
    return {f.relpath: f.data for f in project.files}


def project_data(project: Project) -> dict[str, Json]:
    for f in project.files:
        if f.relpath == PROJECT_FILE and isinstance(f.data, dict):
            return f.data
    raise AbsorbError(f"{PROJECT_FILE}: missing")


def absorbs_of(project: Project) -> dict[str, int]:
    absorbs = project_data(project).get(ABSORBS)
    if not isinstance(absorbs, dict):
        return {}
    return {site: rev for site, rev in absorbs.items() if type(rev) is int}


def _target_rev(draft: SiteDraft, rev: int | None, existing: int | None) -> int:
    target = draft.site_rev if rev is None else rev
    if target < 0:
        raise AbsorbConflictError(f"--rev {target} must be >= 0")
    if target > draft.site_rev:
        raise AbsorbConflictError(f"--rev {target} is above the signed site_rev {draft.site_rev}")
    if existing is not None and existing > target:
        raise AbsorbConflictError(
            f"absorbs[{draft.site_id}] is already {existing} > {target} (absorbs cannot go back)"
        )
    return target


def _write_all(root: Path, contents: Mapping[str, bytes]) -> None:
    """파일마다 tmp → rename. 중간에 실패하면 이미 바꾼 파일을 원래 바이트로 되돌린다."""
    originals: dict[str, bytes] = {}
    try:
        for rel, content in contents.items():
            path = root.joinpath(*rel.split("/"))
            originals[rel] = path.read_bytes()
            tmp = path.with_name(path.name + ".absorb-tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
    except OSError as exc:
        for rel, content in originals.items():
            root.joinpath(*rel.split("/")).write_bytes(content)
        raise AbsorbError(f"cannot write the project tree: {exc}") from exc


def absorb(
    root: Path, draft_bytes: bytes, enrollment: bytes, site_id: str, *, rev: int | None = None
) -> AbsorbResult:
    """위 1~5단계. 거부(`AbsorbError`·`AbsorbConflictError`)면 아무 파일도 바꾸지 않는다."""
    draft = open_site_draft(draft_bytes, enrollment, site_id)
    repo = load_project(root)
    if not repo.ok:
        raise AbsorbError("project is invalid: " + "; ".join(repo.problems))
    repo_id = project_data(repo).get("project_id")
    draft_id = project_data(draft.tree).get("project_id")
    if repo_id != draft_id:
        raise AbsorbError(f"draft project_id {draft_id!r} != repo project_id {repo_id!r}")
    existing = absorbs_of(repo).get(site_id)
    target = _target_rev(draft, rev, existing)
    plan = plan_merge(_data(repo), _data(draft.tree))
    if plan.missing:
        raise AbsorbConflictError(
            "draft items not in the repo (deleted or renamed?): " + ", ".join(plan.missing)
        )
    new_data: dict[str, Json] = dict(plan.files)
    if existing != target:
        project = dict(project_data(repo))
        project[ABSORBS] = {**absorbs_of(repo), site_id: target}
        new_data[PROJECT_FILE] = project
    contents = {rel: dump(data).encode("utf-8") for rel, data in new_data.items()}
    _check_result(repo, contents)
    _write_all(root, dict(sorted(contents.items())))
    after = load_project(root)
    if not after.ok:  # 쓰기 전에 같은 내용을 검증했으므로 디스크가 바뀐 경우뿐이다
        raise AbsorbError("project is invalid after absorb: " + "; ".join(after.problems))
    return AbsorbResult(
        draft=draft,
        changes=plan.changes,
        written=tuple(sorted(contents)),
        absorbs_before=existing,
        absorbs_after=target,
        content_hash=after.content_hash(),
    )


def _check_result(repo: Project, contents: Mapping[str, bytes]) -> None:
    texts: list[tuple[str, Kind, str]] = []
    for f in repo.files:
        content = contents.get(f.relpath)
        text = content.decode("utf-8") if content is not None else f.path.read_text("utf-8")
        texts.append((f.relpath, f.kind, text))
    result = check_texts(repo.root, texts)
    if not result.ok:
        raise AbsorbError("merged project would be invalid: " + "; ".join(result.problems))


def describe(value: Json) -> str:
    """보고용 값 표기: JCS JSON(한 줄), 필드 없음은 `(absent)`."""
    return "(absent)" if value is None else jcs(value).decode("utf-8")


def report_lines(result: AbsorbResult) -> Sequence[str]:
    """stdout 보고(마지막 줄 = 커밋 trailer)."""
    draft = result.draft
    lines = [
        f"site key: {draft.site_id} {draft.fingerprint} (compare with the installation record)",
        f"draft: site_rev={draft.site_rev} base_site_rev={draft.base_site_rev} "
        f"content_hash={draft.content_hash}",
    ]
    if draft.site_rev == draft.base_site_rev:
        lines.append("note: the draft has no site changes (site_rev == base_site_rev)")
    lines.extend(
        f"{c.relpath}: {c.item} {c.field}: {describe(c.before)} -> {describe(c.after)}"
        for c in result.changes
    )
    lines.append(
        f"absorbs[{draft.site_id}]: {describe(result.absorbs_before)} -> {result.absorbs_after}"
    )
    lines.extend(f"wrote {rel}" for rel in result.written)
    lines.append(f"ok: {result.content_hash}")
    lines.append(result.trailer)
    return lines
