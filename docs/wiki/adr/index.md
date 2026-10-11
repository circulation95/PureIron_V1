---
id: adr-index
title: ADR — 아키텍처 결정 기록
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - docs/wiki/sot/product.md
  - docs/wiki/sot/agent-rules.md
  - docs/wiki/sot/architecture.md
  - docs/wiki/sot/data-contracts.md
  - docs/wiki/sot/api-events.md
  - docs/wiki/sot/frontend.md
  - docs/wiki/sot/testing.md
last_verified: 2026-10-04
---

# Architecture Decision Records

이 문서는 PureIron의 **바꾸면 안 되는 핵심 결정과 그 이유**를 모아두는 ADR 정본이다.

세부 규칙과 현재 계약은 각 SOT 문서가 정본이다. 이 ADR은 “무엇을 해야 하는가”보다 “왜 그렇게 결정했는가”를 기록한다.

## 1. ADR 경계

| 문서 | 역할 |
|---|---|
| `docs/wiki/adr/index.md` | 결정 근거와 트레이드오프 정본 |
| `docs/wiki/sot/agent-rules.md` | Agent가 따라야 할 불변 규칙 요약 |
| `docs/wiki/sot/architecture.md` | 시스템 구조와 런타임 경계 |
| `docs/wiki/sot/data-contracts.md` | DB·설정 계약 |
| `docs/wiki/sot/api-events.md` | REST/WebSocket/SSE/event 계약 |
| `docs/wiki/sot/frontend.md` | frontend 구조와 제약 |
| `docs/wiki/sot/testing.md` | 검증 계층과 게이트 |

새 결정은 기능 설명이 아니라 **결정 / 이유 / 트레이드오프 / 관련 SOT** 형식으로 추가한다.

## 2. 제품·사업을 기준으로 한 최상위 결정

PureIron v1은 기술 과시용 플랫폼이 아니다. 첫 계약 가능성과 고객 설득력을 위해 다음 방향을 고정한다.

| ADR | 결정 | 한 줄 근거 |
|---|---|---|
| 013 | PLC 로직은 바꾸지 않고, 기존 주소 쓰기는 고객 승인·가드레일 아래 현장별로 연다(기본 읽기 전용) | 설비 안정성을 지키면서 MES(운전 조건·명령 내려주기)로 확장한다 |
| 014 | Simulator·3D Twin·Agent는 데모/운영 보조 계층이다 | 고객 설득력은 높이되 자동 제어 리스크는 v1에서 자른다 |
| 015 | 현장 구축형 솔루션으로 시작한다 | 완성형 SaaS보다 첫 유료 계약 가능성이 높다 |
| 016 | SOT/projection/generated 문서 경계를 분리한다 | 2인 팀이 문서 drift를 감당하려면 정본 위치가 명확해야 한다 |
| 017 | Operator UI를 우선하고 마케팅 UI를 배제한다 | 매일 쓰는 SCADA 도구는 장식보다 가독성과 조작성이다 |

## 3. 기술 ADR 요약

| ADR | 결정 | 한 줄 근거 |
|---|---|---|
| 001 | No ORM | 쿼리 투명성, asyncpg native 성능, PostgreSQL 고유 기능 직접 활용 |
| 002 | No plugin system | 플러그인 인프라 비용이 MVP 가치보다 크다 |
| 003 | No premature abstract base classes | 단일 구현 추상화는 가독성과 실행 속도를 해친다 |
| 004 | YAML + REST config only | SCADA 구성은 텍스트 diff와 버전 관리가 중요하다 |
| 005 | Fixed expression function set | 임의 함수 확장은 보안·성능·디버깅 리스크가 크다 |
| 006 | ~~Auto-migrations on boot~~ — **Superseded by 024**(legacy 경로에만 유효) | 단일 컨테이너 부팅과 재시작 안전성을 우선한다 |
| 007 | ScadaGateway singleton, no `app.state` (**개정**: 새 경로는 단일 조립 지점 + 생성자 주입) | typed runtime root가 테스트·리팩터링에 유리하다 |
| 008 | Graceful degradation (**개정**: 선택 서비스 = ai·중앙, site-db는 필수) | 부분 장애에도 핵심 모니터링은 유지해야 한다 |
| 009 | TagStore lock discipline | 락 안 I/O와 이벤트 발행은 데드락 위험이 크다 |
| 010 | Event-driven tag updates | polling보다 변경분 pub/sub이 확장성과 효율이 좋다 |
| 011 | Frontend no `as any` / TS suppression | 타입 우회는 운영 UI 런타임 버그로 이어진다 |
| 012 | Tag definition = DB SoT, runtime value = volatile (**개정**: 정본 = 고객 project repo Git) | 정의는 일관성이 중요하고 값은 디바이스 재읽기로 복구 가능하다 |
| 018 | Device health is not a tag | 연결 상태는 드라이버 런타임 상태이지 사용자 태그 정의가 아니다 |

**Foundation 재설계 ADR (2026-10-04, 상태: Accepted — target design(미구현))** — PureIron_V1의 새 경로에 적용한다. legacy(PureIron_ignition@legacy-final) 코드에는 기존 ADR이 그대로 유효하다.

| ADR | 결정 | 한 줄 근거 |
|---|---|---|
| 019 | 현장 서비스 분할(collector/core/ai/web) + 단일 조립 지점 | 장비 장애 격리, 배포 경계 = 서비스 경계 |
| 020 | collector↔core gRPC 계약·Ack·replay | 무상태 collector, 손실·중복 경계 명시 |
| 021 | TimescaleDB 유지·기록 정책(압축·CAGG·프로젝트별 retention) | 검증된 저장소, 보존 요구는 고객별 |
| 022 | DesiredState GitOps·generation 카운터 | PR 승인 = 배포 승인, 감사 가능한 번호 |
| 023 | 단일 서명 번들·신뢰 루트·이미지 신원(config digest) | 온라인·폐쇄망 동일 검증 경로 |
| 024 | 확장형 마이그레이션·epoch envelope·백업 경계(006 대체) | 구 앱이 새 스키마에서 동작해야 롤백이 안전 |
| 025 | 프로젝트 정본 = 고객 Git repo, 드래프트 회수(단조 site_rev) | 현장 수정이 조용히 덮어써지는 사고 차단 |
| 026 | repo 레이아웃(PureIron_V1)·고객별 repo | legacy 무접촉, 고객별 권한·이력 분리 |
| 027 | 지원 호스트 = Ubuntu 24.04 x86_64, venv Updater | 지원 조합 하나로 실측 검증 |
| 028 | 3단 게이트(G1/G2/G3)·generated ≠ authored·runner 격리 | 배포 결함은 VM e2e로만 잡힌다 |

