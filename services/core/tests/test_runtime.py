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
    assert settings.migrations_dir.name == "migrations"
    assert read_secret(settings.updater_token_file) == "abc"
    assert read_secret(None) is None


def test_settings_errors(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="PUREIRON_GRPC_PORT"):
        Settings.from_env({"PUREIRON_GRPC_PORT": "x"})
    with pytest.raises(SettingsError, match="PUREIRON_PLAN_REFRESH_S"):
        Settings.from_env({"PUREIRON_PLAN_REFRESH_S": "0"})
    with pytest.raises(SettingsError, match="PUREIRON_INGEST_LIVE_MAX_AGE_S"):
        Settings.from_env({"PUREIRON_INGEST_LIVE_MAX_AGE_S": "soon"})
    with pytest.raises(SettingsError, match="cannot read secret"):
        read_secret(tmp_path / "missing")


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
        assert client.get("/api/v1/project").status_code == 503
