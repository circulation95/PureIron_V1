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
