# PureIron LLM Wiki (compact projection)

> 생성물 — 직접 편집 금지. `python scripts/wiki.py build`로 갱신.

이 문서는 Harness 가드레일에 주입되는 **압축 투영본**이다. 세부 내용은
아래 `세부 탐색` 절차대로 **섹션 단위**로 찾는다.

## SOT 요약
- **sot-agent-rules** (14KB/13절) — LLM 에이전트가 PureIron에서 작업할 때 따르는 규칙의 **정본**이다. ([docs/wiki/sot/agent-rules.md](wiki/sot/agent-rules.md))
- **sot-api-events** (11KB/10절) — PureIron의 REST prefix, WebSocket wire protocol, Chat SSE event, 내부 event bus topic 계약이다. ([docs/wiki/sot/api-events.md](wiki/sot/api-events.md))
- **sot-architecture** (14KB/14절) — PureIron은 단일 FastAPI 백엔드 + 단일 React 프론트엔드 + PostgreSQL(TimescaleDB + pgvector) + Redis로 구성된 모놀리식 Web HMI/SCADA 서버다. ([docs/wiki/sot/architecture.md](wiki/sot/architecture.md))
- **sot-data-contracts** (16KB/10절) — PureIron의 DB 스키마와 YAML/JSON 설정 파일 계약이다. ([docs/wiki/sot/data-contracts.md](wiki/sot/data-contracts.md))
- **sot-frontend** (18KB/15절) — 이 문서는 PureIron frontend의 **구조·상태·실시간·UI 작업 규칙**에 대한 정본이다. 상세 구현은 `frontend/src/**`가 정본이고, `frontend/AGENTS.md`와 `frontend/CLAUDE.md`는 도구별 operational cheat sheet다. ([docs/wiki/sot/frontend.md](wiki/sot/frontend.md))
- **sot-mes-schedule** (9KB/7절) — 이 문서는 PureIron의 **MES-lite / Schedule 기능의 v1 고객 노출 용어와 사용자 플로우 계약의 정본**이다. ([docs/wiki/sot/mes-schedule.md](wiki/sot/mes-schedule.md))
- **sot-product** (13KB/18절) — PureIron은 중소 제조업과 산업시설을 위한 경량 Web HMI/SCADA 플랫폼이다. ([docs/wiki/sot/product.md](wiki/sot/product.md))
- **sot-runtime-config** (6KB/7절) — PureIron 런타임 설정의 정본은 `backend/src/pureiron/config.py`의 `Settings(BaseSettings)` 클래스다. ([docs/wiki/sot/runtime-config.md](wiki/sot/runtime-config.md))
- **sot-testing** (29KB/17절) — 이 문서는 PureIron의 **검증 계층, 실행 명령, 변경 유형별 최소 게이트**에 대한 정본이다. 개별 테스트의 상세 assertion은 `backend/tests/**`, `backend/tests/e2e/**`, `tests/e2e/**`가 정본이다. ([docs/wiki/sot/testing.md](wiki/sot/testing.md))

## SOT 외 구획 (필요할 때만 열기)
- **gtm-first-contract** — 사업/첫 계약 결정. [docs/wiki/gtm/first-contract.md](wiki/gtm/first-contract.md)
- **report-mvp-gap** — 시점 스냅샷(audit). [docs/wiki/reports/mvp-gap.md](wiki/reports/mvp-gap.md)
`reports/**` 는 `last_verified` 시점의 감사 결과이므로 결론 근거로 쓰기 전에 코드로 재확인한다.

## 핵심 불변 규칙
- No `app.state` — 모든 상태는 `gateway.*` 경유(`request.app.state` 금지)
- No ORM — asyncpg raw SQL, 멱등 마이그레이션만으로 스키마 변경
- No plugin system — v1 지원 프로토콜(OPC-UA·Modbus·S7·LS·AB·MELSEC)은 `drivers/`에 직접 구현하고 동적 로딩·registry·plugin lifecycle로 추상화하지 않는다. 새 프로토콜도 `drivers/`에 직접 추가한다. v1 지원 프로토콜 정본 표는 [architecture](wiki/sot/architecture.md)
- No config UI — 런타임 인프라/env·플러그인 레지스트리·드라이버 빌더·schema-less 범용 no-code config 빌더를 UI로 노출하지 않는다. v1 운영에 필요한 Tag/Device/Alarm/User/Dashboard 설정은 REST API와 제한된 화면으로 제공하고, seed/demo 초기값은 YAML/JSON으로 유지한다(상세는 아래 “No config UI 범위” 절, 근거는 [adr](wiki/adr/index.md) ADR-004)
- 20개 고정 expression 함수 — 확장 불가, 복잡 로직은 샌드박스 스크립트로
- Lock discipline — DB I/O·`tag.changed.*` 발행은 TagStore 락 밖에서(데드락 방지)
- Graceful degradation — 선택적 서비스(DB/Redis/RAG/LangGraph)는 안전하게 실패, 전체 크래시 금지
- Event-driven — 태그 변경은 `tag.changed.*` pub/sub으로 전파
- Device health is not a tag — `DriverHealth.snapshot()`(`/api/devices`)이 단일 소스, `[health]` 시스템 태그 생성 금지
- Frontend — No `as any` / `@ts-ignore` / `@ts-expect-error`, `tsc --noEmit` 통과 필수

## 세부 탐색 (섹션 단위 — 문서를 통째로 읽지 않는다)
위 SOT 는 대부분 10KB 이상이라 전문 Read 는 컨텍스트 낭비다. 순서를 지킨다.

1. `Grep(pattern=<키워드>, path="docs/wiki/_generated/section-index.tsv")`
2. 매칭 줄의 열: `doc_id  path  anchor  offset  limit  bytes  level  heading`
3. `Read(path, offset=<offset>, limit=<limit>)` — 그 절만 읽는다.

문서 전체 목록·메타데이터는 `docs/wiki/_generated/manifest.json`.
`_generated/llms-full.md` 는 외부 ingest 전용이므로 열지 않는다.
