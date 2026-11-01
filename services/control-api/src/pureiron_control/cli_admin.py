"""`pureiron-control token|site` 하위 명령: 사람·CI token 발급과 현장 등록(CLI 전용, plan M4 C5).

token 원문은 발급 때 stdout에 한 번만 나온다(DB에는 SHA-256). 진입점 `cli.py`가 등록만 한다 —
REST(`POST /sites` 등)와 같은 도메인 함수를 쓴다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pureiron_control.credentials import CredentialError, PgCredentialStore, Role
from pureiron_control.platform.clock import utc_now
from pureiron_control.settings import Settings, read_secret
from pureiron_control.sites import (
    PgSiteRegistry,
    SiteError,
    parse_enrollment,
)
from pureiron_core.platform.db import Database


class AdminError(Exception):
    pass


def configure_parsers(token: argparse.ArgumentParser, site: argparse.ArgumentParser) -> None:
    token_sub = token.add_subparsers(dest="token_command", required=True)
    create = token_sub.add_parser("create", help="새 token(원문은 이때 한 번만 출력)")
    create.add_argument("--role", required=True, choices=[role.value for role in Role])
    create.add_argument("--name", required=True, help="용도 이름(유효 token 사이에서 유일)")
    revoke = token_sub.add_parser("revoke", help="token 폐기(즉시 401)")
    revoke.add_argument("--name", required=True)
    token_sub.add_parser("list", help="token 목록(원문 없음)")

    site_sub = site.add_subparsers(dest="site_command", required=True)
    register = site_sub.add_parser("register", help="enrollment.json으로 신규 등록 → 현장 token")
    register.add_argument("enrollment", type=Path)
    rekey = site_sub.add_parser("rekey", help="키 분실 재등록(구 키 폐기) → next_seq")
    rekey.add_argument("site")
    rekey.add_argument("enrollment", type=Path)
    reissue = site_sub.add_parser("token", help="현장 token 재발급(구 token 무효)")
    reissue.add_argument("site")
    for command in (create, revoke, token_sub.choices["list"], register, rekey, reissue):
        command.add_argument("--json", action="store_true")


def _emit(args: argparse.Namespace, value: dict[str, object], text: str) -> int:
    print(json.dumps(value, sort_keys=True) if args.json else text)
    return 0


async def _token(args: argparse.Namespace, store: PgCredentialStore) -> int:
    command: str = args.token_command
    if command == "create":
        created, secret = await store.create_api_token(args.name, Role(args.role))
        value: dict[str, object] = {"id": created.token_id, "name": created.name}
        value |= {"role": created.role.value, "token": secret}
        return _emit(args, value, secret)
    if command == "revoke":
        if not await store.revoke_api_token(args.name):
            raise AdminError(f"no active token named {args.name!r}")
        return _emit(args, {"revoked": args.name}, f"revoked {args.name}")
    tokens = await store.list_api_tokens()
    rows: list[object] = [
        {
            "id": t.token_id,
            "name": t.name,
            "role": t.role.value,
            "created_at": t.created_at.isoformat(),
            "revoked_at": None if t.revoked_at is None else t.revoked_at.isoformat(),
        }
        for t in tokens
    ]
    lines = [
        f"{t.token_id}\t{t.name}\t{t.role.value}\t{'revoked' if t.revoked_at else 'active'}"
        for t in tokens
    ]
    return _emit(args, {"tokens": rows}, "\n".join(lines))


async def _site(args: argparse.Namespace, registry: PgSiteRegistry) -> int:
    command: str = args.site_command
    if command == "token":
        token = await registry.reissue_token(args.site)
        return _emit(args, {"site_id": args.site, "token": token}, token)
    enrollment = parse_enrollment(args.enrollment.read_bytes())
    if command == "register":
        done, token = await registry.register(enrollment)
        value: dict[str, object] = {"site_id": done.site_id, "fingerprint": done.fingerprint}
        value |= {"next_seq": done.next_seq, "token": token}
        return _emit(args, value, token)
    done = await registry.rekey(args.site, enrollment)
    value = {"site_id": done.site_id, "fingerprint": done.fingerprint, "next_seq": done.next_seq}
    return _emit(args, value, f"next_seq={done.next_seq}")


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    db = Database(settings.db_dsn, password=read_secret(settings.db_password_file), max_size=1)
    try:
        if args.command == "token":
            return await _token(args, PgCredentialStore(db, clock=utc_now))
        return await _site(args, PgSiteRegistry(db, clock=utc_now))
    except (CredentialError, SiteError) as exc:
        raise AdminError(str(exc)) from exc
    finally:
        await db.close()


def run(args: argparse.Namespace, settings: Settings) -> int:
    return asyncio.run(_run(args, settings))
