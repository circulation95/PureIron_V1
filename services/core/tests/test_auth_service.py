"""AuthService: 로그인 성공·실패·비활성, 유휴·절대 만료 경계, 로그아웃·비활성화 즉시 무효,
last_seen 1분 throttle, 비밀번호 변경·재설정의 세션 폐기, 계정 입력 검증(가짜 저장소·주입 시계)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import replace
from datetime import timedelta

import pytest

from pureiron_core.auth.ports import (
    AuthUnavailableError,
    InvalidAccountError,
    InvalidCredentialsError,
    InvalidPasswordError,
    Principal,
    Role,
    SessionInvalidError,
    UserExistsError,
    UserNotFoundError,
)

from .auth_fakes import T0, AuthFixture, auth_fixture

PASSWORD = "correct-horse-1"


async def _with_user(
    fx: AuthFixture, username: str = "alice", role: Role = Role.OPERATOR
) -> AuthFixture:
    await fx.service.add_user(username, PASSWORD, role, display_name="  Alice Kim ")
    return fx


async def test_login_creates_session_storing_only_the_token_digest() -> None:
    fx = await _with_user(auth_fixture())
    result = await fx.service.login("alice", PASSWORD)
    assert result.principal == Principal(1, "alice", Role.OPERATOR, "Alice Kim")
    assert result.expires_at == T0 + timedelta(days=7)
    assert len(result.token) >= 43  # token_urlsafe(32) = 43자
    (digest,) = fx.sessions.rows
    assert digest == hashlib.sha256(result.token.encode()).digest()
    assert result.token.encode() not in digest
    row = fx.sessions.rows[digest]
    assert (row.last_seen_at, row.expires_at) == (T0, T0 + timedelta(days=7))


async def test_each_login_gets_a_new_token() -> None:
    fx = await _with_user(auth_fixture())
    first = await fx.service.login("alice", PASSWORD)
    second = await fx.service.login("alice", PASSWORD)
    assert first.token != second.token
    assert len(fx.sessions.rows) == 2


@pytest.mark.parametrize(
    ("username", "password", "reason"),
    [
        ("alice", "wrong-password", "bad_password"),
        ("alice", "", "bad_password"),
        ("nobody", PASSWORD, "unknown_user"),
        ("Alice", PASSWORD, "unknown_user"),
    ],
)
async def test_login_failures_share_one_message(username: str, password: str, reason: str) -> None:
    fx = await _with_user(auth_fixture())
    with pytest.raises(InvalidCredentialsError, match="invalid username or password") as caught:
        await fx.service.login(username, password)
    assert caught.value.reason == reason
    assert fx.sessions.rows == {}


async def test_disabled_user_cannot_log_in_but_wrong_password_does_not_reveal_it() -> None:
    fx = await _with_user(auth_fixture())
    await fx.service.set_enabled("alice", False)
    with pytest.raises(InvalidCredentialsError) as right:
        await fx.service.login("alice", PASSWORD)
    assert right.value.reason == "disabled"
    with pytest.raises(InvalidCredentialsError) as wrong:
        await fx.service.login("alice", "wrong-password")
    assert wrong.value.reason == "bad_password"


async def test_corrupt_stored_hash_refuses_login_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fx = await _with_user(auth_fixture())
    fx.users.records["alice"] = replace(fx.users.records["alice"], password_hash="garbage")
    with caplog.at_level(logging.WARNING), pytest.raises(InvalidCredentialsError):
        await fx.service.login("alice", PASSWORD)
    assert "unusable" in caplog.text


async def test_password_hashing_runs_off_the_event_loop() -> None:
    """scrypt(수십 ms)가 이벤트 루프를 막지 않는다: 로그인 동안 다른 task가 계속 돈다."""
    fx = await _with_user(auth_fixture())
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(ticker())
    try:
        await fx.service.login("alice", PASSWORD)
    finally:
        task.cancel()
    assert ticks > 10


async def test_authenticate_returns_principal_and_rejects_unknown_tokens() -> None:
    fx = await _with_user(auth_fixture(), role=Role.ENGINEER)
    token = (await fx.service.login("alice", PASSWORD)).token
    principal = await fx.service.authenticate(token)
    assert (principal.username, principal.role) == ("alice", Role.ENGINEER)
    with pytest.raises(SessionInvalidError, match="unknown"):
        await fx.service.authenticate(token + "x")
    with pytest.raises(SessionInvalidError, match="missing"):
        await fx.service.authenticate("")


async def test_idle_timeout_boundary() -> None:
    fx = await _with_user(auth_fixture(idle_s=100, absolute_s=1000))
    still = (await fx.service.login("alice", PASSWORD)).token
    expired = (await fx.service.login("alice", PASSWORD)).token
    fx.clock.advance(99.999)
    await fx.service.authenticate(still)
    fx.clock.advance(0.001)  # 마지막 사용 후 정확히 idle_s
    with pytest.raises(SessionInvalidError, match="idle"):
        await fx.service.authenticate(expired)


async def test_absolute_expiry_wins_over_recent_use() -> None:
    fx = await _with_user(auth_fixture(idle_s=100, absolute_s=300))
    token = (await fx.service.login("alice", PASSWORD)).token
    for _ in range(4):  # 60s마다 사용(그때마다 last_seen 갱신) → 유휴 만료는 오지 않는다
        fx.clock.advance(60)
        await fx.service.authenticate(token)
    fx.clock.now = T0 + timedelta(seconds=299.999)
    await fx.service.authenticate(token)
    fx.clock.now = T0 + timedelta(seconds=300)
    with pytest.raises(SessionInvalidError, match="expired"):
        await fx.service.authenticate(token)


async def test_last_seen_is_written_at_most_once_a_minute() -> None:
    fx = await _with_user(auth_fixture())
    token = (await fx.service.login("alice", PASSWORD)).token
    for offset_s in (30, 59.999, 60, 90, 119.999, 120, 121):
        fx.clock.now = T0 + timedelta(seconds=offset_s)
        await fx.service.authenticate(token)
    assert fx.sessions.touches == [T0 + timedelta(seconds=60), T0 + timedelta(seconds=120)]


async def test_logout_revokes_immediately_and_unknown_logout_is_harmless() -> None:
    fx = await _with_user(auth_fixture())
    token = (await fx.service.login("alice", PASSWORD)).token
    other = (await fx.service.login("alice", PASSWORD)).token
    await fx.service.logout(token)
    with pytest.raises(SessionInvalidError, match="revoked"):
        await fx.service.authenticate(token)
    await fx.service.authenticate(other)  # 다른 세션은 그대로
    await fx.service.logout("never-issued")


async def test_disabling_a_user_kills_existing_sessions_even_after_re_enable() -> None:
    fx = await _with_user(auth_fixture())
    token = (await fx.service.login("alice", PASSWORD)).token
    await fx.service.set_enabled("alice", False)
    with pytest.raises(SessionInvalidError):
        await fx.service.authenticate(token)
    await fx.service.set_enabled("alice", True)
    with pytest.raises(SessionInvalidError, match="revoked"):
        await fx.service.authenticate(token)
    await fx.service.authenticate((await fx.service.login("alice", PASSWORD)).token)


async def test_user_disabled_flag_alone_rejects_session() -> None:
    """폐기 표시가 없어도(예: DB에서 직접 끈 경우) enabled=false면 거부한다."""
    fx = await _with_user(auth_fixture())
    token = (await fx.service.login("alice", PASSWORD)).token
    fx.users.records["alice"] = replace(fx.users.records["alice"], enabled=False)
    with pytest.raises(SessionInvalidError, match="disabled"):
        await fx.service.authenticate(token)


async def test_change_password_keeps_this_session_and_revokes_others() -> None:
    fx = await _with_user(auth_fixture())
    current = (await fx.service.login("alice", PASSWORD)).token
    other = (await fx.service.login("alice", PASSWORD)).token
    principal = await fx.service.authenticate(current)
    await fx.service.change_password(principal, current, PASSWORD, "brand-new-pw-2")
    await fx.service.authenticate(current)
    with pytest.raises(SessionInvalidError):
        await fx.service.authenticate(other)
    with pytest.raises(InvalidCredentialsError):
        await fx.service.login("alice", PASSWORD)
    await fx.service.login("alice", "brand-new-pw-2")


@pytest.mark.parametrize(
    ("current", "new", "error"),
    [
        ("wrong-current", "brand-new-pw-2", InvalidCredentialsError),
        (PASSWORD, "short7!", InvalidPasswordError),
        (PASSWORD, "x" * 129, InvalidPasswordError),
        (PASSWORD, PASSWORD, InvalidPasswordError),
    ],
)
async def test_change_password_rejections_leave_password_unchanged(
    current: str, new: str, error: type[Exception]
) -> None:
    fx = await _with_user(auth_fixture())
    token = (await fx.service.login("alice", PASSWORD)).token
    principal = await fx.service.authenticate(token)
    with pytest.raises(error):
        await fx.service.change_password(principal, token, current, new)
    await fx.service.login("alice", PASSWORD)


async def test_password_reset_revokes_sessions_before_storing() -> None:
    """폐기가 먼저다: 비밀번호 저장이 실패해도 옛 세션은 이미 무효다(안전한 쪽으로 실패)."""
    fx = await _with_user(auth_fixture())
    token = (await fx.service.login("alice", PASSWORD)).token
    fx.users.fail_writes = True
    with pytest.raises(AuthUnavailableError):
        await fx.service.set_password("alice", "reset-by-admin")
    fx.users.fail_writes = False
    with pytest.raises(SessionInvalidError):
        await fx.service.authenticate(token)
    await fx.service.login("alice", PASSWORD)  # 비밀번호는 그대로


async def test_admin_password_reset_revokes_every_session() -> None:
    fx = await _with_user(auth_fixture())
    token = (await fx.service.login("alice", PASSWORD)).token
    await fx.service.set_password("alice", "reset-by-admin")
    with pytest.raises(SessionInvalidError):
        await fx.service.authenticate(token)
    await fx.service.login("alice", "reset-by-admin")
    with pytest.raises(UserNotFoundError):
        await fx.service.set_password("nobody", "reset-by-admin")


async def test_add_user_validates_input_and_rejects_duplicates() -> None:
    fx = await _with_user(auth_fixture())
    user = (await fx.service.list_users())[0]
    assert (user.username, user.display_name, user.enabled) == ("alice", "Alice Kim", True)
    with pytest.raises(UserExistsError):
        await fx.service.add_user("alice", PASSWORD, Role.VIEWER)
    for bad in ("Bob", "ab", "b" * 33, "bob smith"):
        with pytest.raises(InvalidAccountError):
            await fx.service.add_user(bad, PASSWORD, Role.VIEWER)
    for password in ("x" * 7, "x" * 129):
        with pytest.raises(InvalidPasswordError):
            await fx.service.add_user("bob", password, Role.VIEWER)
    with pytest.raises(InvalidAccountError):
        await fx.service.add_user("bob", PASSWORD, Role.VIEWER, display_name="n" * 65)
    blank = await fx.service.add_user("bob", PASSWORD, Role.VIEWER, display_name="   ")
    assert blank.display_name is None
    assert [u.username for u in await fx.service.list_users()] == ["alice", "bob"]


async def test_store_outage_surfaces_as_unavailable() -> None:
    fx = await _with_user(auth_fixture())
    token = (await fx.service.login("alice", PASSWORD)).token
    fx.users.available = False
    with pytest.raises(AuthUnavailableError):
        await fx.service.authenticate(token)
    with pytest.raises(AuthUnavailableError):
        await fx.service.login("alice", PASSWORD)


def test_role_levels_compare_by_rank() -> None:
    assert Role.VIEWER < Role.OPERATOR < Role.ENGINEER < Role.ADMIN
    assert [int(r) for r in Role] == [0, 1, 2, 3]
    assert Role.from_label("engineer") is Role.ENGINEER
    assert Role.ADMIN.label == "admin"
    with pytest.raises(ValueError, match="unknown role"):
        Role.from_label("root")
