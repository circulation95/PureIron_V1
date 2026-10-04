# CLAUDE.md

> **PureIron_V1** — Foundation 재설계의 새 코드 repo다. 지금은 위키·문서·도구만 있고 서비스 코드(`services/`·`packages/`·`deploy/`)는 M1부터 추가된다. 이 파일의 backend/frontend/docker 명령·`scripts/execute.py`(harness)는 legacy [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final)에서만 동작한다.

PureIron — 중소 산업시설용 경량 오픈소스 SCADA: FastAPI 백엔드 + React 프론트엔드 + 3D 트윈 + AI 멀티에이전트 채팅.

> 이 파일은 **Wiki SOT의 lean projection**이다. 작업 규칙 canonical은 [docs/wiki/sot/agent-rules.md](docs/wiki/sot/agent-rules.md), 문서 허브는 [docs/wiki/](docs/wiki/index.md). 장문(모듈표·환경변수표·설정 상세)은 위키 SOT가 정본이며 여기서 중복하지 않는다. 에이전트용 동일 규칙은 [AGENTS.md](AGENTS.md)에도 투영된다.

## 불변 규칙

<!-- Projection of docs/wiki/sot/agent-rules.md#불변-규칙. Keep short. Update via wiki workflow (scripts/wiki.py build). -->

- **No `app.state`** — 모든 상태는 `gateway.*` 경유(`request.app.state` 금지)
- **No ORM** — asyncpg raw SQL, 멱등 마이그레이션만으로 스키마 변경
- **No plugin system** — v1 지원 프로토콜(OPC-UA·Modbus·S7·LS·AB·MELSEC)은 `drivers/`에 직접 구현, registry/plugin화 금지. 새 프로토콜도 `drivers/`에 직접 추가
- **No config UI** — 인프라/env·플러그인 레지스트리·범용 config 빌더 UI 금지. v1 운영 설정(Tag/Device/Alarm/User/Dashboard)은 REST API + 제한된 화면 허용, seed는 YAML/JSON(상세 [agent-rules](docs/wiki/sot/agent-rules.md))
- **20개 고정 expression 함수** — 확장 불가, 복잡 로직은 샌드박스 스크립트로
- **Lock discipline** — DB I/O·`tag.changed.*` 발행은 TagStore 락 밖에서(데드락 방지)
- **Graceful degradation** — 선택적 서비스(DB/Redis/RAG/LangGraph)는 안전하게 실패, 전체 크래시 금지
- **Event-driven** — 태그 변경은 `tag.changed.*` pub/sub으로 전파
- **Device health is not a tag** — `DriverHealth.snapshot()`(`/api/devices`)이 단일 소스. 디바이스 추가 시 사용자는 일반 태그만 추가하고 `[health]` 시스템 태그는 생성하지 않는다(상세 [backend/CLAUDE.md](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/CLAUDE.md))
- **Frontend** — No `as any` / `@ts-ignore`, `tsc --noEmit` 통과 필수

결정 근거는 [ADR](docs/wiki/adr/index.md). 도메인 세부는 위키 SOT([architecture](docs/wiki/sot/architecture.md) · [data-contracts](docs/wiki/sot/data-contracts.md) · [runtime-config](docs/wiki/sot/runtime-config.md) · [api-events](docs/wiki/sot/api-events.md) · [frontend](docs/wiki/sot/frontend.md)) 참조.

## Commands

```bash
# Backend
cd backend && uv sync
uv run uvicorn pureiron.main:app --reload --port 8000
uv run pytest tests/ -v
uv run ruff check .

# Frontend
cd frontend && npm install && npm run dev   # http://localhost:3000
npm run lint                                  # tsc --noEmit

# Docker
docker compose up
docker compose -f docker-compose.dev.yml up  # OPC-UA + Modbus 시뮬레이터 포함

# 문서 SOT (load→save→build→check)
python scripts/wiki.py build
python scripts/wiki.py check
```

기본 계정(dev): `admin` / `admin`. 환경변수 전체 표는 [runtime-config](docs/wiki/sot/runtime-config.md)(정본은 `backend/src/pureiron/config.py`).

## Wiki/SOT 작업 규율

위키는 문서 **수명**으로 구획된다: `docs/wiki/sot/`=불변 계약, `gtm/`=사업·첫 계약 결정, `reports/`=시점 스냅샷(감사), `adr/`=결정 근거, `archive/`=동결. 감사·격차 점검 결과는 `sot/`가 아니라 `reports/`에 두고, `reports/**`는 `last_verified` 시점 기준이므로 결론 근거로 쓰기 전에 코드로 재확인한다.

SOT를 **읽을** 때는 문서를 통째로 열지 않는다. `docs/wiki/_generated/section-index.tsv`를 grep 해 필요한 절의 `offset`/`limit`을 얻고 `Read(path, offset, limit)`으로 그 절만 읽는다(`_generated/llms-full.md`는 열지 않음). 정본은 [agent-rules](docs/wiki/sot/agent-rules.md)의 `문서 읽기 규율` 섹션.

문서·SOT를 고칠 때는 `/wiki`(`.claude/commands/wiki.md`) 또는 `skills/llm-wiki-maintainer` 절차를 따른다: **load → save → build → check**. SOT 내용은 `docs/wiki/**`에 저장하고, `AGENTS.md`/`CLAUDE.md`/`README.md`는 투영본이므로 짧게만 고친다. `docs/wiki/_generated/**`·`docs/LLM_WIKI.md`는 수기 편집 금지(생성물). 규율 정본은 [agent-rules](docs/wiki/sot/agent-rules.md).

## Harness Workflow

Phase 기반 단계 실행은 [AGENTS.md](AGENTS.md)의 `Harness Workflow` 섹션과 `skills/harness-framework/SKILL.md` 참조. `scripts/execute.py`가 각 step 프롬프트에 `AGENTS.md` + `CLAUDE.md` + `docs/LLM_WIKI.md`(없으면 `docs/*.md` fallback)를 자동 주입한다. `docs/LLM_WIKI.md`는 `docs/wiki/**` SOT에서 `scripts/wiki.py build`로 생성된다.
