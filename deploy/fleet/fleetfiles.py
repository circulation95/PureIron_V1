"""fleet repo 파일 규칙(ADR-022, plan §6.2, M4-2 F4·F9·F11) — 순수 함수, I/O 없음.

fleet repo 배치:

    sites/<site>.yaml            활성 현장 파일(fleet-site 계약). 파일명 == `site`.
    sites/.retired/<site>.yaml   퇴역 현장. 활성 파일을 **그대로 옮긴 것**만
                                 (이후 수정·삭제·이름 재사용 금지).
    sites/.retired/.gitkeep
    allowed_approvers.txt        publish 승인자 목록(판정은 merge 직전 main의 목록, `approval.py`).

트리는 `{경로: 바이트}` 매핑으로 받는다(git 읽기는 `github.Git`). 오류 코드(F11):
`E_FLEET` = 스키마·파일명·`.retired` 위반, `E_GEN` = generation 규칙(check-gen),
`E_APPROVAL` = 승인 판정(publish), `E_PUBLISH` = 빌드·게시 실패.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from pureiron_bundle.manifest import SchemaName, validate
from pureiron_project.canonical import CanonicalError, Json, load

SITES = "sites/"
RETIRED = "sites/.retired/"
GITKEEP = RETIRED + ".gitkeep"
APPROVERS_FILE = "allowed_approvers.txt"
SITE_SUFFIX = ".yaml"
_SITE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
"""fleet-site 계약 `$defs/id`와 같은 규칙(파일명 = site id)."""


class ErrorCode(StrEnum):
    APPROVAL = "E_APPROVAL"
    GEN = "E_GEN"
    FLEET = "E_FLEET"
    PUBLISH = "E_PUBLISH"


@dataclass(frozen=True, slots=True)
class Finding:
    """판정 결과 한 줄. `code`가 None이면 경고(실패 아님)."""

    code: ErrorCode | None
    path: str
    message: str

    @property
    def is_error(self) -> bool:
        return self.code is not None


def error(code: ErrorCode, path: str, message: str) -> Finding:
    return Finding(code, path, message)


def warning(path: str, message: str) -> Finding:
    return Finding(None, path, message)


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def workflow_command(command: str, message: str) -> str:
    """`::<command>::<message>`(error·warning·notice). 메시지의 `%`·CR·LF를 이스케이프해 다음 줄이
    workflow 명령(`::add-mask::` 등)으로 해석되지 않게 한다(F11)."""
    return f"::{command}::{_escape_data(message)}"


def annotation(finding: Finding) -> str:
    """GitHub Actions workflow command(`::error file=…::E_X …` / `::warning …`)."""
    location = f" file={_escape_property(finding.path)}" if finding.path else ""
    if finding.code is None:
        return f"::warning{location}::{_escape_data(finding.message)}"
    return f"::error{location}::{finding.code} {_escape_data(finding.message)}"


@dataclass(frozen=True, slots=True)
class SiteFile:
    site: str
    generation: int
    product: str
    project_repo: str
    project_version: str
    writes_enabled: bool
    document: dict[str, Json]


def parse_site(data: bytes, path: str) -> tuple[SiteFile | None, list[str]]:
    """현장 파일 바이트 → (해석값, 문제 목록). 문제가 있으면 해석값은 None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [f"not UTF-8: {exc}"]
    try:
        document = load(text, path)
    except CanonicalError as exc:
        return None, [str(exc)]
    problems = validate(SchemaName.FLEET_SITE, document)
    if problems:
        return None, problems
    doc = cast(dict[str, Json], document)
    project = cast(dict[str, Json], doc["project"])
    writes = cast(dict[str, Json] | None, doc.get("writes"))
    return (
        SiteFile(
            site=cast(str, doc["site"]),
            generation=cast(int, doc["generation"]),
            product=cast(str, doc["product"]),
            project_repo=cast(str, project["repo"]),
            project_version=cast(str, project["version"]),
            writes_enabled=writes is not None and writes["enabled"] is True,
            document=doc,
        ),
        [],
    )


@dataclass(frozen=True, slots=True)
class SiteStatus:
    """control-api `GET /fleet?site=` 한 행 중 fleet 도구가 쓰는 값(등록된 현장만)."""

    max_published_gen: int | None
    current_product: str | None


