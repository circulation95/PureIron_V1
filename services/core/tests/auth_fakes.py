"""auth 테스트용 가짜 저장소·시계(`UserStore`·`SessionStore` Protocol을 직접 만족한다)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from pureiron_core.auth.ports import AuthUnavailableError, Role, UserExistsError, UserNotFoundError
from pureiron_core.auth.service import AuthService
from pureiron_core.auth.sessions import SessionRecord
from pureiron_core.auth.users import UserRecord

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeUsers:
    records: dict[str, UserRecord] = field(default_factory=dict[str, UserRecord])
    available: bool = True

    def check(self) -> None:
        if not self.available:
            raise AuthUnavailableError("site-db error: down")

    async def get(self, username: str) -> UserRecord | None:
        self.check()
        return self.records.get(username)

    async def list_all(self) -> tuple[UserRecord, ...]:
        self.check()
        return tuple(self.records[name] for name in sorted(self.records))

    async def add(
        self, *, username: str, display_name: str | None, role: Role, password_hash: str
    ) -> UserRecord:
        self.check()
        if username in self.records:
            raise UserExistsError(username)
        record = UserRecord(
            user_id=len(self.records) + 1,
            username=username,
            display_name=display_name,
            role=role,
            password_hash=password_hash,
            enabled=True,
            created_at=T0,
        )
        self.records[username] = record
        return record

    async def set_password(self, username: str, password_hash: str) -> UserRecord:
        return self._replace(username, password_hash=password_hash)

    async def set_enabled(self, username: str, enabled: bool) -> UserRecord:
        return self._replace(username, enabled=enabled)

    def _replace(self, username: str, **changes: object) -> UserRecord:
        self.check()
        current = self.records.get(username)
        if current is None:
            raise UserNotFoundError(username)
        updated = replace(current, **changes)
        self.records[username] = updated
        return updated


@dataclass
class SessionRow:
    user_id: int
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass
class FakeSessions:
    users: FakeUsers
    rows: dict[bytes, SessionRow] = field(default_factory=dict[bytes, SessionRow])
    touches: list[datetime] = field(default_factory=list[datetime])

    def _user(self, user_id: int) -> UserRecord:
        return next(r for r in self.users.records.values() if r.user_id == user_id)

    async def create(
        self, token_sha256: bytes, user_id: int, *, now: datetime, expires_at: datetime
    ) -> None:
        self.users.check()  # 같은 DB를 쓰는 저장소라 장애도 함께 난다
        self.rows[token_sha256] = SessionRow(user_id, now, expires_at)

    async def lookup(self, token_sha256: bytes) -> SessionRecord | None:
        self.users.check()
        row = self.rows.get(token_sha256)
        if row is None:
            return None
        user = self._user(row.user_id)
        return SessionRecord(
            user_id=user.user_id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            enabled=user.enabled,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )

    async def touch(self, token_sha256: bytes, at: datetime) -> None:
        self.touches.append(at)
        self.rows[token_sha256].last_seen_at = at

    async def revoke(self, token_sha256: bytes, at: datetime) -> None:
        row = self.rows.get(token_sha256)
        if row is not None and row.revoked_at is None:
            row.revoked_at = at

    async def revoke_user(self, user_id: int, at: datetime, *, keep: bytes | None = None) -> None:
        for digest, row in self.rows.items():
            if row.user_id == user_id and row.revoked_at is None and digest != keep:
                row.revoked_at = at


@dataclass
class AuthFixture:
    service: AuthService
    users: FakeUsers
    sessions: FakeSessions
    clock: Clock


def auth_fixture(*, idle_s: float = 43_200.0, absolute_s: float = 604_800.0) -> AuthFixture:
    users = FakeUsers()
    sessions = FakeSessions(users)
    clock = Clock()
    service = AuthService(users, sessions, idle_s=idle_s, absolute_s=absolute_s, clock=clock)
    return AuthFixture(service, users, sessions, clock)
