"""`pureiron-core` 진입점: serve · migrate · project · apply-definitions · users.

종료 코드(plan §6.6): 0 성공, 2 lock, 3 checksum, 4 실패, 5 lint.
`runtime`/`app`을 import할 수 있는 유일한 모듈이다(import-linter 계약 (a)).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
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
from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSource
from pureiron_core.audit.store import PgAuditStore
from pureiron_core.auth.ports import AuthError, Role, User
from pureiron_core.auth.service import AuthService
from pureiron_core.auth.sessions import PgSessionStore
from pureiron_core.auth.users import PgUserStore
from pureiron_core.platform import migrate
from pureiron_core.platform.db import Database, DatabaseUnavailableError, connect_once
from pureiron_core.platform.migrate_lint import lint_directory
from pureiron_core.project.definitions import apply_definitions
from pureiron_core.project.ports import ProjectStateError
from pureiron_core.project.stub import PgProjectStateStore
from pureiron_core.runtime import SiteRuntime
from pureiron_core.settings import Settings, SettingsError, read_secret
from pureiron_project.project import load_project

# SIGTERM 뒤 열린 HTTP 요청을 기다리는 시간(초, uvicorn은 int). compose core
# `stop_grace_period: 25s` 안에서 최종 이력 flush가 SIGKILL 전에 끝나게 정한다(W5).
# half-open DB 최악:
#   uvicorn 0.2s(종료 감지 tick 0.1s + 연결 종료 요청 뒤 0.1s) + 이 값 2s
#   → lifespan `SiteRuntime.stop()`: acquisition = PlanKeeper 취소가 쿼리 도중이면 반환 정리 5s
#     + gRPC grace 2s → historian 15s(진행 중 쓰기 반환 정리 5s + 최종 flush 5s + 그 반환 정리 5s)
#   = 24.2s. 그 뒤 `db.close()`가 SIGKILL에 끊겨도 잃는 이력은 없다. 정상 DB에서는 모두 ms 단위다.
HTTP_GRACEFUL_SHUTDOWN_S = 2

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

    defs = sub.add_parser(
        "apply-definitions",
        help="프로젝트 트리의 장비·태그 정의를 site-db에 반영(path 기준 upsert·retire, M3a)",
    )
    defs.add_argument("project_dir", type=Path)
    defs.add_argument("--json", action="store_true")
    _users_parser(
        sub.add_parser(
            "users",
            help="사용자 계정 관리(CLI 전용, plan M3b D3). 비밀번호는 stdin 또는 --password-file",
        )
    )
    return parser


def _users_parser(users: argparse.ArgumentParser) -> None:
    users_sub = users.add_subparsers(dest="users_command", required=True)
    add = users_sub.add_parser("add", help="계정 추가")
    add.add_argument("username")
    add.add_argument("--role", required=True, choices=[role.label for role in Role])
    add.add_argument("--display-name", default=None)
    passwd = users_sub.add_parser("passwd", help="비밀번호 재설정(그 사용자의 모든 세션 폐기)")
    passwd.add_argument("username")
    for command in (add, passwd):
        command.add_argument(
            "--password-file", type=Path, default=None, help="비밀번호 파일(없으면 stdin 첫 줄)"
        )
    disable = users_sub.add_parser("disable", help="계정 비활성화(모든 세션 즉시 폐기)")
    disable.add_argument("username")
    enable = users_sub.add_parser("enable", help="계정 다시 활성화")
    enable.add_argument("username")
    users_sub.add_parser("list", help="계정 목록")
    for command in (add, passwd, disable, enable, users_sub.choices["list"]):
        command.add_argument("--json", action="store_true")


def _serve(settings: Settings) -> int:
    app = create_app(SiteRuntime.build(settings))
    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
        timeout_graceful_shutdown=HTTP_GRACEFUL_SHUTDOWN_S,
    )
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


def _apply_definitions(args: argparse.Namespace, settings: Settings) -> int:
    project = load_project(args.project_dir)
    if not project.ok:
        for problem in project.problems:
            print(f"error: {problem}", file=sys.stderr)
        return migrate.EXIT_FAILED

    async def run(conn: migrate.Connection) -> int:
        result = await apply_definitions(conn, project)
        if args.json:
            print(json.dumps(result.as_json(), sort_keys=True))
        else:
            print(
                f"apply-definitions: rev={result.rev} changed={result.changed} "
                f"devices +{result.devices_added} ~{result.devices_updated} "
                f"-{result.devices_retired} tags +{result.tags_added} ~{result.tags_updated} "
                f"-{result.tags_retired} historian {'~' if result.historian_changed else '='}"
            )
        return migrate.EXIT_OK

    return asyncio.run(_with_connection(settings, run))


def read_password(password_file: Path | None) -> str:
    """`--password-file`의 첫 줄, 없으면 stdin(터미널이면 에코 없이 묻는다). 끝 줄바꿈만 뗀다."""
    if password_file is not None:
        text = password_file.read_text(encoding="utf-8")
    elif sys.stdin.isatty():
        text = getpass.getpass("password: ")
    else:
        text = sys.stdin.readline()
    return text.splitlines()[0] if text else ""


def _user_json(user: User) -> dict[str, object]:
    return {
        "username": user.username,
        "role": user.role.label,
        "display_name": user.display_name,
        "enabled": user.enabled,
        "created_at": user.created_at.isoformat(),
    }


_USERS_AUDIT = {
    "add": AuditAction.USERS_ADD,
    "passwd": AuditAction.USERS_PASSWD,
    "disable": AuditAction.USERS_DISABLE,
    "enable": AuditAction.USERS_ENABLE,
}


async def _users(
    args: argparse.Namespace, settings: Settings, password: str | None
) -> tuple[User, ...]:
    """계정 명령을 실행하고 결과 사용자(list면 전부)를 돌려준다.

    바꾸는 명령은 감사 기록(source=cli, 행위자 없음 — CLI는 로그인 사용자가 아니다)을 남긴다.
    """
    db = Database(settings.db_dsn, password=read_secret(settings.db_password_file), max_size=1)
    service = AuthService(PgUserStore(db), PgSessionStore(db))
    try:
        command: str = args.users_command
        if command == "list":
            return await service.list_users()
        username: str = args.username
        detail: dict[str, object] = {}
        if command == "add":
            user = await service.add_user(
                username,
                password or "",
                Role.from_label(args.role),
                display_name=args.display_name,
            )
            detail = {"role": user.role.label}
        elif command == "passwd":
            user = await service.set_password(username, password or "")
        else:
            user = await service.set_enabled(username, command == "enable")
        await PgAuditStore(db).record(
            AuditEntry(
                action=_USERS_AUDIT[command], source=AuditSource.CLI, target=username, detail=detail
            )
        )
        return (user,)
    finally:
        await db.close()


def _run_users(args: argparse.Namespace, settings: Settings) -> int:
    needs_password = args.users_command in ("add", "passwd")
    password = read_password(args.password_file) if needs_password else None
    users = asyncio.run(_users(args, settings, password))
    if args.json:
        payload = [_user_json(u) for u in users]
        print(json.dumps(payload if args.users_command == "list" else payload[0], sort_keys=True))
        return 0
    if args.users_command == "list":
        for user in users:
            state = "enabled" if user.enabled else "disabled"
            print(f"{user.username}\t{user.role.label}\t{state}\t{user.display_name or ''}")
        return 0
    user = users[0]
    print(
        f"users {args.users_command}: {user.username} role={user.role.label} "
        f"enabled={str(user.enabled).lower()}"
    )
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
        handlers: dict[str, Callable[[argparse.Namespace, Settings], int]] = {
            "apply-definitions": _apply_definitions,
            "users": _run_users,
            "project": _run_project,
        }
        return handlers[args.command](args, settings)
    except migrate.MigrationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except (
        AuthError,
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
