---
id: sot-data-contracts
title: Data Contracts SOT — DB·설정 스키마
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/db/migrations
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/configs
  - projects/demo-coffee
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/tags/models.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/gateway.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/project_package.py
source_globs:
  - projects/demo-coffee/**/*.yaml
  - projects/demo-coffee/**/*.json
last_verified: 2026-08-22
---

# Data Contracts SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다.

PureIron의 DB 스키마와 YAML/JSON 설정 파일 계약이다.

정본은 실제 마이그레이션 SQL, 선택 가능한 project config root(`configs/**`, `projects/demo-coffee/**`), 그리고 관련 Pydantic 모델이다. 이 문서는 세부 스키마 전체를 복사하지 않고, 깨지면 안 되는 계약과 파일 경계를 정리한다.

## 이 문서의 원천

- [backend/src/pureiron/db/migrations](https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/db/migrations) — boot 시 알파벳순 자동 실행되는 멱등 SQL
- [configs](https://github.com/circulation95/PureIron_ignition/tree/legacy-final/configs) — Coffee 데이터가 없는 최소 기본 project root
- [projects/demo-coffee](../../../projects/demo-coffee) — Coffee 영업/demo project root
- [backend/src/pureiron/tags/models.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/tags/models.py) — Tag/TagDefinition/TagSourceKind 모델
- [backend/src/pureiron/gateway.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/gateway.py) — DB-first tag loading, JSON fallback, seed 정책

## 마이그레이션 규율

- `backend/src/pureiron/db/migrations/*.sql`을 boot 시 **파일명 알파벳순**으로 자동 실행한다.
- 모든 마이그레이션은 멱등이어야 한다(`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`, 보수적 cleanup 등).
- 앱 코드에서 `ALTER TABLE`을 직접 실행하지 않는다. 스키마 변경은 마이그레이션 파일로만 한다.
- 자동 down migration은 없다. forward-only를 기본값으로 한다.
- 현재 마이그레이션은 `001_*`부터 `027_*`까지 존재한다(파일명 prefix 기준 최대 번호 = `027`).
- 같은 번호 prefix 가 의도적으로 중복된 파일이 있다:
  - `013_alarm_per_tag_uniqueness.sql`, `013_create_udt_tables.sql`
  - `025_add_tag_source_and_history_tagid.sql`, `025_demo_device_status_intent.sql`

  이는 알려진 기존 중복이며 정상으로 간주한다. 알파벳순 실행이므로 같은 prefix 안에서는 파일명 뒷부분이 실행 순서를 결정한다.
- 새 마이그레이션은 번호를 재사용하지 말고 **항상 현재 최대 번호 +1**, 즉 다음 번호(`028_*`)부터 사용한다.
- `scripts/wiki.py check` 는 migration prefix 중복을 경고(warn-only)한다. 위 기존 중복(`013`, `025`)은 allowlist 로 통과시키고, 새로 생긴 중복만 경고한다. allowlist 정본은 `scripts/wiki.py` 의 `MIGRATION_DUP_ALLOWLIST` 이다.

결정 근거는 [adr](../adr/index.md)를 따른다.

## Tag 정의 계약

`TagDefinition`은 DB row ownership을 `source_kind`로 구분한다.

| Enum | DB value | 출처 | 소유 identity |
|---|---|---|---|
| `AUTHORED` | `authored` | 사용자가 REST API로 직접 생성 | 없음 |
| `UDT_DERIVED` | `udt` | UDT 인스턴스 expansion | `source_instance_id` + `source_member_path` |
| `DRIVER_DERIVED` | `driver` | 드라이버가 디바이스 설정에서 자동 생성 | `source_device_id` + `source_member_path` |

규칙:

- 모든 in-memory `Tag`는 가능한 한 DB row와 1:1로 대응한다.
- `Tag.current_value`는 런타임 값이며 휘발성이다.
- 정의(definition)는 DB가 단일 진실 소스다.
- JSON fallback 모드에서는 정의 CRUD가 read-only로 제한될 수 있지만 값 write는 계속 허용한다.
- Pydantic 모델에서는 `TagDefinition.tag_config`, DB column에서는 `provider_config`로 표기한다. 둘은 같은 데이터를 가리킨다.

분류와 런타임 흐름은 [architecture](architecture.md#tagsourcekind-taxonomy)를 따른다.

## Approved tag collection contract

driver-derived tag가 historian/trend/agent context로 흘러가려면 **승인 수집 정책(approved collection policy)** 을 통과해야 한다. 기존 `excluded_paths.json`/`is_driver_excluded`는 "수집하면 안 되는 것만 빼는" deny 모델이고, 이 계약은 그 위에 "승인된 것만 흘린다"는 positive allowlist를 얹는다. 코드 정본은 [collection_policy.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/tags/collection_policy.py)이며, 이 절과 precedence가 일치해야 한다.

정책은 값을 실제로 저장하지 않는다. 정책을 통과하면 태그가 수집 **대상(eligible)** 이 되고, 특정 값 row가 `tag_history`에 실제로 기록되는지는 여전히 per-tag `history_enabled` 메타데이터가 결정한다([historian/writer.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/historian/writer.py)).

**Precedence (강한 순서부터):**

| 순위 | 조건 | 결과 | outcome |
|---|---|---|---|
| 1 | path가 excluded set에 있음 | **hard DENY** (승인보다 강함) | `excluded` |
| 2a | `DRIVER_DERIVED` + allowlist 미구성(`None`) | ALLOW (legacy permissive) | `unmanaged` |
| 2b | `DRIVER_DERIVED` + allowlist에 path 있음 | ALLOW | `approved` |
| 2c | `DRIVER_DERIVED` + allowlist 구성됐으나 path 없음 | **DENY (default-deny)** | `not-approved` |
| 3 | `AUTHORED` / `UDT_DERIVED` | ALLOW (작성자 의도가 지배, allowlist 무관) | `authored` |

**authored vs driver-derived 정책 차이:**

- **authored / UDT-derived** 태그는 사람(또는 UDT 인스턴스 작성)이 이미 수집 의도를 명시한 것이므로 driver allowlist를 적용하지 않는다. 수집 여부는 태그 자신의 `history_enabled` 메타데이터로 결정한다. 단, excluded는 여전히 이들도 막는다.
- **driver-derived** 태그는 디바이스 설정에서 자동 생성되므로 무단 수집을 막기 위해 allowlist를 통과해야 한다. allowlist가 구성되면 미등록 태그는 default-deny다. allowlist 미구성(`None`)은 하위호환을 위한 legacy permissive이며, 빈 set(`[]`)은 "driver-derived 전부 거부"라는 명시적 구성이다.

**allowlist 구성:** `approved_paths`는 driver 생성자 인자(set) 또는 `load_approved_collection_paths()`가 읽는 선택적 JSON list(`configs/tags/approved_paths.json` 관례)로 공급한다. v1에서는 승인 관리 UI/워크플로우를 만들지 않는다(config + testable contract). **gateway 배선 완료:** `ScadaGateway._setup_drivers()`가 startup에서 `load_approved_collection_paths(CONFIGS_DIR / "tags" / "approved_paths.json")`를 한 번 읽어 Modbus driver 생성자에 `approved_tag_paths`로 전달한다(파일 없음→`None` legacy permissive, 빈 list→configured deny-all). 나머지 프로토콜 드라이버(OPC-UA·S7·LS·AB·MELSEC)는 아직 생성자에서 `approved_tag_paths`를 받지 않으므로 자동 배선은 후속 범위다.

계약 회귀는 [test_collection_policy_contract.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_collection_policy_contract.py)(순수 unit), [test_approved_tag_collection_harness.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_approved_tag_collection_harness.py)(simulator→store→historian), [test_gateway_approved_paths_wiring.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_gateway_approved_paths_wiring.py)(gateway startup 배선)가 막는다.

## Backend mode (`postgres` / `json`)

`gateway.tag_storage_backend`는 boot 시 결정된다.

| mode | 조건 | 정의 CRUD | 값 write | 목적 |
|---|---|---|---|---|
| `postgres` | DB 사용 가능 | DB로 영속화 | 허용 | 정상 운영 |
| `json` | DB 사용 불가 또는 tag load 실패 | 503/read-only | 허용 | 핵심 모니터링 유지 |

`pureiron_tag_storage_backend{value="postgres|json"}` metric으로 현재 모드를 노출한다.

`PUREIRON_SEED_TAGS_FROM_JSON=true`일 때만 DB가 비어 있는 경우 JSON tag 정의를 DB로 seed한다. 이 변수는 `Settings` 필드가 아니라 `gateway.py`에서 직접 읽는다.

## Historian retention & query window

`tag_history` hypertable의 이력 수집/조회 계약이다. 정본은 마이그레이션과 `historian/reader.py`이며, 이 절은 특히 read-only PoC 리뷰 흐름에서 깨지면 안 되는 계약을 고정한다.

**Retention (PoC 기준):**

- `027_add_tag_history_retention.sql`가 `tag_history`에 **30일 drop-after retention policy**를 멱등(`if_not_exists => TRUE`)으로 건다. 2주 read-only PoC 리뷰 창 + 후속 리뷰 여유를 덮으면서 디스크 무한 증가를 막는다(runbook의 history disk growth 경고 대응).
- TimescaleDB retention 백그라운드 워커가 없으면 마이그레이션 러너가 경고만 남기고 계속한다(graceful degradation). 이때도 조회 경로는 정상 동작한다.
- 특정 고객/SI PoC의 retention 조정은 앱 코드가 아니라 배포 결정이다. 조정/확인 SQL은 [on-prem read-only PoC runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/on-prem-read-only-poc.md) §8을 따른다.

**Query window / auto-resolution (`historian/reader.py`):**

조회 기간 길이에 따라 자동 다운샘플링 해상도를 고른다. Trend/history 리뷰 쿼리를 bounded 상태로 유지하는 계약이다.

| 조회 기간 | Resolution | 비고 |
|---|---|---|
| `< 1h` | RAW | 원본 그대로 |
| `≤ 24h` | 1min 평균 | `time_bucket` |
| `≤ 30d` | 5min 평균 | 2주 PoC 리뷰 창이 여기에 해당 |
| `> 30d` | 1hour 평균 | |

- `max_points` 기본값 10,000(라우터 상한 100,000)으로 응답 크기를 제한한다.
- `end < start` 조회는 `ValueError`로 거부한다(DB 접근 전 guard).
- 조회는 `tag_id` 우선, `tag_id`가 NULL인 pre-backfill 행은 `tag_path` fallback. 태그가 `tag_definitions`에서 삭제되면 `tag_path`만으로 forensic 조회한다.
- retention/query 경로는 read-only PoC-safe mode와 무관하다. read-only mode는 operator/agent write-control만 차단하며 이력 조회는 정상이다.

## Project config root 계약

`PUREIRON_CONFIGS_DIR`는 한 배포 인스턴스에서 활성화할 **단일 project config root**를 가리킨다. 실행 코드나 plugin을 동적으로 로드하는 registry가 아니며, 서버 재시작 없이 project를 전환하지 않는다.

- 저장소의 `configs/`는 Coffee 데이터가 없는 최소 기본 root다.
- Coffee 영업/demo 입력은 `projects/demo-coffee/`에 격리한다.
- Docker 개발 compose는 기존 demo UX 보존을 위해 `projects/demo-coffee/`를 `/app/configs`에 명시적으로 mount한다.
- 고객 배포는 고객 package 경로를 `/app/configs`에 mount하고 `PUREIRON_CONFIGS_DIR=/app/configs`로 선택한다.
- 선택한 root에 `project.json`이 없거나 `features.coffee_demo`가 정확히 `true`가 아니면 Coffee simulator와 Coffee Agent shortcut은 활성화하지 않는다(fail closed).

| 경로 | 내용 |
|---|---|
| `project.json` | schema/project 식별자와 built-in demo feature opt-in |
| `connections/` | OPC-UA, Modbus, S7 등 디바이스 엔드포인트 |
| `tags/` | JSON/YAML tag 정의, excluded path 목록(deny), 선택적 approved allowlist(`approved_paths.json`, [approved tag collection contract](#approved-tag-collection-contract)) |
| `alarms/` | 알람 설정점 |
| `udt/types/` | UDT 타입 JSON 파일 |
| `udt/instances.*` | UDT 인스턴스 선언 |
| `auth/` | 사용자/auth seed |
| `settings/` | 사용자 설정 seed |
| `dashboard/` | 대시보드 seed |

화면 package 계약은 후속 Phase에서 schema와 runtime consumer를 함께 추가한다. loader가 없는 legacy 화면 YAML은 package에 보존하지 않는다.

### Project package export/import 계약

`backend/scripts/project_package.py`는 선택된 config root 하나를 버전형 `.pipkg` ZIP으로 이동한다. 이는 동적 multi-project runtime이나 DB backup 기능이 아니다.

- package는 `project.json`, connection/device seed template, tag 정의·path policy, alarm 정의, UDT type/instance, shared `dashboard/process_dashboards.json`, user identity metadata를 allowlist로 포함한다.
- 현재 runtime consumer가 직접 읽는 canonical artifact는 `tags/tags.json`, tag path policy, `alarms/*.yaml`, `udt/types/*.json`, `udt/instances.json`, shared process dashboard다. loader가 무시할 JSON/YML alarm과 legacy UDT YAML은 package에 넣지 않는다.
- `connections/**`, `tags/*_devices.json`, `tags/*_tags.json`은 redacted seed/reference artifact다. 현재 PostgreSQL `devices`/`tag_definitions` catalog에 자동 적용하거나 driver를 시작하지 않는다.
- 이 v1 config package는 PostgreSQL catalog backup이 아니다. DB-backed target은 격리된 빈 DB와 기존 explicit seed 정책을 별도로 사용해야 하며, 기존 DB row를 import로 overwrite하지 않는다.
- React process screen 코드는 배포 artifact이므로 포함하지 않는다. 개인 `user_dashboards.json`, profile/settings, script·SQL·CSV·`.env`, historian·alarm journal·운영 로그도 제외한다.
- tag의 `value`, `quality`, `timestamp`, `last_changed`는 runtime sample로 보고 제거한다.
- connection/device seed는 credential/secret/username 및 contact PII 계열 key와 URI userinfo를 재귀적으로 `[REDACTED]` 처리한다.
- user는 `username`, `role`, `display_name`, `enabled`만 package에 기록한다. source `hashed_password`와 개인 정보는 절대 포함하지 않는다.
- manifest는 `package_format_version`, scope/exclusions, project metadata, artifact size·SHA-256, redaction count를 가진다. archive entry와 checksum 전체를 import 전에 검증한다.
- ZIP timestamp·entry order·직렬화는 고정하여 동일 source가 동일 byte package를 만든다.
- import target은 absent 또는 empty여야 한다. staging root에서 검증·기록한 뒤 atomic promotion하며 기존 non-empty target을 덮어쓰지 않는다.
- import된 identity는 fresh random password hash를 받고 기본 disabled다. 단 하나의 packaged Admin만 archive 밖 환경변수로 전달한 새 password를 사용해 명시적으로 bootstrap할 수 있다.
- zip-slip, duplicate/undeclared entry, symlink, malformed JSON/YAML, unsupported version, size/checksum mismatch는 target 기록 전에 거부한다.

Repo root 기준 사용 예:

```bash
cd backend
uv run python scripts/project_package.py export \
  --source ../projects/demo-coffee --output /tmp/demo-coffee.pipkg
uv run python scripts/project_package.py inspect --package /tmp/demo-coffee.pipkg
uv run python scripts/project_package.py import \
  --package /tmp/demo-coffee.pipkg --target /tmp/demo-coffee-imported
```

활성 root는 `--source`를 생략하면 `PUREIRON_CONFIGS_DIR`(없으면 `configs`)를 사용한다. bootstrap이 필요하면 password 값을 CLI 인자로 노출하지 않고 `--bootstrap-username`과 `--bootstrap-password-env`를 함께 사용한다.

이 표는 디렉터리 경계를 설명한다. 각 파일의 상세 schema는 코드의 loader/model이 정본이다.

## 문서화 원칙

- 세부 column 목록을 이 문서에 길게 복사하지 않는다. migration SQL을 정본으로 둔다.
- 설정 파일 예시는 `configs/**`에 두고, 문서는 경계와 주의사항만 유지한다.
- `source_globs`가 가리키는 파일 집합이 비면 `scripts/wiki.py check`가 경고한다.
- secret/API key 값은 이 문서에 쓰지 않는다.

## 목표 설계(미구현)

> **target design — 아직 구현되지 않았다.** 이 절은 Foundation 재설계(2026-10-04)로 PureIron_V1에 만들 구조다. 위 절들의 현행 사실(legacy 기준)과 섞어 읽지 않는다. 결정 근거는 [ADR](../adr/index.md) 019~028. 구현되면 해당 내용을 현행 절로 옮기고 이 절에서 지운다.

### 마이그레이션·스키마 호환 (ADR-024)

- 부팅 시 자동 실행 없음. `pureiron-core migrate status|up|verify|lint`, 이력 `schema_migrations(version, name, checksum, applied_at, release)`, `pg_advisory_lock`, 파일 단위 트랜잭션, 실패 즉시 중단.
- ADD-only lint 거부 목록: DROP/RENAME/`ALTER … TYPE`/TRUNCATE, `ADD CONSTRAINT`, NOT NULL 무기본값, `CREATE UNIQUE INDEX`, `CREATE TRIGGER`, `refresh_continuous_aggregate` 호출, `WITH NO DATA` 없는 CAGG.
- `migrate lint --base-ref`의 epoch 항목 불변 기준은 base의 release 기록(`deploy/releases/*.json` `schema.app_expected` 최댓값)이다. 그 이하 epoch는 바꿀 수 없고, 아직 release되지 않은 epoch는 마지막 파일을 뒤 파일로 늘리는 것만 허용한다(r2 = epoch 3 = `0008` → `0009`: r1 앱 envelope 상한이 3이라 r2는 epoch 하나만 올려야 롤백 가능). base에 있는 SQL 파일과 release 기록(`deploy/releases/*.json`)의 수정·삭제는 epoch와 무관하게 금지, 읽을 수 없는 기록은 lint 실패, release 기록이 없는 base는 모든 epoch를 release된 것으로 본다. CAGG 실시간 집계 토글(`ALTER MATERIALIZED VIEW … SET (timescaledb.materialized_only = …)`) 한 형태만 ALTER MATERIALIZED VIEW로 허용한다.
- epoch: 마이그레이션을 포함한 release만 +1. 앱 ready 조건 `app_expected ≤ 현재 epoch ≤ app_max_compatible(= expected + 1)`. 1 epoch 초과 점프는 `E_SCHEMA`(단계 적용).
- baseline 스키마: `schema_meta`, `tags`(`id`, `path unique` — path 기준 안정 id), `tag_history`(1일 chunk hypertable, `UNIQUE(tag_id, time)`, 2일 후 압축), 1분 CAGG, `project_state`(`project_id, version, content_hash, base_site_rev, site_rev, edit_lock_owner, edit_lock_gen`). epoch 2(r1)에서 1시간 CAGG `tag_history_1h`를 더했다(아래 CAGG 조회 계약). epoch 5(미release, r4 예정)는 `0013_project_files`(아래 M5-0 절).

### 기록 정책 (ADR-021)

- retention은 프로젝트 설정 `historian.retention_days`(기본 365), 태그별 `record.deadband`·`record.max_interval_s`(기본값은 `historian.default_*`).

### 프로젝트 정본 (ADR-025)

```yaml
# 고객 repo pureiron-project-<id> / project.yaml
schema_version: 1
project_id: acme
name: "ACME Line 1"
absorbs: { site-a: 3 }        # 회수한 현장 드래프트 rev (선택)
historian: { retention_days: 365, default_deadband: 0.0, default_max_interval_s: 60 }
# devices/<id>.yaml : { id, protocol: modbus_tcp, host, port, unit_id, poll_ms, timeout_ms, credentials?: "secret://…" }
# tags/<device>.yaml: [{ path, device, modbus:{register_type,address,data_type,byte_order,word_order}, eu?, description?, record?:{deadband,max_interval_s}, write?:{kind,…} }]
# alarms/*.yaml     : [{ id, tag, type: above|below, setpoint, deadband, on_delay_s, priority, description? }]
```

- 정규화: YAML 1.2 strict(중복 키·anchor·커스텀 tag 거부), 숫자 RFC 8785, 기본값 생략, POSIX 상대 경로, UTF-8 NFC, LF, 키 정렬. `content_hash = sha256(정렬된 (경로, 정규화 내용))`, 버전 = 고객 repo 태그 `vN`.
- 현장 드래프트: `PATCH`마다 `site_rev += 1`(리셋 없음, 구현됨 — 아래 M5-2 절), 적용 시 `base_site_rev = site_rev`(구현됨, 아래 M5-0 절). 배포 게이트: `absorbs[site]`가 있으면 `site_rev ≤ absorbs[site]`, 없으면 `site_rev == base_site_rev`, 아니면 `E_DRAFT`(구현됨 — bundle verify ⑥·Updater `locked`·`pureiron-project gate`).
- 회수(구현됨, 아래 M5-3a 절): 서명된 현장 `draft.tar` → 벤더가 고객 repo에서 `pureiron-project absorb`(PATCH 허용 필드만 필드 단위 병합 + `absorbs.<site>` 기록 + 커밋 trailer) → PR은 운영자. 고객 repo 템플릿·검증 workflow는 M5-3b.
- 비밀값은 `secret://<name>` 참조만.
- legacy 변환기 1차 범위: `project.json`, Modbus TCP 연결·태그, 상·하한 알람. OPC-UA·UDT·scripts·dashboard·expression/query 태그·shelve·`auth/users.json`은 **미지원으로 보고**.

### V1 구현 현황 (M5-1 legacy 변환기, 2026-10-25)

- 명령: `pureiron-project convert-legacy <legacy_dir> --out <dir> --report <json>`(`packages/project-tools` `convert_legacy.py`·`legacy_items.py`(장비·태그)·`legacy_alarms.py`·`legacy_values.py`(lax 값·보고서 항목)·`legacy_files.py`). `--out`은 없거나 비어 있어야 한다. 종료 코드 **0** = 미지원 없음, **2** = 트리·보고서는 썼지만 미지원 항목 있음(M5 plan D11), **1** = 읽을 수 없거나 잘못된 입력(`project.json` 없음·형식 오류, 변환 대상 파일의 JSON/YAML 오류·alias 펼침 1,000,000값 초과·너무 깊은 중첩) 또는 비어 있지 않은 `--out` — 출력은 메모리에서 만들어 검증한 뒤에만 쓰므로 이때는 아무것도 쓰지 않는다. argparse 사용법 오류도 2(기존 cli 규칙) — 구분은 stdout: 미지원이면 `converted:` 줄이 있고, 사용법 오류면 stdout이 비고 stderr에 usage가 나온다(보고서 파일 존재로 판단하지 않는다 — 이전 실행의 보고서일 수 있음). YAML 오류 메시지는 줄·열과 문제 종류만 쓴다(원문 줄의 비밀값 비노출).
- 값 해석은 legacy pydantic v2 lax 규칙: bool은 `true/false`·`1/0`·`"yes"/"no"`·`"on"/"off"`·`"t"/"f"`·`"y"/"n"`(대소문자 무시), 정수는 정수 문자열·소수부 없는 수도 받는다. 해석할 수 없는 `zero_based_addressing`·`reverse_word_order`는 연결을 `modbus_connection`으로 보고(추측 금지). NaN·Infinity·±2^53 밖의 수는 변환하지 않고 보고한다. 목록이 아닌 값·mapping이 아닌 항목(`<위치>[n]`)·변환 파일의 읽지 않는 최상위 키(kind `file`)도 보고한다 — 조용히 사라지는 입력은 없다.
- 매핑(legacy 시맨틱은 `legacy-final` `drivers/modbus_config.py`·`modbus_client.py`·`tags/config_loader.py`·`alarms/models.py`):

| legacy | V1 | 규칙 |
|---|---|---|
| `project.json` `project_id`·`name` | `project.yaml` | `project_id`는 V1 id 형식이어야 함(아니면 exit 1). `features.*`는 kind `feature`(`features`가 mapping이 아니면 `field` `features`) |
| `connections/*.{yaml,yml,json}`의 `connections[]`(`protocol: tcp`), 장비 행(`{name, type: "Modbus TCP", hostname, port, config}` — `tags/*.json`에 있어도. `config`가 JSON 문자열이면 legacy gateway처럼 풀어 읽고, NULL(키 없음·SQL NULL)·잘못된 JSON·객체 아님은 `modbus_connection`. jsonb `null`·`[]` 같은 빈 값은 legacy `**(config or {})`처럼 행의 host·port만으로 변환) | `devices/<slug(name)>.yaml`(slug에 쓸 글자가 없는 이름 — 예: 한글만 — 은 입력 순서대로 `device-<n>`. slug 충돌은 `modbus_connection`) | `host`(≤253자)·`port`·`unit_id`, `scan_rate_ms` → `poll_ms`, `timeout_s` → `timeout_ms`(50..2^31−1 ms; 키가 없으면 legacy 기본 3 s → `timeout_ms: 3000` — V1 기본 1000 ms로 조용히 바뀌지 않게). 비밀값 키(legacy `project_package._REDACT_KEY_PATTERN`과 같은 패턴 + camelCase 정규화 — `apiKey`·`location` 포함)는 값을 옮기지 않고 `credentials: secret://<device id>` + 보고서 `secrets[]`. 그 밖 키(`reconnect_delay_s` 등)는 kind `field` |
| 연결 안 `tags:` | `tags/<device>.yaml` | 경로의 `[Provider]` 접두 제거(V1 tagPath로 못 쓰면 미지원 — 비ASCII 경로는 따로 kind `non_ascii_tag_path`로 센다, tagPath 유니코드 확장은 별도 ADR 결정 대기), 주소 그대로(legacy가 0-based로 썼음, `reverse_word_order` 미적용 — legacy `_configured_mappings`), `discrete_input` → `discrete`, `engineering_units` → `eu`. data_type은 legacy `ModbusDataType`(float64 없음) |
| `tags/*.json`(TagStore 저장본, `tag_config` 블록)·`tags/*.{yaml,yml}`(legacy `config_loader` 규칙: 최상위 키, `tag_type` 없으면 경로 `[..]` 안 provider에 `modbus`/`opc`가 있을 때만 Device, `address_config` 없으면 평면 형식 — `protocol`(기본 OPC-UA)이 아니면 holding `opc_item_path` 주소·장비 `opc_server`/`connection`. YAML의 `tag_config` 블록은 legacy가 무시했으므로 `field`)의 Device 태그(`protocol: modbus`, 장비 = 변환된 연결 이름) | 같은 `tags/<device>.yaml` | 주소 1-based → −1(65536까지; 연결 `zero_based_addressing`이 참이면 그대로), `reverse_word_order` → `word_order: little`. `read_only`가 참이 아니면(키 없음 = legacy 기본 쓰기 가능 포함, 쓰기는 `write:` 명시 선언 필요)·`history_enabled: false`·태그별 `scan_rate_ms`(1000 아님)·그 밖 metadata는 kind `field`. `tag_config`(JSON)·`address_config` 안의 legacy 모델(`DeviceTagConfig`·`ModbusAddressConfig`)에 없는 키(태그별 `unit_id` 등 — legacy도 무시)는 `field` `tag_config.<키>`·`address_config.<키>`. `address_config`가 없으면 legacy 기본 OPC-UA, 있는데 `protocol`이 없으면 legacy가 거부 — 둘 다 `opcua_tag`(이유 문구로 구분) |
| `alarms/*.yaml` AboveSetpoint·BelowSetpoint | `alarms/<같은 이름>.yaml` | id = slug(name)(없으면 `alarm-<n>`; 이미 쓰였으면 — legacy 알람 키는 `tag_path::name`이라 태그마다 같은 이름이 정상 — `slug(V1 태그 경로-name)`, 그것도 쓰였으면 `alarm` "duplicate alarm id"), 대상 태그가 변환된 경우만, setpoint는 유한한 수. `Diagnostic` → `low`(+ `field`), 기본값이 아닌 `shelve_duration_s`는 kind `shelve`, `off_delay_s`·`notification_channels`는 `field`, 비활성(`enabled` lax bool 거짓)·`enabled` 해석 불가·`name`이 문자열이 아니거나 없는(legacy `AlarmConfig`가 로드하지 않음) 알람은 미지원 |
| `tags/excluded_paths.json`·`approved_paths.json` | (수집 정책, legacy `collection_policy` 범위) | 연결 안 태그(드라이버 등록)는 제외됐거나 허용 목록 밖이면 kind `excluded_tag`. TagStore 태그는 legacy에서도 폴링됐고 이력만 막혔으므로 변환하고 `field` `history`로 보고(excluded, 또는 `source_kind: driver_derived`인데 허용 목록 밖). 정책 파일이 목록이 아니거나 읽을 수 없으면 legacy처럼 없는 것으로 보고(허용 목록 없음) kind `file` |

- 미지원 kind: `opcua_connection`·`opcua_tag`·`modbus_rtu_connection`·`connection`·`modbus_connection`·`modbus_tag`·`non_ascii_tag_path`·`device`·`device_tag`(S7 등)·`memory_tag`·`expression_tag`·`query_tag`·`tag`(`tag_config` 없는 import 자리표시)·`udt_type`·`udt_instance`·`script`·`dashboard`·`users`·`settings`·`alarm`·`shelve`·`feature`·`field`·`excluded_tag`·`file`(알 수 없는 파일). `.gitkeep`과 `.git/`만 보고하지 않는다.
- 보고서(`format: pureiron-convert-legacy-report`, `version: 1`, 키 정렬 JSON): `summary`(파일·장비·태그·알람·미지원 수, kind별 수), `content_hash`, `converted[]`(legacy 항목 → V1 id/경로), `sources[]`(변환 단계가 읽은 파일), `secrets[]`(파일·항목·필드·참조 — 값 없음), `unsupported[]`(`{file, item, kind, reason, field?}`, 파일 경로 순·파일 안 순서). 입력의 모든 파일은 `sources` 또는 `unsupported[].file`에 나온다. 시각·절대 경로가 없어 같은 입력이면 트리·보고서가 바이트 단위로 같다. `auth/`·`settings/`·사용자별 dashboard는 항목 수만 쓴다(사용자 이름·값 비복사).
- 출력은 정규형(기본값 생략)으로 메모리에서 만들고 `validate`와 같은 규칙(`check_texts`)으로 검증한 뒤에만 쓴다(실패 = 변환기 결함, exit 1, 쓴 것 없음). legacy YAML은 PyYAML과 같은 YAML 1.1 규칙으로 읽는다.
- `projects/demo-coffee` 실측(리뷰 r1·r2 반영 후 재측정, 변화 없음 — 장비에 `timeout_s`가 있어 `timeout_ms: 2000`, 두 번 변환한 트리·보고서 바이트 동일): 장비 1(`line1-plc`)·태그 4(모두 연결 안 태그)·알람 0(알람 19개 대상 `[modbus_test]`·`[Memory]` 태그가 Memory 태그라 미지원), unsupported 2,555건(`device_tag` 1,894·`tag` 489·`udt_instance` 86·`memory_tag` 38·`alarm` 19·`udt_type` 15 등), exit 2. 변환 0.27 s·tracemalloc 최대 11 MB(Windows dev PC) — 2.2 MB `gbrb_tags.json`도 스트리밍 불필요(M5 plan R4).

### 배포 정본·번들 (ADR-022, ADR-023)

```yaml
# pureiron-fleet: sites/site-a.yaml
site: site-a
generation: 17                      # PR마다 +1
product: 2026.11.0
project: { repo: pureiron-project-acme, version: v3 }
window: { recurring: { tz: Asia/Seoul, days: [sat, sun], start: "02:00", end: "05:00" } }
```

- `.pib` manifest 주요 필드: `site_id`, `generation`, `created_at`, `product_release{version, min_updater, images{<svc>{config_digest, tag}}, schema{target, app_expected, app_max_compatible, compat_app_min}}`, `project{project_id, version, content_hash, absorbs}`, `files{경로: sha256}`, `trust_update`, `fleet_commit`.
- 거부 코드: `E_SIG`, `E_CERT`, `E_SITE`, `E_GEN`, `E_DIGEST`, `E_SCHEMA`, `E_UPDATER`, `E_DRAFT`(+ PoC 서명 단계 `E_APPROVAL`).

### V1 구현 현황 (M1, 2026-10-04)

- **계약 파일**(정본): `packages/contracts/collector.proto`, `openapi/{core,control-api}.json`(코드에서 export), `project.schema.json`, `manifest.schema.json`, `fleet-site.schema.json`(JSON Schema 2020-12). 호환성은 `contracts-diff`(buf breaking FILE·oasdiff·`tools/schema_diff.py`), 생성물 동기는 `contracts-drift`.
- **마이그레이션**: `services/core/migrations/0001_schema_meta`~`0005_project_state`(epoch 1 = baseline, lint 면제), `0006_cagg_1h`·`0007_cagg_1h_policy`(epoch 2) + `epochs.yaml`. 이력 `schema_migrations`는 runner가 만들고, 현재 epoch는 `schema_meta('epoch')`에 기록한다 — 구 앱이 자기 카탈로그 없이 envelope를 판정하기 위해서다. 새 마이그레이션 파일은 같은 PR에서 `epochs.yaml`에 epoch를 추가해야 한다(lint). checksum은 줄바꿈을 LF로 정규화한 sha256. 종료 코드 0/2 lock/3 checksum/4 실패/5 lint.
- `-- pureiron:no-transaction` 파일은 구문별 autocommit, 이후 파일(epoch ≥ 2)은 구문 1개·재실행 안전(`IF NOT EXISTS`/`if_not_exists`)이어야 한다.
- baseline 이후 파일의 lint는 **허용 목록** 방식이다: `CREATE TABLE`·(UNIQUE 아닌) `CREATE INDEX`·`CREATE VIEW`·`CREATE MATERIALIZED VIEW`(CAGG는 `WITH NO DATA`)·`CREATE TYPE`·`CREATE EXTENSION IF NOT EXISTS`·`ALTER TYPE … ADD VALUE`·`ALTER TABLE`(액션은 `ADD`(제약·NOT NULL 무기본값 제외)와 `SET (timescaledb.…)`만)·`COMMENT ON`·`SELECT create_hypertable|add_compression_policy|add_continuous_aggregate_policy(…)`·`INSERT … ON CONFLICT DO NOTHING`. 그 밖(DO 블록·함수·EXECUTE·BEGIN/COMMIT·UPDATE/DELETE·OR REPLACE·drop_chunks·retention/remove 정책 등)은 거부. contract 제거는 `-- contract: drops <obj> deprecated in epoch <D>` 주석이 있고 파일 epoch ≥ D+1일 때만 허용.
- `--base-ref`가 알 수 없는 커밋이면 실패한다(검사를 조용히 끄지 않음). base에 이미 있는 최대 version 이하 번호의 새 파일도 거부한다.
- **site-db**: `timescale/timescaledb:2.30.2-pg16`(digest 고정, `deploy/releases/site-db.lock`) → `pureiron/site-db:2.30.2-pg16`. 압축·CAGG 정책이 필요해 `-oss` 변형은 쓰지 않는다.
- **project-tools**(`packages/project-tools`): 위 정규화 규칙 구현. `content_hash = "sha256:" + sha256(JCS([[posix_relpath, 정규 데이터], …]))`(경로 UTF-8 바이트 순, 정규 데이터 = NFC + 기본값 생략). `pureiron-project validate|fmt [--check]|hash`. 변환기·`absorb`는 M5.

### V1 구현 현황 (M2a 번들 서명·검증, 2026-10-04)

- **bundle-tools**(`packages/bundle-tools`, CLI `pureiron-bundle`): 검증 진입점 `verify_bundle` 하나를 CI·Updater·control-api가 공유한다. 모듈·`.pib` 형식·검증 규칙의 정본은 [`packages/bundle-tools/README.md`](../../../packages/bundle-tools/README.md).
- **키 형식**: Ed25519 raw 32B 공개키·raw 64B 서명·지문 `sha256(raw32)`, 개인키 = raw 32B seed(0600). root 서명은 용도별로 분리한다 — `release-cert.json`은 필수 필드 `purpose: release-cert`, `trust_update`는 JCS `{purpose: trust-update, min_cert_serial}`에 서명. 운영 root key는 아직 없다(D10, test key만). 키 보관·복구 절차는 `deploy/keys/README.md`.
- **`.pib`**: 정규형 tar — 멤버 순서·헤더 바이트·블록/레코드(10240B) 0 패딩까지 고정이라 서명된 내용이 같으면 파일 바이트도 같다. 서명 확인 전에는 내용을 해석하지 않고, payload는 해시가 맞은 뒤에만 해석한다. 거부 코드에 형식 위반 `E_FORMAT`을 더했다.
- **release 기록**: `deploy/releases/r<N>.json`에 계약 패턴(`YYYY.M.N`)의 `version` 필드 추가(`release_record.py --version`, r0 = `2026.10.0`) — manifest·fleet의 `product`는 이 버전을 쓰고 이미지 태그는 `pureiron/<svc>:r<N>` 유지. site-db tar도 image store에 보관한다.
- **번들 빌더**: `deploy/bundle/build_bundle.py`(fleet 파일 + release 기록 + image store + 프로젝트 → 서명 `.pib` + `<pib 이름>.bundle-metrics.json`), 쓴 뒤 `--root-pub`로 자체 검증, 1.5 GiB 초과 시 실패(1.2 GiB 경보). r0 실측: 738 MB(site-db 599 MB), 빌드 약 18 s, 검증 약 3 s(WSL2).
- 설치 번들(`kind: install`)은 별도 계약 `install-manifest.schema.json`으로 M2c에서 추가했다(`kind` const 변경은 비호환 변경 — 아래 M2c 절).

### V1 구현 현황 (M2b Updater 상태기계·recover, 2026-10-05)

- **Updater**(`services/updater`, CLI `pureiron-site apply|bundle verify|status|recover|run`): plan M2 phase 표 전체와 `recover()` 수렴 규칙. phase별 동작·실패 경로의 정본은 [`services/updater/README.md`](../../../services/updater/README.md).
- **상태 파일** `state/updater.json`: tmp write → fsync → rename → fsync(dir). 저장된 phase = 실행 중 단계(의도)이고 모든 바깥 호출은 멱등이다.
- **generation 기록**: 커밋(`committed`, 창 안) 전 종료(거부·적재 실패)는 generation을 기록하지 않고 번들 sha256만 건너뛴다(서명 실패 번들의 gen은 믿지 않음). 커밋 후 종료는 `last_attempted_gen`·`last_result`로 남아 같은 gen을 다시 시도하지 않는다.
- **번들 payload 추가**: `compose/site.compose.yml`(서명 대상). Updater는 manifest 이미지 태그로 `.env`를 만들고 `docker compose config`가 해석한 활성 서비스·이미지가 manifest 태그와 정확히 같을 때만 적용한다(`E_DIGEST`).
- **site-db**: 적용 중인 것과 config digest·태그가 다른 번들은 `E_DIGEST`로 거부(db-step release 미구현). 롤백은 site-db를 바꾸지 않는다.
- **편집 잠금**: core HTTP(`/api/v1/project/edit-lock`, web 프록시 경유)로 잡고 푼다. HTTP가 없으면 site-db에서 `psql`로 updater 잠금만 푼다(core 이미지 버전 무관). 풀지 못하면 종료 phase로 남아 다음 tick에 다시 푼다. 커밋된 대기 시도는 새 번들이 검증상 더 높은 generation일 때만, 잠금을 풀고 보고한 뒤 대체된다.
- **일시 장애**: docker 데몬이 없으면 진행·복구하지 않고 기다린다. `migrating`은 `up --wait site-db` 뒤에 실행한다(재부팅 직후 DB 복구 대기).
- **Updater 실패 코드**(ApplyAttempt `code`, 번들 거부 코드와 별개): `E_LOAD`(이미지 적재), `E_BACKUP`, `E_MIGRATE`, `E_SWITCH`, `E_HEALTH`, `E_SUPERSEDED`(커밋된 대기 시도를 새 번들이 대체), `E_BUNDLE`(커밋 후 번들 사본 소실).
- **결과 기록**: `state/reports/*.json`(ApplyAttempt 필드 — 서명·전송은 M2c/M4), `state/metrics/updater.prom`.
- **아직 없음**: GOOD 비율·첫 batch 신호(core 태그 품질 지표 = M3a, 그 전엔 ready + collector 세션만), 서비스 메트릭 60 s 스크랩(M3a), enroll·report export·systemd unit·설치 번들(→ 아래 M2c 절), 실제 docker·VM crash 실측(M2d).

### V1 구현 현황 (M2c 설치 번들·부트스트랩·systemd·enroll, 2026-10-05)

- **설치 번들 계약** `packages/contracts/install-manifest.schema.json`(`kind: install`): `release`, `updater{version,file}`, `docker{packages,compose_min}`, `files`, `trust_update`. `files` 경로 패턴은 현장 manifest와 같아야 한다(계약 테스트). 검증은 bundle-tools `verify_install_bundle` — 현장 번들과 서명(①)·payload(④) 코드를 공유하고, 설치 배치(필수 payload)와 번들 안 `keys/root.{pub,pem}` == 검증 root를 추가로 본다. 종류가 다른 번들은 서로의 검증기에서 `E_FORMAT`.
- **부트스트랩 경로**: 검증 전 추출은 전용 빈 디렉터리에 이름 지정 파일만, `bootstrap-verify.sh`는 그 디렉터리에 다른 파일·symlink가 있으면 멈추고 python을 격리 모드(`-I`)로만 부른다(심어진 모듈 import 차단). `bootstrap-verify.sh`(openssl 1회용: 지문 → PEM raw == root.pub → cert → manifest → 서명된 경로만 추출·sha256) → `preflight.sh` → `install.sh`(daemon.json을 **deb보다 먼저** — postinst의 첫 dockerd 기동부터 classic store, storage driver `overlay2`·compose ≥ 2.17.0 확인 → venv·`current` symlink·root.pub·unit enable → **5a 설치된 Updater로 재검증** → secret 생성, 실패 시 이번 실행분 되돌림). 절차·runbook 값(root 지문·`bootstrap-verify.sh` sha256)의 정본은 [`deploy/install/README.md`](../../../deploy/install/README.md).
- **Updater venv tarball**(PD-5): `deploy/build/build_updater.sh` — ubuntu:24.04에서 `/opt/pureiron/updater/<ver>`, uv.lock export(`--require-hashes`) + 저장소 wheel, 결정적 tar. 서비스 `deploy/systemd/pureiron-updater.service`(`ExecStart=…/current/bin/pureiron-site run`, `EnvironmentFile=/etc/pureiron/updater.env`, `Restart=always`).
- **현장 등록·secret·보고**(PD-3·PD-4): `pureiron-site enroll`(site Ed25519 키 `state/site.key` 0600, `state/site.json` = `report_seq` 카운터 정본, `enrollment.json` = `site-enrollment` 자기 서명 봉투), `--re-enroll`(새 키, seq 유지 — `site.json`까지 잃었으면 `seq_pending`이 되고 벤더 rekey 응답을 `enroll --next-seq N`으로 반영해야 export 가능, 키는 바꾸지 않음), `secret set`(0600, 컨테이너 서비스 uid 10001 소유 — compose 파일 secret은 bind mount), `report export`(저널로 seq를 먼저 예약 → `site-reports` 서명 봉투, 중단 후 재실행해도 같은 seq). 자기 키 검증은 enrollment에서만 허용한다. 봉투 = `{format, purpose, payload, signature}`, 서명 대상 JCS(`{format, payload, purpose}`) — control-api(M4)는 `pureiron_bundle.sitesign`으로 검증한다(P3).
- **설정**: Updater·운영자 CLI 모두 프로세스 환경 + `/etc/pureiron/updater.env`(프로세스 환경 우선). `PUREIRON_SITE_ID`는 `enroll`이 기록한다.
- **M2b 이월 처리**: 대기 시도는 검증상 더 높은 generation만 대체(CLI `apply`도 poll과 같은 규칙, 같은·낮은 gen은 새 번들만 `E_GEN` 보고), 보고 파일명에 번들 sha256 12자(같은 순간 거부 충돌 제거), 성공 뒤 GC(적용 gen·직전 gen의 compose·백업·이미지만 보존), compose 최소 버전 2.17.0(`up --wait-timeout`). 최초 설치 실패 뒤 재시도도 최초 설치로 진행(보호할 직전 상태 없음 — 의도된 동작).
- **아직 없음**: 실제 호스트 설치·네트워크 차단·재부팅·`systemctl kill` crash 실측(M2d, Hyper-V VM), control-api 등록·rekey·보고 수신(M4), 운영 root key(D10, test key만).

### V1 구현 현황 (M2d r1 release, 2026-10-05)

- **r1 = epoch 2**(`deploy/releases/r1.json`, 2026.10.1): `tag_history_1h`(1시간 CAGG, 1분 CAGG와 같은 열 `avg·min·max·last·samples`). 1분 CAGG 위에 계층으로 쌓지 않고 원본 `tag_history`에서 직접 집계한다 — 1시간 refresh가 1분 policy 범위(1일)에 묶이지 않게(압축 chunk도 읽음). 앱 envelope는 [2, 3] — r0 앱(envelope [1, 2])은 epoch 2 DB에서 계속 ready다(롤백 경로).
- **r2 = epoch 3**(`deploy/releases/r2.json`, 2026.10.2, S3): `0008`(장비·태그 정의 jsonb)·`0009`(historian 설정·CAGG 실시간 집계, 아래 M3a-3 절). 앱 envelope는 [3, 4] — r1 앱(envelope [2, 3])은 epoch 3 DB에서 계속 ready다(롤백 경로).
- **r3 = epoch 4**(`deploy/releases/r3.json`, 2026.10.3, S4·M3b 마감): `0010`(사용자·세션)·`0011`(감사 기록)·`0012`(알람 정의·저널). 테이블만 더하고 기존 객체는 바꾸지 않는다. 앱 envelope는 [4, 5] — r2 앱(envelope [3, 4])은 epoch 4 DB에서도 ready 조건(envelope)을 만족한다(롤백 경로). 근거는 g1 n-minus-1: r2 이미지 자신의 `migrate status`가 epoch 4 DB에서 cur=4·app_expected 3·envelope 안·checksum ok·`unknown_applied` [10, 11, 12], 같은 DB에서 r2 core-test 실패 0·waiver 0(새 DB·compose 전용 테스트는 설계상 skip). VM 실측 r2→r3 30/30(run `20261017T132857Z`, [vm-e2e-runs](../reports/vm-e2e-runs.md)): 압축 chunk가 있는 epoch 3 DB에서 25 s, r3′ 헬스 실패 → 135 s 안에 r2 digest 복귀, crash 18회 수렴.
- **CAGG 조회 계약**(1분·1시간 공통):
  - `samples` = bucket의 행 수(`count(*)`, `value_num`이 NULL인 bool·BAD 행 포함)라 `avg(value_num)`의 분모와 다르다. 가중 롤업에 쓰지 않는다(필요하면 다음 epoch에 `count(value_num)` 열을 더한다).
  - `materialized_only`(기본 true)라 policy가 아직 채우지 않은 구간은 원본으로 돌아가 읽지 않는다. 1시간 CAGG의 최신 구간은 `end_offset` 1시간 + 주기 1시간으로 최대 약 2시간 늦다 — 소비자(M3a historian)는 최근 구간을 1분 CAGG·원본과 합쳐 읽는다.
  - refresh policy 범위: 1분 `[now-1일, now-1분]`, 1시간 `[now-3일, now-1시간]`. 이 범위보다 오래된 구간은 policy가 채우지 않는다. **업그레이드 이전 이력의 1시간 CAGG backfill은 아직 없다**(→ M3a-3에서 불필요로 닫음: r0·r1에는 historian이 없어 이력 행 0, 아래 M3a-3 절) — 1시간 CAGG를 읽는 쪽(M3a `GET /history?agg=1h`)과 함께 backfill 명령·실행 시점(Updater `succeeded` 뒤 부수 효과 또는 core 명령)을 정한다. 그 전까지 r0→r1 현장의 1시간 CAGG는 업그레이드 시점 3일 전부터만 있다(현재 운영 현장 없음).
- 통합 테스트: 압축 chunk가 있는 epoch 1 DB → `migrate up` → **refresh policy job**이 채운 1시간 CAGG를 원본 직접 집계와 bucket마다 대조(값이 분마다 바뀌고 BAD 행 포함, policy 범위 밖은 비어 있음), 0006·0007 사이 crash 상태(구문 실행·기록 전 / 0006만 기록)에서 `up` 수렴. r1 번들 실측: 739 MB, 빌드 15 s(WSL2).
- **n-minus-1**: 게이트는 N-1 테스트와 별개로 N-1 이미지의 `pureiron-core migrate status --json`이 `cur == N`·envelope 안·checksum 정상인지 직접 본다(롤백된 N-1 앱이 ready가 되는 근거). N-1 core-test 이미지는 동결이라 고칠 수 없으므로, 앱 동작이 아니라 스키마 모양을 지나치게 좁게 단정한 테스트만 `deploy/build/n_minus_1_waivers.json`(N-1 release × 정확한 pytest node id × 사유 × 기대 실패 정규식 `expect`)으로 deselect 한다. 게이트는 waiver 테스트를 따로 돌려 **여전히 그 이유로 실패하는지** 확인하고(통과하거나 다른 이유로 실패하면 → 게이트 실패), 본 실행은 통과 ≥ 1·deselect 수 == waiver 수·실패 0이어야 한다. 기존 DB 모드에서 `fresh_db`·`compose` 테스트의 skip은 설계상 예외다(새 DB·compose 스택이 필요한 테스트라 N 스키마 DB에서 돌 수 없다) — 그 공백은 envelope 직접 판정이 메운다. 첫 항목: r0 `test_baseline_timescale_objects`(refresh policy 전역 개수 == 1, epoch 2에서 2). r1부터 같은 검사는 CAGG별 개수로 센다 — 스키마 단정은 다음 epoch가 객체를 더해도 성립하게 쓴다.
- **아직 없음**: (1시간 CAGG backfill은 M3a-3에서 불필요로 닫음.) (Hyper-V VM 실측은 아래 M2d VM 절.) Updater `schema_epoch`를 migrate 결과(`migrate status`의 `cur`)로 맞추는 보정 — migrate가 DB에는 다 적용하고 0 아닌 코드로 끝나면 상태(1)와 DB(2)가 어긋나 다음 epoch 번들을 `E_SCHEMA`로 거부할 수 있다(M2b 설계 이월).

### V1 구현 현황 (M2d Hyper-V VM 실측, 2026-10-05)

- **하네스**: `tests/e2e`(PD-24) — Ubuntu 24.04 클라우드 이미지 → golden VHDX(읽기 전용) 위 차분 디스크, Gen2 + Secure Boot(MicrosoftUEFICertificateAuthority), cloud-init seed ISO, 내부 전용 vSwitch(전달 없음). 단계 `prepare → vm → install → scenarios → crash → report`, 판정·실행 방법의 정본은 [`tests/e2e/README.md`](../../../tests/e2e/README.md).
- **실측 기록**: release 쌍별 VM 실측 결과는 시점 스냅샷이라 [vm-e2e-runs](../reports/vm-e2e-runs.md)에 둔다.
- **M2 실측이 찾은 제품 결함(PR #11에서 수정)**:
  - **서비스가 실행한 롤백이 항상 `failed_manual`**: Updater unit `UMask=0027`로 백업 `project.json`이 root 0640 → 롤백의 `core project restore`(컨테이너 uid 10001, bind mount)가 읽지 못함. CLI(sudo, umask 0022)가 진행할 때만 통과해 단위·통합 테스트에 드러나지 않았다. core 이미지는 release별 동결이라 Updater가 `PUREIRON_SECRET_UID` 소유 0400 사본(`project.restore.json`)을 mount한다.
  - **TimescaleDB 텔레메트리**: 업스트림 기본값(`basic`)이 `telemetry.timescale.com`을 조회했다(폐쇄망 현장의 외부 접속 시도). site-db 이미지는 db-step release로만 바꾸므로 compose 기동 인자 `-c timescaledb.telemetry_level=off`로 끈다. compose는 번들마다 저장소에서 들어가 기존 release 번들에도 적용되며, 기존 현장은 이 compose를 처음 적용할 때(그리고 그 시도가 롤백되면 한 번 더) site-db 컨테이너가 다시 만들어진다(볼륨 유지).
- **호스트 준비에서 확인한 것**: 한국어 Windows는 Hyper-V 통합 서비스 이름이 번역됨(ID로 선택), `powershell -Command` 종료 코드는 마지막 `$?`(SilentlyContinue '없음'도 1), WSL 페이지 캐시가 4 GiB VM 복원을 `0x8007000E`로 막음(시작·복원 전 회수), nft `fwd`는 예약어, docker0 IGMP(224/4)는 외부 아님, OS 잡음(pollinate·snapd·update-motd 릴리스 확인)은 seed에서 끈다.
- **아직 없음**: R-7 `resume`(같은 gen `pending`)·`gen < last_attempted` 거부의 VM 실측(단위 테스트만), 마이그레이션 SQL 도중 전원 차단, 하네스 후속(리셋 직전 resolved 로그 읽기 지연 제거, CLI 대상 crash의 CLI 종료 확인, 시드 구간 행 수 정확 비교, vSwitch 종류 단정).

### V1 구현 현황 (M3a-3 historian, 2026-10-09)

- **0009(epoch 3, r2)**: `project_state.historian jsonb`(project.yaml `historian` 정규형, NULL = 전부 스키마 기본값) + `tag_history_1m`·`tag_history_1h` `materialized_only = false`(실시간 집계 — refresh 전 최근 구간도 조회에 나온다. 위 M2d 절의 "원본으로 돌아가 읽지 않는다"는 epoch 3부터 해당 없음). 한 트랜잭션으로 적용된다. `apply-definitions`는 historian 설정이 바뀔 때도 `definitions_rev`를 올려 recorder가 plan과 같은 rev·같은 스냅샷으로 정책을 읽는다. retention 적용(`retention_days`)은 M5(Updater `succeeded` 부수 효과) — 지금은 값만 저장한다.
- **1시간 CAGG backfill 불필요**: r0·r1 core에는 historian이 없어 어떤 현장에도 `tag_history` 행이 없다. 첫 기록은 epoch 3 core부터다.
- **기록 규칙**(`pureiron_core.historian.recorder`, ingest sink):
  - fresh live 샘플만 deadband·max_interval을 거친다. 정책 = 태그 `record.*` → 프로젝트 `historian.default_*` → 스키마 기본값(deadband 0, 60 s). 기록 조건(하나라도 참): 태그 첫 샘플, quality 변경, bool 변경, `|값 − 마지막 기록값| > deadband`(0이면 같은 값 반복만 생략), 값 종류·NULL·유한↔비유한 전환, 마지막 기록 뒤 `max_interval_s` 경과. 시각이 역행한 live(마지막으로 본 시각 이하 — 같은 시각 포함: 같은 `(tag_id, time)` 두 번째 행은 어차피 저장되지 않으므로)는 기록하지 않는다. 단 마지막으로 본 시각이 지금보다 2 s(`CLOCK_SLACK_NS`, `tags.ports` — TagStore와 공용) 넘게 미래면 벽시계가 뒤로 보정된 것으로 보고 태그 상태를 리셋해 그 샘플을 첫 샘플처럼 기록한다 — 시계 역보정 때 현재값만 갱신되고 이력이 비는 불일치를 막는다(Hyper-V 게스트 TimeSync 계단 보정 실측, M3a-3).
  - replay·오래된 live(> `PUREIRON_INGEST_LIVE_MAX_AGE_S`)·미래 시각 live(> now + 2 s)는 **원시 기록**한다 — 걸러내지 않고, live deadband 상태를 읽지도 바꾸지도 않는다(이력은 받은 것을 다 남기고 걸러내기는 현재값 경로만).
  - NaN/Inf는 `value_num = NULL`(quality는 받은 값, 모르는 quality는 BAD) — CAGG avg/min/max 오염 방지. deadband 비교에서 비유한값끼리는 같은 값이다.
  - batch `plan_rev`가 현재 rev보다 크면 이력에서 버린다(rev는 단조 증가 → site-db 재생성 신호, 옛 tag_id가 다른 태그로 기록되는 것 방지). plan을 아직 모르면(기동 직후 DB 미준비) live까지 원시로 받아 둔다(collector는 Ack된 batch를 다시 보내지 않으므로 버리면 DB 복구 전 이력이 사라진다) — 그 사이 DB 재생성이 겹치면 막지 못한다(한계). 현재 정의에 없는 tag_id의 live 샘플(정의에 있는 tag_id 기준 — plan에서 문제로 빠진 태그도 정책을 유지해 기록한다)과 `tag_history.tag_id`(int4) 범위 밖 tag_id도 버린다(한 행이 INSERT 전체를 매번 실패시키지 않게). raw는 옛 rev의 tag_id(retired 포함)를 그대로 기록한다(id 재사용 없음). 버린 수는 `/readyz.historian.rejected_total`.
- **쓰기**(`historian.writer`): 메모리 버퍼 상한 600,000행(넘치면 가장 오래된 행부터 버림, `dropped_total`. 쓰는 중 최대 `FLUSH_MAX_ROWS` 추가 — 순간 최대 620,000행), 1 s마다 최대 20,000행씩 `INSERT … ON CONFLICT (tag_id, time) DO NOTHING`(replay 중복 흡수, 시각은 µs). 실패하면 꺼낸 행을 순서대로 버퍼 앞에 되돌리고 상한을 다시 적용한다 — DB 장애 동안은 이 버퍼가, core 단절 동안은 collector 링버퍼(60 s)가 이력을 든다. 단 행 내용 때문에 거부되면(asyncpg `DataError`·`IntegrityConstraintViolationError` — 클라이언트 인자 인코딩 실패도 asyncpg가 `DataError`로 올림) 되돌리지 않고 batch를 반으로 나눠 앞쪽부터 다시 쓰고, 1행까지 줄어도 거부되면 그 행만 버린다(`poisoned_total`, 경고 60 s 간격, M3b-0 L9b) — 한 행이 버퍼 전체를 매 주기 막지 않게. n행 batch의 INSERT는 최대 2n−1번(모든 행이 거부될 때)이다. 격리 도중 연결 오류가 나면 이미 기록·격리한 앞부분을 뺀 뒤쪽만 되돌린다. 한 번의 쓰기(연결 획득·INSERT·연결 반환 대기)는 15 s(INSERT 쿼리 제한 10 s + 여유 5 s)가 지나면 취소해 실패로 보고, 행은 다음 주기에 다시 쓴다(`db_ok=false`). DB 연결 획득과 반환은 `Database`가 각각 5 s로 제한한다(획득 초과는 `DatabaseUnavailableError`, 반환 초과는 연결 terminate — 슬롯도 그 안에 풀림). 15 s 취소가 INSERT 도중에 걸리면 shield된 반환은 그 반환 제한으로 끝나므로, half-open DB에서도 INSERT 한 번은 **최대 20 s**(쓰기 15 s + 반환 5 s) 안에 돌아온다(시간 초과는 격리하지 않으므로 flush도 첫 시간 초과에서 멈춘다). 정지 순서는 수집 정지 → 진행 중 쓰기 취소 → 최종 flush(5 s 제한) → DB close이고, half-open이면 쓰기 취소와 최종 flush 뒤에 반환 정리가 각각 최대 5 s 더해진다(`stop` 최악 15 s). compose core `stop_grace_period: 25s` 안에서 SIGTERM → uvicorn 0.2 s + HTTP 유예 2 s(`HTTP_GRACEFUL_SHUTDOWN_S`) → acquisition(plan keeper 취소가 쿼리 도중이면 반환 정리 5 s + gRPC grace 2 s) → historian 15 s = 최악 24.2 s라 최종 flush는 SIGKILL 전에 끝난다(M3b-0 W5). 반환을 기다리던 쪽이 취소된 뒤의 반환 실패는 `Database`가 회수한다(asyncio `Task exception was never retrieved` 없음, V2-1).
- **허용 손실(문서화)**: core 프로세스 crash 시 아직 쓰지 못한 버퍼(정상 시 ≤ 1 flush 주기분, DB 장애 중이면 상한 안의 전체), 버퍼 상한 초과분(가장 오래된 것부터), collector 링버퍼 60 s를 넘는 core 단절 구간.
- **조회**(`historian.reader`): raw(`tag_history`)·1m·1h(CAGG) + 구간 길이로 고르는 `auto_resolution`(≤ 1 h raw, ≤ 24 h 1m, 그 이상 1h). HTTP 이력 API·Trend 화면은 인증과 함께 M3b.
- **검증**: compose 네트워크 분리 replay는 통합 테스트 `test_network_disconnect_replay`, 2,000태그 1 s 부하 기록은 `g1.sh perf-ingest` — 판정식은 테스트 docstring(`services/core/tests/integration/test_compose_stack.py`), 실행 job은 [testing](testing.md) `integration-compose`·`perf-ingest`.
- **아직 없음**: `GET /history`(M3b), retention 적용(M5).

### V1 구현 현황 (M3b-1 auth·audit, 2026-10-11)

- **epoch 4(r3, 2026-10-17 release — 이후 고정)**: `epochs.yaml` 4 → 최종 `0012_alarms.sql`(M3b-1 때 `0011_audit.sql`이었고 M3b-2가 미release epoch 확장 규칙(migrate lint `--base-ref`)으로 늘렸다). r3 기록(`app_expected` 4) 뒤로는 epoch 4에 파일을 더할 수 없다 — 다음 migration은 epoch 5. 0010·0011 두 파일 모두 트랜잭션 파일이라 실행과 기록이 한 트랜잭션이다 — 파일 안에서 죽으면 객체가 남지 않고, 0010만 기록된 상태에서는 epoch가 3에 머문다(r2 앱 ready). 통합 테스트 `test_epoch4_resumes_after_crash_between_or_inside_files`.
- **0010_auth**: `users(user_id bigserial PK, username text NOT NULL UNIQUE, display_name, role smallint CHECK 0~3, password_hash, enabled DEFAULT true, created_at, updated_at)` — role 0 viewer·1 operator·2 engineer·3 admin. 계정은 지우지 않고 `enabled`만 바꾼다. `password_hash` = `scrypt$n$r$p$salt_b64$hash_b64`(표준 라이브러리 scrypt, 기본 n=2^14·r=8·p=1·16 B salt, 매개변수를 해시에 같이 둬 기본값을 올려도 옛 해시가 검증된다). `sessions(token_sha256 bytea PK, user_id → users, created_at, last_seen_at, expires_at, revoked_at)` + `(user_id)` 인덱스 — 토큰 원문은 저장하지 않는다. 로그인할 때 그 사용자의 절대 만료·폐기된 세션 행을 지운다. UNIQUE·REFERENCES·CHECK는 `CREATE TABLE` 안이라 migrate lint 허용 범위(제약 추가 검사는 `ALTER TABLE`만 본다). `audit_log.source`에는 CHECK를 두지 않는다 — 나중에 값을 더할 수 없게 되므로(ADD-only).
- **0011_audit**: `audit_log(id bigserial PK, time timestamptz DEFAULT now(), username, action NOT NULL, target, detail jsonb DEFAULT '{}', source NOT NULL)` + `(time DESC)`·`(username, time DESC)` 인덱스. 추가만 하고 고치거나 지우지 않는다(보존 정책은 범위 밖). `detail`에는 비밀번호·토큰을 넣지 않는다. action·source 값은 [api-events](api-events.md) V1 인증·감사 절.
- **기본 계정 없음**: migrate는 사용자를 만들지 않는다. compose dev·통합 테스트·VM 하네스는 `pureiron-core users add`(비밀번호는 stdin 또는 `--password-file`)로 만든다.

### V1 구현 현황 (M3b-2 alarms·realtime, 2026-10-11)

- **epoch 4 포인터 → `0012_alarms.sql`**(미release 확장 → r3로 release, 이후 확장 불가). mid-crash 통합 테스트에 `0011-recorded`·`0012-rolled-back`을 더했다 — 0012가 기록되기 전에는 epoch 3.
- **0012_alarms**: `alarms(alarm_id text PK, tag_path NOT NULL, definition jsonb NOT NULL, retired_at)` — `apply-definitions`가 project `alarms/*.yaml`을 alarm_id 기준 upsert, 빠진 정의는 `retired_at`만(되살리면 NULL), 바뀌면 `definitions_rev`+1(태그·장비와 같은 rev 스냅샷, `Definitions.alarms`). `definition`은 정규형 JSON(스키마 기본값 생략 → 소비자가 deadband 0·on_delay_s 0·priority medium을 채운다). `alarm_journal(id bigserial PK, time timestamptz DEFAULT now(), alarm_id, tag_path, from_state, to_state, value float8, username, note)` + `(alarm_id, time DESC)`·`(time DESC)` 인덱스. 저널은 이력·감사이고 현재 상태가 아니다(현재 상태 = core 알람 엔진 메모리).
- **알람 상태 기계(D7)**: 4상태 `NORMAL`·`ACTIVE_UNACKED`·`ACTIVE_ACKED`·`CLEARED_UNACKED`(+ 저널·WS 전용 `RETIRED`). 조건 above `value >= setpoint`, below `value <= setpoint`. 해제는 deadband 적용(above `< setpoint - deadband`, below `> setpoint + deadband`, 그 사이는 유지). NORMAL·CLEARED_UNACKED --조건--> ACTIVE_UNACKED --ACK--> ACTIVE_ACKED --해제--> NORMAL, ACTIVE_UNACKED --해제--> CLEARED_UNACKED --ACK--> NORMAL(legacy의 CLEAR_ACKED·재ACK 없음). `on_delay_s` > 0이면 조건 성립 시 타이머(재진입 시 그대로, 조건이 깨지면 취소), 만료 시 TagStore 현재값이 GOOD이고 조건이 여전할 때만 활성. quality가 GOOD이 아니거나 값이 없거나 NaN/Inf면 평가 보류(상태 유지 — 통신 끊김을 해제로 보지 않음). 같은 태그는 above setpoint 오름차순 → below 내림차순으로 평가. 정의에서 빠진 알람은 `RETIRED` 한 행을 남기고 제거.
- **복원**: core 기동 시 `alarms` 행(retired 포함)마다 저널 마지막 행(time·id 내림차순)의 `to_state`로 시작한다. site-db가 없으면 복원될 때까지 정의 적용·평가를 보류(주기 재시도) — 미확인 알람이 새 알람으로 다시 생기지 않게. core가 꺼진 동안 정의에서 빠진 미해결 알람은 복원 직후 `RETIRED`.
- **저널 쓰기(D8)**: 조건 변화 전이는 `tag.changed` 핸들러가 메모리 큐(상한 10,000행, 넘치면 가장 오래된 것부터 버리고 `/readyz.alarms.dropped_total`)에 넣고 writer task가 1s마다 쓴다(실패하면 큐 앞으로 되돌려 재시도, 정지 시 제한 시간 안 최종 쓰기). **ACK는 큐를 거치지 않는다**: 전이 행과 `audit_log`(`alarm.ack`·`alarm.ack_all`)를 한 트랜잭션으로 쓰고 성공한 뒤에만 메모리 반영·`alarm.changed` 발행. 기록하는 사이 해제만 됐으면 지금 상태 기준 ACK 전이(CLEARED_UNACKED → NORMAL)를 지금 시각으로 저널 큐에 더한다(해제 행보다 뒤라 복원이 고르는 마지막 행 = 메모리 상태). 기록하는 사이 새로 발생(ACTIVE_UNACKED 진입 — 알람별 발생 번호로 판정)했으면 그 ACK는 반영하지 않는다: 운전원이 보지 못한 발생이고, 마지막 행이 그 발생 행이라 메모리와 같다. 기록된 ACK 행·감사는 확인 행위로 남는다.

### V1 구현 현황 (M3c-1 쓰기 계약·collector, 2026-10-18)

ADR-013(개정) 쓰기 경로의 계약과 collector 실행부다. core 쓰기 서비스·REST·감사(현장 스위치 `PUREIRON_WRITES_ENABLED`, 2단계 확인, 실행 전 검사, readback 판정)는 M3c-2([api-events](api-events.md) V1 쓰기 API 절) — 스위치가 꺼진 현장(기본)에서는 core가 `WriteRequest`를 보내지 않고 plan의 `writable`은 전부 false다. migration 없음(쓰기 선언은 `tags.definition jsonb` 원문에 같이 저장된다) → epoch 그대로.

- **쓰기 선언(project 스키마 `$defs/tagWrite`, 태그의 선택 필드 `write`)** — 없으면 읽기 전용.

  ```yaml
  - path: line1/temp_sp
    device: plc-1
    modbus: {register_type: holding, address: 100, data_type: float32}
    write: {kind: setpoint, min: 20, max: 90}
  - path: line1/start
    device: plc-1
    modbus: {register_type: coil, address: 10, data_type: bool}
    write: {kind: command, values: [true], permissive: [line1/remote_ok, line1/auto_mode], role: operator}
  ```

  - `kind: setpoint` = 설정값·운전 조건: `min`·`max` 필수, `values` 금지. `permissive`는 선택.
  - `kind: command` = 운전 명령: `values`(허용 값, 1개 이상)·`permissive`(PLC 허가 조건 태그 path, 1개 이상 — 전부 GOOD·참일 때만 보낸다) 필수, `min`·`max` 금지.
  - `role: operator|engineer`(기본 operator, 정규형에서 생략) = 실행에 필요한 최소 역할.
  - project-tools 교차 검사(현장 도달 전 거부): `register_type`은 holding·coil만(input·discrete는 읽기 전용), holding + `data_type: bool`은 불가(FC6이 레지스터 16비트 전체를 덮어쓴다 — coil 또는 uint16 command로 선언), setpoint는 숫자 형식이고 `min ≤ max`·범위가 data_type으로 인코딩 가능, command 값은 형식 범위 안(bit 형식은 bool만, 정수 형식은 범위 안 정수, float32는 ±3.4028e38 안), permissive는 존재하는 bool 태그(coil·discrete 또는 `data_type: bool`)이고 자기 자신이 아니며 **그 자신이 쓰기 대상(`write` 있음)이 아니고 그 주소를 어떤 쓰기 태그도 덮지 않음**(같은 장비·register_type 주소 범위 비교 — PureIron이 자기 허가 조건을 켜는 우회 방지), 같은 장비·register_type의 쓰기 태그끼리 주소 범위가 겹치지 않음. 승인 기록은 고객 project repo의 PR 리뷰다(ADR-013 2항).
- **proto(호환 추가만, `buf breaking` 통과)**: `TagPoint.writable = 5`(기본 false), `WriteRequest.plan_rev = 4`·`timeout_ms = 5`(uint32, 상대 시간), `WriteResult.readback = 4`(optional double)·`readback_ts_unix_ns = 5`(optional int64), `WriteResult.Status.INDETERMINATE = 3`. bool·coil 쓰기 값은 `WriteRequest.value` 0/1.
- **WriteResult 의미(단계별)** — 결과는 "장비에 무엇이 갔는가"로 정한다. **의심되면 `INDETERMINATE`**: `FAILED`는 운전자가 다시 눌러도 되는 결과라 실행되지 않았다고 확신할 때만 쓴다.

  | status | 언제 | 장비에 |
  |---|---|---|
  | `REJECTED`(0, 기본값 — 빠진 응답도 거부로 읽힌다) | 보내기 전: request_id 비어 있음, `plan_rev` 0·불일치, `timeout_ms` 0·60000 초과, plan에 없는 태그, `writable=false`, 인코딩 불가(범위 밖 정수·비정수·NaN/무한대·float32 범위 밖·bool 0/1 외), 장비별 대기 8건 초과, 같은 태그 대기·진행 중, 기한 경과(`expired before send`), poller 정지(plan 교체·종료) | 안 감 |
  | `FAILED` | 장비 미연결(큐에 넣지 않고 즉시, 새 연결을 열지 않음), 요청 거부형 Modbus 예외 응답: 01 ILLEGAL FUNCTION·02 ILLEGAL DATA ADDRESS·03 ILLEGAL DATA VALUE·06 SLAVE DEVICE BUSY·0A GATEWAY PATH UNAVAILABLE | 확실히 안 써짐 |
  | `INDETERMINATE` | 보낸 뒤 응답 시간 초과·연결 끊김·보내는 도중 취소(연결을 닫고 다음 poll이 다시 연결), 실행 여부를 알 수 없는 Modbus 예외 응답: 04 SLAVE DEVICE FAILURE(실행 도중 실패)·05 ACKNOWLEDGE(처리 중)·0B GATEWAY TARGET FAILED TO RESPOND·모르는 코드 | 써졌을 수도 있음 — 성공으로 읽지 않는다 |
  | `OK` | 장비가 정상 응답 — 반영 여부는 판정하지 않는다 | 써짐(ack) |

  최근 처리한 request_id(64개)가 다시 오면 collector는 **응답하지 않는다**(경고 로그만) — 그 id의 응답은 원래 요청 것 1건뿐이다. core는 쓰기마다 새 request_id를 쓴다.

  OK면 같은 주소를 바로 다시 읽은 값(decode 결과, bool은 0/1)과 시각을 `readback`·`readback_ts_unix_ns`에 싣는다. 재읽기가 예외 응답·통신 실패·도중 취소로 끝나면 OK 그대로 둘 다 비우고 `error`에 이유를 남긴다(이미 써졌으므로 거부·실패로 바꾸지 않는다). 판정(설정값 = readback 일치, 명령 = 장비 응답 기준)은 core(M3c-2)가 한다 — float32는 선언 형식으로 인코딩→디코딩한 값(가장 가까운 float32)과 비교해야 거짓 불일치가 없다. core는 `INDETERMINATE`와 모르는 status를 응답 없음(결과 모름)으로 다룬다.
- **기한(`timeout_ms`)과 응답 없음의 성질**: collector는 쓰기 메시지를 **처리하는 순간** `deadline = monotonic + timeout_ms`를 잡고(수신 시각이 아니다), 장비로 보내기 **직전**에 다시 확인해 지났으면 보내지 않는다(REJECTED). 보낸 쓰기는 쓰기(장비 `timeout_ms`)와 재읽기(장비 `timeout_ms`) 안에 끝난다. 처리 시점은 수신보다 늦을 수 있다 — plan 적용 중에는 collector 수신 루프가 최대 `STOP_TIMEOUT_S`(5 s, poller 정지 대기) 멈춘다. 그래서 **core가 응답 없음을 확정한 뒤에는 그 쓰기가 PLC에 닿지 않는다**는 성질은 다음 두 조건에서 성립한다: (1) core는 collector가 현재 plan_rev의 batch를 보낸 뒤에만 쓰기를 보낸다(= plan 적용이 끝났다, M3c-2 D9), (2) core 응답 대기 시한 ≥ `timeout_ms + 2 × 장비 timeout_ms + 여유`, 여유 ≥ 수신 지연. 그러면 ADR-013 7항 "응답 없음 = 거부로 읽음"이 실제로도 성립한다. 세션이 끝나면 아직 보내지 않은 쓰기는 취소돼 보내지 않는다.
- **collector 실행**: 쓰기 함수는 coil FC5, holding 1워드 FC6, 2·4워드 FC16뿐이다(PLC 로직 불변 — `ModbusClient` 표면 고정 테스트). 값은 `encode()`(= `decode()`의 역, byte·word order 포함). 장비별 `DevicePoller`가 쓰기 큐를 **poll과 poll 사이**에 같은 task·같은 연결로 하나씩 실행해 한 장비에 동시 트랜잭션이 없다. 자동 재시도 없음. 결과를 기다리는 동안에도 세션은 Ack·plan을 계속 읽는다. 같은 plan_rev의 plan이 다시 오면(재연결) poller는 그대로 두고 쓰기 표(tag_id → 주소·`writable`)만 새 plan으로 바꾼다 — 현장 스위치는 정의 rev를 바꾸지 않으므로 스위치를 바꿔 다시 띄운 core의 `writable`이 반영돼야 한다(M3c-2).
- **dev 시뮬레이터(`SIM_MEMORY`, 기본 100)**: holding N..N+M-1(N = `SIM_REGISTERS`)과 coil 0..max(N, M)-1은 사인파로 덮어쓰지 않는 메모리. 거부 시험 주소: holding N+M-1 쓰기 → 예외 응답(ILLEGAL DATA VALUE), N+M-2 쓰기 → 정상 응답하지만 값 유지(readback 불일치). compose 기본값이면 메모리 holding 100..199, 예외 199, 무시 198. 허가 조건 coil은 시험 하네스가 직접 바꾼다.

### V1 구현 현황 (M5-0 번들 프로젝트 적용·트리 백업/복원, 2026-10-25)

번들 `project/project.tar`가 이제 실제로 현장 정의가 된다(이전에는 Updater가 `project_state` 메타만 기록했다). 롤백도 정의를 되돌린다. 근거 plan `pureiron-m5.md` D5~D7.

- **migration `0013_project_files.sql`(epoch 5, ADD-only)**: `project_files(path text PK, content bytea, sha256 text)` = 마지막 apply·restore의 정규형 파일 전체. DB 정의 행만으로는 파일 배치·`project.yaml` 메타를 되살릴 수 없어서 둔다.
- **`pureiron-core project apply --tar <project.tar> --project-id --version --content-hash`**:
  1. tar 안전 읽기(메모리, 압축 없는 tar만, 64 MiB·10,000 파일 상한) — 정규화된 POSIX 상대 경로의 일반 파일만. 절대 경로·`..`·`./`·`//`·역슬래시·심볼릭/하드 링크·디렉터리·장치·FIFO·중복 경로는 거부.
  2. project-tools `load_project` 검증. 프로젝트 파일이 아닌 항목(README, `screens/.gitkeep` 포함)이 섞이면 거부. 다시 계산한 content_hash와 `project.yaml` project_id가 인자와 같아야 한다.
  3. 한 트랜잭션(정의 적용 advisory lock): `apply_definitions`(path·alarm_id 기준 upsert·retire, 바뀌면 `definitions_rev += 1`) + `project_files` 전체 교체(정규형 바이트) + `project_state` 메타·`applied_at`·`base_site_rev = site_rev`.

  1·2의 실패는 DB에 닿지 않고, 3의 실패는 트랜잭션이 되돌린다 → 무엇이든 실패하면 아무것도 바뀌지 않고 exit 4. 같은 project_id·version·hash·파일 재적용은 아무것도 쓰지 않는다(`changed: false`, recover 재실행에 안전). stdout = 정렬 JSON(`state`·`changed`·`files_changed`·`definitions`). `--tar` 없는 이전 형식(메타만 기록)은 M5 이전 Updater 호환으로 남는다.
- **`project export --with-state --out <dir>`**: `state.json`(`ProjectState.to_snapshot()`) + `project.tar` = **현재 유효 트리**(`project_files`에 현재 DB 정의를 덮어쓴 것 — 항목 단위로 `devices/<id>`, 목록의 같은 tag `path`·alarm `id`; retire된 항목은 파일 값 유지). tar는 번들 빌더와 같은 결정적 형식(경로 UTF-8 바이트 순, PAX, mtime 0·root·0644)이라 운영 값 변경이 없으면 번들 project.tar와 바이트가 같다(deploy 테스트). 쓴 트리를 다시 검증해 복원할 수 없는 백업은 만들지 않는다. `project_files`가 비어 있으면(트리 apply 전) `project.tar` 없이 state만 쓴다. `--with-state` 없는 `export`(state JSON 한 개)는 그대로다.
- tar 안전 읽기는 같은 이름이 파일이면서 디렉터리인 경우(`a`와 `a/b`)와 비UTF-8 이름도 `ProjectTreeError`(exit 4)로 거부한다. state 스냅샷 키 누락·날짜 형식 오류도 exit 4다.
- **`project restore --from <dir>`**: 그 디렉터리의 트리(있으면) 재적용 + state 스냅샷 그대로(site_rev·base·잠금 포함, 리셋 없음)를 한 트랜잭션으로. 트리는 운영 값이 덮인 것이라 content_hash는 스냅샷 값과 비교하지 않는다(project_id만 확인). `--from <state JSON 파일>`은 이전 백업 하위호환으로 state만 되돌린다. export → restore → export는 바이트 동일.
- **Updater**: 어느 core 이미지가 무엇을 하는지는 [architecture](architecture.md) 배포 절과 `services/updater/src/pureiron_updater/backup.py` docstring. 트리 명령 판단 = manifest `schema.app_expected ≥ 5`. 백업 `backups/g<gen>/project/{state.json, project.tar}`(+ 기존 `project.json`). 번들 tar·백업은 core 컨테이너 uid 소유 0400 사본을 읽기 전용 mount, export 출력은 그 uid 소유 0700 디렉터리를 쓰기 mount 한다. 쓰기 mount 결과물은 신뢰하지 않는다 — `state.json`(+ `project.tar`) 외 항목·심볼릭/하드 링크·일반 파일이 아닌 항목이면 `E_BACKUP`, root updater는 링크를 따르지 않고(`lstat`·`O_NOFOLLOW`) 읽는다.
- **한계**: 직전 이미지가 epoch 4 이하(r3)면 백업에 트리가 없어, 첫 트리 release(r4) 업데이트가 실패하면 state만 되돌리고 r4가 적용한 정의는 남는다(r3 현장은 번들 정의를 받은 적이 없다). 이 롤백은 결과 `failed` + 보고 `logs`의 `E_DEFINITIONS_LEFT` + Updater alarm(`updater_failed_manual 1`)으로 드러나고(경보는 트리 release 성공만 지운다 — 트리 이전 release 성공은 메타만 기록해 남은 정의를 고치지 않는다), 수동 복구는 updater README runbook(직전 이미지로 `apply-definitions <직전 프로젝트 dir>`). 테스트 스택(`g1.sh`·web e2e)은 계속 `apply-definitions <dir>`를 쓴다(개발 도구 경로, 같은 `apply_definitions`).
- **정의 읽기는 관대하다(롤백 순서 계약)**: Updater 롤백은 직전 앱을 먼저 띄우고(`up`) 그 뒤에 정의를 되돌린다(`restore`). 그래서 N-1 core는 잠시 N 정의 위에서 부팅한다. core가 site-db 정의 행을 읽을 때(`acquisition/plan.py`) 모르는 키는 무시하고, 쓸 수 없는 항목은 problems로 남기고 건너뛴다 — 부팅·ready를 막지 않는다. 엄격한 검증은 project-tools(번들 빌드)와 `project apply`에서만 한다. 이 계약을 깨려면 롤백 순서(restore를 `up` 앞으로)를 먼저 바꿔야 한다.
- **검증**: core 단위(tar 거부 표, 정규형·결정적 왕복, DB 전 hash·project_id 거부), deploy(번들 project.tar = core가 받는 트리·같은 hash·같은 바이트), 통합(v1 → 같은 hash no-op → v2 setpoint·태그 retire → 운영 값 변경 뒤 export → 실패할 v3 적용 → restore가 정의·site_rev·base 복원, 재실행·export 바이트 동일; 변조 tar·hash 불일치·unsafe tar·`project_files` 트리거 주입 중간 실패 무변경), Updater 단위(트리 적용·백업·롤백·전환기 한계와 `E_DEFINITIONS_LEFT` 경보·되돌림 release·쓰기 mount 링크 거부) + crash 주입 수렴(`tree_first_install`·`tree_update`·`tree_health_fail`(흡수 드래프트 site_rev 5·base 2)·`tree_migrate_fail`·`transition_health_fail`(r3→r4)·`transition_meta_success`·`transition_tree_success`(남은 정의 경보 유지·해제), 복구 중 재crash·창 닫힘·재부팅 포함).

### V1 구현 현황 (M5-2 현장 드래프트 PATCH·서명 export, 2026-10-25)

현장 운영 값 긴급 수정이 `site_rev`를 올리고, 그 결과를 site key로 서명한 파일로 벤더에게 넘길 수 있다(회수 `absorb`는 M5-3). 근거 plan `pureiron-m5-2.md` D1′·P1~P7, `pureiron-m5.md` D8·D9.

- **D1′(사용자 결정 2026-10-25)**: PATCH 허용 = 알람 `setpoint`·`deadband`·`on_delay_s`·`priority`, 태그 `description`·`eu`·`record.{deadband,max_interval_s}`. 알람 `enabled`는 스키마·엔진·화면에 없는 개념이라 빼고 백로그로 둔다. 장비 연결·태그 주소·추가·삭제는 Git 정본 경로로만.
- **PATCH**(`/api/v1/project/alarms/{id}`·`/project/tags/{path}`, admin): 규칙·오류 코드·감사는 [api-events](api-events.md) 현장 프로젝트 API 절. DB에는 정의 jsonb만 바꾼다(기본값 생략 정규형) — `project_files`는 그대로이고, 유효 트리 export가 그 위에 DB 정의를 덮는다(M5-0 절). 그래서 PATCH 값은 Updater 백업(`export --with-state`)과 드래프트에 함께 실리고, 다음 번들 apply가 `base_site_rev = site_rev`로 기준점을 옮긴다. 새 마이그레이션은 없다.
- **`pureiron-core project export --draft --out <dir>`**(P5): 한 repeatable-read 스냅샷에서 `project.tar`(유효 트리, `--with-state`와 같은 결정적 tar) + `draft-meta.json` = `{"base_site_rev", "content_hash", "site_rev"}`(정렬 JSON, `content_hash` = 유효 트리를 `load_project`로 다시 계산한 값). `project_files`가 비면(트리 apply 전) 아무것도 쓰지 않고 exit 4. 같은 상태 두 번 export = 같은 바이트. `--with-state`와 함께 쓸 수 없다(백업 형식은 그대로).
- **`pureiron-site project export --out draft.tar`**(P6, Updater CLI): 등록(`enroll`) 전·키 불일치면 core를 부르지 않고 exit 3, 적용된 generation이 없거나 core 실패·출력 위반이면 exit 1(파일 없음). 적용된 generation의 core 이미지로 `export --draft`(쓰기 mount `state/draft-export/`, 컨테이너 uid 소유 0700 — 백업과 같은 규칙으로 정확히 두 일반 파일만, 링크를 따르지 않고 읽은 뒤 지운다) → 봉투 → `draft.tar`. `site_rev == base_site_rev`(흡수할 드래프트 없음)여도 파일은 쓰고 `note: no site drafts …`를 출력한다.
- **드래프트 파일 형식**(`draft.tar`): 비압축 POSIX tar(PAX), 정확히 두 일반 파일 `draft.json` → `project.tar`(경로 UTF-8 바이트 순, mtime 0·uid/gid 0·빈 uname/gname·0644). `draft.json` = sitesign 봉투 `{"format": 1, "purpose": "site-draft", "payload": {…}, "signature": "<hex>"}`(서명 대상 = JCS `{format, payload, purpose}` — enrollment·report 서명과 도메인 분리), payload = `{site_id, site_rev, base_site_rev, content_hash, exported_at(UTC RFC 3339 Z), project_tar_sha256("sha256:<hex>")}`.
- **검증 `pureiron_bundle.sitesign.open_draft(data, public, *, site_id) → (payload, project.tar)`**(P7, bundle-tools): 등록된 site 공개키와 그 현장 id로만(자기 키 검증 없음). payload `site_id`가 기대 현장과 다르면(그 현장 키로 서명했어도) `SignedDocumentError`. 두 파일 외 항목·중복·누락·링크·크기 초과(전체 65 MiB, 봉투 64 KiB)·tar 형식 오류·다른 purpose·서명 불일치·payload 형식(site_rev·base 음 아닌 정수, 나머지 문자열)·`project_tar_sha256` 불일치는 `SignedDocumentError`. tar 헤더는 `tarsafe`(적대적 헤더 제한)로 읽는다. 트리 내용 검증·`content_hash` 재계산은 absorb(M5-3)가 한다.
- **게이트**: 드래프트가 있는 현장(`site_rev > base_site_rev`)에 absorbs 없는 번들은 `E_DRAFT`(bundle verify ⑥·Updater `locked` 재검사, `check_draft_gate`). 벤더가 `absorbs[site] ≥ site_rev`로 회수한 번들만 통과한다.
- **검증**: core 단위(허용·거부 필드 표, null 제거, 기본값 생략·정규 숫자, no-op, admin 미만 403·Origin 403·오류 매핑 423/404/422/503, 활성 ACK 알람 setpoint PATCH), core 통합 site-db(rev·감사 같은 트랜잭션, no-op, 잠금 423·없음/retire 404 무변경, 감사 트리거 실패 전체 롤백, 다른 연결이 정의 적용 advisory lock을 쥔 동안 대기 상한 뒤 503 경로·무변경과 풀리면 대기하던 PATCH 성공, Updater 잠금 획득과 동시 실행 시 결과가 (PATCH 성공 + 획득 site_rev +1)·(423) 둘뿐, `--draft` PATCH 반영·바이트 동일·트리 없음 exit 4), bundle-tools(결정적 tar, 다른 키·위조·다른 purpose·다른 site_id·tar 변조·항목 추가/누락/중복·payload 형식 거부), Updater 단위(등록 전·적용 전·core 실패·출력 위반·작업 디렉터리 준비 실패 거부, 같은 입력 같은 바이트, CLI 종료 코드·안내), compose(트리 apply → PATCH 3회 → site_rev +3 → 게이트 `E_DRAFT`/통과 → 잠금 중 423 → 실제 core 컨테이너로 `export_draft` → `open_draft` + 트리 content_hash·PATCH 값; compose 하네스에 Updater 설치가 없어 Updater 쪽은 같은 argv 모양의 테스트 `Compose` 어댑터로 부른다).

### V1 구현 현황 (M5-3a 드래프트 회수 absorb·gate, 2026-10-30)

벤더가 현장 드래프트 파일(`draft.tar`, M5-2 절)을 고객 repo에 회수하고, 번들을 만들기 전에 드래프트 게이트를 미리 점검한다. 근거 plan `pureiron-m5-3.md` K1·K2·K4~K8. project-tools가 bundle-tools에 직접 의존한다(K7 — `sitesign`·`tarsafe`·`check_draft_gate`를 복사하지 않고 재사용, 그 대가로 core 이미지에 `cryptography`가 들어간다).

- **`pureiron-project absorb --draft <draft.tar> --site <id> --site-key <enrollment.json> [--rev N] [<dir>, 기본 .]`** — 거부되면 repo는 한 바이트도 바뀌지 않는다(전부 아니면 전무):
  1. **신뢰 근거(K1)**: `--site-key`는 Updater `enroll`이 쓴 `state/enrollment.json`(purpose `site-enrollment`, 자기 서명). 자기 서명 검증 + payload `site_id == --site` + payload `fingerprint`가 공개키 fingerprint와 같아야 그 공개키로 `open_draft`(서명·purpose·site_id·`project_tar_sha256`)를 부른다. 자기 서명은 개인키 보유만 증명하므로 위조 파일도 "유효"하다 — 신뢰는 **파일 출처**(설치 때 벤더가 받아 둔 파일)에서 오고, stdout 첫 줄 `site key: <site> <fingerprint> (compare with the installation record)`를 설치 기록과 사람이 대조한다. M4 fleet 등록부(`sites/`)가 생기면 그 키로 바꾼다.
  2. **드래프트 트리**: `project.tar`를 `tarsafe`로 읽는다(정규화된 프로젝트 파일 경로의 일반 파일만, 중복·링크·그 밖의 파일 거부, 10,000 파일 상한) → `check_texts`(= `load_project` 규칙) 검증 → 다시 계산한 content_hash가 payload `content_hash`와 같아야 한다.
  3. **repo**: `load_project` 통과, `project.yaml project_id`가 드래프트와 같아야 한다. 기록할 리비전 N = `--rev`(없으면 서명된 `site_rev`, 음수는 사용법 오류)는 서명된 `site_rev` 이하이고 기존 `absorbs[site]` 이상이어야 한다(퇴행 금지).
  4. **필드 단위 병합(K2, 순수 함수 `merge.plan_merge`)**: 드래프트의 알람(`id`)·태그(`path`, 파일과 무관하게 전역)마다 PATCH 허용 필드 — 알람 `setpoint`·`deadband`·`on_delay_s`·`priority`, 태그 `description`·`eu`·`record`(스키마 `record`는 닫힌 객체이고 하위 필드 `deadband`·`max_interval_s`가 모두 허용 대상이라 객체 통째로 — core 단위 테스트가 스키마까지 고정) — 를 repo의 같은 항목에 덮는다. 드래프트에 없는 선택 필드 = 스키마 기본값이므로 repo에서 지운다. 그 밖의 필드(태그 주소·장비·write 등)와 repo에만 있는 항목(새 태그·장비)은 그대로 둔다. 허용 필드 목록은 core `project/patch.py`와 같다(core 단위 테스트가 고정).
  5. **쓰기(K5)**: 바뀐 파일만 정규형으로 + `project.yaml absorbs.<site> = N`(이미 N이면 그대로). 쓸 결과 트리를 먼저 `check_texts`로 검증한 뒤 두 단계로 쓴다: ① 바뀔 파일 전부를 `<file>.absorb-tmp`로 쓰고 ② 차례로 rename하며 `project.yaml`(absorbs = 회수 표시)은 맨 마지막이다. ①에서 실패하면 만든 tmp를 모두 지운다(원본 무변경). ②에서 실패하면(Windows에서 다른 프로그램이 대상 파일을 잡고 있을 때 등) 남은 tmp를 지우고, 이미 rename한 파일은 되돌리지 않는다 — 되돌리기 쓰기는 원자적이지 않아 또 실패할 수 있고, 그 파일들은 검증한 병합 결과이며 absorbs가 기록되지 않았으므로 게이트가 번들을 계속 막는다. 같은 absorb를 다시 돌리면 나머지를 마저 쓴다. 쓴 뒤 `load_project`로 다시 확인한다. PATCH 값이 이미 repo와 같아도 absorbs는 기록한다.
- **종료 코드(K4)** — 1·2 모두 repo 무변경, stderr 한 줄 `… (repo unchanged)`. 예외는 쓰기 ② 단계에서 이미 rename한 파일이 있거나 tmp를 지우지 못한 경우·쓴 뒤 재확인 실패뿐이다: exit 1, stderr에 바꾼 파일 목록(`already replaced: …; absorbs not recorded`, 지우지 못한 tmp)과 `(repo changed: run absorb again to finish, or restore with git)`:

  | 코드 | 경우 |
  |---|---|
  | 0 | 회수 성공(바꿀 것이 없어도) |
  | 1 (`error:`) | enrollment 서명·형식·현장·fingerprint 불일치, 드래프트 서명·형식·tar·`project_tar_sha256`, 드래프트 트리 검증 실패, content_hash ≠ payload, repo 검증 실패, `project_id` 불일치, 파일을 읽을 수 없음, repo 쓰기 실패 |
  | 2 (`conflict:`) | 드래프트의 알람·태그가 repo에 없음(이름 목록 출력), `--rev` > 서명된 `site_rev`, 기존 `absorbs[site]` > N(퇴행) |
  | 2 (argparse `usage:`) | 사용법 오류(음수 `--rev` 포함) — stdout이 비고 stderr가 `usage:`로 시작해 충돌과 구분된다 |

- **출력**: `site key: …` → `draft: site_rev=… base_site_rev=… content_hash=…` → (`site_rev == base_site_rev`면 `note: the draft has no site changes …`) → 바뀐 필드마다 `<file>: <alarm|tag> <id> <field>: <전> -> <후>`(값은 JCS JSON, 필드 없음 = `(absent)`) → `absorbs[<site>]: <전> -> <N>` → `wrote <file>` → `ok: <회수 뒤 content_hash>` → 마지막 줄 커밋 trailer **`Absorbs-Draft: <site> rev=<N> sha256=<draft.tar 파일 sha256 hex>`**. 커밋·PR은 운영자가 한다(trailer를 커밋 메시지에 붙인다).
- **한계(R1)**: 공통 base 트리가 없어 3-way 병합이 아니다. 번들 이후 repo HEAD에서 벤더가 같은 PATCH 허용 필드를 바꿨다면 현장 값이 이긴다 — 바뀐 필드 전·후를 모두 출력하므로 PR 리뷰에서 사람이 판단한다. repo에서 지운(또는 이름을 바꾼) 항목이 드래프트에 남아 있으면 충돌(exit 2)이다 — 현장 값을 버릴지 사람이 정한다(항목을 되살려 회수한 뒤 다시 지우는 식). 후속: `--drop <alarm id|tag path>` 옵트인(버린 현장 값을 출력에 남김) 또는 M4 base 트리(번들 `project.tar`)를 쓴 3-way 병합(PATCH 필드가 base와 같은 삭제 항목은 자동 건너뜀).
- **`pureiron-project gate <dir> --site <id> (--draft <draft.tar> --site-key <enrollment> | --site-rev N --base-site-rev M)`(K6)**: 현장 리비전을 서명된 드래프트(absorb와 같은 검증, 드래프트 `project_id`가 repo와 같아야 함) 또는 숫자(0 이상)로 받아 repo `absorbs`와 `check_draft_gate`(bundle verify ⑥·Updater `locked`와 같은 함수, M4 publish도 같은 함수)로 판정한다. 0 = 통과(`ok: site … site_rev=… base_site_rev=… absorbs=…`), 1 = `E_DRAFT` 또는 점검할 수 없는 입력(stderr `error:`), 2 = 사용법 오류(입력 형태를 섞거나 짝이 빠짐, 음수 리비전).
- **core `apply_tree` no-op(K8, M5-2 리뷰 LOW-4)**: no-op 조건에 `site_rev == base_site_rev`를 더했다. 같은 project_id·version·hash·파일이라도 드래프트가 남아 있으면(게이트를 이미 통과한 번들 = 흡수 끝) 정의를 다시 적용하고 `base_site_rev = site_rev`로 옮긴다. 첫 적용 뒤에는 rev가 같아 Updater recover 재실행은 그대로 no-op이다. 새 마이그레이션 없음.
- **검증**: project-tools 단위(병합 계획 표, 값 변경·선택 필드 제거·같은 값·재실행 무변경·`--rev`·현재 디렉터리 기본값, 다른 키·enrollment 다른 키/다른 현장/위조·봉투 변조·tar 변조·링크 항목·payload hash 불일치·`project_id` 불일치·잘못된 트리·repo 무효·파일 없음 → 1 무변경, 쓰기 실패 주입(tmp 단계 → tmp 0개·원본 바이트 동일·`(repo unchanged)`, rename 단계 → `(repo changed`·absorbs 미기록·tmp 0개), `project.yaml` 마지막 rename, 음수 `--rev` → 2, repo에 없는 항목·`--rev` 초과·absorbs 퇴행 → 2 무변경, trailer·fingerprint·안내 출력; gate 두 입력 형태 × 3분기·서명 실패·`project_id` 불일치·사용법 오류), G1-6 property(아래 [testing](testing.md) 11절), core 단위(no-op 판정 표, absorb 허용 필드 = PATCH 허용 필드, 스키마 `record` 속성 = `RECORD_FIELDS`·닫힌 객체)·site-db 통합(같은 트리 + 드래프트 → 재적용·base 이동 → 재실행 no-op), compose(M5-2 수직 경로 뒤에 Updater enrollment 파일로 `absorb` CLI → trailer·fingerprint·바뀐 필드 → `gate` 통과 → 회수한 트리 `project apply --tar` → `base_site_rev == site_rev`, 유효 트리 = 회수한 repo hash·PATCH 값).

### V1 구현 현황 (M5-3b 고객 repo 템플릿·validate-project, 2026-10-30)

고객 project repo를 시작하는 템플릿과, 그 repo의 PR·태그마다 프로젝트를 검증하는 reusable workflow. 근거 plan `pureiron-m5-3.md` K3·K9·K10, foundation plan K6.

- **템플릿** `deploy/templates/pureiron-project/`: `project.yaml`(`project_id: change-me`·`name` 자리값, `schema_version: 1`), Modbus 예제 `devices/plc-1.yaml`(문서용 주소 `192.0.2.10:502`) → `tags/plc-1.yaml`(`line1/temp`) → `alarms/line1.yaml`(`temp-high`)가 서로 참조, `screens/.gitkeep`·`assets/.gitkeep`(예약 디렉터리), `.github/workflows/validate.yml`, 짧은 `README.md`(명령·secret 설정법, 값 없음). 고객별 값은 자리값뿐이다(고객별 분기 금지). `load_project`는 README·`.github/`·예약 디렉터리의 `.gitkeep`을 프로젝트 내용으로 보지 않으므로 content hash는 YAML 4개로만 정해진다. 템플릿 그대로 `validate`·`fmt --check` 통과, hash 결정적(`deploy/tests/test_project_template.py`).
- **`.github/workflows/validate-project.yml`**(`on: workflow_call`, 권한 `contents: read`, action SHA 고정):
  - 입력 `project_dir`(기본 `.`, 호출자 repo 안 상대 경로), `runs_on`(JSON 문자열, 기본 `"ubuntu-24.04"` — 고객 repo는 GitHub-hosted, V1 자기 호출은 `["self-hosted", "g1"]`로 PD-23 유지).
  - secret(선택) `PUREIRON_CI_APP_ID`·`PUREIRON_CI_APP_KEY`: 둘 다 있으면 `actions/create-github-app-token`으로 `circulation95/PureIron_V1` contents read 토큰을 만들고, 없으면 `github.token`(V1 자기 호출). 호출자가 PureIron_V1이 아닌데 둘 중 하나라도 없으면 PureIron_V1 checkout 전에 `PUREIRON_CI_APP_ID·PUREIRON_CI_APP_KEY secret 둘 다 필요`로 멈춘다(각각 있음/없음만 로그에, 값은 안 찍음).
  - 단계: 호출자 repo → `project/` 체크아웃 → 이 workflow 파일이 속한 커밋(`job.workflow_sha`, 없으면 `github.job_workflow_sha`; 40자 hex가 아니면 실패 — 빈 ref로 기본 브랜치 도구를 쓰지 않게)의 PureIron_V1 → `pureiron-v1/` 체크아웃 → `uv sync --package pureiron-project-tools --locked --no-dev` → 경로 확인(`pureiron-project` 실행 파일이 `pureiron-v1/` 아래, 프로젝트 경로가 `project/` 아래가 아니면 실패) → `pureiron-project validate` → `fmt --check` → `hash`. 호출자가 `uses: …@<ref>`로 고정한 ref가 곧 검증 도구 버전이다(별도 ref 입력 없음).
  - 출력 `content_hash`(`sha256:` + 64 hex 형식 확인), `project_version` = 호출자 실행의 ref가 태그이고 이름이 `v[1-9][0-9]*`이면 그 이름(fleet-site `project.version` 형식), 아니면 빈 값. reusable workflow 안의 `github` 컨텍스트는 호출자 실행의 것이다. 둘 다 로그·step summary에도 찍는다.
- **고객 repo 호출**: 템플릿 `validate.yml`이 `pull_request`·`main` push·태그 `v*` push에서 `uses: circulation95/PureIron_V1/.github/workflows/validate-project.yml@master`로 부르고 secret 2개를 넘긴다. 운영 repo는 `@master` 대신 PureIron_V1 릴리스 태그로 고정한다.
- **private repo 접근 조건(foundation plan K6)**: PureIron_V1이 private이라 ① PureIron_V1 Actions access level `user`(같은 계정 소유 private repo만 호출 허용, REST `PUT /repos/{owner}/{repo}/actions/permissions/access`) ② 고객 repo도 `circulation95` 소유 ③ PureIron_V1 체크아웃은 App 토큰(App이 PureIron_V1에 설치돼 있어야 함). 고객 소유 repo가 필요해지면 `validate-project.yml`·project-tools를 공개 repo로 분리한다(K6 대안).
- **App 키 신뢰 경계**: workflow가 토큰 범위를 PureIron_V1 contents read로 좁혀도, 키를 가진 사람은 App 권한 전체로 토큰을 직접 만들 수 있다. App(`pureiron-ci`)은 **PureIron_V1 한 repo에만**(Only select repositories) 설치돼 있고, 권한은 Contents read·Metadata read에 V1 CI용(M0) **Pull requests write·Commit statuses write**(`pull_requests`·`statuses`)가 더해져 있다. 고객 repo에 write 권한이 있는 사람은 workflow를 고쳐 이 키를 꺼낼 수 있으므로 키 = PureIron_V1에 대한 App 권한 전체(코드 읽기 + PR·commit status 쓰기)로 본다. 지금 이를 수용하는 근거: 모든 고객 repo가 `circulation95` 소유이고 write 권한자는 벤더뿐이라, 키를 꺼낼 수 있는 사람이 이미 PureIron_V1에 그 이상의 권한을 가진 사람이다. 전환 조건: 외부인(고객 엔지니어 등 벤더 밖)에게 고객 repo write 권한을 주기 **전에** 둘 중 하나를 고른다 — ① 고객 repo 전용 읽기 전용 App(Contents read만, PureIron_V1에만 설치)을 따로 만들고 모든 고객 repo의 secret을 그 App으로 교체 ② K6 대안(`validate-project.yml`·project-tools를 공개 repo로 분리). 키가 샌 것으로 의심되면 App에서 키를 새로 만들고 모든 고객 repo의 secret을 갱신한다.
- **V1 안 검증**: foundation-ci `template-validate`(같은 workflow를 `./`로 호출, `project_dir: deploy/templates/pureiron-project`) → `template-outputs`(`content_hash` 형식·로컬 `pureiron-project hash`와 같음·`project_version` 빈 값). 외부 repo run(G1-8)은 [testing](testing.md) 11절.
