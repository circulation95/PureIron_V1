"""VM 실측의 release 쌍(`ReleasePair`) — 하네스가 release를 아는 유일한 곳.

`m2d/run.py`(Windows 호스트)와 `bundles.py`(WSL runner)가 같이 읽는다. 판정 함수(`judges`)는 쌍을
인자로 받는다 — 상수를 import하지 않는다. 지금 쌍은 `DEFAULT`(M4 마감 r4→r5, fault r5f).
설치 g1 = `BASE`, 업그레이드 `BASE`→`NEXT`(압축 chunk 이력이 있는 DB 위에서 실제 마이그레이션),
헬스 실패 롤백은 `FAULT`(`NEXT` 이미지에 collector만 세션을 맺지 않는 파생,
`bundles.py fault-release`). crash는 `BASE`↔`NEXT` 사이를 오간다. (아래 `BASE`·`NEXT`·`FAULT`는
`ReleasePair.base`·`next`·`fault`.)

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

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ReleasePair:
    """설치 g1 = `base`, 업그레이드 `base`→`next`, 헬스 실패 롤백 `fault`(`next` 파생)와 그
    기대값."""

    base: str
    next: str
    fault: str
    releases: Mapping[str, str]
    """release 이름 → 달력 버전(fleet `product:`, release 기록 `version`)."""
    epochs: Mapping[str, int]
    """기대 스키마 epoch. prepare가 release 기록의 `schema.epoch`와 대조한다(FAULT는 NEXT
    스키마)."""
    # 스키마 산출물(migration SQL 기준, `judges.judge_c0`·`judges.judge_next_schema`가 집합으로
    # 비교). BASE epoch까지의 것은 c0에 있고 업그레이드 뒤에도 남아야 한다. NEXT epoch의 것은 c0에
    # 없고 업그레이드 뒤에 생겨야 한다. 쌍을 바꾸면 새 NEXT epoch의 migration을 읽고 함께 바꾼다
    # (지난 NEXT 산출물은 base_*로 옮긴다 — M4 마감에서 0013 `project_files`를 base_tables로).
    caggs: frozenset[str]
    base_columns: frozenset[str]
    """BASE까지 더해진 열 `테이블.열`."""
    base_tables: frozenset[str]
    base_indexes: frozenset[str]
    """BASE가 이름을 붙여 만든 인덱스(PK·UNIQUE 인덱스는 테이블로 확인된다)."""
    next_tables: frozenset[str]
    """NEXT가 더한 테이블. 빈 집합이면 판정 항목에서 뺀다(`judge_c0`의 `next_absent`·
    `judge_next_schema`의 `next_tables`): 빈 집합과의 서로소·포함 검사는 늘 참이라 아무것도
    확인하지 않는 항목이 결과에 남는다. 다음 쌍이 epoch를 올리면 새 migration의 테이블을 적고 두
    판정을 되살린다(`test_next_artifacts_empty_until_epoch_moves`)."""
    next_indexes: frozenset[str]
    """NEXT가 이름을 붙여 만든 인덱스(위와 같은 이유로 비면 판정에서 뺀다)."""
    # 프로젝트 트리(M4 마감 수정 r1). c0(BASE g1)는 fixture `acme` 트리다. NEXT·FAULT 번들은
    # fixture 사본에서 태그 하나의 `description`만 바꾼 트리를 싣는다(`bundles.release_tree`) — 같은
    # 트리면 core가 재적용을 no-op(`changed=False`)으로 건너뛰어 업그레이드가 정의를 다시 쓰지 않고
    # (c0 시드 장비·태그가 retire되지 않는다), "트리 적용"·"롤백 뒤 정의 복원"이 같은 epoch에서 늘
    # 참인 판정이 된다. 판정은 이 값이 DB 태그 정의에 들어갔는지(`judges.judge_tree`의
    # `definition`)로 구별한다.
    tree_tag: str
    """NEXT·FAULT 트리가 fixture와 다르게 두는 태그. M5 확인 ⓑ의 PATCH 대상과 겹치지 않아야 한다."""
    tree_descriptions: Mapping[str, str]
    """release → 그 번들 트리의 `tree_tag` description."""


DEFAULT = ReleasePair(
    base="r4",
    next="r5",
    fault="r5f",
    releases={"r4": "2026.11.0", "r5": "2026.11.1", "r5f": "2026.11.1-fault1"},
    epochs={"r4": 5, "r5": 5, "r5f": 5},
    # 1분(0004)·1시간(0006) CAGG. 0009부터 둘 다 실시간 집계(`materialized_only = false`).
    caggs=frozenset({"tag_history_1m", "tag_history_1h"}),
    # epoch 3(0008·0009)이 더한 열 — BASE(epoch 5)에도 있다.
    base_columns=frozenset(
        {
            "devices.definition",  # 0008
            "tags.definition",  # 0008
            "project_state.definitions_rev",  # 0008
            "project_state.historian",  # 0009
        }
    ),
    # BASE(epoch 5): 0010 users·sessions, 0011 audit_log, 0012 alarms·alarm_journal, 0013
    # project_files.
    base_tables=frozenset(
        {"users", "sessions", "audit_log", "alarms", "alarm_journal", "project_files"}
    ),
    # 0013은 이름 붙은 인덱스를 만들지 않는다.
    base_indexes=frozenset(
        {
            "sessions_user_id_idx",  # 0010
            "audit_log_time_idx",  # 0011
            "audit_log_username_time_idx",  # 0011
            "alarm_journal_alarm_id_time_idx",  # 0012
            "alarm_journal_time_idx",  # 0012
        }
    ),
    # r5는 같은 epoch 5 — 더한 테이블·인덱스가 없다.
    next_tables=frozenset(),
    next_indexes=frozenset(),
    # `line1/temp_sp` — ⓑ PATCH 대상(`line1/temp`·`temp-high`)과 겹치지 않는다. fixture 값은
    # "1라인 온도 설정값".
    tree_tag="line1/temp_sp",
    tree_descriptions={"r5": "1라인 온도 설정값 (r5)", "r5f": "1라인 온도 설정값 (r5f)"},
)
"""지금 쌍(M4 마감): r4(epoch 5)→r5(epoch 5, 현장 마이그레이션 없음), fault r5f."""
