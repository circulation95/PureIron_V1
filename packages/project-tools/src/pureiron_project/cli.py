"""`pureiron-project validate|fmt|hash|convert-legacy|absorb|gate` — 고객 프로젝트 repo용 도구.

종료 코드: 0 성공, 1 검증 실패·포맷 불일치(`fmt --check`)·읽을 수 없는 입력,
2 사용법 오류(argparse, 음수 리비전 인자 포함). 2를 하나 더 쓰는 명령이 있고, 사용법 오류와는
출력으로 구분한다(사용법 오류면 stdout은 비고 stderr가 argparse `usage:`로 시작한다):

- `convert-legacy`: 변환은 했지만(출력·보고서 기록) 미지원 항목이 있음(M5 plan D11) — stdout에
  `converted:` 줄이 있다.
- `absorb`: 사람이 판단할 충돌(M5-3 K4) — repo에 없는 드래프트 항목, `--rev`가 서명된 site_rev
  초과, `absorbs[site]` 퇴행. stderr가 `conflict:`로 시작한다. 서명·enrollment·tar·트리 검증
  실패·`project_id` 불일치·content_hash 불일치·repo 쓰기 실패는 1이다. 1·2 모두 repo를 바꾸지
  않는다 — 예외는 쓰기 rename 단계 실패뿐이고, 그때 stderr는 `(repo unchanged)` 대신 바뀐 파일과
  `(repo changed: …)`를 쓴다.

`gate`는 0 통과, 1 `E_DRAFT` 또는 점검할 수 없는 입력이다(stderr `error:`).
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from pathlib import Path

from pureiron_bundle.verify import DraftState
from pureiron_project.absorb import (
    AbsorbConflictError,
    AbsorbError,
    AbsorbWriteError,
    absorb,
    report_lines,
)
from pureiron_project.convert_legacy import convert_legacy, report_bytes, write_tree
from pureiron_project.gate import GateError, draft_state, gate
from pureiron_project.legacy_files import ConvertError
from pureiron_project.project import format_project, load_project

EXIT_UNSUPPORTED = 2
EXIT_CONFLICT = 2
ENROLLMENT_HELP = "현장 enrollment.json(Updater enroll이 state/에 씀, 설치 때 수령)"
REPO_CHANGED = "(repo changed: run absorb again to finish, or restore with git)"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pureiron-project")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("validate", "스키마·교차 참조 검증"),
        ("fmt", "정규형으로 다시 쓰기"),
        ("hash", "content hash 출력"),
    ):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("directory", type=Path)
        if name == "fmt":
            cmd.add_argument("--check", action="store_true", help="바꾸지 않고 불일치만 보고")
    convert = sub.add_parser(
        "convert-legacy", help="legacy 프로젝트 → 프로젝트 트리 + 미지원 항목 보고서(JSON)"
    )
    convert.add_argument("directory", type=Path, help="legacy 프로젝트 디렉터리(project.json 위치)")
    convert.add_argument("--out", type=Path, required=True, help="출력 트리(없거나 빈 디렉터리)")
    convert.add_argument("--report", type=Path, required=True, help="보고서 JSON 경로")
    absorb_cmd = sub.add_parser("absorb", help="서명된 현장 드래프트(draft.tar)를 repo에 회수")
    _site_args(absorb_cmd)
    absorb_cmd.add_argument("--draft", type=Path, required=True, help="현장 draft.tar")
    absorb_cmd.add_argument("--site-key", type=Path, required=True, help=ENROLLMENT_HELP)
    absorb_cmd.add_argument(
        "--rev", type=_revision, help="기록할 absorbs 리비전(기본 = 서명된 site_rev, 그 이하만)"
    )
    absorb_cmd.add_argument("directory", type=Path, nargs="?", default=Path("."))
    gate_cmd = sub.add_parser("gate", help="번들 전 드래프트 게이트 사전 점검")
    gate_cmd.add_argument("directory", type=Path)
    _site_args(gate_cmd)
    source = gate_cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("--draft", type=Path, help="현장 draft.tar(--site-key 필요)")
    source.add_argument("--site-rev", type=_revision, help="현장 site_rev(--base-site-rev 필요)")
    gate_cmd.add_argument("--site-key", type=Path, help=ENROLLMENT_HELP)
    gate_cmd.add_argument("--base-site-rev", type=_revision, help="현장 base_site_rev")
    return parser


def _revision(text: str) -> int:
    """리비전 인자 — 0 이상 정수. 음수는 충돌이 아니라 사용법 오류(exit 2)다."""
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, not {value}")
    return value


def _site_args(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--site", required=True, help="현장 id")


def _utf8_console() -> None:
    """Windows 콘솔(cp949 등)에서도 한국어 메시지 출력으로 죽지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _convert_legacy(source: Path, out: Path, report_path: Path) -> int:
    try:
        conversion = convert_legacy(source)
        project = write_tree(conversion, out)
    except ConvertError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    report = conversion.report(project.content_hash())
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(report_bytes(report))
    unsupported = len(conversion.notes.unsupported)
    print(
        f"converted: {len(project.files)} files, {project.content_hash()}; "
        f"unsupported: {unsupported} (see {report_path})"
    )
    return EXIT_UNSUPPORTED if unsupported else 0


def _absorb(root: Path, draft: Path, site_key: Path, site_id: str, rev: int | None) -> int:
    try:
        result = absorb(root, draft.read_bytes(), site_key.read_bytes(), site_id, rev=rev)
    except AbsorbConflictError as exc:
        print(f"conflict: {exc} (repo unchanged)", file=sys.stderr)
        return EXIT_CONFLICT
    except AbsorbWriteError as exc:
        print(f"error: {exc} {REPO_CHANGED}", file=sys.stderr)
        return 1
    except (AbsorbError, OSError) as exc:
        print(f"error: {exc} (repo unchanged)", file=sys.stderr)
        return 1
    for line in report_lines(result):
        print(line)
    return 0


def _gate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    site_id: str = args.site
    draft: Path | None = args.draft
    site_key: Path | None = args.site_key
    site_rev: int | None = args.site_rev
    base: int | None = args.base_site_rev
    try:
        if draft is None:
            if site_rev is None or base is None or site_key is not None:
                parser.error("gate --site-rev needs --base-site-rev (and no --site-key)")
            state, fingerprint = DraftState(site_rev=site_rev, base_site_rev=base), None
        else:
            if site_key is None or base is not None:
                parser.error("gate --draft needs --site-key (and no --base-site-rev)")
            state, fingerprint = draft_state(draft.read_bytes(), site_key.read_bytes(), site_id)
        result = gate(args.directory, site_id, state, fingerprint=fingerprint)
    except (GateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result.fingerprint is not None:
        print(f"site key: {site_id} {result.fingerprint} (compare with the installation record)")
    print(result.line)
    return 0


def _project_command(args: argparse.Namespace) -> int:
    """`validate`·`hash`·`fmt`."""
    project = load_project(args.directory)
    if not project.ok:
        for problem in project.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1
    if args.command == "validate":
        print(f"ok: {len(project.files)} files, {project.content_hash()}")
        return 0
    if args.command == "hash":
        print(project.content_hash())
        return 0
    changed = format_project(project, check=args.check)
    for rel in changed:
        print(("would reformat " if args.check else "reformatted ") + rel)
    return 1 if args.check and changed else 0


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "convert-legacy":
        return _convert_legacy(args.directory, args.out, args.report)
    if args.command == "absorb":
        return _absorb(args.directory, args.draft, args.site_key, args.site, args.rev)
    if args.command == "gate":
        return _gate(parser, args)
    return _project_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
