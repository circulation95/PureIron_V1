"""`pureiron-core` 진입점: serve · migrate · project.

종료 코드(plan §6.6): 0 성공, 2 lock, 3 checksum, 4 실패, 5 lint.
`runtime`/`app`을 import할 수 있는 유일한 모듈이다(import-linter 계약 (a)).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import cast

import asyncpg
import uvicorn

from pureiron_core.app import create_app
from pureiron_core.platform import migrate
from pureiron_core.platform.db import Database, DatabaseUnavailableError, connect_once
from pureiron_core.platform.migrate_lint import lint_directory
from pureiron_core.project.ports import ProjectStateError
from pureiron_core.project.stub import PgProjectStateStore
from pureiron_core.runtime import SiteRuntime
from pureiron_core.settings import Settings, SettingsError, read_secret

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m)?$")


def parse_duration(text: str) -> float:
    match = _DURATION.match(text.strip())
    if match is None:
        raise argparse.ArgumentTypeError(f"invalid duration {text!r} (e.g. 30s, 2m, 500ms)")
    value, unit = float(match[1]), match[2] or "s"
    return value * {"ms": 0.001, "s": 1.0, "m": 60.0}[unit]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pureiron-core")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="HTTP(:8000) + collector gRPC(:50051) 실행")

    mig = sub.add_parser("migrate", help="스키마 마이그레이션(ADD-only, epoch envelope)")
    mig_sub = mig.add_subparsers(dest="migrate_command", required=True)
    st = mig_sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    upp = mig_sub.add_parser("up")
    upp.add_argument("--to", type=int, default=None, metavar="EPOCH")
    upp.add_argument("--lock-timeout", type=parse_duration, default=30.0)
    mig_sub.add_parser("verify")
    lint = mig_sub.add_parser("lint")
    lint.add_argument("directory", type=Path)
    lint.add_argument("--base-ref", default=None)

    proj = sub.add_parser("project", help="현장 프로젝트 상태(M1 골격: project_state만)")
    proj_sub = proj.add_subparsers(dest="project_command", required=True)
    apply = proj_sub.add_parser("apply")
    apply.add_argument("--project-id", required=True)
    apply.add_argument("--version", required=True)
    apply.add_argument("--content-hash", required=True)
    export = proj_sub.add_parser("export")
    export.add_argument("--out", type=Path, default=None)
    restore = proj_sub.add_parser("restore")
    restore.add_argument("--from", dest="source", type=Path, required=True)
    unlock = proj_sub.add_parser(
        "unlock", help="편집 잠금 해제(core HTTP가 없을 때 Updater 복구 경로, plan M2 recover)"
    )
    unlock.add_argument("--owner", required=True)
    return parser


def _serve(settings: Settings) -> int:
    app = create_app(SiteRuntime.build(settings))
    uvicorn.run(app, host=settings.http_host, port=settings.http_port, log_config=None)
    return 0


async def _with_connection(
    settings: Settings, action: Callable[[migrate.Connection], Awaitable[int]]
) -> int:
    conn = await connect_once(settings.db_dsn, password=read_secret(settings.db_password_file))
    try:
        return await action(conn)
    finally:
        await conn.close()


async def _migrate(args: argparse.Namespace, settings: Settings) -> int:
    command: str = args.migrate_command
    if command == "lint":
        report = lint_directory(args.directory, base_ref=args.base_ref)
        for notice in report.notices:
            print(f"notice: {notice}")
        for issue in report.issues:
            print(f"error: {issue}", file=sys.stderr)
        if report.issues:
            return migrate.EXIT_LINT
        print(f"migrate lint: ok ({args.directory})")
        return migrate.EXIT_OK

    catalog = migrate.load_catalog(settings.migrations_dir)

    async def run(conn: migrate.Connection) -> int:
        if command == "status":
            result = await migrate.status(conn, catalog)
            if args.json:
                print(json.dumps(result.as_json(), sort_keys=True))
            else:
                print(
                    f"cur={result.cur} app_expected={result.app_expected} "
                    f"app_max_compatible={result.app_max_compatible} "
                    f"pending={len(result.pending)} checksum_ok={result.checksum_ok}"
                )
            return migrate.EXIT_OK
        if command == "up":
            done = await migrate.up(
                conn,
                catalog,
                release=settings.release,
                to_epoch=args.to,
                lock_timeout_s=args.lock_timeout,
            )
            print(f"migrate up: applied {len(done)}" + (f" ({', '.join(done)})" if done else ""))
            return migrate.EXIT_OK
        await migrate.verify(conn, catalog)
        print("migrate verify: ok")
        return migrate.EXIT_OK

    return await _with_connection(settings, run)


def _read_snapshot(source: Path) -> dict[str, object]:
    snapshot: object = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise ProjectStateError(f"{source}: snapshot must be a JSON object")
    return cast("dict[str, object]", snapshot)


async def _project(
    args: argparse.Namespace, settings: Settings, snapshot: dict[str, object] | None
) -> str:
    """결과 JSON 텍스트를 돌려준다(파일 쓰기는 호출자가 async 밖에서 한다)."""
    db = Database(settings.db_dsn, password=read_secret(settings.db_password_file), max_size=1)
    store = PgProjectStateStore(db)
    try:
        command: str = args.project_command
        if command == "apply":
            state = await store.apply(
                project_id=args.project_id, version=args.version, content_hash=args.content_hash
            )
        elif command == "export":
            state = await store.get()
        elif command == "unlock":
            await store.release_edit_lock(args.owner)
            state = await store.get()
        elif snapshot is not None:
            state = await store.restore(snapshot)
        else:
            raise ProjectStateError("restore needs --from")
        return json.dumps(state.to_snapshot(), sort_keys=True, indent=2) + "\n"
    finally:
        await db.close()


def _run_project(args: argparse.Namespace, settings: Settings) -> int:
    snapshot = _read_snapshot(args.source) if args.project_command == "restore" else None
    text = asyncio.run(_project(args, settings, snapshot))
    out: Path | None = getattr(args, "out", None)
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")
    return 0


def _utf8_console() -> None:
    """Windows 콘솔(cp949 등)에서도 한국어·기호 출력으로 죽지 않게 한다."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_console()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _parser().parse_args(argv)
    try:
        settings = Settings.from_env(os.environ)
        if args.command == "serve":
            return _serve(settings)
        if args.command == "migrate":
            return asyncio.run(_migrate(args, settings))
        return _run_project(args, settings)
    except migrate.MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (
        DatabaseUnavailableError,
        ProjectStateError,
        SettingsError,
        OSError,
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return migrate.EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
