"""`Acquisition.Session` gRPC 서버(core = 서버)와 plan 배포.

세션 계약(M3a-1 PR #14 리뷰에서 확정, `services/collector/.../stream.py` docstring과 짝):
- Hello를 받으면 현재 plan을 보낸다 — 같은 rev여도 세션마다. collector는 첫 plan에서 live
  전송을 켠다. DB가 아직 준비되지 않아 plan이 없으면 보류했다가 처음 읽히는 순간 보낸다.
- Hello 직후부터(plan 전·DB 준비 전 포함) heartbeat(seq 0)를 포함한 **모든 batch에 Ack** 한다
  — collector는 core가 `core_silence_s` 동안 조용하면 세션을 끊는다. Ack는 메모리 반영 직후,
  이벤트 발행 전에 보낸다.
- plan이 바뀌면(`project_state.definitions_rev`) 열린 세션 전부에 새 plan을 보낸다.
- 세션이 하나도 남지 않으면 전 장비 DISCONNECTED·전 태그 BAD(스트림 종료 = 신호 없음).

쓰기(`WriteRequest`)는 ADR-013 가드레일 구현 전까지 보내지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

from grpc import aio

from pureiron_core._gen import collector_pb2 as pb
from pureiron_core._gen import collector_pb2_grpc as pb_grpc
from pureiron_core.acquisition.devices import DeviceMonitor
from pureiron_core.acquisition.ingest import Ingestor
from pureiron_core.acquisition.plan import build_plan
from pureiron_core.acquisition.ports import AcquisitionSummary, IngestCounts
from pureiron_core.platform.bus import Event, EventBus
from pureiron_core.project.ports import (
    Definitions,
    DefinitionsSource,
    DefinitionsUnavailableError,
)
from pureiron_core.tags.ports import TagKey, TagStorePort

log = logging.getLogger(__name__)

SESSION_TOPIC = "collector.session.changed"
WATCHDOG_INTERVAL_S = 1.0
# collector는 1s마다 heartbeat를 보낸다. 이 시간 동안 아무것도 오지 않으면 RST 없이 끊긴(half-open)
# 세션으로 보고 닫는다 — 그렇지 않으면 TCP keepalive(기본 2시간)까지 collector_connected가 거짓 참.
SESSION_SILENCE_S = 15.0

type Outgoing = asyncio.Queue[pb.CoreMsg | None]


@dataclass(eq=False, slots=True)
class _Session:
    outgoing: Outgoing = field(default_factory=asyncio.Queue[pb.CoreMsg | None])
    hello: bool = False


class AcquisitionService(pb_grpc.AcquisitionServicer):
    def __init__(
        self,
        bus: EventBus,
        tags: TagStorePort,
        devices: DeviceMonitor,
        ingestor: Ingestor,
        *,
        session_silence_s: float = SESSION_SILENCE_S,
    ) -> None:
        self._bus = bus
        self._tags = tags
        self._devices = devices
        self._ingestor = ingestor
        self._session_silence_s = session_silence_s
        self._sessions: set[_Session] = set()
        self._plan: pb.AcquisitionPlan | None = None

    @property
    def collector_connected(self) -> bool:
        return bool(self._sessions)

    @property
    def plan_rev(self) -> int | None:
        return self._plan.plan_rev if self._plan is not None else None

    def summary(self) -> AcquisitionSummary:
        tags = self._tags.summary()
        devices = self._devices.summary()
        stats = self._ingestor.stats
        return AcquisitionSummary(
            plan_rev=self.plan_rev,
            devices_total=devices.total,
            devices_connected=devices.connected,
            tags_total=tags.total,
            tags_good=tags.good,
            ingest=IngestCounts(
                live=stats.live, stale=stats.stale, future=stats.future, replay=stats.replay
            ),
        )

    async def _publish(self, events: Iterable[Event]) -> None:
        for event in events:
            await self._bus.publish(event)

    # --- plan ----------------------------------------------------------------------------
    async def set_plan(self, plan: pb.AcquisitionPlan, keys: Sequence[TagKey]) -> None:
        """새 plan을 적용한다: 색인을 먼저 바꾸고(새 tag_id 샘플이 바로 반영되게) 세션에 보낸다."""
        self._tags.reindex(keys)
        self._devices.reindex(plan)
        self._plan = plan
        for session in self._sessions:
            if session.hello:
                session.outgoing.put_nowait(pb.CoreMsg(plan=plan))

    # --- 세션 ----------------------------------------------------------------------------
    async def _set_sessions(self) -> None:
        await self._bus.publish(Event(SESSION_TOPIC, {"sessions": len(self._sessions)}))

    async def _handle(self, message: pb.CollectorMsg, session: _Session) -> None:
        kind = message.WhichOneof("msg")
        if kind == "batch":
            events = self._ingestor.ingest(message.batch)
            session.outgoing.put_nowait(pb.CoreMsg(ack=pb.Ack(seq=message.batch.seq)))
            await self._publish(events)
        elif kind == "hello":
            log.info(
                "collector connected: version=%s last_plan_rev=%d",
                message.hello.collector_version,
                message.hello.last_plan_rev,
            )
            session.hello = True
            if self._plan is not None:
                session.outgoing.put_nowait(pb.CoreMsg(plan=self._plan))
        elif kind == "health":
            await self._publish(self._devices.on_health(message.health))
        elif kind == "write_result":
            log.warning(
                "unexpected write result %s: %s",
                message.write_result.request_id,
                message.write_result.error,
            )

    async def _read(self, requests: AsyncIterator[pb.CollectorMsg], session: _Session) -> None:
        iterator = aiter(requests)
        try:
            while True:
                try:
                    message = await asyncio.wait_for(anext(iterator), self._session_silence_s)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    log.warning(
                        "collector silent for %.0fs; closing session", self._session_silence_s
                    )
                    break
                await self._handle(message, session)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("collector session read failed")
        finally:
            session.outgoing.put_nowait(None)

    async def Session(
        self,
        request_iterator: AsyncIterator[pb.CollectorMsg],
        context: aio.ServicerContext[pb.CollectorMsg, pb.CoreMsg],
    ) -> AsyncIterator[pb.CoreMsg]:
        session = _Session()
        self._sessions.add(session)
        await self._set_sessions()
        reader = asyncio.create_task(self._read(request_iterator, session))
        try:
            while (message := await session.outgoing.get()) is not None:
                yield message
        finally:
            # 동기 구간에서 먼저 뺀다 — 아래 await 중에 다시 취소돼도 죽은 세션이 남지 않게.
            self._sessions.discard(session)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            await self._set_sessions()
            if not self._sessions:
                log.info("no collector session — devices disconnected, tags BAD")
                await self._publish(self._devices.disconnect_all())

    async def run_watchdog(self, interval_s: float = WATCHDOG_INTERVAL_S) -> None:
        """장비 무신호 판정을 주기적으로 돌린다."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self._publish(self._devices.check())
            except Exception:
                log.exception("device silence check failed")


