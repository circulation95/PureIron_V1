---
id: adr-index
title: ADR — 아키텍처 결정 기록
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - docs/ADR.md
  - docs/wiki/sot/product.md
  - docs/wiki/sot/agent-rules.md
  - docs/wiki/sot/architecture.md
  - docs/wiki/sot/data-contracts.md
  - docs/wiki/sot/api-events.md
  - docs/wiki/sot/frontend.md
  - docs/wiki/sot/testing.md
last_verified: 2026-06-28
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
| `docs/ADR.md` | 초기 ADR 원문. 현재는 이 문서가 Wiki 정본 |

새 결정은 기능 설명이 아니라 **결정 / 이유 / 트레이드오프 / 관련 SOT** 형식으로 추가한다.

## 2. 제품·사업을 기준으로 한 최상위 결정

PureIron v1은 기술 과시용 플랫폼이 아니다. 첫 계약 가능성과 고객 설득력을 위해 다음 방향을 고정한다.

| ADR | 결정 | 한 줄 근거 |
|---|---|---|
| 013 | v1은 기존 PLC 제어를 대체하지 않는다 | 고객 설비 안정성이 첫 계약의 전제다 |
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
| 006 | Auto-migrations on boot | 단일 컨테이너 부팅과 재시작 안전성을 우선한다 |
| 007 | ScadaGateway singleton, no `app.state` | typed runtime root가 테스트·리팩터링에 유리하다 |
| 008 | Graceful degradation | 부분 장애에도 핵심 모니터링은 유지해야 한다 |
| 009 | TagStore lock discipline | 락 안 I/O와 이벤트 발행은 데드락 위험이 크다 |
| 010 | Event-driven tag updates | polling보다 변경분 pub/sub이 확장성과 효율이 좋다 |
| 011 | Frontend no `as any` / TS suppression | 타입 우회는 운영 UI 런타임 버그로 이어진다 |
| 012 | Tag definition = DB SoT, runtime value = volatile | 정의는 일관성이 중요하고 값은 디바이스 재읽기로 복구 가능하다 |
| 018 | Device health is not a tag | 연결 상태는 드라이버 런타임 상태이지 사용자 태그 정의가 아니다 |

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

**관련 SOT**: [data-contracts](../sot/data-contracts.md), [api-events](../sot/api-events.md)

## ADR-013: v1 Does Not Replace Existing PLC Control

**결정**
PureIron v1은 기존 PLC 제어 로직을 대체하지 않는다. 관제, 알람, 이력, 트렌드, 작업지시, Agent 보조 계층을 얹는다.

**이유**

- 첫 계약에서 고객이 가장 두려워하는 것은 설비 안정성 저하다.
- PLC 제어를 대체하려면 안전 인증, 현장 검증, 책임 범위가 급격히 커진다.
- 2인 팀이 v1에서 감당할 수 있는 범위를 넘는다.

**트레이드오프**

- “완전 자동화 플랫폼”처럼 보이는 장기 비전 일부는 v1에서 보류된다.
- 대신 고객 설득은 “기존 설비를 유지하면서 웹/모바일 관제를 얹는다”에 집중한다.

**관련 SOT**: [product](../sot/product.md), [architecture](../sot/architecture.md)

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

**관련 SOT**: [architecture](../sot/architecture.md), [api-events](../sot/api-events.md), [frontend](../sot/frontend.md)

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
