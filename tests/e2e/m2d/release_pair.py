"""M2d VM 실측의 release 쌍 — 하네스가 release를 아는 유일한 곳.

`run.py`(Windows 호스트)와 `bundles.py`(WSL runner)가 같이 읽는다. 설치 g1 = `BASE`,
업그레이드 `BASE`→`NEXT`(압축 chunk 이력이 있는 DB 위에서 실제 마이그레이션), 헬스 실패 롤백은
`FAULT`(`NEXT` 이미지에 collector만 세션을 맺지 않는 파생, `bundles.py fault-release`). crash는
`BASE`↔`NEXT` 사이를 오간다.

- M2 실측(run 20261005T055523Z, 30/30): r0(epoch 1)→r1(epoch 2).
- S3 실측(run 20261009T231040Z, 30/30): r1(epoch 2)→r2(epoch 3, 0008·0009).
- S4 실측(run 20261017T132857Z, 30/30): r2(epoch 3)→r3(epoch 4, 0010~0012).
- M5 마감(run 20261101T035728Z, 35/35): r3(epoch 4)→r4(epoch 5, 0013 `project_files`). r4는
  프로젝트 트리를 적용하는 첫 release라 c0(r3)에서의 FAULT가 전환기 롤백(`E_DEFINITIONS_LEFT`
  경보)이었고, M5 확인 항목(트리 적용·현장 PATCH·드래프트 게이트·export→absorb→재적용)이 더해져
  35항목이 됐다.
- M4 마감: r4(epoch 5)→r5(epoch 5). **현장 마이그레이션이 없다**(r4..r5 core 변경은
  `platform/migrate_lint.py` 하나, epochs.yaml 그대로) — 업그레이드·crash `migrating`은 phase를
  지나가지만 SQL은 돌지 않는다. r4가 이미 트리 적용 release라 c0(r4)의 FAULT는 전환기가 아니라
  트리 백업이 있는 일반 롤백(경보 없음)이다. 온라인 단계(`run.step_online`, 중앙 c0 HTTPS
  polling·게시·보고·중앙 중지) 4항목이 더해져 39항목이다.

`BASE`·`NEXT`의 기록은 저장소 `deploy/releases/<이름>.json`, 이미지는 runner image store
`<store>/<이름>/<svc>.tar`(release_record.py)에 있어야 한다.
"""

from __future__ import annotations

BASE, NEXT, FAULT = "r4", "r5", "r5f"
RELEASES = {BASE: "2026.11.0", NEXT: "2026.11.1", FAULT: "2026.11.1-fault1"}
"""release 이름 → 달력 버전(fleet `product:`, release 기록 `version`)."""
EPOCHS = {BASE: 5, NEXT: 5, FAULT: 5}
"""기대 스키마 epoch. prepare가 release 기록의 `schema.epoch`와 대조한다(FAULT는 NEXT 스키마)."""

# 스키마 산출물(migration SQL 기준, `run.judge_c0`·`run.judge_next_schema`가 집합으로 비교).
# BASE epoch까지의 것은 c0에 있고 업그레이드 뒤에도 남아야 한다. NEXT epoch의 것은 c0에 없고
# 업그레이드 뒤에 생겨야 한다. 쌍을 바꾸면 새 NEXT epoch의 migration을 읽고 함께 바꾼다
# (지난 NEXT 산출물은 BASE_*로 옮긴다 — M4 마감에서 0013 `project_files`를 BASE_TABLES로).
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
"""epoch 3(0008·0009)이 더한 열 `테이블.열` — BASE(epoch 5)에도 있다."""
BASE_TABLES = frozenset(
    {"users", "sessions", "audit_log", "alarms", "alarm_journal", "project_files"}
)
"""BASE(epoch 5): 0010 users·sessions, 0011 audit_log, 0012 alarms·alarm_journal, 0013
project_files."""
BASE_INDEXES = frozenset(
    {
        "sessions_user_id_idx",  # 0010
        "audit_log_time_idx",  # 0011
        "audit_log_username_time_idx",  # 0011
        "alarm_journal_alarm_id_time_idx",  # 0012
        "alarm_journal_time_idx",  # 0012
    }
)
"""BASE(epoch 5)가 이름을 붙여 만든 인덱스(PK·UNIQUE 인덱스는 테이블로 확인된다). 0013은
이름 붙은 인덱스를 만들지 않는다."""
NEXT_TABLES: frozenset[str] = frozenset()
"""NEXT(r5)가 더한 테이블 — 없다(같은 epoch 5). 빈 집합이라 판정 항목에서 뺐다(`judge_c0`의
`next_absent`·`judge_next_schema`의 `next_tables`): 빈 집합과의 서로소·포함 검사는 늘 참이라
아무것도 확인하지 않는 항목이 결과에 남는다. 다음 쌍이 epoch를 올리면 새 migration의 테이블을
여기에 적고 두 판정을 되살린다(`test_next_artifacts_empty_until_epoch_moves`)."""
NEXT_INDEXES: frozenset[str] = frozenset()
"""NEXT가 이름을 붙여 만든 인덱스 — 없다(위와 같은 이유로 판정에서 뺐다)."""

# 프로젝트 트리(M4 마감 수정 r1). c0(BASE g1)는 fixture `acme` 트리다. NEXT·FAULT 번들은 fixture
# 사본에서 태그 하나의 `description`만 바꾼 트리를 싣는다(`bundles.release_tree`) — 같은 트리면
# core가 재적용을 no-op(`changed=False`)으로 건너뛰어 업그레이드가 정의를 다시 쓰지 않고(c0 시드
# 장비·태그가 retire되지 않는다), "트리 적용"·"롤백 뒤 정의 복원"이 같은 epoch에서 늘 참인 판정이
# 된다. 판정은 이 값이 DB 태그 정의에 들어갔는지(`run.judge_tree`의 `definition`)로 구별한다.
TREE_TAG = "line1/temp_sp"
"""NEXT·FAULT 트리가 fixture와 다르게 두는 태그. M5 확인 ⓑ의 PATCH 대상(`line1/temp`·
`temp-high`)과 겹치지 않는다."""
TREE_DESCRIPTIONS = {NEXT: "1라인 온도 설정값 (r5)", FAULT: "1라인 온도 설정값 (r5f)"}
"""release → 그 번들 트리의 `TREE_TAG` description(fixture는 "1라인 온도 설정값")."""
