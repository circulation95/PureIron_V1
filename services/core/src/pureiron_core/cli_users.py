"""`pureiron-core users` 하위 명령: 계정 관리(CLI 전용, plan M3b D3). 진입점 `cli.py`가 등록한다.

비밀번호는 argv로 받지 않는다 — `--password-file`(첫 줄, BOM 허용) 또는 stdin(터미널이면 에코 없이).
바꾸는 명령은 감사 기록(source=cli, 행위자 없음 — CLI는 로그인 사용자가 아니다)을 남긴다.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from pureiron_core.audit.ports import AuditAction, AuditEntry, AuditSource
from pureiron_core.audit.store import PgAuditStore
from pureiron_core.auth.ports import InvalidPasswordError, Role, User
from pureiron_core.auth.service import AuthService
from pureiron_core.auth.sessions import PgSessionStore
from pureiron_core.auth.users import PgUserStore
from pureiron_core.platform.db import Database
from pureiron_core.settings import Settings, read_secret


def configure_parser(users: argparse.ArgumentParser) -> None:
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


def read_password(password_file: Path | None) -> str:
    """`--password-file`의 첫 줄, 없으면 stdin(터미널이면 에코 없이 묻는다). 끝 줄바꿈만 뗀다."""
    if password_file is not None:
        try:
            # utf-8-sig: PowerShell 5.1 `Out-File -Encoding utf8`이 붙이는 BOM을 떼고 읽는다.
            text = password_file.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            raise InvalidPasswordError(f"{password_file}: password file is not UTF-8") from None
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


def run(args: argparse.Namespace, settings: Settings) -> int:
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
