"""core와의 `Acquisition.Session` 스트림(ADR-020). M1 골격: Hello → plan 수신 → 빈 batch heartbeat.

장비 polling·링버퍼(60s, Ack 전 미해제)는 M3a에서 이 세션 위에 붙는다. PLC 쓰기 경로는
ADR-013(개정) 가드레일과 함께 별도 마일스톤에서 구현한다 — 그 전까지 `WriteRequest`는 `REJECTED`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from grpc import aio

from pureiron_collector import __version__
from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector._gen import collector_pb2_grpc as pb_grpc
from pureiron_collector.settings import CollectorSettings

log = logging.getLogger(__name__)

WRITE_DISABLED = "write path not implemented yet (ADR-013 guardrails pending)"


@dataclass(slots=True)
class SessionState:
    plan_rev: int = 0
    next_seq: int = 1
    acked_seq: int = 0
    unacked: list[int] = field(default_factory=list[int])

    def hello(self) -> pb.CollectorMsg:
        return pb.CollectorMsg(
            hello=pb.Hello(
                collector_version=__version__,
                last_plan_rev=self.plan_rev,
                oldest_unacked_seq=self.unacked[0] if self.unacked else 0,
            )
        )

    def heartbeat(self) -> pb.CollectorMsg:
        seq = self.next_seq
        self.next_seq += 1
        self.unacked.append(seq)
        return pb.CollectorMsg(batch=pb.ValueBatch(plan_rev=self.plan_rev, seq=seq))

    def handle(self, message: pb.CoreMsg) -> pb.CollectorMsg | None:
        kind = message.WhichOneof("msg")
        if kind == "plan":
            self.plan_rev = message.plan.plan_rev
            log.info("plan rev %d received (%d tags)", self.plan_rev, len(message.plan.tags))
            return None
        if kind == "write":
            log.warning("rejecting write request %s: %s", message.write.request_id, WRITE_DISABLED)
            return pb.CollectorMsg(
                write_result=pb.WriteResult(
                    request_id=message.write.request_id,
                    status=pb.WriteResult.REJECTED,
                    error=WRITE_DISABLED,
                )
            )
        if kind == "ack":
            self.acked_seq = max(self.acked_seq, message.ack.seq)
            self.unacked = [seq for seq in self.unacked if seq > self.acked_seq]
        return None


class Backoff:
    """지수 backoff(상한 있음). 세션이 plan을 받으면 reset 한다."""

    def __init__(self, initial_s: float, max_s: float, factor: float = 2.0) -> None:
        self._initial = initial_s
        self._max = max_s
        self._factor = factor
        self._next = initial_s

    def next_delay(self) -> float:
        delay = self._next
        self._next = min(self._max, self._next * self._factor)
        return delay

    def reset(self) -> None:
        self._next = self._initial


type ChannelFactory = Callable[[str], aio.Channel]


class Collector:
    def __init__(
        self,
        settings: CollectorSettings,
        channel_factory: ChannelFactory = aio.insecure_channel,
    ) -> None:
        self._settings = settings
        self._channel_factory = channel_factory
        self.state = SessionState()

    async def run_session(self, stop: asyncio.Event) -> bool:
        """세션 하나를 끝날 때까지 돈다. plan을 한 번이라도 받았으면 True."""
        outgoing: asyncio.Queue[pb.CollectorMsg] = asyncio.Queue()
        got_plan = False

        async def requests() -> AsyncIterator[pb.CollectorMsg]:
            yield self.state.hello()
            while True:
                yield await outgoing.get()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self._settings.heartbeat_s)
                await outgoing.put(self.state.heartbeat())

        async with self._channel_factory(self._settings.core_target) as channel:
            call = pb_grpc.AcquisitionStub(channel).Session(requests())
            beat = asyncio.create_task(heartbeat())
            stopper = asyncio.create_task(stop.wait())
            try:
                while not stop.is_set():
                    reader = asyncio.ensure_future(call.read())
                    done, _ = await asyncio.wait(
                        {reader, stopper}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if reader not in done:
                        reader.cancel()
                        break
                    message = reader.result()
                    if not isinstance(message, pb.CoreMsg):
                        break
                    got_plan = got_plan or message.WhichOneof("msg") == "plan"
                    reply = self.state.handle(message)
                    if reply is not None:
                        await outgoing.put(reply)
            finally:
                beat.cancel()
                stopper.cancel()
                call.cancel()
        return got_plan

    async def run_forever(self, stop: asyncio.Event, backoff: Backoff | None = None) -> None:
        backoff = backoff or Backoff(
            self._settings.reconnect_initial_s, self._settings.reconnect_max_s
        )
        while not stop.is_set():
            try:
                if await self.run_session(stop):
                    backoff.reset()
            except aio.AioRpcError as exc:
                log.warning("core session failed: %s %s", exc.code().name, exc.details())
            if stop.is_set():
                break
            delay = backoff.next_delay()
            log.info("reconnecting to core in %.1fs", delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
