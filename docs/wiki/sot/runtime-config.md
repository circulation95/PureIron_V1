---
id: sot-runtime-config
title: Runtime Config SOT — 환경변수·설정
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/src/pureiron/config.py
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/.env.example
  - services/control-api/src/pureiron_control/settings.py
  - deploy/fleet/publish.py
last_verified: 2026-11-04
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
- 현장 CLI(`pureiron-site`): `enroll`, `apply <pib>`, `bundle verify <pib>`, `status`, `secret set`, `report export`, `project export [--upload]`. 서비스 loop(`run`)는 `PUREIRON_CONTROL_API`가 있으면 tick마다 poll → heartbeat → 보고 전송(M4-1, [data-contracts](data-contracts.md) M4-1 절).
- Redis 없음(ADR-008 개정). core `/readyz` = DB 연결 + 스키마 호환 범위 + collector 세션. 응답의 `acquisition`(plan_rev·장비 연결 수·태그 GOOD 수·`good_ratio`·ingest 누적 `live`/`stale`/`future`/`replay`)은 집계일 뿐 준비 판정에 쓰지 않으며, Updater `checking` 단계의 GOOD 비율 기준이 이 값을 쓴다(M3a). `historian`(버퍼 행 수·drop·쓰기 지연·`db_ok`·버린 샘플 수·site-db가 거부해 격리한 행 수)도 정보로만 낸다 — 이력 DB 쓰기가 막혀도 수집·현재값은 계속된다(M3a-3).
- 장비·태그 정의: `pureiron-core apply-definitions <프로젝트 디렉터리>`가 project-tools로 검증한 뒤 `devices.definition`·`tags.definition`(jsonb)에 path 기준 upsert·retire 하고, `project.yaml`의 `historian`(기록 기본값, 정규형·비면 NULL)은 `project_state.historian`에 같은 트랜잭션으로 저장한다. 바뀐 것이 있을 때만 `project_state.definitions_rev`(= plan_rev)를 올린다. core는 이 rev를 주기적으로 읽어 collector에 새 plan을 보낸다(정식 project apply는 M5).
- **dev/VM DB 주의(M3a-3 D12)**: D12로 epoch 3 내용이 0008 → 0009로 늘었다(release된 epoch는 2까지라 현장 영향 없음). 0008까지 적용된 dev/VM DB는 새 core를 올리기 전에 `pureiron-core migrate up`이 필수다 — `/readyz`·Updater는 epoch 번호만 보므로, 안 하면 `/readyz`는 ready인데 `project_state.historian` 열이 없어 정의 로드가 실패하고 plan이 적용되지 않아 수집 전체가 멈춘다.
- 관측: core·collector `/metrics`, Updater가 60초마다 `state/metrics/<svc>.prom`·일별 jsonl로 기록, `/var/lib/pureiron` 사용률 경고 ≥ 0.80·위험 ≥ 0.90(M4-1부터 heartbeat `disk_state` `warn`·`critical`). Updater 송신 메트릭 `updater_reports_pending`·`updater_reports_rejected_total`·`updater_heartbeat_last_success_timestamp_seconds`(M4-1).

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
| `PUREIRON_WRITES_ENABLED` | core·compose | `false` | 현장 PLC 쓰기 스위치(ADR-013 2항, M3c-2). 꺼지면 plan `writable` 전부 false·모든 쓰기 거부. 켜고 기동하면 감사 `writes.enabled_at_boot` 1행 + `/readyz.writes.enabled`. compose는 `${PUREIRON_WRITES_ENABLED:-false}`로 core에 넘긴다. **현장에서는 fleet 현장 파일 `writes.enabled`로만 켠다**(M4-1 C2): 서명된 manifest `site_config.writes_enabled` → Updater 0.2.0이 generation `.env`에 늘 기록하고 compose 호출 환경에서는 이 키를 지운다 — `updater.env`·셸에 넣은 값은 무시(서비스 시작 경고) |
| `PUREIRON_WRITE_CONFIRM_TTL_S` | core | `60` | 쓰기 prepare 뒤 confirm까지 허용 시간(초, 최대 3600) |
| `PUREIRON_WRITE_TIMEOUT_S` | core | `10` | confirm이 collector 쓰기 결과를 기다리는 시한(초, 최대 60 = collector `timeout_ms` 상한). 넘으면 `NO_RESPONSE`. collector 보내기 기한 = 이 값 − 2s − 2 × 장비 `timeout_ms` |
| `PUREIRON_CORE_GRPC` | collector | `127.0.0.1:50051` | core gRPC 주소(compose: `core:50051`) |
| `PUREIRON_COLLECTOR_HEARTBEAT_S` | collector | `1.0` | 빈 batch heartbeat 주기 |
| `PUREIRON_COLLECTOR_RECONNECT_INITIAL_S` / `_MAX_S` | collector | `0.5` / `5` | 재연결 backoff. 상한은 링 보관 창(60 s)보다 훨씬 작아야 한다 — 0.5 s부터 두 배씩 30 s까지 늘면 35 s 단절 뒤 다음 시도가 ~66 s에 와서 분리 구간 앞부분이 링 창 밖으로 밀려 버려진다(M3a-3). LAN 재접속 비용은 작다 |
| `PUREIRON_SIM_REGISTERS` | compose(dev) | `100` | 시뮬레이터 holding 레지스터 수(`SIM_REGISTERS`). 부하 측정은 2000 |
| `PUREIRON_SECRETS_DIR` | compose | `/var/lib/pureiron/state/secrets` | `db_password`·`updater_token` 파일 위치 |
| `PUREIRON_DB_DATA` | compose | `/var/lib/pureiron/db` | site-db 데이터(개발·CI는 named volume `pureiron-db`) |
| `PUREIRON_WEB_BIND` / `PUREIRON_WEB_PORT` | compose | `0.0.0.0` / `80` | web 노출 |
| `PUREIRON_SITE_DB_VERSION` | compose | `deploy/releases/site-db.lock`의 version | site-db 이미지 태그 |