type PlanApplier = Callable[[pb.AcquisitionPlan, Sequence[TagKey]], Awaitable[None]]
type DefinitionsListener = Callable[[Definitions], None]


class PlanKeeper:
    """정의 rev를 주기적으로 보고 바뀌면 plan을 다시 만들어 적용한다.

    DB·스키마 문제는 경고 후 다음 주기에 다시 시도한다(graceful degradation). 마지막 plan은
    유지된다. `on_definitions`(historian recorder)는 plan을 적용하기 전에 같은 정의를 받는다.
    """

    def __init__(
        self,
        source: DefinitionsSource,
        apply: PlanApplier,
        *,
        refresh_s: float,
        on_definitions: DefinitionsListener | None = None,
    ) -> None:
        self._source = source
        self._apply = apply
        self._on_definitions = on_definitions
        self._refresh_s = refresh_s
        self._rev: int | None = None
        self._failing = False
        self._last_error: str | None = None

    async def refresh(self) -> bool:
        """한 번 확인한다. plan을 새로 적용했으면 True."""
        try:
            rev = await self._source.rev()
            if rev == self._rev:
                return False
            definitions = await self._source.load()
        except DefinitionsUnavailableError as exc:
            if not self._failing:
                log.warning("cannot load definitions (will retry): %s", exc)
            self._failing = True
            return False
        if self._failing:
            log.info("definitions available again")
        self._failing = False
        built = build_plan(definitions)
        for problem in built.problems:
            log.warning("plan rev %d: %s", definitions.rev, problem)
        # collector에 plan을 보내기 전에 알린다 — historian이 새 rev·태그를 먼저 알아야 그 rev의
        # 샘플을 "앞선 rev"(D6)나 "plan에 없는 tag_id"(D7)로 버리지 않는다.
        if self._on_definitions is not None:
            self._on_definitions(definitions)
        await self._apply(built.plan, built.keys)
        self._rev = definitions.rev
        log.info(
            "plan rev %d: %d device(s), %d tag(s)",
            definitions.rev,
            len(built.plan.devices),
            len(built.plan.tags),
        )
        return True

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
                self._last_error = None
            except Exception as exc:
                # 같은 정의로 같은 오류가 주기마다 반복되므로 바뀔 때만 traceback을 남긴다.
                if repr(exc) != self._last_error:
                    log.exception("plan refresh failed (will retry)")
                self._last_error = repr(exc)
            await asyncio.sleep(self._refresh_s)


class AcquisitionServer:
    """gRPC 서버와 백그라운드 작업(plan 갱신·무신호 판정) 수명주기.

    `port=0`이면 임의 포트를 받아 `bound_port`로 알려준다.
    """

    def __init__(
        self,
        service: AcquisitionService,
        keeper: PlanKeeper | None,
        *,
        host: str,
        port: int,
        watchdog_interval_s: float = WATCHDOG_INTERVAL_S,
    ) -> None:
        self._watchdog_interval_s = watchdog_interval_s
        self._service = service
        self._keeper = keeper
        self._address = f"{host}:{port}"
        self._server: aio.Server | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self.bound_port: int | None = None

    @property
    def collector_connected(self) -> bool:
        return self._service.collector_connected

    def summary(self) -> AcquisitionSummary:
        return self._service.summary()

    async def start(self) -> None:
        server = aio.server()
        pb_grpc.add_AcquisitionServicer_to_server(self._service, server)
        self.bound_port = server.add_insecure_port(self._address)
        await server.start()
        self._server = server
        self._tasks.append(
            asyncio.create_task(
                self._service.run_watchdog(self._watchdog_interval_s), name="acq-watchdog"
            )
        )
        if self._keeper is not None:
            self._tasks.append(asyncio.create_task(self._keeper.run(), name="acq-plan"))

    async def stop(self, grace_s: float = 2.0) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._server is not None:
            await self._server.stop(grace_s)
            self._server = None
