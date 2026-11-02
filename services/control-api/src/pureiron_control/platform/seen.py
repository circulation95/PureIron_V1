"""`sites.last_seen`(stale 판정 기준, C8) 갱신 — token(·서명)이 확인된 온라인 요청이면 seq·반영
여부와 무관하게 중앙 시각을 남긴다. heartbeat·보고(`reports`)와 desired polling(`bundles`,
M4-1 E3)이 같이 쓴다(도메인끼리 import하지 않게 여기 둔다).
"""

from __future__ import annotations

from datetime import datetime

from pureiron_core.platform.db import Connection

# 시각은 잠금을 잡기 전에 잰다 — 동시 요청이 먼저 쓴 더 늦은 시각을 되돌리지 않는다.
TOUCH_SQL = """
UPDATE sites SET last_seen = GREATEST(coalesce(last_seen, $2), $2) WHERE site_id = $1
"""


async def touch(conn: Connection, site_id: str, seen_at: datetime) -> None:
    await conn.execute(TOUCH_SQL, site_id, seen_at)
