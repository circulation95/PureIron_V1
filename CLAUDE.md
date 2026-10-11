# CLAUDE.md

> **PureIron_V1** — Foundation 재설계의 새 코드 repo다. M1(2026-10-04)부터 서비스 코드 골격(`services/`·`packages/`·`deploy/`)과 G1 게이트(`scripts/g1.sh`)가 있다. `scripts/execute.py`(harness)는 legacy [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final)에서만 동작한다.

PureIron — 중소 산업시설용 경량 오픈소스 SCADA: FastAPI 백엔드 + React 프론트엔드 + 3D 트윈 + AI 멀티에이전트 채팅.

> 이 파일은 **Wiki SOT의 lean projection**이다. 작업 규칙 canonical은 [docs/wiki/sot/agent-rules.md](docs/wiki/sot/agent-rules.md), 문서 허브는 [docs/wiki/](docs/wiki/index.md). 장문(모듈표·환경변수표·설정 상세)은 위키 SOT가 정본이며 여기서 중복하지 않는다. 에이전트용 동일 규칙은 [AGENTS.md](AGENTS.md)에도 투영된다.

## 불변 규칙

<!-- Projection of docs/wiki/sot/agent-rules.md#불변-규칙. Keep short. Update via wiki workflow (scripts/wiki.py build). -->

V1 새 코드(`services/`·`packages/`·`deploy/`)에 적용한다. 구현 전이라 ADR-019~028의 목표 설계 기준이다.

- **No ORM** — asyncpg raw SQL만
- **단일 조립 지점** — core는 `SiteRuntime.build(settings)` → `create_app(rt)`, 생성자 주입, 전역 조회·`app.state` 금지, import-linter로 경계 강제
- **서비스 경계** — collector 무상태·DB 무접근, collector↔core는 `collector.proto` gRPC만, 이벤트 pub/sub은 core 내부에서만
- **Lock discipline** — TagStore 락 안에서 DB I/O·이벤트 발행 금지
- **계약 우선** — proto·OpenAPI·JSON Schema가 정본, 호환성 diff 통과, 생성물 drift 0
- **마이그레이션** — `pureiron-core migrate` ADD-only만, 부팅 자동 실행 금지
- **서명 번들** — 현장 변경은 서명된 `.pib`로만, 검증 실패는 중단 전 거부, registry pull 금지
- **프로젝트 정본** — 고객 project repo(Git), 비밀값은 `secret://`만, 고객별 코드 분기 금지
- **Graceful degradation** — `ai`·중앙 없이도 현장 동작, collector 다운 = BAD/DISCONNECTED, site-db 필수
- **Device health is not a tag** — collector `DeviceHealth` → core `/api/v1/devices` 단일 소스
- **PLC 로직 불변·쓰기 가드레일** — PLC 프로그램 수정 금지. 쓰기는 core만, 선언된 태그·현장별 승인(기본 읽기 전용)·사람 확인 또는 승인된 예약·readback·감사로만(ADR-013 개정)
- **No plugin system / No config UI / 20개 고정 expression 함수** — 프로토콜은 collector `drivers/` 직접 구현, 현장 편집은 REST `PATCH`만(편집 화면 없음)
- **엄격 게이트** — pyright·tsc strict, 억제 주석 0건, 생성 코드(`_gen/`)는 검사 제외 + drift 게이트

legacy [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 코드를 읽을 때의 규칙(`gateway.*`, Redis 등)은 [agent-rules](docs/wiki/sot/agent-rules.md)의 `legacy 코드 참고 규칙` 절에만 있다. 새 코드에 적용하지 않는다.

코드 구조·이름·주석·테스트 규약과 리뷰 체크리스트는 [coding-style](docs/wiki/sot/coding-style.md). 결정 근거는 [ADR](docs/wiki/adr/index.md). 도메인 세부는 위키 SOT([architecture](docs/wiki/sot/architecture.md) · [data-contracts](docs/wiki/sot/data-contracts.md) · [runtime-config](docs/wiki/sot/runtime-config.md) · [api-events](docs/wiki/sot/api-events.md) · [frontend](docs/wiki/sot/frontend.md)) 참조.

## Commands

```bash
# V1 G1 게이트 (로컬 = CI foundation-ci, 상세: docs/wiki/sot/testing.md 11절)
uv sync --all-packages
bash scripts/g1.sh all                    # lint-type·import-linter·unit·contracts-diff/drift·migration-lint·web
bash scripts/g1.sh integration-compose    # 이미지 빌드 + compose + TimescaleDB 통합 테스트
bash scripts/g1.sh contracts-gen          # 계약 생성물 갱신(OpenAPI·proto·schema.d.ts)

# core
uv run pureiron-core migrate status --json
docker compose -p pureiron -f deploy/compose/site.compose.yml -f deploy/compose/site.dev.yml --profile dev up -d

# 문서 SOT (load→save→build→check)
python scripts/wiki.py build
python scripts/wiki.py check
```

V1 환경변수 표는 [runtime-config](docs/wiki/sot/runtime-config.md)의 `V1 환경변수` 절(정본은 `services/core/src/pureiron_core/settings.py`). legacy backend/frontend/docker 명령은 `PureIron_ignition@legacy-final`에서만 동작한다.

## Wiki/SOT 작업 규율

위키는 문서 **수명**으로 구획된다: `docs/wiki/sot/`=불변 계약, `gtm/`=사업·첫 계약 결정, `reports/`=시점 스냅샷(감사), `adr/`=결정 근거, `archive/`=동결. 감사·격차 점검 결과는 `sot/`가 아니라 `reports/`에 두고, `reports/**`는 `last_verified` 시점 기준이므로 결론 근거로 쓰기 전에 코드로 재확인한다.

SOT를 **읽을** 때는 문서를 통째로 열지 않는다. `docs/wiki/_generated/section-index.tsv`를 grep 해 필요한 절의 `offset`/`limit`을 얻고 `Read(path, offset, limit)`으로 그 절만 읽는다(`_generated/llms-full.md`는 열지 않음). 정본은 [agent-rules](docs/wiki/sot/agent-rules.md)의 `문서 읽기 규율` 섹션.

문서·SOT를 고칠 때는 `/wiki`(`.claude/commands/wiki.md`) 또는 `skills/llm-wiki-maintainer` 절차를 따른다: **load → save → build → check**. SOT 내용은 `docs/wiki/**`에 저장하고, `AGENTS.md`/`CLAUDE.md`/`README.md`는 투영본이므로 짧게만 고친다. `docs/wiki/_generated/**`·`docs/LLM_WIKI.md`는 수기 편집 금지(생성물). 규율 정본은 [agent-rules](docs/wiki/sot/agent-rules.md).

## Harness Workflow

Phase 기반 단계 실행은 [AGENTS.md](AGENTS.md)의 `Harness Workflow` 섹션과 `skills/harness-framework/SKILL.md` 참조. `scripts/execute.py`가 각 step 프롬프트에 `AGENTS.md` + `CLAUDE.md` + `docs/LLM_WIKI.md`(없으면 `docs/*.md` fallback)를 자동 주입한다. `docs/LLM_WIKI.md`는 `docs/wiki/**` SOT에서 `scripts/wiki.py build`로 생성된다.
