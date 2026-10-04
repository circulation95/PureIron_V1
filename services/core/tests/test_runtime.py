from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from grpc import aio

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core._gen import collector_pb2_grpc as pb_grpc
from pureiron_core.acquisition.server import (
    SESSION_TOPIC,
    AcquisitionServer,
    AcquisitionService,
)
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
    assert settings.migrations_dir.name == "migrations"
    assert read_secret(settings.updater_token_file) == "abc"
    assert read_secret(None) is None


def test_settings_errors(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="PUREIRON_GRPC_PORT"):
        Settings.from_env({"PUREIRON_GRPC_PORT": "x"})
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


async def test_acquisition_session_plan_ack_and_status() -> None:
    bus = EventBus()
    sessions: list[object] = []

    async def on_session(event: Event) -> None:
        sessions.append(event.payload["sessions"])

    bus.subscribe(SESSION_TOPIC, on_session)
    service = AcquisitionService(bus)
    server = AcquisitionServer(service, host="127.0.0.1", port=0)
    await server.start()
    try:
        outgoing: asyncio.Queue[pb.CollectorMsg | None] = asyncio.Queue()

        async def requests() -> AsyncIterator[pb.CollectorMsg]:
            while (message := await outgoing.get()) is not None:
                yield message

        async with aio.insecure_channel(f"127.0.0.1:{server.bound_port}") as channel:
            stub = pb_grpc.AcquisitionStub(channel)
            call = stub.Session(requests())
            await outgoing.put(pb.CollectorMsg(hello=pb.Hello(collector_version="0.1.0")))
            first = await call.read()
            assert isinstance(first, pb.CoreMsg)
            assert first.WhichOneof("msg") == "plan"
            assert first.plan.plan_rev == 0
            assert list(first.plan.tags) == []
            assert server.collector_connected

            await outgoing.put(pb.CollectorMsg(batch=pb.ValueBatch(seq=42)))
            second = await call.read()
            assert isinstance(second, pb.CoreMsg)
            assert second.ack.seq == 42

            await outgoing.put(None)
            assert await call.read() is aio.EOF
        for _ in range(50):
            if not server.collector_connected:
                break
            await asyncio.sleep(0.02)
        assert not server.collector_connected
        assert sessions == [1, 0]
    finally:
        await server.stop()


def test_create_app_starts_without_site_db(tmp_path: Path) -> None:
    """site-db가 없어도 core는 기동하고 readyz로 미준비를 알린다(graceful degradation)."""
    app = create_app(SiteRuntime.build(_settings(tmp_path)))
    with TestClient(app) as client:
        assert client.get("/api/v1/healthz").status_code == 200
        ready = client.get("/api/v1/readyz")
        assert ready.status_code == 503
        assert ready.json()["checks"]["db"]["ok"] is False
        assert client.get("/api/v1/project").status_code == 503
