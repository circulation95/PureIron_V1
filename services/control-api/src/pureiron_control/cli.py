"""`pureiron-control` 진입점: serve · migrate · token · site.

- `migrate status|up|verify|lint`는 core와 같은 migrate 라이브러리로 control-db를 다룬다.
  부팅 때 자동 실행하지 않는다(불변 규칙) — 배포 절차가 `migrate up`을 먼저 실행한다.
- `token`·`site` 하위 명령은 `cli_admin.py`(사람·CI token 발급은 CLI로만, 기본 계정 없음 — C5).

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
import sys
from collections.abc import Sequence
from pathlib import Path

import asyncpg
import uvicorn

from pureiron_control import cli_admin
from pureiron_control.app import create_app
from pureiron_control.runtime import ControlRuntime
from pureiron_control.settings import Settings, SettingsError, read_secret
from pureiron_core.platform import migrate
from pureiron_core.platform.db import DatabaseUnavailableError, connect_once
from pureiron_core.platform.migrate_lint import lint_directory

# control-api 카탈로그의 release 기록(중앙 release, `release_record.py --central`이 쓴다).
# migrate lint의 "release된 epoch" 판정은 이 기록만 본다 — 현장 release 기록
# (deploy/releases/*.json)과 섞지 않는다(G10).
CENTRAL_RELEASES_DIR = "deploy/releases/central"

# SIGTERM 뒤 열린 HTTP 요청을 기다리는 시간(초). 번들 내려받기는 Range로 이어지므로 짧게 둔다.
HTTP_GRACEFUL_SHUTDOWN_S = 5


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pureiron-control")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="HTTP(/api/v1) 실행")

    mig = sub.add_parser("migrate", help="control-db 스키마 마이그레이션(ADD-only, epoch envelope)")
    mig_sub = mig.add_subparsers(dest="migrate_command", required=True)
    mig_sub.add_parser("status").add_argument("--json", action="store_true")
    upp = mig_sub.add_parser("up")
    upp.add_argument("--to", type=int, default=None, metavar="EPOCH")
    upp.add_argument("--lock-timeout", type=float, default=30.0, metavar="SECONDS")
    mig_sub.add_parser("verify")
    lint = mig_sub.add_parser("lint")
    lint.add_argument("directory", type=Path)
    lint.add_argument("--base-ref", default=None)

    cli_admin.configure_parsers(
        sub.add_parser("token", help="사람·CI token(admin·ci·viewer) 발급·폐기·목록"),
        sub.add_parser("site", help="현장 등록·재등록·token 재발급(REST admin 경로와 같은 동작)"),
    )
    return parser


def _serve(settings: Settings) -> int:
    app = create_app(ControlRuntime.build(settings))
    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
        timeout_graceful_shutdown=HTTP_GRACEFUL_SHUTDOWN_S,
    )
    uvicorn.Server(config).run()
    return 0


def _lint(args: argparse.Namespace) -> int:
    report = lint_directory(
        args.directory, base_ref=args.base_ref, releases_dir=CENTRAL_RELEASES_DIR
    )
    for notice in report.notices:
        print(f"notice: {notice}")
    for issue in report.issues:
        print(f"error: {issue}", file=sys.stderr)
    if report.issues:
        return migrate.EXIT_LINT
    print(f"migrate lint: ok ({args.directory})")
    return migrate.EXIT_OK


async def _migrate(args: argparse.Namespace, settings: Settings) -> int:
    command: str = args.migrate_command
    catalog = migrate.load_catalog(settings.migrations_dir)
    conn = await connect_once(settings.db_dsn, password=read_secret(settings.db_password_file))
    try:
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
        elif command == "up":
            done = await migrate.up(
                conn,
                catalog,
                release=settings.release,
                to_epoch=args.to,
                lock_timeout_s=args.lock_timeout,
            )
            print(f"migrate up: applied {len(done)}" + (f" ({', '.join(done)})" if done else ""))
        else:
            await migrate.verify(conn, catalog)
            print("migrate verify: ok")
    finally:
        await conn.close()
    return migrate.EXIT_OK


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
        if args.command == "migrate" and args.migrate_command == "lint":
            return _lint(args)
        settings = Settings.from_env(os.environ)
        if args.command == "serve":
            return _serve(settings)
        if args.command == "migrate":
            return asyncio.run(_migrate(args, settings))
        return cli_admin.run(args, settings)
    except migrate.MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (
        cli_admin.AdminError,
        DatabaseUnavailableError,
        SettingsError,
        OSError,
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return migrate.EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