core의 초 단위 값(`…_S`)은 양의 유한 수만 받는다 — 0·음수·`nan`·`inf`는 기동 시 `SettingsError`로 실패한다(M3b-0). compose core는 `stop_grace_period: 25s`다(종료 예산은 [data-contracts](data-contracts.md) M3a-3 historian 현황).

### V1 control-api 환경변수 (M4-0 구현)

정본은 `services/control-api/src/pureiron_control/settings.py`. 벤더 쪽 중앙(M4 C3: 개발 PC WSL compose, 운영 compose는 `deploy/central` — 아래 M4-3 절)에서만 쓴다. 비밀값은 파일 경로로만 받는다.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `PUREIRON_CONTROL_DB_DSN` | `postgresql://pureiron@localhost:5432/pureiron_control` | control-db(PostgreSQL 16) 접속(비밀번호 제외) |
| `PUREIRON_CONTROL_DB_PASSWORD_FILE` | 없음 | control-db 비밀번호 파일 |
| `PUREIRON_CONTROL_HTTP_HOST` / `_HTTP_PORT` | `127.0.0.1` / `8100` | REST `/api/v1`(앞단 Caddy가 TLS) |
| `PUREIRON_CONTROL_DATA_DIR` | `/var/lib/pureiron-control` | 게시 번들 `bundles/<site>/g<gen>.pib`·드래프트 `drafts/<site>/<sha256>.tar` |
| `PUREIRON_CONTROL_POLL_S` | `60` | 현장 poll 주기 가정. `GET /fleet`의 `online` = `last_seen` ≤ 3 × 이 값(기본 180 s, C8) — 현장 Updater `PUREIRON_POLL_INTERVAL_S`와 맞춘다 |
| `PUREIRON_CONTROL_MAX_BUNDLE_BYTES` | `17179869184`(16 GiB) | `PUT /bundles` 업로드 상한(넘으면 413) |
| `PUREIRON_CONTROL_MIGRATIONS_DIR` | 소스 트리 `services/control-api/migrations` | 이미지(`pureiron/control-api`, M4-3)에서는 `/opt/pureiron/migrations` |
| `PUREIRON_RELEASE` | 패키지 버전 | `schema_migrations.release` 기록값 |
| `PUREIRON_DEV_CONTROL_DB_PORT` | `25433` | 개발·통합 테스트 compose(`deploy/compose/control.dev.yml`) control-db 호스트 포트 |

초·바이트 값은 양의 유한 수만 받고 잘못된 값은 기동 시 `SettingsError`(CLI 종료 코드 4)다. 사람·CI token은 env가 아니라 `pureiron-control token create --role admin|ci|viewer --name <이름>`으로만 만든다(원문은 그때 한 번만 출력, DB에는 SHA-256).

### V1 중앙 compose 변수·비밀 파일 (M4-3 구현)

