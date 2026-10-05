"""드라이버 공통: 샘플 생성과 장비 1개의 poll 루프.

드라이버는 `poll()` 한 번에 장비의 모든 태그 샘플을 돌려준다. 장비와 통신이 안 되면 예외를 올리고,
poll 루프가 그 장비의 모든 태그를 BAD로 내보낸 뒤 다음 주기에 다시 연결한다(registry·plugin 없음).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector.health import DeviceHealth
from pureiron_collector.plan import DevicePlan

log = logging.getLogger(__name__)

type Value = float | bool
type SampleSink = Callable[[Sequence[pb.Sample]], None]


class Driver(Protocol):
    async def poll(self) -> list[pb.Sample]:
        """연결이 없으면 연결하고 한 번 읽는다. 통신 실패는 예외, 태그 단위 실패는 BAD 샘플."""
        ...

    async def close(self) -> None: ...


type DriverFactory = Callable[[DevicePlan], Driver]


def good(tag_id: int, value: Value, ts_ns: int) -> pb.Sample:
    if isinstance(value, bool):
        return pb.Sample(tag_id=tag_id, ts_unix_ns=ts_ns, flag=value, quality=pb.GOOD)
    return pb.Sample(tag_id=tag_id, ts_unix_ns=ts_ns, num=value, quality=pb.GOOD)


def bad(tag_ids: Iterable[int], ts_ns: int) -> list[pb.Sample]:
    return [pb.Sample(tag_id=tag_id, ts_unix_ns=ts_ns, quality=pb.BAD) for tag_id in tag_ids]


MAX_RETRY_S = 30.0


def retry_delay(period_s: float, failures: int) -> float:
    """연속 실패 n번째 뒤 다음 시도까지(poll 주기에서 시작해 두 배씩, 상한 30s)."""
    return min(MAX_RETRY_S, max(period_s, 0.5) * 2 ** max(0, failures - 1))


class DevicePoller:
    """장비 하나를 `poll_ms`마다 읽어 sink로 보낸다.

    읽기가 주기보다 길면 쉬지 않고 바로 다음을 읽는다. 통신이 끊기면 그 장비의 태그를 **끊긴 순간
    한 번** BAD로 내보내고(매 주기 반복하지 않음 — core는 무신호·DeviceHealth로 판정), 재연결은
    지수 backoff로 시도한다.
    """

    def __init__(
        self, plan: DevicePlan, driver: Driver, health: DeviceHealth, sink: SampleSink
    ) -> None:
        self.plan = plan
        self._driver = driver
        self.health = health
        self._sink = sink
        self.health.tag_count = len(plan.points)

    async def poll_once(self) -> bool:
        """한 번 읽는다. 성공하면 True."""
        started = time.monotonic()
        try:
            samples = await self._driver.poll()
        except Exception as exc:
            # pymodbus는 응답 대기 중 받은 취소를 ModbusIOException으로 바꿔 던진다.
            # 취소로 되돌린다 — 삼키면 task.cancel()이 사라져 stop_pollers·종료가 멈춘다.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError from exc
            first = self.health.consecutive_failures == 0
            self.health.record_failure(exc)
            if first:
                log.warning("device %s: poll failed: %s", self.plan.device_id, exc)
                self._sink(bad((point.tag_id for point in self.plan.points), time.time_ns()))
            await self._driver.close()
            return False
        latency_ms = (time.monotonic() - started) * 1000
        bad_count = sum(1 for sample in samples if sample.quality != pb.GOOD)
        if self.health.consecutive_failures:
            log.info("device %s: recovered", self.plan.device_id)
        self.health.record_success(latency_ms, bad_count)
        self._sink(samples)
        return True

    async def run(self) -> None:
        period = self.plan.poll_ms / 1000
        try:
            while True:
                started = time.monotonic()
                if await self.poll_once():
                    delay = period - (time.monotonic() - started)
                else:
                    delay = retry_delay(period, self.health.consecutive_failures)
                await asyncio.sleep(max(0.0, delay))
        finally:
            await self._driver.close()
