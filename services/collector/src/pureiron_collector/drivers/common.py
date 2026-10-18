"""드라이버 공통: 샘플 생성과 장비 1개의 poll 루프.

드라이버는 `poll()` 한 번에 장비의 모든 태그 샘플을 돌려준다. 장비와 통신이 안 되면 예외를 올리고,
poll 루프가 그 장비의 모든 태그를 BAD로 내보낸 뒤 다음 주기에 다시 연결한다(registry·plugin 없음).

쓰기(M3c-1 D7)는 장비별 큐에 넣고 **poll과 poll 사이에** 같은 poller task가 같은 연결로 하나씩
실행한다 — 한 장비에 동시 트랜잭션이 없다. 재시도는 없다. 결과는 단계로 정한다:

- 보내기 전(장비에 아무것도 가지 않음) → `REJECTED`: 큐 가득(`MAX_QUEUED_WRITES`)·같은 태그 진행 중·
  poller 정지, 요청자가 포기(취소), 기한(`deadline_s`) 경과, 인코딩 불가.
- 확실히 안 써짐 → `FAILED`: 장비 미연결(큐에 넣지 않음), Modbus 예외 응답.
- 보낸 뒤 모름 → `INDETERMINATE`: 응답 시간 초과·연결 끊김·보내는 도중 취소(연결을 닫는다).
- 장비 정상 응답 → `OK`, 이어서 같은 주소 재읽기. 재읽기 실패·도중 취소는 OK 그대로 + readback 비움
  + error(이미 써졌으므로 거부·실패로 바꾸지 않는다).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pureiron_collector._gen import collector_pb2 as pb
from pureiron_collector.health import DeviceHealth
from pureiron_collector.plan import DevicePlan, ModbusPoint

log = logging.getLogger(__name__)

type Value = float | bool
type SampleSink = Callable[[Sequence[pb.Sample]], None]

MAX_QUEUED_WRITES = 8  # 장비별 대기 쓰기 상한(넘치면 REJECTED — 늦은 쓰기가 쌓이지 않게)
POLLER_STOPPED = "poller stopped before write (plan change or shutdown)"


class EncodeError(ValueError):
    """값을 이 태그의 형식으로 장비에 쓸 수 없다(보내기 전 거부)."""


class ReadbackError(Exception):
    """재읽기에 장비가 예외로 답했거나 값을 해석할 수 없다(연결은 정상)."""


class Driver(Protocol):
    async def poll(self) -> list[pb.Sample]:
        """연결이 없으면 연결하고 한 번 읽는다. 통신 실패는 예외, 태그 단위 실패는 BAD 샘플."""
        ...

    async def write(self, point: ModbusPoint, value: float) -> pb.WriteResult:
        """열린 연결로 한 번만 보낸다(새로 연결하지 않음). 결과는 OK·FAILED(미연결·예외 응답).

        인코딩 불가는 보내기 전에 `EncodeError`, 통신 실패(보낸 뒤 모름)는 그 밖의 예외.
        """
        ...

    async def read_back(self, point: ModbusPoint) -> float:
        """같은 주소를 다시 읽는다(bool은 0/1). 장비 예외 응답·해석 불가는 `ReadbackError`."""
        ...

    async def close(self) -> None: ...


type DriverFactory = Callable[[DevicePlan], Driver]


def good(tag_id: int, value: Value, ts_ns: int) -> pb.Sample:
    if isinstance(value, bool):
        return pb.Sample(tag_id=tag_id, ts_unix_ns=ts_ns, flag=value, quality=pb.GOOD)
    return pb.Sample(tag_id=tag_id, ts_unix_ns=ts_ns, num=value, quality=pb.GOOD)


def bad(tag_ids: Iterable[int], ts_ns: int) -> list[pb.Sample]:
    return [pb.Sample(tag_id=tag_id, ts_unix_ns=ts_ns, quality=pb.BAD) for tag_id in tag_ids]


def raise_if_cancelling(exc: BaseException) -> None:
    """pymodbus는 응답 대기 중 받은 취소를 ModbusIOException으로 바꿔 던진다 — 취소로 되돌린다.

    삼키면 task.cancel()이 사라져 stop_pollers·종료가 멈춘다.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError from exc


@dataclass(slots=True)
class _Write:
    point: ModbusPoint
    value: float
    deadline_s: float  # time.monotonic() 기준 — 지나면 보내지 않는다
    result: asyncio.Future[pb.WriteResult]


def write_result(status: pb.WriteResult.Status.ValueType, error: str = "") -> pb.WriteResult:
    return pb.WriteResult(status=status, error=error)


MAX_RETRY_S = 30.0
MAX_RETRY_EXPONENT = 16  # 2**16배면 이미 상한을 넘는다 — 장시간 정지에서 float overflow 방지