정본은 `deploy/central/compose.yml`·`Caddyfile`·`.env.example`, 절차는 `deploy/central/README.md`. compose 변수(보간용, `.env` 또는 `--env-file`):

| 변수 | 기본값 | 의미 |
|---|---|---|
| `PUREIRON_CENTRAL_RELEASE` | compose `0.1.0-dev`, `.env.example` `c0` | `pureiron/control-api`·`control-web` 이미지 태그 = release 이름(기록 `deploy/releases/central/<이름>.json`). control-api `PUREIRON_RELEASE`로도 넘어간다. 운영은 기록이 있는 이름(`c0`, `c1`, …)만 — `0.1.0-dev`는 CI·개발용 태그(같은 docker 데몬의 CI가 PR 코드로 다시 빌드해 덮어씀)라 운영 금지 |
| `PUREIRON_CENTRAL_SECRETS_DIR` | 없음(필수 — 없으면 compose가 거부) | 비밀 파일 디렉터리(저장소 밖, 0700) |
| `PUREIRON_CENTRAL_HOST` | `localhost` | Caddy 사이트 주소 = `tls internal` 인증서 이름(쉼표로 여러 개). 여기 없는 `Host`로 온 요청은 421 |
| `PUREIRON_CENTRAL_DEFAULT_SNI` | `localhost` | Caddy 전역 `default_sni` — SNI 없는 클라이언트에 줄 인증서 이름. **IP로 운영하면 그 IP**(같은 IP를 `PUREIRON_CENTRAL_HOST`에도): IP로 접속하는 클라이언트는 SNI를 보내지 않아 기본값이면 TLS 핸드셰이크가 끊긴다(alert 80, M4-3 리뷰 실측). IP는 하나만 — 나머지는 DNS 이름으로 접속 |
| `PUREIRON_CENTRAL_BIND` / `PUREIRON_CENTRAL_HTTPS_PORT` | `127.0.0.1` / `28443` | 바깥에 여는 유일한 포트(Caddy 컨테이너 8443). control-db·control-api는 호스트 포트 없음 |
| `PUREIRON_CONTROL_POLL_S` | `60` | control-api 설정 그대로 전달(위 표) |

비밀 파일(`$PUREIRON_CENTRAL_SECRETS_DIR/…`, 컨테이너 `/run/secrets/…`): `control_db_password`(control-db·control-api), `viewer_token`(Caddy만 — `GET /ui/fleet`에 Bearer로 붙임, `{file.*}` placeholder가 요청마다 읽고 끝 줄바꿈은 버린다), `ui_basic_auth`(Caddy만 — 한 줄 `<사용자> <bcrypt 해시>`, 기동 때 Caddyfile `import`, 없으면 Caddy가 뜨지 않는다. **빈 파일이면 경고만 남기고 떠서 화면 요청이 모두 401**). 컨테이너 사용자가 읽도록 파일은 0644, 보호는 디렉터리 권한. 볼륨: `control-db`·`control-data`(`/var/lib/pureiron-control` 번들·드래프트, 이미지가 UID 10001 소유로 만든다)·`caddy-data`(로컬 CA — 지우면 루트 CA가 바뀐다)·`caddy-config`.

**비밀 파일 교체 규칙(모든 비밀 파일 공통)**: 같은 파일에 덮어쓴다(`cat 새파일 > 비밀파일`), `mv`로 바꾸지 않는다. compose `secrets:`는 파일 하나의 bind mount라 컨테이너가 처음 inode를 계속 본다. Docker Desktop(WSL) 실측(M4-3 수정 r1): 덮어쓰기는 재시작 없이 보이고, `mv` 뒤에는 옛 내용을 보다가 `restart`·`up -d` 때 `error mounting … no such file or directory`로 컨테이너가 뜨지 않는다. 복구는 `up -d --force-recreate <서비스>`. viewer 토큰 교체 순서 = 새 이름으로 `token create` → 다른 파일에 받기 → 확인 → 같은 파일에 덮어쓰기(여기까지 실패하면 멈추는 한 명령) → 화면 확인 → 옛 토큰 `token revoke --name`. `control_db_password` 값 변경은 덮어쓰기만으로 끝나지 않는다: postgres는 이 파일을 첫 initdb 때만 읽으므로 먼저 `ALTER ROLE` → 같은 파일에 덮어쓰기 → `restart control-api` 순서다. 파일만 바꾸면 재시작 뒤 `GET /ui/fleet` 503, `/api/v1/healthz` 200, 컨테이너 healthy다(실측). 명령은 `deploy/central/README.md` "비밀 파일 교체".

