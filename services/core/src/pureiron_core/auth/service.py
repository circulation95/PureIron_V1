"""로그인·세션 검증·로그아웃·비밀번호 변경과 CLI 계정 관리(plan M3b D2·D3·D4).

세션 = `secrets.token_urlsafe(32)` 불투명 토큰, DB에는 sha256만. 유효 조건은
`now < expires_at`(절대 만료), `now < last_seen_at + idle_s`(유휴 만료), 폐기되지 않음, 사용자
enabled 넷 모두. `last_seen_at`은 `min(TOUCH_INTERVAL_S, idle_s / 2)`에 한 번만 쓴다(요청마다
UPDATE 하지 않게). 간격이 유휴 만료의 절반을 넘지 않아야 활동 중인 세션이 갱신 전에 끊기지 않는다.
로그인 실패는 이유와 무관하게 같은 예외·비슷한 시간(없는 사용자도 더미 해시를 검증)으로 끝난다.
감사 기록은 이 모듈이 하지 않는다 — 호출자(api·cli)가 결과를 보고 남긴다(auth는 platform에만 의존).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pureiron_core.auth.passwords import (
    PasswordHashError,
    hash_password,
    password_problem,
    username_problem,
    verify_password,
)
from pureiron_core.auth.ports import (
    InvalidAccountError,
    InvalidCredentialsError,
    InvalidPasswordError,
    LoginResult,
    Principal,
    Role,
    SessionInvalidError,
    User,
    UserNotFoundError,
)
from pureiron_core.auth.sessions import SessionRecord
from pureiron_core.auth.users import UserRecord

log = logging.getLogger(__name__)

DEFAULT_SESSION_IDLE_S = 12 * 3600.0
DEFAULT_SESSION_ABSOLUTE_S = 7 * 24 * 3600.0
TOUCH_INTERVAL_S = 60.0
TOKEN_BYTES = 32
# 동시에 도는 scrypt 수 상한(한 번에 ~16 MiB·수십 ms). 로그인이 몰려도 CPU·기본 executor 스레드를
# 다른 `to_thread` 사용처와 다투며 core 전체를 늦추지 않게 한다 — 넘치는 요청은 기다린다.
MAX_CONCURRENT_HASHES = 4
DISPLAY_NAME_MAX_CHARS = 64
# 없는 사용자 로그인도 실제 검증과 같은 비용을 쓰게 하는 해시(아무 비밀번호와도 맞지 않는 임의 값).
# 매개변수는 passwords.DEFAULT_*와 같아야 한다(test_passwords가 확인).
DUMMY_HASH = "scrypt$16384$8$1$DZB2GXCbB3yo27Az5I3TGw$HfGkLOoUuy/qCdBGI7tizeVkzqmzHRyGaHA6zMFN8kw"


class UserStore(Protocol):
    async def get(self, username: str) -> UserRecord | None: ...

    async def list_all(self) -> tuple[UserRecord, ...]: ...

    async def add(
        self, *, username: str, display_name: str | None, role: Role, password_hash: str
    ) -> UserRecord:
        """같은 username이 있으면 `UserExistsError`."""
        ...

    async def set_password(self, username: str, password_hash: str) -> UserRecord:
        """없는 사용자면 `UserNotFoundError`."""
        ...

    async def set_enabled(self, username: str, enabled: bool) -> UserRecord: ...


class SessionStore(Protocol):
    async def create(
        self, token_sha256: bytes, user_id: int, *, now: datetime, expires_at: datetime
    ) -> None: ...

    async def lookup(self, token_sha256: bytes) -> SessionRecord | None: ...

    async def touch(self, token_sha256: bytes, at: datetime) -> None: ...

    async def revoke(self, token_sha256: bytes, at: datetime) -> None: ...

    async def revoke_user(
        self, user_id: int, at: datetime, *, keep: bytes | None = None
    ) -> None: ...


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _display_name(value: str | None) -> str | None:
    text = (value or "").strip()
    if len(text) > DISPLAY_NAME_MAX_CHARS:
        raise InvalidAccountError(
            f"display name must be at most {DISPLAY_NAME_MAX_CHARS} characters, got {len(text)}"
        )
    return text or None


def _check_password(password: str) -> None:
    if (problem := password_problem(password)) is not None:
        raise InvalidPasswordError(problem)


class AuthService:
    def __init__(
        self,
        users: UserStore,
        sessions: SessionStore,
        *,
        idle_s: float = DEFAULT_SESSION_IDLE_S,
        absolute_s: float = DEFAULT_SESSION_ABSOLUTE_S,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._idle = timedelta(seconds=idle_s)
        self._absolute = timedelta(seconds=absolute_s)
        # idle_s ≤ 60이면 60초 간격 갱신은 유휴 만료 뒤에야 온다 — 활동 중인 세션도 끊긴다.
        self._touch_interval = timedelta(seconds=min(TOUCH_INTERVAL_S, idle_s / 2))
        self._clock = clock
        self._hashing = asyncio.Semaphore(MAX_CONCURRENT_HASHES)

    async def _verify(self, password: str, stored: str) -> bool:
        try:
            async with self._hashing:
                return await asyncio.to_thread(verify_password, password, stored)
        except PasswordHashError as exc:
            log.warning("stored password hash is unusable, login refused: %s", exc)
            return False

    async def _hash(self, password: str) -> str:
        async with self._hashing:
            return await asyncio.to_thread(hash_password, password)

    # --- 세션(AuthPort) ---------------------------------------------------------------------

    async def login(self, username: str, password: str) -> LoginResult:
        user = await self._users.get(username)
        matched = await self._verify(password, DUMMY_HASH if user is None else user.password_hash)
        if user is None:
            raise InvalidCredentialsError("unknown_user")
        if not matched:
            raise InvalidCredentialsError("bad_password")
        if not user.enabled:
            raise InvalidCredentialsError("disabled")
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = self._clock()
        expires_at = now + self._absolute
        await self._sessions.create(
            token_digest(token), user.user_id, now=now, expires_at=expires_at
        )
        return LoginResult(token=token, principal=user.principal(), expires_at=expires_at)

    async def authenticate(self, token: str) -> Principal:
        if not token:
            raise SessionInvalidError("missing session token")
        digest = token_digest(token)
        record = await self._sessions.lookup(digest)
        now = self._clock()
        if record is None:
            raise SessionInvalidError("unknown session")
        if record.revoked_at is not None:
            raise SessionInvalidError("session revoked")
        if not record.enabled:
            raise SessionInvalidError("user disabled")
        if now >= record.expires_at:
            raise SessionInvalidError("session expired")
        if now >= record.last_seen_at + self._idle:
            raise SessionInvalidError("session idle timeout")
        if now - record.last_seen_at >= self._touch_interval:
            await self._sessions.touch(digest, now)
        return record.principal()

    async def logout(self, token: str) -> None:
        await self._sessions.revoke(token_digest(token), self._clock())

    async def change_password(
        self, principal: Principal, token: str, current: str, new: str
    ) -> None:
        _check_password(new)
        user = await self._users.get(principal.username)
        if user is None or not await self._verify(current, user.password_hash):
            raise InvalidCredentialsError("bad_password")
        if new == current:
            raise InvalidPasswordError("new password must differ from the current password")
        hashed = await self._hash(new)
        # 다른 세션을 먼저 폐기한다: 그 뒤 저장이 실패해도 탈취됐을 수 있는 세션이 남지 않는다.
        await self._sessions.revoke_user(user.user_id, self._clock(), keep=token_digest(token))
        await self._users.set_password(user.username, hashed)

    # --- 계정 관리(CLI 전용, plan D3) ----------------------------------------------------------

    async def add_user(
        self, username: str, password: str, role: Role, *, display_name: str | None = None
    ) -> User:
        if (problem := username_problem(username)) is not None:
            raise InvalidAccountError(problem)
        _check_password(password)
        name = _display_name(display_name)
        hashed = await self._hash(password)
        record = await self._users.add(
            username=username, display_name=name, role=role, password_hash=hashed
        )
        return record.public()

    async def set_password(self, username: str, password: str) -> User:
        """관리자 재설정: 그 사용자의 모든 세션을 폐기한다.

        폐기를 저장보다 먼저 한다 — 저장이 실패해도 옛 세션이 살아남지 않게.
        """
        _check_password(password)
        hashed = await self._hash(password)
        current = await self._users.get(username)
        if current is None:
            raise UserNotFoundError(username)
        await self._sessions.revoke_user(current.user_id, self._clock())
        return (await self._users.set_password(username, hashed)).public()

    async def set_enabled(self, username: str, enabled: bool) -> User:
        record = await self._users.set_enabled(username, enabled)
        if not enabled:
            await self._sessions.revoke_user(record.user_id, self._clock())
        return record.public()

    async def list_users(self) -> tuple[User, ...]:
        return tuple(record.public() for record in await self._users.list_all())