def retry_delay(period_s: float, failures: int) -> float:
    """연속 실패 n번째 뒤 다음 시도까지(poll 주기에서 시작해 두 배씩, 상한 30s)."""
    exponent = min(max(0, failures - 1), MAX_RETRY_EXPONENT)
    return min(MAX_RETRY_S, max(period_s, 0.5) * 2**exponent)


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
        self._writes: deque[_Write] = deque()
        self._current: _Write | None = None
        self._stopped = False
        self._wake = asyncio.Event()

    def _pending(self) -> list[_Write]:
        """아직 결과가 정해지지 않은 대기·진행 중 쓰기(요청자가 포기한 것은 빼고)."""
        jobs = [job for job in self._writes if not job.result.done()]
        if self._current is not None:
            jobs.append(self._current)
        return jobs

    async def write(self, point: ModbusPoint, value: float, *, deadline_s: float) -> pb.WriteResult:
        """다음 poll 사이 구간에 실행될 쓰기를 기다린다(`deadline_s` = monotonic 기한).

        취소하면 아직 안 보낸 쓰기는 버려진다.
        """
        if self._stopped:
            return write_result(pb.WriteResult.REJECTED, POLLER_STOPPED)
        if not self.health.connected:
            return write_result(pb.WriteResult.FAILED, "device not connected")
        pending = self._pending()
        if any(job.point.tag_id == point.tag_id for job in pending):
            return write_result(
                pb.WriteResult.REJECTED, f"write in progress for tag {point.tag_id}"
            )
        if len(pending) >= MAX_QUEUED_WRITES:
            return write_result(pb.WriteResult.REJECTED, "write queue full")
        job = _Write(point, value, deadline_s, asyncio.get_running_loop().create_future())
        self._writes.append(job)
        self._wake.set()
        return await job.result

    def _finish(self, job: _Write, result: pb.WriteResult) -> None:
        if not job.result.done():
            job.result.set_result(result)
        log.info(
            "device %s: write tag %d value %r -> %s %s",
            self.plan.device_id,
            job.point.tag_id,
            job.value,
            pb.WriteResult.Status.Name(result.status),
            result.error,
        )

    async def _send(self, job: _Write) -> pb.WriteResult:
        try:
            return await self._driver.write(job.point, job.value)
        except EncodeError as exc:
            return write_result(pb.WriteResult.REJECTED, str(exc))  # 보내지 않았다 — 연결 유지
        except Exception as exc:
            raise_if_cancelling(exc)
            log.warning(
                "device %s: write to tag %d got no confirmed response: %s",
                self.plan.device_id,
                job.point.tag_id,
                exc,
            )
            await self._driver.close()  # 연결 상태를 믿을 수 없다 — 다음 poll이 다시 연결한다.
            return write_result(
                pb.WriteResult.INDETERMINATE,
                f"no confirmed response: {str(exc) or type(exc).__name__}",
            )

    async def _read_back(self, job: _Write, result: pb.WriteResult) -> None:
        """OK 결과에 readback을 싣는다. 실패해도 OK는 그대로(이미 써졌다)."""
        try:
            value = await self._driver.read_back(job.point)
        except ReadbackError as exc:
            result.error = f"readback failed: {exc}"
            return
        except Exception as exc:
            raise_if_cancelling(exc)
            result.error = f"readback failed: {str(exc) or type(exc).__name__}"
            await self._driver.close()
            return
        result.readback = value
        result.readback_ts_unix_ns = time.time_ns()
        result.error = ""

    async def _run_write(self, job: _Write) -> None:
        if job.result.done():
            return  # 요청자가 기다리기를 그만뒀다 — 보내지 않는다.
        if time.monotonic() > job.deadline_s:
            self._finish(job, write_result(pb.WriteResult.REJECTED, "expired before send"))
            return
        self._current = job
        # 아래 await 도중 취소되면 이 값이 결과다: 보내는 중 = 모름, 재읽기 중 = OK(readback 없음).
        result = write_result(
            pb.WriteResult.INDETERMINATE, "write interrupted after send started; outcome unknown"
        )
        try:
            result = await self._send(job)
            if result.status == pb.WriteResult.OK:
                result.error = "readback interrupted"
                await self._read_back(job, result)
        finally:
            self._current = None
            self._finish(job, result)

    async def _idle(self, delay_s: float) -> None:
        """다음 poll까지 기다리며 들어오는 쓰기를 순서대로 실행한다."""
        deadline = time.monotonic() + delay_s
        while True:
            while self._writes:
                await self._run_write(self._writes.popleft())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), remaining)

    def _drop_writes(self) -> None:
        while self._writes:
            job = self._writes.popleft()
            if not job.result.done():
                job.result.set_result(write_result(pb.WriteResult.REJECTED, POLLER_STOPPED))

    async def poll_once(self) -> bool:
        """한 번 읽는다. 성공하면 True."""
        started = time.monotonic()
        try:
            samples = await self._driver.poll()
        except Exception as exc:
            raise_if_cancelling(exc)
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
                await self._idle(max(0.0, delay))
        finally:
            self._stopped = True  # 이후 들어오는 쓰기는 큐에 넣지 않는다
            self._drop_writes()
            await self._driver.close()
