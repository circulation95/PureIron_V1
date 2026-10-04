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
- epoch: 마이그레이션을 포함한 release만 +1. 앱 ready 조건 `app_expected ≤ 현재 epoch ≤ app_max_compatible(= expected + 1)`. 1 epoch 초과 점프는 `E_SCHEMA`(단계 적용).
- baseline 스키마: `schema_meta`, `tags`(`id`, `path unique` — path 기준 안정 id), `tag_history`(1일 chunk hypertable, `UNIQUE(tag_id, time)`, 2일 후 압축), 1분 CAGG(이후 1시간), `project_state`(`project_id, version, content_hash, base_site_rev, site_rev, edit_lock_owner, edit_lock_gen`).

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
