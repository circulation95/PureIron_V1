# PureIron LLM Wiki (compact projection)

> 생성물 — 직접 편집 금지. `python scripts/wiki.py build`로 갱신.

이 문서는 Harness 가드레일에 주입되는 **압축 투영본**이다. 세부 내용은
아래 `세부 탐색` 절차대로 **섹션 단위**로 찾는다.

## SOT 요약
- **sot-agent-rules** (18KB/14절) — LLM 에이전트가 PureIron에서 작업할 때 따르는 규칙의 **정본**이다. ([docs/wiki/sot/agent-rules.md](wiki/sot/agent-rules.md))
- **sot-api-events** (11KB/10절) — PureIron의 REST prefix, WebSocket wire protocol, Chat SSE event, 내부 event bus topic 계약이다. ([docs/wiki/sot/api-events.md](wiki/sot/api-events.md))
- **sot-architecture** (19KB/19절) — PureIron은 단일 FastAPI 백엔드 + 단일 React 프론트엔드 + PostgreSQL(TimescaleDB + pgvector) + Redis로 구성된 모놀리식 Web HMI/SCADA 서버다. ([docs/wiki/sot/architecture.md](wiki/sot/architecture.md))
- **sot-data-contracts** (26KB/18절) — PureIron의 DB 스키마와 YAML/JSON 설정 파일 계약이다. ([docs/wiki/sot/data-contracts.md](wiki/sot/data-contracts.md))
- **sot-frontend** (18KB/15절) — 이 문서는 PureIron frontend의 **구조·상태·실시간·UI 작업 규칙**에 대한 정본이다. 상세 구현은 `frontend/src/**`가 정본이고, `frontend/AGENTS.md`와 `frontend/CLAUDE.md`는 도구별 operational cheat sheet다. ([docs/wiki/sot/frontend.md](wiki/sot/frontend.md))
- **sot-mes-schedule** (9KB/7절) — 이 문서는 PureIron의 **MES-lite / Schedule 기능의 v1 고객 노출 용어와 사용자 플로우 계약의 정본**이다. ([docs/wiki/sot/mes-schedule.md](wiki/sot/mes-schedule.md))
- **sot-product** (14KB/18절) — PureIron은 중소 제조업과 산업시설을 위한 경량 Web HMI/SCADA 플랫폼이다. ([docs/wiki/sot/product.md](wiki/sot/product.md))
- **sot-runtime-config** (10KB/12절) — PureIron 런타임 설정의 정본은 `backend/src/pureiron/config.py`의 `Settings(BaseSettings)` 클래스다. ([docs/wiki/sot/runtime-config.md](wiki/sot/runtime-config.md))
- **sot-testing** (32KB/18절) — 이 문서는 PureIron의 **검증 계층, 실행 명령, 변경 유형별 최소 게이트**에 대한 정본이다. 개별 테스트의 상세 assertion은 `backend/tests/**`, `backend/tests/e2e/**`, `tests/e2e/**`가 정본이다. ([docs/wiki/sot/testing.md](wiki/sot/testing.md))

## SOT 외 구획 (필요할 때만 열기)
- **gtm-first-contract** — 사업/첫 계약 결정. [docs/wiki/gtm/first-contract.md](wiki/gtm/first-contract.md)
- **report-mvp-gap** — 시점 스냅샷(audit). [docs/wiki/reports/mvp-gap.md](wiki/reports/mvp-gap.md)
`reports/**` 는 `last_verified` 시점의 감사 결과이므로 결론 근거로 쓰기 전에 코드로 재확인한다.

## 핵심 불변 규칙
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

## 세부 탐색 (섹션 단위 — 문서를 통째로 읽지 않는다)
위 SOT 는 대부분 10KB 이상이라 전문 Read 는 컨텍스트 낭비다. 순서를 지킨다.

1. `Grep(pattern=<키워드>, path="docs/wiki/_generated/section-index.tsv")`
2. 매칭 줄의 열: `doc_id  path  anchor  offset  limit  bytes  level  heading`
3. `Read(path, offset=<offset>, limit=<limit>)` — 그 절만 읽는다.

문서 전체 목록·메타데이터는 `docs/wiki/_generated/manifest.json`.
`_generated/llms-full.md` 는 외부 ingest 전용이므로 열지 않는다.
