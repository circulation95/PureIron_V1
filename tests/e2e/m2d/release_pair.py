"""M2d VM 실측의 release 쌍 — 하네스가 release를 아는 유일한 곳.

`run.py`(Windows 호스트)와 `bundles.py`(WSL runner)가 같이 읽는다. 설치 g1 = `BASE`,
업그레이드 `BASE`→`NEXT`(압축 chunk 이력이 있는 DB 위에서 실제 마이그레이션), 헬스 실패 롤백은
`FAULT`(`NEXT` 이미지에 collector만 세션을 맺지 않는 파생, `bundles.py fault-release`). crash는
`BASE`↔`NEXT` 사이를 오간다.

- M2 실측(run 20261005T055523Z, 30/30): r0(epoch 1)→r1(epoch 2).
- S3 실측(run 20261009T231040Z, 30/30): r1(epoch 2)→r2(epoch 3, 0008·0009).
- S4(M3b 마감): r2(epoch 3)→r3(epoch 4, 0010~0012).

`BASE`·`NEXT`의 기록은 저장소 `deploy/releases/<이름>.json`, 이미지는 runner image store
`<store>/<이름>/<svc>.tar`(release_record.py)에 있어야 한다.
"""

from __future__ import annotations

BASE, NEXT, FAULT = "r2", "r3", "r3f"
RELEASES = {BASE: "2026.10.2", NEXT: "2026.10.3", FAULT: "2026.10.3-fault1"}
"""release 이름 → 달력 버전(fleet `product:`, release 기록 `version`)."""
EPOCHS = {BASE: 3, NEXT: 4, FAULT: 4}
"""기대 스키마 epoch. prepare가 release 기록의 `schema.epoch`와 대조한다(FAULT는 NEXT 스키마)."""

# 스키마 산출물(migration SQL 기준, `run.judge_c0`·`run.judge_next_schema`가 집합으로 비교).
# BASE epoch까지의 것은 c0에 있고 업그레이드 뒤에도 남아야 한다. NEXT epoch의 것은 c0에 없고
# 업그레이드 뒤에 생겨야 한다. 쌍을 바꾸면 새 NEXT epoch의 migration을 읽고 함께 바꾼다.
CAGGS = frozenset({"tag_history_1m", "tag_history_1h"})
"""1분(0004)·1시간(0006) CAGG. 0009부터 둘 다 실시간 집계(`materialized_only = false`)."""
BASE_COLUMNS = frozenset(
    {
        "devices.definition",  # 0008
        "tags.definition",  # 0008
        "project_state.definitions_rev",  # 0008
        "project_state.historian",  # 0009
    }
)
"""BASE(epoch 3)가 더한 열 `테이블.열`."""
NEXT_TABLES = frozenset({"users", "sessions", "audit_log", "alarms", "alarm_journal"})
"""NEXT(epoch 4): 0010 users·sessions, 0011 audit_log, 0012 alarms·alarm_journal."""
NEXT_INDEXES = frozenset(
    {
        "sessions_user_id_idx",  # 0010
        "audit_log_time_idx",  # 0011
        "audit_log_username_time_idx",  # 0011
        "alarm_journal_alarm_id_time_idx",  # 0012
        "alarm_journal_time_idx",  # 0012
    }
)
"""NEXT(epoch 4)가 이름을 붙여 만든 인덱스(PK·UNIQUE 인덱스는 테이블로 확인된다)."""
