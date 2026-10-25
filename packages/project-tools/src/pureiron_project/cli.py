"""`pureiron-project validate|fmt|hash|convert-legacy` — 고객 프로젝트 repo용 도구.

종료 코드: 0 성공, 1 검증 실패·포맷 불일치(`fmt --check`)·읽을 수 없는 입력,
2 사용법 오류(argparse). `convert-legacy`는 2를 하나 더 쓴다: 변환은 했지만(출력·보고서 기록)
미지원 항목이 있음(M5 plan D11). 둘은 stdout으로 구분한다 — 미지원이면 `converted:` 줄이 있고,
사용법 오류면 stdout은 비고 stderr에 argparse usage가 나온다.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from pathlib import Path

from pureiron_project.convert_legacy import convert_legacy, report_bytes, write_tree
from pureiron_project.legacy_files import ConvertError
from pureiron_project.project import format_project, load_project

EXIT_UNSUPPORTED = 2


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
    return parser


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


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    args = _parser().parse_args(argv)
    if args.command == "convert-legacy":
        return _convert_legacy(args.directory, args.out, args.report)
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


if __name__ == "__main__":
    raise SystemExit(main())
