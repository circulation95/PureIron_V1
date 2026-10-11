---
id: sot-api-events
title: API & Events SOT — 라우터·이벤트 계약
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/main.py
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron/events
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/chat/sse.py
  - https://github.com/circulation95/PureIron_ignition/tree/legacy-final/backend/src/pureiron
last_verified: 2026-06-26
---

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
- replay batch는 이력에만 기록되고 `tag.changed.*`를 발행하지 않는다. 이력 조회 HTTP API는 M3b.

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

## 내부 이벤트 버스

`backend/src/pureiron/events/bus.py`는 in-process async pub/sub이다. 외부 message broker 계약이 아니다.

현재 고정 topic:

- `tag.changed.{path}` — 특정 태그 값 변경
- `tag.changed.all` — 모든 태그 값 변경 공통 topic

발행자는 `TagStore.write_value()` 계열이다. 구독자는 WebSocket broadcaster, AlarmEngine, HistorianWriter 등이다.

중요 규칙:

- definition CRUD는 `tag.changed.*` 이벤트가 아니다. 이 이벤트는 값 변경용이다.
- DB I/O와 `event_bus.publish()`는 TagStore lock 해제 후 실행한다.
- Device health는 tag event로 만들지 않는다. `/api/devices`의 DriverHealth snapshot이 정본이다.

데이터 흐름과 lock discipline은 [architecture](architecture.md#이벤트-버스--잠금-규율)를 따른다.
