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

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import cast

from harness.context import E2EError


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
    fault_migrate: str = ""
    """마이그레이션 실패 fault(G2-6 ②, nightly 쌍만 — `NEXT` core에 실패하는 migration과 epoch + 1을
    얹은 파생). `DEFAULT`(run.py)에는 없다(빈 이름)."""


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


NIGHTLY_SUFFIX = "nightly"
FAULT_HEALTH, FAULT_MIGRATE = "h", "m"
"""nightly FAULT 2종의 이름 접미사(`n<sha7>h`·`n<sha7>m`)와 버전 접미사(`<NEXT 버전>.fh`·`.fm`)."""
FAULT_CHANGED = {FAULT_HEALTH: "collector", FAULT_MIGRATE: "core"}
"""FAULT마다 NEXT와 달라야 하는 서비스 — 나머지 이미지(site-db 포함)는 NEXT와 digest가 같다."""
NIGHTLY_TREE_TAG = "line1/temp_sp"
"""nightly 쌍의 NEXT·FAULT 트리가 G2 트리(fixture + 시뮬레이터 100태그)와 다르게 두는 태그."""
RELEASE_NAME = re.compile(r"^r(\d+)$")
CALENDAR = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)$")
HEAD_SHA = re.compile(r"^[0-9a-f]{40}$")


def latest_tagged(records: Mapping[str, Mapping[str, object]], tags: Collection[str]) -> str:
    """git 태그가 붙은 `r<N>` release 기록 가운데 N이 가장 큰 이름(nightly BASE, D8)."""
    found = [
        (int(match[1]), name)
        for name in records
        if (match := RELEASE_NAME.fullmatch(name)) is not None and name in tags
    ]
    if not found:
        raise E2EError("no tagged r<N> release record (deploy/releases/r*.json + git tag)")
    return max(found)[1]


def nightly_version(base_version: str, head_sha: str) -> str:
    """`<BASE 다음 번호>-nightly.<sha7>` — 달력 버전의 마지막 번호만 하나 올린다. 접미사가 있는
    버전은 fleet `$defs/release` 패턴을 통과하고 `compat_app_min`(정식 release만 고른다)에 끼지
    않는다(`2026.11.1-fault1` 선례)."""
    match = CALENDAR.fullmatch(base_version)
    if match is None:
        raise E2EError(f"base version {base_version!r} is not YYYY.M.N")
    year, month, number = match.groups()
    return f"{year}.{int(month)}.{int(number) + 1}-{NIGHTLY_SUFFIX}.{head_sha[:7]}"


