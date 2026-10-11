---
id: wiki-index
title: PureIron Wiki — 문서 허브
status: canonical
audience: both
owners: [pureiron-core]
sources:
  - README.md
last_verified: 2026-06-30
---

# PureIron Wiki

PureIron 문서의 **단일 정본(Source of Truth) 허브**. 사람과 LLM 에이전트 모두 이 위키를 기준으로 삼는다. 루트 `README.md`·`AGENTS.md`·`CLAUDE.md`는 이 위키로 연결되는 짧은 진입점/투영(projection)이다.

> SOT 문서는 실제 정본 내용을 가진다. 운영자용 상세 절차는 [archive/manual-legacy](archive/manual-legacy.md)를 통해 `MANUAL.md`로 연결된다.

## 구획 (문서 수명 기준)

| 구획 | 수명 | 내용 |
|---|---|---|
| [`sot/`](sot/agent-rules.md) | 불변 | 계약. 시점 개념이 없다 |
| [`gtm/`](gtm/first-contract.md) | 결정 시점 고정 | 사업/첫 계약 결정 |
| [`reports/`](reports/mvp-gap.md) | 시점 스냅샷 | 감사·audit. `last_verified` 이후 낡을 수 있다 |
| [`adr/`](adr/index.md) | 결정 시점 고정 | 아키텍처 결정 근거 |
| [`archive/`](archive/manual-legacy.md) | 동결 | legacy 포인터 |

감사·격차 점검 결과는 `sot/`가 아니라 `reports/`에 둔다. 상세 규칙은 [agent-rules](sot/agent-rules.md).

## SOT 지도

| 주제 | 문서 | 원천(SOT) |
|------|------|-----------|
| 제품 의도 | [product](sot/product.md) | Wiki SOT |
| 아키텍처 | [architecture](sot/architecture.md) | `gateway.py`, backend/frontend source |
| 런타임 설정·환경변수 | [runtime-config](sot/runtime-config.md) | `config.py`, `.env.example` |
| 데이터 계약(DB·설정) | [data-contracts](sot/data-contracts.md) | `db/migrations/*.sql`, `configs/**` |
| API·이벤트 | [api-events](sot/api-events.md) | `*/router.py`, `events/` |
| MES/Schedule v1 용어·플로우 | [mes-schedule](sot/mes-schedule.md) | Wiki SOT (`mes/`, `agents/tools/schedule_tools.py`, `ScheduleScreen.tsx`) |
| 프론트엔드 | [frontend](sot/frontend.md) | `frontend/src/**`, `frontend/package.json` |
| V1 코드 스타일·구조·리뷰 기준 | [coding-style](sot/coding-style.md) | `pyproject.toml`, `services/core/.importlinter`, V1 `services/**` |
| 테스트 | [testing](sot/testing.md) | `backend/tests/`, `tests/e2e/`, `frontend/package.json` |
| 에이전트 작업 규칙 | [agent-rules](sot/agent-rules.md) | Wiki SOT + projection files |
| 아키텍처 결정 기록 | [adr](adr/index.md) | Wiki ADR |

### SOT 외 구획

| 주제 | 문서 | 구획 |
|------|------|------|
| 첫 계약 GTM/PoC | [first-contract](gtm/first-contract.md) | `gtm/` — 사업 결정 |
| MVP 격차 audit | [mvp-gap](reports/mvp-gap.md) | `reports/` — 시점 스냅샷 |

## 생성물 (수기 편집 금지)

`scripts/wiki.py build`가 생성한다. 직접 고치지 말고 `build`를 다시 돌린다.

- [manifest.json](_generated/manifest.json) — 전체 문서 색인 + 메타데이터(`bytes`·`section_count` 포함)
- [section-index.tsv](_generated/section-index.tsv) — 섹션 단위 검색 인덱스. **문서를 통째로 읽지 않기 위한 진입점**으로, grep 해서 `offset`/`limit`을 얻어 해당 절만 Read 한다(상세는 [agent-rules](sot/agent-rules.md#문서-읽기-규율-섹션-단위-검색))
- [llms.txt](_generated/llms.txt) — LLM용 링크 인덱스
- [llms-full.md](_generated/llms-full.md) — canonical 문서 본문 연결. 외부 LLM ingest 전용이며 에이전트는 열지 않는다
- [sot-drift-report.md](_generated/sot-drift-report.md) — env/API drift 경고
- `../LLM_WIKI.md` — Harness가 읽는 compact projection

## 도구

```bash
python scripts/wiki.py inventory   # 문서 후보·제외 목록
python scripts/wiki.py build        # 생성물 갱신
python scripts/wiki.py check        # 구조 검증(비파괴) + drift 경고
```

운영 절차(load→save→build→check)는 `/wiki` 명령([.claude/commands/wiki.md](../../.claude/commands/wiki.md)) 또는 `skills/llm-wiki-maintainer`를 따른다. 규율 정본은 [agent-rules](sot/agent-rules.md).

pre-commit 훅 활성화: `git config core.hooksPath .githooks`
