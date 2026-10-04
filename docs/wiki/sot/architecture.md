---
id: sot-architecture
title: Architecture SOT — 시스템 구조
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - docs/ARCHITECTURE.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/gateway.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/CLAUDE.md
last_verified: 2026-07-04
---

# Architecture SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다.

PureIron은 단일 FastAPI 백엔드 + 단일 React 프론트엔드 + PostgreSQL(TimescaleDB + pgvector) + Redis로 구성된 모놀리식 Web HMI/SCADA 서버다.

모든 백엔드 런타임 상태는 `ScadaGateway` 싱글톤이 보유한다. 라우터와 서비스는 `request.app.state`를 사용하지 않고 `gateway.*`를 경유한다.

제품 의도와 v1 범위는 [product](product.md), 불변 작업 규칙은 [agent-rules](agent-rules.md)를 따른다.

## 이 문서의 원천

- [docs/ARCHITECTURE.md](../../ARCHITECTURE.md) — 통신 레이어, 모듈 책임, 데이터 흐름
- [backend/src/pureiron/gateway.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/gateway.py) — ScadaGateway 싱글톤(런타임 정본)
- [backend/CLAUDE.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/CLAUDE.md) — gateway 속성 표·라이프사이클·DB·락 규율

## 아키텍처 경계

v1 아키텍처는 다음 경계를 지킨다.

- PLC 로직을 수정하지 않는다. 기존 PLC 주소 쓰기는 ADR-013(개정) 가드레일 아래에서만 한다(기본 읽기 전용).
- PureIron은 관제, 알람, 이력, 트렌드, 작업지시, Agent 보조 계층을 제공한다.
- 실제 설비 자동 제어와 사람 승인 없는 자동 조치는 v1 범위가 아니다.
- 선택적 의존성(DB/Redis/RAG/LangGraph)은 실패해도 핵심 모니터링을 중단시키지 않는다.
- Simulator는 개발/영업/검증용 핵심 컴포넌트이며, 장기적으로 3D Twin·Agent-assisted Simulation과 연결된다.

## Core Domain Model Boundary (SoT)

PureIron 코어 도메인의 경계를 **한 번에 읽도록** 여기에 고정한다. 각 도메인은 단일 진실 소스(SoT)가 하나뿐이며, 아래 경계 문장은 silent drift를 막기 위한 불변(invariant)이다. 이 경계가 흐려지면 Agent·Work Order/MES-lite·Digital Twin·Historian 확장에서 계속 회귀가 난다. 세부 계약은 링크된 SoT 문서가 정본이고, 이 절은 경계 문장만 고정한다. 문구·금지 패턴은 `backend/tests/test_core_domain_boundary_contract.py`가 검증한다.

