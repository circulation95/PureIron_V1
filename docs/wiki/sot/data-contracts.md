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
- 특정 고객/SI PoC의 retention 조정은 앱 코드가 아니라 배포 결정이다. 조정/확인 SQL은 [on-prem read-only PoC runbook](../../runbooks/on-prem-read-only-poc.md) §8을 따른다.

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
- baseline 스키마: `schema_meta`, `tags`(`id`, `path unique` — path 기준 안정 id), `tag_history`(1일 chunk hypertable, `UNIQUE(tag_id, time)`, 2일 후 압축), 1분 CAGG, `project_state`(`project_id, version, content_hash, base_site_rev, site_rev, edit_lock_owner, edit_lock_gen`). epoch 2(r1)에서 1시간 CAGG `tag_history_1h`를 더했다(아래 CAGG 조회 계약).

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
# tags/<device>.yaml: [{ path, device, modbus:{register_type,address,data_type,byte_order,word_order}, eu?, description?, record?:{deadband,max_interval_s} }]
# alarms/*.yaml     : [{ id, tag, type: above|below, setpoint, deadband, on_delay_s, priority, description? }]
```

- 정규화: YAML 1.2 strict(중복 키·anchor·커스텀 tag 거부), 숫자 RFC 8785, 기본값 생략, POSIX 상대 경로, UTF-8 NFC, LF, 키 정렬. `content_hash = sha256(정렬된 (경로, 정규화 내용))`, 버전 = 고객 repo 태그 `vN`.
- 현장 드래프트: `PATCH`마다 `site_rev += 1`(리셋 없음), 적용 시 `base_site_rev = site_rev`. 배포 게이트: `absorbs[site]`가 있으면 `site_rev ≤ absorbs[site]`, 없으면 `site_rev == base_site_rev`, 아니면 `E_DRAFT`.
- 비밀값은 `secret://<name>` 참조만.
- legacy 변환기 1차 범위: `project.json`, Modbus TCP 연결·태그, 상·하한 알람. OPC-UA·UDT·scripts·dashboard·expression/query 태그·shelve·`auth/users.json`은 **미지원으로 보고**.

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
- **CAGG 조회 계약**(1분·1시간 공통):
  - `samples` = bucket의 행 수(`count(*)`, `value_num`이 NULL인 bool·BAD 행 포함)라 `avg(value_num)`의 분모와 다르다. 가중 롤업에 쓰지 않는다(필요하면 다음 epoch에 `count(value_num)` 열을 더한다).
  - `materialized_only`(기본 true)라 policy가 아직 채우지 않은 구간은 원본으로 돌아가 읽지 않는다. 1시간 CAGG의 최신 구간은 `end_offset` 1시간 + 주기 1시간으로 최대 약 2시간 늦다 — 소비자(M3a historian)는 최근 구간을 1분 CAGG·원본과 합쳐 읽는다.
  - refresh policy 범위: 1분 `[now-1일, now-1분]`, 1시간 `[now-3일, now-1시간]`. 이 범위보다 오래된 구간은 policy가 채우지 않는다. **업그레이드 이전 이력의 1시간 CAGG backfill은 아직 없다**(→ M3a-3에서 불필요로 닫음: r0·r1에는 historian이 없어 이력 행 0, 아래 M3a-3 절) — 1시간 CAGG를 읽는 쪽(M3a `GET /history?agg=1h`)과 함께 backfill 명령·실행 시점(Updater `succeeded` 뒤 부수 효과 또는 core 명령)을 정한다. 그 전까지 r0→r1 현장의 1시간 CAGG는 업그레이드 시점 3일 전부터만 있다(현재 운영 현장 없음).
- 통합 테스트: 압축 chunk가 있는 epoch 1 DB → `migrate up` → **refresh policy job**이 채운 1시간 CAGG를 원본 직접 집계와 bucket마다 대조(값이 분마다 바뀌고 BAD 행 포함, policy 범위 밖은 비어 있음), 0006·0007 사이 crash 상태(구문 실행·기록 전 / 0006만 기록)에서 `up` 수렴. r1 번들 실측: 739 MB, 빌드 15 s(WSL2).
- **n-minus-1**: 게이트는 N-1 테스트와 별개로 N-1 이미지의 `pureiron-core migrate status --json`이 `cur == N`·envelope 안·checksum 정상인지 직접 본다(롤백된 N-1 앱이 ready가 되는 근거). N-1 core-test 이미지는 동결이라 고칠 수 없으므로, 앱 동작이 아니라 스키마 모양을 지나치게 좁게 단정한 테스트만 `deploy/build/n_minus_1_waivers.json`(N-1 release × 정확한 pytest node id × 사유 × 기대 실패 정규식 `expect`)으로 deselect 한다. 게이트는 waiver 테스트를 따로 돌려 **여전히 그 이유로 실패하는지** 확인하고(통과하거나 다른 이유로 실패하면 → 게이트 실패), 본 실행은 통과 ≥ 1·deselect 수 == waiver 수·실패 0이어야 한다. 기존 DB 모드에서 `fresh_db`·`compose` 테스트의 skip은 설계상 예외다(새 DB·compose 스택이 필요한 테스트라 N 스키마 DB에서 돌 수 없다) — 그 공백은 envelope 직접 판정이 메운다. 첫 항목: r0 `test_baseline_timescale_objects`(refresh policy 전역 개수 == 1, epoch 2에서 2). r1부터 같은 검사는 CAGG별 개수로 센다 — 스키마 단정은 다음 epoch가 객체를 더해도 성립하게 쓴다.
- **아직 없음**: (1시간 CAGG backfill은 M3a-3에서 불필요로 닫음.) (Hyper-V VM 실측은 아래 M2d VM 절.) Updater `schema_epoch`를 migrate 결과(`migrate status`의 `cur`)로 맞추는 보정 — migrate가 DB에는 다 적용하고 0 아닌 코드로 끝나면 상태(1)와 DB(2)가 어긋나 다음 epoch 번들을 `E_SCHEMA`로 거부할 수 있다(M2b 설계 이월).

