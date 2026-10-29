"""`pureiron-core` 진입점: serve · migrate · project · apply-definitions · users.

`project apply --tar`·`export --with-state`·`restore --from <dir>`(M5-0)는 번들 트리를
적용·백업·복원한다(`project/apply.py`·`export.py`). tar 읽기·검증은 DB에 닿기 전에 끝낸다 —
실패하면 아무것도 바꾸지 않는다. `export --draft`(M5-2 P5)는 현장 드래프트용 유효 트리 +
`draft-meta.json`을 쓴다(적용된 트리가 없으면 실패).

종료 코드(plan §6.6): 0 성공, 2 lock, 3 checksum, 4 실패, 5 lint.
`runtime`/`app`을 import할 수 있는 유일한 모듈이다(import-linter 계약 (a)).
`users` 하위 명령의 parser·실행은 `cli_users.py`에 있고 여기서는 등록만 한다.
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
from hashlib import sha256
from pathlib import Path
from typing import cast

import asyncpg
import uvicorn
from fastapi import FastAPI

from pureiron_core import cli_users
from pureiron_core.api.ws import WS_MAX_MESSAGE_BYTES
from pureiron_core.app import create_app
from pureiron_core.auth.ports import AuthError
from pureiron_core.platform import migrate
from pureiron_core.platform.db import Database, DatabaseUnavailableError, connect_once
from pureiron_core.platform.migrate_lint import lint_directory
from pureiron_core.project.apply import ProjectTree, apply_tree, restore_tree, tree_from_tar
from pureiron_core.project.definitions import apply_definitions
from pureiron_core.project.export import (
    DRAFT_META_FILE,
    STATE_FILE,
    TREE_FILE,
    effective_files,
    tree_tar,
)
from pureiron_core.project.ports import ProjectStateError
from pureiron_core.project.stub import PgProjectStateStore, state_from_snapshot
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

    proj = sub.add_parser("project", help="현장 프로젝트 적용·백업·복원(ADR-025, M5-0)")
    proj_sub = proj.add_subparsers(dest="project_command", required=True)
    apply = proj_sub.add_parser(
        "apply", help="번들 project.tar를 적용(정의·project_files·project_state 한 트랜잭션)"
    )
    apply.add_argument(
        "--tar",
        type=Path,
        default=None,
        help="번들 project/project.tar. 없으면 project_state 메타만 기록한다(M5 이전 Updater 호환)",
    )
    apply.add_argument("--project-id", required=True)
    apply.add_argument("--version", required=True)
    apply.add_argument("--content-hash", required=True, help="manifest 값(트리 hash와 같아야 함)")
    export = proj_sub.add_parser(
        "export",
        help="project_state JSON. --with-state·--draft면 --out 디렉터리에 유효 트리와 함께",
    )
    export_kind = export.add_mutually_exclusive_group()
    export_kind.add_argument(
        "--with-state",
        action="store_true",
        help=f"--out 디렉터리에 {STATE_FILE} + 현재 유효 트리 {TREE_FILE}(Updater 백업)",
    )
    export_kind.add_argument(
        "--draft",
        action="store_true",
        help=f"--out 디렉터리에 유효 트리 {TREE_FILE} + {DRAFT_META_FILE}(현장 드래프트, "
        "서명은 pureiron-site project export). 적용된 트리가 없으면 실패",
    )
    export.add_argument("--out", type=Path, default=None)
    restore = proj_sub.add_parser(
        "restore", help="--from <state JSON | export --with-state 디렉터리>(site_rev·base 그대로)"
    )
    restore.add_argument("--from", dest="source", type=Path, required=True)

    defs = sub.add_parser(
        "apply-definitions",
        help="프로젝트 트리의 장비·태그 정의를 site-db에 반영(path 기준 upsert·retire, M3a). "
        "개발·수동 복구용 — project_files·project_state는 갱신하지 않으므로 현장 적용은 "
        "`project apply --tar`(Updater)로만",
    )
    defs.add_argument("project_dir", type=Path)
    defs.add_argument("--json", action="store_true")
    cli_users.configure_parser(
        sub.add_parser(
            "users",
            help="사용자 계정 관리(CLI 전용, plan M3b D3). 비밀번호는 stdin 또는 --password-file",
        )
    )
    return parser


def http_config(app: FastAPI, *, host: str, port: int) -> uvicorn.Config:
    """core HTTP·WS 서버 설정. WS 프레임 상한은 uvicorn 기본(16 MiB) 대신 `WS_MAX_MESSAGE_BYTES`
    — 인증 전에도 프레임을 받으므로(첫 메시지 auth) 이 값이 비인증 메모리 사용의 상한이다."""
    return uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        timeout_graceful_shutdown=HTTP_GRACEFUL_SHUTDOWN_S,
        ws_max_size=WS_MAX_MESSAGE_BYTES,
    )


def _serve(settings: Settings) -> int:
    app = create_app(SiteRuntime.build(settings))
    uvicorn.Server(http_config(app, host=settings.http_host, port=settings.http_port)).run()
    return 0


async def _with_connection[T](
    settings: Settings, action: Callable[[migrate.Connection], Awaitable[T]]
) -> T:
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


def _print_json(value: object) -> int:
    print(json.dumps(value, sort_keys=True))
    return migrate.EXIT_OK


def _apply_tar(args: argparse.Namespace, settings: Settings, tar: Path) -> int:
    tree = tree_from_tar(tar.read_bytes())

    async def run(conn: migrate.Connection) -> int:
        result = await apply_tree(
            conn,
            tree,
            project_id=args.project_id,
            version=args.version,
            content_hash=args.content_hash,
        )
        return _print_json(result.as_json())

    return asyncio.run(_with_connection(settings, run))


def _export_with_state(settings: Settings, out: Path | None) -> int:
    if out is None:
        raise ProjectStateError("export --with-state needs --out <directory>")
    state, files = asyncio.run(_with_connection(settings, effective_files))
    tar = tree_tar(files) if files else None
    if tar is not None:
        # 복원할 수 없는 백업을 만들지 않는다(백업 단계 실패는 서비스에 영향이 없다).
        tree_from_tar(tar)
    out.mkdir(parents=True, exist_ok=True)
    snapshot = state.to_snapshot()
    state_text = json.dumps(snapshot, sort_keys=True, indent=2) + "\n"
    (out / STATE_FILE).write_text(state_text, encoding="utf-8", newline="\n")
    if tar is None:
        (out / TREE_FILE).unlink(missing_ok=True)
    else:
        (out / TREE_FILE).write_bytes(tar)
    tree = None if tar is None else {"files": len(files), "sha256": sha256(tar).hexdigest()}
    return _print_json({"state": snapshot, "tree": tree})


def _export_draft(settings: Settings, out: Path | None) -> int:
    if out is None:
        raise ProjectStateError("export --draft needs --out <directory>")
    state, files = asyncio.run(_with_connection(settings, effective_files))
    if not files:
        raise ProjectStateError(
            "no applied project tree (project_files is empty) — apply a bundle first"
        )
    tar = tree_tar(files)
    meta = {
        "site_rev": state.site_rev,
        "base_site_rev": state.base_site_rev,
        "content_hash": tree_from_tar(tar).content_hash,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / TREE_FILE).write_bytes(tar)
    meta_text = json.dumps(meta, sort_keys=True, indent=2) + "\n"
    (out / DRAFT_META_FILE).write_text(meta_text, encoding="utf-8", newline="\n")
    return _print_json({**meta, "files": len(files), "sha256": sha256(tar).hexdigest()})


def _restore_dir(settings: Settings, source: Path) -> int:
    state = state_from_snapshot(_read_snapshot(source / STATE_FILE))
    tar = source / TREE_FILE
    tree: ProjectTree | None = tree_from_tar(tar.read_bytes()) if tar.is_file() else None

    async def run(conn: migrate.Connection) -> int:
        return _print_json((await restore_tree(conn, tree, state)).as_json())

    return asyncio.run(_with_connection(settings, run))


def _run_project(args: argparse.Namespace, settings: Settings) -> int:
    command: str = args.project_command
    if command == "apply" and args.tar is not None:
        return _apply_tar(args, settings, args.tar)
    if command == "export" and args.with_state:
        return _export_with_state(settings, args.out)
    if command == "export" and args.draft:
        return _export_draft(settings, args.out)
    if command == "restore" and args.source.is_dir():
        return _restore_dir(settings, args.source)
    snapshot = _read_snapshot(args.source) if command == "restore" else None
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
                f"-{result.tags_retired} alarms +{result.alarms_added} ~{result.alarms_updated} "
                f"-{result.alarms_retired} historian {'~' if result.historian_changed else '='}"
            )
        return migrate.EXIT_OK

    return asyncio.run(_with_connection(settings, run))


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
            "users": cli_users.run,
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
