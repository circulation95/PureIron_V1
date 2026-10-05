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


class DevicePoller:
    """장비 하나를 `poll_ms`마다 읽어 sink로 보낸다.

    읽기가 주기보다 길면 쉬지 않고 바로 다음을 읽는다.
    """

    def __init__(
        self, plan: DevicePlan, driver: Driver, health: DeviceHealth, sink: SampleSink
    ) -> None:
        self.plan = plan
        self._driver = driver
        self.health = health
        self._sink = sink
        self.health.tag_count = len(plan.points)

    async def poll_once(self) -> None:
        started = time.monotonic()
        try:
            samples = await self._driver.poll()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self.health.consecutive_failures == 0:
                log.warning("device %s: poll failed: %s", self.plan.device_id, exc)
            self.health.record_failure(exc)
            self._sink(bad((point.tag_id for point in self.plan.points), time.time_ns()))
            await self._driver.close()
            return
        latency_ms = (time.monotonic() - started) * 1000
        bad_count = sum(1 for sample in samples if sample.quality != pb.GOOD)
        if self.health.consecutive_failures:
            log.info("device %s: recovered", self.plan.device_id)
        self.health.record_success(latency_ms, bad_count)
        self._sink(samples)

    async def run(self) -> None:
        period = self.plan.poll_ms / 1000
        try:
            while True:
                started = time.monotonic()
                await self.poll_once()
                await asyncio.sleep(max(0.0, period - (time.monotonic() - started)))
        finally:
            await self._driver.close()
