"""현장 secret 파일(PD-4): `state/secrets/<name>`, 0600.

프로젝트는 `secret://<name>`만 참조하고 값은 이 디렉터리에만 둔다. compose는 파일을 그대로
bind mount하므로(비 swarm `secrets:`는 uid/mode를 바꾸지 못한다) 컨테이너 서비스 사용자
(`PUREIRON_SECRET_UID`, 기본 10001)가 읽을 수 있게 root로 실행할 때 소유자를 그 uid로 바꾼다.
site-db(postgres)는 entrypoint가 root일 때 파일을 읽으므로 소유자와 무관하다.

알려진 이름: `db_password`·`updater_token`(설치 때 `--generate`), `control_api_token`(온라인 현장,
벤더가 등록 때 1회 발급).
"""

from __future__ import annotations

import base64
import os
import re
import sys
from pathlib import Path

from pureiron_updater.state import fsync_dir

NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
WELL_KNOWN = ("db_password", "updater_token", "control_api_token")


class SecretError(ValueError):
    pass


def generate_secret() -> bytes:
    """URL-safe base64 32바이트 난수(셸·DSN·HTTP 헤더에 그대로 쓸 수 있다)."""
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=")


def value_from_input(data: bytes) -> bytes:
    """stdin 값: 끝 줄바꿈 하나(`\\n`·`\\r\\n`)만 뗀다. 비었거나 중간에 줄바꿈이 있으면 거부."""
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    if not data.strip():
        raise SecretError("secret value is empty")
    if b"\n" in data or b"\r" in data:
        raise SecretError("secret value must be a single line")
    return data


def set_secret(
    secrets_dir: Path, name: str, value: bytes, *, uid: int | None, if_missing: bool = False
) -> bool:
    """`secrets_dir/name`을 원자적으로 쓴다(0600). `if_missing`이고 이미 있으면 False."""
    if not NAME.fullmatch(name):
        raise SecretError(f"invalid secret name {name!r} (lowercase, digits, _)")
    secrets_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = secrets_dir / name
    if if_missing and path.exists():
        return False
    tmp = secrets_dir / f".{name}.tmp"
    tmp.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    if sys.platform != "win32" and uid is not None and os.geteuid() == 0:
        os.chown(tmp, uid, uid)
    tmp.replace(path)
    fsync_dir(secrets_dir)
    return True
