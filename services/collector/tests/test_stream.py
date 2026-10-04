from __future__ import annotations

import ast
import asyncio
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from grpc import aio

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector._gen import collector_pb2_grpc as pb_grpc
from pureiron_collector.settings import CollectorSettings, SettingsError
from pureiron_collector.stream import Backoff, Collector, SessionState

COLLECTOR = Path(__file__).resolve().parents[1]
DB_MODULES = {"asyncpg", "psycopg", "psycopg2", "sqlalchemy", "sqlite3"}


class FakeCore(pb_grpc.AcquisitionServicer):
    """plan 하나와 쓰기 요청 하나를 보내고, collector가 보낸 메시지를 기록한다."""

    def __init__(self) -> None:
        self.received: list[pb.CollectorMsg] = []
        self.write_result = asyncio.Event()
        self.batches = asyncio.Event()

    async def Session(
        self,
        request_iterator: AsyncIterator[pb.CollectorMsg],
        context: aio.ServicerContext[pb.CollectorMsg, pb.CoreMsg],
    ) -> AsyncIterator[pb.CoreMsg]:
        async for message in request_iterator:
            self.received.append(message)
            kind = message.WhichOneof("msg")
            if kind == "hello":
                yield pb.CoreMsg(plan=pb.AcquisitionPlan(plan_rev=7, default_poll_ms=1000))
                yield pb.CoreMsg(write=pb.WriteRequest(request_id="w-1", tag_id=1, value=1.0))
            elif kind == "write_result":
                self.write_result.set()
            elif kind == "batch":
                yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))
                if message.batch.seq >= 2:
                    self.batches.set()


async def test_session_hello_plan_write_rejected_and_heartbeat() -> None:
    core = FakeCore()
    server = aio.server()
    pb_grpc.add_AcquisitionServicer_to_server(core, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        settings = CollectorSettings(
            core_target=f"127.0.0.1:{port}",
            heartbeat_s=0.05,
            reconnect_initial_s=0.05,
            reconnect_max_s=0.1,
        )
        collector = Collector(settings)
        stop = asyncio.Event()
        session = asyncio.create_task(collector.run_session(stop))
        await asyncio.wait_for(core.write_result.wait(), 5)
        await asyncio.wait_for(core.batches.wait(), 5)
        stop.set()
        assert await asyncio.wait_for(session, 5) is True
    finally:
        await server.stop(0)

    hello = core.received[0]
    assert hello.WhichOneof("msg") == "hello"
    assert hello.hello.collector_version
    assert hello.hello.last_plan_rev == 0
    assert collector.state.plan_rev == 7

    results = [m.write_result for m in core.received if m.WhichOneof("msg") == "write_result"]
    assert [(r.request_id, r.status) for r in results] == [("w-1", pb.WriteResult.REJECTED)]
    seqs = [m.batch.seq for m in core.received if m.WhichOneof("msg") == "batch"]
    assert seqs[:2] == [1, 2]
    assert all(m.batch.plan_rev == 7 for m in core.received if m.WhichOneof("msg") == "batch")


def test_state_tracks_unacked_and_reports_oldest_on_hello() -> None:
    state = SessionState()
    for _ in range(3):
        state.heartbeat()
    assert state.hello().hello.oldest_unacked_seq == 1
    state.handle(pb.CoreMsg(ack=pb.Ack(seq=2)))
    assert state.unacked == [3]
    assert state.hello().hello.oldest_unacked_seq == 3
    state.handle(pb.CoreMsg(ack=pb.Ack(seq=1)))
    assert state.acked_seq == 2


def test_default_write_result_status_is_rejected() -> None:
    assert pb.WriteResult().status == pb.WriteResult.REJECTED


def test_backoff_doubles_to_cap_and_resets() -> None:
    backoff = Backoff(0.5, 4.0)
    assert [backoff.next_delay() for _ in range(6)] == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]
    backoff.reset()
    assert backoff.next_delay() == 0.5


async def test_run_forever_retries_until_stopped() -> None:
    settings = CollectorSettings("127.0.0.1:1", 1.0, 0.01, 0.02)
    attempts = 0

    class CountingCollector(Collector):
        async def run_session(self, stop: asyncio.Event) -> bool:
            nonlocal attempts
            attempts += 1
            if attempts >= 3:
                stop.set()
            return False

    await asyncio.wait_for(CountingCollector(settings).run_forever(asyncio.Event()), 5)
    assert attempts == 3


def test_settings_validation() -> None:
    assert CollectorSettings.from_env({}).core_target == "127.0.0.1:50051"
    with pytest.raises(SettingsError):
        CollectorSettings.from_env({"PUREIRON_COLLECTOR_HEARTBEAT_S": "0"})


def test_collector_has_no_database_dependency() -> None:
    project = tomllib.loads((COLLECTOR / "pyproject.toml").read_text(encoding="utf-8"))
    deps = " ".join(project["project"]["dependencies"]).lower()
    assert not any(name in deps for name in DB_MODULES)
    for path in (COLLECTOR / "src").rglob("*.py"):
        if "_gen" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not {n.split(".")[0] for n in names} & DB_MODULES, path