| 도메인 | 단일 진실 소스(SoT) | 경계 문장(불변) | 세부 정본 |
|---|---|---|---|
| Tag | 정의=DB `tag_definitions`(`source_kind` 소유), 값=`TagStore` in-memory | **Tag definition != runtime value** | [TagSourceKind](#tagsourcekind-taxonomy) · [data-contracts](data-contracts.md#tag-정의-계약) |
| Device | 연결/건강 상태=`DriverHealth.snapshot()`(`/api/devices`) | **DeviceHealth is not a tag** | [지원 디바이스 프로토콜](#지원-디바이스-프로토콜-v1) · [backend/CLAUDE.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/CLAUDE.md) |
| Alarm | 현재 상태=`AlarmEngine` 활성 알람, 이력/감사=`AlarmJournal` | **AlarmJournal is audit/history not current state** | [api-events](api-events.md#주요-api-계약-요약) |
| Historian | `tag_history` hypertable(write=구독 append, read=downsample 조회) | **Historian is append/query path** | [data-contracts](data-contracts.md#historian-retention--query-window) |
| Agent Context | tag/alarm/history/health/logs read-only 도구 projection | **Agent is read-only/context projection** | [api-events](api-events.md#chat-sse-이벤트) · [agent-rules](agent-rules.md) |
| Work Order / MES-lite | 미래 business workflow 경계(backend 기능 아님) | Work Order는 tag/alarm의 hidden mutation이 아니다 | [testing](testing.md) AC-18 · [mvp-gap](../reports/mvp-gap.md) |

경계 규칙:

- **Tag definition != runtime value.** `TagDefinition`(source_kind별 DB row 소유)과 `Tag.current_value`(휘발성 런타임 값)를 섞지 않는다. 정의 CRUD는 `/api/tags`, 값 write는 `PUT /api/tags/{path}/value`로 분리된다. 정의는 DB가 SoT이고 런타임 값은 in-memory only다.
- **DeviceHealth is not a tag.** 디바이스 연결/건강은 드라이버 런타임 상태이며 `DriverHealth.snapshot()`이 단일 소스다. 디바이스를 추가해도 사용자는 일반 태그만 추가하고 `[health]` 시스템 태그는 생성하지 않는다(구 `DeviceHealthPublisher` 제거됨).
- **AlarmJournal is audit/history not current state.** 현재 활성 알람은 `AlarmEngine`(`GET /api/alarms/active`)이 소스이고, `AlarmJournal`(`GET /api/alarms/journal`)은 ack/shelve/note 전이를 append하는 감사·이력이다. 저널을 현재 상태 소스로 쓰지 않는다.
- **Historian is append/query path.** Historian은 `tag.changed.*` 구독으로 값을 append하고 downsample 조회를 제공하는 경로다. 현재 값 소스가 아니며, 조회는 read-only여서 write/control 경로와 무관하다(조회창/retention은 [data-contracts](data-contracts.md#historian-retention--query-window)).
- **Agent is read-only/context projection.** Agent 컨텍스트 도구(`tag_tools`/`alarm_tools`/`history_tools`/`health_tools`/`system_log_tools`)는 tag/alarm/history/health/logs에 대한 read-only projection이다. 상태를 바꾸는 도구는 `agents/tools/control_tools.py`(태그 write/시뮬레이션)와 `agents/tools/alarm_action_tools.py`(알람 ack/note)에 격리되어 있고, 프로덕션에서는 스스로 실행하지 않고 approval envelope만 반환한다(사람 승인·RBAC·read-only latch로만 실행). Agent는 액추에이터가 아니다.
- **Work Order / MES-lite is a future business workflow boundary.** Work Order는 v1 backend 기능이 아니라 수동 handoff 데모다([testing](testing.md) AC-18). 미래에 추가되더라도 tag/alarm을 우회해 몰래 바꾸는 hidden mutation 경로가 아니라 사람이 보는 workflow 계층이어야 한다. `mes/`(recipe/plan/task)는 Work Order와 다른 것이다.

## 통신 레이어

| 채널 | 경로 | 용도 |
|---|---|---|
| REST | `/api/*` | CRUD, 인증, 디바이스·태그·UDT·알람·설정 관리 |
| WebSocket | `/ws` | 태그 값 라이브 푸시(250ms 배치) |
| SSE | `/api/chat/conversations/{id}/messages` | Agent 챗봇 토큰·도구 호출·승인 이벤트 스트림 |

엔드포인트·토픽 계약은 [api-events](api-events.md)를 따른다.

## 백엔드 주요 모듈 (`backend/src/pureiron/`)

| Module | Responsibility |
|---|---|
| `gateway.py` | ScadaGateway 싱글톤, 런타임 상태와 라이프사이클 루트 |
| `tags/` | 태그 엔진, memory/OPC-UA/Modbus/S7 provider, 계산 태그 |
| `devices/`, `drivers/` | 디바이스 등록·관리, 프로토콜 드라이버(OPC-UA/Modbus/S7/LS/AB/MELSEC), DriverHealth. 지원 프로토콜 정본은 아래 [지원 디바이스 프로토콜](#지원-디바이스-프로토콜-v1) |
| `alarms/` | 알람 상태머신, 활성 알람, ack/shelve, 저널 |
| `historian/` | 시계열 write/read, Trend 기반 데이터 |
| `events/` | `tag.changed.*` pub/sub 이벤트 버스 |
| `ws/` | WebSocket 연결 관리와 태그 브로드캐스트 |
| `auth/` | JWT, RBAC, 감사 로그 |
| `chat/`, `agents/`, `rag/` | Agent Chat, 도구 호출, 문서/RAG 기반 질의 |
| `models/` | 3D 모델 메타데이터와 Digital Twin 연결 기반 |
| `udt/`, `mes/`, `processes/` | 장기 확장 기능: UDT, MES-lite, 공정 타입 |
| `db/` | asyncpg, SQL 마이그레이션(no ORM) |

세부 API는 [api-events](api-events.md), DB·설정 계약은 [data-contracts](data-contracts.md)를 따른다.

## 지원 디바이스 프로토콜 (v1)

v1은 다음 6개 산업 프로토콜 드라이버를 **직접 구현**해 제공한다. 모두 `drivers/`에 하드코딩하며 동적 로딩·plugin registry로 추상화하지 않는다([agent-rules](agent-rules.md) No plugin system, [adr](../adr/index.md) ADR-002). 각 드라이버는 boot 시 `gateway._setup_drivers()`가 `devices` 테이블의 `type`별로 로드하고, `POST/PUT/DELETE /api/devices`가 런타임에 시작·재시작·중지한다.

| 프로토콜 | Device `type` (`/api/devices`) | 드라이버 | 상태 |
|---|---|---|---|
| OPC-UA | `OPC UA` | `drivers/opcua_client.py` | v1 supported |
| Modbus TCP | `Modbus TCP` | `drivers/modbus_client.py` | v1 supported |
| Siemens S7 | `Siemens S7` | `drivers/s7_client.py` | v1 supported |
| LS Electric XGB/XGI | `LS Electric` | `drivers/ls_client.py` | v1 supported |
| Allen-Bradley ControlLogix | `Allen-Bradley ControlLogix` | `drivers/ab_client.py` | v1 supported |
| Mitsubishi MELSEC | `Mitsubishi MELSEC` | `drivers/melsec_client.py` | v1 supported |

LS/AB/MELSEC는 한국·아시아 현장 영업 데모와 첫 계약 설득력을 위해 v1 범위에 포함한다([product](product.md)). 모든 드라이버 태그는 `DRIVER_DERIVED`로 생성되고, 연결 상태는 태그가 아니라 `DriverHealth.snapshot()`(`/api/devices`)이 단일 소스다. `Simulator`는 드라이버가 아니라 별도 컴포넌트로 `tag_simulator`가 담당한다.

새 프로토콜을 늘리는 것은 2인 팀 유지보수 부담을 키우므로, v1에서는 위 6개로 고정한다. 추가는 product 우선순위 재검토 후 같은 방식(`drivers/`에 직접 구현)으로만 진행한다.

## ScadaGateway 싱글톤

런타임 정본은 `backend/src/pureiron/gateway.py`다. 전체 속성 표는 [backend/CLAUDE.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/CLAUDE.md)를 따른다.

핵심 규칙은 다음이다.

- **라이프사이클:** `setup()` → `start()` → `stop()` 순서로 관리한다.
- **상태 접근:** 라우터는 `from pureiron.gateway import gateway` 또는 FastAPI `Depends`를 사용한다.
- **금지:** `request.app.state` 접근 금지.
- **Graceful degradation:** DB/Redis/RAG/LangGraph 초기화 실패는 경고로 처리하고, 핵심 모니터링은 유지한다.

## TagSourceKind Taxonomy

모든 `TagDefinition`은 `source_kind`로 DB row ownership을 구분한다.

| `source_kind` | 출처 | CRUD 진입점 | Cascade 키 |
|---|---|---|---|
| `AUTHORED` | 사용자 작성 | `POST /api/tags/` | 없음 |
| `UDT_DERIVED` | UDT 인스턴스 expansion | `POST/PUT /api/udt/instances` → `udt/expander.py` | `source_instance_id` |
| `DRIVER_DERIVED` | 디바이스 자동 생성 | 드라이버 startup(idempotent re-seed) | `source_device_id` |

세부 DB 계약은 [data-contracts](data-contracts.md)를 따른다.

## 온톨로지 투영 (Ontology Projection)

`backend/src/pureiron/ontology/`는 **저장소가 아니라 투영**이다. 새 테이블·마이그레이션·REST 표면을 만들지 않고, 이미 존재하는 정본(TagStore · UDT 타입/인스턴스 repo · AlarmEngine · DriverHealth)을 읽어 메모리 그래프로 조립한다. `gateway.ontology`가 소유하며 성격은 `ProcessRegistry`와 같다.

**엔티티 6종:** `device` · `udt_type` · `equipment`(UDT 인스턴스) · `tag` · `alarm` · `process`

**관계 5종(고정):**

| relation | source → target | 근거 |
|---|---|---|
| `instance_of` | equipment → udt_type | `UdtInstance.type_id` |
| `member_of` | tag → equipment | `source_kind=UDT_DERIVED` + `source_instance_id` |
| `provided_by` | tag → device | `source_kind=DRIVER_DERIVED` + `source_device_id` |
| `monitors` | alarm → tag | `AlarmConfig.tag_path` |
| `belongs_to` | tag → process | `TagDefinition.process_type` |

근거 데이터가 없는 추론 관계(예: "같은 라인")는 만들지 않는다. 에이전트가 사실로 단언하는 순간 환각이 되기 때문이다. 같은 이유로 조인 근거가 없으면 엣지를 만들지 않고 `snapshot.warnings`에 사유만 남긴다(예: `devices` 인덱스가 없으면 `provided_by` 생략).

투영은 TTL 캐시로만 갱신하고 `tag.changed.*`를 구독하지 않는다 — 값 변경 고빈도 스트림이라 구독하면 초당 수백 번 재조립하게 된다. 캐시되므로 노드에 태그 현재값을 담지 않는다(정의 vs 런타임 값 경계).

**소비자 3곳:**

1. `agents/nodes/entity_resolver.py` — supervisor가 뽑은 `reference_tags`를 실재하는 경로로 확정하고, 모호하면 고르지 않는다.
2. `agents/tools/ontology_tools.py` — 관계 질의 도구(`describe_entity` · `get_related_tags` · `get_equipment_topology`). 읽기 전용 투영 계약에 묶인다.
3. graph RAG — `expand_tag_paths()`가 검색 대상 태그를 **1홉**(같은 설비의 형제 태그 + 같은 디바이스 태그)까지 넓힌다. 문서는 개별 태그가 아니라 설비 단위로 붙는 경우가 많아, 태그 하나로만 찾으면 전역 검색으로 떨어져 정확도가 무너진다. 벡터 스토어나 인덱싱은 바꾸지 않는다 — 검색 **입력 집합**만 넓힌다.

## 이벤트 버스 · 잠금 규율

- `tag.changed.{path}` — 특정 태그 변경
- `tag.changed.all` — 전체 태그 변경

WebSocket broadcaster, alarm engine, historian writer가 이벤트를 구독한다.

**잠금 규율:** `tag.changed.*` 발행과 DB I/O는 TagStore 락 **해제 후** 실행한다. 락 안에서 DB I/O나 이벤트 발행을 하면 데드락 위험이 있다.

## 데이터 흐름: 태그 쓰기

```text
Frontend setTag()
→ POST /api/tags/{path}
→ router
→ gateway.tag_store.set_value(lock)
→ lock 해제
→ DB write
→ event_bus.publish("tag.changed.{path}")
→ ws_manager broadcast(250ms batch)
→ frontend tagValuesAtom 업데이트
```

## DB 마이그레이션

`backend/src/pureiron/db/migrations/*.sql`을 boot 시 알파벳순 자동 실행한다. 모든 마이그레이션은 멱등이어야 한다.

앱 코드에서 `ALTER TABLE`을 직접 실행하지 않는다. 세부 규율은 [data-contracts](data-contracts.md), 결정 근거는 [adr](../adr/index.md)를 따른다.

## 프론트엔드 경계

프론트엔드 정본은 [frontend](frontend.md)다.

아키텍처 관점에서 중요한 경계는 다음이다.

- React + Vite + TypeScript
- Jotai 기반 상태 관리
- WebSocket singleton을 통한 실시간 태그 반영
- React Three Fiber 기반 3D Digital Twin
- Agent SSE 이벤트와 3D 하이라이트 연결

## 목표 설계(미구현)

> **target design — 아직 구현되지 않았다.** 이 절은 Foundation 재설계(2026-10-04)로 PureIron_V1에 만들 구조다. 위 절들의 현행 사실(legacy 기준)과 섞어 읽지 않는다. 결정 근거는 [ADR](../adr/index.md) 019~028. 구현되면 해당 내용을 현행 절로 옮기고 이 절에서 지운다.

### 서비스 구성 (ADR-019)

| 서비스 | 역할 | 비고 |
|---|---|---|
| `collector` | 장비 통신(1차 Modbus TCP), `ValueBatch`·`DeviceHealth` 송신 | 무상태, DB 접근 없음, 60s 메모리 링버퍼 |
| `core` | 태그·알람·이력·인증·감사·실시간(WS)·장비 상태·프로젝트 적용, REST `/api/v1` | 유일한 조립 지점 `SiteRuntime.build(settings)` → `create_app(rt)`, 생성자 주입 |
| `web` | nginx + React(로그인 + 태그 현황·알람/ACK·트렌드·장비 상태·감사 로그) | core OpenAPI에서 타입 생성 |
| `site-db` | TimescaleDB PG16 (`pureiron/site-db:<ver>`로 retag) | core readiness 필수 |
| `ai` | core read-only API 소비 | 계약만, 첫 완료 범위 제외 |
| Updater | 호스트 systemd 서비스(컨테이너 아님) | 번들 검증·백업·마이그레이션·전환·롤백 |

- core 모듈: `platform`(db·settings·bus·migrate), `acquisition`, `tags`, `alarms`, `historian`, `auth`, `audit`, `realtime`, `devices`, `project`, `api` + `runtime.py`·`app.py`·`cli.py`.
- import 계약(import-linter): 도메인·`platform`·`api`는 `runtime`·`app`을 import하지 않는다(진입점 `cli`만), 계층 `api` > 도메인 > `platform`, 도메인 간은 `<module>.ports`만.
- 이벤트 pub/sub(ADR-010)은 core 프로세스 내부에서만 쓴다.

### 통신 경계 (ADR-020)

- collector ↔ core: gRPC 양방향 스트림 `Acquisition.Session`(core가 서버). core → `AcquisitionPlan`/`Ack`, collector → `Hello`/`ValueBatch`/`DeviceHealth`.
- Ack는 core in-memory ingest 직후. collector 링버퍼는 core 다운·단절 구간만, DB 장애는 core historian 상한 버퍼(600,000행)가 담당.
- `replay=true` batch는 historian에만 기록(deadband·알람·WS 우회), 중복은 `UNIQUE(tag_id, time)`로 흡수.
- 무신호 `max(3×poll, 5s)` → 장비 태그 BAD, 장비 DISCONNECTED.

### 배포·중앙 (ADR-022, ADR-023)

- 배포 정본: 벤더 repo `pureiron-fleet`의 `sites/<site>.yaml`(generation 카운터). PR 승인·merge = 배포 승인 → 서명 번들 `.pib` 게시.
- 중앙 = 벤더 내부 도구: 작은 `control-api`(등록·자격증명·heartbeat·desired·reports·drafts·fleet) + 읽기 전용 `control-web`(basic auth, HTTPS 뒤). 현장 → 중앙 polling만 있다.
- 현장 Updater 적용 단계: `fetched` → `verified` → `committed` → `locked` → `backed_up` → `migrating` → `switching` → `checking` → `succeeded`, 실패 시 `rolling_back`/`failed`. 상태 파일은 fsync 후 rename, systemd 재시작 시 `recover()`가 끝까지 수렴시킨다.
- 폐쇄망 현장도 같은 `.pib`를 USB 등으로 반입해 같은 검증을 거친다.

### V1 구현 현황 (M1 skeleton, 2026-10-04)

M1에서 PureIron_V1에 **골격으로** 들어간 것. 기능 본구현은 표의 다음 단계에서 채운다.

| 구성 | M1에 있는 것 | 다음 단계 |
|---|---|---|
| core 조립 | `services/core/src/pureiron_core/runtime.py`(`SiteRuntime.build`) → `app.py`(`create_app`), `cli.py`(`serve`·`migrate`·`project`), import-linter 계약 3종 + 음성 fixture(`services/core/.importlinter`) | M3a/b에서 도메인 모듈 추가 시 계약에 등록 |
| core 모듈 | `platform`(db·bus·migrate·migrate_lint·sqltext), `acquisition`(gRPC 세션 골격), `project`(project_state·edit-lock stub), `api`(health·project) | `tags`·`historian`(M3a), `alarms`·`auth`·`audit`·`realtime`·`devices`(M3b) |
| collector | `Hello` 송신 → plan 수신, `WriteRequest`는 아직 항상 `REJECTED`(쓰기 경로 미구현), 빈 batch heartbeat, 지수 backoff 재연결 | Modbus 드라이버·링버퍼·`DeviceHealth`(M3a), ADR-013 가드레일을 갖춘 쓰기 경로(별도 마일스톤) |
| web | readyz 상태 화면 1개, OpenAPI 생성 타입(`src/api/schema.d.ts`), nginx `/api` 프록시 | 로그인 + 5개 화면(M3b) |
| updater·control-api·bundle-tools | workspace 멤버 자리(버전 상수·테스트), control-api는 `GET /api/v1/healthz` 계약만 | M2(updater·bundle-tools), M4(control-api) |
| compose | `deploy/compose/site.compose.yml`(`name: pureiron`, `pull_policy: never`, 비밀은 파일만) + dev override | M2 Updater가 `g<gen>.yml`로 렌더 |