e2e(`deploy/tests/central_stack.py`)는 `PUREIRON_IT_PROJECT`(프로젝트 `<값>-central`)·`PUREIRON_IT_CENTRAL_PORT`(기본 28543)·`PUREIRON_RELEASE`로 격리하고 `PUREIRON_CONTROL_POLL_S=2`로 띄운다. 쓰지 않는 포트: 25432·25433(CI DB), 25443·8100(M4-2 리허설 central).

### V1 fleet publish 호스트 경로·환경변수 (M4-2 구현)

정본은 `deploy/fleet/publish.py`(`Config`·`parser`)와 `deploy/fleet/check_gen.py`. publish 호스트(PoC = 개발 PC WSL, 전용 사용자 `pureiron-publish`, runner 라벨 `publish`)에서만 쓴다. 우선순위는 인자 > 환경변수 > 기본값이고, 테스트는 모두 tmp 경로를 넘긴다.

| 인자 / 환경변수 | 기본값 | 의미 |
|---|---|---|
| `--keys-dir` / `PUREIRON_PUBLISH_KEYS_DIR` | `/etc/pureiron/keys` | `release.key`(0600)·`release-cert.json`·`release-cert.sig`·`root.pub`(`--root-pub`로 따로 지정 가능) |
| `--ci-token-file` / `PUREIRON_PUBLISH_CI_TOKEN_FILE` | `/etc/pureiron/publish/ci_token` | control-api ci 토큰(0600) — `PUT /bundles` |
| `--viewer-token-file` / `PUREIRON_PUBLISH_VIEWER_TOKEN_FILE` | `/etc/pureiron/publish/viewer_token` | control-api viewer 토큰(0600) — `GET /fleet`(ci 역할은 fleet을 못 읽는다) |
| `--app-key-file` / `PUREIRON_PUBLISH_APP_KEY_FILE` | `/etc/pureiron/publish/app.pem` | GitHub App private key(0600) — fleet(contents·pull_requests 읽기)·프로젝트(contents 읽기) 설치 토큰 |
| `--app-id` / `PUREIRON_PUBLISH_APP_ID` | 없으면 `/etc/pureiron/publish/app_id` 파일 | GitHub App ID(fleet-publish workflow가 secret `PUREIRON_CI_APP_ID`로 env를 준다) |
| `--bundles-dir` / `PUREIRON_PUBLISH_BUNDLES_DIR` | `/srv/pureiron/bundles` | `<site>/pureiron-<site>-g<gen>.pib`(+ `.bundle-metrics.json`) |
| `--metrics-dir` / `PUREIRON_PUBLISH_METRICS_DIR` | `/srv/pureiron/metrics` | `publish-<site>-g<gen>.json` 게시 기록 |
| `--state-dir` / `PUREIRON_PUBLISH_STATE_DIR` | `/srv/pureiron/publish` | 승인 사슬 `<owner>/<repo>/{last_approved, approved.log}`(주 1회 백업 대상), 잠금 `lock`(run·trust-base `flock`), 프로젝트 태그별 직전 게시 커밋 `project_commits.json` |
| `--store` / `PUREIRON_IMAGE_STORE` | `~/pureiron/images`(build_bundle 기본) | 이미지 store `<release>/<svc>.tar` — publish 호스트는 `/srv/pureiron/images`로 설정 |
| `--control-api` / `PUREIRON_CONTROL_API` | `http://127.0.0.1:8100/api/v1` | control-api 기준 URL(publish·check-gen 공통) |
| `--github-api` / `PUREIRON_GITHUB_API` | `https://api.github.com` | GitHub REST |
| `--git-base` / `PUREIRON_GIT_BASE` | `https://github.com` | git 원격 앞부분(`<base>/<owner>/<repo>.git`, 테스트는 로컬 경로) |
| `--project-owner` | fleet repo owner | 고객 프로젝트 repo owner |
| `PUREIRON_FLEET_VIEWER_TOKEN` | 없음(필수) | check-gen viewer 토큰 — fleet repo secret 같은 이름에서 |

토큰은 git argv가 아니라 환경(`GIT_CONFIG_*` → `http.extraHeader`)으로만 넘긴다. publish 호스트 git은 2.38 이상이어야 한다(승인 판정 트리 동치 `git merge-tree --write-tree`, WSL Ubuntu 24.04 = 2.43).
