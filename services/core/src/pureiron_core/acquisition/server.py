"""`Acquisition.Session` gRPC 서버(core = 서버). M1 골격: Hello에 빈 plan, batch마다 Ack.

ingest(현재값·historian 버퍼)는 M3a에서 이 서비스 뒤에 붙는다. 쓰기(`WriteRequest`)는 보내지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from grpc import aio

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core._gen import collector_pb2_grpc as pb_grpc
from pureiron_core.platform.bus import Event, EventBus

log = logging.getLogger(__name__)

SESSION_TOPIC = "collector.session.changed"
EMPTY_PLAN_REV = 0
DEFAULT_POLL_MS = 1000


class AcquisitionService(pb_grpc.AcquisitionServicer):
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._sessions = 0

    @property
    def collector_connected(self) -> bool:
        return self._sessions > 0

    async def _set_sessions(self, delta: int) -> None:
        self._sessions += delta
        await self._bus.publish(Event(SESSION_TOPIC, {"sessions": self._sessions}))

    async def Session(
        self,
        request_iterator: AsyncIterator[pb.CollectorMsg],
        context: aio.ServicerContext[pb.CollectorMsg, pb.CoreMsg],
    ) -> AsyncIterator[pb.CoreMsg]:
        await self._set_sessions(+1)
        try:
            async for message in request_iterator:
                kind = message.WhichOneof("msg")
                if kind == "hello":
                    log.info("collector connected: version=%s", message.hello.collector_version)
                    yield pb.CoreMsg(
                        plan=pb.AcquisitionPlan(
                            plan_rev=EMPTY_PLAN_REV, default_poll_ms=DEFAULT_POLL_MS
                        )
                    )
                elif kind == "batch":
                    yield pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq))
        finally:
            await self._set_sessions(-1)


class AcquisitionServer:
    """gRPC 서버 수명주기. `port=0`이면 임의 포트를 받아 `bound_port`로 알려준다."""

    def __init__(self, service: AcquisitionService, *, host: str, port: int) -> None:
        self._service = service
        self._address = f"{host}:{port}"
        self._server: aio.Server | None = None
        self.bound_port: int | None = None

    @property
    def collector_connected(self) -> bool:
        return self._service.collector_connected

    async def start(self) -> None:
        server = aio.server()
        pb_grpc.add_AcquisitionServicer_to_server(self._service, server)
        self.bound_port = server.add_insecure_port(self._address)
        await server.start()
        self._server = server

    async def stop(self, grace_s: float = 2.0) -> None:
        if self._server is not None:
            await self._server.stop(grace_s)
            self._server = None
