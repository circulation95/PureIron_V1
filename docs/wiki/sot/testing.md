---
id: sot-testing
title: Testing SOT — 검증 계층·명령·게이트
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/pyproject.toml
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/pytest.ini
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/tests
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/e2e/test_phase2_acceptance.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/e2e/test_demo_acceptance.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/tests/e2e/test_acceptance.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/tests/e2e/test_nonfunctional.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/package.json
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/frontend/AGENTS.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/CLAUDE.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/.github/workflows/ci.yml
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/.github/workflows/wiki.yml
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_core_domain_boundary_contract.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_alarm_ack_note_history_contract.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_alarm_summary_ui_contract.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_error_logging_quality_gate.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_operational_events.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/tests/test_project_package.py
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/operational
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/poc-browser-smoke.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/runbooks/agent-troubleshooting-answer-quality.md
last_verified: 2026-08-22
---

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
| `template-validate` → `template-outputs` | G1-8 V1 안 대리: 고객 repo와 같은 reusable workflow `validate-project.yml`을 `./`로 불러 `deploy/templates/pureiron-project`를 validate·`fmt --check`·hash → 출력 `content_hash`가 `sha256:` 형식이고 로컬 `pureiron-project hash`와 같으며 `project_version`이 빈 값(태그 실행 아님)인지 확인. 자기 호출은 `project/`·`pureiron-v1/`가 같은 트리라 hash 비교로는 경로 분리를 못 잡으므로 reusable 안에서 도구·프로젝트 경로를 직접 확인한다. 경로 분리·`project_version` 태그 분기는 외부 run(G1-8)에서 검증. 로컬 대응은 `unit`의 `deploy/tests/test_project_template.py` | CI만(`g1.sh`에 없는 job), 항상 |
| `integration-compose` | 이미지 빌드 → compose(`pureiron-it`, `PUREIRON_IT_PROJECT`로 변경 가능) site-db → `migrate up` → 전체 기동 → `pytest -m "integration and not perf"`(readyz 200, migrate 종료코드, Timescale 객체, edit-lock 경쟁, historian DB, 시뮬레이터 100태그·collector kill, collector 30 s 네트워크 분리 replay, 현장 PATCH·드래프트 게이트·서명 export(M5-2)) | 항상 |
| `web-e2e` | 이미지 빌드 → `uv sync` → compose(`pureiron-it`) site-db → `migrate up` → 전체 기동 → core 쓰기 스위치 켬(`PUREIRON_WRITES_ENABLED=true`) → 시뮬레이터 100태그 + 쓰기 선언 2개(설정값 `sim/sp_float`·명령 `sim/start`) + 허가 조건 coil `sim/remote_ok` + 알람 1개 정의 apply·CLI 계정(engineer·operator·viewer) → 쓰기 검사 통과 대기 → Playwright chromium 스모크(로그인 → Overview GOOD → Alarms ACK 1회 → Trend canvas → Devices 연결됨 → Audit `alarm.ack` → 로그아웃 뒤 보호 경로 리다이렉트, 조회자 권한, 로그인 실패 고정 문구) + 쓰기(M3c-3: 설정값 CONFIRMED·표 값 갱신, 조회자 쓰기 버튼 없음, 허가 조건 거짓 → 검사 거부, 명령 확인 2단계 → 장비 수신, confirm 응답을 바꿔(core에 보내지 않음) "장비 수신, 값 미확인"·"결과 모름" 문구, 명령 더블클릭·Enter 연타가 2단계 확인을 건너뛰지 않음, 전송 중 Esc 반복에도 대화상자 유지, confirm 409·410·404·503 문구). 결과·trace·스크린샷 `artifacts/web-e2e/`, CI는 성공·실패 모두 `artifacts/web-e2e/` 전체(스크린샷 포함)를 artifact로 올린다. 명세는 `services/web/e2e/`, 정본은 [frontend](frontend.md) 16절 | 항상(CI: integration-compose 다음 job) |
| `perf-ingest` | 이미지 빌드 → `PUREIRON_SIM_REGISTERS=2000` compose → 2,000태그·poll 1 s 60 s 측정(r3부터 알람 정의 50개 + 전 태그 구독 WS 1개를 붙여서) → `artifacts/perf/<PUREIRON_PERF_RELEASE:-r3>/ingest.json`(batch/s·샘플/s·write_lag p50/p95/max·buffer max·drop·CPU/RSS·분당 행, r3부터 `alarms`·`ws` 수신 통계). 기록만, 합격선 없음 | 수동 |
| `n-minus-1` | 현재 커밋 core를 `pureiron/core:ci-<sha>`로 빌드해 migrate한 DB에서, 직전 epoch release 기록(`deploy/releases/r<N>.json`, 번호 순)의 core-test 이미지를 실행. 이미지가 없으면 image store(`PUREIRON_IMAGE_STORE`, 기본 `~/pureiron/images/<release>/`)의 tar를 load하고 config digest를 기록과 대조. 테스트와 별개로 N-1 앱의 `migrate status --json`이 N 스키마에서 envelope 안인지 확인. `deploy/build/n_minus_1_waivers.json`의 테스트만 deselect(waiver 테스트는 따로 돌려 여전히 실패해야 하고, 본 실행은 통과 ≥ 1·deselect 수 == waiver 수). 현재 epoch 5(r4) × r3 core-test. epoch 1이면 notice 후 통과 | 마이그레이션·`db` 경로 변경 PR, master push, nightly, `full-gate` 라벨 |
| `repro-build` | 캐시 없이 2회 빌드해 core·collector·web·simulator·core-test config digest 동일 확인(site-db 제외) | Dockerfile·`uv.lock`·`package-lock.json`·`deploy/build/` 변경 PR, master push, nightly, `full-gate` 라벨 |

