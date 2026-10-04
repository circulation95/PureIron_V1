"""`pureiron-collector` 진입점. SIGTERM/SIGINT에 세션을 닫고 종료한다."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from pureiron_collector.settings import CollectorSettings
from pureiron_collector.stream import Collector


async def _run(settings: CollectorSettings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    await Collector(settings).run_forever(stop)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(_run(CollectorSettings.from_env(os.environ)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
