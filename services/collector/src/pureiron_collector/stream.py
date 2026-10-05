"""core와의 `Acquisition.Session` 스트림(ADR-020)과 수집 파이프라인 조립.

장비 poller → 샘플 버퍼 → (100ms 또는 500값) `ValueBatch` → 링버퍼(Ack 전 보관) → 세션.
poll은 세션과 무관하게 계속 돈다 — core가 없는 동안의 batch는 링버퍼에만 쌓였다가 재연결 직후
`replay=true`로 먼저 나간다.

세션 계약(core M3a-2가 지켜야 함):
- core는 세션마다 Hello 뒤에 plan을 보낸다(같은 rev여도) — collector는 첫 plan에서 live를 켠다.
- core는 heartbeat(seq 0)를 포함한 모든 batch에 Ack 한다 — `core_silence_s` 동안 아무 메시지도
  없으면 collector는 세션을 끊고 재연결한다(RST 없는 단절에서 지연 데이터가 live로 가지 않게).
- seq는 collector 프로세스마다 1부터 다시 시작하고, 링버퍼 drop으로 비는 구간이 있다. replay batch는
  원래 plan_rev를 유지한다.

PLC 쓰기 경로는 ADR-013(개정) 가드레일과 함께 별도 마일스톤에서
구현한다 — 그 전까지 `WriteRequest`는 `REJECTED`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence

from grpc import aio

from pureiron_collector import __version__
from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector._gen import collector_pb2_grpc as pb_grpc
from pureiron_collector.drivers.common import DevicePoller, DriverFactory
from pureiron_collector.drivers.modbus import ModbusDevice
from pureiron_collector.health import DeviceHealth
from pureiron_collector.plan import parse_plan
from pureiron_collector.ring import RingBuffer
from pureiron_collector.settings import CollectorSettings

log = logging.getLogger(__name__)

WRITE_DISABLED = "write path not implemented yet (ADR-013 guardrails pending)"
HEARTBEAT_SEQ = 0  # 빈 heartbeat batch는 링버퍼에 넣지 않는다(core는 Ack(0)으로 답한다).
STOP_TIMEOUT_S = 5.0

type Outgoing = asyncio.Queue[pb.CollectorMsg]


class Acquisition:
    """plan을 poller로 바꾸고, 샘플을 batch로 묶어 링버퍼와 현재 세션에 넘긴다."""

    def __init__(self, settings: CollectorSettings, driver_factory: DriverFactory) -> None:
        self._settings = settings
        self._driver_factory = driver_factory
        self.ring = RingBuffer(settings.ring_window_s)
        self.plan_rev = 0
        self._applied = False
        self._buffer: list[pb.Sample] = []
        self._full = asyncio.Event()
        self._pollers: dict[str, tuple[DevicePoller, asyncio.Task[None]]] = {}
        self._live: Outgoing | None = None

    # --- 샘플 → batch -------------------------------------------------------------------
    def accept(self, samples: Sequence[pb.Sample]) -> None:
        self._buffer.extend(samples)
        if len(self._buffer) >= self._settings.batch_max_samples:
            self._full.set()

    def flush(self) -> None:
        while self._buffer:
            size = self._settings.batch_max_samples
            chunk, self._buffer = self._buffer[:size], self._buffer[size:]
            batch = self.ring.append(self.plan_rev, chunk)
            if self._live is not None:
                self._live.put_nowait(pb.CollectorMsg(batch=batch))

    async def run_batcher(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._full.wait(), self._settings.batch_interval_s)
            self._full.clear()
            self.flush()

    # --- plan → poller -----------------------------------------------------------------
    async def apply_plan(self, plan: pb.AcquisitionPlan) -> None:
        if self._applied and plan.plan_rev == self.plan_rev:
            return  # 같은 plan으로 재연결 — poll을 끊지 않는다.
        parsed = parse_plan(plan)
        for problem in parsed.problems:
            log.warning("plan rev %d: %s", parsed.plan_rev, problem)
        self.flush()  # 이전 plan으로 읽은 샘플은 이전 plan_rev로 내보낸다.
        await self.stop_pollers()
        self.plan_rev = parsed.plan_rev
        self._applied = True
        for device in parsed.devices:
            poller = DevicePoller(
                device,
                self._driver_factory(device),
                DeviceHealth(device.device_id, device.protocol),
                self.accept,
            )
            task = asyncio.create_task(poller.run(), name=f"poll:{device.device_id}")
            task.add_done_callback(_log_crash)
            self._pollers[device.device_id] = (poller, task)
        log.info(
            "plan rev %d applied: %d device(s), %d tag(s)",
            parsed.plan_rev,
            len(parsed.devices),
            sum(len(device.points) for device in parsed.devices),
        )

    async def stop_pollers(self) -> None:
        tasks = [task for _, task in self._pollers.values()]
        self._pollers.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            _, stuck = await asyncio.wait(tasks, timeout=STOP_TIMEOUT_S)
            for task in stuck:
                log.error("%s did not stop within %.0fs", task.get_name(), STOP_TIMEOUT_S)

    @property
    def pollers(self) -> dict[str, asyncio.Task[None]]:
        """장비별 poll task 사본(관찰용)."""
        return {device_id: task for device_id, (_, task) in self._pollers.items()}

    def health(self) -> list[pb.DeviceHealth]:
        return [poller.health.to_proto() for poller, _ in self._pollers.values()]

    # --- 세션 연결 ---------------------------------------------------------------------
    def attach(self, outgoing: Outgoing) -> None:
        """남은 batch를 replay로 먼저 넣고 live 전송을 켠다(사이에 await 없음 → 순서 보장).

        세션당 한 번만 — 세션 도중 새 plan이 와도 이미 live로 보낸 batch를 다시 replay 하지 않는다.
        """
        if self._live is outgoing:
            return
        replays = self.ring.pending()
        for batch in replays:
            outgoing.put_nowait(pb.CollectorMsg(batch=batch))
        if replays:
            log.info("replaying %d unacked batch(es) from seq %d", len(replays), replays[0].seq)
        self._live = outgoing

    def detach(self) -> None:
        self._live = None


def _log_crash(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("%s crashed", task.get_name(), exc_info=exc)


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


def _reject(write: pb.WriteRequest) -> pb.CollectorMsg:
    log.warning("rejecting write request %s: %s", write.request_id, WRITE_DISABLED)
    return pb.CollectorMsg(
        write_result=pb.WriteResult(
            request_id=write.request_id, status=pb.WriteResult.REJECTED, error=WRITE_DISABLED
        )
    )


class Collector:
    def __init__(
        self,
        settings: CollectorSettings,
        channel_factory: ChannelFactory = aio.insecure_channel,
        driver_factory: DriverFactory = ModbusDevice,
    ) -> None:
        self._settings = settings
        self._channel_factory = channel_factory
        self.acquisition = Acquisition(settings, driver_factory)

    def hello(self) -> pb.CollectorMsg:
        return pb.CollectorMsg(
            hello=pb.Hello(
                collector_version=__version__,
                last_plan_rev=self.acquisition.plan_rev,
                oldest_unacked_seq=self.acquisition.ring.oldest_unacked_seq,
            )
        )

    async def _handle(self, message: pb.CoreMsg, outgoing: Outgoing) -> bool:
        """메시지 하나를 처리한다. plan이었으면 True."""
        kind = message.WhichOneof("msg")
        if kind == "plan":
            await self.acquisition.apply_plan(message.plan)
            self.acquisition.attach(outgoing)
            return True
        if kind == "ack":
            self.acquisition.ring.ack(message.ack.seq)
        elif kind == "write":
            outgoing.put_nowait(_reject(message.write))
        return False

    async def run_session(self, stop: asyncio.Event) -> bool:
        """세션 하나를 끝날 때까지 돈다. plan을 한 번이라도 받았으면 True."""
        outgoing: Outgoing = asyncio.Queue()
        got_plan = False

        async def requests() -> AsyncIterator[pb.CollectorMsg]:
            yield self.hello()
            while True:
                yield await outgoing.get()

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self._settings.heartbeat_s)
                empty = pb.ValueBatch(plan_rev=self.acquisition.plan_rev, seq=HEARTBEAT_SEQ)
                outgoing.put_nowait(pb.CollectorMsg(batch=empty))

        last_rx = time.monotonic()
        silent = asyncio.Event()

        async def watchdog() -> None:
            while True:
                await asyncio.sleep(min(1.0, self._settings.core_silence_s / 4))
                if time.monotonic() - last_rx > self._settings.core_silence_s:
                    log.warning(
                        "core silent for %.1fs; reconnecting", self._settings.core_silence_s
                    )
                    silent.set()
                    return

        async def health() -> None:
            while True:
                await asyncio.sleep(self._settings.health_interval_s)
                for item in self.acquisition.health():
                    outgoing.put_nowait(pb.CollectorMsg(health=item))

        async with self._channel_factory(self._settings.core_target) as channel:
            call = pb_grpc.AcquisitionStub(channel).Session(requests())
            side_tasks = [
                asyncio.create_task(heartbeat()),
                asyncio.create_task(health()),
                asyncio.create_task(watchdog()),
            ]
            stopper = asyncio.create_task(stop.wait())
            silenced = asyncio.create_task(silent.wait())
            try:
                while not stop.is_set():
                    reader = asyncio.ensure_future(call.read())
                    done, _ = await asyncio.wait(
                        {reader, stopper, silenced}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if reader not in done:
                        reader.cancel()
                        break
                    message = reader.result()
                    if not isinstance(message, pb.CoreMsg):
                        break
                    last_rx = time.monotonic()
                    got_plan = await self._handle(message, outgoing) or got_plan
            finally:
                self.acquisition.detach()
                for task in side_tasks:
                    task.cancel()
                stopper.cancel()
                silenced.cancel()
                call.cancel()
                await asyncio.gather(*side_tasks, return_exceptions=True)
                await asyncio.gather(stopper, silenced, return_exceptions=True)
        return got_plan

    async def run_forever(self, stop: asyncio.Event, backoff: Backoff | None = None) -> None:
        backoff = backoff or Backoff(
            self._settings.reconnect_initial_s, self._settings.reconnect_max_s
        )
        batcher = asyncio.create_task(self.acquisition.run_batcher(), name="batcher")
        batcher.add_done_callback(_log_crash)
        try:
            while not stop.is_set():
                try:
                    if await self.run_session(stop):
                        backoff.reset()
                except aio.AioRpcError as exc:
                    log.warning("core session failed: %s %s", exc.code().name, exc.details())
                except Exception:
                    # 링버퍼는 메모리에만 있으므로 프로세스를 죽이지 않고 재연결한다.
                    log.exception("core session crashed")
                if stop.is_set():
                    break
                delay = backoff.next_delay()
                log.info("reconnecting to core in %.1fs", delay)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    continue
        finally:
            batcher.cancel()
            await self.acquisition.stop_pollers()
