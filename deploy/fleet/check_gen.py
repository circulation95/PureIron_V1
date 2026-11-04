"""fleet PR 검사 check-gen(M4-2 F4, plan C10) — 순수 판정 `check` + CLI.

    uv run python deploy/fleet/check_gen.py --repo fleet --base <sha> --head <sha> \\
        --control-api http://127.0.0.1:8100/api/v1

규칙(변경된 활성 현장 파일마다):
- `E_FLEET`: 계약 스키마, 파일명 == `site`, `.retired` 규칙(`fleetfiles.structure`).
- `E_GEN`: `new == old + 1`(새 파일은 old = 0), 그리고 `new > max_published_gen`
  (`GET /fleet?site=` 404 = 미등록 → gen 1만).
경고(실패 아님): product release가 V1 `deploy/releases`에 없음, `writes.enabled: true`
(Updater ≥ 0.2.0 필요), 현장 current product → 새 product의 schema epoch가 2 이상 뜀(현장
`E_SCHEMA` 예상, plan §6.6), 미등록 현장(merge 전 등록해야 publish가 성공 — M4-0 L-7).

토큰은 viewer(env `PUREIRON_FLEET_VIEWER_TOKEN`) — PR 단계에는 게시 권한을 두지 않는다(M4-0 L-7).
PR 검사라 서명 경계가 아니다: 트리는 runner가 checkout한 fleet repo(`--repo`)에서 읽는다.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bundle"))

from build_bundle import RELEASES, BuildError, load_releases

from fleet.fleetfiles import (
    ErrorCode,
    Finding,
    SiteStatus,
    SiteUpdate,
    annotation,
    diff_trees,
    error,
    structure,
    warning,
)
from fleet.github import Git, GitError
from fleet.publish import ControlApi, ControlApiError

VIEWER_TOKEN_ENV = "PUREIRON_FLEET_VIEWER_TOKEN"
CONTROL_API_ENV = "PUREIRON_CONTROL_API"
DEFAULT_CONTROL_API = "http://127.0.0.1:8100/api/v1"
EPOCH_JUMP_WARN = 2


type Lookup = Callable[[str], SiteStatus | None]
"""site → 등록 상태(미등록 None). 조회 실패는 `LookupError`."""


def check(
    updates: Sequence[SiteUpdate],
    status: Mapping[str, SiteStatus | None],
    epochs: Mapping[str, int],
) -> list[Finding]:
    """`E_GEN` 규칙과 경고.

    `status`는 updates의 site마다(미등록 None), `epochs`는 release 버전 → schema epoch.
    """
    findings: list[Finding] = []
    for update in updates:
        site, path = update.site, update.path
        if update.old_problems:
            findings.append(
                error(
                    ErrorCode.GEN,
                    path,
                    "base 쪽 파일을 해석할 수 없어 gen 규칙을 확인할 수 없다: "
                    + "; ".join(update.old_problems),
                )
            )
            continue
        old_gen = 0 if update.old is None else update.old.generation
        if site.generation != old_gen + 1:
            findings.append(
                error(
                    ErrorCode.GEN,
                    path,
                    f"generation은 정확히 +1: {old_gen} → {old_gen + 1}이어야 하는데 "
                    f"{site.generation}",
                )
            )
        current = status[site.site]
        if current is None:
            if site.generation != 1:
                findings.append(
                    error(
                        ErrorCode.GEN,
                        path,
                        f"control-api에 미등록 현장 {site.site!r}은 gen 1만 — {site.generation}",
                    )
                )
            findings.append(
                warning(
                    path,
                    f"현장 {site.site!r} 미등록 — merge 전에 control-api 등록"
                    "(pureiron-control site register)을 먼저 해야 publish가 성공한다",
                )
            )
        else:
            published = current.max_published_gen or 0
            if site.generation <= published:
                findings.append(
                    error(
                        ErrorCode.GEN,
                        path,
                        f"generation {site.generation} ≤ 게시된 최대 gen {published}"
                        "(control-api max_published_gen)",
                    )
                )
        findings.extend(_warnings(update, current, epochs))
    return findings


def _warnings(
    update: SiteUpdate, current: SiteStatus | None, epochs: Mapping[str, int]
) -> list[Finding]:
    site, path = update.site, update.path
    found: list[Finding] = []
    new_epoch = epochs.get(site.product)
    if new_epoch is None:
        found.append(
            warning(path, f"product {site.product} release 기록이 V1 deploy/releases에 없다")
        )
    if site.writes_enabled:
        found.append(
            warning(
                path,
                "writes.enabled: true — 현장 Updater 0.2.0 이상만 적용한다(구 Updater는 E_UPDATER)",
            )
        )
    current_product = None if current is None else current.current_product
    if new_epoch is not None and current_product is not None:
        old_epoch = epochs.get(current_product)
        if old_epoch is not None and new_epoch - old_epoch >= EPOCH_JUMP_WARN:
            found.append(
                warning(
                    path,
                    f"schema epoch {old_epoch}({current_product}) → {new_epoch}({site.product}): "
                    f"{EPOCH_JUMP_WARN} 이상 뛰면 현장이 E_SCHEMA로 거부한다(plan §6.6 단계 적용)",
                )
            )
    return found


def run(git: Git, base: str, head: str, lookup: Lookup, epochs: Mapping[str, int]) -> list[Finding]:
    """트리 읽기 → 구조 규칙 → 현장 상태 조회 → gen 규칙."""
    base_tree, head_tree = git.tree(base), git.tree(head)
    updates, findings = structure(diff_trees(base_tree, head_tree), head_tree)
    status: dict[str, SiteStatus | None] = {}
    for update in updates:
        name = update.site.site
        try:
            status[name] = lookup(name)
        except LookupError as exc:
            findings.append(error(ErrorCode.GEN, update.path, f"control-api 조회 실패: {exc}"))
    checkable = [u for u in updates if u.site.site in status]
    return findings + check(checkable, status, epochs)


def release_epochs(directory: Path) -> dict[str, int]:
    return {r.version: r.epoch for r in load_releases(directory)}


def report(findings: Sequence[Finding]) -> int:
    for finding in findings:
        print(annotation(finding))
    errors = sum(f.is_error for f in findings)
    warnings = len(findings) - errors
    print(f"check-gen: 오류 {errors}건, 경고 {warnings}건")
    return 1 if errors else 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="check_gen")
    p.add_argument(
        "--repo", type=Path, default=Path(), help="fleet repo checkout(기본 현재 디렉터리)"
    )
    p.add_argument("--base", required=True, help="PR base sha")
    p.add_argument("--head", required=True, help="PR head sha")
    p.add_argument(
        "--control-api",
        default=os.environ.get(CONTROL_API_ENV, DEFAULT_CONTROL_API),
        help=f"control-api 기준 URL(기본 env {CONTROL_API_ENV} 또는 {DEFAULT_CONTROL_API})",
    )
    p.add_argument("--releases", type=Path, default=RELEASES, help="release 기록 디렉터리")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    token = os.environ.get(VIEWER_TOKEN_ENV, "").strip()
    if not token:
        print(f"::error::{ErrorCode.GEN} env {VIEWER_TOKEN_ENV}(control-api viewer 토큰)가 없다")
        return 1
    api = ControlApi(args.control_api, viewer_token=token, ci_token=None)

    def lookup(site: str) -> SiteStatus | None:
        try:
            return api.site_status(site)
        except ControlApiError as exc:
            raise LookupError(str(exc)) from exc

    try:
        epochs = release_epochs(args.releases)
        findings = run(Git(args.repo), args.base, args.head, lookup, epochs)
    except (BuildError, GitError) as exc:
        print(f"::error::{ErrorCode.GEN} {exc}")
        return 1
    return report(findings)


if __name__ == "__main__":
    raise SystemExit(main())
