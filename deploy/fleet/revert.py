"""현장 되돌리기 PR 만들기(M4-2 F8): `revert.py <site> --to-gen N`.

    uv run python deploy/fleet/revert.py site-a --to-gen 3 --repo .

fleet repo git 이력에서 generation N이던 `sites/<site>.yaml`을 찾아 `product`·`project`·`window`·
`writes`를 가져오고 `generation = 현재 + 1`로 새 파일을 쓴 뒤 브랜치·커밋·push·PR(`gh`, 작성자 =
실행자)을 만든다. 일반 승인 경로(check-gen → 승인 → merge → publish)를 그대로 탄다 — 우회 없음.

경고(실패 아님): 현재 product의 schema epoch가 되돌릴 product의 envelope
`[app_expected, app_max_compatible]` 밖이면 현장이 `E_SCHEMA`로 거부한다(plan §6.6 — DB는 내려가지
않는다), 쓰기 스위치(`writes.enabled`)가 바뀐다, release 기록이 없다.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bundle"))

from build_bundle import RELEASES, BuildError, ReleaseRecord, load_releases

from fleet.fleetfiles import SiteFile, parse_site, site_path
from fleet.github import Git, GitError
from pureiron_project.canonical import Json, dump

CARRIED = ("product", "project", "window", "writes")
"""되돌릴 때 옛 파일에서 가져오는 키(site·generation은 현재 기준)."""


class RevertError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RevertResult:
    document: dict[str, Json]
    warnings: tuple[str, ...]


def revert_content(
    history: Sequence[SiteFile],
    current: SiteFile,
    to_gen: int,
    releases: Mapping[str, ReleaseRecord],
) -> RevertResult:
    """gen `to_gen`의 내용 + `generation = 현재 + 1`.

    `history`는 그 현장 파일의 과거 판들(순서 무관), `releases`는 release 버전 → 기록.
    """
    if to_gen >= current.generation:
        raise RevertError(f"되돌릴 gen {to_gen}은 현재 gen {current.generation}보다 작아야 한다")
    matches = {_key(h): h for h in history if h.site == current.site and h.generation == to_gen}
    if not matches:
        raise RevertError(f"{current.site}: 이력에 generation {to_gen} 파일이 없다")
    if len(matches) > 1:
        raise RevertError(f"{current.site}: generation {to_gen} 파일이 서로 다른 내용으로 여럿")
    target = next(iter(matches.values()))
    document: dict[str, Json] = {"site": current.site, "generation": current.generation + 1}
    for key in CARRIED:
        if key in target.document:
            document[key] = target.document[key]
    return RevertResult(document, tuple(_warnings(current, target, releases)))


def _key(site: SiteFile) -> str:
    return dump(site.document)


def _warnings(
    current: SiteFile, target: SiteFile, releases: Mapping[str, ReleaseRecord]
) -> list[str]:
    found: list[str] = []
    now, then = releases.get(current.product), releases.get(target.product)
    if now is None or then is None:
        missing = [v for v, r in ((current.product, now), (target.product, then)) if r is None]
        found.append(f"release 기록 없음({', '.join(missing)}) — schema envelope를 확인하지 못했다")
    elif not then.app_expected <= now.epoch <= then.app_max_compatible:
        found.append(
            f"현재 {current.product}의 schema epoch {now.epoch}가 {target.product}의 envelope "
            f"[{then.app_expected}, {then.app_max_compatible}] 밖 — 현장이 E_SCHEMA로 거부한다"
            "(DB는 내려가지 않는다, plan §6.6)"
        )
    if current.writes_enabled != target.writes_enabled:
        state = "켜진다" if target.writes_enabled else "꺼진다"
        found.append(f"쓰기 스위치 writes.enabled가 {state}")
    return found


def site_history(git: Git, ref: str, site: str) -> tuple[SiteFile, list[SiteFile]]:
    """(ref의 현재 파일, 그 파일이 바뀐 커밋들의 판)."""
    path = site_path(site)
    current_bytes = git.file(ref, path)
    if current_bytes is None:
        raise RevertError(f"{ref}에 {path}가 없다")
    current, problems = parse_site(current_bytes, path)
    if current is None:
        raise RevertError(f"{path}: " + "; ".join(problems))
    history: list[SiteFile] = []
    for sha in git.text("log", "--format=%H", ref, "--", path).splitlines():
        data = git.file(sha, path)
        if data is None:
            continue
        parsed, _ = parse_site(data, path)
        if parsed is not None:
            history.append(parsed)
    return current, history


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="revert")
    p.add_argument("site", help="현장 id(sites/<site>.yaml)")
    p.add_argument("--to-gen", type=int, required=True, help="되돌릴 과거 generation")
    p.add_argument(
        "--repo", type=Path, default=Path(), help="fleet repo checkout(기본 현재 디렉터리)"
    )
    p.add_argument("--ref", default="HEAD", help="기준 커밋(기본 HEAD — 최신 main을 받아 둔 상태)")
    p.add_argument("--base", default="main", help="PR base 브랜치")
    p.add_argument("--releases", type=Path, default=RELEASES, help="release 기록 디렉터리")
    p.add_argument("--no-push", action="store_true", help="브랜치·커밋만(push·PR 없음)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    git = Git(args.repo)
    try:
        releases = {r.version: r for r in load_releases(args.releases)}
        current, history = site_history(git, args.ref, args.site)
        result = revert_content(history, current, args.to_gen, releases)
        new_gen = current.generation + 1
        branch = f"revert/{args.site}-g{new_gen}"
        git.text("switch", "-q", "-c", branch, args.ref)
        target = args.repo / site_path(args.site)
        target.write_text(dump(result.document), encoding="utf-8", newline="\n")
        title = f"revert({args.site}): gen {new_gen} = gen {args.to_gen} 내용"
        git.text("add", "--", site_path(args.site))
        git.text("commit", "-q", "-m", title)
    except (RevertError, BuildError, GitError, OSError) as exc:
        print(f"::error::revert: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"::warning::{warning}")
    print(f"{branch}: {title}")
    if args.no_push:
        return 0
    body = "\n".join(
        [
            f"`sites/{args.site}.yaml`을 gen {args.to_gen} 내용으로 되돌린다(새 gen {new_gen}).",
            "일반 승인 경로를 탄다: check-gen → allowed_approvers 승인 → merge → publish.",
            *(f"- 경고: {w}" for w in result.warnings),
        ]
    )
    try:
        git.text("push", "-q", "-u", "origin", branch)
        subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                args.base,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=args.repo,
            check=True,
        )
    except (GitError, OSError, subprocess.CalledProcessError) as exc:
        print(
            f"::error::revert: push/PR 실패({exc}) — 브랜치 {branch}는 로컬에 있다", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
