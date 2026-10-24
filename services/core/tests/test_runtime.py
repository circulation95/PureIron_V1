from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pureiron_core.app import create_app
from pureiron_core.platform.bus import Event, EventBus
from pureiron_core.runtime import SiteRuntime
from pureiron_core.settings import Settings, SettingsError, read_secret


def _settings(tmp_path: Path, **overrides: str) -> Settings:
    env = {
        "PUREIRON_DB_DSN": "postgresql://pureiron@127.0.0.1:1/none",
        "PUREIRON_GRPC_PORT": "0",
        **overrides,
    }
    return Settings.from_env(env)


def test_settings_defaults_and_overrides(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("abc\n", encoding="utf-8")
    settings = Settings.from_env(
        {"PUREIRON_HTTP_PORT": "9000", "PUREIRON_UPDATER_TOKEN_FILE": str(token)}
    )
    assert settings.http_port == 9000
    assert settings.grpc_port == 50051
    assert (
        settings.plan_refresh_s,
        settings.ingest_live_max_age_s,
        settings.device_silence_min_s,
        settings.session_silence_s,
    ) == (2.0, 5.0, 5.0, 15.0)
    tuned = Settings.from_env(
        {"PUREIRON_PLAN_REFRESH_S": "0.5", "PUREIRON_DEVICE_SILENCE_MIN_S": "8"}
    )
    assert (tuned.plan_refresh_s, tuned.device_silence_min_s) == (0.5, 8.0)
    assert (
        settings.historian_buffer_max_rows,
        settings.historian_flush_s,
        settings.historian_flush_max_rows,
    ) == (600_000, 1.0, 20_000)
    historian = Settings.from_env(
        {
            "PUREIRON_HISTORIAN_BUFFER_MAX_ROWS": "1000",
            "PUREIRON_HISTORIAN_FLUSH_S": "0.25",
            "PUREIRON_HISTORIAN_FLUSH_MAX_ROWS": "500",
        }
    )
    assert (
        historian.historian_buffer_max_rows,
        historian.historian_flush_s,
        historian.historian_flush_max_rows,
    ) == (1000, 0.25, 500)
    assert settings.migrations_dir.name == "migrations"
    assert read_secret(settings.updater_token_file) == "abc"
    assert read_secret(None) is None


def test_auth_settings_defaults_and_overrides() -> None:
    defaults = Settings.from_env({})
    assert (
        defaults.auth_session_idle_s,
        defaults.auth_session_absolute_s,
        defaults.auth_cookie_secure,
    ) == (43_200.0, 604_800.0, False)
    tuned = Settings.from_env(
        {
            "PUREIRON_AUTH_SESSION_IDLE_S": "600",
            "PUREIRON_AUTH_SESSION_ABSOLUTE_S": "3600",
            "PUREIRON_AUTH_COOKIE_SECURE": "true",
        }
    )
    assert (tuned.auth_session_idle_s, tuned.auth_session_absolute_s) == (600.0, 3600.0)
    assert tuned.auth_cookie_secure is True
    for raw, expected in (("1", True), ("YES", True), ("off", False), ("0", False), ("", False)):
        settings = Settings.from_env({"PUREIRON_AUTH_COOKIE_SECURE": raw})
        assert settings.auth_cookie_secure is expected, raw
    with pytest.raises(SettingsError, match="PUREIRON_AUTH_COOKIE_SECURE"):
        Settings.from_env({"PUREIRON_AUTH_COOKIE_SECURE": "maybe"})
    with pytest.raises(SettingsError, match="PUREIRON_AUTH_SESSION_IDLE_S"):
        Settings.from_env({"PUREIRON_AUTH_SESSION_IDLE_S": "0"})
    with pytest.raises(SettingsError, match="at most"):
        Settings.from_env({"PUREIRON_AUTH_SESSION_ABSOLUTE_S": "1e20"})


def test_realtime_batch_setting() -> None:
    assert Settings.from_env({}).realtime_batch_ms == 250
    assert Settings.from_env({"PUREIRON_REALTIME_BATCH_MS": "100"}).realtime_batch_ms == 100
    with pytest.raises(SettingsError, match="PUREIRON_REALTIME_BATCH_MS"):
        Settings.from_env({"PUREIRON_REALTIME_BATCH_MS": "0"})


def test_settings_errors(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="PUREIRON_GRPC_PORT"):
        Settings.from_env({"PUREIRON_GRPC_PORT": "x"})
    with pytest.raises(SettingsError, match="PUREIRON_PLAN_REFRESH_S"):
        Settings.from_env({"PUREIRON_PLAN_REFRESH_S": "0"})
    with pytest.raises(SettingsError, match="PUREIRON_INGEST_LIVE_MAX_AGE_S"):
        Settings.from_env({"PUREIRON_INGEST_LIVE_MAX_AGE_S": "soon"})
    for key in ("PUREIRON_HISTORIAN_BUFFER_MAX_ROWS", "PUREIRON_HISTORIAN_FLUSH_MAX_ROWS"):
        with pytest.raises(SettingsError, match=key):
            Settings.from_env({key: "0"})
    with pytest.raises(SettingsError, match="PUREIRON_HISTORIAN_FLUSH_S"):
        Settings.from_env({"PUREIRON_HISTORIAN_FLUSH_S": "-1"})
    with pytest.raises(SettingsError, match="cannot read secret"):
        read_secret(tmp_path / "missing")


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "Infinity", "1e999"])
def test_settings_reject_non_finite_seconds(raw: str) -> None:
    """R1 L11: float()이 받는 비유한값은 거부한다(무한 주기면 그 일이 멈춘다)."""
    with pytest.raises(SettingsError, match="PUREIRON_HISTORIAN_FLUSH_S must be a positive finite"):
        Settings.from_env({"PUREIRON_HISTORIAN_FLUSH_S": raw})


