"""`pureiron-project validate|fmt|hash <dir>` — 고객 프로젝트 repo용 도구(M1 최소판).

종료 코드: 0 성공, 1 검증 실패·포맷 불일치(`fmt --check`), 2 사용법 오류.
"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence
from pathlib import Path

from pureiron_project.project import format_project, load_project


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
    return parser


def _utf8_console() -> None:
    """Windows 콘솔(cp949 등)에서도 한국어 메시지 출력으로 죽지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    args = _parser().parse_args(argv)
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