def nightly(
    records: Mapping[str, Mapping[str, object]],
    tags: Collection[str],
    head_sha: str,
    next_epoch: int,
) -> ReleasePair:
    """nightly 쌍(D8·E4, M6-3 H9): BASE = 태그가 있는 최신 `r<N>` 기록, NEXT = `n<sha7>`(HEAD 빌드,
    `bundles.py nightly-release`가 WSL 작업 디렉터리에만 기록한다), FAULT 2종(NEXT 파생,
    `bundles.py nightly-faults`): `fault` = `n<sha7>h`(collector만 세션을 맺지 않는 health 실패,
    버전 `<NEXT 버전>.fh`, NEXT epoch)·`fault_migrate` = `n<sha7>m`(core에 실패하는 migration, 버전
    `.fm`, NEXT epoch + 1). 수동 스키마 집합은 비운다 — G2 스키마 판정은 자동(D9,
    `expected-schema.json`)이다. 트리: NEXT·FAULT 번들은 G2 트리에서 태그 `NIGHTLY_TREE_TAG`의
    description만 release마다 바꾼다. `records`는 이름 → release 기록(JSON), `next_epoch`는 HEAD
    migration의 epoch다(상수로 박지 않는다 — HEAD가 migration을 더하면 FAULT-m도 따라 오른다)."""
    if HEAD_SHA.fullmatch(head_sha) is None:
        raise E2EError(f"head sha must be 40 lowercase hex digits, got {head_sha!r}")
    base = latest_tagged(records, tags)
    record = records[base]
    schema = record.get("schema")
    epoch = cast(Mapping[str, object], schema).get("epoch") if isinstance(schema, Mapping) else None
    if not isinstance(epoch, int):
        raise E2EError(f"{base}: release record has no schema.epoch")
    nxt = f"n{head_sha[:7]}"
    health, migrate = nxt + FAULT_HEALTH, nxt + FAULT_MIGRATE
    version = nightly_version(str(record.get("version")), head_sha)
    return ReleasePair(
        base=base,
        next=nxt,
        fault=health,
        fault_migrate=migrate,
        releases={
            base: str(record.get("version")),
            nxt: version,
            health: f"{version}.f{FAULT_HEALTH}",
            migrate: f"{version}.f{FAULT_MIGRATE}",
        },
        epochs={base: epoch, nxt: next_epoch, health: next_epoch, migrate: next_epoch + 1},
        caggs=frozenset(),
        base_columns=frozenset(),
        base_tables=frozenset(),
        base_indexes=frozenset(),
        next_tables=frozenset(),
        next_indexes=frozenset(),
        tree_tag=NIGHTLY_TREE_TAG,
        tree_descriptions={name: f"e2e tree {name}" for name in (nxt, health, migrate)},
    )


def fault_record_problems(
    pair: ReleasePair, name: str, record: Mapping[str, object], next_record: Mapping[str, object]
) -> list[str]:
    """nightly FAULT 기록(`name` = `pair.fault`·`pair.fault_migrate`)이 NEXT 파생 규칙을 어긴 곳(빈
    목록이면 맞다): 버전·스키마 envelope(epoch·app_expected = `pair.epochs[name]`,
    app_max_compatible = + 1), 바뀌어야 하는 서비스(`FAULT_CHANGED`) 하나만 NEXT와 config digest가
    다르고 나머지(site-db 포함)는 같다. WSL 빌드(`bundles.py nightly-faults`)와 prepare가 같이
    쓴다."""
    kinds = {pair.fault: FAULT_HEALTH, pair.fault_migrate: FAULT_MIGRATE}
    if not name or name not in kinds or not name.startswith(pair.next):
        return [f"{name!r} is not a nightly fault of {pair.next}"]
    changed = FAULT_CHANGED[kinds[name]]
    problems: list[str] = []
    if record.get("version") != pair.releases[name]:
        problems.append(f"version {record.get('version')!r} != {pair.releases[name]!r}")
    epoch = pair.epochs[name]
    schema = record.get("schema")
    got = (
        tuple(cast(Mapping[str, object], schema).get(k) for k in SCHEMA_KEYS)
        if isinstance(schema, Mapping)
        else None
    )
    if got != (epoch, epoch, epoch + 1):
        problems.append(f"schema {got} != {(epoch, epoch, epoch + 1)}")
    mine, theirs = _digests(record), _digests(next_record)
    if not theirs or set(mine) != set(theirs):
        return [*problems, f"services {sorted(mine)} != NEXT {sorted(theirs)}"]
    for svc, digest in sorted(mine.items()):
        same = digest == theirs[svc]
        if svc == changed and same:
            problems.append(f"{svc} digest equals NEXT (the fault must change it)")
        if svc != changed and not same:
            problems.append(f"{svc} digest {digest} != NEXT {theirs[svc]}")
    return problems


SCHEMA_KEYS = ("epoch", "app_expected", "app_max_compatible")


def _digests(record: Mapping[str, object]) -> dict[str, object]:
    images = record.get("images")
    if not isinstance(images, Mapping):
        return {}
    return {
        str(svc): cast(Mapping[str, object], info).get("config_digest")
        for svc, info in cast(Mapping[object, object], images).items()
        if isinstance(info, Mapping)
    }
