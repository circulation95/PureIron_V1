"""historian 수명주기(flush task)와 상태 집계. 조립은 `runtime.py`가 한다."""

from __future__ import annotations

from pureiron_core.historian.ports import HistorianStatus
from pureiron_core.historian.recorder import Recorder
from pureiron_core.historian.writer import DEFAULT_STOP_TIMEOUT_S, HistoryWriter


class Historian:
    def __init__(
        self,
        recorder: Recorder,
        writer: HistoryWriter,
        *,
        stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    ) -> None:
        self.recorder = recorder
        self.writer = writer
        self._stop_timeout_s = stop_timeout_s

    def start(self) -> None:
        self.writer.start()

    async def stop(self) -> None:
        """수집을 멈춘 뒤 호출한다: flush loop 정지 → 남은 버퍼 마지막 쓰기(제한 시간)."""
        await self.writer.stop(self._stop_timeout_s)

    def status(self) -> HistorianStatus:
        writer = self.writer.status()
        return HistorianStatus(
            buffer_rows=writer.buffer_rows,
            dropped_total=writer.dropped_total,
            written_total=writer.written_total,
            poisoned_total=writer.poisoned_total,
            write_lag_s=writer.write_lag_s,
            db_ok=writer.db_ok,
            rejected_total=self.recorder.stats.rejected,
        )