class PathKind(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"
    GITKEEP = "gitkeep"
    OTHER = "other"


def classify(path: str) -> tuple[PathKind, str | None]:
    """`sites/` 아래 경로의 종류와 site 이름(활성·퇴역 파일만)."""
    if path == GITKEEP:
        return PathKind.GITKEEP, None
    for kind, prefix in ((PathKind.RETIRED, RETIRED), (PathKind.ACTIVE, SITES)):
        if path.startswith(prefix):
            rest = path.removeprefix(prefix)
            name = rest.removesuffix(SITE_SUFFIX)
            if rest.endswith(SITE_SUFFIX) and _SITE_ID.fullmatch(name):
                return kind, name
            return PathKind.OTHER, None
    return PathKind.OTHER, None


def site_path(site: str) -> str:
    return f"{SITES}{site}{SITE_SUFFIX}"


def retired_path(site: str) -> str:
    return f"{RETIRED}{site}{SITE_SUFFIX}"


class ChangeKind(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class Change:
    kind: ChangeKind
    path: str
    old: bytes | None
    new: bytes | None


def diff_trees(base: Mapping[str, bytes], head: Mapping[str, bytes]) -> list[Change]:
    """두 트리의 `sites/` 아래 변경(경로 순). 이동은 삭제 + 추가 한 쌍으로 나온다."""
    changes: list[Change] = []
    for path in sorted(set(base) | set(head)):
        if not path.startswith(SITES):
            continue
        old, new = base.get(path), head.get(path)
        if old == new:
            continue
        if old is None:
            changes.append(Change(ChangeKind.ADDED, path, None, new))
        elif new is None:
            changes.append(Change(ChangeKind.DELETED, path, old, None))
        else:
            changes.append(Change(ChangeKind.MODIFIED, path, old, new))
    return changes


@dataclass(frozen=True, slots=True)
class SiteUpdate:
    """활성 현장 파일의 추가·수정(게시 대상). `old`는 base 쪽 해석값(추가면 None)."""

    path: str
    site: SiteFile
    old: SiteFile | None
    old_problems: tuple[str, ...]


def structure(
    changes: Iterable[Change], head: Mapping[str, bytes]
) -> tuple[list[SiteUpdate], list[Finding]]:
    """변경 목록의 `E_FLEET` 규칙 → (게시 대상 현장 갱신, 위반).

    - 활성 파일 추가·수정: 계약 스키마 통과, 파일명 == `site`. 추가는 같은 이름의 퇴역 파일이
      없을 것.
    - 활성 파일 삭제: 같은 커밋에서 `sites/.retired/<site>.yaml`로 같은 내용이 생겨야 한다(이동만).
    - 퇴역 파일: 이동으로 생기는 것만 허용, 수정·삭제 금지.
    - 그 밖의 `sites/` 아래 파일(`.yml`·하위 디렉터리·이름 규칙 위반)은 추가·수정 금지.
    """
    changes = list(changes)
    deleted = {c.path: c.old for c in changes if c.kind is ChangeKind.DELETED}
    added = {c.path: c.new for c in changes if c.kind is ChangeKind.ADDED}
    updates: list[SiteUpdate] = []
    findings: list[Finding] = []
    for change in changes:
        kind, name = classify(change.path)
        if kind is PathKind.GITKEEP:
            continue
        if kind is PathKind.OTHER:
            if change.kind is not ChangeKind.DELETED:
                findings.append(
                    error(
                        ErrorCode.FLEET,
                        change.path,
                        "sites/ 아래에는 <site>.yaml(site id 규칙)과 .retired/<site>.yaml만 둔다",
                    )
                )
            continue
        assert name is not None
        if kind is PathKind.RETIRED:
            findings.extend(_retired_change(change, name, deleted))
            continue
        if change.kind is ChangeKind.DELETED:
            if added.get(retired_path(name)) != change.old:
                findings.append(
                    error(
                        ErrorCode.FLEET,
                        change.path,
                        f"현장 파일 삭제 금지 — 퇴역은 {retired_path(name)}로 같은 내용 그대로 "
                        "이동만 한다",
                    )
                )
            continue
        update, problems = _active_update(change, name, head)
        findings.extend(problems)
        if update is not None:
            updates.append(update)
    return updates, findings


def _retired_change(
    change: Change, name: str, deleted: Mapping[str, bytes | None]
) -> list[Finding]:
    if change.kind is not ChangeKind.ADDED:
        return [
            error(
                ErrorCode.FLEET, change.path, "퇴역 파일은 고치거나 지울 수 없다(이름 재사용 방지)"
            )
        ]
    if deleted.get(site_path(name)) != change.new:
        return [
            error(
                ErrorCode.FLEET,
                change.path,
                f"퇴역 파일은 {site_path(name)}을 같은 내용 그대로 옮긴 것이어야 한다",
            )
        ]
    return []


def _active_update(
    change: Change, name: str, head: Mapping[str, bytes]
) -> tuple[SiteUpdate | None, list[Finding]]:
    assert change.new is not None
    findings: list[Finding] = []
    if change.kind is ChangeKind.ADDED and retired_path(name) in head:
        findings.append(
            error(
                ErrorCode.FLEET,
                change.path,
                f"퇴역한 현장 이름 {name!r}은 다시 쓸 수 없다({retired_path(name)})",
            )
        )
    site, problems = parse_site(change.new, change.path)
    if site is None:
        findings.extend(error(ErrorCode.FLEET, change.path, p) for p in problems)
        return None, findings
    if site.site != name:
        findings.append(
            error(
                ErrorCode.FLEET,
                change.path,
                f"파일명 {name!r} != site {site.site!r}(sites/<site>.yaml)",
            )
        )
        return None, findings
    old: SiteFile | None = None
    old_problems: list[str] = []
    if change.old is not None:
        old, old_problems = parse_site(change.old, change.path)
    if findings:
        return None, findings
    return SiteUpdate(change.path, site, old, tuple(old_problems)), findings


def gen_step(update: SiteUpdate) -> Finding | None:
    """`E_GEN` +1 규칙: 활성 파일 추가·수정은 `new == old + 1`(새 파일은 old = 0).

    check-gen(PR)과 publish(merge 뒤 — Free는 빨간 check-gen이어도 merge할 수 있다)가 같이 쓴다.
    """
    if update.old_problems:
        return error(
            ErrorCode.GEN,
            update.path,
            "base 쪽 파일을 해석할 수 없어 gen 규칙을 확인할 수 없다: "
            + "; ".join(update.old_problems),
        )
    old_gen = 0 if update.old is None else update.old.generation
    if update.site.generation != old_gen + 1:
        return error(
            ErrorCode.GEN,
            update.path,
            f"generation은 정확히 +1: {old_gen} → {old_gen + 1}이어야 하는데 "
            f"{update.site.generation}",
        )
    return None


@dataclass(frozen=True, slots=True)
class ActiveSite:
    path: str
    site: SiteFile


def active_sites(head: Mapping[str, bytes]) -> tuple[list[ActiveSite], list[Finding]]:
    """트리의 모든 활성 현장 파일(`sites/<site>.yaml`) → (해석값, `E_FLEET` 위반).

    퇴역 이름을 다시 쓴 활성 파일은 그 커밋에서만이 아니라 트리에 있는 동안 매번 위반이다
    (diff 규칙은 추가한 커밋에서만 보이므로, 트리 전체로 다시 본다).
    """
    sites: list[ActiveSite] = []
    findings: list[Finding] = []
    for path in sorted(head):
        kind, name = classify(path)
        if kind is not PathKind.ACTIVE:
            continue
        assert name is not None
        if retired_path(name) in head:
            findings.append(
                error(
                    ErrorCode.FLEET,
                    path,
                    f"퇴역한 현장 이름 {name!r}은 다시 쓸 수 없다({retired_path(name)}) — "
                    "이 활성 파일을 지우는 PR로 고친다",
                )
            )
            continue
        site, problems = parse_site(head[path], path)
        if site is None:
            findings.extend(error(ErrorCode.FLEET, path, p) for p in problems)
        elif site.site != name:
            findings.append(
                error(
                    ErrorCode.FLEET,
                    path,
                    f"파일명 {name!r} != site {site.site!r}(sites/<site>.yaml)",
                )
            )
        else:
            sites.append(ActiveSite(path, site))
    return sites, findings
