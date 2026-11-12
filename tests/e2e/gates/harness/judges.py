"""순수 판정 함수 — probe 스냅샷·보고·`/fleet` 행·release 기록을 받아 항목별 참/거짓을 낸다.

I/O가 없다(원격 조작은 `site`, 트리 파일 읽기는 `trees`). release에 기대는 판정은 `ReleasePair`를,
현장 이름에 기대는 판정은 site id를 인자로 받는다 — 모듈 상수를 import하지 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from harness import golden
from harness.context import E2EError
from harness.release_pair import ReleasePair

APP_SERVICES = ("core", "collector", "web", "site-db")
ROLLBACK_LIMIT_S = 300
E_DEFINITIONS_LEFT = "E_DEFINITIONS_LEFT"
"""전환기 롤백 경보 코드(updater `phases.E_DEFINITIONS_LEFT`, README runbook)."""
FAULT_CODE = "E_HEALTH"
"""FAULT(r4f) 시도의 보고 `code` — checking phase의 health 실패(updater `phases.E_HEALTH`)."""
ALARM_METRIC = "updater_failed_manual"
AUDIT_COUNTERS = frozenset(name for name, _ in (*golden.OUT_RULES, *golden.FWD_RULES))
"""판정에 꼭 있어야 하는 외부 접속 카운터 이름. `field_fwd`는 끝점이 없으면 규칙도 없다."""
LOOKUP = re.compile(r"Looking up RR for (\S+) IN \S+")
ABSORBED_HASH = re.compile(r"^ok: (sha256:[0-9a-f]{64})$", re.MULTILINE)

PROJECT_VERSION = "v1"
"""`bundles.py site` fleet 파일의 `project.version` — `GET /project`의 `version`이 된다."""
SEED_DEVICE, SEED_TAG = "e2e-dev", "e2e/tag"
"""c0 이력 시드가 DB에 직접 넣는 장비·태그 — 트리 밖이라 트리를 다시 적용하면 retire된다."""
PATCH_TAG = "line1/temp"
PATCH_TAG_BODY: dict[str, object] = {"record": {"deadband": 0.8, "max_interval_s": 30}}
"""fixture `record: {deadband: 0.5, max_interval_s: 60}`의 운영 값 변경."""
PATCH_ALARM = "temp-high"
PATCH_ALARM_BODY: dict[str, object] = {"setpoint": 82.5}
"""fixture `setpoint: 80`의 운영 값 변경."""
PATCHES = 2
"""바꾸는 PATCH 수 — 하나마다 site_rev +1."""
ONLINE_GEN = 2
"""온라인 ②의 generation — c0의 applied g1 다음. 15분 실측의 fleet PR이 site-a gen 2 → 3이어야
check-gen의 `new == old + 1`·`new > max_published_gen`(= 2)을 함께 만족한다."""


# ---------------------------------------------------------------- probe 스냅샷


def running_digests(snapshot: dict[str, object]) -> dict[str, str]:
    containers = cast(dict[str, dict[str, object]], snapshot.get("containers") or {})
    return {
        svc: str(info.get("image_id"))
        for svc, info in containers.items()
        if svc in APP_SERVICES and info.get("running")
    }


def updater_state(snapshot: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], snapshot.get("updater") or {})


def ready(snapshot: dict[str, object]) -> bool:
    readyz = cast(dict[str, object], snapshot.get("readyz") or {})
    return readyz.get("status") == 200


def as_int(value: object) -> int:
    """probe 값(문자열 숫자, 실패 시 `{"error": ...}`)을 정수로 — 못 읽으면 -1."""
    try:
        return int(str(value))
    except ValueError:
        return -1


def alarm_has(alarm: object, code: str) -> bool:
    """Updater 경보(`g<gen>: <code> …` 항목을 `; `로 이은 문자열)에 `code`로 끝나는 항목이
    있는가."""
    return isinstance(alarm, str) and any(part.endswith(code) for part in alarm.split("; "))


def metric(snap: dict[str, object], name: str) -> float | None:
    """probe `metrics`(textfile 메트릭) 값. 못 읽었으면 None."""
    value = cast(dict[str, object], snap.get("metrics") or {}).get(name)
    return float(value) if isinstance(value, int | float) else None


def edit_unlocked(snap: dict[str, object]) -> bool:
    project = cast(dict[str, object], snap.get("project") or {})
    body = cast(dict[str, object], project.get("body") or {})
    return project.get("status") == 200 and body.get("edit_locked") is False


def core_started_at(snap: dict[str, object]) -> object:
    containers = cast(dict[str, dict[str, object]], snap.get("containers") or {})
    return containers.get("core", {}).get("started_at")


def db_of(snap: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], snap.get("db") or {})


def names(db: dict[str, object], key: str) -> frozenset[str]:
    """probe의 쉼표 구분 이름 목록을 집합으로. 못 읽었으면(`{"error": ...}`) 빈 집합."""
    value = db.get(key)
    return frozenset(n for n in value.split(",") if n) if isinstance(value, str) else frozenset()


# ---------------------------------------------------------------- release 기록·스키마


def digests_of(release_json: dict[str, object]) -> dict[str, str]:
    images = cast(dict[str, dict[str, str]], release_json["images"])
    return {svc: images[svc]["config_digest"] for svc in APP_SERVICES}


def check_record(pair: ReleasePair, name: str, record: dict[str, object]) -> None:
    """release 기록이 쌍의 기대(달력 버전·스키마 epoch·앱 envelope [epoch, epoch + 1])와 같은지 —
    다르면 잘못된 기반에서 35개를 돌리게 되므로 prepare에서 멈춘다. envelope는 롤백 판정(BASE
    앱이 NEXT epoch DB에서 ready)의 전제다."""
    schema = cast(dict[str, object], record.get("schema") or {})
    epoch = pair.epochs[name]
    got = (
        record.get("version"),
        schema.get("epoch"),
        schema.get("app_expected"),
        schema.get("app_max_compatible"),
    )
    want = (pair.releases[name], epoch, epoch, epoch + 1)
    if got != want:
        raise E2EError(
            f"{name}.json: (version, epoch, app_expected, app_max_compatible) {got} != {want}"
        )


def base_schema_kept(db: dict[str, object], pair: ReleasePair) -> bool:
    """BASE(epoch 5)까지의 산출물: 1m·1h CAGG와 실시간 집계, 0008·0009 열, 0010~0013
    테이블·인덱스."""
    return (
        names(db, "caggs") == pair.caggs
        and names(db, "realtime_caggs") == pair.caggs
        and names(db, "columns") >= pair.base_columns
        and names(db, "tables") >= pair.base_tables
        and names(db, "indexes") >= pair.base_indexes
    )


def judge_c0(db: dict[str, object], pair: ReleasePair) -> dict[str, bool]:
    """체크포인트 c0 기반: BASE epoch와 그 산출물, 압축 chunk가 있는 이력 — 업그레이드가 압축 DB
    위에서 돌고 이력 보존(`rows_kept`)을 잴 수 있어야 한다. NEXT가 같은 epoch라 NEXT 산출물이
    없다(`ReleasePair.next_tables` 빈 집합) — `next_absent`는 늘 참이 되므로 뺐다."""
    return {
        "epoch": db.get("epoch") == str(pair.epochs[pair.base]),
        "base_schema": base_schema_kept(db, pair),
        "compressed_chunks": as_int(db.get("compressed_chunks")) > 0,
        "seed_rows": as_int(db.get("tag_history_rows")) > 0,
    }


def judge_next_schema(db: dict[str, object], pair: ReleasePair) -> dict[str, bool]:
    """업그레이드 뒤 NEXT(r5) 스키마: epoch 5 그대로(마이그레이션 없음), BASE 산출물 유지(1분·1시간
    CAGG와 실시간 집계, 0008·0009 열, 0010~0013 테이블·인덱스), 압축 chunk 유지. NEXT 산출물이
    없어 `next_tables`·`next_indexes`는 보지 않는다(`ReleasePair.next_tables`·`next_indexes`)."""
    return {
        "epoch": db.get("epoch") == str(pair.epochs[pair.next]),
        "caggs": names(db, "caggs") == pair.caggs,
        "realtime_caggs": names(db, "realtime_caggs") == pair.caggs,
        "base_columns": names(db, "columns") >= pair.base_columns,
        "base_tables": names(db, "tables") >= pair.base_tables,
        "base_indexes": names(db, "indexes") >= pair.base_indexes,
        "compressed_chunks": as_int(db.get("compressed_chunks")) > 0,
    }


SCHEMA_SETS = ("tables", "columns", "indexes", "caggs", "realtime_caggs")
"""자동 판정(D9)이 비교하는 이름 집합 5종 — probe `SCHEMA_KEYS`와 같다(단위 시험이 대조한다)."""


def judge_schema_equal(db: dict[str, object], expected: dict[str, object]) -> dict[str, bool]:
    """D9 ⓐ(업그레이드 뒤)·ⓒ(새 설치 c0): 이름 집합 5종이 기대 스키마(`expected-schema.json`)와
    정확히 같고 epoch도 같다. 기대 집합이 비면 거짓이다 — 못 읽은 기대값이 통과가 되지 않게."""
    checks = {
        f"schema_{key}": bool(names(expected, key)) and names(db, key) == names(expected, key)
        for key in SCHEMA_SETS
    }
    want = expected.get("epoch")
    checks["schema_epoch"] = isinstance(want, str) and bool(want) and db.get("epoch") == want
    return checks


def judge_schema_kept(before: dict[str, object], after: dict[str, object]) -> dict[str, bool]:
    """D9 ⓑ: 업그레이드 전(c0, BASE) 이름 집합 5종이 업그레이드 뒤 집합에 모두 있다(ADD-only). 전
    집합이 비면(못 읽음) 거짓."""
    return {
        f"kept_{key}": bool(names(before, key)) and names(before, key) <= names(after, key)
        for key in SCHEMA_SETS
    }


def schema_diff(db: dict[str, object], expected: dict[str, object]) -> dict[str, object]:
    """기록용: 집합마다 기대에 없는 이름(`extra`)·기대에 있는데 없는 이름(`missing`), 다른 것만."""
    diff: dict[str, object] = {}
    for key in SCHEMA_SETS:
        got, want = names(db, key), names(expected, key)
        if got != want:
            diff[key] = {"extra": sorted(got - want), "missing": sorted(want - got)}
    if db.get("epoch") != expected.get("epoch"):
        diff["epoch"] = {"got": db.get("epoch"), "want": expected.get("epoch")}
    return diff


# ---------------------------------------------------------------- 끝난 시도


def judge_attempt(
    snap: dict[str, object],
    report: dict[str, object],
    *,
    expect: str,
    want_digests: dict[str, str],
    want_applied: int | None,
    expect_alarm: str | None = None,
    expect_code: str | None = None,
    check_metric: bool = False,
) -> dict[str, bool]:
    """끝난 시도 판정 항목(모두 참이어야 통과). `last_result`는 `failed_manual`도 `failed`로
    접으므로(metrics.result_of) 자동 롤백 실패는 경보·보고의 `failed_manual`로 따로 본다.

    `expect_alarm`이 없으면 경보가 없어야 한다(`no_manual_alarm`). 있으면(전환기 롤백
    `E_DEFINITIONS_LEFT`) 그 코드의 경보 항목이 있고, 보고 `logs`에도 그 코드가 있으며, 자동 롤백
    자체는 성공(`failed_manual` False)이어야 한다(`expected_alarm`). `expect_code`가 있으면 보고
    `code`(실패한 phase의 코드)가 그 값이어야 한다(`code`) — 전환기 경보는 어느 phase에서 실패했든
    붙으므로 실패 원인은 이것으로 고정한다. `check_metric`이면 textfile 메트릭
    `updater_failed_manual`이 경보 유무(1/0)와 같아야 한다 — crash 케이스에는 켜지 않는다
    (`_finish`가 상태 저장 뒤 메트릭을 쓰므로 그 사이 kill이면 메트릭만 이전 값일 수 있다). 비crash
    항목도 서비스 tick이 시도를 집었다면(CLI가 exit 2 재시도를 다 쓴 경우) 그 사이(수 ms)에 probe가
    끼어 거짓 실패할 수 있으나, 확률이 무시할 만해 다시 보지 않는다."""
    state = updater_state(snap)
    manual_ok = report.get("failed_manual") is False
    checks: dict[str, bool] = {"result": state.get("last_result") == expect}
    if expect_code is not None:
        checks["code"] = report.get("code") == expect_code
    if expect_alarm is None:
        checks["no_manual_alarm"] = state.get("alarm") is None and manual_ok
    else:
        checks["expected_alarm"] = (
            alarm_has(state.get("alarm"), expect_alarm)
            and expect_alarm in str(report.get("logs") or "")
            and manual_ok
        )
    if check_metric:
        checks["alarm_metric"] = metric(snap, ALARM_METRIC) == (0 if expect_alarm is None else 1)
    checks |= {
        "applied_gen": state.get("applied_gen") == want_applied,
        "images_match": running_digests(snap) == want_digests,
        "edit_unlocked": edit_unlocked(snap),
    }
    return checks


def judge_rollback_time(result: dict[str, object]) -> bool:
    """FAULT 롤백(`apply_and_check` 결과)이 BASE digest로 돌아왔고 `ROLLBACK_LIMIT_S` 안에
    끝났다."""
    return bool(result["images_match"]) and float(str(result["seconds"])) <= ROLLBACK_LIMIT_S


ROLLING_BACK = "rolling_back"
"""Updater phase — 실패한 phase에서 들어가 직전 이미지 + ready를 확인하면 `failed`로 나간다."""
CONVERGE_LIMIT_S = 600
"""fault 시도가 failed로 수렴해야 하는 시간(G2-6 "10분 안")."""


def rolling_back_seconds(report: dict[str, object]) -> float | None:
    """보고 `phase_durations_s.rolling_back` — `rolling_back` 진입부터 다음 phase(`failed`·
    `failed_manual`) 진입까지. Updater는 직전 이미지를 띄우고 ready를 확인한 뒤에야 `failed`로
    들어가므로(`phases._rollback`) 이 구간이 "롤백 시작부터 직전 이미지 + ready까지"다. 롤백을 다시
    해도(`rollback_tries`) phase 진입 시각은 처음 한 번이라 다시 한 시간이 포함된다. 없으면 None."""
    durations = report.get("phase_durations_s")
    if not isinstance(durations, dict):
        return None
    value = cast(dict[str, object], durations).get(ROLLING_BACK)
    return float(value) if isinstance(value, int | float) else None


def judge_rollback_window(report: dict[str, object]) -> bool:
    """`rolling_back` 구간(`rolling_back_seconds`)이 있고 `ROLLBACK_LIMIT_S` 이하다."""
    seconds = rolling_back_seconds(report)
    return seconds is not None and 0 <= seconds <= ROLLBACK_LIMIT_S


def rows_kept(before: dict[str, object], after: dict[str, object]) -> dict[str, bool]:
    """행 수 4종(`ROW_KEYS`)이 이전 이상이다(이전 값을 못 읽었으면 거짓)."""
    return {
        f"rows_{key.removesuffix('_rows')}": 0 <= as_int(before.get(key)) <= as_int(after.get(key))
        for key in ROW_KEYS
    }


def prefixed(prefix: str, checks: dict[str, bool]) -> dict[str, bool]:
    return {f"{prefix}{name}": ok for name, ok in checks.items()}


def judge_g2_rollback(
    before: dict[str, object],
    after: dict[str, object],
    report: dict[str, object],
    *,
    base_digests: dict[str, str],
    base_tree: TreeExpect,
) -> dict[str, bool]:
    """G2-4(revert 전): c0(BASE)에서 FAULT-h를 적용한 시도가 `failed`·보고 `E_HEALTH`로 끝나고 실행
    이미지가 BASE digest이며 `rolling_back` 진입부터 직전 이미지 + ready까지 `ROLLBACK_LIMIT_S`
    이하다. 행 수 4종 ≥ 이전, site_rev·base_site_rev가 이전과 같고, BASE 트리(content_hash·정의
    값)로 돌아왔으며, 경보 없음·메트릭 `updater_failed_manual` 0·edit-lock 해제·applied gen
    그대로."""
    applied = updater_state(before).get("applied_gen")
    checks = judge_attempt(
        after,
        report,
        expect="failed",
        want_digests=base_digests,
        want_applied=cast(int | None, applied),
        expect_code=FAULT_CODE,
        check_metric=True,
    )
    rev = revs(project_body(before))
    return {
        "c0_ready": ready(before),
        **checks,
        "rolling_back_window": judge_rollback_window(report),
        **rows_kept(db_of(before), db_of(after)),
        "revs_kept": rev is not None and revs(project_body(after)) == rev,
        **prefixed("base_tree_", judge_tree(after, base_tree)),
    }


def judge_health_failure(
    snap: dict[str, object],
    report: dict[str, object],
    seconds: float,
    *,
    gen: int,
    base_digests: dict[str, str],
) -> dict[str, bool]:
    """G2-6 ③(G2-4와 같은 FAULT-h 시도): `CONVERGE_LIMIT_S` 안에 그 gen이 failed로 수렴했고(보고
    `E_HEALTH`), 시도 잔류가 없으며(probe가 읽은 `attempt`가 None — 못 읽었으면 거짓), 직전 정상
    이미지(BASE)로 ready이고 edit-lock이 풀렸다."""
    state = updater_state(snap)
    return {
        "failed_in_time": state.get("last_result") == "failed"
        and state.get("last_attempted_gen") == gen
        and 0 <= seconds <= CONVERGE_LIMIT_S,
        "code": report.get("code") == FAULT_CODE,
        "no_attempt_left": "attempt" in state and state["attempt"] is None,
        "previous_ready": ready(snap) and running_digests(snap) == base_digests,
        "edit_unlocked": edit_unlocked(snap),
    }


def judge_g2_retry(
    c0_db: dict[str, object],
    snap: dict[str, object],
    report: dict[str, object],
    *,
    gen: int,
    next_digests: dict[str, str],
    next_tree: TreeExpect,
    expected_schema: dict[str, object],
) -> dict[str, bool]:
    """G2-4 재시도(롤백된 BASE 위에서 NEXT): `succeeded`·실행 이미지 == NEXT·NEXT 트리 적용·경보
    없음·메트릭 0, D9 ⓐ(이름 집합 5종 == 기대 스키마)·ⓑ(c0 집합 ⊂ 지금 집합), 압축 chunk 유지
    (c0 이상, c0 > 0)."""
    checks = judge_attempt(
        snap,
        report,
        expect="succeeded",
        want_digests=next_digests,
        want_applied=gen,
        check_metric=True,
    )
    db = db_of(snap)
    chunks = as_int(c0_db.get("compressed_chunks"))
    return {
        **checks,
        **prefixed("next_tree_", judge_tree(snap, next_tree)),
        **judge_schema_equal(db, expected_schema),
        **judge_schema_kept(c0_db, db),
        "compressed_chunks_kept": 0 < chunks <= as_int(db.get("compressed_chunks")),
    }


# ---------------------------------------------------------------- 외부 접속(nftables·resolved)


def nft_accumulate(
    total: dict[str, int], last: dict[str, int], now: dict[str, int]
) -> dict[str, int]:
    """`last` 이후 늘어난 만큼 `total`에 더한 새 합계. 값이 줄었으면(표시 없이 재부팅)
    0부터 센다."""
    merged = dict(total)
    for key, value in now.items():
        before = last.get(key, 0)
        merged[key] = merged.get(key, 0) + (value - before if value >= before else value)
    return merged


def no_external(counters: dict[str, int]) -> bool:
    """외부 접속 카운터가 모두 있고 0인가. 하나라도 없으면(nft 출력 형식·규칙이 바뀌어 못 읽음)
    거짓이다 — 빈 카운터가 통과하지 않게. `field_fwd`(선언된 현장 장비 폴링)는 외부 접속이
    아니다."""
    if not counters.keys() >= AUDIT_COUNTERS:
        return False
    return not any(value for name, value in counters.items() if name != golden.FIELD_RULE)


def dns_names(log: str, site_id: str) -> dict[str, int]:
    """resolved 디버그 로그의 조회 이름별 횟수(로컬에서 답하는 호스트 이름 `site_id` 제외)."""
    counts: dict[str, int] = {}
    for found in LOOKUP.findall(log):
        name = found.rstrip(".")
        if name not in (site_id, "localhost"):
            counts[name] = counts.get(name, 0) + 1
    return counts


# ---------------------------------------------------------------- 프로젝트 트리·M5 확인


@dataclass(frozen=True)
class TreeExpect:
    """번들에 실은 프로젝트 트리에서 DB·`GET /project`에 있어야 할 값(`trees.tree_expect`)."""

    files: int
    content_hash: str
    devices: frozenset[str]
    tags: frozenset[str]
    alarms: frozenset[str]
    tree_tag: str
    """정의 값을 보는 태그(`ReleasePair.tree_tag`)."""
    tree_tag_description: str | None
    """태그 `tree_tag`의 description — 트리마다 다른 정의 값(fixture·NEXT·FAULT)."""


def project_body(snap: dict[str, object]) -> dict[str, object]:
    project = cast(dict[str, object], snap.get("project") or {})
    return (
        cast(dict[str, object], project.get("body") or {}) if project.get("status") == 200 else {}
    )


def revs(body: object) -> tuple[int, int] | None:
    """`GET /project` 본문의 (site_rev, base_site_rev), 못 읽으면 None."""
    if not isinstance(body, dict):
        return None
    state = cast(dict[str, object], body)
    site, base = state.get("site_rev"), state.get("base_site_rev")
    return (site, base) if isinstance(site, int) and isinstance(base, int) else None


def definitions(db: dict[str, object], key: str) -> dict[str, dict[str, object]]:
    """probe `tag_defs`·`alarm_defs`(JSON 문자열)를 path·id → 정의로. 못 읽으면 빈 dict."""
    value = db.get(key)
    try:
        parsed: object = json.loads(value) if isinstance(value, str) else {}
    except ValueError:
        return {}
    return cast(dict[str, dict[str, object]], parsed) if isinstance(parsed, dict) else {}


def judge_tree(snap: dict[str, object], want: TreeExpect) -> dict[str, bool]:
    """번들 트리 `want`가 적용된 상태인가: `GET /project`의 version·content_hash가 그 트리이고
    드래프트가 없으며(`site_rev == base_site_rev`), DB `project_files` 행 수 = 트리 파일 수,
    retire되지 않은 장비·태그·알람이 트리와 같고, c0 시드 장비·태그(`SEED_DEVICE`·`SEED_TAG`, 트리
    밖)가 retire됐으며(`seed_retired`), 태그 `want.tree_tag`의 DB 정의 값(description)이 그 트리
    값이다(`definition`). 시드 retire·정의 값은 core가 트리를 실제로 다시 적용했을 때만 참이다 —
    같은 트리 재적용은 no-op이라(H1) c0 DB가 그대로 남는다."""
    body, db = project_body(snap), db_of(snap)
    rev = revs(body)
    devices, tags = names(db, "device_ids"), names(db, "tag_paths")
    tag_def = definitions(db, "tag_defs").get(want.tree_tag, {})
    return {
        "project_version": body.get("version") == PROJECT_VERSION
        and body.get("content_hash") == want.content_hash,
        "base_site_rev": rev is not None and rev[0] == rev[1],
        "project_files": as_int(db.get("project_files")) == want.files,
        "devices": devices == want.devices,
        "tags": tags == want.tags,
        "alarms": names(db, "alarm_ids") == want.alarms,
        "seed_retired": bool(devices)
        and bool(tags)
        and SEED_DEVICE not in devices
        and SEED_TAG not in tags,
        "definition": want.tree_tag_description is not None
        and tag_def.get("description") == want.tree_tag_description,
    }


def patch_kept(db: dict[str, object]) -> bool:
    """DB 정의에 ⓑ의 PATCH 값이 그대로 있는가(태그·알람 둘 다)."""
    tag = definitions(db, "tag_defs").get(PATCH_TAG, {})
    alarm = definitions(db, "alarm_defs").get(PATCH_ALARM, {})
    return all(tag.get(k) == v for k, v in PATCH_TAG_BODY.items()) and all(
        alarm.get(k) == v for k, v in PATCH_ALARM_BODY.items()
    )


def reply(out: dict[str, object], key: str) -> tuple[int, object]:
    """`site_api.py` 출력의 `[상태, 본문]`, 없으면 (0, None)."""
    value = out.get(key)
    if isinstance(value, list):
        pair = cast(list[object], value)
        if len(pair) == 2 and isinstance(pair[0], int):
            return pair[0], pair[1]
    return 0, None


def judge_patch(out: dict[str, object], db: dict[str, object]) -> dict[str, bool]:
    """ⓑ 현장 PATCH: web 경유 로그인(세션 쿠키) → 태그·알람 PATCH가 각각 200이고 site_rev를 하나씩
    올리며, 끝난 뒤 site_rev = 전 + 2·base_site_rev 그대로(흡수 전 드래프트), DB 정의 = PATCH 값."""
    before = revs(reply(out, "project_before")[1])
    after = revs(reply(out, "project_after")[1])
    login, _ = reply(out, "login")
    rev0 = before[0] if before is not None else -1

    def patched(key: str, want_rev: int) -> bool:
        status, body = reply(out, key)
        site_rev = cast(dict[str, object], body).get("site_rev") if isinstance(body, dict) else None
        return status == 200 and site_rev == want_rev

    return {
        "login": login == 200 and out.get("session_cookie") is True,
        "tag_patch": patched("tag", rev0 + 1),
        "alarm_patch": patched("alarm", rev0 + PATCHES),
        "site_rev": before is not None
        and after is not None
        and after == (before[0] + PATCHES, before[1]),
        "readback": patch_kept(db),
    }


def judge_absorb(result: dict[str, object], want_rev: int, site_id: str) -> dict[str, bool]:
    """ⓓ WSL 단계: `absorb` exit 0이고 `absorbs[site]`를 서명된 site_rev로 기록, `gate --draft`
    통과."""
    absorb = cast(dict[str, object], result.get("absorb") or {})
    gate = cast(dict[str, object], result.get("gate") or {})
    lines = str(absorb.get("stdout") or "").splitlines()
    return {
        "absorb": absorb.get("rc") == 0,
        "absorbs_rev": any(
            line.startswith(f"absorbs[{site_id}]:") and line.endswith(f"-> {want_rev}")
            for line in lines
        ),
        "gate": gate.get("rc") == 0,
    }


def absorbed_hash(result: dict[str, object]) -> str | None:
    absorb = cast(dict[str, object], result.get("absorb") or {})
    found = ABSORBED_HASH.search(str(absorb.get("stdout") or ""))
    return found[1] if found else None


def judge_reapplied(
    snap: dict[str, object], want_rev: int, content_hash: str | None
) -> dict[str, bool]:
    """ⓓ 재적용 뒤: 흡수한 트리가 적용됐고(content_hash) 드래프트 기준점이 PATCH 뒤 site_rev로
    옮겨졌으며(`base_site_rev == site_rev == want_rev`) PATCH 값이 DB 정의에 남았다."""
    body = project_body(snap)
    return {
        "content_hash": content_hash is not None and body.get("content_hash") == content_hash,
        "base_site_rev": revs(body) == (want_rev, want_rev),
        "patch_kept": patch_kept(db_of(snap)),
    }


# ---------------------------------------------------------------- 온라인(중앙 `/fleet`)


def fleet_row(reply: dict[str, object], site_id: str) -> dict[str, object]:
    """`central.py fleet` 응답에서 `site_id` 행. 200이 아니거나 행이 정확히 하나가 아니면 빈
    dict."""
    body = reply.get("body")
    if reply.get("status") != 200 or not isinstance(body, list):
        return {}
    rows = [
        cast(dict[str, object], r)
        for r in cast(list[object], body)
        if isinstance(r, dict) and cast(dict[str, object], r).get("site") == site_id
    ]
    return rows[0] if len(rows) == 1 else {}


def sub(row: dict[str, object], key: str) -> dict[str, object]:
    value = row.get(key)
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def seen_at(row: dict[str, object]) -> datetime | None:
    """`/fleet` 행의 `last_seen`(중앙 수신 시각, ISO 8601). 없거나 못 읽으면 None."""
    value = row.get("last_seen")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def seen_after(row: dict[str, object], earlier: dict[str, object]) -> bool:
    later, before = seen_at(row), seen_at(earlier)
    return later is not None and before is not None and later > before


def ca_fingerprint(openssl_out: str) -> str:
    """`openssl x509 -noout -fingerprint -sha256` 출력(`sha256 Fingerprint=AB:…`)의 값."""
    _, _, value = openssl_out.strip().partition("=")
    return value.strip().upper()


def judge_online_poll(
    row: dict[str, object],
    *,
    ca_sha256: str,
    vm_ca_sha256: str,
    site_id: object,
    want_site: str,
    pair: ReleasePair,
) -> dict[str, bool]:
    """① HTTPS polling·heartbeat: VM이 설치한 CA가 중앙에서 내보낸 것과 같고(지문 대조, D8),
    등록된 현장(`site_id`)이 `want_site`이며 그 heartbeat가 들어와 `/fleet`이 online이고 heartbeat
    수위·current(g1 = c0의 BASE)가 찼다."""
    current = sub(row, "current")
    return {
        "ca_fingerprint": bool(ca_sha256) and vm_ca_sha256 == ca_sha256,
        "registered": site_id == want_site,
        "connection_online": row.get("connection") == "online",
        "heartbeat_seq": as_int(row.get("last_heartbeat_seq")) > 0,
        "current_g1": current.get("gen") == 1
        and current.get("product") == pair.releases[pair.base],
    }


def judge_online_published(
    put: dict[str, object], row: dict[str, object], pair: ReleasePair
) -> dict[str, bool]:
    """② 게시: ci 토큰 `PUT /bundles/{site}/{ONLINE_GEN}` 201, `/fleet` desired = 그 gen·BASE."""
    desired = sub(row, "desired")
    return {
        "put_201": put.get("status") == 201,
        "desired_gen": desired.get("generation") == ONLINE_GEN
        and row.get("max_published_gen") == ONLINE_GEN,
        "desired_product": desired.get("product_version") == pair.releases[pair.base],
    }


def judge_online_report(row: dict[str, object], pair: ReleasePair) -> dict[str, bool]:
    """③ 온라인 보고: `/fleet` current = 적용한 g2(BASE·v1)이고 source online, 마지막 ApplyAttempt =
    g2 succeeded(source online), 연결 online."""
    current, attempt = sub(row, "current"), sub(row, "last_attempt")
    return {
        "current": current.get("gen") == ONLINE_GEN
        and current.get("product") == pair.releases[pair.base]
        and current.get("project_version") == PROJECT_VERSION,
        "current_source": current.get("source") == "online",
        "last_attempt": attempt.get("generation") == ONLINE_GEN
        and attempt.get("result") == "succeeded",
        "attempt_source": attempt.get("source") == "online",
        "connection_online": row.get("connection") == "online",
    }


def judge_outage(
    during: dict[str, object],
    *,
    want_digests: dict[str, str],
    stale: dict[str, object],
    back: dict[str, object],
    updater_active: bool,
    restarts: tuple[int, int],
    core_started: tuple[object, object],
) -> dict[str, bool]:
    """④ 중앙(control-web·Caddy) 중지: 현장은 그대로 — core ready·실행 이미지·core `StartedAt` 불변,
    Updater 서비스 active·재시작 없음·경보 없음·새 시도 없음(g2 succeeded 그대로). 3 x poll 뒤
    control-api가 직접 본 `/fleet`은 stale(current는 마지막 보고값 g2), control-web 재기동 뒤 다시
    online이고 `last_seen`이 stale 때보다 뒤다(heartbeat 재개)."""
    state = updater_state(during)
    return {
        "site_ready": ready(during),
        "images_match": running_digests(during) == want_digests,
        "core_not_restarted": core_started[0] is not None and core_started[0] == core_started[1],
        "updater_active": updater_active,
        "updater_not_restarted": restarts[0] >= 0 and restarts[0] == restarts[1],
        "no_alarm": state.get("alarm") is None,
        "no_new_attempt": state.get("attempt") is None
        and state.get("applied_gen") == ONLINE_GEN
        and state.get("last_result") == "succeeded",
        "stale": stale.get("connection") == "stale"
        and sub(stale, "current").get("gen") == ONLINE_GEN,
        "back_online": back.get("connection") == "online" and seen_after(back, stale),
    }


def judge_window_flake(checks: dict[str, bool], stage: dict[str, object]) -> bool:
    """crash 건이 창 여유 부족 flake(E10, `infra`)인가: CLI 스테이징은 성공했지만(`rc` 0) 창이 열린
    뒤에 끝났고(`margin_s <= 0`) 그 밖의 판정(제품 동작)은 모두 참이다. 다른 판정이 하나라도
    거짓이면 제품 실패다 — 다시 하지 않는다. CLI 대상(`fetched`)은 창 판정이 없어 늘 거짓이다.
    창 판정 자체가 거짓이어야 한다 — 기록된 `margin_s`는 반올림 값이라 실제 여유가 0~0.05초면
    판정은 참인데 0.0으로 남는다(리뷰 L1)."""
    margin = stage.get("margin_s")
    if "staged_before_window" not in checks or not isinstance(margin, int | float):
        return False
    product = [ok for name, ok in checks.items() if name != "staged_before_window"]
    late = checks["staged_before_window"] is False
    return late and margin <= 0 and stage.get("rc") == 0 and all(product)


# ---------------------------------------------------------------- G2-1 설치·100태그(M6-2)


def judge_installed(snap: dict[str, object], want_digests: dict[str, str]) -> dict[str, bool]:
    """설치 직후: `/readyz` 200, 실행 이미지 `.Id` == 기대 release config digest, overlay2 + classic
    image store."""
    docker = cast(dict[str, object], snap.get("docker") or {})
    return {
        "ready": ready(snap),
        "images_match": running_digests(snap) == want_digests,
        "overlay2": docker.get("driver") == "overlay2",
        "classic_store": "containerd" not in json.dumps(docker.get("driver_status")),
    }


def api_rows(body: object, key: str) -> list[dict[str, object]]:
    """`GET /tags`·`GET /devices` 본문의 `key` 행 목록(형식이 다르면 빈 목록)."""
    rows = cast(dict[str, object], body).get(key) if isinstance(body, dict) else None
    items = cast(list[object], rows) if isinstance(rows, list) else []
    return [cast(dict[str, object], r) for r in items if isinstance(r, dict)]


def tag_rows(body: object) -> list[dict[str, object]]:
    return api_rows(body, "tags")


def judge_tags_good(body: object, want_paths: frozenset[str]) -> dict[str, bool]:
    """`want_paths`(G2 트리의 시뮬레이터 태그) 전부가 `GET /tags`에 있고 품질이 GOOD이며 값이
    있다. 빈 기대 집합은 거짓이다 — 아무것도 확인하지 않는 통과가 되지 않게."""
    quality = {str(r.get("path")): r for r in tag_rows(body)}
    present = bool(want_paths) and want_paths <= quality.keys()
    good = present and all(
        quality[p].get("quality") == "GOOD" and quality[p].get("value") is not None
        for p in want_paths
    )
    return {"tags_present": present, "tags_good": good}


def tags_not_good(body: object, want_paths: frozenset[str]) -> list[str]:
    """GOOD이 아니거나 없는 기대 태그(결과 기록용, 정렬)."""
    quality = {str(r.get("path")): r.get("quality") for r in tag_rows(body)}
    return sorted(p for p in want_paths if quality.get(p) != "GOOD")


def judge_device_connected(body: object, device_id: str) -> dict[str, bool]:
    """`GET /api/v1/devices`의 `device_id`가 core 판정 CONNECTED·collector health connected·무신호
    아님(장비 상태는 태그가 아니다 — 정본은 이 API)."""
    found = [r for r in api_rows(body, "devices") if r.get("device_id") == device_id]
    device = found[0] if len(found) == 1 else {}
    return {
        "device_connected": device.get("state") == "CONNECTED"
        and device.get("connected") is True
        and device.get("silent") is False
    }


ROW_KEYS = ("users_rows", "audit_log_rows", "alarm_journal_rows", "tag_history_rows")
"""probe 행 수 4종 — 롤백·실패한 시도 뒤 "≥ 이전"으로 본다(users·감사·ACK 기록·이력)."""


def judge_c0_base(db: dict[str, object], epoch: int) -> dict[str, bool]:
    """G2 c0 기반(M6-3 F5): 이력 시드(`site.seed_history`) 뒤 압축 chunk와 `tag_history` 행이 있고
    DB epoch가 그 현장 release의 epoch다 — 업그레이드가 압축 DB 위에서 돌고 행 수 보존을 잴 수
    있어야 한다."""
    return {
        "c0_compressed_chunks": as_int(db.get("compressed_chunks")) > 0,
        "c0_seed_rows": as_int(db.get("tag_history_rows")) > 0,
        "c0_epoch": db.get("epoch") == str(epoch),
    }


def judge_live(
    out: dict[str, object], want_paths: frozenset[str], device_id: str
) -> dict[str, bool]:
    """`site_api.py live` 출력: `GET /tags`·`GET /devices`가 200이고 시뮬레이터 태그 전부 GOOD,
    장비 연결."""
    tags_status, tags = reply(out, "tags")
    devices_status, devices = reply(out, "devices")
    return {
        "api_ok": tags_status == 200 and devices_status == 200,
        **judge_tags_good(tags, want_paths),
        **judge_device_connected(devices, device_id),
    }