- **G1-8 고객 repo workflow**(M5-3b): V1 안 대리는 위 `template-*` job. 외부 run = private 테스트 repo `circulation95/pureiron-project-acme-test`(PureIron_V1 Actions access `user`, secret `PUREIRON_CI_APP_ID`·`PUREIRON_CI_APP_KEY`)에 템플릿을 복사하고 workflow ref를 검증할 브랜치로 바꿔 PR → run 로그 `content_hash` = 로컬 `pureiron-project hash deploy/templates/pureiron-project`, 이어서 태그 `v1` push → `project_version: v1`. 계약·접근 조건은 [data-contracts](data-contracts.md) M5-3b 절. 외부 run은 M5-3b 브랜치 push 뒤 실행한다(2026-10-30 기준 미실행).
- release 기록: `PUREIRON_RELEASE=r<N> bash deploy/build/build_images.sh` → `uv run python deploy/build/release_record.py r<N>`(커밋된 깨끗한 트리에서만, 이미지 tar를 image store에 보관).
- 도구 버전 고정: buf·oasdiff는 `scripts/tools.sh`(버전·sha256), uv 0.10.12, Node 22, 베이스 이미지·BuildKit은 digest 고정.
- 이미지 빌드(`deploy/build/build_images.sh`)는 전용 docker-container builder + `SOURCE_DATE_EPOCH`·`rewrite-timestamp`로 tar를 만든 뒤 `docker load` 한다(현장 번들과 같은 save/load 경로). uv가 남기는 설치 시각 메타데이터(`uv_cache.json`)는 `deploy/build/normalize_venv.py`가 지운다.
- web-e2e의 Playwright는 CI에서 브라우저만 받는다(`npx playwright install chromium`, `--with-deps` 아님). chromium 실행에 필요한 시스템 라이브러리는 self-hosted runner에 미리 설치돼 있어야 하며, runner 준비 때 한 번만 `sudo npx playwright install-deps chromium`을 실행한다(job에서는 sudo를 쓰지 않는다).
- 통합 테스트는 `PUREIRON_TEST_EXISTING_DSN`을 주면 새 DB를 만들지 않고 주어진 DB에서 돈다(n-minus-1이 쓰는 모드).
- **G1-6 property(hypothesis 1,000예, `deadline=None`)**: `packages/project-tools/tests/test_canonical.py`(JSON 값 dump/load 왕복)와 `test_absorb_property.py`(M5-3a) ⓐ 무작위 유효 트리를 JSON(정규형이 아닌 YAML)으로 쓰기 → `load_project` → `fmt` 고정점·fmt 전후 hash 동일 ⓑ 무작위 base 트리 + 무작위 PATCH 허용 값(설정·제거) 드래프트를 base 사본에 absorb → repo hash == 드래프트 `content_hash` ⓒ repo가 앞선 경우(태그 추가 + 기존 항목의 PATCH 비허용 필드 — 태그 `modbus.address`, 알람 `type`·`tag`·`description` — 변경) 그 변경 보존(드래프트 항목을 통째로 덮는 병합 버그를 잡는다). ⓑ·ⓒ의 hash 비교는 absorb가 기록한 `project.yaml absorbs` 키를 지운 트리 기준이다(기록 값은 따로 확인). `unit` job에 포함된다.