---

## ADR-001: No ORM

**결정**
SQLAlchemy, Tortoise, Piccolo 같은 ORM을 도입하지 않는다. DB 접근은 `asyncpg` raw SQL을 사용한다.

**이유**

- TimescaleDB, pgvector, JSONB, `ON CONFLICT`, idempotent migration 등 PostgreSQL 고유 기능을 직접 써야 한다.
- SCADA/IIoT 계열에서는 쿼리의 실제 비용과 동작을 숨기지 않는 것이 중요하다.
- ORM 추상화는 초기 2인 팀에서 디버깅 비용을 늘릴 수 있다.

**트레이드오프**

- query boilerplate가 늘어난다.
- type-safe query builder가 없으므로 테스트와 코드 리뷰로 보완해야 한다.

**관련 SOT**: [data-contracts](../sot/data-contracts.md), [testing](../sot/testing.md)

## ADR-002: No Plugin System

**결정**
동적 드라이버 로딩, third-party plugin registry, plugin lifecycle API를 만들지 않는다. 지원 프로토콜은 `backend/src/pureiron/drivers/`에 직접 구현하며, 새 프로토콜도 같은 방식으로 직접 추가한다. v1 지원 프로토콜은 OPC-UA / Modbus TCP / Siemens S7 / LS Electric / Allen-Bradley ControlLogix / Mitsubishi MELSEC 6종이며, 정본 표는 [architecture](../sot/architecture.md#지원-디바이스-프로토콜-v1)에 둔다. "No plugin system"은 프로토콜 수를 3종으로 제한한다는 뜻이 아니라 plugin/registry 추상화를 만들지 않는다는 뜻이다.

**이유**

- v1 고객은 “플러그인을 직접 작성할 수 있는가”보다 “OPC-UA/Modbus/S7/LS/AB/MELSEC 등 현장 연결이 되는가”를 본다. 특히 한국·아시아 현장에서는 LS Electric·Mitsubishi 지원이 첫 계약 설득력에 직접 기여한다.
- plugin isolation, versioning, lifecycle, security model은 MVP 비용이 크다.
- 산업 프로토콜 추가 빈도는 일반 SaaS extension보다 낮다.

**트레이드오프**

- 새 프로토콜을 추가할 때 core code 수정이 필요하다.
- 장기적으로 외부 개발자 생태계를 열려면 별도 재검토가 필요하다.

**관련 SOT**: [architecture](../sot/architecture.md), [agent-rules](../sot/agent-rules.md)

## ADR-003: No Premature Abstract Base Classes

**결정**
구현체가 하나뿐인 모듈에는 abstract base class나 protocol을 먼저 만들지 않는다. 구현체가 2개 이상이 되거나 테스트 격리가 실제로 필요할 때 도입한다.

**이유**

- 한 번만 쓰는 추상화는 코드 탐색 비용을 늘린다.
- 2인 팀에서는 간단한 직접 구현이 더 빠르고 유지보수도 쉽다.
- “언젠가 필요할 수도 있음”은 v1에 추상화를 추가할 충분한 이유가 아니다.

**트레이드오프**

- 두 번째 구현체가 생기면 사후 리팩터링 비용이 든다.
- 단, 실제 요구가 생긴 뒤 리팩터링하는 비용이 지금 추상화를 유지하는 비용보다 낮다고 판단한다.

**관련 SOT**: [architecture](../sot/architecture.md)

## ADR-004: YAML + REST Config Only

**결정**
태그, UDT, 알람, 디바이스 구성은 YAML/JSON 파일 또는 REST API로 관리한다. v1에서 drag-and-drop UI builder나 복잡한 config UI를 만들지 않는다.

**이유**

- SCADA 구성은 변경 이력과 diff가 중요하다.
- UI builder는 개발 비용이 크고 첫 계약 설득에 직접 기여하는 정도가 낮다.
- REST API가 있으면 필요한 최소 운영 화면은 만들 수 있다.

**트레이드오프**

- 비개발자 운영자는 YAML/JSON 구조를 직접 다루기 어렵다.
- 향후 고객 현장 반복 패턴이 쌓이면 제한된 admin UI는 검토할 수 있다.

**관련 SOT**: [data-contracts](../sot/data-contracts.md), [runtime-config](../sot/runtime-config.md)

## ADR-005: Fixed Expression Function Set

**결정**
계산 태그 expression 함수는 고정 집합으로 유지한다. 사용자 정의 expression function registry를 만들지 않는다.

**이유**

- 임의 함수 등록은 보안, 성능, 재현성 문제를 만든다.
- 고객 현장 expression은 대체로 산술, 비교, 논리, 간단한 수학 함수로 충분하다.
- 복잡한 로직은 sandbox scripting으로 분리하는 편이 더 명확하다.

**트레이드오프**

- 고급 계산 로직은 expression만으로 표현하기 어렵다.
- scripting 계층과 테스트가 필요하다.

**관련 SOT**: [agent-rules](../sot/agent-rules.md), [testing](../sot/testing.md)

## ADR-006: Auto-migrations on Boot

**상태**: **Superseded by [ADR-024](#adr-024-확장형-마이그레이션--epoch-envelope--백업-경계)** — Foundation 재설계(2026-10-04). 아래 결정은 legacy(PureIron_ignition@legacy-final) 경로에만 유효하다. 새 경로는 부팅 시 자동 마이그레이션을 하지 않고 Updater가 `pureiron-core migrate`를 호출한다.

**결정**
`backend/src/pureiron/db/migrations/*.sql`을 boot 시 알파벳순으로 자동 실행한다. 모든 migration은 멱등이어야 하며 forward-only를 기본으로 한다.

**이유**

- 초기 배포는 단일 컨테이너/소규모 현장 중심이다.
- 별도 migration orchestration 도구는 운영 부담을 늘린다.
- 멱등 SQL이면 재시작과 반복 부팅이 안전하다.

**트레이드오프**

- down migration 자동화는 없다.
- 운영 DB 변경 전에는 migration review와 backup 절차가 필요하다.

**관련 SOT**: [data-contracts](../sot/data-contracts.md), [testing](../sot/testing.md)

## ADR-007: ScadaGateway Singleton, No `app.state`

**결정**
백엔드 런타임 상태는 `pureiron.gateway.gateway` 싱글톤에 집중한다. production code에서 `request.app.state` 접근은 금지한다.

**이유**

- FastAPI `app.state`는 untyped bag이라 IDE 지원과 리팩터링 안정성이 낮다.
- `ScadaGateway`가 lifecycle root가 되면 선택 서비스 초기화와 graceful degradation을 한 곳에서 관리할 수 있다.
- Agent/개발자가 상태 위치를 추정하지 않아도 된다.

**트레이드오프**

- 테스트에서 gateway monkeypatch가 필요할 수 있다.
- 멀티 인스턴스 상태 공유는 Redis/DB 등 외부 계층으로 분리해야 한다.

**개정 (Foundation 재설계(2026-10-04), 새 경로 — target design(미구현))**
새 경로(core)는 전역 싱글톤을 두지 않는다. 유일한 조립 지점 `SiteRuntime.build(settings)`가 런타임을 만들고 `create_app(rt)`에 넘기며, 모든 의존성은 **생성자 주입**이다. 모듈에서 전역 상태를 조회(`gateway.*` 같은 import-time 싱글톤)하지 않고, `app.state` 금지는 그대로 유지한다. typed runtime root라는 원래 이유는 `SiteRuntime`이 이어받는다. 근거·경계 검사는 [ADR-019](#adr-019-현장-서비스-분할--collector--core--ai--web).

**관련 SOT**: [architecture](../sot/architecture.md), [testing](../sot/testing.md)

## ADR-008: Graceful Degradation

**결정**
DB, Redis, RAG, LangGraph, external LLM 등 선택 서비스 초기화가 실패해도 가능한 범위에서 서버는 계속 뜬다. 핵심 모니터링 기능을 우선 유지한다.

**이유**

- 현장 운영 시스템은 부분 장애 때문에 전체 관제가 죽으면 안 된다.
- 첫 계약/데모 환경에서도 외부 서비스 준비가 완벽하지 않을 수 있다.
- 고객에게 “설비를 불안정하게 만들지 않는다”는 신뢰가 중요하다.

**트레이드오프**

- 일부 기능은 503 또는 degraded 상태를 반환한다.
- UI/API가 현재 비활성 기능을 명확히 보여줘야 한다.

**개정 (Foundation 재설계(2026-10-04), 새 경로 — target design(미구현))**
새 경로에는 Redis가 없다. 선택 서비스 = `ai` 서비스와 중앙(control-api) — 둘이 없어도 현장 모니터링·알람·이력은 동작한다. collector가 다운되면 core는 죽지 않고 해당 장비 태그를 BAD, 장비를 DISCONNECTED로 표시한다. 반면 **site-db는 선택이 아니라 core readiness의 필수 조건**이다(`/readyz`는 DB 연결과 스키마 호환 범위를 확인). 관련: [ADR-019](#adr-019-현장-서비스-분할--collector--core--ai--web), [ADR-020](#adr-020-collector--core-grpc-계약--ack--replay-의미론).

**관련 SOT**: [architecture](../sot/architecture.md), [runtime-config](../sot/runtime-config.md), [testing](../sot/testing.md)

## ADR-009: TagStore Lock Discipline

**결정**
TagStore lock은 in-memory 상태 변경에만 사용한다. DB I/O와 `event_bus.publish()`는 lock 해제 후 실행한다.

**이유**

- lock 안에서 DB나 이벤트 구독자를 호출하면 재진입/대기 상황에서 데드락 위험이 커진다.
- WebSocket broadcaster, alarm engine, historian writer가 tag event를 구독하므로 lock 경계가 명확해야 한다.

**트레이드오프**

- in-memory 상태와 DB 사이에 짧은 eventual consistency window가 생긴다.
- 테스트는 이 경계를 전제로 작성해야 한다.

**유지 (Foundation 재설계(2026-10-04))**: 새 경로 core `tags/`에도 그대로 적용한다(DB I/O·이벤트 발행은 락 밖).

**관련 SOT**: [architecture](../sot/architecture.md), [api-events](../sot/api-events.md), [testing](../sot/testing.md)

## ADR-010: Event-driven Tag Updates

**결정**
태그 값 변경은 `tag.changed.{path}`와 `tag.changed.all` 이벤트로 전파한다. WebSocket, alarm engine, historian writer는 이 이벤트를 구독한다.

**이유**

- polling은 CPU와 네트워크 비용이 크고 지연이 불필요하게 커진다.
- 새 구독자를 추가해도 TagStore core를 크게 바꾸지 않아도 된다.
- UI는 변경분 batch를 받아 더 효율적으로 갱신할 수 있다.

**트레이드오프**

- 이벤트 순서, 중복, 실패 처리는 구독자 책임이다.
- 정의 CRUD와 값 변경 이벤트를 혼동하면 안 된다.

**유지 (Foundation 재설계(2026-10-04))**: 새 경로에서는 **core 프로세스 내부(in-process) 범위**의 이벤트 전파에 한정한다. 서비스 간(collector↔core)은 gRPC 계약([ADR-020](#adr-020-collector--core-grpc-계약--ack--replay-의미론))이다.

**관련 SOT**: [api-events](../sot/api-events.md), [frontend](../sot/frontend.md)

## ADR-011: Frontend TypeScript Strictness

**결정**
Frontend에서 `as any`, `@ts-ignore`, `@ts-expect-error`, tsconfig strictness 약화를 금지한다. `npm run lint`(`tsc --noEmit`) 통과를 최소 게이트로 둔다.

**이유**

- 운영자 UI의 런타임 버그는 현장 신뢰를 깎는다.
- API/SSE payload는 contract drift가 생기기 쉬우므로 타입으로 조기에 잡아야 한다.
- 타입 우회는 초기에는 빨라 보여도 장기 유지보수 비용을 늘린다.

**트레이드오프**

- 외부 라이브러리 타입이 부족할 때 wrapper/type guard 작성 비용이 생긴다.
- 빠른 화면 실험에서도 타입 경계를 유지해야 한다.

**관련 SOT**: [frontend](../sot/frontend.md), [testing](../sot/testing.md)

## ADR-012: Tag Definition DB SoT, Runtime Value Volatile

**결정**
`TagDefinition`은 DB가 단일 진실 소스다. `Tag.current_value`는 in-memory runtime value이며 재시작 시 휘발될 수 있다.

**이유**

- 태그 정의는 이력, 권한, UDT/driver-derived ownership, multi-process 일관성이 중요하다.
- 런타임 값은 디바이스나 simulator에서 다시 읽거나 계산할 수 있다.
- 모든 값을 즉시 영속화하면 성능과 저장 비용이 커진다.

**트레이드오프**

- 재시작 직후 값이 비는 시간이 있을 수 있다.
- historian 저장과 current value contract를 명확히 구분해야 한다.

**개정 (Foundation 재설계(2026-10-04), 새 경로 — target design(미구현))**
태그 정의의 정본은 **고객 project repo(Git)**다. 현장 site-db는 적용본 + 현장 드래프트(단조 `site_rev`)를 담는다. `tag_id`는 태그 path 기준으로 안정적이며 재사용하지 않는다(삭제는 `retired_at`). 런타임 값이 휘발된다는 부분은 그대로 유지한다. 근거: [ADR-025](#adr-025-프로젝트-정본--드래프트-회수단조-site_rev).

**관련 SOT**: [data-contracts](../sot/data-contracts.md), [api-events](../sot/api-events.md)

## ADR-013: PLC 로직은 바꾸지 않는다 · 기존 주소 쓰기는 가드레일 아래 현장별로 연다

**상태**: Accepted — **개정(2026-10-04)**. 원래 결정 "v1 Does Not Replace Existing PLC Control"(쓰기 전면 비활성)을 아래로 대체한다. 원문은 이 절 끝 `이전 결정`에 남긴다.

**결정**
PureIron은 **PLC 로직(프로그램)을 수정·다운로드하지 않는다.** 제어의 주체와 인터록은 계속 PLC다. 그 위에서 MES 기능을 위해 **기존 PLC에 이미 있는 주소(태그)에 값을 쓸 수 있다.** 쓰기는 다음 가드레일을 모두 지킬 때만 허용한다.

1. **쓰기 대상**: 고객 프로젝트(project repo)에 쓰기 가능으로 **선언된 태그만**. 종류는 두 가지다.
   - 설정값·운전 조건(셋포인트, 레시피 값): 허용 범위(min/max)를 반드시 선언한다.
   - 운전 명령(시작·정지·리셋 등 명령 비트/값): 허용 값과 **PLC 측 허가 조건 태그**(예: 원격 운전 허가·자동 모드)를 반드시 선언한다. 허가 조건이 참이 아니면 보내지 않는다.
2. **기본은 읽기 전용**: 쓰기는 현장 단위 스위치로 켜며 기본값은 꺼짐이다. 고객이 쓰기 대상 태그·범위·허가 조건을 승인한 현장만 켠다. 승인 기록은 고객 project repo의 PR 리뷰로 남는다.
3. **실행 주체**:
   - 권한 있는 사람이 화면에서 대상·현재값·목표값을 확인한 뒤 실행한다. 운전 명령은 2단계 확인을 거친다.
   - 사람이 미리 승인한 MES 예약 작업이 정해진 시각에 실행한다.
   - AI(Agent)는 값을 **제안만** 하고, 실행하려면 사람 승인이 필요하다(ADR-014).
4. **실행 전 검사**: 태그 품질이 GOOD인지, 장비가 연결되어 있는지, 범위 안인지, 허가 조건이 참인지 확인한다. 업데이트 중(편집 잠금·마이그레이션)에는 쓰기와 예약 실행을 멈춘다. 어느 검사든 실패하면 보내지 않고 알린다.
5. **실행 후**:
   - 다시 읽어 반영됐는지 확인한다(readback).
   - **자동 재시도는 하지 않는다.** 실패는 그대로 보고한다.
6. **감사**: 모든 쓰기 시도(누가·무엇을·이전값·목표값·결과·사유·승인자)를 감사 로그에 남기고, 지우지 않는다.
7. **경로**: core만 `WriteRequest`를 보낸다. collector는 계약된 응답(`WriteResult`)으로 결과만 돌려준다. 응답이 없거나 상태 값이 빠진 경우는 거부(`REJECTED`)로 읽는다(fail-safe).

**이유**

- 고객 현장에는 이미 잘 도는 PLC 로직이 있다. 로직을 건드리면 안전 인증·현장 검증·책임 범위가 급격히 커진다. 반면 기존 주소에 값을 쓰는 것은 현장 HMI가 이미 하는 일과 같은 수준의 개입이다.
- MES의 핵심 가치(운전 조건 내려주기, 작업 시작·정지 지시, 실적과 조건 연결)는 쓰기 없이 만들 수 없다.
- 읽기 전용으로 시작해 신뢰를 쌓은 뒤 고객 승인으로 쓰기를 여는 순서를 따른다. 그래야 첫 계약 설득("기존 제어를 건드리지 않는다")과 장기 확장을 둘 다 지킬 수 있다.

**트레이드오프**

- 운전 명령 쓰기는 잘못 보내면 설비 동작에 직접 영향을 준다. 허가 조건·2단계 확인·감사·readback 같은 장치를 구현하고 시험하는 비용이 든다. G2 e2e에도 쓰기 시나리오와 거부 시나리오가 들어가야 한다.
- 현장별 승인·선언 절차가 늘어 도입 작업량이 커진다.
- 영업 문구는 "PLC 로직은 바꾸지 않는다, 기본은 읽기 전용, 쓰기는 고객이 승인한 항목만"으로 정확히 바꿔야 한다. "읽기만 한다"는 첫 PoC 범위의 설명으로만 쓴다.
- 쓰기 구현 시점은 Foundation 플랜의 별도 마일스톤에서 정한다. 그 전까지 collector는 `WriteRequest`를 받으면 `REJECTED`로 답한다(미구현).

**이전 결정 (2026-10-04 이전)**: "PureIron v1은 기존 PLC 제어 로직을 대체하지 않는다. 관제, 알람, 이력, 트렌드, 작업지시, Agent 보조 계층을 얹는다." Foundation 재설계 시점에는 `WriteRequest`를 계약만 두고 비활성으로 유지했다. 이번 개정은 "로직 불변"은 유지하고 "쓰기 전면 금지"만 가드레일 아래 허용으로 바꾼다.

**관련 SOT**: [product](../sot/product.md), [architecture](../sot/architecture.md), [mes-schedule](../sot/mes-schedule.md), [agent-rules](../sot/agent-rules.md#불변-규칙)

## ADR-014: Simulator·3D Twin·Agent Are Assistive, Not Autonomous Control

**결정**
Simulator, 3D Digital Twin, Agent-assisted Simulation은 v1의 고객 설득과 운영 보조 기능으로 둔다. 실제 설비 자동 제어와 사람 승인 없는 자동 조치는 v1 범위에서 제외한다.

**이유**

- Simulator+3D+Agent는 데모 설득력을 크게 높인다.
- 하지만 AI 자동 제어는 신뢰성, 책임, 안전성 검증 비용이 너무 크다.
- v1은 “원인 후보 설명과 조치 흐름 제안”까지만 제공해도 충분히 차별화된다.

**트레이드오프**

- Agent가 할 수 있는 액션 범위가 제한된다.
- 장기적으로 자동화로 확장하려면 approval, audit, simulator validation 데이터가 축적되어야 한다.

**유지 (Foundation 재설계(2026-10-04))**: 새 경로에서도 유지한다. `ai` 서비스는 core read-only API만 소비한다. ADR-013 개정(PLC 쓰기 허용) 이후에도 AI는 쓰기 값을 **제안만** 하고 실행은 사람 승인을 거친다.

**관련 SOT**: [product](../sot/product.md), [frontend](../sot/frontend.md), [api-events](../sot/api-events.md)

## ADR-015: Start as Field-deployed Solution, Not Finished SaaS

**결정**
초기 GTM은 완성형 self-serve SaaS가 아니라 현장 구축형 솔루션으로 시작한다. 고객별 구축 과정에서 반복되는 패턴을 제품 코어로 흡수한다.

**이유**

- 중소 제조업 고객은 “가입하고 알아서 설정”보다 현장 문제 해결을 원할 가능성이 높다.
- 첫 유료 계약에는 맞춤 구축과 데모 설득력이 더 중요하다.
- 2인 팀은 시장을 넓게 열기보다 좁은 현장 문제를 깊게 해결해야 한다.

**트레이드오프**

- 초기에는 서비스/프로젝트 성격이 강해진다.
- 반복 패턴을 제품화하지 않으면 커스텀 지옥이 될 수 있으므로 SOT와 config 구조가 중요하다.

**유지 (Foundation 재설계(2026-10-04))**: 새 경로에서도 유지한다. 중앙(control-api·control-web·fleet repo)은 고객용 SaaS가 아니라 벤더 내부 배포 도구다.

**관련 SOT**: [product](../sot/product.md), [data-contracts](../sot/data-contracts.md)

## ADR-016: SOT / Projection / Generated Boundary

**결정**
정본은 `docs/wiki/sot/**`와 `docs/wiki/adr/index.md`에 둔다. `AGENTS.md`, `CLAUDE.md`, `README.md`는 짧은 projection으로 유지한다. `docs/wiki/_generated/**`와 `docs/LLM_WIKI.md`는 직접 수정하지 않는다.

**이유**

- Agent가 여러 파일을 읽으면서 같은 규칙이 서로 다르면 오류가 난다.
- 2인 팀은 중복 문서 drift를 계속 관리할 수 없다.
- generated context는 build/check로 일관되게 갱신해야 한다.

**트레이드오프**

- projection 문서만 읽으면 상세 맥락이 부족할 수 있다.
- 대신 projection은 반드시 SOT 링크와 핵심 불변 규칙만 담아야 한다.

**관련 SOT**: [agent-rules](../sot/agent-rules.md), [testing](../sot/testing.md)

## ADR-017: Operator UI Over Marketing UI

**결정**
Frontend는 SaaS landing page처럼 보이면 안 된다. 운영자가 매일 보는 SCADA/HMI 도구로서 데이터 밀도, 상태 인지, 예측 가능한 조작을 우선한다.

**이유**

- 고객이 돈을 내는 이유는 “예쁘다”가 아니라 “공장 상태를 빨리 안다”다.
- 과한 gradient, glass morphism, orb, neon glow는 산업 운영 도구의 신뢰감을 떨어뜨린다.
- 모바일 알람 확인과 조치 흐름이 v1 고객 가치에 더 직접적이다.

**트레이드오프**

- 데모의 시각적 임팩트를 일부 절제해야 한다.
- 3D Digital Twin은 장식이 아니라 상태 이해와 공정 맥락 설명에 써야 한다.

**관련 SOT**: [frontend](../sot/frontend.md), [product](../sot/product.md)

## ADR-018: Device Health Is Not a Tag

**결정**
디바이스 연결 상태는 tag definition이나 `tag.changed.*` 이벤트로 모델링하지 않는다. 각 driver의 `DriverHealth.snapshot()`을 `/api/devices` merged response로 노출한다.

**이유**

- device health는 사용자가 정의한 공정 태그가 아니라 드라이버 런타임 상태다.
- `[health]...` 시스템 태그를 만들면 tag search, historian, UDT/driver-derived ownership이 오염된다.
- health와 process value의 source of truth를 분리해야 Agent와 UI가 혼동하지 않는다.

**트레이드오프**

- health history가 필요하면 별도 `device_health_history` 저장소를 설계해야 한다.
- 기존 `[health]` 잔여 데이터는 검색에서 방어적으로 숨겨야 한다.

**유지 (Foundation 재설계(2026-10-04))**: 새 경로에서도 유지한다. 장비 상태는 collector가 `collector.proto`의 `DeviceHealth` 메시지로 보내고 core `GET /api/v1/devices`가 단일 소스다. 태그로 만들지 않는다.

**관련 SOT**: [architecture](../sot/architecture.md), [api-events](../sot/api-events.md), [frontend](../sot/frontend.md)

## ADR-019: 현장 서비스 분할 — collector / core / ai / web

**상태**: Accepted — target design(미구현). Foundation 재설계(2026-10-04).

**결정**
현장 런타임을 단일 FastAPI 프로세스(legacy `ScadaGateway`)에서 서비스 단위로 나눈다. compose 프로젝트 `pureiron` = `collector`(장비 통신) · `core`(태그·알람·이력·인증·감사·실시간·프로젝트 적용) · `web`(nginx + React) · `site-db`(TimescaleDB PG16) · `simulator`(dev profile). `ai`는 core read-only API 계약만 정의하고 첫 완료 범위에서 제외한다. Updater는 컨테이너가 아니라 **호스트 systemd 서비스**다. core의 유일한 조립 지점은 `SiteRuntime.build(settings)` → `create_app(rt)`이며 모든 의존성은 생성자로 주입한다. 도메인 모듈은 import-linter 계약(진입점만 runtime/app import, `api` > 도메인 > `platform` 계층, 도메인 간은 `<module>.ports`만)으로 경계를 강제한다.

**이유**

- 장비 통신 장애·재시작이 core(알람·이력·UI)를 같이 죽이지 않게 격리한다.
- 업데이트·롤백을 이미지 단위로 다루려면 서비스 경계가 배포 경계와 일치해야 한다.
- 전역 싱글톤 조회를 없애면 테스트에서 런타임을 통째로 갈아끼울 수 있고, 의존 방향을 정적 검사로 지킬 수 있다.
- Updater가 컨테이너 안에 있으면 자기 자신과 docker를 교체·복구할 수 없다.

**트레이드오프**

- 프로세스 간 통신(gRPC)과 배포 단위가 늘어 운영 표면이 커진다.
- legacy 기능(OPC-UA 외 프로토콜, UDT, 스크립트, 대시보드, Agent, MES)은 순서대로 다시 이식해야 하며 그동안 legacy와 새 경로 2벌을 유지한다.

**관련 SOT**: [architecture](../sot/architecture.md#시스템-구성-adr-019), [runtime-config](../sot/runtime-config.md#목표-설계미구현)

## ADR-020: collector ↔ core gRPC 계약 · Ack · replay 의미론

**상태**: Accepted — target design(미구현).

**결정**
collector와 core는 `packages/contracts/collector.proto`의 양방향 스트림 `Acquisition.Session` 하나로 통신한다(core = 서버). core가 `AcquisitionPlan`(장비·태그 주소)을 내려주고 collector는 `ValueBatch{seq}`를 100ms 또는 500값마다 올린다. collector는 **무상태**이며 DB에 접근하지 않고, core `Ack` 전까지 최대 60초 메모리 링버퍼에 보관한다. core는 in-memory ingest(현재값 반영 + historian 상한 버퍼 600,000행 적재) 직후 Ack한다. 재연결 후 재전송분(`replay=true`)은 **historian에만** 원시 기록하고(deadband·알람·WS 우회), 중복은 `UNIQUE(tag_id, time)`로 흡수한다. 스트림 종료 또는 `max(3×poll, 5s)` 무신호면 해당 장비 태그는 BAD, 장비는 DISCONNECTED. 쓰기(`WriteRequest`)는 core만 보내며 ADR-013 가드레일(선언된 태그·범위·허가 조건, 현장별 활성화, 사람 확인 또는 승인된 예약, readback, 감사)을 따른다. 구현 전까지 collector는 `REJECTED`로 답한다.

**이유**

- collector를 무상태로 두면 재시작·교체가 안전하고 장비 측 장애를 core와 분리할 수 있다.
- Ack 시점을 core in-memory ingest로 고정하면 링버퍼는 core 다운·단절 구간만, DB 장애는 core 상한 버퍼만 책임진다.
- replay를 라이브 경로와 분리해야 알람 중복·deadband 상태 오염이 없다.

**트레이드오프**

- core 프로세스 crash 시 아직 flush되지 않은 버퍼는 손실될 수 있다(정상 시 flush 주기 1초분, DB 장애 중이면 상한 내 전체). 이 허용 손실을 문서화한다.
- proto는 호환성 diff 게이트(ADR-028)로 보호해야 하며 프로토콜 추가는 `TagPoint.address` oneof 확장으로만 한다.

**관련 SOT**: [architecture](../sot/architecture.md#통신-경계-adr-020), [data-contracts](../sot/data-contracts.md#목표-설계미구현)

## ADR-021: TimescaleDB 유지 · 기록 정책

**상태**: Accepted — target design(미구현).

**결정**
이력 저장소는 TimescaleDB(PG16)를 유지한다. `tag_history`는 1일 chunk hypertable, 2일 지난 chunk는 압축, 보존 기간은 **프로젝트 설정**(`historian.retention_days`, 기본 365일), 1분·1시간 continuous aggregate(`WITH NO DATA` + refresh policy)를 둔다. 기록은 태그별 deadband + `max_interval`로 줄인다. `tag_id`는 태그 path 기준으로 안정적이며 재사용하지 않는다(삭제는 `retired_at` 표시).

**이유**

- legacy에서 이미 검증된 저장소이고, 압축·CAGG·retention을 DB 기능으로 해결해 코드량을 줄인다.
- legacy의 하드코딩 retention(`027_*`)을 고객별 프로젝트 설정으로 옮겨야 현장마다 다른 보존 요구를 수용한다.

**트레이드오프**

- Timescale extension 업그레이드는 전진 전용이라 site-db 이미지 변경은 앱 변경과 분리된 `db-step` release로만 한다(ADR-024).
- 압축 chunk와 CAGG는 마이그레이션 lint 제약(ADR-024)을 강하게 만든다.

**관련 SOT**: [data-contracts](../sot/data-contracts.md#목표-설계미구현)

## ADR-022: DesiredState GitOps · generation 카운터

**상태**: Accepted — target design(미구현).

**결정**
현장별 배포 목표(DesiredState)는 벤더 내부 repo `pureiron-fleet`의 `sites/<site>.yaml`이 정본이다(product release, 프로젝트 repo·버전, 적용 창). **이 PR의 승인·merge가 곧 배포 승인**이다. 파일 안 `generation: N` 카운터는 PR마다 정확히 +1이어야 하고(`check-gen.yml`), 중앙 `control-api`에 이미 게시된 최대 gen보다 커야 한다. 현장 Updater 규칙: 같은 gen은 멱등(no-op), 낮은 gen은 `E_GEN` 거부, 실패 후 **자동 재시도 없음**, 취소는 이전 내용으로 되돌린 **더 높은 gen**(revert PR). 현장은 중앙을 polling만 하고 중앙이 현장에 접속하지 않는다. control-web은 읽기 전용이다.

PoC 무료 모드(첫 유료 계약 전): GitHub Free private repo라 branch protection·Environment 승인이 없으므로, fleet `publish.yml`이 서명 직전 merge된 PR의 승인(허용 승인자 목록, 작성자 제외)을 API로 확인하고 없으면 `E_APPROVAL`로 서명을 거부한다. PR을 거치지 않은 main 직접 push(merge commit에 PR 연결이 없음)도 서명을 거부한다. 유료 전환 시 GitHub 기능(branch protection, Environment required reviewer)으로 되돌린다.

**이유**

- 누가 언제 어떤 현장에 무엇을 배포했는지가 Git 이력과 PR 리뷰로 남는다.
- git 이력 기반 번호(`rev-list --count`)는 fetch 깊이·merge 방식·이력 재작성에 흔들리므로 파일 내 카운터가 감사 가능하다.
- 현장발 polling만 허용하면 고객 방화벽 인바운드 개방이 필요 없다.

**트레이드오프**

- PoC 동안 승인 강제는 GitHub 기능이 아니라 workflow 검사 수준이다(단일 runner 호스트 장애 위험 포함).
- 여러 epoch를 건너뛰는 현장은 단계별 연속 PR(`stepwise.py`)이 필요하다.

**관련 SOT**: [architecture](../sot/architecture.md#배포--중앙-adr-022-adr-023), [runtime-config](../sot/runtime-config.md#목표-설계미구현)

## ADR-023: 단일 서명 번들 · 신뢰 루트 · 이미지 신원(config digest)

**상태**: Accepted — target design(미구현).

**결정**
현장에 들어가는 모든 변경은 서명된 단일 번들 `.pib`(tar: `manifest.json`·`manifest.sig`·`release-cert.json`·`.sig`·이미지·프로젝트)로만 전달하며, **온라인과 폐쇄망이 같은 파일·같은 검증 경로**를 쓴다. registry pull은 하지 않는다. 신뢰 체인: 오프라인 root key(Ed25519) → release key 인증서(유효 180일, serial) → manifest 서명. 검증 순서는 ① 서명·인증서(`E_SIG`/`E_CERT`) ② site_id(`E_SITE`) ③ generation(`E_GEN`) ④ 파일 sha256·이미지 신원(`E_DIGEST`) ⑤ 스키마 호환·Updater 버전(`E_SCHEMA`/`E_UPDATER`) ⑥ 미회수 드래프트(`E_DRAFT`)이며, 어느 단계든 실패하면 **서비스를 멈추기 전에** 거부한다. 이미지 신원은 image **config digest**(`docker image inspect .Id`)다: 설치 시 classic image store를 고정하고, 로드 후 `pureiron/<svc>:<release>` 태그와 `.Id == manifest.config_digest`를 assert하며 compose는 `pull_policy: never`. site-db도 업스트림 이미지를 `pureiron/site-db:<ver>`로 retag해 같은 규칙을 따른다. 서명·검증 코드는 `packages/bundle-tools` 한 벌을 CI·Updater·control-api가 공유한다.

**이유**

- 폐쇄망 현장이 1순위 고객군이므로 온라인 전용 경로를 따로 두면 검증되지 않은 경로가 생긴다.
- `docker save/load`는 registry digest를 잃지만 config digest는 내용 주소라 보존된다.
- "CI가 서명한 것과 Updater가 검증하는 것이 다르다"는 결함을 코드 한 벌로 원천 차단한다.

**트레이드오프**

- 번들은 항상 전체 이미지를 포함한다(델타 없음). 크기 예산 1.5 GiB, 1.2 GiB 경보.
- root key 분실은 치명적이다: 대표가 오프라인 장비에서 생성하고 사본 2부를 서로 다른 장소에 보관한다(M2 착수 전). 그 전에는 test key만 쓴다.

**관련 SOT**: [runtime-config](../sot/runtime-config.md#목표-설계미구현), [data-contracts](../sot/data-contracts.md#목표-설계미구현)

## ADR-024: 확장형 마이그레이션 · epoch envelope · 백업 경계

**상태**: Accepted — target design(미구현). **ADR-006을 대체**한다(새 경로).

**결정**
새 경로에서는 부팅 시 자동 마이그레이션을 하지 않는다. 스키마 변경은 별도 명령 `pureiron-core migrate up`(이력 테이블 `schema_migrations` + checksum, `pg_advisory_lock`, 파일 단위 트랜잭션, 실패 즉시 중단)으로만 하고 **Updater가 호출**한다. 마이그레이션은 **ADD-only**(확장형)이며 lint가 DROP·RENAME·타입 변경·제약 추가·NOT NULL 무기본값·UNIQUE INDEX·trigger 등을 거부한다. 제거(contract)는 deprecated 표시 후 다음 epoch 이후에만 한다. 마이그레이션을 포함한 release마다 정수 **epoch**를 +1하고, 앱은 `app_expected ≤ 현재 스키마 ≤ app_max_compatible(= expected + 1)` 범위에서만 ready다. 따라서 N-1 앱이 N 스키마에서 동작해야 하고(G1 게이트), 1 epoch를 넘는 점프는 거부한다. 업데이트 전 백업 = `pg_dump`(tag_history 제외) + 프로젝트 스냅샷(`project_state` 포함) + compose/manifest + secret 참조 목록. 롤백 = 직전 이미지 digest + 직전 프로젝트 복원이며 **스키마는 전진만**, 운영 이력(사용자·ACK·감사·tag_history)은 되돌리지 않는다.

**이유**

- 업데이트 실패 시 구 앱이 새 스키마에서 계속 동작해야 롤백·취소(revert PR)가 실제로 안전하다.
- 부팅 시 자동 실행은 실패 구문을 경고 후 건너뛰는 등 중단 지점을 통제할 수 없었다(legacy `gateway.py`).
- 운영 이력을 되돌리면 현장 감사 기록이 사라진다.

**트레이드오프**

- 컬럼 제거·이름 변경이 최소 2 release에 걸친다.
- 오래 방치된 현장은 epoch당 1단계씩 적용해야 해 업데이트 시간이 늘어난다.

**관련 SOT**: [data-contracts](../sot/data-contracts.md#목표-설계미구현), [architecture](../sot/architecture.md#db--마이그레이션-adr-021-adr-024)

## ADR-025: 프로젝트 정본 · 드래프트 회수(단조 site_rev)

**상태**: Accepted — target design(미구현). ADR-012 개정의 근거.

**결정**
고객 프로젝트(장비·태그·알람·화면 설정)의 정본은 **고객별 Git repo**(`pureiron-project-<id>`)다. 현장 site-db의 프로젝트는 적용본이며, 현장에서 REST `PATCH`로 고친 내용은 드래프트로 남는다. `PATCH`마다 `site_rev`가 단조 증가(리셋 없음)하고, 적용 시 `base_site_rev`를 기록한다. 미회수 드래프트가 있으면 새 배포는 `E_DRAFT`로 거부된다. 회수는 현장 전체 스냅샷 export → 벤더가 고객 repo에서 `pureiron-project absorb` → PR(`absorbs: {site: rev}` 기록) 순서다. 비밀값은 프로젝트에 넣지 않고 `secret://<name>` 참조만 둔다. 프로젝트 파일은 정규화 규칙(YAML 1.2 strict, RFC 8785 숫자, 기본값 생략, POSIX 경로, UTF-8 NFC, LF)으로 **무손실 왕복**하고 `content_hash`로 버전을 식별한다. legacy 변환기는 지원하지 않는 항목을 반드시 보고한다.

**이유**

- 고객별 설정 이력·리뷰·롤백을 Git으로 일원화한다.
- 현장 긴급 수정은 허용하되, 그 수정이 다음 배포에 조용히 덮어써지는 사고를 차단한다.

**트레이드오프**

- 현장 편집 화면은 두지 않는다(REST만, No config UI). 회수는 벤더 운영 절차가 필요하다.
- 정규화 규칙 때문에 사람이 쓴 YAML 서식이 export 후 바뀔 수 있다(`fmt --check`로 고정).

**관련 SOT**: [data-contracts](../sot/data-contracts.md#목표-설계미구현)

## ADR-026: repo 레이아웃 · 고객별 repo

**상태**: Accepted — target design(미구현).

**결정**
새 코드는 새 repo `circulation95/PureIron_V1`의 `services/`(core·collector·updater·control-api·web·control-web·ai) · `packages/`(contracts·project-tools·bundle-tools) · `deploy/` · `tests/e2e/`에 둔다. 기존 `PureIron_ignition`은 태그 `legacy-final`로 고정해 참조만 하고(이식 완료 전 archive 금지), legacy 인용은 `PureIron_ignition@legacy-final:<path>` permalink로 한다. repo는 개인 계정 `circulation95` 소유 private으로 시작한다: 제품(V1), `pureiron-fleet`(배포 정본), `pureiron-fleet-e2e`(nightly e2e·runner), 고객별 `pureiron-project-<id>`. 고객 repo는 V1의 재사용 workflow `validate-project.yml`을 호출하며(V1 Actions access = "repositories owned by the user"), 제품 코드에 고객별 분기(`site_id == "…"`, 고객 이름 리터럴)를 두지 않는다.

**이유**

- legacy repo의 CI·runner·미커밋 작업을 건드리지 않고 깨끗한 게이트로 시작할 수 있다.
- 고객별 repo로 접근 권한·이력·배포 단위를 고객마다 분리한다.

**트레이드오프**

- 이식 기간 동안 두 repo를 오가며 참조해야 한다.
- 고객이 repo를 직접 소유해야 하는 시점이 오면 project-tools·workflow를 공개 repo로 분리하거나 org로 이전해야 한다.

**관련 SOT**: [architecture](../sot/architecture.md#시스템-구성-adr-019), [agent-rules](../sot/agent-rules.md)

## ADR-027: 지원 호스트 매트릭스

**상태**: Accepted — target design(미구현).

**결정**
현장 호스트는 **Ubuntu 24.04 x86_64 하나만** 지원한다. 설치는 오프라인 기준: preflight(OS·아키텍처·`/usr/bin/python3.12`·디스크 ≥ 50 GiB·포트·시간 동기) → 번들에 든 docker-ce `.deb` 설치 → `/etc/docker/daemon.json`으로 classic image store 고정 → 이미지 로드 → systemd. Updater는 Python 3.12 **venv tarball**(`/opt/pureiron/updater/<ver>`, `current` symlink)이다. 데이터 경로는 `/var/lib/pureiron/{db,state,backups,bundles}`. 개발·PoC 검증은 개발 PC(Windows 11 Pro) Hyper-V 위 Ubuntu 24.04 VM으로 한다.

**이유**

- 지원 조합을 하나로 묶어야 2인 팀이 설치·업데이트·롤백을 실측 검증할 수 있다.
- zipapp은 native `.so`(cryptography)를 못 읽고, shiv는 런타임 추출 캐시에 의존해 기각했다. venv는 서명·검증 코드를 CI와 공유할 수 있다.

**트레이드오프**

- 다른 배포판·ARM·Windows 서버 현장은 지원하지 않는다(요구가 생기면 별도 ADR).
- 호스트 python3.12·classic image store·Ubuntu 24.04에 묶인다.

**관련 SOT**: [runtime-config](../sot/runtime-config.md#목표-설계미구현)

## ADR-028: 3단 검증 게이트 · generated ≠ authored · runner 격리

**상태**: Accepted — target design(미구현).

**결정**
검증은 3단이다. **G1**(PR마다): ruff, pyright strict, tsc/eslint strict-type-checked, import-linter(+음성 fixture), 계약 호환성 diff(`buf breaking`, `oasdiff`, schema diff)와 생성물 drift, migration lint, 단위·통합, 재현 빌드, N-1 스키마 호환, 고객 분기 금지 검사 — **부채 예외 없음**(`type: ignore`·`eslint-disable`·`as any`·`@ts-ignore` 0건). **G2**(nightly): VM 3대(site-a·site-b·central) e2e로 설치·예약 업데이트·격리·롤백·거부·장애 주입·폐쇄망·드래프트 회수·fleet 표시. **G3**(release candidate): 중앙 차단 24h, 백업·복원, 2,000태그 성능 측정. 계약(proto·OpenAPI·JSON Schema)이 정본이고 코드 생성물(`src/<pkg>/_gen/`, `schema.d.ts`)은 커밋하되 **generated ≠ authored**: 생성물(`.py`·`.pyi`)의 진단은 pyright·ruff에서 빼고 `.pyi`(mypy-protobuf)를 타입 정본으로 authored 코드에서 strict 소비하며, 재생성 diff로 drift를 막는다(설정 원문: `[tool.pyright] ignore = ["src/<pkg>/_gen/**"]`, ruff `extend-exclude = ["services/*/src/*/_gen", "packages/*/src/*/_gen"]`, 검사 job `contracts-drift`). M1 구현 시 생성 `.pyi` 자체가 생성기·`types-protobuf` 버전 조합에 따라 strict 진단을 내는 것이 확인되어 `.py`만이 아니라 `_gen/**` 전체를 진단에서 뺐다(2026-10-04). runner는 PoC 동안 개발 PC의 self-hosted 3개(`g1`, `e2e`, `publish`)를 repo 단위로 등록하고, nightly·runner는 별도 repo `pureiron-fleet-e2e`에 둔다. V1에는 legacy workflow가 없어 legacy job이 새 runner를 점유하지 않는다.

**이유**

- 배포 경로 결함은 PR 단위 검사로 잡히지 않아 VM e2e가 필요하다.
- 생성 코드를 strict 검사에 넣으면 억제 주석이 생기고, 억제 주석 0건 규칙과 충돌한다.
- 첫 계약 전 현금 지출 0 원칙 때문에 hosted runner 분 과금을 쓰지 않는다.

**트레이드오프**

- 단일 호스트(개발 PC)가 runner·VM·서명 키를 함께 맡아 장애 시 동시 소실 위험이 있다(주 1회 외장 백업, 키 오프라인 사본).
- 유료 전환(첫 유료 계약 또는 2번째 상시 개발자) 시 hosted G1·Environment 승인으로 되돌리는 작업이 남는다.

**관련 SOT**: [testing](../sot/testing.md), [agent-rules](../sot/agent-rules.md)

## 4. 새 ADR 추가 규칙

새 ADR은 다음 기준을 만족할 때만 추가한다.

- v1 범위, 고객 설득, 설비 안정성, 데이터 계약, Agent 동작에 장기 영향을 준다.
- 단순 구현 세부가 아니라 되돌리기 어려운 방향 결정이다.
- 트레이드오프가 존재한다.

형식:

```md
## ADR-XXX: 제목

**결정**
...

**이유**

- ...

**트레이드오프**

- ...

**관련 SOT**: `../sot/<target>.md`
```
