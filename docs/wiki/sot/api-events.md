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
  - `writes`(M3c-2): `enabled`(현장 쓰기 스위치), `in_flight`(확인·전송 중), `outcomes{confirmed, written, mismatch, failed, rejected, no_response}`(core 기동 후 confirm 결과 수 — confirm 때 검사 거부는 rejected, prepare 거부는 세지 않음), `audit_failures_total`(기록하지 못한 쓰기 감사 행). 켜진 스위치를 밖에서 볼 수 있게 내는 정보 필드다.
  - **무인증 노출 정보**: `/readyz`는 인증 없이 열린다. 위 필드는 모두 집계 수(개수·비율·지연)뿐이고 태그 path·값, 알람 id·내용, 사용자 이름은 싣지 않는다 — 활성·미확인 알람 수의 변화 정도는 LAN에서 보인다는 것을 감수한 설계다(PR #26 리뷰 L-A3).

## V1 인증·감사 API (`/api/v1`, M3b-1 구현)

정본은 `services/core/src/pureiron_core/api/{auth,audit}.py`와 생성된 OpenAPI. 계정 생성·비밀번호 재설정·비활성화는 **CLI 전용**(`pureiron-core users add|passwd|disable|enable|list`)이고 REST에는 본인용 4개만 있다. 기본 계정은 자동 생성하지 않는다.

| 메서드·경로 | 권한 | 동작 |
|---|---|---|
| `POST /auth/login` | 없음 | `{username, password}` → `{token, expires_at, user}` + 세션 쿠키. 실패는 이유와 무관하게 401 `invalid username or password`(DB가 받지 못하는 NUL이 든 username도 조회 없이 같은 401). 실패 제한 중인 username은 **429** + `Retry-After`(아래 "로그인 실패 제한") |
| `POST /auth/logout` | 없음 | 보낸 세션(쿠키·Bearer)을 즉시 폐기 + 쿠키 삭제, 항상 204(이미 끝난 세션이어도 — HttpOnly 쿠키는 브라우저 JS가 지울 수 없다). 감사는 유효한 세션을 끝냈을 때만 |
| `GET /auth/me` | 로그인 | `{user_id, username, role, display_name}` |
| `POST /auth/password` | 로그인 | `{current_password, new_password}` → 204. 현재 비밀번호 틀림 400, 정책 위반·같은 비밀번호 422. 이 세션만 남기고 나머지 세션 폐기 |
| `GET /audit` | engineer+ | `?username&action&start&end&limit` → `{entries}` 최신순. 시간 `[start, end)`(시간대 필수), `limit` 1~1000(기본 100). NUL이 든 `username`·`action`은 422 |

- **세션 토큰 전달**: 브라우저는 로그인 응답의 쿠키 `pureiron_session`(HttpOnly·SameSite=Strict·Path=/api·Max-Age=절대 만료, Secure는 `PUREIRON_AUTH_COOKIE_SECURE`)을 그대로 쓴다 — JS가 토큰을 읽지 않고 WS 업그레이드에도 실린다. 비브라우저(ai·스크립트)는 응답 본문 `token`을 `Authorization: Bearer`로 보낸다. 둘 다 오면 쿠키를 쓴다.
- **OpenAPI 보안 스키마 이름**: 사용자 세션 = `SessionCookie`(apiKey/cookie)·`SessionBearer`(http bearer), Updater 편집 잠금 = `HTTPBearer`(http bearer, description `Updater token` — M1부터의 이름. 바꾸면 oasdiff `api-security-removed` breaking). 둘은 같은 `Authorization` 헤더를 쓰지만 서로 다른 비밀이다.
- **역할**: `viewer < operator < engineer < admin`(수준 비교, 응답은 소문자 이름). 미인증·무효 세션 401(`WWW-Authenticate: Bearer`, detail은 이유와 무관하게 `not authenticated`), 역할 부족 403, site-db 장애 503(detail은 고정 `site-db unavailable` — DB 오류 원문은 core 로그에만, `GET /project`도 같다).
- **REST Origin 검사**(M3c-0, `api/origin.py` `OriginGuard` — WS와 같은 host:port 비교): 안전 메서드(GET·HEAD·OPTIONS·TRACE)가 아닌 요청은 전부 대상이다. 세션 쿠키 `pureiron_session`이 실렸으면 `Origin`이 있어야 하고, `Origin`이 있으면(쿠키가 없어도, 경로와 무관하게 — login CSRF 포함) 그 host:port가 요청 `Host`와 같아야 하며(포트가 없으면 scheme 기본 포트) `Sec-Fetch-Site`가 있으면 `same-origin`이어야 한다. 쿠키도 `Origin`도 없는 요청(`Authorization: Bearer`를 쓰는 ai·Updater·스크립트)은 검사하지 않는다. 실패는 라우터에 닿기 전 **403 `{"detail": "E_ORIGIN"}`**, 감사 없이 core 경고 로그만(path는 이스케이프해 찍는다). 같은 PC의 다른 포트 웹앱은 same-site라 Strict 쿠키가 실리므로 이 경로(CSRF)를 막는다 — 그래서 프록시는 브라우저 Host를 그대로 넘긴다(WS 절과 같은 조건). 쿠키 jar를 쓰는 비브라우저 클라이언트는 Origin을 직접 싣거나 Bearer로 보낸다. **TLS 종단(백로그)**: 앞단 프록시가 TLS를 끝내고 기본 포트 443으로 접속하면(`https://site`, Host `site`) core는 http로 받으므로 Host 포트를 80으로 추정해 쿠키 쓰기·WS가 모두 403/4403이 된다 — core가 원래 scheme을 알아야 한다(`X-Forwarded-Proto` + `forwarded_allow_ips`, 또는 Host에 포트 명시). 지금 현장 web은 http라 영향이 없고 HTTPS 배치 마일스톤에서 고친다.
- **로그인 실패 제한**(M3c-0, `auth/throttle.py`): 같은 username이 10분 안에 5번 실패하면 그 뒤 10분 동안 `POST /auth/login`은 비밀번호를 확인하지 않고 **429** `too many failed logins, retry later` + `Retry-After`(남은 차단 초, 올림)로 끝난다. 결과를 기다리는 동시 시도도 실패처럼 세고(동시 추측 우회 방지) 이 거부는 곧 끝날 시도를 기다리는 것이라 `Retry-After: 5`(고정)다. site-db 장애(503)·예상 밖 예외(500)는 세지 않는다. 성공하면 그 username의 실패 수를 지운다. 영구 잠금은 없다(운전원 계정 DoS 방지) — 메모리에만 두므로 core 재기동 시 초기화된다. 추적 username은 10,000개까지: 넘으면 기록이 빈 항목, 그다음 차단되지 않은 가장 오래된 항목을 버리고, **차단 중인 항목은 버리지 않는다**(다른 이름을 뿌려 제한을 푸는 우회 방지). 버릴 항목이 없으면 새 username 시도는 `Retry-After: 5`의 429다. username 규칙에 맞지 않는 입력은 계정이 될 수 없어 세지 않는다. 429 응답은 감사 행을 더 쓰지 않는다(제한 시작 1건만). 값은 상수이며 env는 없다.
- **세션 무효 조건**: 로그아웃, 사용자 비활성화(CLI — 즉시, 다시 활성화해도 옛 세션은 폐기 상태), 관리자 비밀번호 재설정(그 사용자 전체), 유휴·절대 만료(`PUREIRON_AUTH_SESSION_*`).
- **감사 action**: `auth.login`·`auth.login_failed`(입력한 username과 `detail.reason` = `unknown_user|bad_password|disabled`, 비밀번호는 남기지 않음. username 규칙에 맞지 않는 입력 — 예: 비밀번호를 username 칸에 넣은 실수 — 은 username 없이 `detail.username_invalid = true`만)·`auth.login_throttled`(M3c-0, 실패 제한이 시작될 때 한 건: `username`·`target` = 그 username, `detail{failures, window_s, block_s}`)·`auth.logout`·`auth.password_change`(source `api`), `users.add|passwd|disable|enable`(source `cli`, 행위자 `username` = null, `target` = 계정), `project.edit_lock_acquire|release`(source `updater`, `target` = owner). 감사 기록 실패는 경고 로그만 남기고 요청은 성공한다.

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

## V1 쓰기 API (`/api/v1`, M3c-2 구현)

정본은 `services/core/src/pureiron_core/api/writes.py`·`writes/{ports,policy,service}.py`와 생성된 OpenAPI. ADR-013(개정) 가드레일의 core 쪽이다 — 사람 확인 쓰기만 있다(승인된 예약·AI 제안은 범위 밖, 감사 `origin`·`approved_by` 자리만 있음). 화면은 M3c-3.

| 메서드·경로 | 권한 | 동작 |
|---|---|---|
| `GET /writes/targets` | viewer+ | `{enabled, confirm_ttl_s, targets:[{tag_id, path, device_id, kind(setpoint·command), role(operator·engineer), data_type, min, max, values, permissive}]}` — 스위치가 꺼져도 선언은 보인다(path 순) |
| `POST /writes` | operator+ | prepare: `{tag_path, value, reason(1~200자, 공백만 불가)}` → 검사만 하고 **아무것도 보내지 않는다**. 통과 200 `{write_id, tag_id, tag_path, kind, value, current(TagValueOut), checks, expires_at}`, 하나라도 실패 **409** `{detail, write_id: null, checks}` + 감사 `tag.write_rejected`(stage prepare). `value`는 bit 형식(coil·bool)이면 `true/false`, 그 밖은 숫자 — 문자열은 바꾸지 않고 422, NaN·Inf 422 |
| `POST /writes/{write_id}/confirm` | operator+ | prepare한 **본인만**, `expires_at` 전, **1회용**(원자적으로 꺼낸다 — 두 번째·다른 사용자·모르는 id는 404, 다른 사용자 시도는 꺼내지 않는다). 만료 410(꺼냄). 검사를 다시 해 실패하면 409(`write_id` 포함, stage confirm). 감사 의도 행을 못 쓰면 **503 `audit unavailable; write not sent`**. 보냈으면 200 `{write_id, tag_id, tag_path, kind, value, before, outcome, readback, readback_ts, error}` |

- **실행 전 검사(prepare·confirm 둘 다, 응답 `checks` 순서 고정)**: `writes_enabled`(스위치) → `declared`(쓰기 선언 있음 — 없으면 이 둘만 낸다) → `role`(사용자 역할 ≥ 선언 role) → `value`(형식·setpoint 범위·command 허용 값) → `tag_good`(대상 GOOD) → `device_connected` → `single_collector_session`(세션 정확히 1개) → `plan_reported`(core plan rev = 정의 rev이고 collector가 그 rev로 live batch를 보고함 — plan 적용 중 수신 정지 창 차단) → `send_window`(장비 timeout으로 보내기 기한을 만들 수 있음) → `permissive`(허가 조건 태그 전부 GOOD·참) → `no_edit_lock`(site-db 편집 잠금 없음 — 못 읽으면 실패) → `not_in_flight`(같은 태그 진행 중 쓰기 없음). 실패 항목은 `ok: false`·`detail`(영어 이유).
- **결과(`outcome`)**: `CONFIRMED` setpoint readback = 쓴 값(float32는 그 형식으로 반올림한 값과 비교) · `WRITTEN` 장비 ack — command의 성공이고, setpoint면 readback을 못 읽은 경우(`error`에 이유 — 확인되지 않음) · `MISMATCH` setpoint readback 불일치 · `FAILED` 확실히 안 써짐(장비 미연결·요청 거부형 예외 응답) · `REJECTED` 보내지 않음(collector 거부, 의도 행 기록 중 검사가 바뀜) · `NO_RESPONSE` **결과 모름, 현장 확인**(core 시한 초과·세션 종료·collector `INDETERMINATE`·모르는 status — 써졌을 수 있다). command는 PLC가 명령 비트를 바로 지울 수 있어 readback을 기록만 한다. 자동 재시도는 없다 — 다시 쓰려면 새 prepare(현재값을 다시 보여 준다).
- **시한**: confirm은 collector 응답을 최대 `PUREIRON_WRITE_TIMEOUT_S`(기본 10s) 기다린다. collector `WriteRequest.timeout_ms` = 그 값 − 2s(수신 여유) − 2 × 장비 `timeout_ms`(기본 장비면 6000ms, 1000ms 미만이 되면 `send_window` 실패). request_id는 쓰기마다 새 값.
- **감사**(source `api`, `username` = 실행자, `target` = 태그 path): `tag.write` 두 행 — 의도 행 `detail.outcome = "SENDING"`이 **커밋돼야** 보내고, 결과 행(같은 `write_id`, best-effort)에 결과·`readback`·`readback_ts_ns`·`error`. `detail` 공통 = `{write_id, request_id, tag_path, tag_id, kind, before{value, quality, ts_ns}, target, reason, origin: "human", approved_by}`. `tag.write_rejected`(best-effort) = `{write_id, stage(prepare·confirm), tag_path, tag_id, target, reason, outcome: "REJECTED", failed:[검사 이름], checks}`. 스위치를 켠 채 기동하면 `writes.enabled_at_boot`(`detail{confirm_ttl_s, write_timeout_s}`) 1행 — 이 행이 남기 전에는 보내지 않는다(site-db가 없으면 confirm 503). SENDING만 있고 결과 행이 없는 쓰기는 결과 모름(기록 실패 또는 core 중단)이다. 이력은 `GET /audit?action=tag.write`(engineer+).
- WS 방송은 하지 않는다 — 값 변화는 기존 `tag_update`로 보인다.

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