async def test_bus_pattern_isolation_and_unsubscribe() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def record(event: Event) -> None:
        seen.append(event.topic)

    async def boom(event: Event) -> None:
        raise RuntimeError(event.topic)

    bus.subscribe("tag.changed.*", boom)
    unsubscribe = bus.subscribe("tag.changed.*", record)
    await bus.publish(Event("tag.changed.a"))
    await bus.publish(Event("alarm.raised"))
    unsubscribe()
    await bus.publish(Event("tag.changed.b"))
    assert seen == ["tag.changed.a"]


async def test_bus_publishes_returned_follow_ups_after_current_event() -> None:
    """핸들러가 돌려준 이벤트는 지금 이벤트가 모든 구독자에게 간 뒤 이어서 발행된다(D8)."""
    bus = EventBus()
    seen: list[str] = []

    async def derive(event: Event) -> list[Event]:
        seen.append(f"derive {event.topic}")
        return [Event("alarm.changed.x"), Event("alarm.changed.y")]

    async def record(event: Event) -> None:
        seen.append(f"record {event.topic}")

    bus.subscribe("tag.changed.*", derive)
    bus.subscribe("*", record)
    await bus.publish(Event("tag.changed.a"))
    assert seen == [
        "derive tag.changed.a",
        "record tag.changed.a",
        "record alarm.changed.x",
        "record alarm.changed.y",
    ]


def test_create_app_starts_without_site_db(tmp_path: Path) -> None:
    """site-db가 없어도 core는 기동하고 readyz로 미준비를 알린다(graceful degradation)."""
    app = create_app(SiteRuntime.build(_settings(tmp_path)))
    with TestClient(app) as client:
        assert client.get("/api/v1/healthz").status_code == 200
        ready = client.get("/api/v1/readyz")
        assert ready.status_code == 503
        assert ready.json()["checks"]["db"]["ok"] is False
        # 정의를 아직 못 읽었다 — plan 없음, 집계는 0
        assert ready.json()["acquisition"]["plan_rev"] is None
        assert ready.json()["acquisition"]["good_ratio"] is None
        # 이력은 받은 것이 없다(빈 버퍼 — DB가 없어도 정지 시 최종 flush가 막히지 않음)
        historian = ready.json()["historian"]
        assert (historian["buffer_rows"], historian["written_total"]) == (0, 0)
        assert client.get("/api/v1/project").status_code == 503
        # 인증 경로도 죽지 않는다: 토큰 없음은 DB 없이 401, DB가 필요한 검증·로그인은 503.
        assert client.get("/api/v1/auth/me").status_code == 401
        bearer = {"Authorization": "Bearer some-token"}
        assert client.get("/api/v1/auth/me", headers=bearer).status_code == 503
        login = client.post("/api/v1/auth/login", json={"username": "a", "password": "b"})
        assert login.status_code == 503


def test_create_app_guards_cookie_posts_by_origin(tmp_path: Path) -> None:
    """조립된 앱에 Origin 검사(plan M3c D14)가 걸려 있다: 쿠키 + Origin 없음 → 403."""
    app = create_app(SiteRuntime.build(_settings(tmp_path)))
    client = TestClient(app, cookies={"pureiron_session": "any"})
    rejected = client.post("/api/v1/auth/logout")
    assert (rejected.status_code, rejected.json()) == (403, {"detail": "E_ORIGIN"})
    cross = TestClient(app).post(
        "/api/v1/auth/login",
        json={"username": "a", "password": "b"},
        headers={"Origin": "http://testserver:3000"},
    )
    assert cross.status_code == 403
