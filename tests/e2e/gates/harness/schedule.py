"""nightly cron 실행의 늦은 시작 판단(M6-2b G7) — 순수 함수다.

self-hosted runner가 꺼져 있으면 cron 실행은 queued로 최대 24시간 기다렸다가 PC를 켜는 순간
시작한다. 그러면 사용자가 일하는 낮에 preflight가 VM·결과를 지우고 VM 2대를 띄운다. 그래서 cron
(`schedule` 이벤트)으로 시작한 실행이 cron 시각보다 `LATE_AFTER` 넘게 늦었으면 `nightly.py all`은
아무것도 하지 않고 끝낸다. 수동 실행(`workflow_dispatch`)과 로컬 실행에는 적용하지 않는다.

cron 시각의 출처는 `CRON` 하나다 — `.github/workflows/nightly.yml`의 `on.schedule` cron과 같은지
단위 시험이 대조한다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

CRON = "0 18 * * *"
"""`nightly.yml` `on.schedule` cron(UTC) — 03:00 KST."""
SCHEDULE_EVENT = "schedule"
"""cron 실행의 `GITHUB_EVENT_NAME`."""
LATE_AFTER = timedelta(hours=2)
"""cron 시각보다 이만큼 넘게 늦게 시작한 cron 실행은 건너뛴다(같으면 돈다)."""
DAILY = re.compile(r"(\d{1,2}) (\d{1,2}) \* \* \*")
"""해석하는 cron 형식 — 매일 한 번(`분 시 * * *`)."""


def daily_time(cron: str) -> tuple[int, int] | None:
    """`분 시 * * *` → (시, 분). 그 밖의 형식이나 범위 밖 값이면 None."""
    match = DAILY.fullmatch(cron.strip())
    if match is None:
        return None
    minute, hour = int(match[1]), int(match[2])
    if minute > 59 or hour > 23:
        return None
    return hour, minute


def delay(cron: str, now: datetime) -> timedelta | None:
    """`now`(UTC, 시간대 있음) 이전 가장 최근 cron 시각부터 지난 시간. 자정을 넘겨 시작해도
    어제 시각부터 잰다(다음 날 17:50 UTC 시작 = 23시간 50분). 해석하지 못하면 None."""
    at = daily_time(cron)
    if at is None:
        return None
    hour, minute = at
    planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if planned > now:
        planned -= timedelta(days=1)
    return now - planned


def late_start(event: str, cron: str, now: datetime) -> bool:
    """cron(`schedule`)으로 시작한 실행이 cron 시각보다 `LATE_AFTER` 넘게 늦었는가. 다른 이벤트·
    로컬 실행(빈 이벤트 이름)은 늘 거짓이다. cron을 해석하지 못하면 거짓(돈다) — 건너뛰면 이력·
    status 하나 없이 밤마다 조용히 사라지지만, 돌면 최악이 늦은 실행 한 번이다(`CRON`은 시험이
    yml과 대조하므로 실제로는 해석된다)."""
    if event != SCHEDULE_EVENT:
        return False
    waited = delay(cron, now)
    return waited is not None and waited > LATE_AFTER
