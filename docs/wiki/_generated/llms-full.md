# PureIron Wiki — Full Context

> 생성물. `python scripts/wiki.py build`로 갱신.
>
> **에이전트는 이 파일을 열지 않는다.** canonical 문서 전문을 이어붙인 외부 LLM
> ingest 전용 산출물이라 한 번 읽으면 컨텍스트를 통째로 소진한다. 작업 중
> 필요한 내용은 `docs/wiki/_generated/section-index.tsv` 를 grep 해서 해당
> 절만 읽는다.

<!-- source: docs/wiki/adr/index.md -->
# ADR — 아키텍처 결정 기록

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

---

<!-- source: docs/wiki/gtm/first-contract.md -->
# First Contract — 첫 계약 GTM/PoC 결정 프레임

# First Contract

이 문서는 PureIron의 **첫 유료 계약(PoC → 유료 전환)에 관한 결정의 정본 위치**다. 타깃 고객, PoC 패키지, 데모, 가격/패키징, 성공 기준, 제외 범위, 설비 안정성 FAQ를 여기서 다룬다.

> ⚠️ 이 문서는 완성된 GTM/가격 전략이 아니다. **1차 창업자 딥인터뷰(2026-06-30)로 타깃 업종·진입 채널·첫 계약 메시지·PoC 범위·제외 범위·가격 방향·성공 기준/전환 신호 후보가 확정**됐고, **추가 창업자 인터뷰(2026-06-30, Q9~Q13)로 1순위 고객 페인 한 문장·첫 PoC PLC/프로토콜/네트워크 원칙·데이터 저장/배포 방식·PoC 기간/투입·가격 anchor가 추가 확정**됐다. 가격은 더 이상 임의 숫자가 아니라 **anchor(범위)로 확정**됐으며, **실제 견적은 현장 범위·태그 수·라인 수·네트워크 조건·지원 범위에 따라 조정**된다([가격 / 패키징](#가격--패키징)). 남은 미확정 항목(구체 고객사, 현장별 PLC/네트워크 실측 구성)은 여전히 본문에서 `가설` / `TBD` / `needs-founder-input`으로 명시하고, 추정을 확정처럼 쓰지 않는다([창업자 딥인터뷰 질문](#창업자-딥인터뷰-질문)).

[product](../sot/product.md)이 "자세한 영업 전략, 가격, 채널, 세일즈 자료는 향후 GTM 문서에서 분리해 다룬다"고 미뤄 둔 내용을 이 문서가 받는다. 제품 의도·v1 범위의 정본은 여전히 [product](../sot/product.md)이고, 이 문서는 그 위에서 "첫 계약을 어떻게 만드는가"만 다룬다.

## 이 문서의 원천

- [product](../sot/product.md) — 제품 의도, 초기 고객, v1 가치, 첫 계약 전략의 상위 방향
- [USER.md](../../../USER.md) — 팀 역할, 2인 팀 감당 범위, 작업 성향

## 결정 상태 표기

이 문서의 각 항목은 다음 중 하나로 표기한다.

| 표기 | 의미 |
|---|---|
| `확정` | 제품 불변 규칙·SOT에서 이미 정해진 안전한 기본값 |
| `가설` | 검증 전 작업가설. 데모/인터뷰로 검증해야 함 |
| `TBD` | 아직 정하지 않음. 결정 자체가 열려 있음 |
| `needs-founder-input` | 창업자/대표 고객 인터뷰 없이는 채우면 안 되는 항목 |

## 초기 타깃 고객 가설

- `확정` 첫 타깃 업종/라인: **금속·가공·프레스·사출 등 일반 중소 제조**(창업자 인터뷰 확정). 이유: PLC/설비 데이터 페인이 분명하고, read-only PoC를 1개 라인/설비군으로 작게 자르기 좋으며, 첫 계약 영업 사이클이 상대적으로 짧다.
- 타깃 선정 기준(`확정`, 위 업종을 고른 근거):
  - 이미 PLC로 자동화가 돌아가고 있어 v1이 제어를 대체하지 않아도 가치가 나오는 현장.
  - 사장/공장장이 현장 밖에서 설비 상태·알람을 보고 싶어 하는 페인이 분명한 곳.
  - 외산 HMI/SCADA가 비용·무게 측면에서 과한 중소 규모 라인.
  - LS·MELSEC·AB 등 한국·아시아 현장 프로토콜이 쓰이는 곳([architecture](../sot/architecture.md#지원-디바이스-프로토콜-v1)).
- 구매 의사결정자/실사용자 구분은 [product](../sot/product.md#대상-고객과-사용자)을 정본으로 따른다(여기서 중복하지 않는다).

## 첫 구매자 / 진입 채널

`확정`(창업자 인터뷰). 첫 계약을 만들기 위한 진입 경로를 두 갈래로 둔다.

- **Primary path**: 현장의 **설비보전팀장 / 전기팀장**과 **자동화 SI 업체**. 이들은 PLC/설비 데이터 페인을 직접 겪고, read-only PoC의 가치를 빠르게 이해한다.
- **Secondary (challenger) path**: 공장 **대표/임원 직접 영업**을 병행한다. 의사결정 권한이 있으나 영업 사이클·접점 확보가 더 까다로워 보조 경로로 둔다.
- **SI 경로 주의(`확정`)**: SI 업체를 통할 때 PureIron을 고객마다 새로 짜는 **무제한 커스텀 SI 도구로 만들지 않는다.** 반복 가능한 표준 PoC 패키지로 제한하고, 반복되는 화면·태그 구조·알람 조건은 제품 코어로 흡수한다(아래 [가격 / 패키징](#가격--패키징)).

## 고객 페인 (요약)

상세 정본은 [product](../sot/product.md#고객이-겪는-문제)에 있다. 첫 계약 관점의 핵심 페인만 추린다.

- 현장에 가거나 전화해야만 설비 상태를 안다.
- 알람 원인·조치 이력이 체계적으로 남지 않는다.
- 기존 설비를 건드렸다가 생산이 불안정해질까 두렵다.

### 대표 고객 / 1순위 후보의 가장 큰 페인 (`확정`, 추가 창업자 인터뷰)

> **설비 이상/알람이 나도 원인·조치·이력이 남지 않아 같은 문제가 반복된다.**

이 한 문장이 첫 계약 메시지의 1순위 앵커다. PureIron 첫 계약은 "예쁜 대시보드"가 아니라 **반복되는 설비 문제의 원인·조치·이력을 남기는 시스템**으로 포지셔닝한다. 위 [첫 계약 메시지](#첫-계약-메시지)와 데모/PoC 성공 기준은 이 페인 해소를 중심으로 정렬한다.

## 첫 계약 메시지

`확정`(창업자 인터뷰). 첫 계약 영업의 핵심 메시지를 다음으로 고정한다.

> **기존 PLC 제어를 건드리지 않고, 설비 상태와 알람을 AI가 설명해 조치 이력까지 남기는 경량 모니터링 시스템.**

이 메시지는 "기존 설비를 유지한 채 시작 가능 + 사람 판단 보조(자동 제어 아님)"를 한 문장으로 전달하는 것을 목표로 한다. 대상별로 같은 메시지를 다음과 같이 변주한다.

- **대표 / 임원**: 기존 설비·생산을 건드리지 않고, 현장 밖에서도 공장 상태와 알람을 보고 조치 이력까지 남기는 가벼운 시스템을 작게(소액 유료 PoC) 검증한다.
- **설비보전팀장 / 전기팀장**: 제어 로직을 바꾸지 않고 주요 태그를 읽어, 알람 원인 후보와 조치 시나리오를 AI가 설명하고 조치 이력을 남긴다. 현장 대응이 빨라지고 기록이 자동으로 쌓인다.
- **자동화 SI 업체**: 반복 가능한 read-only 모니터링 PoC 패키지로, 기존 SI 구축 위에 얹어 빠르게 가치를 보여줄 수 있다(고객별 무제한 커스터마이징이 아니라 표준 패키지).

대상별 페인·관심사 정본은 [product](../sot/product.md#대상-고객과-사용자)을 따르고, 여기서는 첫 계약 메시지 표현만 둔다.

## PoC 제안서 구조

PoC 제안서(고객 제출용)는 다음 골격을 따른다. 내용 채움은 타깃 확정 후.

1. 현장 현황 / 해결할 페인 1~2개 (고객 언어로)
2. PoC 범위 — 포함/제외를 한 페이지에 명시 (아래 [설치 범위 / 제외 범위](#현장-설치-범위--제외-범위) 인용)
3. "기존 PLC 제어를 건드리지 않는다" 명시 (아래 [PLC 제어 불간섭 표현](#plc-제어-불간섭을-계약제안서에서-표현하는-법))
4. 데모/PoC 일정과 투입
5. PoC 성공 기준 (측정 가능한 형태, 아래 [성공 기준](#poc-성공-기준과-유료-전환-조건))
6. 유료 전환 신호와 가격/패키징 (방향·가격 anchor `확정`, 실제 견적은 현장 조건별 산정 — 아래 [가격 / 패키징](#가격--패키징) 인용)
7. 보안/데이터/설비 안정성 FAQ (아래 [FAQ](#보안--데이터--설비-안정성-faq) 인용)

## 데모 시나리오 (영업/대표 미팅용 데모 패키지)

`확정`(안전한 기본값). 이 시나리오는 **영업/대표 미팅용 데모 패키지**이며, 실제 현장 PoC 범위와 구분한다(창업자 인터뷰 확정). **Simulator + 3D Digital Twin + Agent + Work Order**는 이 데모 패키지에서 보여주고, 실제 현장 PoC 기본 범위에는 포함하지 않는다(아래 [현장 설치 범위 / 제외 범위](#현장-설치-범위--제외-범위)). v1 기능만으로 구성하며 실제 설비 제어를 포함하지 않는다. 기능 정본은 [product](../sot/product.md#v1-핵심-기능).

1. 웹/모바일에서 라인 상태·주요 태그를 연다 — "현장 밖에서도 공장이 보인다".
2. Simulator + 3D Digital Twin으로 실제 공정처럼 동작하는 라인을 보여준다.
3. 알람을 발생시키고 모바일에서 확인 → 조치 이력 남김.
4. Agent가 알람 원인 후보와 조치 시나리오를 설명한다(자동 제어 아님, 사람 판단 보조).
5. 알람 ack·조치 note·이력 위에서 사람이 기존 작업 지시/CMMS 프로세스로 넘기는 **수동 Work Order handoff**를 보여준다(backend Work Order 기능 아님, 사람 판단·수동 handoff까지만). 반복 가능한 스크립트는 [work-order-demo-handoff 런북](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/work-order-demo-handoff.md).

데모는 "기존 설비를 유지한 채 시작 가능"하다는 메시지를 짧고 명확하게 전달하는 것을 목표로 한다.

## PoC 기간 / 투입

`확정`(추가 창업자 인터뷰). 첫 PoC는 **단계형**으로 진행하며, 2인 팀이 감당 가능한 범위로 자른다.

### 1단계: 사전 데모 / 현장 점검 — 1주

- Simulator / 3D Twin / Agent / Work Order 데모([데모 시나리오](#데모-시나리오-영업대표-미팅용-데모-패키지)).
- 고객 페인 확인([대표 고객 / 1순위 후보의 가장 큰 페인](#대표-고객--1순위-후보의-가장-큰-페인-확정-추가-창업자-인터뷰)).
- PLC/프로토콜 확인.
- 네트워크 접근 가능성 확인.
- 태그 후보 선정.
- PoC 성공 기준 합의([PoC 성공 기준](#poc-성공-기준과-유료-전환-조건)).

### 2단계: read-only 현장 PoC — 2주

- 승인된 주요 태그만 연결([첫 PoC PLC / 프로토콜 / 네트워크 원칙](#첫-poc-plc--프로토콜--네트워크-원칙-확정-추가-창업자-인터뷰)).
- 웹/모바일 대시보드 구성.
- 알람 발생 → 확인 → 조치 이력 흐름 검증.
- Agent troubleshooting 검증.
- 결과 리뷰 및 유료 전환 판단([유료 전환 신호](#유료-전환-신호-후보-확정-후보군)).

총 기간은 기본적으로 **3주 이내**다. 4주 이상으로 늘어나는 경우 범위를 재협상하거나 **정식 구축/별도 SI 계약으로 분리**한다(2인 팀 감당 범위 통제, [agent-rules](../sot/agent-rules.md#제품-판단-원칙)).

## 가격 / 패키징

방향과 **가격 anchor 모두 `확정`(창업자 인터뷰)**. anchor는 협상 출발점(범위)이며, **실제 견적은 현장 조건에 따라 조정**한다.

- `확정` 첫 PoC는 **소액 유료 PoC가 원칙**이다. **무료 PoC는 기본 금지.** 유료여야 고객의 진지함과 유료 전환 가능성을 검증할 수 있다. (단 아래 1단계 사전 데모/현장 점검은 영업 진입 비용으로 무료 또는 실비를 허용한다 — [PoC 기간 / 투입](#poc-기간--투입).)
- `확정` 패키징 순서: **소액 유료 PoC → PoC 성공 후 정식 구축비 + 월/연 유지보수·구독 → 운영**. 완성형 SaaS가 아니라 **현장 구축형 솔루션**으로 시작한다([product](../sot/product.md#첫-계약-전략)).
- `확정` 첫 1~2건은 **레퍼런스 조건으로 전략적 할인 가능**하다(레퍼런스 확보가 목적).

### 가격 anchor (`확정`, 추가 창업자 인터뷰)

| 단계 | anchor |
|---|---|
| 사전 데모 / 현장 점검 | 무료 또는 실비 |
| 2주 read-only 현장 PoC | 300만~700만 원 |
| 첫 1~2건 레퍼런스 할인 PoC | 100만~300만 원 |
| 정식 구축비 | 1,000만~3,000만 원 / 라인 또는 소규모 현장 단위 |
| 유지보수 / 구독 | 월 50만~200만 원 또는 연 600만~2,400만 원 |

> 위 anchor는 **시작 범위**이며, **실제 견적은 현장 범위, 태그 수, 라인 수, 네트워크 조건, 지원 범위에 따라 조정**한다. 제안서에는 anchor를 그대로 적지 말고 현장에 맞춰 산정한 값을 적는다.

- `확정` 구축 과정에서 반복되는 화면·태그 구조·알람 조건을 제품 코어로 흡수해 다음 계약의 한계비용을 낮춘다(SI 경로가 무제한 커스텀으로 번지지 않도록 [첫 구매자 / 진입 채널](#첫-구매자--진입-채널)의 표준 패키지 원칙과 연결).

## 현장 설치 범위 / 제외 범위

### 실제 현장 PoC 기본 범위 (`확정`, 창업자 인터뷰)

실제 현장에서 진행하는 유료 PoC의 기본 범위는 **read-only**로 고정한다.

- 기존 PLC/설비에서 주요 태그를 **읽기 중심으로 수집**(Tag/Device 수집, 트렌드/Historian).
- 실시간 상태 **대시보드**(웹/모바일).
- **알람** 발생 → 확인 → 조치 이력.
- **Agent troubleshooting** — 알람 원인 후보·조치 시나리오 설명(사람 판단 보조).
- Auth/RBAC/Audit 최소 구성.
- **실제 현장 PoC에는 write/control을 포함하지 않는다.**

> Simulator + 3D Digital Twin + Agent + Work Order 풀 데모는 실제 현장 PoC 기본 범위가 아니라 **영업/대표 미팅용 데모 패키지**다([데모 시나리오](#데모-시나리오-영업대표-미팅용-데모-패키지)). 현장 PoC와 데모 패키지를 제안서에서 명확히 구분한다.

### 첫 PoC PLC / 프로토콜 / 네트워크 원칙 (`확정`, 추가 창업자 인터뷰)

- **기존 PLC 제어 로직을 변경하지 않는다.** SI/전기팀장이 승인한 주요 태그만 **read-only**로 수집한다.
- 프로토콜은 **OPC-UA / Modbus TCP / Siemens S7 / LS Electric / Allen-Bradley ControlLogix / Mitsubishi MELSEC** 중 현장에 맞는 것을 사용한다(프로토콜 정본 표는 [architecture](../sot/architecture.md#지원-디바이스-프로토콜-v1)).
- **write/control은 PoC 범위에 포함하지 않는다.**
- 가능하면 PureIron 수집 환경은 **기존 제어망과 분리하거나 접근 범위를 제한**한다.
- PoC 태그 목록은 **SI/전기팀장과 사전에 합의**한다.
- 고객 불안이 큰 경우 **demo/simulator로 흐름을 먼저 확인한 뒤 실제 PLC 연결로 넘어간다**([PoC 기간 / 투입](#poc-기간--투입)의 단계형 PoC와 연결).

### 제외 (`확정`, 창업자 인터뷰 + [product](../sot/product.md#mvp-제외--제한) · [agent-rules](../sot/agent-rules.md#제품-판단-원칙) 정본)

첫 계약에서 다음은 **약속하지 않는다.**

- PLC 제어 로직 수정.
- 무인 자동 제어.
- 사람 승인 없는 write/control.
- MES/ERP 전체 대체.
- 고객별 대형 SI 커스터마이징.
- 범용 no-code SCADA builder.
- 모든 PLC/프로토콜 지원 약속.
- 24/7 SLA 보장.
- 예측정비 정확도 보장.
- 공정 최적화/수율 개선 수치 보장.

이 제외 목록은 제품 불변 규칙과 정렬하며, 계약/제안서의 "제외 범위" 절에 그대로 인용한다.

## PLC 제어 불간섭을 계약/제안서에서 표현하는 법

`확정` 원칙: **PLC 로직은 바꾸지 않는다. 첫 PoC는 읽기 전용이고, 기존 PLC 주소 쓰기(MES: 설정값·운전 조건·운전 명령)는 고객이 대상 태그·범위·허가 조건을 승인한 뒤 별도 단계로 연다**([product](../sot/product.md#mvp-제외--제한), [ADR-013](../adr/index.md)). 이를 계약/제안서에서 다음과 같이 표현한다(문구는 법무 검토 전 초안, `TBD`).

- "PureIron은 기존 PLC 및 제어 로직을 변경하지 않으며, 설비 데이터를 **읽어** 관제·알람·이력 계층을 제공한다."
- "쓰기/제어 명령은 PoC 범위에 포함하지 않는다."
- "PoC 도중 설비 제어 동작에 영향을 주지 않으며, 데이터 수집은 기존 제어 네트워크와 분리하거나 접근 범위를 제한하는 것을 우선한다." (원칙 `확정`, [첫 PoC PLC / 프로토콜 / 네트워크 원칙](#첫-poc-plc--프로토콜--네트워크-원칙-확정-추가-창업자-인터뷰). 구체 분리/단방향 실측 구성은 현장별 `needs-founder-input`)
- 쓰기(설정값·운전 조건·운전 명령)가 필요한 향후 단계는 고객 승인(대상 태그·범위·허가 조건)과 별도 계약/안전 검토 대상으로 명시한다. PLC 로직 수정은 어느 단계에서도 하지 않는다.

## 보안 / 데이터 / 설비 안정성 FAQ

고객(사장/공장장)이 첫 계약 전에 가장 자주 묻는 질문에 대한 기본 답변 골격. 일부는 현장별로 확정해야 한다.

- **Q. 설치하다가 설비가 불안정해지지 않나?**
  A. PLC 로직은 바꾸지 않고, 첫 단계는 데이터를 읽기만 한다(`확정`). 이후 쓰기는 고객이 승인한 항목만, 사람 확인·감사 기록을 거쳐 연다([ADR-013](../adr/index.md)). 수집 방식은 기존 제어와 분리/단방향을 우선한다(`needs-founder-input`, 현장 구성별 확정).
- **Q. 우리 공장 데이터는 어디에 저장되나?**
  A. `확정` **온프레미스 우선** — 기본 데이터는 고객 현장에 남긴다. 리포트/대시보드/원격지원/외부 접근은 **고객이 동의한 범위에서만** 제한적으로 연다([데이터 저장 / 배포 방식](#데이터-저장--배포-방식-확정-추가-창업자-인터뷰)). 기본 배포는 [runtime-config](../sot/runtime-config.md) 참조.
- **Q. 외부에서 공장이 보이면 보안은?**
  A. Auth/RBAC/Audit이 v1 범위에 포함된다(`확정`, [product](../sot/product.md#v1-핵심-기능)). 네트워크 노출 범위·접근 통제는 현장별 확정(`needs-founder-input`).
- **Q. 직원이 어렵지 않게 쓸 수 있나?**
  A. 운영자 중심의 경량 SCADA UI, 모바일 알람 확인을 핵심 경험으로 둔다(`확정`, [product](../sot/product.md#디자인-방향)).

> 데이터 계약·저장 상세는 [data-contracts](../sot/data-contracts.md), 런타임/배포는 [runtime-config](../sot/runtime-config.md)가 정본이다. 이 FAQ는 영업 대응용 요약이며 기술 정본을 중복하지 않는다.

## 데이터 저장 / 배포 방식 (`확정`, 추가 창업자 인터뷰)

첫 계약/PoC의 데이터 저장·배포 방식은 **온프레미스 우선 + 선택적 클라우드 리포트/원격접속**으로 고정한다.

- 기본 데이터는 **고객 현장에 남긴다.**
- 고객이 **동의한 범위에서만** 리포트/대시보드/원격지원/외부 접근을 제한적으로 연다.

### 중앙 관제 / 승인 기반 제어는 장기 방향 (`확정`, v1/첫 계약 범위 아님)

- 장기적으로 중앙 관제 / 승인 기반 제어는 가능하지만, **v1/첫 계약 약속에는 넣지 않는다.**
- 중앙 관제/제어는 **별도 안전 검토, 네트워크/보안 설계, 책임 범위 계약, fail-safe 설계, 고객 승인 체계**가 갖춰진 이후의 **별도 단계**로 다룬다([PLC 제어 불간섭](#plc-제어-불간섭을-계약제안서에서-표현하는-법) · [제외 범위](#현장-설치-범위--제외-범위)).
- 첫 계약 제안서/메시지에서 중앙제어·write/control을 **약속하지 않는다.**

기술 정본은 [runtime-config](../sot/runtime-config.md)·[data-contracts](../sot/data-contracts.md)를 따르며, 이 절은 첫 계약 약속 범위만 고정한다.

## PoC 성공 기준과 유료 전환 조건

`확정`(창업자 인터뷰): **PoC 성공 기준**과 **유료 전환 신호**를 분리한다. 성공 기준은 "PoC가 잘 됐는가"이고, 전환 신호는 "고객이 돈을 낼 준비가 됐는가"다.

### PoC 성공 기준 후보 (`확정` 후보군, 구체 수치/기간은 `needs-founder-input`)

1. 기존 PLC 제어를 건드리지 않고 주요 태그 수집에 성공한다.
2. 웹/모바일에서 주요 설비 상태를 확인할 수 있다.
3. 알람 발생 → 확인 → 조치 이력 흐름이 검증된다.
4. Agent가 알람 원인 후보와 조치 시나리오를 설명한다.
5. PoC 기간 동안 설비 제어/생산 안정성에 영향이 없다(불간섭 검증).
6. 대표/공장장/설비보전 담당자가 "현장 밖에서도 상태 파악이 된다"고 인정한다.
7. 정식 구축 시 확장할 라인/설비 후보가 나온다.

위 후보 중 어떤 항목을 측정 기준으로 채택할지와 구체 수치/기간은 `needs-founder-input`로 고객과 합의해 PoC 제안서에 적는다. 성공 기준은 v1 제품 성공 기준([product](../sot/product.md#성공-기준))과 정렬한다 — 기능 개수가 아니라 고객 설득력.

### 유료 전환 신호 후보 (`확정` 후보군)

1. 고객이 추가 라인/설비 연결을 요청한다.
2. 고객이 알람/리포트/이력 기능을 계속 쓰고 싶다고 말한다.
3. 고객 내부에서 대표/공장장/설비보전팀이 같이 리뷰 미팅을 잡는다.
4. 기존 수기/전화/엑셀 보고를 PureIron으로 대체하고 싶다고 말한다.
5. 정식 구축 견적서를 요청한다.

### 유료 전환 조건 (`TBD` / `needs-founder-input`)

- 위 전환 신호 + PoC 성공 기준 충족 + 합의된 가격/계약 조건 수락 시 유료 전환.
- 전환 시 확장 범위(추가 라인/디바이스)와 운영 책임 범위를 명시한다(제어 단계는 별도 계약·별도 안전 검토 대상, [PLC 제어 불간섭](#plc-제어-불간섭을-계약제안서에서-표현하는-법)).
- 구체 가격 숫자는 인터뷰 후 확정([가격 / 패키징](#가격--패키징)).

## 창업자 딥인터뷰 질문

이 문서의 `needs-founder-input` / `TBD`를 닫기 위한 질문. 1차 창업자 딥인터뷰(2026-06-30)로 일부가 확정됐다. 답을 받은 절은 위에서 `확정`으로 갱신했다.

1. ✅ `확정` 첫 타깃 업종/라인 → 금속·가공·프레스·사출 등 일반 중소 제조([초기 타깃 고객](#초기-타깃-고객-가설)).
2. ✅ `확정`(Q9) 대표 고객/1순위 후보의 가장 큰 페인 한 문장 → "설비 이상/알람이 나도 원인·조치·이력이 남지 않아 같은 문제가 반복된다"([대표 고객 / 1순위 후보의 가장 큰 페인](#대표-고객--1순위-후보의-가장-큰-페인-확정-추가-창업자-인터뷰)). 구체 고객사명은 여전히 `needs-founder-input`.
3. ✅ `확정`(Q10, 원칙) 첫 PoC PLC/프로토콜/네트워크 원칙 → 제어 로직 미변경 + 승인된 주요 태그만 read-only + 수집망 분리/접근 제한 우선([첫 PoC PLC / 프로토콜 / 네트워크 원칙](#첫-poc-plc--프로토콜--네트워크-원칙-확정-추가-창업자-인터뷰)). 현장별 실측 PLC/네트워크 구성은 `needs-founder-input`.
4. ✅ `확정`(방향 + Q13 anchor) 가격 모델 → 소액 유료 PoC + 정식 구축비 + 월/연 유지보수·구독, 첫 1~2건 레퍼런스 할인. 가격 anchor 확정([가격 anchor](#가격-anchor-확정-추가-창업자-인터뷰)). 실제 견적은 현장 조건별 조정.
5. ✅ `확정`(Q11) 데이터 저장/배포 → 온프레미스 우선 + 선택적 클라우드 리포트/원격접속([데이터 저장 / 배포 방식](#데이터-저장--배포-방식-확정-추가-창업자-인터뷰)). 중앙 관제/제어는 장기 방향(v1/첫 계약 범위 아님).
6. ✅ `확정`(Q12) PoC 기간/투입 → 단계형 PoC(1주 사전 데모/점검 + 2주 read-only 현장 PoC), 총 3주 이내([PoC 기간 / 투입](#poc-기간--투입)).
7. ✅ `확정` 유료 전환 신호 → 후보군 확정([유료 전환 신호 후보](#poc-성공-기준과-유료-전환-조건)).
8. ✅ `확정` 첫 계약 제외 범위 → 약속하지 않는 항목 확정([제외](#현장-설치-범위--제외-범위)).

## Backlog / 다음 단계

- [x] 1차 창업자 딥인터뷰 진행 → 타깃 업종·진입 채널·첫 계약 메시지·PoC 범위·제외 범위·가격 방향·성공 기준/전환 신호 후보 확정(2026-06-30).
- [x] 추가 창업자 인터뷰(Q9~Q13) 반영 → 1순위 고객 페인 한 문장, 첫 PoC PLC/프로토콜/네트워크 원칙, 데이터 저장/배포 방식, PoC 기간/투입, 가격 anchor 확정(2026-06-30).
- [ ] 남은 `needs-founder-input` 확정: 구체 고객사명, 현장별 실측 PLC/네트워크 구성.
- [ ] 타깃 확정 후 표준 read-only PoC 제안서 1종 실제 작성(가격 anchor를 현장 조건으로 산정해 반영). 표준 제안서 초안: [docs/sales/read-only-poc-proposal.md](../../sales/read-only-poc-proposal.md)(고객/영업용 초안, SOT 아님).
- [x] Work Order 데모 경로를 수동 handoff로 명확화 + 반복 가능한 스크립트/체크리스트 고정([work-order-demo-handoff 런북](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/work-order-demo-handoff.md), 2026-07-03).
- [ ] 나머지 데모 시나리오(1~4단계)를 실제 데모 스크립트/체크리스트로 구체화([testing](../sot/testing.md)의 데모/E2E 기준과 연결).

이 문서는 "GTM 전략이 끝났다"가 아니라 "첫 계약 결정을 모으는 정본 위치가 생겼고, 1차+추가 인터뷰로 핵심 방향·범위·가격 anchor가 확정됐다"는 상태를 나타낸다.

---

<!-- source: docs/wiki/index.md -->
# PureIron Wiki — 문서 허브

# PureIron Wiki

PureIron 문서의 **단일 정본(Source of Truth) 허브**. 사람과 LLM 에이전트 모두 이 위키를 기준으로 삼는다. 루트 `README.md`·`AGENTS.md`·`CLAUDE.md`는 이 위키로 연결되는 짧은 진입점/투영(projection)이다.

> SOT 문서는 실제 정본 내용을 가진다. 운영자용 상세 절차는 [archive/manual-legacy](archive/manual-legacy.md)를 통해 `MANUAL.md`로 연결된다.

## 구획 (문서 수명 기준)

| 구획 | 수명 | 내용 |
|---|---|---|
| [`sot/`](sot/agent-rules.md) | 불변 | 계약. 시점 개념이 없다 |
| [`gtm/`](gtm/first-contract.md) | 결정 시점 고정 | 사업/첫 계약 결정 |
| [`reports/`](reports/mvp-gap.md) | 시점 스냅샷 | 감사·audit. `last_verified` 이후 낡을 수 있다 |
| [`adr/`](adr/index.md) | 결정 시점 고정 | 아키텍처 결정 근거 |
| [`archive/`](archive/manual-legacy.md) | 동결 | legacy 포인터 |

감사·격차 점검 결과는 `sot/`가 아니라 `reports/`에 둔다. 상세 규칙은 [agent-rules](sot/agent-rules.md).

## SOT 지도

| 주제 | 문서 | 원천(SOT) |
|------|------|-----------|
| 제품 의도 | [product](sot/product.md) | Wiki SOT |
| 아키텍처 | [architecture](sot/architecture.md) | `services/core/src/pureiron_core/runtime.py`, `collector.proto`, V1 `services/**` |
| 런타임 설정·환경변수 | [runtime-config](sot/runtime-config.md) | `config.py`, `.env.example` |
| 데이터 계약(DB·설정) | [data-contracts](sot/data-contracts.md) | `db/migrations/*.sql`, `configs/**` |
| API·이벤트 | [api-events](sot/api-events.md) | `*/router.py`, `events/` |
| MES/Schedule v1 용어·플로우 | [mes-schedule](sot/mes-schedule.md) | Wiki SOT (`mes/`, `agents/tools/schedule_tools.py`, `ScheduleScreen.tsx`) |
| 프론트엔드 | [frontend](sot/frontend.md) | `frontend/src/**`, `frontend/package.json` |
| V1 코드 스타일·구조·리뷰 기준 | [coding-style](sot/coding-style.md) | `pyproject.toml`, `services/core/.importlinter`, V1 `services/**` |
| 테스트 | [testing](sot/testing.md) | `backend/tests/`, `tests/e2e/`, `frontend/package.json` |
| 에이전트 작업 규칙 | [agent-rules](sot/agent-rules.md) | Wiki SOT + projection files |
| 아키텍처 결정 기록 | [adr](adr/index.md) | Wiki ADR |

### SOT 외 구획

| 주제 | 문서 | 구획 |
|------|------|------|
| 첫 계약 GTM/PoC | [first-contract](gtm/first-contract.md) | `gtm/` — 사업 결정 |
| MVP 격차 audit | [mvp-gap](reports/mvp-gap.md) | `reports/` — 시점 스냅샷 |
| Hyper-V VM 실측 기록(release 쌍별) | [vm-e2e-runs](reports/vm-e2e-runs.md) | `reports/` — 시점 스냅샷 |

## 생성물 (수기 편집 금지)

`scripts/wiki.py build`가 생성한다. 직접 고치지 말고 `build`를 다시 돌린다.

- [manifest.json](_generated/manifest.json) — 전체 문서 색인 + 메타데이터(`bytes`·`section_count` 포함)
- [section-index.tsv](_generated/section-index.tsv) — 섹션 단위 검색 인덱스. **문서를 통째로 읽지 않기 위한 진입점**으로, grep 해서 `offset`/`limit`을 얻어 해당 절만 Read 한다(상세는 [agent-rules](sot/agent-rules.md#문서-읽기-규율-섹션-단위-검색))
- [llms.txt](_generated/llms.txt) — LLM용 링크 인덱스
- [llms-full.md](_generated/llms-full.md) — canonical 문서 본문 연결. 외부 LLM ingest 전용이며 에이전트는 열지 않는다
- [sot-drift-report.md](_generated/sot-drift-report.md) — env/API drift 경고
- `../LLM_WIKI.md` — Harness가 읽는 compact projection

## 도구

```bash
python scripts/wiki.py inventory   # 문서 후보·제외 목록
python scripts/wiki.py build        # 생성물 갱신
python scripts/wiki.py check        # 구조 검증(비파괴) + drift 경고
```

운영 절차(load→save→build→check)는 `/wiki` 명령([.claude/commands/wiki.md](../../.claude/commands/wiki.md)) 또는 `skills/llm-wiki-maintainer`를 따른다. 규율 정본은 [agent-rules](sot/agent-rules.md).

pre-commit 훅 활성화: `git config core.hooksPath .githooks`

---

<!-- source: docs/wiki/reports/mvp-gap.md -->
# MVP Gap Audit — 첫 read-only PoC 기준 구현 격차

# MVP Gap Audit

> **legacy(PureIron_ignition) 기준** — 이 감사는 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 코드 기준이며 PureIron_V1 코드에는 해당하지 않는다.

> **시점 스냅샷 (`docs/wiki/reports/`)** — 이 문서는 불변 계약이 아니라 frontmatter `last_verified` 시점의 **감사 결과**다. 그 이후의 코드·테스트 변경은 반영되지 않았을 수 있으므로, 여기 적힌 상태를 근거로 결론을 쓰기 전에 해당 코드/테스트로 재확인한다. 재감사하면 본문과 `last_verified`를 함께 갱신한다. 불변 계약은 [`docs/wiki/sot/`](../sot/agent-rules.md)에 있다.

이 문서는 **첫 read-only PoC**([read-only-poc-proposal](../../sales/read-only-poc-proposal.md))를 실제로 수행하기 위해 현재 MVP 구현에서 부족한 부분을 코드/테스트/SOT 기준으로 점검한 **gap audit**이다.

목적은 새 영업 문서를 늘리는 것이 아니라 **다음 구현 backlog를 결정**하는 것이다. 제품 의도·v1 범위 정본은 [product](../sot/product.md), 첫 계약 프레임은 [first-contract](../gtm/first-contract.md), 검증 기준은 [testing](../sot/testing.md)를 따른다. 이 문서는 그 위에서 "PoC를 돌리려면 지금 뭐가 부족한가"만 다룬다.

> 범위 제한: 고객 설비 안정성 위험(write/control, 중앙 제어, 사람 승인 없는 자동 조치)은 v1 제외로 **유지**한다([product](../sot/product.md) MVP 제외). 장기 플랫폼 기능(Ontology/Graph, 예측 모델, 고급 Twin, MES 전체)은 backlog로 낮춘다.

## 상태 분류 기준

| 상태 | 의미 |
|---|---|
| `implemented` | 핵심 경로가 코드+테스트로 존재하고 PoC에서 시연 가능 |
| `partial` | 코드는 있으나 PoC 조건(DB/키/현장 검증/승인 범위 등)에서 빠진 고리가 있음 |
| `missing` | PoC 흐름에 필요한데 backend/제품 기능으로 존재하지 않음 |
| `unknown` | 코드 존재는 확인했으나 PoC 수준 동작을 이 audit에서 확정하지 못함 |

## 흐름별 현재 상태

read-only PoC 핵심 흐름 8개([read-only-poc-proposal](../../sales/read-only-poc-proposal.md) §5) 기준.

| # | 흐름 | 상태 | 근거(코드/테스트) | 남은 격차 |
|---|---|---|---|---|
| 1 | Device/driver 연결(read-only) | `partial` | 6개 드라이버(`drivers/{opcua,modbus,s7,ls,ab,melsec}_client.py`) poll 루프·`DriverHealth`·`/api/devices` CRUD, `gateway._setup_drivers`가 DB devices 행으로 `driver.start()`. 전역 read-only latch `safety.py`(`PUREIRON_POC_READ_ONLY_MODE`)가 `DeviceTagProvider.write`에서 드라이버 write를 하드 차단. tests: `test_opcua_driver`·`test_modbus_driver`·`test_driver_health*`·`test_poc_read_only_mode` | 실드라이버는 `db_pool` 있을 때만 기동(무DB → 현장 수집 없음). s7/ls/ab/melsec는 현장 read 검증 근거 얕음 |
| 2 | 승인 태그 수집 + historian/trend | `partial` | `HistorianWriter`가 `tag.changed.*` 구독→`tag_history` batch insert, `historian/reader.py`+`/api/historian`, `TrendManagement.tsx`, `test_historian`. 승인 수집 계약: [approved tag collection contract](../sot/data-contracts.md#approved-tag-collection-contract) + `tags/collection_policy.py`(excluded>approved>default-deny, authored vs driver-derived), gateway 배선 `_setup_drivers`가 `configs/tags/approved_paths.json`→모든 드라이버 `approved_tag_paths`, tests `test_collection_policy_contract`·`test_approved_tag_collection_harness`·`test_gateway_approved_paths_wiring`·`test_driver_approved_collection`. DB-present pre-field gate는 `test_db_poc_smoke.py`가 historian 기록/조회 + alarm journal 영속을 한 smoke로 고정(`PUREIRON_DATABASE_URL` 없으면 skip) | DB(Timescale) 없으면 503·미영속(AC-7). gateway/driver allowlist 배선은 OPC-UA·Modbus·S7·LS·AB·MELSEC 모두 완료. 2주 PoC retention/조회창은 `027_add_tag_history_retention.sql`(30일 drop-after)+downsample 조회로 고정([data-contracts](../sot/data-contracts.md#historian-retention--query-window)); Trend 화면 CSV export로 리뷰용 산출물은 생성 가능. 현장 전 DB-present smoke는 고정됐고, 남은 격차는 실제 고객 DB+다중 드라이버 e2e |
| 3 | 대시보드 주요 상태 확인 | `implemented` | `dashboard/router.py`+`DashboardGrid`/widgets, WS 250ms batch, process/SCADA 화면, `AlarmSummary.tsx` 모바일 ack/note card, `test_user_dashboard`·`test_alarm_summary_ui_contract` | 모바일 알람 확인의 실제 브라우저 스크린샷/현장 증거는 PoC 전 수동 smoke 실행 시점에 남김 |
| 4 | 알람 발생/확인/조치 이력 | `partial` | `alarms/engine.py`(발생), ack 전이(AC-5/AC-6), `alarms/journal.py`가 operator·note 포함 전이를 `alarm_journal`에 영속, `AlarmSummary` active row의 조치 note 저장 UI와 History의 operator/note 표시, tests `test_alarm_ack_note_history_contract`·`test_alarm_summary_ui_contract`, PoC 전 수동 증거 절차는 [PoC browser smoke runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/poc-browser-smoke.md) | journal은 `db_pool` 없으면 조용히 no-op(무DB → 조치 이력 미영속). 실제 고객/PoC 브라우저 스크린샷은 PoC 전 실행 시점에 남겨야 함 |
| 5 | Agent troubleshooting | `partial` | `agents/`(supervisor + tag/alarm/history/health/logs agent), tools `read_tag`/`get_active_alarms`/`query_history`, 구조 검증 AC-12·AC-18c. read-only smoke `test_agent_troubleshooting_smoke.py`가 결정론적 chat model seam(외부 키 불필요)으로 읽기 grounding + write 도구 하드 차단을 고정 | 실제 provider/키에서의 답변 품질은 [agent troubleshooting answer quality runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/agent-troubleshooting-answer-quality.md)으로 수동 평가 경로를 고정했다. 실제 고객/PoC 환경에서 AQ-1~AQ-6 실행과 sanitization 증거 확보는 여전히 PoC 실행 시점 작업이다. 온프레미스 provider/키 취급은 [runtime-config](../sot/runtime-config.md)에 문서화됨 |
| 6 | 사용자/권한/감사 최소 구성 | `implemented` | JWT+refresh(AC-1), 보호 401(AC-2), RBAC 4역할+write RBAC(AC-10), `auth/audit.py`·`auth/audit_utils.py`·`user_repository.py`, `AccountsTab`/`SecurityTab`. `test_auth.py`가 login/profile/password/admin 계정 변경의 감사 로그 기록·비밀번호 평문 비노출·audit sink 실패 격리를 고정하고, `test_logs_router.py`·`test_log_triage.py`·`test_user_dashboard.py`·`test_user_settings.py`가 로그/triage/dashboard/settings 운영 경로의 safe audit 경계를 고정 | 최소 구성 충족. 감사 sink 장애는 핵심 auth/profile/password 및 운영 보조 요청을 실패시키지 않는 warn-and-continue 경계로 격리된다. v1은 서버 session store/token blacklist 없이 JWT 만료 기반 active session policy를 명시한다 |
| 7 | 온프레미스 우선 배포/환경 | `partial` | `docker/docker-compose.yml`·`docker-compose.dev.yml`·`Dockerfile.*`·`nginx.conf`, `config.py` env. PoC 전용 `docker/docker-compose.poc.yml`(timescaledb+redis+backend+frontend, simulator 없음·DB/Redis host port 미공개)+`.env.poc.example`+[on-prem read-only PoC runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/on-prem-read-only-poc.md)(구성/배포/검증/백업/롤백/handoff·수집망 분리·retention §8) | 오프라인 설치 번들 자동화는 runbook non-goal로 post-P0 유보(§2). 실제 고객 호스트 현장 검증은 유료 PoC 착수 시 |
| 8 | demo/simulator + 3D Twin + Work Order | `partial` | Simulator(`CoffeeTankProcessSimulator`/`TagSimulator`) 자동 기동, 3D Twin(`components/twin/*`·`models/router.py`·tag binding AC-16/17), 데모 spine AC-18 | **Work Order는 backend 기능 아님**(`missing`, v1 제외 유지) — [work-order-demo-handoff 런북](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/work-order-demo-handoff.md) + [testing](../sot/testing.md) §5 AC-18로 **수동 handoff 데모 경로/체크리스트 고정**(알람 ack·note·이력 → 수동 작업 지시 handoff). `mes/`는 recipe/plan/task이지 work order 아님. 3D Twin 시각 highlight·Agent 답변 품질은 수동 검증 |

## 교차(cross-cutting) 격차

흐름 여러 개를 동시에 막던 항목. **read-only PoC-safe 모드 부재는 해결**(아래 1)됐고, 남은 무거운 잔여는 DB 존재 경로의 현장 검증과 실 LLM 답변 품질이다.

1. ✅ **read-only PoC-safe 모드 — 해결(2026-07-04).** 제안서의 "쓰기/제어 명령 미포함"([read-only-poc-proposal](../../sales/read-only-poc-proposal.md) §5·§7)이 이제 관례가 아니라 정책 강제다. 전역 latch `PUREIRON_POC_READ_ONLY_MODE`(`config.py`→`safety.py`)가 드라이버 write(`DeviceTagProvider.write`)·REST tag write(403)·agent control/schedule tool·approved-action executor를 하드 차단하고 `/api/health`가 `poc_read_only_mode`를 노출한다. 내부 수집(driver poll/simulator `write_value`)·읽기 경로는 그대로 동작한다. `test_poc_read_only_mode.py`가 회귀를 고정한다.
2. **DB 존재 경로 현장 검증(잔여).** 실드라이버 기동·historian 영속·alarm journal이 `db_pool` 없으면 조용히 축소된다. [on-prem runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/on-prem-read-only-poc.md)+`docker-compose.poc.yml`이 timescaledb 포함 경로를 문서화·검증 가능하게 만들었고, `test_db_poc_smoke.py`가 `PUREIRON_DATABASE_URL`이 있는 환경에서 historian 기록/조회와 alarm journal operator/note 영속을 한 pre-field smoke로 고정한다. 남은 것은 실제 고객 DB+다중 드라이버 end-to-end 현장 검증이다(CI는 DB env 없으면 skip).
3. ✅ **실 LLM 답변 품질 평가 경로 — runbook 고정(2026-07-07).** troubleshooting 읽기 grounding·write 차단은 키 없는 결정론 seam(`test_agent_troubleshooting_smoke.py`)으로 검증되고, 실제 provider/키가 있을 때의 답변 품질은 [agent troubleshooting answer quality runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/agent-troubleshooting-answer-quality.md)의 AQ-1~AQ-6 질문 세트·pass/fail rubric·증거 sanitization으로 수동 검증한다. 온프레미스 provider/키 취급은 [runtime-config](../sot/runtime-config.md)에 문서화됐다. 남은 일은 실제 고객/PoC 환경에서 runbook을 실행하고 sanitized evidence를 남기는 것이다.

## 다음 구현 우선순위 (issue 후보)

첫 계약 가능성·설비 안정성 기준으로 정렬. `[P0]`가 PoC 착수를 직접 막는다. 완료 항목은 traceability를 위해 ✅로 남긴다.

1. ✅ **[P0] read-only PoC-safe 모드 flag — 완료(2026-07-04).** `PUREIRON_POC_READ_ONLY_MODE` 전역 latch(`config.py`→`safety.py`)가 드라이버 write·REST tag write(403)·agent control/schedule tool·approved-action executor를 하드 차단하고 `/api/health`가 `poc_read_only_mode`를 노출한다. 제안서의 "쓰기/제어 미포함"이 관례가 아니라 정책 강제가 됐다. `test_poc_read_only_mode.py`가 회귀를 고정. env 계약은 [runtime-config](../sot/runtime-config.md).
2. ✅ **[P0] 온프레미스 PoC 배포 runbook — 완료(2026-07-04).** read-only PoC용 검증 가능한 compose 프로파일 `docker/docker-compose.poc.yml`(timescaledb+redis+backend+frontend, simulator 없음·DB/Redis host port 미공개), `.env.poc.example`, 수집망 분리·검증·백업·롤백·handoff를 [on-prem read-only PoC runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/on-prem-read-only-poc.md)에 고정. 남은 격차: 오프라인 설치 번들 자동화는 runbook non-goal로 post-P0 유보(§2), 실제 고객 호스트 현장 검증은 유료 PoC 착수 시.
3. **[P1] 승인 태그 현장 수집 검증 harness — 계약/harness/전체 드라이버 allowlist 배선 + DB-present pre-field smoke 완료(2026-07-10).** 수집 대상 whitelist/승인 개념을 제외목록 위에 얹는 [approved tag collection contract](../sot/data-contracts.md#approved-tag-collection-contract)를 SOT에 고정하고, 코드 정본 `tags/collection_policy.py`(excluded>approved>default-deny, authored vs driver-derived)와 순수 계약 test(`test_collection_policy_contract`) + modbus simulator→store→historian harness(`test_approved_tag_collection_harness`)를 추가했다. `ScadaGateway._setup_drivers()`가 startup에서 `configs/tags/approved_paths.json`를 한 번 읽어 OPC-UA·Modbus·S7·LS·AB·MELSEC driver `approved_tag_paths`로 전달하며(파일 없음→`None`, 빈 list→deny-all), `test_gateway_approved_paths_wiring`·`test_driver_approved_collection`이 gateway 배선과 각 드라이버 materialize/poll fallback 필터를 고정한다. `test_db_poc_smoke.py`는 `PUREIRON_DATABASE_URL`이 있는 환경에서 historian 기록/조회와 alarm journal 영속을 한 smoke로 검증한다. 남은 격차: 실제 고객 DB+다중 드라이버 end-to-end 현장 검증.
4. ✅ **[P1] 알람→ack→조치 note→이력 end-to-end — 계약/수동 smoke 경로 완료(2026-07-07).** backend `AlarmEngine.acknowledge`/`add_note`/`alarm_journal` 계약은 `test_alarm_ack_note_history_contract`가 고정하고, frontend `AlarmSummary` active row에서 조치 note를 `/api/alarms/{name}/notes`로 저장하며 History 화면에서 operator/note를 드러내도록 보강했다. `test_alarm_summary_ui_contract`가 UI/API 문자열 계약을 회귀 방지한다. PoC 전 브라우저 수동 smoke와 sanitized screenshot/CSV/notes 증거 절차는 [PoC browser smoke runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/poc-browser-smoke.md)에 고정했다. 남은 것은 실제 고객/PoC 환경에서 해당 런북을 실행해 증거를 남기는 일이다.
5. ✅ **[P1] Agent troubleshooting PoC 검증 경로 — smoke + 수동 품질 runbook 완료(2026-07-07).** 외부 키 없는 결정론적 chat model seam으로 알람 상황에서 read-only 도구(device health)로 답을 grounding하고 write/control 도구는 하드 차단됨을 `test_agent_troubleshooting_smoke.py`가 고정한다. 실제 provider/키에서의 답변 품질은 [agent troubleshooting answer quality runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/agent-troubleshooting-answer-quality.md)으로 질문 세트(AQ-1~AQ-6), pass/fail rubric, secret/customer-data sanitization 증거 기준을 고정했다. 필요한 provider/키의 온프레미스 취급은 [runtime-config](../sot/runtime-config.md)에 문서화. 남은 일은 실제 고객/PoC 환경에서 runbook을 실행하는 것이다.
6. ✅ **[P2] Historian retention/query window + Trend export — 완료(2026-07-07).** 2주 PoC에 맞는 retention/조회창을 `027_add_tag_history_retention.sql`(30일 drop-after, 멱등)+요청 창별 downsample(`historian/reader.query_aggregated`)로 고정하고 [data-contracts](../sot/data-contracts.md#historian-retention--query-window)에 계약화, 조정 SQL은 [runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/on-prem-read-only-poc.md) §8. Trend Management의 Download Data가 현재 선택 태그·조회 기간·시계열 값·pen statistics를 UTF-8 BOM CSV로 내보내 PoC 결과 리뷰 산출물로 쓸 수 있다.
7. ✅ **[P2] Work Order 데모 경로 명확화(backend는 v1 제외 유지) — 완료(2026-07-03).** backend Work Order는 v1 밖으로 유지([testing](../sot/testing.md))하고, 영업 데모가 반복 가능하도록 [work-order-demo-handoff 런북](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/work-order-demo-handoff.md)에 수동 handoff 스크립트·검증 체크리스트를 고정했다(알람 ack·조치 note·이력 → Agent troubleshooting → 수동 작업 지시 handoff). SOT/testing/product 문구를 "backend 기능"이 아니라 "demo/manual handoff"로 정렬. 다음 단계: backend Work Order API가 생기면 AC-18 자동화 표로 승격(현재는 v1 제외 유지). 범위 확대 아님.
8. **[P3] 멀티 드라이버 현장 준비도.** s7/ls/ab/melsec 통합 검증 확대는 실제 고객 PLC가 정해진 뒤로 미룬다(`needs-founder-input`).
9. ✅ **[베이스 강화] Core domain model boundary 고정 — 완료(2026-07-04).** 단기 영업 흐름에 코어 모델이 흐려지지 않도록 Tag(정의≠런타임 값)·Device/DriverHealth·Alarm/AlarmJournal·Historian(append/query)·Agent(read-only projection)·Work Order/MES-lite 경계를 [architecture](../sot/architecture.md#core-domain-model-boundary-sot) 한 절에 SoT로 고정하고, `backend/tests/test_core_domain_boundary_contract.py`가 문구·금지 패턴(agent read-only, device health가 태그로 새지 않음)으로 silent drift를 막는다. 새 기능/스키마 추가가 아니라 기존 경계 명문화다.

## 다음 후보 backlog (잔여만)

위 표에서 ✅가 아닌, 첫 read-only PoC를 실제로 굴릴 때 남은 후보다. PoC-safe 모드·온프레미스 runbook·retention은 이미 완료됐으므로 착수 자체를 막는 P0는 없다.

1. **실제 고객 DB+다중 드라이버 e2e 현장 검증(교차 격차 2 + #3 잔여).** `docker-compose.poc.yml` 경로와 `test_db_poc_smoke.py` pre-field smoke는 고정됐으므로, 유료 PoC 착수 시 실제 고객 호스트/DB/드라이버 조합에서 수집·영속·조회 증거를 남긴다.
2. **실 LLM 답변 품질 runbook 실행(#5 잔여).** 평가 경로는 고정됐고, provider/키가 있는 실제 PoC 환경에서 AQ-1~AQ-6 답변 품질과 sanitized evidence를 확인해야 한다.
3. **멀티 드라이버 현장 준비도(#8, `needs-founder-input`).** s7/ls/ab/melsec 통합 검증 확대는 실제 고객 PLC 확정 후.

## v1에서 유지되는 제외 (backlog 낮춤)

audit 결과와 무관하게 v1 제외로 **유지**한다. 근거는 [product](../sot/product.md) MVP 제외와 [agent-rules](../sot/agent-rules.md).

- 설비 write/control, 사람 승인 없는 자동 조치, 중앙 제어.
- 예측 모델 기반 이상 감지, 완전 예측 AI Simulation.
- Ontology/Graph 원인 추론, 고급 Digital Twin 완성도.
- 완전한 MES, 범용 no-code config builder.

## 검증

이 문서는 SOT/문서 변경이므로 아래를 실행한다.

```bash
python scripts/wiki.py build
python scripts/wiki.py check
```

---

<!-- source: docs/wiki/reports/vm-e2e-runs.md -->
# Hyper-V VM 실측 기록 — release 쌍별 M2d 하네스 결과

# Hyper-V VM 실측 기록

> **시점 스냅샷 (`docs/wiki/reports/`)** — 이 문서는 불변 계약이 아니라 각 실측 run 시점의 **결과 기록**이다. 하네스·제품이 그 뒤에 바뀌었을 수 있으므로, 여기 적힌 수치를 근거로 결론을 쓰기 전에 해당 run의 `results.json`이나 새 실측으로 다시 확인한다. 새 실측을 하면 절을 더하고 `last_verified`를 갱신한다. 하네스 구성·판정의 정본은 [`tests/e2e/README.md`](../../../tests/e2e/README.md), 계약 쪽 요약은 [data-contracts](../sot/data-contracts.md)의 M2d VM 절이다.

M2d 하네스(`tests/e2e`, PD-24)를 release 쌍(`tests/e2e/m2d/release_pair.py`)마다 한 번씩 `run.py all`로 돌린 결과다. 항목은 매번 30개(`run.expected_items()`)이고, 결과 파일은 `artifacts/e2e/m2d-<run>/results.json`·`bundle-metrics.json`(gitignore)이다.

## M2 r0→r1 (run `20261005T055523Z`, 30/30 PASS)

plan M2 종료 조건 실측.

- 폐쇄망 부트스트랩: runbook 2단계(root 셸, 메타데이터만 추출 → `bootstrap-verify.sh` sha256·root 지문) → `install.sh --offline-time-confirmed` → enroll → g1(r0) 51 s, `/readyz` 200, overlay2 + classic image store, 실행 이미지 `.Id` == r0 config digest.
- 거부(VM 수동 1회): 다른 현장 `E_SITE`·변조 `E_DIGEST`·낮은 gen `E_GEN` exit 1, 같은 gen 재빌드는 R-7대로 no-op exit 0 — 모두 core `StartedAt`·실행 이미지 불변. 커밋 전 거부 보고는 gen을 믿지 않아 `g unknown`이라 번들 sha256 12자로 찾는다. 먼저 빌드한 낮은 gen 번들은 cert 단계 `max_seen_created_at` 역행(`E_CERT`)에 먼저 걸린다.
- 압축 chunk 3개가 있는 epoch 1 DB에서 r0→r1 24 s(`switching` 11 s, `migrating` 2 s) → epoch 2·`tag_history_1h`, 이력 행 보존.
- r1′(collector 세션 없음) → `E_HEALTH`, 134 s 안에 r0 digest 복귀 + `failed`(5분 이내, `checking` 90 s·`rolling_back` 19 s).
- crash 18회(phase `fetched`~`rolling_back` × `systemctl kill -s KILL` / `Restart-VM -Type Reset`): 모두 15~106 s 안에 수렴 — 결과·`applied_gen`·실행 이미지·편집 잠금 해제·경보 없음·이력 행 보존. 감시자가 phase 저장 직후 서비스 cgroup을 얼리므로 `migrating`은 "기록 뒤·다음 기록 전"을 끊는다(SQL 중간 중단은 core 통합 테스트 몫).
- nftables 외부 접속 시도(설치 전 기준값·설치 이후 누적, 복원·리셋 직전마다 누적) 0. 외부 DNS 패킷은 모두 systemd-resolved가 보내므로 하네스는 resolved 디버그 로그의 조회 이름을 출처 증거로 남긴다.
- 번들 크기·빌드 시간은 `bundle-metrics.json`(install 80 MB, 현장 번들 739 MB 중 site-db 599 MB, 빌드 약 58 s).
- 이 실측이 찾은 제품 결함 2건(서비스 롤백 `failed_manual`, TimescaleDB 텔레메트리)은 같은 PR에서 고쳤다 — 내용은 data-contracts M2d VM 절.

## S3 r1→r2 (run `20261009T231040Z`, 30/30 PASS, missing 0, 2026-10-10)

같은 하네스, 쌍만 `release_pair.py`로 바꿈(fault `r2f`). 제품 결함·하네스 수정 없이 한 번에 통과.

- 폐쇄망 부트스트랩 + g1(r1) 56 s. c0 = epoch 2(1분·1시간 CAGG) + 압축 chunk 3 + 이력 7141행.
- r1→r2 25 s(`switching` 12 s, `migrating` 2 s) → epoch 3, 1분·1시간 CAGG 실시간 집계, `project_state.historian` 열, 압축 chunk·이력 행 보존. 그 위에서 낮은 gen `E_GEN`. c0에서 `E_SITE`·`E_DIGEST` 거부와 같은 gen no-op도 M2와 같다(core `StartedAt`·실행 이미지 불변).
- r2′ → `E_HEALTH`, 135 s 안에 r1 digest 복귀 + `failed`(`checking` 90 s·`rolling_back` 20 s). crash 단계의 r2→r1 시도 7회도 `succeeded`(헬스 통과) — r1 앱(envelope [2, 3])이 epoch 3 DB에서 ready인 롤백 경로 실측.
- crash 18회 모두 16~109 s 안에 수렴(`fetched`·`verified` 92~109 s, 나머지 16~38 s, M2는 15~106 s). nftables 외부 접속 시도 누적 0.
- 번들: install 80 MB, r2 현장 번들 739 MB(site-db 599 MB, core 61 MB), 빌드 약 55 s.

## S4 r2→r3 (run `20261017T132857Z`, 30/30 PASS, missing 0, 2026-10-17)

같은 하네스, 쌍 r2→r3(fault `r3f`). 1차 run `20261017T113834Z`는 16/30 — 하네스 결함 1건을 고친 2차가 이 기록이다. 제품 코드는 바꾸지 않았다.

- 폐쇄망 부트스트랩 + g1(r2) 53 s. c0 = epoch 3(1분·1시간 CAGG 실시간 집계, 0008·0009 열) + 압축 chunk, epoch 4 테이블 없음.
- r2→r3 25 s(`switching` 12 s, `migrating` 2 s) → epoch 4, `users`·`sessions`·`audit_log`·`alarms`·`alarm_journal`과 인덱스 5개, CAGG·BASE 열·압축 chunk·이력 행 보존. `E_SITE`·`E_DIGEST`·`E_GEN` 거부와 같은 gen no-op은 M2·S3와 같다(core `StartedAt`·실행 이미지 불변).
- r3′ → `E_HEALTH`, 135 s 안에 r2 digest 복귀 + `failed`(`checking` 90 s·`rolling_back` 20 s). crash 단계의 r3→r2 시도도 `succeeded` — r2 앱(envelope [3, 4])이 epoch 4 DB에서 ready인 롤백 경로 실측.
- crash 18회 모두 16~99 s 안에 수렴(`fetched`·`verified` 94~99 s, 나머지 16~57 s). service 대상 16회는 스테이징이 창보다 78~82 s 먼저 끝났다(apply rc 0, 1회). nftables 외부 접속 시도 누적 0.
- 번들: install 80 MB, r3 현장 번들 740 MB(site-db 599 MB, core 61 MB), 빌드 약 60 s.
- **1차 실패와 수정**: crash 14건이 `staged_before_window`만 false(복구 판정은 모두 true). 14건은 정확히 "apply ssh가 끝날 때 crash 감시자 ssh가 살아 있던" 케이스였다(감시자가 먼저 끝나는 `verified`, CLI 대상 `fetched`는 통과). 실측을 콘솔에서 띄우면 동시에 도는 ssh 둘이 같은 콘솔 stdin을 물려받는다 → `tests/e2e/infra/vm.py`의 ssh·scp를 모두 `stdin=DEVNULL`(`ssh -n`과 같음)로 띄우게 고쳤다. 2차는 같은 조건(콘솔 실행, 14건 모두 `watcher_alive` true)에서 통과했다. 하네스는 crash마다 `stage`(apply rc·시도 횟수·창까지 여유·감시자 생존)를 결과에 남긴다.

---

<!-- source: docs/wiki/sot/agent-rules.md -->
# Agent Rules SOT — 에이전트 작업 규칙

# Agent Rules SOT

> **PureIron_V1 기준** — 아래 `불변 규칙`은 V1 새 코드(`services/`·`packages/`·`deploy/`)에 적용하며, 아직 구현 전이라 [ADR](../adr/index.md) 019~028의 목표 설계를 따른다. legacy [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 코드를 읽을 때의 규칙은 `legacy 코드 참고 규칙` 절에만 있고 새 코드에 적용하지 않는다.

LLM 에이전트가 PureIron에서 작업할 때 따르는 규칙의 **정본**이다.

`AGENTS.md` / `CLAUDE.md` / `README.md`는 이 문서와 다른 SOT 문서로 연결되는 짧은 projection이다. 장문 규칙, 제품 설명, 아키텍처 상세를 루트 projection에 중복해서 쓰지 않는다.

## 이 문서의 역할

이 문서는 “에이전트가 PureIron에서 어떻게 일해야 하는가”만 다룬다.

위키는 **문서의 수명**으로 구획을 나눈다. 새 문서를 만들 때 어느 구획에 속하는지 먼저 정한다.

| 구획 | 수명 | 성격 |
|---|---|---|
| `docs/wiki/sot/**` | 불변 | 계약. 코드와 함께 바뀌지만 "시점" 개념이 없다 |
| `docs/wiki/gtm/**` | 결정 시점에 고정 | 사업/첫 계약 결정. canonical 이지만 엔지니어링 계약이 아니다 |
| `docs/wiki/reports/**` | 시점 스냅샷 | 감사·audit. `last_verified` 이후 낡을 수 있다 |
| `docs/wiki/adr/**` | 결정 시점에 고정 | 아키텍처 결정 근거 |
| `docs/wiki/archive/**` | 동결 | legacy 포인터. `llms-full.md` 입력에서 제외 |

**감사·격차 점검·현황 조사 결과를 `sot/`에 두지 않는다.** 그것은 계약이 아니라 스냅샷이고, 낡은 스냅샷은 없는 것보다 나쁘게 작동한다(실제와 다른 상태를 근거로 결론이 나옴). `reports/`에 두고 `last_verified`를 갱신한다. 90일이 지나면 `wiki.py check`가 `[stale-report]` 경고를 낸다.

문서별 책임은 다음과 같이 나눈다.

| 문서 | 책임 |
|---|---|
| [product](product.md) | 제품 의도, 고객, v1 가치, 장기 방향 |
| [first-contract](../gtm/first-contract.md) | 첫 계약 GTM/PoC/데모/가격 결정 프레임(미확정은 `가설`/`TBD`/`needs-founder-input`) |
| [architecture](architecture.md) | 시스템 구조, 런타임 구성, 모듈 경계 |
| [data-contracts](data-contracts.md) | DB·설정 데이터 계약 |
| [api-events](api-events.md) | REST / WebSocket / SSE / 이벤트 계약 |
| [frontend](frontend.md) | 프론트엔드 구조와 제약 |
| [testing](testing.md) | 검증 명령과 인수 기준 |
| [mvp-gap](../reports/mvp-gap.md) | 첫 read-only PoC 기준 구현 격차(시점 스냅샷) |
| [adr](../adr/index.md) | 아키텍처 결정 근거 |
| [USER.md](../../../USER.md) | 왕순환/팀 역할과 작업 성향 |

에이전트는 특정 내용을 수정할 때 해당 책임 문서에 저장한다. 같은 내용을 여러 SOT에 반복하지 않는다.

## 작업 전 컨텍스트 로딩 순서

PureIron 작업을 시작할 때는 다음 순서로 컨텍스트를 확인한다.

1. 현재 대화에서 사용자가 직접 준 지시
2. `USER.md` — 사용자/팀 역할, 작업 성향
3. `docs/wiki/sot/product.md` — 제품 의도와 v1 범위
4. 작업 성격에 맞는 SOT 문서
   - 아키텍처: `architecture.md`
   - DB/설정: `data-contracts.md`
   - API/이벤트: `api-events.md`
   - 프론트엔드: `frontend.md`
   - 테스트: `testing.md`
5. 필요한 경우 projection 문서: `AGENTS.md`, `CLAUDE.md`

루트 projection보다 `docs/wiki/sot/**`를 우선한다.

## 제품 판단 원칙

PureIron의 v1 판단 기준은 다음이다.

1. 첫 계약 가능성
2. 고객 설득력
3. 2인 팀 감당 가능성
4. 장기 플랫폼화
5. 실행 속도
6. 기술적 완성도

에이전트는 기술적으로 멋있지만 v1 고객 설득과 첫 계약에 직접 기여하지 않는 기능을 보류해야 한다.

PureIron은 PLC 로직(프로그램)을 바꾸지 않는다. 기본은 읽기 전용이고, MES를 위한 기존 PLC 주소 쓰기(설정값·운전 조건·운전 명령)는 고객이 승인한 현장에서 ADR-013 가드레일 아래에서만 한다. 무인 자동 제어, 완전 예측 AI Simulation, 사람 승인 없는 자동 조치(AI 단독 실행 포함)는 v1 범위가 아니다.

## 불변 규칙

- No ORM — asyncpg raw SQL만 사용한다(core·control-api 모두). 근거 ADR-001
- 단일 조립 지점 — core는 `SiteRuntime.build(settings)` → `create_app(rt)`에서만 조립하고 의존성은 생성자로 주입한다. 전역 싱글톤 조회·`app.state` 금지, 모듈 경계는 import-linter 계약으로 강제. 근거 ADR-007 개정·ADR-019
- 서비스 경계 — collector는 무상태·DB 무접근, collector↔core 통신은 `collector.proto` gRPC 계약만. core 내부 이벤트 pub/sub은 프로세스 안에서만 쓴다. 근거 ADR-010·ADR-020
- Lock discipline — TagStore 락 안에서 DB I/O·이벤트 발행 금지(데드락 방지). 근거 ADR-009
- 계약 우선 — `collector.proto`·OpenAPI·JSON Schema가 정본이다. 호환성 diff를 통과해야 하고 생성물은 재생성 diff 0을 유지한다. 근거 ADR-028
- 마이그레이션 — 스키마 변경은 `pureiron-core migrate`의 ADD-only 마이그레이션만. 부팅 시 자동 실행 금지, 제거는 epoch 규칙(deprecated 후 다음 epoch 이후)으로만. 근거 ADR-024
- 서명 번들 — 현장 변경은 서명된 `.pib`로만 반입한다. 검증 실패는 서비스 중단 전에 거부, registry pull 금지, 이미지는 config digest로 확인. 근거 ADR-023
- 프로젝트 정본 — 고객 설정 정본은 고객 project repo(Git)다. 비밀값은 `secret://<name>` 참조만, 제품 코드에 고객별 분기(`site_id == "…"`, 고객 이름) 금지. 근거 ADR-025·ADR-026
- Graceful degradation — 선택 서비스(`ai`·중앙)가 없어도 현장 모니터링·알람·이력은 동작한다. collector 다운 = 해당 장비 BAD/DISCONNECTED, site-db는 필수(core readiness 조건). 근거 ADR-008 개정
- Device health is not a tag — 장비 상태는 collector `DeviceHealth` → core `GET /api/v1/devices`가 단일 소스, 태그로 만들지 않는다. 근거 ADR-018
- PLC 로직 불변·쓰기 가드레일 — PLC 프로그램을 수정·다운로드하지 않는다. 쓰기는 core만 `WriteRequest`로 보내며, 프로젝트에 선언된 태그(설정값은 범위, 운전 명령은 허가 조건 필수)만, 현장 스위치(기본 꺼짐)가 켜진 현장에서, 사람 확인(명령은 2단계) 또는 사람이 승인한 예약으로만 한다. 실행 전 품질·연결·범위·허가 검사, 실행 후 readback, 모든 시도 감사 로그. 자동 재시도·AI 단독 실행·업데이트 중 쓰기 금지. 근거 ADR-013(개정)·ADR-014
- No plugin system — 프로토콜은 collector `drivers/`에 직접 구현하고, 추가는 proto `TagPoint.address` oneof 확장으로만 한다. registry·동적 로딩 금지. 근거 ADR-002
- No config UI — 인프라·env·드라이버·범용 config 빌더 UI 금지. 현장 편집은 REST `PATCH`만(아래 “No config UI” 범위 절). 근거 ADR-004·ADR-025
- 20개 고정 expression 함수 — 확장 불가, 복잡 로직은 샌드박스 스크립트로. 근거 ADR-005
- 엄격 게이트 — pyright strict·tsc/eslint strict, 억제 주석(`type: ignore`·`eslint-disable`·`as any`·`@ts-ignore`·`@ts-expect-error`) 0건. 생성 코드(`_gen/`)는 억제 대신 검사 제외 + drift 게이트. 근거 ADR-011·ADR-028

> 위 bullet은 `scripts/wiki.py build`가 파싱해 `docs/LLM_WIKI.md`·`docs/wiki/_generated/agent-context.md`로 투영한다. 한 줄을 고치면 build 후 양쪽에 반영된다. 결정 근거·트레이드오프는 [adr](../adr/index.md) 참조.

## legacy 코드 참고 규칙 (PureIron_ignition)

이식 작업에서 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 코드를 읽을 때의 맥락이다. **V1 새 코드에는 적용하지 않는다.**

- legacy 백엔드 상태는 `ScadaGateway` 싱글톤 `gateway.*`에 모여 있다(`request.app.state` 미사용). V1은 이 패턴을 쓰지 않는다(ADR-007 개정)
- legacy 마이그레이션은 부팅 시 `backend/src/pureiron/db/migrations/*.sql` 알파벳순 자동 실행이다(ADR-006, V1에서는 ADR-024로 대체)
- legacy 선택 서비스는 DB/Redis/RAG/LangGraph이고, 실패해도 서버가 계속 뜬다
- legacy 태그 변경은 `tag.changed.*` pub/sub, 장비 상태는 `DriverHealth.snapshot()`(`/api/devices`)이다
- legacy v1 운영 설정 화면(Tag/Device/Alarm/User/Dashboard)과 `/api/settings`·`/api/tags`·`/api/devices`가 존재한다

## “No config UI” 범위

“No config UI”는 **모든 설정 화면 금지가 아니다.** PureIron frontend는 운영자가 매일 쓰는 경량 SCADA 도구이므로([frontend](frontend.md)) v1 운영 설정 화면은 제품에 필요하다. 이 규칙이 막는 것은 제품 코어를 무겁게 만드는 범용 config builder와 인프라 설정 UI이며, 운영에 필요한 고정 스코프 설정 화면은 정상 범위다.

**금지(추가하지 않는다):**

- 런타임 인프라/env(DB·Redis·포트·시크릿 등) 설정 UI — 인프라 설정은 env와 [runtime-config](runtime-config.md)로만 한다.
- 플러그인 marketplace/registry UI, 프로토콜·드라이버를 UI에서 동적으로 등록·확장하는 빌더 — 위의 “No plugin system” 규칙과 연결된다.
- 임의 스키마를 그리는 schema-less 범용 no-code config builder, drag-and-drop config UI builder.

**허용(v1 운영에 필요):**

- Tag/Device/Alarm/User/Dashboard 등 v1 운영 설정의 REST API와 고정 스코프 설정 화면. API 계약은 [api-events](api-events.md), 화면 규칙은 [frontend](frontend.md)를 따른다.
- seed/demo 초기값을 담은 YAML/JSON config.

legacy의 Settings/Tag/Device 화면과 `/api/settings`·`/api/tags`·`/api/devices` 등 API는 이 허용 범위에 속한다.

**V1(새 경로)**: 운영 설정의 정본은 고객 project repo(YAML)다. 현장 화면은 로그인 + 태그 현황·알람/ACK·트렌드·장비 상태·감사 로그 5개이고 **설정 편집 화면은 없다**. 현장 긴급 수정은 REST `PATCH`(드래프트, `site_rev` 증가)로만 하고 벤더가 고객 repo로 회수한다(ADR-025). 결정 근거와 트레이드오프는 [adr](../adr/index.md)의 ADR-004(YAML + REST Config Only)를 따른다.

## 구현 작업 규율

에이전트는 성급하게 코딩부터 하지 않는다. 그러나 구현을 요청받았는데 설명만 하고 멈추지도 않는다.

기본 흐름은 다음과 같다.

1. 관련 문서와 기존 구조 확인
2. 목적과 고객 가치 확인
3. v1 범위와 장기 확장성 확인
4. 최소 설계
5. 작은 단위 구현
6. 실제 실행 또는 테스트
7. 결과 검증
8. 필요한 문서 반영

V1 새 코드를 쓰거나 리뷰할 때는 [coding-style](coding-style.md)(구조·추상화 기준·이름·주석 언어·테스트·리뷰 체크리스트)을 따른다.

완료 보고는 실제 실행 결과에 근거해야 한다. 실행하지 않은 결과를 실행한 것처럼 말하지 않는다.

## 작업 증거 규율

리뷰용 화면 증거(스크린샷)를 저장소에 커밋하지 않는다. legacy의 `docs/pr-evidence/**` 는 동결됐고 PureIron_V1로 가져오지 않았다(`.gitignore` 가 신규 유입을 막는다).

- 증거는 **PR 본문에 직접 업로드**한다. GitHub 첨부 스토리지에 올라가 브랜치를 지워도 살아남고 저장소가 커지지 않는다.
- 업로드가 불가능한 CLI/자동화 경로에서 부득이 커밋해야 하면, 링크를 **브랜치명이 아니라 커밋 SHA permalink**로 건다. 브랜치명 ref 는 병합 후 브랜치 삭제와 함께 죽는다(기존 `docs/pr-evidence/**` 링크가 전부 이 이유로 깨졌다).
- 회귀 방지 가치가 있는 증거는 스크린샷 대신 **계약 테스트**로 고정한다. 이미지보다 오래 가고 CI 가 지켜준다(기준은 [testing](testing.md)).
- 배경과 대체 절차는 legacy [`docs/pr-evidence/README.md`](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/pr-evidence/README.md).

## 위키 규율

- Wiki/SOT 작업은 `skills/llm-wiki-maintainer` 절차를 따른다: **load → save → build → check**.
- SOT 내용은 `docs/wiki/**`의 canonical 문서에만 저장한다.
- 투영 문서(`AGENTS.md`, `CLAUDE.md`, `README.md`)는 사용자가 명시 요청한 범위에서만 짧게 고친다.
- 문서 수정 후 `python scripts/wiki.py build`로 생성물을 갱신하고 함께 커밋한다.
- `docs/wiki/_generated/**`와 `docs/LLM_WIKI.md`는 **수기 편집 금지**다.
- secret/API key 값을 위키 본문에 복사하지 않는다. `sources`는 실재 경로만, glob은 `source_globs`만 사용한다.
- `check`를 통과시키려고 소스 코드(`config.py`·라우터 등)를 임의로 바꾸지 않는다. drift는 경고일 뿐이다.

## 문서 읽기 규율 (섹션 단위 검색)

SOT 문서는 대부분 10KB를 넘고 `testing.md`는 27KB다. 한 가지 규칙을 확인하려고 문서를 통째로 Read 하면 그 자체로 컨텍스트를 소진한다. **문서 단위가 아니라 섹션 단위로 읽는다.**

1. `Grep(pattern=<키워드>, path="docs/wiki/_generated/section-index.tsv")` — 매칭된 줄만 돌아온다.
2. 줄 형식: `doc_id · path · anchor · offset · limit · bytes · level · heading` (탭 구분).
3. `Read(path, offset=<offset>, limit=<limit>)` — 해당 절만 읽는다.

`offset`/`limit`은 파일 전체 기준 1-indexed 시작 줄과 줄 수이므로 `Read` 인자로 그대로 쓴다. 섹션 범위는 `자기 heading ~ 다음 동급/상위 heading 직전`이라 `##` 절을 읽으면 하위 `###`가 함께 들어온다. `bytes`는 그 절을 읽는 비용이다.

- 문서 전체 목록·메타데이터(`bytes`·`section_count` 포함)는 `docs/wiki/_generated/manifest.json`.
- `docs/wiki/_generated/llms-full.md`(약 180KB)는 **열지 않는다.** canonical 전문을 이어붙인 외부 LLM ingest 전용 산출물이다.
- 키워드를 모를 때만 문서 전문을 읽고, 그때도 어떤 절이 필요했는지 확인한 뒤 다음부터는 인덱스를 쓴다.
- 섹션 인덱스의 `offset` 정확성은 `wiki.py check`의 `[section]` 검사가 hard-fail로 보장한다. 인덱스가 코드와 어긋나면 `build`를 다시 돌린다.

## 중복 방지 원칙

SOT 문서는 서로 링크하고, 같은 내용을 복사하지 않는다.

- 제품/고객/사업 전략은 `product.md`에 둔다.
- 첫 계약 GTM/PoC/데모/가격 결정은 `gtm/first-contract.md`에 둔다.
- 구현 격차·현황 감사 결과는 `reports/**`에 둔다(`sot/` 아님).
- 사용자/팀 성향은 `USER.md`에 둔다.
- 불변 기술 규칙과 작업 방식은 이 문서에 둔다.
- 코드 구조와 런타임 흐름은 `architecture.md`에 둔다.
- DB·설정 계약은 `data-contracts.md`에 둔다.
- API 세부는 `api-events.md`에 둔다.
- 프론트엔드 세부는 `frontend.md`에 둔다.
- 테스트 명령과 기준은 `testing.md`에 둔다.

중복이 필요해 보이면 원문을 복사하지 말고 해당 SOT로 링크한다.

## 자동 생성 vs 수기 관리 범위

- **자동 생성(직접 편집 금지):** `docs/LLM_WIKI.md`, `docs/wiki/_generated/**`(`manifest.json`·`llms.txt`·`llms-full.md`·`sot-drift-report.md`·`agent-context.md`·`section-index.tsv`).
- **수기 관리(정본):** `docs/wiki/sot/**`, `docs/wiki/gtm/**`, `docs/wiki/reports/**`, `docs/wiki/adr/index.md`.
- **수기 관리(projection):** `AGENTS.md` / `CLAUDE.md` / `README.md` — 소개 + canonical 포인터 + 불변 규칙 블록 + Commands 정도만 유지한다.
- Harness(`scripts/execute.py`)는 `docs/LLM_WIKI.md`를 중심으로 step 컨텍스트를 주입한다.
- 세 계층(raw 원본 → wiki SOT → 생성물)의 역할 구분과 변경 제출 흐름은 아래 `기여 흐름` 섹션이 정본이다.

## 기여 흐름 (raw → wiki → PR → review)

PureIron 문서는 **3계층**으로 나뉘며, 계층마다 역할과 편집 권한이 다르다.

- **raw source (보존 계층)** — `backend/**`, `frontend/**`, `configs/**`, DB 마이그레이션, `config.py`·`.env.example` 등 런타임/설정 파일. 시스템의 정본(system of record). LLM은 문서나 `check`를 통과시키려고 raw source를 임의로 고치지 않는다(drift는 경고일 뿐). legacy 루트 문서(`PureIron_ignition@legacy-final`의 `docs/PRD.md`·`docs/ADR.md`·`MANUAL.md`)는 위키가 요약하는 **source input**일 뿐 canonical SOT가 아니다(ADR 정본은 [adr](../adr/index.md)).
- **wiki SOT (통합 계층)** — `docs/wiki/**`(`_generated/` 제외). LLM이 raw source를 통합·요약·교차참조하는 곳이며 문서의 canonical.
- **generated projection (생성물)** — `docs/wiki/_generated/**`·`docs/LLM_WIKI.md`. `python scripts/wiki.py build` 출력 전용, 수기 편집 금지.

변경은 **raw → wiki → PR → review** 한 방향으로 흐른다.

- LLM은 raw source를 읽고 → wiki SOT 계층을 작성/수정 → `build` → `check` 한다(절차 정본은 위 `위키 규율` 섹션의 load→save→build→check).
- **default branch(`master`/`main`) 직접 커밋/푸시 금지** — 모든 변경은 전용 브랜치에서 한다.
- 위키/문서 변경은 LLM이 직접 머지하지 않고 **PR로 제안**한다.
- **머지 전 human review 필수** — LLM은 wiki 변경을 self-merge 하지 않는다.
- 문서 PR은 **documentation-only**: 앱 코드·CI·스케줄러 변경을 같이 싣지 않는다.

---

<!-- source: docs/wiki/sot/api-events.md -->
# API & Events SOT — 라우터·이벤트 계약

# API & Events SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다.

PureIron의 REST prefix, WebSocket wire protocol, Chat SSE event, 내부 event bus topic 계약이다.

정확한 request/response schema와 endpoint 전체 목록은 런타임 OpenAPI(`/docs`, `/redoc`)와 각 `router.py`가 정본이다. 이 문서는 Agent와 개발자가 drift를 빨리 잡기 위한 **경계/계약 요약**만 유지한다.

## 이 문서의 원천

- [backend/src/pureiron/main.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/main.py) — router 등록, 직접 `@app.*` route, `/metrics` mount
- [backend/src/pureiron/*/router.py](https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron) — 각 기능별 `APIRouter(prefix=...)`
- [backend/src/pureiron/ws/router.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/ws/router.py) — WebSocket `/ws`
- [backend/src/pureiron/chat/sse.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/chat/sse.py) — Chat SSE event 생성
- [backend/src/pureiron/events](https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/events) — in-process pub/sub

## 라우터 프리픽스

`main.py`가 등록하는 주요 HTTP/WebSocket prefix는 다음이다.

| Prefix | 모듈 | 책임 |
|---|---|---|
| `/api/auth` | `auth/router.py` | 로그인, 토큰 갱신, 내 정보, 사용자/프로필 관리 |
| `/api/tags` | `tags/router.py` | 태그 정의 CRUD, 값 write, search, bulk read, folder 작업 |
| `/api/alarms` | `alarms/router.py` | 활성 알람, 알람 config, ack/shelve, journal, summary |
| `/api/history` | `historian/router.py` | 태그 이력 조회, bulk history query |
| `/api/chat` | `chat/router.py` | 대화 CRUD, message SSE stream, pending action approve/reject |
| `/api/models` | `models/router.py` | 3D model metadata/storage |
| `/api/devices` | `devices/router.py` | device CRUD와 DriverHealth 기반 연결 상태 |
| `/api/logs` | `logs/router.py` | 애플리케이션 파일 로그 조회. `/{date}`는 마지막 N건 bare list(기존 계약 유지), `/{date}/page`는 실제 전체 건수와 안정적인 `before_id` snapshot + offset 페이징 |
| `/api/operational-events` | `operational/router.py` | DB-backed 주요 운영 오류/거부 이벤트 조회. category/severity/actor/source/target/text/time 필터, DB 미연결 시 `/api/logs/{date}` baseline 사용 |
| `/api/diagnostics` | `diagnostics/router.py` | Admin 전용 Compose 서비스 상태·redacted stdout/stderr 조회·text export. 고정 service allowlist와 read-only Docker socket proxy를 사용하며 임의 container id/name은 받지 않음 |
| `/api/mes` | `mes/router.py` | MES-lite recipe/plan/task 계층. 내부 경로·모델명은 유지하되 v1 고객 노출 라벨·플로우는 [mes-schedule](mes-schedule.md) 참조 |
| `/api/udt` | `udt/router.py` | UDT type/instance 관리와 derived tag sync |
| `/api/settings` | `settings/router.py` | 사용자/앱 설정 |
| `/api/dashboard` | `dashboard/router.py` | 사용자 dashboard 저장/조회 |
| `/api/docs` | `rag/router.py` | 문서 업로드, folder, tag link, RAG search |
| `/ws` | `ws/router.py` | 실시간 tag update WebSocket |
| `/metrics` | `main.py` mount | Prometheus metrics |

`main.py` 직접 route:

- `GET /api/health` — 앱 health, version, DB 연결, LangSmith 상태, `poc_read_only_mode` 안전 latch 상태
- `GET /api/debug_tags` — 태그 path debug용. 운영 API로 확장하지 않는다.

> `scripts/wiki.py check`는 소스에서 추출한 prefix와 이 문서의 인라인 코드 목록을 비교해 누락을 경고한다. 정규식 기반 heuristic이므로 상세 endpoint 정합성은 OpenAPI와 router source를 따른다.

## 주요 API 계약 요약

세부 schema는 OpenAPI와 router source가 정본이다. 이 표는 프론트/Agent가 자주 쓰는 흐름만 고정한다.

| 영역 | 주요 endpoint | 계약상 주의 |
|---|---|---|
| Auth | `POST /api/auth/login`, `POST /api/auth/refresh`, `GET /api/auth/me`, `POST /api/auth/change-password` | JWT access/refresh 기반. RBAC는 auth dependency가 담당. v1은 서버 session store/token blacklist를 두지 않으므로 비밀번호 변경은 이후 로그인에 즉시 적용되지만 이미 발급된 JWT는 설정된 만료 시간까지 유효하다. Settings UI는 이를 “active session policy”로 안내하고, 공유 단말에서는 비밀번호 변경 후 명시적 Sign Out을 요구한다. |
| Tags | `GET/POST /api/tags/`, `GET/PUT/DELETE /api/tags/{path:path}`, `PUT /api/tags/{path:path}/value` | tag path는 slash를 포함할 수 있으므로 URL encoding 필요. JSON fallback mode에서 definition CRUD는 503 가능, value write는 허용 |
| Tags bulk/search | `POST /api/tags/bulk-read`, `GET /api/tags/search`, `GET /api/tags/history-enabled` | UI 성능을 위해 개별 tag read loop보다 bulk/search 우선 |
| Alarms | `GET /api/alarms/active`, `GET /api/alarms/journal`, `GET /api/alarms/summary`, `POST /api/alarms/{name}/ack` | active state와 journal을 구분. journal은 `alarm_name`과 `tag_path`로 특정 alarm identity 필터링 가능. ack/shelve는 audit 대상. `ACTIVE_UNACKED` 발생 전이는 사용자 Settings notification preference와 alarm `notification_channels`를 해석해 Webhook v1 발송을 시도하며, quiet hours 억제와 성공/실패는 operational event로 남기고 알람 상태 전이를 막지 않는다. |
| History | `GET /api/history/{tag_path:path}`, `POST /api/history/bulk-query` | historian 미초기화 시 503. trend 화면은 bulk query 우선. 조회 기간 길이에 따라 자동 다운샘플링(RAW/1min/5min/1hour), `end<start`는 raw·`agg` 경로 모두 400/ValueError(DB 접근 전 guard). GET 응답 shape는 `agg` 없으면 `{time,value,quality}`(HistoryPoint), `agg` 지정 시 `{timestamp,value}`(집계 버킷). retention·window 계약은 [data-contracts](data-contracts.md) |
| Devices | `GET/POST/PUT/DELETE /api/devices` | 연결 상태는 tag가 아니라 `DriverHealth.snapshot()` 기반 merged response |
| Chat | `POST /api/chat/conversations`, `POST /api/chat/conversations/{id}/messages`, `POST /api/chat/conversations/{id}/approve` | message endpoint는 `text/event-stream`; message body는 `content`와 선택적 `context`(`process_type`, `scenario`, `source`, `action_id`)를 받을 수 있고, 시나리오 quick action routing에 사용한다. 위험 action은 pending→approve 흐름 유지 |
| MES tasks | `GET /api/mes/tasks?start&end&process` | range 필터는 **overlap 의미**: 창 `[start, end)` 와 겹치는 task 를 모두 반환(`end_datetime > start AND start_datetime < end`). 창 안에서 시작한 task 만이 아니므로 야간/교대처럼 날짜 경계를 넘는 task 도 포함된다. start/end 는 독립 적용, 잘못된 ISO 8601 은 422. 프론트는 저장 후 같은 overlap 규칙으로 현재 view 에 걸치는 task 만 로컬 반영 |
| Docs/RAG | `/api/docs/*` | vector store/embedding service 미초기화 시 503 가능 |

## WebSocket (`/ws`)

> legacy(`PureIron_ignition@legacy-final`) 계약이다. V1은 아래 [WebSocket `/api/v1/ws`](#websocket-apiv1ws-v1-m3b-2) 절(인증·tag_id 구독·알람/장비 방송).

목적: tag value live update. REST definition CRUD와 분리한다.

### 연결 직후

서버는 연결 직후 connection status 메시지를 보낸다.

```json
{"type":"connection_status","connected":true}
```

### 클라이언트 → 서버

```json
{"type":"subscribe","tags":["[Memory]Plant/Tank1/Level"]}
{"type":"unsubscribe","tags":["[Memory]Plant/Tank1/Level"]}
```

### 서버 → 클라이언트

태그 업데이트는 batch interval(`PUREIRON_WS_BATCH_INTERVAL_MS`, 기본 250ms)로 묶어서 전송한다.

```json
{"type":"tag_update","values":[{"path":"...","value":42.5,"quality":"Good","ts":"..."}]}
```

`values[]` 각 항목의 field set은 정확히 `{path, value, quality, ts}`로 고정된다. timestamp field 이름은 REST tag read 응답의 `timestamp`와 달리 **`ts`**이다(ISO8601, `Z` 접미사). 정본 모델은 [backend/src/pureiron/ws/protocol.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/ws/protocol.py)의 `TagUpdateValue`이며, wire 계약은 `backend/tests/test_websocket.py`의 field-set contract test가 고정한다. 외부 client/Agent는 `timestamp`가 아니라 `ts`를 파싱해야 한다.

프론트엔드에서는 `scadaWS` singleton과 `registry.applyTagBatch()` 경로를 사용한다. 세부 규칙은 [frontend](frontend.md)를 따른다.

## Chat SSE 이벤트

`POST /api/chat/conversations/{conv_id}/messages`는 `text/event-stream`을 반환한다.

SSE wire format은 `data: <SSEEvent JSON>\n\n`이다. 생성 로직은 `chat/sse.py`가 정본이다.

| type | 의미 |
|---|---|
| `token` | assistant 텍스트 token/chunk |
| `tool_call` | Agent tool 호출 시작 |
| `tool_result` | Agent tool 결과 |
| `approval_request` | 위험 action 승인 요청. 프론트는 승인 UI를 보여야 함 |
| `tag_reference` | 응답 중 언급된 tag path. 3D highlight 등에 사용 |
| `component` | UI component payload |
| `error` | stream 중 오류 |
| `done` | stream 종료 |

위험 action은 SSE에서 직접 실행하지 않는다. pending action 저장 후 `POST /api/chat/conversations/{conv_id}/approve`에서 RBAC 확인 후 실행한다.

## V1 core 운영 API (`/api/v1`, M3a 구현)

PureIron_V1 core의 정본은 `services/core/src/pureiron_core/api/` 와 생성된 OpenAPI(`packages/contracts/openapi/core.json`)다.

- `GET /api/v1/healthz` — 프로세스 생존(`status`, `version`).
- `GET /api/v1/readyz` — 준비 = site-db 연결 + 스키마 epoch envelope + collector 세션(아니면 503). 준비 판정에 쓰지 않는 정보 필드:
  - `acquisition`: `plan_rev`, 장비 수·연결 수, 태그 수·GOOD 수, `good_ratio`, `ingest{live, stale, future, replay}`(core 기동 후 누적 샘플 수 — live만 현재값에 반영, 나머지는 이력 전용)·`ingest{batches, heartbeats}`(받은 ValueBatch 수, 그중 빈 heartbeat 수 — 부하 측정의 batch/s).
  - `historian`: `buffer_rows`(쓰지 못한 버퍼 행, 쓰는 중 포함), `dropped_total`(상한 초과로 버린 행), `written_total`(INSERT에 넘긴 행, 중복 포함), `poisoned_total`(site-db가 행 내용 때문에 거부해 반분 격리 끝에 버린 행, M3b-0), `write_lag_s`(버퍼 최고령 행 대기 시간, 비면 null), `db_ok`(마지막 쓰기 성공), `rejected_total`(이력에서 버린 샘플 = batch `plan_rev`가 현재 rev보다 앞선 것 + 현재 정의에 없는 tag_id의 live 샘플(정의 기준 — plan에서 문제로 빠진 태그는 기록, [data-contracts](data-contracts.md) M3a-3 historian 현황) + int4 범위 밖 tag_id).
- replay batch는 이력에만 기록되고 `tag.changed.*`를 발행하지 않는다(알람·WS에도 가지 않는다). 이력 조회는 `GET /history`(M3b-2, 아래 절).
  - `alarms`(M3b-2): `active_total`(ACTIVE_UNACKED + ACTIVE_ACKED), `unacked_total`(ACTIVE_UNACKED + CLEARED_UNACKED), `journal_queue_rows`(DB에 아직 못 쓴 저널 행), `dropped_total`(저널 큐 상한 초과로 버린 행). `realtime`: `connections`(인증된 WS 연결 수).
  - **무인증 노출 정보**: `/readyz`는 인증 없이 열린다. 위 필드는 모두 집계 수(개수·비율·지연)뿐이고 태그 path·값, 알람 id·내용, 사용자 이름은 싣지 않는다 — 활성·미확인 알람 수의 변화 정도는 LAN에서 보인다는 것을 감수한 설계다(PR #26 리뷰 L-A3).

## V1 인증·감사 API (`/api/v1`, M3b-1 구현)

정본은 `services/core/src/pureiron_core/api/{auth,audit}.py`와 생성된 OpenAPI. 계정 생성·비밀번호 재설정·비활성화는 **CLI 전용**(`pureiron-core users add|passwd|disable|enable|list`)이고 REST에는 본인용 4개만 있다. 기본 계정은 자동 생성하지 않는다.

| 메서드·경로 | 권한 | 동작 |
|---|---|---|
| `POST /auth/login` | 없음 | `{username, password}` → `{token, expires_at, user}` + 세션 쿠키. 실패는 이유와 무관하게 401 `invalid username or password`(DB가 받지 못하는 NUL이 든 username도 조회 없이 같은 401) |
| `POST /auth/logout` | 없음 | 보낸 세션(쿠키·Bearer)을 즉시 폐기 + 쿠키 삭제, 항상 204(이미 끝난 세션이어도 — HttpOnly 쿠키는 브라우저 JS가 지울 수 없다). 감사는 유효한 세션을 끝냈을 때만 |
| `GET /auth/me` | 로그인 | `{user_id, username, role, display_name}` |
| `POST /auth/password` | 로그인 | `{current_password, new_password}` → 204. 현재 비밀번호 틀림 400, 정책 위반·같은 비밀번호 422. 이 세션만 남기고 나머지 세션 폐기 |
| `GET /audit` | engineer+ | `?username&action&start&end&limit` → `{entries}` 최신순. 시간 `[start, end)`(시간대 필수), `limit` 1~1000(기본 100). NUL이 든 `username`·`action`은 422 |

- **세션 토큰 전달**: 브라우저는 로그인 응답의 쿠키 `pureiron_session`(HttpOnly·SameSite=Strict·Path=/api·Max-Age=절대 만료, Secure는 `PUREIRON_AUTH_COOKIE_SECURE`)을 그대로 쓴다 — JS가 토큰을 읽지 않고 WS 업그레이드에도 실린다. 비브라우저(ai·스크립트)는 응답 본문 `token`을 `Authorization: Bearer`로 보낸다. 둘 다 오면 쿠키를 쓴다.
- **OpenAPI 보안 스키마 이름**: 사용자 세션 = `SessionCookie`(apiKey/cookie)·`SessionBearer`(http bearer), Updater 편집 잠금 = `HTTPBearer`(http bearer, description `Updater token` — M1부터의 이름. 바꾸면 oasdiff `api-security-removed` breaking). 둘은 같은 `Authorization` 헤더를 쓰지만 서로 다른 비밀이다.
- **역할**: `viewer < operator < engineer < admin`(수준 비교, 응답은 소문자 이름). 미인증·무효 세션 401(`WWW-Authenticate: Bearer`, detail은 이유와 무관하게 `not authenticated`), 역할 부족 403, site-db 장애 503(detail은 고정 `site-db unavailable` — DB 오류 원문은 core 로그에만, `GET /project`도 같다).
- **세션 무효 조건**: 로그아웃, 사용자 비활성화(CLI — 즉시, 다시 활성화해도 옛 세션은 폐기 상태), 관리자 비밀번호 재설정(그 사용자 전체), 유휴·절대 만료(`PUREIRON_AUTH_SESSION_*`).
- **감사 action**: `auth.login`·`auth.login_failed`(입력한 username과 `detail.reason` = `unknown_user|bad_password|disabled`, 비밀번호는 남기지 않음. username 규칙에 맞지 않는 입력 — 예: 비밀번호를 username 칸에 넣은 실수 — 은 username 없이 `detail.username_invalid = true`만)·`auth.logout`·`auth.password_change`(source `api`), `users.add|passwd|disable|enable`(source `cli`, 행위자 `username` = null, `target` = 계정), `project.edit_lock_acquire|release`(source `updater`, `target` = owner). 감사 기록 실패는 경고 로그만 남기고 요청은 성공한다.

## V1 알람·실시간·장비·이력 API (`/api/v1`, M3b-2 구현)

정본은 `services/core/src/pureiron_core/api/{alarms,tags,devices,history,ws}.py`, WS 메시지는 `realtime/protocol.py`(OpenAPI components `WsServerMessage`·`WsClientMessage`로 생성). 모든 조회는 viewer+.

| 메서드·경로 | 권한 | 동작 |
|---|---|---|
| `GET /tags` | viewer+ | `{tags:[{tag_id, path, device_id, value, quality, ts}]}` 현재값 스냅샷(tag_id 순). `quality` = `GOOD`·`BAD`·`UNCERTAIN`, NaN/Inf·값 없음은 `value: null`, 샘플이 없으면 `ts: null` |
| `GET /alarms` | viewer+ | `{alarms:[AlarmOut]}` NORMAL이 아닌 알람(활성 + 해제됐지만 미확인), 우선순위 → 최근 전이 순 |
| `GET /alarms/journal` | viewer+ | `?alarm_id&start&end&limit` → `{entries}` 최신순, `[start, end)`, `limit` 1~1000(기본 100) |
| `POST /alarms/{alarm_id}/ack` | operator+ | 전이 행 + 감사 `alarm.ack`를 한 트랜잭션으로 쓴 뒤 상태 반영 → `AlarmOut`. 모르는 알람 404, **NORMAL·ACTIVE_ACKED는 409**(다른 사용자가 먼저 ACK했거나 화면이 오래됨 — no-op 200이면 감사 없는 확인이 성공처럼 보인다), site-db 장애 503. 기록하는 사이 해제만 됐으면 CLEARED_UNACKED → NORMAL로 반영(그 행을 더 쓴다), **새로 발생했으면 반영하지 않고 지금 상태(ACTIVE_UNACKED)로 200** — 운전원이 보지 못한 발생이므로 다시 ACK한다 |
| `POST /alarms/ack-all` | operator+ | ACK할 수 있는 알람 전부를 한 트랜잭션(저널 행들 + 감사 `alarm.ack_all` 한 건, `detail{alarm_ids, count}`) → `{acknowledged:[AlarmOut]}`. 0건이면 기록 없이 200 |
| `GET /devices` | viewer+ | `{devices:[DeviceOut]}`: `device_id, protocol, state(CONNECTED·DISCONNECTED — core 판정), silent(core 무신호), tag_count` + collector `DeviceHealth` 필드(`connected, status, last_success_at, last_error, last_error_at, consecutive_failures, poll_latency_ms, read_success_rate, poll_total, timeout_count, bad_quality_tag_count, reconnect_count` — health를 아직 못 받았으면 null) |
| `GET /history` | viewer+ | `?tag_id=…(반복, 1~20개, 1~2^31-1)&start&end(시간대 필수)&resolution=auto\|raw\|1m\|1h&limit_rows(태그별, 1~20,000)` → `{resolution, series:[{tag_id, truncated, raw:[{time, value, quality}], buckets:[{time, avg, min, max, last, samples}]}]}`. auto = ≤1h raw · ≤24h 1m · 그 이상 1h. 구간 상한 raw 1h · 1m 7d · 1h 400d(넘으면 422). 1m·1h 첫 bucket은 start를 품은 `time_bucket` 경계부터 |

`AlarmOut`: `alarm_id, tag_path, alarm_type(above·below = 프로젝트 정의 type), setpoint, deadband, on_delay_s, priority, description, state, value(마지막 전이를 만든 값), value_ts(그 샘플 시각, 복원한 알람은 null), changed_at, changed_by(ACK한 사용자, 조건 변화는 null)`. 상태 기계는 [data-contracts](data-contracts.md) M3b-2 절.

### WebSocket `/api/v1/ws` (V1, M3b-2)

- **인증**: 업그레이드 요청의 세션 쿠키 `pureiron_session`, 쿠키가 없으면 연결 후 5s 안의 첫 메시지 `{"type":"auth","token"}`. 실패 close **4401**, site-db 장애로 검증 불가 **1013**. 60s마다 세션을 다시 검증해 로그아웃·만료·비활성화면 4401(검증 자체가 DB 장애로 안 되면 연결 유지).
- **Origin**: 업그레이드 요청에 `Origin`이 있고(브라우저) 그 host:port가 요청 `Host`와 다르면 **4403**(포트가 없으면 각자 scheme의 기본 포트). 같은 PC의 다른 포트 웹앱은 same-site라 Strict 쿠키가 실리므로(CSWSH) 막는다. `Origin`이 없는 비브라우저 클라이언트는 허용한다. 그래서 프록시는 브라우저의 Host를 그대로 넘겨야 한다: web nginx `proxy_set_header Host $http_host`(`$host`는 포트를 버린다), vite dev proxy는 `changeOrigin`을 켜지 않는다. 다른 이름으로 프록시하는 배치를 위한 허용 목록 env는 아직 없다.
- **자원 상한**: 프레임 256 KiB(`WS_MAX_MESSAGE_BYTES`, 가장 큰 정상 메시지 subscribe 10,000개 ≈ 110 KB — 넘으면 uvicorn이 1009), 인증 중인 연결 동시 32개(`MAX_PENDING_AUTH`, 넘으면 1013).
- **서버 → 클라이언트**(JSON 텍스트 프레임, `type`으로 구분): `hello{username, role}` · `tag_update{values:[TagValueOut]}`(구독한 태그만, `PUREIRON_REALTIME_BATCH_MS`(기본 250ms)마다 태그별 최신값만 합쳐서, 구독 직후 현재값은 곧바로 한 번) · `alarm_event{…AlarmOut}` · `device_update{…DeviceOut}`(알람·장비는 인증된 모든 연결에 방송, 깨어나는 즉시) · `ping`(20s마다).
- **클라이언트 → 서버**: `subscribe{tag_ids}`·`unsubscribe{tag_ids}`(연결당 최대 10,000개) · `pong`. 형식이 어긋난 메시지는 무시한다.
- **close code**: 1000 정상, 1001 core 정지, 1009 프레임 상한 초과, 1013 연결별 이벤트 큐(1,000) 초과·인증 시 site-db 장애·인증 대기 연결 상한 초과, 4401 인증 실패·세션 무효, 4403 Origin ≠ Host, 4408 pong이 60s 없음.
- legacy WS와 달리 무인증 연결·`connection_status`·태그 path 구독은 없다(구독은 tag_id).

## 내부 이벤트 버스

V1 core `platform/bus.py` `EventBus`는 core 프로세스 내부 async pub/sub이다(외부 broker 계약이 아니다). 구독은 fnmatch 패턴.

| 토픽 | 발행 | payload |
|---|---|---|
| `tag.changed.<path>` | acquisition(TagStore 결과, Ack 뒤) | `tag_id, path, device_id, value(NaN→null), quality(int), ts_ns` |
| `device.changed.<device_id>` | acquisition `DeviceMonitor` | `device_id, state, reason` |
| `collector.session.changed` | acquisition server | `sessions` |
| `alarm.changed.<alarm_id>` | alarms(M3b-2) | `alarm` = 전이 뒤 `AlarmSnapshot` |

- **`tag.changed.all`은 V1에 없다**(legacy 전용). 구독자는 `tag.changed.*`를 쓴다.
- **핸들러 규칙(M3b-2 D8)**: `publish`는 핸들러를 순서대로 await 하므로 핸들러는 I/O를 await 하지 않는다 — 메모리 갱신·큐 적재만. 새 이벤트(알람 전이)는 직접 발행하지 않고 **돌려주면** 버스가 지금 이벤트를 모든 구독자에게 전한 뒤 이어서 발행한다.
- definition 변경은 `tag.changed.*` 이벤트가 아니다. Device health는 tag event로 만들지 않는다(`/api/v1/devices`·WS `device_update`가 정본).

legacy(`backend/src/pureiron/events/bus.py`)의 `tag.changed.all`·HistorianWriter 구독은 V1에 적용하지 않는다.

데이터 흐름과 lock discipline은 [architecture](architecture.md#이벤트-버스--잠금-규율)를 따른다.

---

<!-- source: docs/wiki/sot/architecture.md -->
# Architecture SOT — 시스템 구조 (V1)

# Architecture SOT

> **PureIron_V1 기준** — 이 문서는 Foundation 재설계(ADR-019~028)로 만든 V1 구조와 현재 구현 상태의 정본이다.
> legacy(`PureIron_ignition`) 모놀리식 구조는 마지막 절 [legacy 참고](#legacy-참고-pureiron_ignition)에 요약만 두고,
> 전체 설명은 [legacy-final의 architecture.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/wiki/sot/architecture.md)를 본다.

PureIron V1은 현장 한 곳에 설치되는 **서비스 4개(collector·core·web·site-db) + 호스트 Updater**와, 벤더 쪽 **중앙
도구(control-api·fleet repo)**로 이루어진다. 현장은 중앙·AI 없이도 모니터링·알람·이력이 동작하고, 현장 변경은
서명된 번들(`.pib`)로만 들어온다.

제품 의도와 v1 범위는 [product](product.md), 불변 작업 규칙은 [agent-rules](agent-rules.md), 코드 작성 규약은
[coding-style](coding-style.md), 결정 근거는 [ADR](../adr/index.md), DB·마이그레이션·release 계약은
[data-contracts](data-contracts.md)를 따른다.

## 이 문서의 원천

- `services/core/src/pureiron_core/runtime.py` — core 유일한 조립 지점(`SiteRuntime.build`)
- `services/core/.importlinter` — core 모듈 경계 계약 (a)(b)(c)
- `packages/contracts/collector.proto` — collector ↔ core 계약
- `services/updater/src/pureiron_updater/phases.py` — 현장 적용 상태기계
- `deploy/compose/site.compose.yml`, `deploy/releases/*.json` — 현장 구성과 release 기록

## 아키텍처 경계

- PLC 로직을 수정하지 않는다. 기존 PLC 주소 쓰기는 ADR-013(개정) 가드레일 아래에서만 한다(기본 읽기 전용, 쓰기
  경로는 M3c).
- PureIron은 관제·알람·이력·트렌드·감사와 Agent 보조 계층을 제공한다. 사람 승인 없는 자동 조치는 v1 범위가 아니다.
- **graceful degradation**: `ai` 서비스와 중앙(control-api)은 선택이다. 없어도 현장은 동작한다. collector가
  죽으면 태그는 BAD/DISCONNECTED가 되고, site-db는 필수다. core `/readyz`는 DB 연결 + schema epoch envelope +
  collector 세션이 모두 있을 때만 ready다(수집·이력 상태는 정보로만 낸다).
- 현장 변경 경로는 서명 번들 하나뿐이다. 검증에 실패하면 서비스를 멈추기 전에 거부한다. registry pull은 하지 않는다.

## 시스템 구성 (ADR-019)

| 구성 | 역할 | 구현 상태 (2026-10-17) |
|---|---|---|
| `collector` | 장비 통신, `ValueBatch`·`DeviceHealth` 송신. 무상태·DB 접근 없음, 60s 메모리 링버퍼 | Modbus TCP 드라이버·링버퍼·replay·DeviceHealth (M3a) |
| `core` | 태그·이력·장비 상태·프로젝트 적용 + 알람·인증·감사·실시간(M3b), REST `/api/v1`, gRPC 서버 | acquisition·tags·historian·project (M3a), 알람·인증·감사·WS·`/api/v1/devices` (M3b) |
| `web` | nginx + React(로그인 + 태그 현황·알람/ACK·트렌드·장비 상태·감사 로그) | 로그인 + 5화면, WS 실시간, Playwright 스모크 (M3b) |
| `site-db` | TimescaleDB PG16(`pureiron/site-db:2.30.2-pg16`) | 마이그레이션 `0001~0012`, epoch 4 (r3) |
| `ai` | core read-only API 소비 | 계약만, 첫 완료 범위 제외 |
| Updater | 호스트 systemd 서비스(컨테이너 아님): 번들 검증·백업·마이그레이션·전환·롤백 | 완료(M2), VM 실측 30/30(r0→r1, r1→r2, r2→r3) |
| control-api | 벤더 내부 중앙 API: 등록·heartbeat·desired·reports·drafts·fleet | `GET /api/v1/healthz` 골격만(M4) |
| control-web | 중앙 읽기 전용 표(basic auth) | 미착수(M4) |
| `simulator` | 개발용 Modbus 시뮬레이터(compose profile `dev`) | `deploy/simulator/` |

포트: 호스트에 노출되는 것은 web `${PUREIRON_WEB_PORT:-80}` → 컨테이너 `8080`뿐이고, nginx가 `/api/`를 `core:8000`으로
프록시한다. core HTTP `:8000`과 gRPC `:50051`(collector가 `PUREIRON_CORE_GRPC=core:50051`로 접속)은 compose 내부 포트다.

저장소 레이아웃(ADR-026): `services/{core,collector,web,updater,control-api,ai}` · `packages/{contracts,bundle-tools,project-tools}`(계약·공용 도구) · `deploy/`(compose·install·build·bundle·release 기록) · `tests/e2e/`(Hyper-V VM 실측). 고객 프로젝트는 고객별 repo에 둔다.

## 통신 경계 (ADR-020)

| 채널 | 경로 | 용도 | 상태 |
|---|---|---|---|
| gRPC 양방향 스트림 | `Acquisition.Session`(core가 서버) | collector → `Hello`·`ValueBatch`·`DeviceHealth`, core → `AcquisitionPlan`·`Ack` | M3a |
| REST | `/api/v1/*` | `healthz`·`readyz`, `project`(edit lock) → M3b에서 history·alarms·auth·audit·devices | 일부 |
| WebSocket | `/api/v1/ws` | 태그 값·알람·장비 상태 푸시(PD-10, 메시지 계약은 M3b에서 확정) | M3b |
| 현장 → 중앙 | HTTPS polling(현장이 먼저 연결) | desired 조회·보고·드래프트 업로드 | M4 |

- **Ack 의미론**: core는 batch를 in-memory ingest(현재값 반영 + historian 버퍼 적재) 직후 Ack 한다. collector 링버퍼는
  core 다운·단절 구간만, DB 장애는 core historian 상한 버퍼(600,000행 + 쓰는 중 최대 20,000행)가 담당한다.
- **replay**: 재연결 뒤 링버퍼에서 다시 보낸 `replay=true` batch는 이력에만 기록한다(현재값·알람·WS 갱신 없음, deadband 우회).
  중복은 `UNIQUE(tag_id, time)`로 흡수한다.
- **단절·무신호**: collector 세션이 모두 끊기면 전 장비 DISCONNECTED·전 태그 BAD. 세션이 있어도 장비별
  `max(3×poll_ms, 5s)` 동안 신호가 없으면 그 장비 태그는 BAD, 장비는 DISCONNECTED.
- 프로토콜 추가는 proto `TagPoint.address` oneof 확장으로만 한다(호환성 diff 게이트, ADR-028).

## core 구조

```
cli.py ──▶ runtime.SiteRuntime.build(settings) ──▶ app.create_app(rt)
              │ 생성자 주입
              ├─ platform: Database · EventBus · migrate(Catalog, SchemaGuard)
              ├─ tags: TagStore
              ├─ acquisition: AcquisitionServer(gRPC)
              │     ├─ AcquisitionService ─ Ingestor · DeviceMonitor
              │     └─ PlanKeeper ─ PgDefinitionsSource(정의 → plan)
              ├─ historian: Historian ─ Recorder ─ HistoryWriter ─ PgRowStore
              └─ project: PgProjectStateStore(project_state·edit lock)
```

- 전역 싱글톤·`app.state`·service locator가 없다. 모든 객체는 `SiteRuntime.build`에서 만들어 생성자로 넘긴다.
- import 경계(import-linter): `api > domain > platform` 계층, 도메인 간은 `<module>.ports`만, `runtime`·`app`은 진입점만
  import한다. 모듈 내부 구성·추상화 기준은 [coding-style](coding-style.md) 3·4절.
- CLI `pureiron-core`: `serve`(HTTP + gRPC), `migrate status|up|verify|lint`, `project apply|export|restore`(M5 본구현),
  `apply-definitions <project dir>`(태그·장비 정의 upsert).

| 모듈 | 책임 | 상태 |
|---|---|---|
| `platform` | asyncpg `Database`(획득·반환 제한), 내부 `EventBus`, 마이그레이션 catalog·lint·`SchemaGuard` | M1~M3a |
| `acquisition` | collector 세션, plan 배포·갱신, batch ingest, 장비 무신호 판정 | M3a |
| `tags` | 현재값 `TagStore`(in-memory), 품질 | M3a |
| `historian` | recorder(deadband·max_interval, replay 우회) → writer(상한 버퍼·1s flush) → `tag_history`, reader(raw/1m/1h) | M3a(HTTP 노출은 M3b) |
| `project` | `project_state`·edit lock, 정의 소스(`definitions_rev`) | 골격 + 정의(M3a), 정식 apply는 M5 |
| `api` | `build_<x>_router` + pydantic 모델 | health·project |
| `alarms` | 4상태 알람 엔진(`tag.changed.*` 동기 평가)·저널(상한 큐 + writer)·ACK 트랜잭션·기동 복원 | M3b-2 |
| `auth`·`audit` | 세션 인증·역할, 감사 기록·조회 | M3b-1 |
| `realtime` | WS `/api/v1/ws` 연결·구독·태그 합치기·알람/장비 방송 | M3b-2 |
| 장비 상태 | 새 도메인 없이 `acquisition.ports` `DeviceSnapshot`·`DeviceStatusSource`(`DeviceMonitor`) → `GET /devices` | M3b-2 |

## Core Domain Model Boundary (SoT)

각 도메인의 단일 진실 소스(SoT)는 하나다. 이 경계 문장은 silent drift를 막는 불변이다.

| 도메인 | 단일 진실 소스 | 경계 문장(불변) |
|---|---|---|
| Tag | 정의 = 고객 project repo → site-db `tags.definition`(`apply-definitions`), 값 = core `TagStore` in-memory | **Tag definition != runtime value** |
| Device | 연결·건강 = collector `DeviceHealth` → core `DeviceMonitor` → `/api/v1/devices`(M3b) | **DeviceHealth is not a tag** |
| Alarm (M3b) | 현재 상태 = core 알람 엔진, 이력·감사 = 알람 저널 | **AlarmJournal is audit/history not current state** |
| Historian | `tag_history` hypertable + CAGG `tag_history_1m`·`_1h`(epoch 3부터 실시간 집계) | **Historian is append/query path** — 현재값 소스가 아니다 |
| Project | 고객 project repo(Git), 현장은 적용본 + 드래프트(단조 `site_rev`, ADR-025) | 고객별 코드 분기 없음, 비밀값은 `secret://`만 |
| Agent Context | core read-only API projection(`ai` 서비스) | **Agent is read-only/context projection** |
| Work Order / MES-lite | 미래 business workflow 경계(backend 기능 아님) | tag/alarm의 hidden mutation이 아니다 |

## 데이터 흐름: 수집 → 현재값·이력

```text
PLC ─Modbus TCP─▶ collector poller ─▶ ValueBatch(100ms 또는 500값) ─▶ 링버퍼(Ack 전 보관)
   ─gRPC Session─▶ core AcquisitionService
        ① Ingestor
           ├─ 이력: Recorder(fresh live는 deadband·max_interval, replay·오래된/미래 live는 원시 기록) ─▶ HistoryWriter 버퍼
           └─ 현재값: fresh live만 TagStore.commit() + DeviceMonitor.seen() (동기, 이벤트 목록 반환)
        ② Ack(seq) ─▶ collector 링버퍼에서 제거          ← 메모리 반영 직후, 발행 전
        ③ 락 밖에서 EventBus.publish(tag.changed.<path>, device.changed.<id>) ─▶ (M3b) 알람·WS
HistoryWriter ─1s flush, INSERT … ON CONFLICT DO NOTHING─▶ site-db tag_history ─▶ CAGG 1m/1h
```

- Recorder는 일부 샘플을 거부한다(plan_rev가 앞선 batch, 현재 정의에 없는 tag_id의 live 샘플, int4 범위 밖 tag_id → `rejected_total`).
  상세 규칙은 [data-contracts](data-contracts.md)의 M3a 절.
- 이력 기록은 `tag.changed` 구독이 아니라 Ingestor가 직접 넘기는 sink다(이벤트 루프의 reader 경로에 DB I/O가 없다).
  M3b-2 결정(D8): 별도 publisher task를 두지 않는다. 대신 **bus 핸들러는 I/O를 await 하지 않는다** — 알람 엔진은
  동기 평가 후 저널 행을 상한 큐에 넣고(writer task가 비움) 전이 이벤트를 돌려주며, WS는 연결별 메모리(태그 합치기·
  상한 이벤트 큐)에만 넣는다. 그래서 느린 DB·클라이언트가 ingest 루프를 붙잡지 않는다.

## 이벤트 버스 · 잠금 규율

core 내부 `platform/bus.py` `EventBus`(ADR-010 유지 — core 프로세스 내부 범위)만 있다. 서비스 간 pub/sub은 없다.
legacy와 달리 V1에는 `tag.changed.all` 토픽이 없고, historian은 이벤트를 구독하지 않는다(위 데이터 흐름의 sink).

| 토픽 | 발행 | 내용 |
|---|---|---|
| `tag.changed.<path>` | acquisition(TagStore 결과) | 값·품질이 바뀐 태그 |
| `device.changed.<device_id>` | acquisition `DeviceMonitor` | 장비 연결 상태 변화 |
| `collector.session.changed` | acquisition server | collector 세션 수 |
| `alarm.changed.<alarm_id>` | alarms(M3b-2, `tag.changed` 핸들러가 돌려준 전이·타이머·정의 변경·ACK) | 전이 뒤 `AlarmSnapshot` |

- 구독은 fnmatch 패턴(`tag.changed.*`). 핸들러 예외는 기록만 하고 다른 구독자 전달을 막지 않는다.
- **핸들러 규칙(M3b-2 D8)**: `publish`는 핸들러를 순서대로 await 한다(발행자 = ingest 루프·Ack 뒤). 핸들러는 I/O를
  await 하지 않고 메모리 갱신·큐 적재만 한다. 핸들러가 내야 할 이벤트(알람 전이)는 **돌려주면** 버스가 지금
  이벤트를 모든 구독자에게 전한 뒤 이어서 발행한다(너비 우선, 순서 결정적).
- 기동 순서: db → historian → alarms(저널 복원·writer) → realtime → acquisition, 정지는 역순(수집 중단 → WS
  1001 close → 알람 저널 최종 쓰기 → 이력 최종 쓰기 → db).
- **잠금 규율**: TagStore를 바꾸는 메서드는 동기로 끝나고 이벤트 목록을 반환한다. DB I/O와 `publish`는 그 뒤, 바깥에서
  한다(불변 규칙 Lock discipline, [coding-style](coding-style.md) 8절).

## 지원 디바이스 프로토콜 (v1)

| 프로토콜 | V1 구현 | 비고 |
|---|---|---|
| Modbus TCP | collector `drivers/modbus.py` (M3a) | 1차 범위 |
| OPC-UA → Siemens S7 → LS Electric → Allen-Bradley → Mitsubishi MELSEC | 미이식 | legacy-final에는 6개 모두 있다. 이 순서로 collector `drivers/`에 직접 구현하고 proto oneof를 확장한다(foundation plan §9, 마일스톤 미정) |

- 프로토콜은 동적 로딩·plugin registry 없이 `drivers/`에 직접 구현한다(No plugin system).
- 장비 연결 상태는 태그가 아니라 `DeviceHealth`로만 전달한다.
- 영업 자료의 "6개 프로토콜 지원"은 legacy 기준이다. V1에서 이식이 끝나기 전까지 제안·계약 문구는 [product](product.md)와 함께 확인한다.

## DB · 마이그레이션 (ADR-021, ADR-024)

- asyncpg raw SQL만(No ORM). 스키마 변경은 `services/core/migrations/NNNN_*.sql` + `pureiron-core migrate`로만, **ADD-only**,
  부팅 시 자동 실행하지 않는다(Updater `migrating` 단계 또는 운영자가 실행).
- 마이그레이션을 포함한 release만 schema epoch를 +1 한다. 앱은 `app_expected ≤ 현재 epoch ≤ app_max_compatible`일 때만
  ready다 → 한 단계 이전 앱으로 롤백할 수 있다.
- 현재: r0 = epoch 1, r1 = epoch 2, r2 = epoch 3(`0009`), **r3 = epoch 4**(`0010~0012`). 다음 migration은 `0013~`, epoch 5.
  세부(테이블·CAGG·lint 규칙·release 기록)는 [data-contracts](data-contracts.md).

## 배포 · 중앙 (ADR-022, ADR-023)

- 배포 정본: 벤더 repo `pureiron-fleet`의 `sites/<site>.yaml`(generation 카운터). PR 승인·merge = 배포 승인 → 서명 번들
  `.pib` 게시(M4).
- 현장 Updater 상태기계: `fetched` → `verified` →(적용 창)→ `committed` → `locked` → `backed_up` → `migrating` →
  `switching` → `checking` → `succeeded`, 실패 시 `rolling_back` → `failed`(수동 개입 필요 시 `failed_manual`).
  서명·검증 실패는 커밋 전에 `rejected`로 끝난다(서비스 영향 없음, generation을 기록하지 않음).
  각 단계는 멱등이라 crash·전원 리셋 뒤 다시 실행해 수렴한다.
- 폐쇄망 현장도 같은 `.pib`를 USB 등으로 반입해 같은 검증을 거친다. 설치는 install 번들(`deploy/install/`)로 오프라인 부트스트랩.
- 이미지 신원 = config digest(release 기록 `deploy/releases/<r>.json`). 현장 이미지는 번들에서 `docker load`만 한다.

## V1 구현 현황 (2026-10-17)

| 마일스톤 | 내용 | 상태 |
|---|---|---|
| M1 | 저장소 골격, core 조립·import 계약, G1 게이트(CI), compose | 완료 |
| M2 | Updater·bundle-tools·install 번들, r0·r1, Hyper-V VM 실측 30/30 | 완료 |
| M3a | collector(Modbus·링버퍼·replay), core tags·historian·정의, r2(epoch 3), VM 실측 r1→r2 30/30 | 완료 |
| M3b | alarms·auth·audit·realtime·devices·history API, web 로그인 + 5화면, r3(epoch 4), VM 실측 r2→r3 30/30 | 완료 |
| M3c | PLC 쓰기 경로(ADR-013 가드레일) | 예정 |
| M4 | control-api·control-web·fleet·release 파이프라인 | 예정 |
| M5 | 프로젝트 수명주기(project-tools 완성, apply·드래프트 회수) | 예정 |

진행 기록의 정본은 각 PR과 [data-contracts](data-contracts.md)의 release·실측 절이다.

## 프론트엔드 경계

- `services/web`: React + Vite + TypeScript(strict), API 타입은 core OpenAPI에서 생성한 `src/api/schema.d.ts`만 쓴다.
- 실시간은 `/api/v1/ws`(M3b). 상태 관리·UI 라이브러리는 필요가 생긴 PR에서 결정한다.
- 작성 규약은 [coding-style](coding-style.md) 10절. legacy 화면 구조(Jotai·3D Twin·Agent SSE)는 [frontend](frontend.md)(legacy 기준).

## legacy 참고 (PureIron_ignition)

M3b 이후 이식 작업에서 legacy 의미를 확인할 때만 쓴다. 새 코드에는 적용하지 않는다. 코드는
`git -C C:/PureIron_ignition show legacy-final:<path>`로 읽는다.

- legacy는 단일 FastAPI 백엔드 + 단일 React 프론트엔드 + PostgreSQL(TimescaleDB + pgvector) + Redis 모놀리스였다. 모든 런타임
  상태를 `ScadaGateway` 싱글톤(`backend/src/pureiron/gateway.py`)이 가졌고, 마이그레이션은 부팅 시 자동 실행했다. V1은
  이 둘을 각각 `SiteRuntime` 생성자 주입과 ADD-only `migrate` 명령으로 바꿨다.
- 주요 모듈: `tags/`·`drivers/`(6개 프로토콜)·`alarms/`·`historian/`·`events/`·`ws/`·`auth/`·`chat/`·`agents/`·`rag/`·`models/`(3D)·
  `udt/`·`mes/`. V1 이식 대상과 순서는 foundation plan §9.

### TagSourceKind Taxonomy

legacy `TagDefinition.source_kind`는 DB row 소유자를 구분했다: `AUTHORED`(사용자 작성) · `UDT_DERIVED`(UDT 인스턴스 확장) ·
`DRIVER_DERIVED`(장비 자동 생성). V1의 태그 정의는 고객 project repo가 정본이고 `apply-definitions`가 path 기준으로
upsert한다(UDT는 이식 순서 2번). 상세는 [legacy-final architecture.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/wiki/sot/architecture.md#tagsourcekind-taxonomy).

### 온톨로지 투영

legacy `ontology/`는 저장소가 아니라 기존 정본(TagStore·UDT·AlarmEngine·DriverHealth)을 읽는 TTL 캐시 투영이었다(엔티티 6종,
관계 5종 고정, 근거 없는 관계는 만들지 않음). V1에서는 `ai` 서비스 이식 때 core read-only API 위에서 다시 정한다.

---

<!-- source: docs/wiki/sot/coding-style.md -->
# Coding Style SOT — V1 코드 구조·추상화·이름·주석·테스트 규약

# Coding Style SOT

이 문서는 PureIron_V1 새 코드(`services/`·`packages/`·`deploy/`·`tests/e2e/`)를 **어떻게 쓰는가**의 정본이다.
1~9절은 M1~M3a 코드에 이미 반복되는 패턴을 글로 옮긴 것이고(어긋나는 기존 코드는 12절 백로그), 10절
프론트엔드는 코드가 아직 작아 **처방**이다. 무엇을 만드는가(설계 불변 규칙)는 [agent-rules](agent-rules.md)와
[ADR](../adr/index.md)가 정본이다. legacy 기준인 [frontend](frontend.md)·[testing](testing.md)의 코드 규칙과
이 문서가 다르면 V1 코드에는 이 문서가 우선한다.

## 1. 우선순위

판단이 갈리면 이 순서로 고른다.

1. **정확성** — 게이트(2절)와 테스트가 먼저다.
2. **읽는 사람이 한 번에 이해하는가** — 2인 팀이 몇 달 뒤 다시 읽는다. 영리한 코드보다 뻔한 코드가 낫다.
3. **가장 짧은 올바른 코드** — 지금 필요 없는 추상화·옵션·확장점은 만들지 않는다(4절). 줄 수를 줄이려고
   읽기 어렵게 만드는 것은 짧은 것이 아니다.
4. **주변 코드와 같은 모양** — 같은 일을 하는 코드가 이미 있으면 그 이름·구조·관용구를 따른다.

## 2. 기계 게이트 (어기면 CI 실패)

| 대상 | 게이트 | 설정 위치 |
|---|---|---|
| Python 문법·스타일 | ruff `E F W I B UP SIM RUF N ASYNC PT C4 PIE RET PL`, 줄 100자 | 루트 `pyproject.toml` `[tool.ruff]` |
| Python 타입 | pyright strict | 서비스·패키지별 `pyproject.toml` `[tool.pyright]`; `deploy/`·`packages/contracts/`·`tests/e2e/`는 `pyrightconfig.json` |
| core 모듈 경계 | import-linter 계약 (a)(b)(c) — 3절 | `services/core/.importlinter` |
| TypeScript | tsc `strict` + `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes` 등 | `services/web/tsconfig.json` |
| TypeScript lint | typescript-eslint `strictTypeChecked` + `stylisticTypeChecked` + react-hooks | `services/web/eslint.config.js` |
| 억제·우회 | 0건. 정본 패턴은 `scripts/g1.sh` no-suppressions job(`type: ignore`, `noqa`, `pyright: ignore/basic/standard/reportGeneralTypeIssues`, `fmt: off/skip`, `pragma: no cover`, `eslint-disable`, `as any`, `@ts-ignore`·`@ts-expect-error`·`@ts-nocheck`, `app.state`) | `scripts/g1.sh` |
| 생성 코드 | `_gen/`·`schema.d.ts`는 lint 제외, 대신 drift 0 | ADR-028 |

- ruff는 `PLR0913`(인자 수)·`PLR2004`(매직 넘버)만 전역으로 끈다. 인자가 많으면 선택 인자를 키워드 전용(`*,`)
  으로 둔다(6절). 매직 넘버는 끈 대신 상수로 이름을 붙이는 것을 기본으로 한다(5절).
- 규칙 예외는 줄 단위 억제가 아니라 `pyproject.toml`에서 **범위를 좁혀** 둔다: 파일 단위 `per-file-ignores`
  (예: pymodbus 콜백 시그니처) 또는 이름 단위 `pep8-naming.extend-ignore-names`(예: proto rpc 이름을 따르는
  gRPC servicer 메서드 `Session`). 새 예외에는 이유 주석을 붙이고 리뷰에서 확인받는다.
- 게이트가 잡는 것은 리뷰에서 다시 지적하지 않는다. 리뷰는 3~10절처럼 게이트가 못 잡는 것을 본다.

## 3. core 모듈 구조

```
services/core/src/pureiron_core/
  runtime.py      유일한 조립 지점: SiteRuntime.build(settings) — 모든 객체를 만들고 생성자로 주입
  app.py          create_app(rt): HTTP 노출만. 라우터 팩토리에 rt의 부품을 넘긴다
  cli.py          진입점(pureiron-core). runtime·app을 import하는 곳
  cli_<명령>.py    진입점 하위 명령(예: cli_users.py). cli.py가 등록만 한다 — runtime·app은 import하지 않는다
  api/            HTTP 계층: build_<x>_router(deps) + pydantic 응답 모델
  <domain>/       acquisition · historian · project · tags (M3b: alarms · auth · audit · realtime · devices)
    ports.py      다른 모듈이 기대도 되는 유일한 표면: 상태 dataclass · Protocol · 공개 예외
    service.py    수명주기(start/stop)와 status 집계 — 필요할 때만
    <역할>.py      구현: writer · recorder · reader · store · ingest …
  platform/       도메인 무관 기반: db · bus · migrate · sqltext
  settings.py     환경변수 → frozen dataclass
```

- 의존 방향은 `api > domain > platform`(계약 (b)). 도메인끼리는 **상대 모듈의 `ports`만** import한다(계약 (c)).
  `runtime`·`app`을 import하는 것은 진입점뿐이다(`app`이 `runtime`을 쓰는 것은 제외) — 도메인·api·platform은 import하지 못한다(계약 (a)).
  **새 도메인 모듈을 만들면 `.importlinter`의 (a)(b)(c) 세 곳에 모두 추가**한다 — 빠지면
  `tests/test_importlinter_negative.py`가 실패한다.
- 값 객체와 모듈 경계를 넘는 상태 스냅샷은 `@dataclass(frozen=True, slots=True)`다. 한 소유자 안의 누적 통계·
  세션 상태는 `@dataclass(slots=True)` 가변을 허용하고(예: `SiteRuntime`, `RecorderStats`, collector
  `DeviceHealth`), 밖으로 낼 때는 frozen 스냅샷으로 복사한다(예: `Historian.status()` → `HistorianStatus`).
- HTTP 경계 모델만 pydantic `BaseModel`이다. 도메인 상태를 API 모델로 옮기는 변환은 `api/`에서 한다
  (예: `HistorianStatus` → `api/health.py` `Historian`).
  예외 하나: WS 메시지 모델은 `realtime/protocol.py`가 소유하고, 같은 모양의 REST 응답 모델(`AlarmOut`·
  `DeviceOut`·`TagValueOut`)은 이를 재사용한다 — WS와 REST가 같은 스키마여야 하기 때문이다(M3b D10 계약 일치).
- 라우터는 전역을 읽지 않고 `build_<x>_router(...)`가 받은 의존성(Protocol 타입)만 쓴다. `app.state`·
  모듈 전역 싱글톤·service locator 금지.
- 다른 서비스(collector·updater·control-api)와 `packages/*`도 같은 원칙이다: 진입점(`main.py`·`cli.py`)에서
  조립하고, 나머지는 주입받는다.

## 4. 추상화 기준

**Protocol(인터페이스)을 만드는 경우는 넷이다.**

1. 다른 도메인 모듈이 기대는 표면 → `ports.py` (예: `HistorianStatusSource`).
2. I/O 경계를 테스트에서 가짜로 바꿔 끼워야 할 때 (예: `RowStore` ← 실제 `PgRowStore`, 테스트 `FakeStore`).
3. 실제 구현이 이미 둘 이상일 때.
4. 타입이 없는 외부 라이브러리 표면을 strict 타입으로 좁힐 때 — 모듈 내부 `_` Protocol
   (예: `platform/migrate.py` `_YamlLoader`, `pureiron_bundle/manifest.py` `_Validator`).

그 밖에는 구체 클래스나 함수로 쓴다. "나중에 바뀔지 몰라서"는 이유가 되지 않는다.

| 쓴다 | 쓰지 않는다 (이유) |
|---|---|
| 생성자 주입 + `typing.Protocol`(구조적 타입) | ABC 상속 트리 — 구현이 하나면 간접층만 늘어난다 |
| 값 객체는 frozen dataclass, 가변 상태는 소유자 하나(3절) | 여러 객체가 같이 고치는 가변 공유 상태 |
| `Enum`/`StrEnum`/`IntEnum`으로 상태·품질 표현 | 문자열·정수 리터럴 비교 |
| 작은 함수 + 명시적 분기 | 레지스트리·플러그인·동적 import — 불변 규칙 "No plugin system" |
| 예외 계층(모듈 기준 1단계) | 제네릭 Repository·추상 Factory·메타클래스 |
| 표준 라이브러리 → 이미 쓰는 의존성 → 새 의존성 순 | 한두 함수 때문에 새 패키지 추가 |

- 상속은 예외 계층(`MigrationError` ← `LockTimeoutError` …)과 Protocol 합성(`AcquisitionStatus(CollectorSessionStatus, Protocol)`)에만 쓴다.
- 래퍼·헬퍼는 두 번째 사용처가 생길 때 추출한다. 한 곳에서만 쓰는 헬퍼는 그 자리에 둔다(작은 `_` 함수 허용).

## 5. 이름

| 대상 | 규칙 | 예 |
|---|---|---|
| 모듈 | 역할 명사, snake_case | `writer.py`, `recorder.py`, `store.py`, `ports.py` |
| 클래스 | 역할 명사 | `HistoryWriter`, `Recorder`, `TagStore`, `SiteRuntime` |
| asyncpg 구현 | 같은 Protocol의 DB 구현이면 `Pg` 접두 | `PgRowStore`, `PgProjectStateStore`, `PgDefinitionsSource` |
| Protocol | 제공하는 것 기준. 구체 클래스와 이름이 겹치면 `…Port` | `RowStore`, `HistorianStatusSource`, `DatabaseProbe`, `TagStorePort` |
| 예외 | `…Error`, 모듈 기반 클래스에서 파생 | `MigrationError`, `EditLockConflictError` |
| 상수 | `UPPER_SNAKE`, 기본값은 `DEFAULT_` 접두 | `DEFAULT_MAX_ROWS`, `QUERY_TIMEOUT_S` |
| SQL 문자열 | 모듈 상단 `<동작>_SQL` 상수 | `INSERT_SQL` |
| 시간 값 | 필드·인자·상수에 단위 접미사 `_s` `_ms` `_ns` `_us` | `write_lag_s`, `ts_ns`, `POLL_MS` |
| 크기 값 | 필드·인자·상수에 단위 접미사 `_bytes`(개수면 `_rows` 등) | `size_bytes`(bundle-metrics), `flush_max_rows` |
| 라우터 팩토리 | `build_<x>_router` | `build_health_router` |
| 모듈 내부 | `_` 접두 | `_schema_check`, `_int` |
| 환경변수 | 사이트 공용 값은 `PUREIRON_<이름>`, 새 서비스 전용 튜닝 값은 `PUREIRON_<서비스>_<이름>`, 비밀값은 `…_FILE` | `PUREIRON_SITE_ID`, `PUREIRON_COLLECTOR_HEARTBEAT_S`, `PUREIRON_DB_PASSWORD_FILE` |

- 단위 접미사의 예외: 함수 안 짧은 지역 변수(`delay`, `deadline`), 외부 API 시그니처를 따르는 이름
  (`subprocess`·`asyncio`의 `timeout`), `timedelta`처럼 타입이 단위를 담는 값(`RAW_MAX_SPAN`). `Pg` 접두의 예외: Protocol 짝이 없는 기반 클래스(`Database`, `SchemaGuard`).
- 축약은 업계 표준(`db`, `ts`, `rev`, `gen`, `dsn`)만 쓴다. 한 글자 이름은 컴프리헨션·짧은 람다 안에서만.
- bool은 상태를 말하는 이름(`db_ok`, `connected`)으로 쓴다. 상태·API로 노출하는 누적값은 `_total` 접미사
  (`dropped_total`, `devices_total`)를 쓴다. 모듈 내부 통계 필드(`RecorderStats.rejected_rev`)와 이미 정해진 계약 필드
  (`collector.proto` `timeout_count`)는 예외 — 계약 필드 이름은 바꾸지 않는다.

## 6. 함수와 파일 크기

- 함수는 한 가지 일을 한다. 분기·문장 수 한도는 ruff `PL`이 강제한다. 리뷰 기준은 **화면 한 장(약 50줄)**
  을 넘으면 나눌 수 있는지 본다.
- 파일은 **약 300줄**을 넘으면 역할별 분리를 검토한다. 줄 수 자체보다 "이 파일의 역할을 한 문장으로 말할 수
  있는가"가 기준이다 — 모듈 docstring 첫 줄이 그 문장이다. 300줄을 넘는 기존 파일(예: updater `phases.py`,
  e2e `run.py`)은 그 파일을 크게 고칠 때 분리를 검토한다.
- 선택 인자는 키워드 전용으로 둔다: `def __init__(self, recorder, writer, *, stop_timeout_s=DEFAULT_STOP_TIMEOUT_S)`.
- 시간·난수처럼 테스트에서 통제해야 하는 것은 인자로 주입한다: `clock: Callable[[], float] = time.monotonic`.
- `__init__.py`를 뺀 새 Python 파일은 `from __future__ import annotations`로 시작한다(빠진 기존 파일은 손댈 때
  맞춘다). 타입은 `X | None`, `type Alias = …`(3.12 문법)을 쓴다.

## 7. 주석·docstring·메시지 언어

| 무엇 | 언어 | 내용 |
|---|---|---|
| 모듈 docstring | 한국어 | 첫 줄 = 이 모듈의 역할(근거 문서 id — plan·ADR·PD — 가 있으면 함께). 다음 줄부터 계약(불변식·단위·실패 시 동작·허용 손실) |
| 클래스·함수 docstring | 한국어 | **왜**와 계약만. 이름·시그니처로 알 수 있는 "무엇"은 쓰지 않는다 |
| 줄 주석 | 한국어 | 코드만 보고는 알 수 없는 이유(예: `# 값 없는 BAD는 마지막 값을 지우지 않는다(품질만 바뀜).`) |
| OpenAPI 설명(`Field(description=…)`) | 한국어 | 계약 생성물(`core.json`·`schema.d.ts`)에 그대로 들어간다 |
| CLI `--help`(argparse `help=`) | 한국어 | 운영자가 읽는다 |
| 로그 메시지 | 영어 | 운영 grep·외부 도구용. `log.warning("cannot load definitions (will retry): %s", exc)` |
| 예외 메시지·CLI stdout/stderr | 영어 | `f"{key} must be an integer, got {raw!r}"` |
| UI 문구 | 한국어 | 운영자용 화면 텍스트 |
| `.importlinter` 등 ASCII 강제 파일 | 영어 | 한국어 Windows에서 cp949로 읽힌다 |

- 주석이 코드를 그대로 다시 말하면 지운다. 주석이 길어지면 이름이나 구조로 표현할 수 없는지 먼저 본다.
- 모듈 docstring이 없는 기존 파일은 손댈 때 채운다.
- TODO·FIXME로 미완성을 남기지 않는다. 남는 일은 PR 본문 백로그 표에 쓴다.
- `print`는 CLI 진입점(`cli.py`·`main.py`·`if __name__ == "__main__"` 단독 스크립트)에서만 쓰고, 라이브러리 코드는 `log`를 쓴다. 기계가 읽는 출력은
  `--json` 옵션으로 `json.dumps(..., sort_keys=True)`를 낸다.

## 8. 에러 처리·로깅·비동기

- 모듈마다 기반 예외 하나를 두고 세부 예외를 파생한다. 다른 모듈이 잡아야 하는 예외는 `ports.py`에 둔다.
- 변환할 때는 `raise NewError(...) from exc`(원인 보존) 또는 `from None`(원인이 잡음일 때)을 명시한다.
  맨 `except:`와 이유 없이 예외를 삼키는 `pass`는 금지다.
- 넓은 `except Exception:`(정리 경로에서는 `BaseException`)은 아래 셋에서만 쓴다.
  1. **오래 도는 loop·task 경계** — 한 번의 실패가 수집·flush·이벤트 전달 전체를 멈추지 않게 로그를 남기고
     계속한다(예: `historian/writer.py` flush loop, `platform/bus.py` 핸들러 호출). 반복될 수 있는 실패는
     traceback을 첫 실패·상태가 바뀔 때만 내고(`log.exception`), 이후는 `log.warning`이나 간격을 둔다.
  2. **변환 후 재던짐** — `raise MigrationFailedError(...) from exc` (`platform/migrate.py`).
  3. **정리 후 재던짐 또는 명시적 best-effort** — 자원을 정리하고 원래 예외를 다시 던지거나(`platform/db.py`),
     실패해도 되는 작업을 `contextlib.suppress(Exception)` 또는 `log` 후 계속으로 처리한다(updater `phases.py`
     gc). best-effort인 이유는 주석이나 docstring에 적는다.
- 일시적 실패(DB·collector 단절)는 **로그 + 재시도 + 상태 노출**(`db_ok`, `/readyz`)로 다루고 프로세스를
  죽이지 않는다(graceful degradation). 설정 오류처럼 고칠 수 없는 것은 기동 시 바로 실패한다(`SettingsError`).
- 로거는 모듈마다 `log = logging.getLogger(__name__)`, 인자는 `%s` 지연 포맷으로 넘긴다(f-string 금지).
  반복되는 경고는 간격을 두고 낸다(예: `DROP_WARN_INTERVAL_S`).
- 모든 외부 I/O에는 제한 시간을 건다(`asyncio.timeout`, DB 쿼리 `timeout`, `subprocess` `timeout`). 제한 값은
  이름 붙은 상수다.
- 백그라운드 task는 소유자가 `start()`/`stop()`으로 만들고 거둔다. 고아 task 금지.
- **임계 구역**: `TagStore`처럼 공유 상태를 바꾸는 메서드는 동기(`await` 없음)로 써서 이벤트 루프 안에서
  끊기지 않게 하고, 바뀐 결과(이벤트 목록)를 **반환**한다. DB I/O·이벤트 발행은 호출자가 그 뒤에 한다
  (불변 규칙 Lock discipline).
- **bus 핸들러**: `EventBus.publish`는 핸들러를 순서대로 await 하므로 핸들러는 I/O를 await 하지 않는다 — 메모리
  갱신·큐 적재만 하고, 새 이벤트는 직접 발행하지 않고 **돌려준다**(버스가 이어서 발행, M3b-2 D8).

## 9. 테스트

- 파일은 `tests/test_<모듈>.py`, 모듈 docstring 첫 줄에 이 파일이 증명하는 것을 쓴다(빠진 기존 파일은 손댈 때).
- 테스트 이름은 행동을 말한다: `test_columns_convert_ns_to_us`.
- 가짜 객체는 **직접 쓴 작은 클래스**(`FakeStore`, `Clock`)로 Protocol을 만족시킨다. `unittest.mock`은 쓰지
  않는다 — 가짜가 Protocol과 어긋나면 pyright가 잡게 하기 위해서다.
- `monkeypatch` 허용: 환경변수, 표준 라이브러리·서드파티 함수(`os.*`, `sys.stdin`, `asyncpg.create_pool` 등).
  금지: 자기 코드의 모듈 속성·private 메서드 치환 → 그 값을 주입 인자로 바꾼다.
- 기대값은 구현에서 계산하지 않고 독립적으로 적는다(손으로 계산한 값·고정 예시).
- DB가 필요한 테스트는 `@pytest.mark.integration`(compose에서만 실행), 부하 측정은 `perf`. 무조건 `skip`·
  `.only`·빈 테스트로 통과시키지 않는다. 환경 조건부 `skipif`(OS·외부 도구 유무)는 `reason`을 적고 허용한다.

## 10. 프론트엔드 (`services/web`, 처방 — M3b부터 적용)

- API 타입은 `packages/contracts/openapi/core.json`에서 생성한 `src/api/schema.d.ts`만 쓴다(`openapi-fetch`).
  응답 타입을 손으로 다시 선언하지 않는다.
- 비동기 화면 상태는 판별 유니온으로 표현한다: `{ kind: "loading" } | { kind: "ok"; value } | { kind: "error"; message }`.
- `any`·non-null `!` 대신 타입 좁히기(`instanceof`, `in`, 판별 필드)를 쓴다. `as` 단언은 TS가 표현하지 못하는
  경우(예: `Object.keys` 결과를 키 유니온으로)만 허용한다.
- 화면은 `src/pages/<Name>.tsx`, WebSocket은 `src/ws/`, 재사용 컴포넌트는 두 번째 사용처가 생길 때 분리한다(4절).
- 상수는 단위 접미사를 붙인다(`POLL_MS`). effect는 정리 함수(`AbortController`·`clearInterval`)를 반드시 돌려준다.
- 상태 관리·UI 라이브러리 도입은 필요가 생긴 PR에서 근거와 함께 결정한다(legacy [frontend](frontend.md)의
  Jotai·singleton WS 등을 자동으로 가져오지 않는다).

## 11. 리뷰 체크리스트 (가독성·구조)

리뷰 워커는 정확성 검토와 **별도로** 이 항목을 확인하고, 어긋나면 심각도(대개 LOW·MEDIUM)와 함께 적는다.
12절 백로그에 있는 기존 위반은 새로 지적하지 않는다(그 파일을 고친 PR이면 맞췄는지 본다).

- [ ] 새 모듈이 3절 구조와 import-linter 세 계약에 맞게 등록됐는가
- [ ] 새 Protocol·추상화가 4절의 네 경우 중 하나인가, 쓰이지 않는 옵션·확장점은 없는가
- [ ] 가변 상태의 소유자가 하나이고 밖으로는 frozen 스냅샷을 내는가(3절)
- [ ] 이름이 5절을 따르는가(특히 단위 접미사, `Pg` 접두, `…Error`)
- [ ] 함수·파일이 한 문장으로 설명되는가(6절), 모듈 docstring 첫 줄이 그 문장인가
- [ ] 주석이 "무엇"을 반복하지 않고 "왜"·계약을 말하는가, 언어가 7절 표와 맞는가
- [ ] 넓은 catch가 8절의 세 경우인가, I/O에 이름 붙은 제한 시간이 있는가, 임계 구역 안에 await·I/O·발행이 없는가
- [ ] 가짜 객체가 직접 쓴 클래스이고, monkeypatch가 자기 코드를 치환하지 않고, 기대값이 독립적인가(9절)
- [ ] `print`는 CLI 진입점에만 있고 백그라운드 task에 소유자가 있는가(7·8절)
- [ ] (web) 생성 타입만 쓰고, `any`·`!`·불필요한 `as`가 없고, effect가 정리 함수를 돌려주는가(10절)
- [ ] 같은 일을 하는 기존 코드와 모양이 같은가(1절 4번)

## 12. 현재 코드와의 차이 (백로그, 2026-10-11 검토 기준)

규칙은 맞고 기존 코드 일부가 어긋나는 곳이다. 그 파일을 고치는 PR에서 함께 맞춘다.

| 규칙 | 위치 | 내용 |
|---|---|---|
| 8절 제한 시간 상수 | `services/updater/src/pureiron_updater/images.py` | `timeout=60/1800/30/120` 리터럴 → 이름 붙은 상수 |
| 9절 monkeypatch | `services/updater/tests/test_cli.py`(`cli.real_deps`), `deploy/tests/test_build_bundle.py`(`SIZE_LIMIT`), `packages/bundle-tools/tests/test_pib.py`·`test_keys_images_cli.py`(`BoundedTarInfo._base_proc_member`), `services/updater/tests/test_phases.py`(`pureiron_updater.gc.collect`) | 자기 코드 치환 → 주입 인자 |
| 2절 예외 이유 주석 | 루트 `pyproject.toml` `"**/tests/**" = ["PLR0915"]` | 이유 주석 없음 |
| 8절 로거 이름 | `getLogger("pureiron_updater")`, `getLogger("simulator")` | `__name__`으로 |
| 5절 크기 단위 접미사 | `SIZE_LIMIT`(`deploy/bundle/build_bundle.py`), `METADATA_LIMIT`(`pureiron_bundle/pib.py`) | `_BYTES` 접미사 |
| 5절 환경변수 접두 | updater `PUREIRON_POLL_INTERVAL_S`·`PUREIRON_CHECK_TIMEOUT_S`(서비스 전용인데 접두 없음) | **변경하지 않는다** — 현장 `.env`·compose와 맞물린 배포 계약 |
| 7·9절 docstring | 모듈 docstring 없음: `services/control-api/src/pureiron_control/app.py`; 테스트 docstring 없음 17개(예: collector `test_modbus.py`·`test_plan.py`) | 손댈 때 채움 |

## 13. 이 문서를 바꾸는 법

코드가 이 문서와 달라져야 할 이유가 생기면 코드를 먼저 바꾸지 말고 이 문서를 PR로 고친다(이유 포함). 규칙을
바꾸는 PR은 기존 코드를 함께 맞추거나, 맞추지 않으면 12절에 적는다.

---

<!-- source: docs/wiki/sot/data-contracts.md -->
# Data Contracts SOT — DB·설정 스키마

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
# tags/<device>.yaml: [{ path, device, modbus:{register_type,address,data_type,byte_order,word_order}, eu?, description?, record?:{deadband,max_interval_s}, write?:{kind,…} }]
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

ADR-013(개정) 쓰기 경로의 계약과 collector 실행부다. core 쓰기 서비스·REST·감사(현장 스위치 `PUREIRON_WRITES_ENABLED`, 2단계 확인, 실행 전 검사, readback 판정)는 M3c-2 — 그 전까지 core는 `WriteRequest`를 보내지 않고 plan의 `writable`은 전부 false다. migration 없음(쓰기 선언은 `tags.definition jsonb` 원문에 같이 저장된다) → epoch 그대로.

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
- **collector 실행**: 쓰기 함수는 coil FC5, holding 1워드 FC6, 2·4워드 FC16뿐이다(PLC 로직 불변 — `ModbusClient` 표면 고정 테스트). 값은 `encode()`(= `decode()`의 역, byte·word order 포함). 장비별 `DevicePoller`가 쓰기 큐를 **poll과 poll 사이**에 같은 task·같은 연결로 하나씩 실행해 한 장비에 동시 트랜잭션이 없다. 자동 재시도 없음. 결과를 기다리는 동안에도 세션은 Ack·plan을 계속 읽는다.
- **dev 시뮬레이터(`SIM_MEMORY`, 기본 100)**: holding N..N+M-1(N = `SIM_REGISTERS`)과 coil 0..max(N, M)-1은 사인파로 덮어쓰지 않는 메모리. 거부 시험 주소: holding N+M-1 쓰기 → 예외 응답(ILLEGAL DATA VALUE), N+M-2 쓰기 → 정상 응답하지만 값 유지(readback 불일치). compose 기본값이면 메모리 holding 100..199, 예외 199, 무시 198. 허가 조건 coil은 시험 하네스가 직접 바꾼다.

---

<!-- source: docs/wiki/sot/frontend.md -->
# Frontend SOT — React·Jotai·SCADA UI·3D Twin

# Frontend SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다. V1 새 코드의 작성 규약은 [coding-style](coding-style.md) 10절이 우선한다. **V1 `services/web` 현행은 16절**이다.

이 문서는 PureIron frontend의 **구조·상태·실시간·UI 작업 규칙**에 대한 정본이다. 상세 구현은 `frontend/src/**`가 정본이고, `frontend/AGENTS.md`와 `frontend/CLAUDE.md`는 도구별 operational cheat sheet다.

Frontend는 v1에서 “운영자가 매일 보는 경량 Web HMI/SCADA 화면”이어야 한다. SaaS 랜딩처럼 보이는 장식형 UI가 아니라, 태그·알람·트렌드·MES·3D Twin·AI Chat을 빠르게 읽고 조작할 수 있는 작업 도구가 목표다.

## 1. 문서 경계

| 문서 | 역할 |
|---|---|
| `docs/wiki/sot/frontend.md` | frontend 아키텍처와 변경 규칙의 정본 |
| `frontend/AGENTS.md` | frontend 작업 전 읽는 짧은 guardrail |
| `frontend/CLAUDE.md` | Claude Code용 atom/API/화면 cheat sheet |
| [legacy `docs/UI_GUIDE.md`](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/UI_GUIDE.md) | 운영자 중심 UI 디자인 가이드(legacy-final) |
| `frontend/src/**` | 실제 구현과 타입의 최종 정본 |

이 문서는 코드 전체를 복붙하지 않는다. 신규 기능을 추가할 때 어떤 경계를 지켜야 하는지, 어떤 파일군을 같이 봐야 하는지만 고정한다.

## 2. 스택과 실행 명령

`frontend/package.json` 기준 스택:

- React 19 + TypeScript
- Vite 6
- Jotai
- React Router v7
- React Three Fiber + Drei + Three
- Recharts
- Lucide React
- Tailwind CSS v4 via `@tailwindcss/vite`
- `motion`, `react-grid-layout`, `react-markdown`

명령은 반드시 `frontend/`에서 실행한다.

```bash
npm run dev      # Vite dev server: http://localhost:3000
npm run lint     # tsc --noEmit
npm run build    # production build
```

Frontend 변경의 최소 검증은 `npm run lint`다. 라우팅, import, bundling, UI composition이 바뀌면 `npm run build`까지 실행한다.

## 3. 디렉터리 책임

| Path | Responsibility |
|---|---|
| `src/api/` | REST API client. API 함수는 auth atom을 직접 읽지 않고 `token: string | null`을 인자로 받는다. |
| `src/atoms/` | Jotai app state와 app-level hook. WS bridge도 여기서 연결한다. |
| `src/components/` | route screen과 재사용 UI. 큰 화면 변경은 회귀 위험이 높으므로 unrelated refactor 금지. |
| `src/components/process/` | 공정 화면 composition: Overview / SCADA / 3D View. |
| `src/components/scada/` | 공정별 SCADA visualization. |
| `src/components/twin/` | 3D Twin runtime, model loading, scene controls, tag mapping. |
| `src/components/dashboard/` | 대시보드 grid/widget 런타임. |
| `src/components/mes/` | MES recipe/plan/task/calendar UI. v1 고객 노출 라벨·플로우 계약은 [mes-schedule](mes-schedule.md). |
| `src/components/settings/` | 설정 화면 탭과 local UI state. |
| `src/config/` | frontend client-side config/registry. |
| `src/hooks/` | 재사용 React hook. |
| `src/lib/` | UI/helper utility. |
| `src/registry/` | tag registry bridge와 tag event helper. |
| `src/types/` | shared TypeScript type. |
| `src/ws/` | WebSocket client singleton. |

## 4. 상태 관리 원칙

Jotai state는 `src/atoms/`에 둔다. 컴포넌트 안에 임시 상태를 둘 수는 있지만, 앱 전체에서 공유되는 상태를 ad hoc global 변수나 별도 singleton으로 만들지 않는다.

현재 주요 atom 파일:

| File | Responsibility |
|---|---|
| `auth.ts` | 인증 상태, `localStorage('pureiron_auth')`, `useAuthFetch()` |
| `tags.ts` | WS 연결 상태와 live tag value map, `useTag()`/`useTags()`/`useWsSetup()` |
| `theme.ts` | theme, `localStorage('pureiron-theme')`, `<html data-theme>` |
| `language.ts` | app shell language, `localStorage('pureiron-language')`, server hydration from `GET /api/settings/me` |
| `scene.ts` | 3D Twin active model, scene config, highlighted tag |
| `trend.ts` | trend tag config |
| `dashboard.ts` | dashboard layout/widget state |
| `devices.ts` | device UI state |
| `alarmUi.ts` | alarm UI state |

규칙:

- `useWsSetup()`은 앱 루트에서 1회 호출한다.
- 컴포넌트는 live tag가 필요하면 `useTag()` 또는 `useTags()`를 사용한다.
- 컴포넌트에서 별도 WebSocket을 직접 열지 않는다.
- app-level 상태를 prop drilling으로 억지 전파하지 말고 atom/hook 경계를 사용한다.
- 언어 설정은 `languageAtom`과 `useLanguageSync()`가 담당하며, `GET/PATCH /api/settings/me`의 `language` 계약을 유지한다.
- 시스템 언어의 대상은 사용자가 수정하지 않는 제품 고정 UI 문자열이다. 언어 변경을 구독하는 화면은 새로고침 없이 반영해야 하며, navigation·Settings·공통 controls·Login·Dashboard 편집 UI·전역 Alarm drawer는 필수 동적 번역 범위다. 이후 운영 화면도 같은 계약으로 단계적으로 전환하며, 새 고정 문자열을 locale 하드코딩으로 추가하지 않는다.
- 사용자 저장 제목/캡션, seed 데이터, tag path, 설비명, 단위는 자동 번역하지 않는다. enum/quality/status의 표시 문자열은 locale별 mapper/translation key를 사용하고 raw 계약값은 유지한다.

## 5. WebSocket·실시간 태그 규칙

`src/ws/client.ts`의 `scadaWS`가 WebSocket singleton이다.

- URL은 현재 host 기준 `/ws`다.
- `ScadaWebSocket`은 reconnect backoff와 ref-counted subscription을 가진다.
- `subscribe(tags)`/`unsubscribe(tags)`는 중복 구독을 ref count로 처리한다.
- reconnect 후 현재 subscription set을 다시 전송한다.
- tag update는 `src/atoms/tags.ts`에서 Jotai `tagValuesAtom`에 반영하고, 같은 업데이트를 `registry.applyTagBatch()`로 전달한다.

금지:

- 컴포넌트에서 `new WebSocket(...)` 생성.
- `scadaWS.unsubscribe()`를 우회하는 cleanup.
- `registry.applyTagBatch()` 경로를 건너뛰는 tag update 반영.
- Device health를 tag로 취급하는 UI 계약. Device health는 backend `/api/devices` merged response가 정본이다.

## 6. API client 규칙

- API helper는 `src/api/`에 둔다.
- 함수는 `token: string | null`을 인자로 받는다. Jotai atom을 API layer에서 직접 읽지 않는다.
- 인증 header는 기존 `authHeaders(token)` 패턴을 따른다.
- 오류 처리는 기존 `handleResponse<T>()` 계열 패턴을 재사용한다. FastAPI `detail` 파싱 동작을 깨지 않는다.
- backend contract가 바뀌면 `docs/wiki/sot/api-events.md`와 관련 타입을 같이 확인한다.

## 7. TypeScript 규칙

절대 금지:

- `as any`
- `@ts-ignore`
- `@ts-expect-error`
- `tsconfig` strictness 약화
- lint/build 통과만을 위한 타입 무력화

대안:

- 명시적 interface/type 정의
- discriminated union
- `unknown` payload에 대한 type guard
- component boundary에서 props type 고정
- API/SSE payload는 좁혀서 처리

`npm run lint`는 ESLint가 아니라 `tsc --noEmit`이다. 따라서 타입 오류를 숨기는 방식은 허용하지 않는다.

## 8. 공정 화면 규칙

`src/types/processes.ts`의 `ProcessType`과 `PROCESS_META`가 frontend 공정 표시명·색상·아이콘·route의 단일 진실 소스다. Backend `ProcessType` enum과 동기화되어야 한다.

공정 화면 기본 구조:

```text
ProcessLayout
├── Overview        → ProcessScreen / ProcessTagPanel / ProcessSchedulePanel
├── Custom SCADA    → src/components/scada/*
└── 3D View         → ProcessTwinView, enable3D=true인 경우
```

새 공정 추가 시 최소 변경 범위:

1. `src/types/processes.ts`: `ProcessType`, `PROCESS_META`
2. `src/App.tsx`: route
3. `src/components/Layout.tsx`: sidebar item
4. `src/components/process/*Screen.tsx`: `ProcessLayout` 기반 화면
5. Backend process enum/contract 동기화

공정명, 색상, icon, route를 컴포넌트마다 하드코딩하지 않는다.

## 9. 3D Twin 규칙

3D Twin은 React Three Fiber 기반이다.

- scene 상태는 `scene.ts` atom을 통해 공유한다.
- `NodeMapping`은 mesh name과 tag path를 연결한다.
- `tag_reference` 이벤트는 `highlightedTagAtom`으로 들어가고, 3D mesh highlight로 연결된다.
- 3D View는 “보여주기용 장식”이 아니라 operator가 설비/태그 맥락을 이해하기 위한 화면이다.
- v1에서 실제 설비 자동 제어 또는 AI 기반 자동 제어를 3D UI에 추가하지 않는다. 위험 액션은 backend 승인 흐름과 UI 승인 UX를 유지한다.

## 10. ChatBot SSE 규칙

주 구현은 `src/components/ChatBot.tsx`다. 샘플/복사본 파일은 명시적으로 그 파일을 수정하는 작업이 아니면 reference로만 본다.

보존해야 하는 SSE event type:

- `token`
- `tool_call`
- `tool_result`
- `approval_request`
- `tag_reference`
- `error`
- `done`

규칙:

- 위험 액션은 `ApprovalCard` 승인 UX를 거친다.
- UI에 direct-execute path를 추가하지 않는다.
- 도구명 한글 표시와 code block copy UX는 유지한다.
- `tag_reference`는 3D highlight와 연결된다.

## 11. Operator UI 규칙

PureIron frontend는 운영자 중심 SCADA UI다. [legacy `docs/UI_GUIDE.md`](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/UI_GUIDE.md)가 상세 디자인 가이드다.

우선순위:

1. 한 화면에서 핵심 운영 정보를 빠르게 읽을 수 있는 데이터 밀도
2. 상태·알람·품질·시간의 시맨틱 표현
3. 예측 가능한 조작: button / toggle / numeric input / tab
4. desktop과 mobile에서 텍스트가 깨지지 않는 레이아웃
5. 과한 장식보다 장시간 사용 가능한 절제된 UI

금지:

- marketing landing page 스타일
- glass morphism, blur orb, gradient text, neon glow
- “Powered by AI” 같은 가치 없는 장식 배지
- 무의미한 hover scale과 과한 page-load animation
- nested card 남발
- 랜덤 chart color

허용되는 animation은 값 변경 highlight, 짧은 fade/slide, loading spinner 정도다.

## 12. 변경·검증 원칙

Frontend 작업은 화면이 크고 회귀 위험이 높다. 다음 원칙을 지킨다.

- 요청 범위 밖 unrelated refactor 금지.
- 기존 route/sidebar entry 삭제는 명시 요청 없이는 금지.
- generated/dependency file은 dependency update 작업이 아니면 수정 금지.
- backend contract가 바뀌면 frontend type/API client와 `api-events.md`를 함께 본다.
- UI 변경은 가능하면 작은 화면/컴포넌트 단위로 나누고, 각 단계마다 `npm run lint` 또는 `npm run build`로 확인한다.

검증 기준:

| 변경 유형 | 최소 검증 |
|---|---|
| Type/API/hook 변경 | `npm run lint` |
| Route/import/component composition 변경 | `npm run lint && npm run build` |
| Backend contract 동반 변경 | frontend 검증 + 관련 backend test |
| SOT 문서 변경 | repo root에서 `python scripts/wiki.py build && python scripts/wiki.py check` |

## 13. Design system primitive 계약

업무 화면의 공통 control과 surface는 `frontend/src/components/ui/index.ts`의 public API를 사용한다. 시각 계약은 `frontend/src/styles/ds/primitives.css`가 소유하며, 화면이 같은 button/input/card/table/state class bundle을 다시 정의하지 않는다.

| Primitive | 용도와 불변 계약 |
|---|---|
| `Button`, `IconButton` | `primary/secondary/danger/ghost`, `sm/md`, loading·disabled, 공통 `focus-visible`, coarse pointer 44px touch target. Icon-only control은 접근 가능한 `label` 필수. |
| `Field`, `Input`, `Select`, `Textarea`, `SearchInput` | label/control/message id와 `aria-invalid`를 연결하고 semantic surface/border/focus token을 사용한다. |
| `Card`, `SectionCard`, `StatCard` | 업무 surface·section heading·stat tone을 통일한다. nested card는 피한다. |
| `DataTable`, `SortableHeader`, `Pagination` | table semantics, `aria-sort`, loading/error/empty row, pagination status를 통일한다. 복잡한 계층형 table도 최소한 `ds-table` visual contract를 사용한다. |
| `EmptyState`, `LoadingState`, `ErrorState`, `Alert` | 정상 empty와 장애 error를 구분하고 live region 및 recovery action을 제공한다. |
| `Tabs`, `TabList`, `Tab`, `TabPanel` | 같은 영역의 peer view 콘텐츠 전환을 통일한다. controlled `value`/`onValueChange`, `role="tablist"`/`"tab"`/`"tabpanel"`, `aria-selected`, `aria-controls`/`aria-labelledby`, roving tabIndex, Left/Right·Home/End 이동, disabled tab skip, overflow 시 tablist만 가로 스크롤. active 색상·focus는 `--accent`/`--border-focus` token으로만 표현한다. |
| `SegmentedControl`, `Segment` | 한 view의 표시 범위/모드를 바꾸는 compact single-select를 통일한다(예: 생산 일정 Month/Week/Day). controlled `value`/`onValueChange`, `role="radiogroup"`+접근 가능한 이름, `role="radio"`/`aria-checked`/disabled, roving tabIndex, Left/Right·Up/Down·Home/End 이동, disabled option skip, 컨트롤 자체 overflow 흡수로 페이지 가로 overflow 방지. 선택 상태는 raw fill이 아니라 `--surface-card`/`--text-accent`/`--border-focus` token으로 표현한다. |
| `SideNav`, `SideNavItem` | 사이드바 계층 이동을 통일한다(예: 설정 화면 Profile/System/Security/Notifications/Accounts/RAG Lab). controlled `value`/`onValueChange`, `nav` landmark + 접근 가능한 이름, 활성 항목 `aria-current="page"`, disabled 항목 계약, 공통 `focus-visible`. 각 항목은 실제 `<button>`이라 roving tabIndex가 아니라 Tab 키로 순차 이동한다. 활성 상태는 raw indigo fill이 아니라 `--accent-dim` 배경, `--accent` border, AA 대비를 유지하는 `--text-1` label, `--border-focus` focus token으로 표현하고, full-width 항목은 데스크톱/모바일 모두 페이지 가로 overflow를 만들지 않는다. |
| `StatusBadge` | 장비·통신·알람의 운영 상태를 `normal/info/warning/critical/offline/unknown` tone으로 통일한다. backend raw status는 화면 경계에서 tone으로 매핑하고, 모든 상태는 색상뿐 아니라 보이는 text+tone별 icon을 함께 제공한다. 지속 정상 상태에 pulse/glow를 쓰지 않으며 semantic status/surface token만 사용하고 393px 페이지 가로 overflow를 만들지 않는다. Alarm severity(priority)·lifecycle state 매핑 단일 소스는 `frontend/src/lib/alarmHelpers.ts`이고, Alarm Summary/History와 전역 Alarm drawer가 이를 공유한다(매핑 표는 [legacy `docs/UI_GUIDE.md`](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/UI_GUIDE.md)). |

**Tabs vs SegmentedControl vs SideNav 경계** — `Tabs`는 한 영역의 콘텐츠를 peer view로 교체할 때(예: 시스템 기록 3분류, 공정 Overview/SCADA/3D). 같은 콘텐츠의 표시 범위/모드만 상호배타적으로 토글하면 `SegmentedControl`(예: 생산 일정 Month/Week/Day), 사이드바 계층을 이동하며 콘텐츠 영역 전체를 교체하면 `SideNav`(예: 설정 화면 하위 카테고리)가 담당한다. 화면을 바꾸지 않고 콘텐츠 자체를 바꾸면 `Tabs`, 콘텐츠는 그대로 두고 범위/모드만 바꾸면 `SegmentedControl`, 목적지(콘텐츠 영역 전체)를 바꾸는 사이드 navigation이면 `SideNav`가 정답이다.

추가 규칙:

- primitive CSS는 `styles/ds/tokens.css` 계열 semantic token만 참조하고 raw hex/palette를 추가하지 않는다.
- `frontend/src/index.css`에서 `styles/ds/primitives.css`를 한 번만 import한다.
- Dashboard 전용 `ds-btn`/`ds-icon-btn` 구현을 별도로 유지하지 않는다. 동일 class contract는 primitives가 소유한다.
- 대표 관리 화면 migration과 primitive 변경은 light/dark, keyboard focus, disabled/error/empty 상태를 E2E 또는 component contract로 검증한다.

## 15. Overlay 계약

제품 화면의 modal surface는 `frontend/src/components/ui/index.ts`가 공개하는 `Dialog`, `Drawer`, `ConfirmDialog`를 사용한다. 화면별 `fixed inset-0` backdrop이나 임의 z-index를 새로 만들지 않는다.

- `Dialog`와 `Drawer`는 portal, overlay stack, Escape, focus trap/restore, scroll lock, background `inert`를 공통 소유한다.
- `sm/md/lg/wide` 크기는 primitive의 size prop으로 선택하고 화면별 width/backdrop/radius/shadow 계약을 만들지 않는다.
- 파괴적 작업은 안전한 취소 버튼에 initial focus를 두는 `ConfirmDialog` 또는 `useConfirm()`을 사용한다. 신규 제품 flow에 native `window.alert()`/`window.confirm()`을 추가하지 않는다.
- `dismissible=false`인 busy overlay는 Escape, backdrop, header close button으로 닫히지 않아야 한다.
- migration은 대표 Desktop/Mobile keyboard E2E로 focus trap/restore, Escape, background `inert`, 중첩 overlay 순서를 검증한다.
- 기존 화면 migration은 회귀 위험을 줄이기 위해 Device/Dashboard/Settings → Tag/UDT → Schedule/MES → Trend/ChatBot 순서의 작은 PR로 진행한다.

## 16. V1 web (M3b)

> 이 절은 **PureIron_V1 현행**이다(위 1~15절은 legacy 기준). 근거: plan M3b D10·D13·D14·§2.5, 규약은 [coding-style](coding-style.md) 10절.

**스택**: Vite + React 19 + TypeScript strict, `openapi-fetch`(생성 타입), `react-router`(라우팅·로그인 보호), `uplot`(트렌드 canvas — 1h 원시 3,600점 × 여러 태그), `@playwright/test`(devDep). 상태 라이브러리·UI 키트는 없다(React state + WS 클라이언트 모듈, 스타일은 `src/styles.css`). 편집 화면·쓰기 UI는 없다(No config UI, PLC 쓰기는 M3c).

| 경로 | 책임 |
|---|---|
| `src/App.tsx` | 경로 표. `/login` 밖은 모두 `Layout` 아래 |
| `src/app/Layout.tsx` | `GET /auth/me`로 세션 확인, 상단 내비·사용자/역할·로그아웃·미확인 알람 배지·실시간 연결 상태, WS 연결 하나와 알람 목록 소유 |
| `src/app/session.ts` · `alarms.ts` | 세션 context(`useSession`), 알람 목록 갱신 규칙(서버와 같은 집합·순서) |
| `src/api/client.ts` | `openapi-fetch` 클라이언트(`credentials: "include"`), 401 → 로그인 이벤트, 역할 비교·실패 문구, 생성 타입 별칭 |
| `src/api/schema.d.ts` | 생성물(`npm run gen:api`, contracts-drift가 검사). REST·WS 메시지 타입의 정본 |
| `src/ws/client.ts` | `RealtimeClient` |
| `src/pages/` | `Login` · `Overview`(태그) · `Alarms`(활성·ACK·저널) · `Trend` · `Devices` · `Audit` |
| `e2e/` · `playwright.config.ts` | compose 스택 위 chromium 스모크 |

**상태 규칙**
- 비동기 화면 상태는 `Load<T>` = `{kind:"loading"} | {kind:"ok",value} | {kind:"error",message}`. 응답 타입을 손으로 다시 선언하지 않는다.
- effect는 정리 함수(AbortController·WS 해제·ResizeObserver/uPlot 해제)를 돌려준다. 다시 불러올 때의 loading 전환은 effect가 아니라 이벤트 핸들러에서 한다.
- 실패 문구는 HTTP 상태로 고른 고정 문구다(서버 문구를 그대로 보여주지 않음). 로그인 실패는 "아이디 또는 비밀번호가 올바르지 않습니다."
- 401이 오면(로그인 요청 제외) `/login?next=<원래 경로>`로 보내고, 로그인 뒤 그 경로로 돌아간다(같은 사이트 경로만).
- 역할별 표시: ACK·전체 ACK는 운전원 이상, 감사 로그는 엔지니어 이상(아래는 권한 없음 안내). 서버가 같은 규칙으로 403을 낸다.

**WS** (`/api/v1/ws`, 쿠키 인증)
- 로그인한 레이아웃이 연결 하나를 만들고 화면이 `on(type, handler)`·`subscribe(tagIds)`(참조 계수)로 나눠 쓴다. 해제 함수를 effect 정리에서 부른다.
- 끊기면 0.5s부터 두 배씩 최대 10s 간격으로 재연결, `hello`를 받으면 구독 전체를 다시 보낸다. `ping`에는 `pong`. 서버는 20s마다 ping을 보내므로 50s 동안 아무 메시지도 없으면 죽은 연결로 보고 소켓을 버린 뒤 같은 backoff로 재연결한다.
- 재연결(`hello`)마다 알람·장비 목록은 REST로 다시 받아 끊긴 동안 놓친 이벤트를 메운다(태그는 재구독 때 서버가 현재값을 보냄).
- 알람 목록: 스냅샷 요청(제한 시간 10s) 중 도착한 `alarm_event`는 버퍼에 모았다가 스냅샷 적용 뒤 도착 순서대로 재생한다. `changed_at`은 이 합치기에서만 비교한다(같은 알람의 스냅샷이 더 새로우면 그 이벤트는 건너뜀). 그 뒤 실시간 이벤트는 비교 없이 순서대로 적용한다. ACK·ack-all 응답은 목록에 이미 있는 알람만 갱신한다. 재조회가 실패해도 기존 목록·미확인 배지를 유지하고 버퍼를 그 위에 재생한다.
- close 4401 → 로그인으로, 4403(Origin ≠ Host) → 재연결하지 않음. 그래서 nginx는 `Host $http_host`(포트 포함), vite proxy는 `changeOrigin`을 켜지 않는다.

**명령**

```bash
cd services/web && npm ci && npm run typecheck && npm run lint && npm run build
npm run dev                      # http://localhost:3000 → core 127.0.0.1:18000 (PUREIRON_CORE_URL)
bash scripts/g1.sh web-e2e       # 이미지 빌드 → pureiron-it compose(시뮬레이터 100태그 + 알람 1개, CLI 계정) → Playwright
```

e2e 결과·trace·화면 스크린샷은 `artifacts/web-e2e/`(`test-results/`·`report/`·`screens/`·`compose.log`). `PUREIRON_IT_KEEP=1`이면 스택을 내리지 않는다. 포트·프로젝트 이름은 core 통합 테스트와 같은 환경변수(`PUREIRON_IT_PROJECT`·`PUREIRON_DEV_*_PORT`·`PUREIRON_IT_WEB_PORT`)로 바꾼다.

---

<!-- source: docs/wiki/sot/mes-schedule.md -->
# MES/Schedule SOT — v1 용어와 작업 플로우 계약

# MES/Schedule SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다.

이 문서는 PureIron의 **MES-lite / Schedule 기능의 v1 고객 노출 용어와 사용자 플로우 계약의 정본**이다.

내부 도메인 모델(`recipe` / `plan` / `task` 3계층)은 유지하되, **고객이 보는 용어와 화면 플로우는 v1 도입 저항을 낮추는 방향으로 분리**한다는 결정을 담는다. 제품 상위 의도는 [product](product.md#확장-기능--장기-방향), API 계약은 [api-events](api-events.md), 화면 규칙은 [frontend](frontend.md)를 정본으로 따르고, 이 문서는 그 위에서 "MES/Schedule를 v1 고객에게 어떻게 부르고 어떻게 쓰게 하는가"만 다룬다.

> 배경: [#133](https://github.com/circulation95/PureIron_ignition/issues/133). #127(MES CRUD 인증/RBAC) 진행 중, 기존 PLC 로직이 이미 잘 도는 현장에 "레시피 생성 → 플랜 생성 → 작업 등록" 3단계 절차와 `Recipe`라는 용어를 전면 노출하면 "MES를 새로 운영하라"는 느낌을 줘 도입 저항이 커진다는 제품 우려가 제기됐다.

## 결정 상태 표기

[first-contract](../gtm/first-contract.md#결정-상태-표기)와 같은 표기를 쓴다.

| 표기 | 의미 |
|---|---|
| `확정` | 이 이슈에서 정해진 SOT 계약. 구현은 이 계약을 따른다 |
| `가설` | 검증 전 작업가설. 데모/현장 피드백으로 검증해야 함 |
| `needs-founder-input` | 창업자/대표 고객 확인 없이는 최종 확정하면 안 되는 항목(주로 브랜드 문구) |

## 1. 내부 모델 vs 고객 노출 라벨 분리

`확정` **내부 모델명과 고객 노출 라벨을 분리한다.** DB/API는 그대로 두고, 프론트엔드 표시 문자열만 바꾼다.

이유:
- 내부 3계층(`recipe`/`plan`/`task`)은 장기 에이전트 시나리오(아래 [4](#4-에이전트-연계-시나리오에서-3계층이-필요한-지점))에서 확장 가치가 있어 당장 대수술하지 않는다.
- DB/API migration을 함께 바꾸면 #127 같은 안전 경계 작업과 섞여 범위·리스크가 커진다.
- 라벨만 프론트에서 바꾸면 회귀 위험이 UI 문자열로 국한된다.

따라서 다음은 **v1에서 바꾸지 않는다**(내부 이름 유지):
- DB 테이블: `mes_recipes` / `mes_plans` / `mes_tasks`
- REST 프리픽스/경로: `/api/mes/recipes` · `/api/mes/plans` · `/api/mes/tasks`([api-events](api-events.md), `backend/src/pureiron/mes/router.py`)
- Pydantic 모델·에이전트 도구 인자: `Recipe*` / `Plan*` / `Task*`, `create_schedule(plan_id=...)`(`backend/src/pureiron/agents/tools/schedule_tools.py`)

## 2. v1 고객 노출 용어

`확정`. 내부 모델 → v1 고객 노출 라벨 매핑은 다음과 같다.

| 내부 모델 | v1 고객 노출 라벨 | 성격 |
|---|---|---|
| `Recipe` | **운전 조건** | 태그/셋포인트 묶음. 사용자는 "만든다"가 아니라 기존 조건을 **불러온다/복사한다**로 느끼게 한다 |
| `Plan` | (v1 UI에서 이름 노출 안 함) | 내부적으로만 생성하거나 고급 옵션으로 숨긴다 |
| `Task` | **작업** | 캘린더에 올라가는 실제 예약 작업. v1에서 사용자가 주로 다루는 1차 객체 |

`Recipe`의 v1 라벨 판단:

- 후보(이슈 #133): 작업 조건 세트 / 운전 조건 세트 / 공정 조건 템플릿 / 표준 조건 / 작업 템플릿.
- v1에서는 **"운전 조건"**으로 결정한다. 이유: (1) `Task`의 라벨 "작업"과 단어 충돌(작업 조건 세트/작업 템플릿)을 피한다. (2) "기존 공정의 운전 조건을 불러온다"는 v1 프레이밍에 맞는다. (3) "레시피"보다 업종 중립적이고 부담이 적다. (4) "공정 조건"은 공정 설계 자체를 새로 정의하는 뉘앙스가 있어 첫 PoC에서는 더 무겁다.
- 향후 특정 업종 현장 인터뷰에서 더 자연스러운 표현이 확인되면 표시 문자열은 조정할 수 있다. 단, v1 구현 이슈는 이 SOT 기준으로 `Recipe`를 "운전 조건"으로 노출한다.

`확정` `Recipe`를 영어 그대로 "Recipe"로 노출하지 않는다. 현재 화면(`Recipes`/`Plans` 버튼, "Manage recipes, production plans, and scheduled tasks" 카피)이 노출 그대로다 — v1 구현 시 위 매핑으로 교체한다(`frontend/src/components/ScheduleScreen.tsx`).

## 3. v1 사용자 플로우 (1~2단계)

`확정`(플로우 구조). 현재 플로우는 3단계 노출(레시피 관리 → 플랜 관리 → 작업 등록)이다. v1은 이를 **1차 1단계 + 선택적 고급 1단계**로 압축한다.

**v1 기본 플로우 (1단계): "작업 등록"**
- 진입점은 Schedule 화면의 "작업 등록"(현 `Add Task`) 하나다.
- 작업 등록 폼에서 사용자는 둘 중 하나로 조건을 채운다.
  - 기존 **운전 조건(세트)**(`Recipe`)을 선택한다, 또는
  - 최근 **작업**(`Task`)의 조건을 복사한다.
- `Plan`은 사용자가 명시적으로 만들지 않는다. 백엔드는 선택된 운전 조건에서 내부적으로 `plan`을 생성/재사용해 `task.plan_id`를 채운다(에이전트 `create_schedule`도 `plan_id`를 요구하므로, 이 자동 생성/선택 경로가 사람·에이전트 공통 진입점이 된다).

**v1 선택적 고급 플로우 (1단계): "운전 조건 관리"**
- 반복 사용할 운전 조건(세트)을 미리 정의/편집하는 화면은 **고급/설정 영역**으로 내린다. 1차 화면의 주요 버튼(`Recipes`/`Plans`)에서 빼고, 필요할 때만 진입한다.
- `Plan` 편집 UI는 v1 기본 노출에서 제외한다(내부 자동 생성 또는 고급 옵션).

즉 v1 노출 단계 수: **작업 등록(1) [+ 운전 조건 관리(선택)]**. "레시피 만들기 → 플랜 만들기 → 작업 등록" 3단계 학습을 요구하지 않는다.

> 이 절은 UX **계약**이다. 실제 프론트 구현(폼 재구성, 버튼 강등, 복사 기능)은 이 계약을 따르는 **후속 구현 이슈**에서 진행한다. 이슈 #133은 용어·플로우 결정과 SOT 반영까지를 범위로 한다.

## 4. 에이전트 연계 시나리오에서 3계층이 필요한 지점

`확정`(문서화). 내부 3계층은 아래 지점에서 실제로 쓰이므로 v1에서 제거하지 않는다.

- **지금(v1):** 에이전트 `create_schedule` 도구는 `plan_id`를 입력으로 받아 `task`를 만든다(`backend/src/pureiron/agents/tools/schedule_tools.py`, 프로덕션에서는 policy-first + approval 봉투). 따라서 에이전트가 작업을 예약하려면 `plan`이 존재해야 한다 → v1에서 `plan`은 최소/자동 생성으로 뒤에 남긴다. `task`는 사람과 에이전트가 공유하는 예약 실행 단위다.
- **장기:** 에이전트가 태그/공정 조건을 **추천·자동 구성**할 때 3계층이 각 층으로 자연스럽게 매핑된다.
  - `Recipe`(운전 조건) = 에이전트가 제안하는 태그/셋포인트 묶음(재사용 가능한 조건 세트).
  - `Plan` = 여러 조건을 순서/조합으로 엮은 다단계 공정(에이전트가 시퀀스를 구성).
  - `Task` = 그 조합의 예약 실행.
- 결론: 3계층은 **에이전트가 조건을 자동 생성/시퀀싱할 때 값을 한다.** 그 전까지는 내부 모델로 유지하고 고객 UI에는 점진적으로만 드러낸다([product](product.md#simulator--3d-digital-twin--ai-simulation-방향)의 "사람 판단을 돕는 역할" 제한과 일관).

## 5. v1 범위 밖 / 주의

- 이 문서는 **용어·플로우 계약**이다. DB/API 모델명 변경, migration, 프론트 대규모 재구성은 v1 이 이슈 범위가 아니다([1](#1-내부-모델-vs-고객-노출-라벨-분리)).
- 완전한 MES 운영(BOM, 실적 집계, 재고 연동 등)은 v1 목표가 아니다([product](product.md#mvp-제외--제한)).
- 에이전트가 사람 승인 없이 작업을 자동 실행하지 않는다. `create_schedule`는 프로덕션에서 approval 봉투를 반환한다([agent-rules](agent-rules.md#제품-판단-원칙)).
- 운전 조건 내려주기·작업 시작/정지는 **기존 PLC 주소 쓰기**로 한다. PLC 로직은 바꾸지 않으며, [ADR-013](../adr/index.md)(개정) 가드레일을 따른다: 프로젝트에 선언된 태그만(설정값은 범위, 명령은 PLC 허가 조건 필수), 고객이 켠 현장에서만, 사람 확인 또는 사람이 미리 승인한 예약 `작업`으로만 실행, 실행 전 검사·실행 후 readback·감사 로그, 자동 재시도 없음. 에이전트는 값을 제안만 한다.

---

<!-- source: docs/wiki/sot/product.md -->
# Product SOT — 제품 의도

# Product SOT

PureIron은 중소 제조업과 산업시설을 위한 경량 Web HMI/SCADA 플랫폼이다.

v1은 기존 PLC와 설비 제어 로직을 대체하지 않고, 그 위에 웹 기반 관제, 알람, 이력, 트렌드, 작업지시, Agent 보조 계층을 얹는 것을 목표로 한다.

장기적으로는 공장 운영 데이터를 Agent, Digital Twin, Ontology, 예측 모델과 연결하는 IIoT Agent Platform으로 확장한다.

## 이 문서의 원천

- [README.md](../../../README.md) — 설치/실행 진입점, 기능 요약
- [USER.md](../../../USER.md) — 사용자/팀 맥락, 장기 기술 비전, 작업 성향

## 제품 정체성

PureIron의 제품 정체성은 두 단계로 정의한다.

### v1: 경량 Web HMI/SCADA

v1은 고객이 기존 설비를 유지하면서 웹과 모바일로 설비 상태, 알람, 이력, 트렌드, 작업 지시 흐름을 확인할 수 있는 경량 Web HMI/SCADA다.

PLC 로직(프로그램)은 바꾸지 않는다. 고객 현장에서 이미 PLC로 동작하는 프로세스 위에 관제와 운영 보조 계층을 얹고, 기본은 읽기 전용이다. MES 기능(운전 조건 내려주기, 작업 시작·정지 지시)을 위해 기존 PLC 주소에 쓰는 것은 고객이 대상 태그·범위·허가 조건을 승인한 현장에서만 연다([ADR-013](../adr/index.md)).

### 장기: IIoT Agent Platform

장기적으로 PureIron은 단순 HMI 화면을 넘어선 IIoT Agent Platform으로 확장한다.

목표는 공장 운영 데이터를 사람이 보는 화면에만 머무르게 하지 않고, Agent와 Digital Twin, Ontology, 예측 모델이 이해하고 조치 후보를 제안할 수 있는 운영 지식 구조로 발전시키는 것이다.

## 대상 고객과 사용자

PureIron은 구매 의사결정자와 실제 사용자를 구분한다.

### 초기 구매/의사결정 고객

초기 핵심 고객은 소규모 제조업 사장, 공장장, 현장 관리자다.

이들은 대기업 자동화팀이나 전문 설비팀이 아니라, 현장의 문제를 직접 겪고 비용 대비 효과를 중요하게 보는 의사결정자다.

이들에게 중요한 질문은 다음과 같다.

- 기존 설비를 그대로 쓸 수 있는가?
- 현장 밖에서도 공장 상태를 볼 수 있는가?
- 알람을 빠르게 확인하고 조치할 수 있는가?
- 설치하다가 설비가 불안정해지지는 않는가?
- 외산 HMI/SCADA보다 비용 효율이 좋은가?
- 직원들이 어렵지 않게 쓸 수 있는가?
- 나중에 Agent, Digital Twin, 예측 모델로 확장할 수 있는가?

### 실제 사용자 역할

| 역할 | 하는 일 |
|---|---|
| 운영자(operator) | 라인 상태 모니터링, 알람 확인, 기본 조치 |
| 엔지니어(engineer) | 태그·디바이스·알람 셋포인트·설비 설정 |
| 관리자(administrator) | 사용자·권한 관리, 감사 로그 확인 |
| 분석가/뷰어(viewer/analyst) | Historian, Trend, 리포트, Agent 질의 |

RBAC 4개 역할은 admin / engineer / operator / viewer를 기본값으로 둔다. 권한 모델 상세는 [api-events](api-events.md)·[agent-rules](agent-rules.md)를 따른다.

## 고객이 겪는 문제

초기 고객은 다음 문제를 겪는다.

- 공장에 직접 가거나 현장에 전화해야 설비 상태를 알 수 있다.
- 알람이 발생해도 원인과 조치 이력이 체계적으로 남지 않는다.
- 설비 상태, 생산 현황, 알람, 트렌드, 작업 지시가 흩어져 있다.
- 외산 HMI/SCADA는 소규모 현장에 비싸고 무겁게 느껴질 수 있다.
- 기존 설비를 건드렸다가 생산 안정성이 깨질까 걱정한다.
- AI, Digital Twin, Agent는 관심은 있지만 당장 쓸 수 있는 형태로 느껴지지 않는다.

PureIron은 이 문제를 “기존 설비 위에 얹는 가벼운 웹 기반 관제·운영 계층”으로 해결한다.

## v1 핵심 가치

v1은 첫 계약 가능성과 영업 데모 설득력을 높이는 가치에 집중한다.

1. 기존 설비를 유지하면서 웹/모바일에서 공장 상태를 본다.
2. 알람을 즉시 확인하고 조치 이력을 남긴다.
3. 설비, 태그, 로그, 트렌드를 한 화면에서 확인한다.
4. 상태 머신 기반 Simulator와 3D Digital Twin을 연결해 실제 공정처럼 보이는 데모를 제공한다.
5. Agent가 Simulator, Tag, Alarm, Log, 3D Twin 맥락을 기반으로 이상 상황과 원인 후보를 설명한다.
6. Work Order 생성/승인 흐름을 데모한다.

핵심 경험은 다음이다.

> 공장에 가지 않아도 공장을 본다.
> 전화하지 않아도 상황을 안다.
> 복잡한 화면을 뒤지지 않아도 조치 흐름으로 이어진다.

## v1 핵심 기능

v1은 첫 계약 가능성과 영업 데모 설득력을 높이는 기능에 집중한다.

1. **Device / Tag 모델**
   - 설비와 태그를 구조화한다.
   - 향후 Agent, Digital Twin, Ontology의 기반이 된다.
   - v1은 OPC-UA / Modbus TCP / Siemens S7 / LS Electric / Allen-Bradley ControlLogix / Mitsubishi MELSEC 6개 프로토콜을 직접 지원한다. LS·MELSEC·AB는 한국·아시아 현장 영업 데모와 첫 계약 설득력을 위한 우선 지원 대상이다. 프로토콜 정본 표와 추가 정책은 [architecture](architecture.md#지원-디바이스-프로토콜-v1)를 따른다.

2. **실시간 상태 대시보드**
   - 설비 상태, 생산 현황, 주요 태그 값을 웹/모바일에서 확인한다.

3. **Alarm / Alarm History**
   - 알람 발생, 확인, 조치 이력을 관리한다.
   - 모바일 알람 확인을 핵심 경험으로 둔다.

4. **Trend / Historian**
   - 주요 태그의 시계열 변화를 저장하고 조회한다.
   - 원인 분석과 리포트의 기반이 된다.

5. **상태 머신 기반 Simulator**
   - 단순 랜덤 값 생성기가 아니라 실제 공정 흐름처럼 동작한다.
   - 영업 데모, 개발 테스트, 알람 시나리오 검증에 사용한다.

6. **3D Digital Twin View**
   - Simulator와 태그 상태를 3D 설비/공정 화면에 연결한다.
   - 고객이 공정 상태를 직관적으로 이해하게 만든다.

7. **Agent-assisted Troubleshooting**
   - Agent가 Tag, Alarm, Log, Trend, 3D Twin 맥락을 기반으로 이상 상황과 원인 후보를 설명한다.
   - v1에서는 완전 자동 제어가 아니라 사람의 판단을 돕는 역할로 제한한다.

8. **Work Order Demo (수동 handoff, backend 기능 아님)**
   - 이상 상황에서 조치 후보를 제안하고, 알람 ack·조치 note·이력 위에서 **사람이 기존 작업 지시/CMMS 프로세스로 넘기는 수동 handoff 흐름**을 데모한다.
   - v1은 Work Order를 backend에서 생성·저장·승인하지 않는다(DB/API/router 없음, `mes/`는 recipe/plan/task이지 work order 아님). 반복 가능한 데모 스크립트·검증 체크리스트는 [work-order-demo-handoff 런북](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/work-order-demo-handoff.md), 자동/수동 분리는 [testing](testing.md#5-acceptance-criteria-현재-범위) AC-18을 따른다.

9. **Auth / RBAC / Audit**
   - 사용자, 권한, 감사 로그를 제공한다.
   - 현장 운영 시스템으로서 최소한의 신뢰성을 확보한다.

## Simulator / 3D Digital Twin / AI Simulation 방향

PureIron의 Simulator는 단순 랜덤 태그 생성기가 아니다.

v1 Simulator는 상태 머신 기반으로 실제 공정 흐름처럼 동작하며, 3D Digital Twin과 연결되어 설비 상태, 알람, 이상 징후를 시각적으로 보여준다.

초기 AI Simulation은 실제 설비를 자동 제어하거나 공정을 완전 예측하는 기능이 아니다. v1에서는 Simulator, Tag, Alarm, Log, 3D Twin 맥락을 기반으로 Agent가 이상 상황의 원인 후보와 조치 시나리오를 설명하는 Agent-assisted Simulation으로 제한한다.

장기적으로는 실제 현장 데이터, Digital Twin, Ontology, 예측 모델을 연결해 공정 이상 감지, 조치 시뮬레이션, Work Order 자동 제안으로 확장한다.

## 확장 기능 / 장기 방향

다음 기능은 장기 플랫폼화를 위해 고려하지만, v1에서는 범위를 조심한다.

1. **UDT**
   - 반복되는 설비/태그 구조를 타입화한다.
   - 현장별 커스터마이징을 제품 코어로 흡수하기 위한 기반이다.

2. **고급 Historian / 리포트**
   - 장기 데이터 분석, 설비별 리포트, 가동률/OEE 분석으로 확장한다.

3. **Schedule / Recipe / Batch 등 MES-lite 기능**
   - 완전한 MES를 v1 목표로 삼지 않는다.
   - 초기에는 작업 지시, 일정, 생산 흐름 일부만 다룬다.
   - v1 고객 노출 용어와 사용자 플로우(내부 `recipe/plan/task`는 유지, 고객 라벨·1~2단계 플로우는 분리)는 [mes-schedule](mes-schedule.md) SOT를 정본으로 따른다.

4. **Ontology / Graph 기반 원인 추론**
   - 설비, 태그, 알람, 로그, 문서 관계를 그래프로 연결한다.

5. **Predictive Model 기반 이상 감지**
   - 실제 현장 데이터가 쌓인 뒤 예측 모델로 이상 징후를 감지한다.

6. **Multi-Agent 운영 지원**
   - 장기적으로 운영, 유지보수, 분석, 작업지시 Agent를 분리한다.

7. **고급 Digital Twin**
   - 단순 3D 시각화를 넘어 실제 설비 데이터와 공정 관계를 반영한다.

8. **Robot / Humanoid Automation 연계**
   - 장기 비전에는 포함하되 v1에서는 제외한다.
   - 초기에는 사람이 승인하는 Work Order 흐름까지만 다룬다.

## MVP 제외 / 제한

v1에서는 다음을 의도적으로 제외하거나 제한한다.

- PLC 로직(프로그램)을 수정하지 않는다. 기존 주소 쓰기는 고객이 승인한 현장에서 ADR-013 가드레일 아래에서만 한다(기본 읽기 전용).
- 실제 설비를 AI가 자동 제어하지 않는다.
- 공정을 완전히 예측한다고 주장하지 않는다.
- 사람 승인 없는 자동 조치를 제공하지 않는다.
- 복잡한 MES 전체를 구현하지 않는다.
- 범용 산업 전체를 한 번에 커버하려 하지 않는다.
- 복잡한 3D Digital Twin 완성도를 v1 성공 기준으로 삼지 않는다.
- Robot / Humanoid Automation은 장기 비전으로만 둔다.

기술 제약의 정본은 [agent-rules](agent-rules.md)와 [adr](../adr/index.md)를 따른다. 특히 No ORM, No plugin system, No config UI, 고정 expression 함수 제한은 v1 범위 통제 원칙이다.

## 디자인 방향

PureIron의 UI는 전통적인 낡은 HMI 느낌에 갇히지 않는다.

기본 방향은 Modern SaaS Dashboard와 산업 현장 가독성의 균형이다.

- SCADA 운영 도구처럼 보일 것 — 매일 쓰는 대시보드, 마케팅 페이지 아님.
- Mobile-first Alarm Handling을 핵심 경험으로 둘 것.
- 데이터 밀도, 명확한 상태 표현, 큰 버튼, 빠른 인지성을 우선할 것.
- 3D Digital Twin은 v1에서 고객 설득과 공정 이해를 돕는 데 집중할 것.
- AI-슬롭 안티패턴을 피할 것. 상세는 [UI_GUIDE.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/UI_GUIDE.md)를 따른다.

## 첫 계약 전략

PureIron의 첫 검증은 완성도 높은 기술 데모가 아니라 첫 유료 계약이다.

초기 전략은 완성형 SaaS가 아니라 현장 구축형 솔루션이다. 고객별 구축 과정에서 반복되는 화면, 태그 구조, 알람 조건, 공정 패턴, Work Order 흐름을 제품 코어로 흡수한다.

초기 영업 데모는 다음 가치를 짧고 명확하게 보여줘야 한다.

- 기존 설비를 유지하면서 시작할 수 있다.
- 웹/모바일로 설비 상태와 알람을 확인할 수 있다.
- Simulator와 3D Digital Twin으로 실제 공정처럼 보이는 데모를 볼 수 있다.
- Agent가 알람 원인 후보와 조치 시나리오를 설명한다.
- 외산 HMI/SCADA보다 가볍고 저렴하게 시작할 수 있다.

자세한 영업 전략, 가격, 채널, 세일즈 자료, PoC 패키지는 [first-contract](../gtm/first-contract.md) SOT에서 분리해 다룬다. 1차+추가 창업자 딥인터뷰로 첫 타깃 업종(금속·가공·프레스·사출 등 일반 중소 제조), 진입 채널, **read-only 중심 현장 PoC 범위**, 1순위 고객 페인, 첫 PoC PLC/프로토콜/네트워크 원칙, **온프레미스 우선 + 선택적 클라우드** 배포 방식, 단계형 PoC 기간(총 3주 이내), **가격 anchor**가 확정됐다. 중앙 관제/제어는 장기 방향일 뿐 v1/첫 계약에 약속하지 않는다. 가격 anchor는 시작 범위이며 실제 견적은 현장 조건별로 조정하고, 구체 고객사·현장 실측 구성 등 남은 항목은 그 문서에서 `needs-founder-input`으로 표기한다(상세는 [first-contract](../gtm/first-contract.md)).

## 성공 기준

v1의 성공 기준은 기능 개수가 아니라 고객 설득력이다.

핵심 성공 기준은 다음과 같다.

- 첫 유료 계약 가능성을 높이는가?
- 고객이 “현장 밖에서도 공장이 보인다”고 느끼는가?
- 모바일 알람 확인과 조치 흐름이 명확한가?
- Simulator + 3D Digital Twin + Agent-assisted Simulation이 데모에서 차별점으로 보이는가?
- 기존 설비를 불안정하게 만들지 않는가?
- 2인 팀이 만들고 유지할 수 있는 범위인가?
- 장기적으로 Agent, Digital Twin, Ontology, 예측 모델로 확장 가능한 데이터 구조를 남기는가?

## 성능 / 비기능 요구

기존 성능 목표는 유지한다.

- p95 응답 < 500ms
- ~1800 writes/sec 처리
- WebSocket 10개 동시 연결, 250ms 배치 전송

검증 기준은 [testing](testing.md)의 E2E AC/NF 표를 따른다.

---

<!-- source: docs/wiki/sot/runtime-config.md -->
# Runtime Config SOT — 환경변수·설정

# Runtime Config SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다.

PureIron 런타임 설정의 정본은 `backend/src/pureiron/config.py`의 `Settings(BaseSettings)` 클래스다.

- 환경변수 접두사: `PUREIRON_`
- 로컬 템플릿: `.env.example`
- 실제 로컬 실행 파일: `backend/.env`

`.env.example`은 운영자가 복사해서 쓰는 projection이다. `Settings` 필드가 추가되면 `.env.example`도 같이 갱신한다.

## 이 문서의 원천

- [backend/src/pureiron/config.py](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/config.py) — `Settings(BaseSettings)` 정본
- [.env.example](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/.env.example) — 복사용 환경변수 템플릿

## `Settings` 환경변수

아래 변수는 `Settings(BaseSettings)` 필드 기준이다. 기본값은 코드에서 확인한다. secret처럼 보이는 기본값은 이 문서에 복사하지 않는다.

| 변수 | 용도 |
|---|---|
| `PUREIRON_HOST` / `PUREIRON_PORT` | FastAPI 바인드 주소·포트 |
| `PUREIRON_DATABASE_URL` | PostgreSQL + TimescaleDB + pgvector 접속 |
| `PUREIRON_REDIS_URL` | Redis 대화 저장소와 캐시 |
| `PUREIRON_POC_READ_ONLY_MODE` | read-only PoC 안전 모드. true이면 운영자/Agent write-control 경로를 하드 차단 |
| `PUREIRON_WS_BATCH_INTERVAL_MS` | WebSocket 태그 업데이트 배치 주기 |
| `PUREIRON_LOG_LEVEL` | 애플리케이션 로그 레벨 |
| `PUREIRON_DIAGNOSTICS_DOCKER_PROXY_URL` | Admin Container Diagnostics가 사용하는 내부 read-only Docker socket proxy URL. 빈 값이면 기능 비활성화 |
| `PUREIRON_CORS_ORIGINS` | 허용 CORS origin 목록(JSON array) |
| `PUREIRON_JWT_SECRET_KEY` | JWT 서명 키(secret) |
| `PUREIRON_JWT_ALGORITHM` | JWT 서명 알고리즘 |
| `PUREIRON_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | access token 만료 시간(분) |
| `PUREIRON_JWT_REFRESH_TOKEN_EXPIRE_MINUTES` | refresh token 만료 시간(분) |
| `PUREIRON_AGENT_MODEL_PROVIDER` | Agent LLM provider(`auto`/`openrouter`/`openai`/`gemini`/`custom`) |
| `PUREIRON_AGENT_MODEL` | Agent LLM 모델명 |
| `PUREIRON_AGENT_TEMPERATURE` | Agent planner/worker 온도 |
| `PUREIRON_AGENT_SYNTHESIZER_TEMPERATURE` | Agent synthesizer 온도 |
| `PUREIRON_AGENT_MAX_TOKENS` | Agent OpenAI-compatible provider 응답 토큰 상한 |
| `PUREIRON_GEMINI_API_KEY` | Gemini API key(secret) |
| `PUREIRON_OPENROUTER_API_KEY` / `PUREIRON_OPENROUTER_BASE_URL` | OpenRouter-compatible provider 설정 |
| `PUREIRON_OPENAI_API_KEY` | OpenAI 모델 또는 RAG embedding 키(secret) |
| `PUREIRON_AGENT_API_KEY` / `PUREIRON_AGENT_BASE_URL` | custom OpenAI-compatible provider 설정 |
| `PUREIRON_LANGSMITH_API_KEY` | LangSmith tracing 키(secret, 선택) |
| `PUREIRON_LANGSMITH_PROJECT` | LangSmith project 이름 |
| `PUREIRON_LANGSMITH_TRACING` | LangSmith tracing on/off |
| `PUREIRON_SIMULATOR_DEVICE_NAMES` | DB 없이 Simulator로 취급할 device_name 목록 |

## 직접 읽는 환경변수

아래 변수는 `Settings` 필드가 아니라 런타임 코드에서 직접 `os.getenv()`로 읽는다. 따라서 `scripts/wiki.py check`의 env drift 비교 대상이 아니다.

| 변수 | 원천 코드 | 용도 |
|---|---|---|
| `PUREIRON_CONFIGS_DIR` | `gateway.py` | `configs/` 루트 위치 override |
| `PUREIRON_SEED_TAGS_FROM_JSON` | `gateway.py` | DB가 비어 있을 때 JSON tag 정의를 DB로 seed할지 결정 |

## 설정 파일 위치

`configs/` 디렉터리 계약은 [data-contracts](data-contracts.md)를 따른다.

`PUREIRON_CONFIGS_DIR` 미설정 시 런타임은 docker/local 경로를 자동 탐색한다. 로컬 fallback `configs/`는 Coffee 데이터가 없는 최소 root이고, Coffee demo는 `projects/demo-coffee/`를 명시적으로 선택해야 한다. 경로 결정 로직은 `gateway.py`가 정본이며 세부 package 경계는 [data-contracts](data-contracts.md#project-config-root-계약)를 따른다.

## Agent troubleshooting PoC 설정

알람 원인 후보·조치 제안을 반환하는 Agent troubleshooting 경로를 온프레미스 PoC에서 실제로 쓰려면 위 표의 Agent LLM 설정이 필요하다.

- provider 1개를 `PUREIRON_AGENT_MODEL_PROVIDER`로 고르고, 해당 provider의 key/base-url을 설정한다: `openrouter`→`PUREIRON_OPENROUTER_API_KEY`(+`PUREIRON_OPENROUTER_BASE_URL`), `openai`→`PUREIRON_OPENAI_API_KEY`, `gemini`→`PUREIRON_GEMINI_API_KEY`, `custom`→`PUREIRON_AGENT_API_KEY`(+`PUREIRON_AGENT_BASE_URL`).
- key가 없으면 troubleshooting 경로는 조용히 실패하지 않고 명확한 설정 오류(`... requires an API key`)를 던진다. 정본은 `agents/llm.py`.
- `PUREIRON_POC_READ_ONLY_MODE=true`에서도 troubleshooting **읽기** 경로(태그/알람/히스토리/디바이스 health 조회)는 동작한다. write/control 도구는 [agent-rules](agent-rules.md)의 read-only 규칙대로 하드 차단된다.
- CI/스모크 검증은 외부 provider 없이 결정론적 chat model seam으로 한다: `backend/tests/test_agent_troubleshooting_smoke.py`.

## drift 검사

`python scripts/wiki.py check`는 `config.py`의 `Settings` 필드와 `.env.example` 키를 비교한다.

- `.env.example`에 빠진 `Settings` 필드는 경고한다.
- `.env.example`에만 있는 직접 환경변수는 drift 대상이 아니다.
- secret처럼 보이는 비공란 기본값은 source warning으로만 표기한다.
- check를 통과시키기 위해 `config.py` 기본값을 임의로 바꾸지 않는다.
- secret/API key 값을 위키 본문에 복사하지 않는다.

## 목표 설계(미구현)

> **target design — 아직 구현되지 않았다.** 이 절은 Foundation 재설계(2026-10-04)로 PureIron_V1에 만들 구조다. 위 절들의 현행 사실(legacy 기준)과 섞어 읽지 않는다. 결정 근거는 [ADR](../adr/index.md) 019~028. 구현되면 해당 내용을 현행 절로 옮기고 이 절에서 지운다.

### 지원 호스트 (ADR-027)

- Ubuntu 24.04 x86_64 전용, 오프라인 설치: preflight(OS·`/usr/bin/python3.12`·`/var/lib/pureiron` 여유 ≥ 50 GiB·포트 80/443·시간 동기) → 번들의 docker-ce `.deb` → `/etc/docker/daemon.json` `{"features":{"containerd-snapshotter":false}}`(classic image store) → 이미지 로드 → systemd.
- 개발·PoC: 개발 PC(Windows 11 Pro) Hyper-V의 Ubuntu 24.04 VM(site-a·site-b·central).

### 호스트 경로

| 경로 | 내용 |
|---|---|
| `/var/lib/pureiron/db` | site-db 데이터 |
| `/var/lib/pureiron/state/` | `updater.json`(적용 단계 상태), `site.key`(0600, 현장 Ed25519 키), `secrets/<name>`(0600), `compose/g<gen>.yml`, `metrics/` |
| `/var/lib/pureiron/backups/g<gen>/` | 업데이트 전 백업(pg_dump −tag_history, 프로젝트 스냅샷, compose/manifest, secret 참조 목록) |
| `/var/lib/pureiron/bundles/` | 받은 `.pib` |
| `/opt/pureiron/updater/<ver>`, `current` | Updater venv(symlink 전환) |
| `/etc/pureiron/root.pub` | 신뢰 루트 공개키(raw 32바이트 Ed25519) |

### 실행 규칙

- compose 프로젝트명 `pureiron` 고정(`-p pureiron`), 모든 이미지 `pureiron/<svc>:<release>` + `pull_policy: never`, 로드 후 `.Id == config_digest` assert.
- 비밀값: `pureiron-site secret set <name>`으로 `state/secrets/`에 저장, 프로젝트는 `secret://<name>`만 참조. 중앙 토큰은 `control_api_token`.
- 현장 CLI(`pureiron-site`): `enroll`, `apply <pib>`, `bundle verify <pib>`, `status`, `secret set`, `report export`, `project export`.
- Redis 없음(ADR-008 개정). core `/readyz` = DB 연결 + 스키마 호환 범위 + collector 세션. 응답의 `acquisition`(plan_rev·장비 연결 수·태그 GOOD 수·`good_ratio`·ingest 누적 `live`/`stale`/`future`/`replay`)은 집계일 뿐 준비 판정에 쓰지 않으며, Updater `checking` 단계의 GOOD 비율 기준이 이 값을 쓴다(M3a). `historian`(버퍼 행 수·drop·쓰기 지연·`db_ok`·버린 샘플 수·site-db가 거부해 격리한 행 수)도 정보로만 낸다 — 이력 DB 쓰기가 막혀도 수집·현재값은 계속된다(M3a-3).
- 장비·태그 정의: `pureiron-core apply-definitions <프로젝트 디렉터리>`가 project-tools로 검증한 뒤 `devices.definition`·`tags.definition`(jsonb)에 path 기준 upsert·retire 하고, `project.yaml`의 `historian`(기록 기본값, 정규형·비면 NULL)은 `project_state.historian`에 같은 트랜잭션으로 저장한다. 바뀐 것이 있을 때만 `project_state.definitions_rev`(= plan_rev)를 올린다. core는 이 rev를 주기적으로 읽어 collector에 새 plan을 보낸다(정식 project apply는 M5).
- **dev/VM DB 주의(M3a-3 D12)**: D12로 epoch 3 내용이 0008 → 0009로 늘었다(release된 epoch는 2까지라 현장 영향 없음). 0008까지 적용된 dev/VM DB는 새 core를 올리기 전에 `pureiron-core migrate up`이 필수다 — `/readyz`·Updater는 epoch 번호만 보므로, 안 하면 `/readyz`는 ready인데 `project_state.historian` 열이 없어 정의 로드가 실패하고 plan이 적용되지 않아 수집 전체가 멈춘다.
- 관측: core·collector `/metrics`, Updater가 60초마다 `state/metrics/<svc>.prom`·일별 jsonl로 기록, `/var/lib/pureiron` 사용률 경고 ≥ 0.80·위험 ≥ 0.90.

### V1 환경변수 (M1~M3a 구현)

정본은 `services/core/src/pureiron_core/settings.py`, `services/collector/src/pureiron_collector/settings.py`, `deploy/compose/site.compose.yml`. 비밀값은 값이 아니라 **파일 경로**로만 받는다.

| 변수 | 서비스 | 기본값 | 의미 |
|---|---|---|---|
| `PUREIRON_DB_DSN` | core | `postgresql://pureiron@localhost:5432/pureiron` | site-db 접속(비밀번호 제외) |
| `PUREIRON_DB_PASSWORD_FILE` | core | 없음 | DB 비밀번호 파일(compose: `/run/secrets/db_password`) |
| `PUREIRON_HTTP_HOST` / `PUREIRON_HTTP_PORT` | core | `127.0.0.1` / `8000` | REST(이미지에서는 `0.0.0.0`) |
| `PUREIRON_GRPC_HOST` / `PUREIRON_GRPC_PORT` | core | `127.0.0.1` / `50051` | collector gRPC 서버 |
| `PUREIRON_RELEASE` | core·compose | `0.1.0-dev` | release 이름(`schema_migrations.release`, 이미지 태그) |
| `PUREIRON_MIGRATIONS_DIR` | core | 소스 트리 `services/core/migrations` | 이미지에서는 `/opt/pureiron/migrations` |
| `PUREIRON_UPDATER_TOKEN_FILE` | core | 없음 | 편집 잠금 API용 Updater 토큰 파일. 없으면 잠금 API는 503(기본 거부) |
| `PUREIRON_PLAN_REFRESH_S` | core | `2.0` | 정의 rev(= plan_rev) 확인 주기. 바뀌면 열린 collector 세션에 새 plan |
| `PUREIRON_INGEST_LIVE_MAX_AGE_S` | core | `5.0` | 샘플 시각이 이보다 오래된 live 샘플은 이력에만(현재값·알람 갱신 없음) |
| `PUREIRON_DEVICE_SILENCE_MIN_S` | core | `5.0` | 장비 무신호 판정 `max(3×poll_ms, 이 값)` → 장비 DISCONNECTED·태그 BAD |
| `PUREIRON_SESSION_SILENCE_S` | core | `15.0` | collector가 이 시간 동안 아무것도 보내지 않으면 세션을 닫음(RST 없는 단절 대비). collector `PUREIRON_COLLECTOR_HEARTBEAT_S`의 2배 이상 |
| `PUREIRON_HISTORIAN_BUFFER_MAX_ROWS` | core | `600000` | 이력 메모리 버퍼 상한(행). 넘치면 가장 오래된 행부터 버리고 `/readyz.historian.dropped_total`에 센다. DB 장애 동안 이 버퍼가 이력을 든다. 쓰는 중인 행(최대 `FLUSH_MAX_ROWS`)은 상한 밖이라 순간 최대는 이 값 + `FLUSH_MAX_ROWS`(기본 620,000행) |
| `PUREIRON_HISTORIAN_FLUSH_S` | core | `1.0` | 이력 flush 주기(초). 밀려 있으면 같은 주기에 버퍼가 빌 때까지 계속 쓴다 |
| `PUREIRON_HISTORIAN_FLUSH_MAX_ROWS` | core | `20000` | `tag_history` INSERT 한 번에 쓰는 최대 행 수 |
| `PUREIRON_AUTH_SESSION_IDLE_S` | core | `43200`(12 h) | 세션 유휴 만료 — 마지막 사용(`sessions.last_seen_at`) 뒤 이 시간이 지나면 401(M3b-1). `last_seen_at` 갱신 간격 = `min(60 s, IDLE_S / 2)` — 짧은 값(예: 60)에서도 그보다 자주 쓰는 세션은 유지된다 |
| `PUREIRON_AUTH_SESSION_ABSOLUTE_S` | core | `604800`(7 d) | 세션 절대 만료(로그인 기준, 사용 중이어도 끝남). 세션 쿠키 `Max-Age`도 이 값 |
| `PUREIRON_AUTH_COOKIE_SECURE` | core | `false` | 세션 쿠키 `Secure` 속성(`true/false`, `1/0`, `yes/no`, `on/off`). 현장 web은 http라 기본 false — true면 브라우저가 http로는 쿠키를 보내지 않는다 |
| `PUREIRON_REALTIME_BATCH_MS` | core | `250` | WS `tag_update` 합치기 주기(ms, 양의 정수). 구독한 태그의 변화는 이 주기마다 태그별 최신값만 보낸다(M3b-2). 알람·장비 이벤트는 주기와 무관하게 즉시 |
| `PUREIRON_CORE_GRPC` | collector | `127.0.0.1:50051` | core gRPC 주소(compose: `core:50051`) |
| `PUREIRON_COLLECTOR_HEARTBEAT_S` | collector | `1.0` | 빈 batch heartbeat 주기 |
| `PUREIRON_COLLECTOR_RECONNECT_INITIAL_S` / `_MAX_S` | collector | `0.5` / `5` | 재연결 backoff. 상한은 링 보관 창(60 s)보다 훨씬 작아야 한다 — 0.5 s부터 두 배씩 30 s까지 늘면 35 s 단절 뒤 다음 시도가 ~66 s에 와서 분리 구간 앞부분이 링 창 밖으로 밀려 버려진다(M3a-3). LAN 재접속 비용은 작다 |
| `PUREIRON_SIM_REGISTERS` | compose(dev) | `100` | 시뮬레이터 holding 레지스터 수(`SIM_REGISTERS`). 부하 측정은 2000 |
| `PUREIRON_SECRETS_DIR` | compose | `/var/lib/pureiron/state/secrets` | `db_password`·`updater_token` 파일 위치 |
| `PUREIRON_DB_DATA` | compose | `/var/lib/pureiron/db` | site-db 데이터(개발·CI는 named volume `pureiron-db`) |
| `PUREIRON_WEB_BIND` / `PUREIRON_WEB_PORT` | compose | `0.0.0.0` / `80` | web 노출 |
| `PUREIRON_SITE_DB_VERSION` | compose | `deploy/releases/site-db.lock`의 version | site-db 이미지 태그 |

core의 초 단위 값(`…_S`)은 양의 유한 수만 받는다 — 0·음수·`nan`·`inf`는 기동 시 `SettingsError`로 실패한다(M3b-0). compose core는 `stop_grace_period: 25s`다(종료 예산은 [data-contracts](data-contracts.md) M3a-3 historian 현황).

---

<!-- source: docs/wiki/sot/testing.md -->
# Testing SOT — 검증 계층·명령·게이트

# Testing SOT

> **legacy(PureIron_ignition) 기준** — 이 문서의 현행 구현 사실(코드 경로·모듈·명령·테스트)은 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 기준이다. 이 repo(PureIron_V1)에는 legacy 코드가 없고, 새 구조의 목표 설계는 [ADR](../adr/index.md)에서 다룬다. V1 새 코드의 작성 규약은 [coding-style](coding-style.md) 9절이 우선한다.

이 문서는 PureIron의 **검증 계층, 실행 명령, 변경 유형별 최소 게이트**에 대한 정본이다. 개별 테스트의 상세 assertion은 `backend/tests/**`, `backend/tests/e2e/**`, `tests/e2e/**`가 정본이다.

목표는 “모든 것을 항상 오래 돌린다”가 아니다. 2인 팀이 감당 가능한 속도를 유지하면서도, 고객 설비 안정성·데이터 무결성·운영 UI 회귀를 막는 검증 게이트를 고정하는 것이다.

## 1. 문서 경계

| 문서/경로 | 역할 |
|---|---|
| `docs/wiki/sot/testing.md` | 검증 전략과 실행 기준의 정본 |
| `backend/tests/**` | backend unit/integration/API/도메인 테스트 |
| `backend/tests/e2e/test_phase2_acceptance.py` | Phase 2 acceptance: chat/agent/SSE/approval/document/model/3D 관련 AC-11~AC-17 |
| `backend/tests/e2e/test_demo_acceptance.py` | Demo acceptance path: Simulator→3D Twin→Agent→Work Order 데모 spine의 자동화 가능 부분 AC-18 |
| `tests/e2e/test_acceptance.py` | legacy/in-process E2E acceptance: AC-1~AC-10 |
| `tests/e2e/test_nonfunctional.py` | legacy/in-process non-functional checks: NF-1~NF-4 |
| `frontend/package.json` | frontend 검증 명령 정본 |
| `frontend/playwright.config.ts` | 모바일 브라우저 E2E 실행 설정 정본 |
| `frontend/e2e/**` | Playwright browser E2E 테스트 |
| `scripts/wiki.py` | Wiki/SOT build/check 정본 |
| `.github/workflows/ci.yml` | 필수 PR smoke/focused gate 정본 |
| `.github/workflows/wiki.yml` | Wiki structure/drift gate 정본 |
| `.github/workflows/db-poc-smoke.yml` | Timescale/Postgres 포함 DB-present PoC smoke gate 정본 |

이 문서는 테스트 코드를 복붙하지 않는다. 어떤 변경에 어떤 검증을 돌릴지 결정하는 기준만 고정한다.

## 2. 기본 실행 명령

Repo root 기준:

```bash
# Wiki / SOT
python scripts/wiki.py build
python scripts/wiki.py check

# Backend 전체 테스트
cd backend && uv run pytest tests/ -v

# Backend 특정 테스트
cd backend && uv run pytest tests/test_auth.py -v
cd backend && uv run pytest tests/test_devices.py -v
cd backend && uv run pytest tests/test_project_package.py -v
cd backend && uv run pytest tests/e2e/test_phase2_acceptance.py -v
cd backend && uv run pytest tests/e2e/test_demo_acceptance.py -v

# Legacy E2E acceptance / non-functional
cd backend && uv run pytest ../tests/e2e/ -v

# Frontend
cd frontend && npm run lint   # tsc --noEmit
cd frontend && npm run build
cd frontend && npm run e2e:browser   # desktop + mobile Chromium
```

주의:

- backend는 Python `>=3.12` 프로젝트다. 명령은 `backend/`에서 `uv run ...`로 실행한다.
- frontend `npm run lint`는 ESLint가 아니라 `tsc --noEmit`이다.
- SOT 문서를 수정하면 repo root에서 `python scripts/wiki.py build && python scripts/wiki.py check`를 반드시 실행한다.
- generated 문서(`docs/wiki/_generated/**`, `docs/LLM_WIKI.md`)는 직접 수정하지 않고 build 결과로 갱신한다.

## 3. 테스트 계층

| Layer | Scope | Main command | 언제 실행 |
|---|---|---|---|
| Wiki check | SOT frontmatter, sources, drift, generated context | `python scripts/wiki.py build && python scripts/wiki.py check` | SOT/projection 변경 시 항상 |
| Backend focused test | 변경 모듈 주변 단위/통합 테스트 | `cd backend && uv run pytest tests/<file>.py -v` | backend 코드 변경 시 최소 1개 이상 |
| Backend full test | backend 전체 regression | `cd backend && uv run pytest tests/ -v` | gateway/tag/db/auth/agent/event 등 핵심 경계 변경 시 |
| Backend Phase 2 acceptance | chat/agent/SSE/approval/document/model/3D acceptance | `cd backend && uv run pytest tests/e2e/test_phase2_acceptance.py -v` | Phase 2 API 또는 agent/chat/model 경계 변경 시 |
| Backend demo acceptance | Simulator→3D Twin→Agent→Work Order 데모 spine 자동화 부분 | `cd backend && uv run pytest tests/e2e/test_demo_acceptance.py -v` | simulator/approval/agent tool/3D binding 등 데모 핵심 경계 변경 시 |
| Alarm ack/note/journal contract | 알람 ack, 조치 note, journal 조회와 frontend action-note UI/API 문자열 계약 | `cd backend && uv run pytest tests/test_alarm_ack_note_history_contract.py tests/test_alarm_summary_ui_contract.py -v` | alarm engine/journal/router 또는 `AlarmSummary` 조치 note/History 화면 변경 시 |
| Operational event store | DB-backed `operational_events` record/query, recorder graceful degradation, `/api/operational-events` 라우터 | `cd backend && uv run pytest tests/test_operational_events.py -v` | `operational/**` 또는 5xx 예외 핸들러·RBAC deny 기록 경로 변경 시. 실제 DB row는 `tests/test_db_poc_smoke.py`(DB-present) |
| Backend log paging / healthcheck noise | `/api/logs/{date}/page` 실제 전체 건수·`before_id` snapshot + offset 페이징, `/api/logs/{date}` 하위 호환, `/api/health` access 로그 daily log 제외 | `cd backend && uv run pytest tests/test_logs_paging.py -v` | `logs/reader.py`·`logs/router.py`·`logging_config.py` 변경 시 |
| Container Diagnostics security / parsing | Admin RBAC, 고정 service allowlist, Docker multiplex stream, credential redaction, 기간·level·text/noise 필터, export, unavailable/replacement, Compose rotation | `cd backend && uv run pytest tests/test_diagnostics.py -v` | `diagnostics/**`, `/logs` Container Diagnostics 또는 Compose logging 변경 시 |
| Legacy E2E acceptance | AC-1~AC-10 in-process FastAPI acceptance | `cd backend && uv run pytest ../tests/e2e/test_acceptance.py -v` | auth/tag/ws/alarm/history/expression/script/RBAC 변경 시 |
| Legacy non-functional | WS latency, throughput, concurrency, reconnect | `cd backend && uv run pytest ../tests/e2e/test_nonfunctional.py -v` | WebSocket/tag update/event bus 성능·동시성 변경 시 |
| Frontend type check | TS type/API/component contract | `cd frontend && npm run lint` | frontend 변경 시 항상 |
| Frontend build | bundling/import/route/UI composition | `cd frontend && npm run build` | route/import/component composition 변경 시 |
| Browser E2E | Chromium desktop+mobile viewport에서 operator-facing alarm ack/note 흐름 | `cd frontend && npm run e2e:browser` | 알람/데모 핵심 UI, layout, route/auth guard 변경 시 |

CI 기준:

- `.github/workflows/ci.yml`은 PR merge 판단에 쓰는 **필수 blocking gate**다.
  현재 baseline 전체 backend lint/test에는 아직 기술부채가 있으므로, backend ruff는 변경된
  `backend/**/*.py` 파일만 검사한다. 대신 backend pytest는 항상 smoke spine
  (`test_agent_llm.py`, `test_approve_execution.py`)을 돌리고, 변경 파일 경로에 따라 auth/logs/
  dashboard/settings/alarm/historian/tag/agent/chat/driver 관련 focused test를 추가 선택한다.
- frontend PR gate는 `npm run lint`(`tsc --noEmit`), `npm run build`, `npm run e2e:browser`를 blocking으로 실행한다.
  TypeScript 타입만 맞아도 bundling/import/route composition이나 desktop/mobile viewport의 실제 조작 흐름이 깨질 수 있으므로
  build와 browser E2E를 별도 gate로 둔다.
- `.github/workflows/wiki.yml`은 wiki structure/drift만 blocking으로 검사한다.
  문서 수정자는 로컬에서 `python scripts/wiki.py build && python scripts/wiki.py check`를 먼저 실행하고
  generated artifacts를 커밋해야 한다.
- `.github/workflows/db-poc-smoke.yml`은 DB/historian/alarm/tag/gateway/DB smoke 테스트 변경 PR 또는
  수동 실행(`workflow_dispatch`)에서 Timescale service를 띄우고 `tests/test_db_poc_smoke.py`를 실행한다.
  이것은 고객 현장 검증이 아니라 pre-field DB-present gate다.
- backend 전체 ruff와 전체 `pytest tests/`는 아직 blocking gate가 아니다. 현재 baseline은 full ruff
  다수 위반과 full pytest 환경 의존 실패(OPC-UA 포트, DB 없는 JSON fallback, LLM provider 없는 agent graph 등)를
  포함한다. 이 부채는 별도 정리 이슈로 줄이고, 각 PR은 변경 영역 focused test와 위 blocking gate로 방어한다.


### 오류 로깅 점검 게이트

기능 점검 중 재현한 API/runtime 오류는 운영자가 추적 가능한 로그·audit·trace에 남는지 확인하고, PR/이슈 결과에 확인 경로를 기록한다. 단순히 “요청이 실패했다”가 아니라 “실패가 나중에 추적 가능한 형태로 남았다”까지가 점검 완료 기준이다.

파일 기반 운영 로그(`backend/logs/YYYY-MM-DD.log`) + audit(`audit.jsonl`) + agent trace(`backend/traces/`)가 **항상 켜져 있는 baseline**이다. 이슈 #124부터는 그 위에 **DB-backed `operational_events` 저장소**가 추가되어, 주요 오류/거부 이벤트가 DB row로도 남는다. DB 미연결 시에는 recorder가 위 파일 로그 경로로 graceful degradation 하므로 baseline은 그대로 유지된다. 따라서 PR에서는 아래 표의 실제 저장 위치를 명시하되, DB 연결이 확인되지 않았으면 “DB row로 저장됨”이라고 단정하지 않는다.

| 오류/거부 유형 | 현재 저장·조회 경로 | 점검 기준 | 현재 판단 |
|---|---|---|---|
| API validation 4xx | FastAPI 응답. 라우터가 별도 audit을 남기는 write 거부만 `AuditLogger`/`record_audit_safe` 대상 | 의도된 4xx인지 focused test로 고정하고, 운영 추적이 필요한 write 거부면 audit action을 확인 | 부분 충족 |
| API/server 5xx 또는 repository 예외 | `logging_config._DailyJsonHandler`가 `pureiron.*` logger를 `backend/logs/YYYY-MM-DD.log` JSONL로 저장하고 `/api/logs/{date}`로 조회. **추가로** `main.py`의 `Exception` 핸들러가 미처리 5xx를 `operational_events`(category=`api_5xx`) DB row로 기록하고 `/api/operational-events`로 조회 | 500으로 매핑되는 예외는 logger.exception/error 또는 테스트에서 error path를 확인. DB 연결 시 row 저장, 미연결 시 daily log degrade | 충족 |
| RBAC deny / read-only write block | FastAPI auth/role dependency 응답 + write 성공/거부 경로의 `record_audit_safe`. **추가로** `require_role` 거부가 `operational_events`(category=`rbac_deny`) DB row로 기록 | 권한 거부 자체와, write intent가 있는 경로의 audit/operational_events 기록 여부를 함께 확인 | 충족 |
| runtime background task exception | 각 driver/gateway/historian/task logger의 exception/error가 daily JSON log로 저장. 필요 시 `record_operational_event_safe(category=”background_task”)`로 DB row 기록 가능 | background loop 예외가 swallowed 되지 않고 `pureiron.*` logger로 남는지 확인 | 부분 충족 |
| Agent tool 실행 오류 | LangGraph 실행 시 `TraceLogger`가 `backend/traces/YYYY-MM-DD/*.json`에 tool_calls/error/final_response를 저장. 관련 logger는 daily JSON log로 저장. 필요 시 `record_operational_event_safe(category=”agent_tool_error”)`로 DB row 기록 가능 | LLM 없는 테스트는 tool 함수 직접 호출로 error envelope를 고정하고, 실제 agent smoke는 trace 파일 생성 여부를 확인 | 부분 충족 |

**DB-backed store 경계 (issue #124):**

- `/api/logs/{date}` — 파일 기반 daily JSON 로그 전체(모든 level, 프로세스 전역). DB 없이도 항상 조회 가능한 baseline. 마지막 N건 bare list 계약을 유지하며, 실제 전체 건수와 페이징이 필요하면 `/api/logs/{date}/page`(newest-first, `total`/`offset`)를 쓴다. Compose healthcheck의 `/api/health` access 로그는 daily log에서 제외된다(issue #197).
- `/api/operational-events` — DB-backed `operational_events` 저장소. 큐레이션된 주요 오류/거부 이벤트(`api_5xx`, `rbac_deny` 등)만 row로 저장하며 category/severity/actor/기간으로 필터한다. DB 미연결 시 503(같은 이벤트는 graceful degradation으로 daily log에 남음). Engineer+ 권한.
- 저장소 정본은 `backend/src/pureiron/operational/`(migration `029_create_operational_events.sql` + repository + recorder + router). recorder는 `record_operational_event_safe`로 어느 호출부에서든 재사용 가능하고 **절대 예외를 올리지 않는다**.

남은 확장 여지: background task 예외와 agent tool 오류는 위 recorder를 재사용해 DB row로 승격할 수 있으나, 현재는 파일 로그/trace가 baseline이고 5xx·RBAC deny만 DB 저장을 직접 배선했다.

PR 완료 보고에는 다음 중 해당 항목을 최소 1개 이상 적는다.

```text
오류 로깅 확인:
- API 4xx/5xx: <테스트명 또는 /api/logs 확인 경로>
- RBAC/write deny audit: <audit action 또는 테스트명>
- runtime exception: <logger/로그 파일/테스트명>
- Agent tool error: <trace 파일 또는 tool error-envelope 테스트명>
- DB-backed log 미확인/미구현이면: 현재 파일 기반이며 후속 이슈 <번호>로 분리
```

## 4. 변경 유형별 최소 게이트

| 변경 유형 | 반드시 실행 | 추가로 실행할 것 |
|---|---|---|
| SOT/wiki 문서만 변경 | `python scripts/wiki.py build && python scripts/wiki.py check` | 없음 |
| Core domain model boundary(SoT) 변경 | `cd backend && uv run pytest tests/test_core_domain_boundary_contract.py -v` | [architecture](architecture.md#core-domain-model-boundary-sot) 경계 문구·금지 패턴 고정, DB/LLM 불필요 |
| Root projection(`AGENTS.md`, `CLAUDE.md`, `README.md`) 변경 | `python scripts/wiki.py build && python scripts/wiki.py check` | projection이 명령/규칙을 바꾸면 관련 테스트 smoke |
| Backend router/API 변경 | 관련 `backend/tests/test_*.py` | contract 영향 시 `api-events.md` 확인, 필요 시 E2E. 재현한 4xx/5xx의 로그/audit/trace 확인 경로를 PR에 기록 |
| Alarm ack/note/journal 또는 알람 화면 변경 | `cd backend && uv run pytest tests/test_alarm_ack_note_history_contract.py tests/test_alarm_summary_ui_contract.py -v`; `cd frontend && npm run lint && npm run build && npm run e2e:browser` | PoC 전 브라우저 수동 smoke는 [PoC browser smoke runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/poc-browser-smoke.md)으로 증거를 남김 |
| read-only PoC-safe 모드(`PUREIRON_POC_READ_ONLY_MODE`)/write-control 경로 변경 | `cd backend && uv run pytest tests/test_poc_read_only_mode.py tests/test_agent_troubleshooting_smoke.py -v` | `/api/health`·[runtime-config](runtime-config.md) env 계약 확인, 내부 수집·읽기 경로가 유지되는지 |
| Gateway/lifecycle 변경 | `cd backend && uv run pytest tests/ -v` | graceful degradation 관련 테스트 추가 |
| TagStore/tag write/event bus 변경 | 관련 tag/alarm/ws 테스트 | `../tests/e2e/test_acceptance.py`, 필요 시 `test_nonfunctional.py` |
| DB migration/repository 변경 | repository/migration 관련 테스트 | DB 필요 테스트는 환경변수 준비 후 실행, migration 멱등성 확인 |
| Operational event store(`operational/**`)·5xx/RBAC deny 기록 경로 변경 | `cd backend && uv run pytest tests/test_operational_events.py -v` | DB 연결 시 row 저장·미연결 시 daily log degrade 확인, `/api/logs`와의 경계 유지. 실제 DB row는 `tests/test_db_poc_smoke.py` |
| DB-present PoC 수집/영속 경로 변경 | `cd backend && uv run pytest tests/test_db_poc_smoke.py -v` | `PUREIRON_DATABASE_URL` 없으면 skip. 실제 고객 DB+다중 드라이버 현장 검증은 PoC 실행 시 별도 증거 확보 |
| Driver/device health 변경 | driver/device health 테스트 | Device health가 tag로 새지 않는지 `/api/devices` 계약 확인 |
| Agent/chat/SSE/approval 변경 | 관련 chat/agent/approval 테스트 | `backend/tests/e2e/test_phase2_acceptance.py`. Agent tool 오류는 error envelope 또는 trace 저장 여부 확인 |
| Simulator/데모 spine 변경 | `backend/tests/e2e/test_demo_acceptance.py` (AC-18) | 시각/Work Order 영향 시 AC-18 수동 체크리스트 확인 |
| Frontend type/API/hook 변경 | `cd frontend && npm run lint` | backend contract 변경 시 관련 backend 테스트 |
| Frontend route/import/UI composition 변경 | `cd frontend && npm run lint && npm run build` | 주요 화면이면 수동 smoke 또는 screenshot 확인 |
| Full-stack contract 변경 | backend focused + frontend lint/build | `api-events.md`, `frontend.md`, 관련 E2E |

## 5. Acceptance Criteria 현재 범위

### AC-1~AC-10 (`tests/e2e/test_acceptance.py`)

| ID | 기준 |
|---|---|
| AC-1 | 로그인 시 JWT access token + refresh token 반환 |
| AC-2 | 토큰 없이 보호 엔드포인트 접근 시 401 |
| AC-3 | 태그 CRUD: 생성·조회·값 쓰기·삭제 |
| AC-4 | WebSocket 구독 시 태그 업데이트 전달 |
| AC-5 | 설정점 초과 시 알람 발생 |
| AC-6 | 알람 ack 시 `Active/Unacked` → `Active/Acked` 전환 |
| AC-7 | historian endpoint 사용 가능. DB 없으면 503 허용 |
| AC-8 | expression engine 산술·논리·내장 함수 평가 |
| AC-9 | script engine 허용 import 실행, `os`/`subprocess` 차단 |
| AC-10 | write endpoint RBAC/auth 요구사항 확인 |

### AC-11~AC-17 (`backend/tests/e2e/test_phase2_acceptance.py`)

| ID | 기준 |
|---|---|
| AC-11 | Chat conversation 생성·목록·삭제와 인증 요구사항 |
| AC-12 | Agent graph/tool routing 구조 검증. 실제 LLM 호출 없이 확인 |
| AC-13 | Chat SSE stream이 event-stream과 `done` event를 제공 |
| AC-14 | Tag write approval gate 동작 |
| AC-15 | Document upload/list/delete. RAG는 DB+OpenAI 환경에 의존 |
| AC-16 | 3D model upload와 config round-trip |
| AC-17 | 3D tag binding 저장·조회 |

### AC-18 Demo Acceptance Path (`backend/tests/e2e/test_demo_acceptance.py` + 수동 체크리스트)

AC-11~AC-17은 기능별 검증이다. AC-18은 그 기능들을 영업 데모의 **한 흐름**으로 묶는다: Simulator → 3D Twin → Agent → Work Order. 목적은 개별 API가 아니라 "고객이 5분 안에 이해하는 경로"가 깨지면 CI/문서에서 감지하는 것이다. 제품 가치 정의는 [product](product.md) v1 핵심 가치를 따른다.

데모 내러티브(7단계):

1. Simulator가 정상 운전 상태를 만든다.
2. 특정 이상/운전 시나리오를 주입한다.
3. tag/alarm/history가 변화한다.
4. 3D Twin이 관련 설비/tag를 highlight한다.
5. Agent가 이상 원인 후보와 조치 시나리오를 설명한다.
6. 위험 action은 pending approval로 멈춘다.
7. 사람이 알람 ack·조치 note·이력을 기존 작업 지시/CMMS 프로세스로 넘기는 **수동 Work Order handoff**로 이어진다(backend Work Order 기능 아님).

이 경로는 **자동화 가능한 backend spine**과 **수동 데모 체크리스트**로 분리한다. 시각 렌더링, Agent 답변의 자연어 품질, Work Order 수동 handoff는 in-process로 자동 검증하지 않는다(Work Order는 backend 기능이 아니므로 자동화할 대상 자체가 없다).

#### AC-18 자동화(backend in-process, DB/Redis/LLM 불필요)

| ID | 데모 단계 | 자동 검증 기준 | 재사용하는 기존 계약 |
|---|---|---|---|
| AC-18a | 1~3 정상→시나리오 주입→tag 변화 | `CoffeeTankProcessSimulator`가 valve open + pump run 주입 시 tank level·pump output을 **coupled**하게 변화시킨다(랜덤 노이즈 아님) | Simulator 상태 머신(`simulation.py`) |
| AC-18b | 6 위험 action 차단 | `write_tag(require_approval=True)`가 success가 아니라 approval envelope를 반환하고 tag 값은 불변 | AC-14 approval gate |
| AC-18c | 5 Agent 설명 도구 | agent troubleshooting toolset(`read_tag`/`get_active_alarms`/`query_history`)이 구성된다. LLM 호출·graph compile 없음 | AC-12 tool routing |
| AC-18d | 4 3D Twin highlight 데이터 계약 | Agent가 tag를 인용할 때 3D Twin이 듣는 `tag_reference` SSE event가 well-formed | AC-13 SSE / AC-17 tag binding |

tag/alarm/history 변화 자체의 상세 검증은 AC-5·AC-6·AC-7로 이미 커버되므로 AC-18에서 중복하지 않고 위 spine의 연결고리만 고정한다.

#### AC-18 수동 데모 체크리스트(자동화 대상 아님)

데모 전 또는 릴리스 PoC 전에 사람이 확인한다. v1에서 자동화하지 않는 이유를 함께 적는다. 고객-site read-only PoC 리뷰 전에는 [PoC browser smoke runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/poc-browser-smoke.md)을 실행해 sanitized screenshots/CSV/notes 증거를 남긴다.

- [ ] **3D Twin 시각 highlight** — `tag_reference`가 온 설비/노드가 실제 3D 화면에서 색/라벨로 강조되는지 (렌더링은 in-process 검증 불가).
- [ ] **Agent 설명 품질** — 실제 provider/key가 있을 때 Agent가 이상 원인 후보와 조치 시나리오를 설득력 있게 설명하는지 확인한다. 자동화하지 않는 이유는 LLM 키와 자연어 품질이 결정적 assertion 대상이 아니기 때문이다. 실행 절차·질문 세트·pass/fail rubric·증거 sanitization은 [agent troubleshooting answer quality runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/agent-troubleshooting-answer-quality.md)을 따른다.
- [ ] **Agent graph 전체 compile** — 실제 LLM provider 키가 있을 때 graph가 supervisor 포함해 정상 compile되는지 (키 없는 CI에서는 미검증, 현재 baseline 부채).
- [ ] **Work Order 수동 handoff** — 조치 후보에서 알람 ack·조치 note·이력 위로 사람이 기존 작업 지시/CMMS 프로세스로 넘기는 흐름. **Work Order는 현재 backend feature가 아니라 데모/수동 handoff**이며, v1은 사람 판단·수동 handoff까지만 다룬다(Work Order DB/API/router 없음, [product](product.md) MVP 제외: 사람 승인 없는 자동 조치 없음). 반복 가능한 데모 스크립트·검증 체크리스트는 [work-order-demo-handoff 런북](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/work-order-demo-handoff.md)을 따른다. 자동화는 backend Work Order API가 생긴 뒤로 미룬다.
- [x] **모바일 알람 확인 UI 경로** — `AlarmSummary.tsx`가 모바일 전용 ack/note card와 history action context를 제공하고, `test_alarm_summary_ui_contract.py`가 unsupported delete affordance 제거까지 고정한다. 실제 브라우저/현장 스크린샷 증거는 PoC 실행 시점에 남긴다.
- [ ] **알람 ack·조치 note·이력 브라우저 smoke** — active alarm before/after ack, action note 저장, history/journal row, read-only block 증거를 [PoC browser smoke runbook](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/poc-browser-smoke.md)에 따라 남긴다.

수동 항목이 자동화 가능해지면(예: Work Order API 추가) 해당 항목을 AC-18 자동화 표로 승격한다.

## 6. Non-functional 현재 범위

`tests/e2e/test_nonfunctional.py` 기준:

| ID | 기준 |
|---|---|
| NF-1 | tag write → WebSocket push p95 latency < 500ms |
| NF-2 | 100회 연속 tag write가 5xx 없이 완료 |
| NF-3 | 동시 WebSocket client 10개가 update를 수신. 테스트 환경 timing edge 때문에 1개 miss까지 허용 |
| NF-4 | reconnect 후 subscribe 시 current tag value 전달 |

이 NF 테스트는 실제 공장 부하 테스트가 아니라 in-process regression gate다. 고객 데모/현장 PoC 전에는 별도 장시간 soak test와 실제 driver/simulator 기반 테스트가 필요하다.

## 7. 외부 의존성·환경 기준

기본 원칙:

- 단위/통합 테스트는 가능한 한 외부 DB/Redis/LLM 없이 통과해야 한다.
- 선택 서비스(DB, Redis, LangGraph, RAG, OpenAI 등) 실패는 graceful degradation으로 처리되어야 한다.
- 테스트가 외부 서비스를 요구하면 환경변수 조건을 명시하고 skip/503 허용 정책을 둔다.

현재 예:

- `AC-7` historian은 DB가 없으면 503을 허용한다.
- `AC-15` RAG/document 계열은 DB+OpenAI 환경에 따라 skip 또는 제한 검증이 될 수 있다.
- repository legacy adoption 계열은 PostgreSQL 사용 가능 여부에 따라 skip된다.

문서 경고를 없애기 위해 secret/API key 기본값을 코드나 `.env.example`에 임의로 채우지 않는다.

## 8. 테스트 작성 규칙

Backend:

- 새 backend 기능은 최소 1개 이상의 focused pytest를 추가한다.
- FastAPI 테스트는 가능한 한 ASGITransport/TestClient 기반 in-process test로 시작한다.
- production code에서 `request.app.state` 접근은 금지한다. 새 테스트도 가능하면 `from pureiron.gateway import gateway` 경계를 사용한다.
- async test는 `pytest-asyncio`의 `asyncio_mode = auto`를 따른다.
- 파일/사용자/설정 상태를 건드리는 테스트는 `tmp_path`, `monkeypatch`, 임시 `PUREIRON_CONFIGS_DIR`로 격리한다.
- DB schema 변경은 migration으로만 한다. 테스트 통과를 위해 앱 코드에서 `ALTER TABLE`을 넣지 않는다.
- TagStore lock discipline을 깨는 코드는 테스트가 통과해도 허용하지 않는다. DB I/O와 `tag.changed.*` 발행은 lock 밖이어야 한다.

Frontend:

- TypeScript 오류를 `as any`, `@ts-ignore`, `@ts-expect-error`로 숨기지 않는다.
- API/SSE payload는 `unknown`에서 type guard 또는 discriminated union으로 좁힌다.
- frontend contract 변경은 backend API와 `docs/wiki/sot/api-events.md`를 같이 확인한다.
- route/import/component composition 변경은 `npm run build`까지 돌린다.

E2E/NF:

- acceptance test는 고객에게 설명 가능한 기능 기준이어야 한다.
- non-functional test는 숫자를 과장하지 않는다. in-process 수치는 현장 성능 보장이 아니라 regression 신호다.
- timing 기반 테스트는 환경 흔들림을 고려하되, 실제 장애를 가리는 과도한 완화는 금지한다.

## 9. 실패 처리 원칙

테스트가 실패하면 다음 순서로 판단한다.

1. 실패가 실제 기능 회귀인지 확인한다.
2. 테스트가 최신 계약을 반영하지 못한 stale test인지 확인한다.
3. 환경 의존성(DB/Redis/LLM/port/path) 문제인지 확인한다.
4. 기능 회귀면 코드를 고친다.
5. 계약 변경이면 SOT/API 문서와 테스트를 함께 고친다.
6. 환경 문제면 skip 조건, fixture, 문서 명령을 정리한다.

금지:

- 실패를 숨기기 위한 assertion 완화.
- 테스트 통과만을 위한 production invariant 약화.
- 임의 sleep 증가로 race를 덮기.
- `No app.state`, `No ORM`, lock discipline, Device health 계약을 깨면서 테스트만 맞추기.
- 실행하지 않은 테스트를 실행했다고 보고하기.

## 10. PR/작업 완료 보고 형식

완료 보고에는 최소한 다음을 포함한다.

- 변경 파일 요약
- 실행한 명령
- 실제 결과: pass/fail, exit code, 주요 경고
- 실행하지 못한 검증과 이유
- 남은 risk 또는 후속 테스트

예:

```text
검증:
- python scripts/wiki.py build && python scripts/wiki.py check → pass
- cd backend && uv run pytest tests/test_devices.py -v → pass
- cd frontend && npm run lint → not run, frontend 미변경
```

## 11. V1 G1 게이트 (PureIron_V1, M1 구현)

> 이 절은 **PureIron_V1 현행**이다(위 1~10절은 legacy 기준). 결정 근거는 [ADR-028](../adr/index.md#adr-028-3단-검증-게이트--generated--authored--runner-격리).

로컬과 CI가 같은 러너를 쓴다: `bash scripts/g1.sh <job> [<job> ...]`(`all` = 통합·조건부 제외 전부). CI는 `.github/workflows/foundation-ci.yml`이 self-hosted runner `g1`(개발 PC WSL2 Ubuntu 24.04, 사용자 systemd 서비스 `pureiron-g1-runner`)에서 job마다 같은 이름으로 호출한다(PD-23: hosted runner 미사용).

| job | 내용 | 실행 조건 |
|---|---|---|
| `lint-type` | `uv sync --locked`, ruff check·format, pyright strict(패키지 6 + `packages/contracts` + `deploy`), 억제 주석·`app.state` grep 0건 | 항상 |
| `import-linter` | core 계약 3종 + 음성 fixture(`services/core/tests/test_importlinter_negative.py`) | 항상 |
| `unit` | `pytest services packages deploy`(통합 제외) | 항상 |
| `contracts-diff` | `buf lint`·`buf breaking`(FILE), `oasdiff breaking --fail-on ERR`, `schema_diff.py` — base에 파일이 없으면 notice 후 통과 | 항상 |
| `contracts-drift` | OpenAPI export·proto 생성·`schema.d.ts`·project-tools 스키마 복사 후 `git status` 변경 0 | 항상 |
| `migration-lint` | `pureiron-core migrate lint services/core/migrations --base-ref $BASE_REF` | 항상 |
| `no-customer-branching` | `(project_id\|site_id) == "…"` 리터럴, `deploy/lint/customer-names.txt` 이름 0건(제품 `src/`만) | 항상 |
| `web` | `npm ci`, `tsc --noEmit`, `eslint --max-warnings 0`, `vite build` | 항상 |
| `integration-compose` | 이미지 빌드 → compose(`pureiron-it`, `PUREIRON_IT_PROJECT`로 변경 가능) site-db → `migrate up` → 전체 기동 → `pytest -m "integration and not perf"`(readyz 200, migrate 종료코드, Timescale 객체, edit-lock 경쟁, historian DB, 시뮬레이터 100태그·collector kill, collector 30 s 네트워크 분리 replay) | 항상 |
| `web-e2e` | 이미지 빌드 → `uv sync` → compose(`pureiron-it`) site-db → `migrate up` → 전체 기동 → 시뮬레이터 100태그 + 알람 1개 정의 apply·CLI 계정(engineer·viewer) → Playwright chromium 스모크(로그인 → Overview GOOD → Alarms ACK 1회 → Trend canvas → Devices 연결됨 → Audit `alarm.ack` → 로그아웃 뒤 보호 경로 리다이렉트, 조회자 권한, 로그인 실패 고정 문구). 결과·trace·스크린샷 `artifacts/web-e2e/`, CI는 성공·실패 모두 `artifacts/web-e2e/` 전체(스크린샷 포함)를 artifact로 올린다. 명세는 `services/web/e2e/`, 정본은 [frontend](frontend.md) 16절 | 항상(CI: integration-compose 다음 job) |
| `perf-ingest` | 이미지 빌드 → `PUREIRON_SIM_REGISTERS=2000` compose → 2,000태그·poll 1 s 60 s 측정(r3부터 알람 정의 50개 + 전 태그 구독 WS 1개를 붙여서) → `artifacts/perf/<PUREIRON_PERF_RELEASE:-r3>/ingest.json`(batch/s·샘플/s·write_lag p50/p95/max·buffer max·drop·CPU/RSS·분당 행, r3부터 `alarms`·`ws` 수신 통계). 기록만, 합격선 없음 | 수동 |
| `n-minus-1` | 현재 커밋 core를 `pureiron/core:ci-<sha>`로 빌드해 migrate한 DB에서, 직전 epoch release 기록(`deploy/releases/r<N>.json`, 번호 순)의 core-test 이미지를 실행. 이미지가 없으면 image store(`PUREIRON_IMAGE_STORE`, 기본 `~/pureiron/images/<release>/`)의 tar를 load하고 config digest를 기록과 대조. 테스트와 별개로 N-1 앱의 `migrate status --json`이 N 스키마에서 envelope 안인지 확인. `deploy/build/n_minus_1_waivers.json`의 테스트만 deselect(waiver 테스트는 따로 돌려 여전히 실패해야 하고, 본 실행은 통과 ≥ 1·deselect 수 == waiver 수). 현재 epoch 4(r3) × r2 core-test. epoch 1이면 notice 후 통과 | 마이그레이션·`db` 경로 변경 PR, master push, nightly, `full-gate` 라벨 |
| `repro-build` | 캐시 없이 2회 빌드해 core·collector·web·simulator·core-test config digest 동일 확인(site-db 제외) | Dockerfile·`uv.lock`·`package-lock.json`·`deploy/build/` 변경 PR, master push, nightly, `full-gate` 라벨 |

- release 기록: `PUREIRON_RELEASE=r<N> bash deploy/build/build_images.sh` → `uv run python deploy/build/release_record.py r<N>`(커밋된 깨끗한 트리에서만, 이미지 tar를 image store에 보관).
- 도구 버전 고정: buf·oasdiff는 `scripts/tools.sh`(버전·sha256), uv 0.10.12, Node 22, 베이스 이미지·BuildKit은 digest 고정.
- 이미지 빌드(`deploy/build/build_images.sh`)는 전용 docker-container builder + `SOURCE_DATE_EPOCH`·`rewrite-timestamp`로 tar를 만든 뒤 `docker load` 한다(현장 번들과 같은 save/load 경로). uv가 남기는 설치 시각 메타데이터(`uv_cache.json`)는 `deploy/build/normalize_venv.py`가 지운다.
- web-e2e의 Playwright는 CI에서 브라우저만 받는다(`npx playwright install chromium`, `--with-deps` 아님). chromium 실행에 필요한 시스템 라이브러리는 self-hosted runner에 미리 설치돼 있어야 하며, runner 준비 때 한 번만 `sudo npx playwright install-deps chromium`을 실행한다(job에서는 sudo를 쓰지 않는다).
- 통합 테스트는 `PUREIRON_TEST_EXISTING_DSN`을 주면 새 DB를 만들지 않고 주어진 DB에서 돈다(n-minus-1이 쓰는 모드).

---
