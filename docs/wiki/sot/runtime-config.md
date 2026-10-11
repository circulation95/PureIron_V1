---
id: sot-runtime-config
title: Runtime Config SOT — 환경변수·설정
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/config.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/.env.example
last_verified: 2026-06-26
---

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