### V1 구현 현황 (M2d Hyper-V VM 실측, 2026-10-05)

- **하네스**: `tests/e2e`(PD-24) — Ubuntu 24.04 클라우드 이미지 → golden VHDX(읽기 전용) 위 차분 디스크, Gen2 + Secure Boot(MicrosoftUEFICertificateAuthority), cloud-init seed ISO, 내부 전용 vSwitch(전달 없음). 단계 `prepare → vm → install → scenarios → crash → report`, 판정·실행 방법의 정본은 [`tests/e2e/README.md`](../../../tests/e2e/README.md).
- **plan M2 종료 조건 실측(run `20261005T055523Z`, 30/30 PASS)**:
  - 폐쇄망 부트스트랩: runbook 2단계(root 셸, 메타데이터만 추출 → `bootstrap-verify.sh` sha256·root 지문) → `install.sh --offline-time-confirmed` → enroll → g1(r0) 51 s, `/readyz` 200, overlay2 + classic image store, 실행 이미지 `.Id` == r0 config digest.
  - 거부(VM 수동 1회): 다른 현장 `E_SITE`·변조 `E_DIGEST`·낮은 gen `E_GEN` exit 1, 같은 gen 재빌드는 R-7대로 no-op exit 0 — 모두 core `StartedAt`·실행 이미지 불변. 커밋 전 거부 보고는 gen을 믿지 않아 `g unknown`이라 번들 sha256 12자로 찾는다. 먼저 빌드한 낮은 gen 번들은 cert 단계 `max_seen_created_at` 역행(`E_CERT`)에 먼저 걸린다.
  - 압축 chunk 3개가 있는 epoch 1 DB에서 r0→r1 24 s(`switching` 11 s, `migrating` 2 s) → epoch 2·`tag_history_1h`, 이력 행 보존.
  - r1′(collector 세션 없음) → `E_HEALTH`, 134 s 안에 r0 digest 복귀 + `failed`(5분 이내, `checking` 90 s·`rolling_back` 19 s).
  - crash 18회(phase `fetched`~`rolling_back` × `systemctl kill -s KILL` / `Restart-VM -Type Reset`): 모두 15~106 s 안에 수렴 — 결과·`applied_gen`·실행 이미지·편집 잠금 해제·경보 없음·이력 행 보존. 감시자가 phase 저장 직후 서비스 cgroup을 얼리므로 `migrating`은 "기록 뒤·다음 기록 전"을 끊는다(SQL 중간 중단은 core 통합 테스트 몫).
  - nftables 외부 접속 시도(설치 전 기준값·설치 이후 누적, 복원·리셋 직전마다 누적) 0. 외부 DNS 패킷은 모두 systemd-resolved가 보내므로 하네스는 resolved 디버그 로그의 조회 이름을 출처 증거로 남긴다.
  - 번들 크기·빌드 시간은 `bundle-metrics.json`(install 80 MB, 현장 번들 739 MB 중 site-db 599 MB, 빌드 약 58 s).
- **실측이 찾은 제품 결함(이 PR에서 수정)**:
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
- **쓰기**(`historian.writer`): 메모리 버퍼 상한 600,000행(넘치면 가장 오래된 행부터 버림, `dropped_total`. 쓰는 중 최대 `FLUSH_MAX_ROWS` 추가 — 순간 최대 620,000행), 1 s마다 최대 20,000행씩 `INSERT … ON CONFLICT (tag_id, time) DO NOTHING`(replay 중복 흡수, 시각은 µs). 실패하면 꺼낸 행을 순서대로 버퍼 앞에 되돌리고 상한을 다시 적용한다 — DB 장애 동안은 이 버퍼가, core 단절 동안은 collector 링버퍼(60 s)가 이력을 든다. 한 번의 쓰기 전체(연결 획득·INSERT·연결 반환 대기)는 15 s(INSERT 쿼리 제한 10 s + 여유 5 s)를 넘으면 취소해 실패로 본다 — half-open DB 연결에서도 flush가 그 이상 멈추지 않고 행은 다음 주기에 다시 쓴다(`db_ok=false`). 취소된 연결의 정리는 asyncpg가 백그라운드에서 계속할 수 있다. 정지 순서는 수집 정지 → 최종 flush(5 s 제한) → DB close.
- **허용 손실(문서화)**: core 프로세스 crash 시 아직 쓰지 못한 버퍼(정상 시 ≤ 1 flush 주기분, DB 장애 중이면 상한 안의 전체), 버퍼 상한 초과분(가장 오래된 것부터), collector 링버퍼 60 s를 넘는 core 단절 구간.
- **조회**(`historian.reader`): raw(`tag_history`)·1m·1h(CAGG) + 구간 길이로 고르는 `auto_resolution`(≤ 1 h raw, ≤ 24 h 1m, 그 이상 1h). HTTP 이력 API·Trend 화면은 인증과 함께 M3b.
- **검증**: compose 네트워크 분리 replay는 통합 테스트 `test_network_disconnect_replay`, 2,000태그 1 s 부하 기록은 `g1.sh perf-ingest` — 판정식은 테스트 docstring(`services/core/tests/integration/test_compose_stack.py`), 실행 job은 [testing](testing.md) `integration-compose`·`perf-ingest`.
- **아직 없음**: `GET /history`(M3b), retention 적용(M5).
