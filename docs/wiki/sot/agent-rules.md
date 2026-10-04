---
id: sot-agent-rules
title: Agent Rules SOT — 에이전트 작업 규칙
status: canonical
audience: agent
owners: [pureiron-core]
sources:
  - AGENTS.md
  - CLAUDE.md
  - USER.md
  - https://github.com/circulation95/PureIron_ignition/blob/legacy-final/backend/CLAUDE.md
last_verified: 2026-08-17
---

# Agent Rules SOT

> **PureIron_V1 기준** — 아래 `불변 규칙`은 V1 새 코드(`services/`·`packages/`·`deploy/`)에 적용하며, 아직 구현 전이라 [ADR](../adr/index.md) 019~028의 목표 설계를 따른다. legacy [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 코드를 읽을 때의 규칙은 `legacy 코드 참고 규칙` 절에만 있고 새 코드에 적용하지 않는다.

LLM 에이전트가 PureIron에서 작업할 때 따르는 규칙의 **정본**이다.

`AGENTS.md` / `CLAUDE.md` / `README.md`는 이 문서와 다른 SOT 문서로 연결되는 짧은 projection이다. 장문 규칙, 제품 설명, 아키텍처 상세를 루트 projection에 중복해서 쓰지 않는다.

## 이 문서의 역할

이 문서는 “에이전트가 PureIron에서 어떻게 일해야 하는가”만 다룬다.

위키는 **문서의 수명**으로 구획을 나눈다. 새 문서를 만들 때 어느 구획에 속하는지 먼저 정한다.

| 구획 | 수명 | 성격 |
|---|---|---|
| `docs/wiki/sot/**` | 불변 | 계약. 코드와 함께 바뀌지만 "시점" 개념이 없다 |
| `docs/wiki/gtm/**` | 결정 시점에 고정 | 사업/첫 계약 결정. canonical 이지만 엔지니어링 계약이 아니다 |
| `docs/wiki/reports/**` | 시점 스냅샷 | 감사·audit. `last_verified` 이후 낡을 수 있다 |
| `docs/wiki/adr/**` | 결정 시점에 고정 | 아키텍처 결정 근거 |
| `docs/wiki/archive/**` | 동결 | legacy 포인터. `llms-full.md` 입력에서 제외 |

**감사·격차 점검·현황 조사 결과를 `sot/`에 두지 않는다.** 그것은 계약이 아니라 스냅샷이고, 낡은 스냅샷은 없는 것보다 나쁘게 작동한다(실제와 다른 상태를 근거로 결론이 나옴). `reports/`에 두고 `last_verified`를 갱신한다. 90일이 지나면 `wiki.py check`가 `[stale-report]` 경고를 낸다.

문서별 책임은 다음과 같이 나눈다.

| 문서 | 책임 |
|---|---|
| [product](product.md) | 제품 의도, 고객, v1 가치, 장기 방향 |
| [first-contract](../gtm/first-contract.md) | 첫 계약 GTM/PoC/데모/가격 결정 프레임(미확정은 `가설`/`TBD`/`needs-founder-input`) |
| [architecture](architecture.md) | 시스템 구조, 런타임 구성, 모듈 경계 |
| [data-contracts](data-contracts.md) | DB·설정 데이터 계약 |
| [api-events](api-events.md) | REST / WebSocket / SSE / 이벤트 계약 |
| [frontend](frontend.md) | 프론트엔드 구조와 제약 |
| [testing](testing.md) | 검증 명령과 인수 기준 |
| [mvp-gap](../reports/mvp-gap.md) | 첫 read-only PoC 기준 구현 격차(시점 스냅샷) |
| [adr](../adr/index.md) | 아키텍처 결정 근거 |
| [USER.md](../../../USER.md) | 왕순환/팀 역할과 작업 성향 |

에이전트는 특정 내용을 수정할 때 해당 책임 문서에 저장한다. 같은 내용을 여러 SOT에 반복하지 않는다.

## 작업 전 컨텍스트 로딩 순서

PureIron 작업을 시작할 때는 다음 순서로 컨텍스트를 확인한다.

1. 현재 대화에서 사용자가 직접 준 지시
2. `USER.md` — 사용자/팀 역할, 작업 성향
3. `docs/wiki/sot/product.md` — 제품 의도와 v1 범위
4. 작업 성격에 맞는 SOT 문서
   - 아키텍처: `architecture.md`
   - DB/설정: `data-contracts.md`
   - API/이벤트: `api-events.md`
   - 프론트엔드: `frontend.md`
   - 테스트: `testing.md`
5. 필요한 경우 projection 문서: `AGENTS.md`, `CLAUDE.md`

루트 projection보다 `docs/wiki/sot/**`를 우선한다.

## 제품 판단 원칙

PureIron의 v1 판단 기준은 다음이다.

1. 첫 계약 가능성
2. 고객 설득력
3. 2인 팀 감당 가능성
4. 장기 플랫폼화
5. 실행 속도
6. 기술적 완성도

에이전트는 기술적으로 멋있지만 v1 고객 설득과 첫 계약에 직접 기여하지 않는 기능을 보류해야 한다.

v1은 기존 PLC 제어를 대체하지 않는다. 실제 설비 자동 제어, 완전 예측 AI Simulation, 사람 승인 없는 자동 조치는 v1 범위가 아니다.

## 불변 규칙

- No ORM — asyncpg raw SQL만 사용한다(core·control-api 모두). 근거 ADR-001
- 단일 조립 지점 — core는 `SiteRuntime.build(settings)` → `create_app(rt)`에서만 조립하고 의존성은 생성자로 주입한다. 전역 싱글톤 조회·`app.state` 금지, 모듈 경계는 import-linter 계약으로 강제. 근거 ADR-007 개정·ADR-019
- 서비스 경계 — collector는 무상태·DB 무접근, collector↔core 통신은 `collector.proto` gRPC 계약만. core 내부 이벤트 pub/sub은 프로세스 안에서만 쓴다. 근거 ADR-010·ADR-020
- Lock discipline — TagStore 락 안에서 DB I/O·이벤트 발행 금지(데드락 방지). 근거 ADR-009
- 계약 우선 — `collector.proto`·OpenAPI·JSON Schema가 정본이다. 호환성 diff를 통과해야 하고 생성물은 재생성 diff 0을 유지한다. 근거 ADR-028
- 마이그레이션 — 스키마 변경은 `pureiron-core migrate`의 ADD-only 마이그레이션만. 부팅 시 자동 실행 금지, 제거는 epoch 규칙(deprecated 후 다음 epoch 이후)으로만. 근거 ADR-024
- 서명 번들 — 현장 변경은 서명된 `.pib`로만 반입한다. 검증 실패는 서비스 중단 전에 거부, registry pull 금지, 이미지는 config digest로 확인. 근거 ADR-023
- 프로젝트 정본 — 고객 설정 정본은 고객 project repo(Git)다. 비밀값은 `secret://<name>` 참조만, 제품 코드에 고객별 분기(`site_id == "…"`, 고객 이름) 금지. 근거 ADR-025·ADR-026
- Graceful degradation — 선택 서비스(`ai`·중앙)가 없어도 현장 모니터링·알람·이력은 동작한다. collector 다운 = 해당 장비 BAD/DISCONNECTED, site-db는 필수(core readiness 조건). 근거 ADR-008 개정
- Device health is not a tag — 장비 상태는 collector `DeviceHealth` → core `GET /api/v1/devices`가 단일 소스, 태그로 만들지 않는다. 근거 ADR-018
- 쓰기 비활성 — `WriteRequest`는 계약만 두고 보내지 않는다. 기존 PLC 제어를 대체하지 않는다. 근거 ADR-013
- No plugin system — 프로토콜은 collector `drivers/`에 직접 구현하고, 추가는 proto `TagPoint.address` oneof 확장으로만 한다. registry·동적 로딩 금지. 근거 ADR-002
- No config UI — 인프라·env·드라이버·범용 config 빌더 UI 금지. 현장 편집은 REST `PATCH`만(아래 “No config UI” 범위 절). 근거 ADR-004·ADR-025
- 20개 고정 expression 함수 — 확장 불가, 복잡 로직은 샌드박스 스크립트로. 근거 ADR-005
- 엄격 게이트 — pyright strict·tsc/eslint strict, 억제 주석(`type: ignore`·`eslint-disable`·`as any`·`@ts-ignore`·`@ts-expect-error`) 0건. 생성 코드(`_gen/`)는 억제 대신 검사 제외 + drift 게이트. 근거 ADR-011·ADR-028

> 위 bullet은 `scripts/wiki.py build`가 파싱해 `docs/LLM_WIKI.md`·`docs/wiki/_generated/agent-context.md`로 투영한다. 한 줄을 고치면 build 후 양쪽에 반영된다. 결정 근거·트레이드오프는 [adr](../adr/index.md) 참조.

## legacy 코드 참고 규칙 (PureIron_ignition)

이식 작업에서 [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final) 코드를 읽을 때의 맥락이다. **V1 새 코드에는 적용하지 않는다.**

- legacy 백엔드 상태는 `ScadaGateway` 싱글톤 `gateway.*`에 모여 있다(`request.app.state` 미사용). V1은 이 패턴을 쓰지 않는다(ADR-007 개정)
- legacy 마이그레이션은 부팅 시 `backend/src/pureiron/db/migrations/*.sql` 알파벳순 자동 실행이다(ADR-006, V1에서는 ADR-024로 대체)
- legacy 선택 서비스는 DB/Redis/RAG/LangGraph이고, 실패해도 서버가 계속 뜬다
- legacy 태그 변경은 `tag.changed.*` pub/sub, 장비 상태는 `DriverHealth.snapshot()`(`/api/devices`)이다
- legacy v1 운영 설정 화면(Tag/Device/Alarm/User/Dashboard)과 `/api/settings`·`/api/tags`·`/api/devices`가 존재한다

## “No config UI” 범위

“No config UI”는 **모든 설정 화면 금지가 아니다.** PureIron frontend는 운영자가 매일 쓰는 경량 SCADA 도구이므로([frontend](frontend.md)) v1 운영 설정 화면은 제품에 필요하다. 이 규칙이 막는 것은 제품 코어를 무겁게 만드는 범용 config builder와 인프라 설정 UI이며, 운영에 필요한 고정 스코프 설정 화면은 정상 범위다.

**금지(추가하지 않는다):**

- 런타임 인프라/env(DB·Redis·포트·시크릿 등) 설정 UI — 인프라 설정은 env와 [runtime-config](runtime-config.md)로만 한다.
- 플러그인 marketplace/registry UI, 프로토콜·드라이버를 UI에서 동적으로 등록·확장하는 빌더 — 위의 “No plugin system” 규칙과 연결된다.
- 임의 스키마를 그리는 schema-less 범용 no-code config builder, drag-and-drop config UI builder.

**허용(v1 운영에 필요):**

- Tag/Device/Alarm/User/Dashboard 등 v1 운영 설정의 REST API와 고정 스코프 설정 화면. API 계약은 [api-events](api-events.md), 화면 규칙은 [frontend](frontend.md)를 따른다.
- seed/demo 초기값을 담은 YAML/JSON config.

legacy의 Settings/Tag/Device 화면과 `/api/settings`·`/api/tags`·`/api/devices` 등 API는 이 허용 범위에 속한다.

**V1(새 경로)**: 운영 설정의 정본은 고객 project repo(YAML)다. 현장 화면은 로그인 + 태그 현황·알람/ACK·트렌드·장비 상태·감사 로그 5개이고 **설정 편집 화면은 없다**. 현장 긴급 수정은 REST `PATCH`(드래프트, `site_rev` 증가)로만 하고 벤더가 고객 repo로 회수한다(ADR-025). 결정 근거와 트레이드오프는 [adr](../adr/index.md)의 ADR-004(YAML + REST Config Only)를 따른다.

## 구현 작업 규율

에이전트는 성급하게 코딩부터 하지 않는다. 그러나 구현을 요청받았는데 설명만 하고 멈추지도 않는다.

기본 흐름은 다음과 같다.

1. 관련 문서와 기존 구조 확인
2. 목적과 고객 가치 확인
3. v1 범위와 장기 확장성 확인
4. 최소 설계
5. 작은 단위 구현
6. 실제 실행 또는 테스트
7. 결과 검증
8. 필요한 문서 반영

완료 보고는 실제 실행 결과에 근거해야 한다. 실행하지 않은 결과를 실행한 것처럼 말하지 않는다.

## 작업 증거 규율

리뷰용 화면 증거(스크린샷)를 저장소에 커밋하지 않는다. legacy의 `docs/pr-evidence/**` 는 동결됐고 PureIron_V1로 가져오지 않았다(`.gitignore` 가 신규 유입을 막는다).

- 증거는 **PR 본문에 직접 업로드**한다. GitHub 첨부 스토리지에 올라가 브랜치를 지워도 살아남고 저장소가 커지지 않는다.
- 업로드가 불가능한 CLI/자동화 경로에서 부득이 커밋해야 하면, 링크를 **브랜치명이 아니라 커밋 SHA permalink**로 건다. 브랜치명 ref 는 병합 후 브랜치 삭제와 함께 죽는다(기존 `docs/pr-evidence/**` 링크가 전부 이 이유로 깨졌다).
- 회귀 방지 가치가 있는 증거는 스크린샷 대신 **계약 테스트**로 고정한다. 이미지보다 오래 가고 CI 가 지켜준다(기준은 [testing](testing.md)).
- 배경과 대체 절차는 legacy [`docs/pr-evidence/README.md`](https://github.com/circulation95/PureIron_ignition/blob/legacy-final/docs/pr-evidence/README.md).

## 위키 규율

- Wiki/SOT 작업은 `skills/llm-wiki-maintainer` 절차를 따른다: **load → save → build → check**.
- SOT 내용은 `docs/wiki/**`의 canonical 문서에만 저장한다.
- 투영 문서(`AGENTS.md`, `CLAUDE.md`, `README.md`)는 사용자가 명시 요청한 범위에서만 짧게 고친다.
- 문서 수정 후 `python scripts/wiki.py build`로 생성물을 갱신하고 함께 커밋한다.
- `docs/wiki/_generated/**`와 `docs/LLM_WIKI.md`는 **수기 편집 금지**다.
- secret/API key 값을 위키 본문에 복사하지 않는다. `sources`는 실재 경로만, glob은 `source_globs`만 사용한다.
- `check`를 통과시키려고 소스 코드(`config.py`·라우터 등)를 임의로 바꾸지 않는다. drift는 경고일 뿐이다.

## 문서 읽기 규율 (섹션 단위 검색)

SOT 문서는 대부분 10KB를 넘고 `testing.md`는 27KB다. 한 가지 규칙을 확인하려고 문서를 통째로 Read 하면 그 자체로 컨텍스트를 소진한다. **문서 단위가 아니라 섹션 단위로 읽는다.**

1. `Grep(pattern=<키워드>, path="docs/wiki/_generated/section-index.tsv")` — 매칭된 줄만 돌아온다.
2. 줄 형식: `doc_id · path · anchor · offset · limit · bytes · level · heading` (탭 구분).
3. `Read(path, offset=<offset>, limit=<limit>)` — 해당 절만 읽는다.

`offset`/`limit`은 파일 전체 기준 1-indexed 시작 줄과 줄 수이므로 `Read` 인자로 그대로 쓴다. 섹션 범위는 `자기 heading ~ 다음 동급/상위 heading 직전`이라 `##` 절을 읽으면 하위 `###`가 함께 들어온다. `bytes`는 그 절을 읽는 비용이다.

- 문서 전체 목록·메타데이터(`bytes`·`section_count` 포함)는 `docs/wiki/_generated/manifest.json`.
- `docs/wiki/_generated/llms-full.md`(약 180KB)는 **열지 않는다.** canonical 전문을 이어붙인 외부 LLM ingest 전용 산출물이다.
- 키워드를 모를 때만 문서 전문을 읽고, 그때도 어떤 절이 필요했는지 확인한 뒤 다음부터는 인덱스를 쓴다.
- 섹션 인덱스의 `offset` 정확성은 `wiki.py check`의 `[section]` 검사가 hard-fail로 보장한다. 인덱스가 코드와 어긋나면 `build`를 다시 돌린다.

## 중복 방지 원칙

SOT 문서는 서로 링크하고, 같은 내용을 복사하지 않는다.

- 제품/고객/사업 전략은 `product.md`에 둔다.
- 첫 계약 GTM/PoC/데모/가격 결정은 `gtm/first-contract.md`에 둔다.
- 구현 격차·현황 감사 결과는 `reports/**`에 둔다(`sot/` 아님).
- 사용자/팀 성향은 `USER.md`에 둔다.
- 불변 기술 규칙과 작업 방식은 이 문서에 둔다.
- 코드 구조와 런타임 흐름은 `architecture.md`에 둔다.
- DB·설정 계약은 `data-contracts.md`에 둔다.
- API 세부는 `api-events.md`에 둔다.
- 프론트엔드 세부는 `frontend.md`에 둔다.
- 테스트 명령과 기준은 `testing.md`에 둔다.

중복이 필요해 보이면 원문을 복사하지 말고 해당 SOT로 링크한다.

## 자동 생성 vs 수기 관리 범위

- **자동 생성(직접 편집 금지):** `docs/LLM_WIKI.md`, `docs/wiki/_generated/**`(`manifest.json`·`llms.txt`·`llms-full.md`·`sot-drift-report.md`·`agent-context.md`·`section-index.tsv`).
- **수기 관리(정본):** `docs/wiki/sot/**`, `docs/wiki/gtm/**`, `docs/wiki/reports/**`, `docs/wiki/adr/index.md`.
- **수기 관리(projection):** `AGENTS.md` / `CLAUDE.md` / `README.md` — 소개 + canonical 포인터 + 불변 규칙 블록 + Commands 정도만 유지한다.
- Harness(`scripts/execute.py`)는 `docs/LLM_WIKI.md`를 중심으로 step 컨텍스트를 주입한다.
- 세 계층(raw 원본 → wiki SOT → 생성물)의 역할 구분과 변경 제출 흐름은 아래 `기여 흐름` 섹션이 정본이다.

## 기여 흐름 (raw → wiki → PR → review)

PureIron 문서는 **3계층**으로 나뉘며, 계층마다 역할과 편집 권한이 다르다.

- **raw source (보존 계층)** — `backend/**`, `frontend/**`, `configs/**`, DB 마이그레이션, `config.py`·`.env.example` 등 런타임/설정 파일. 시스템의 정본(system of record). LLM은 문서나 `check`를 통과시키려고 raw source를 임의로 고치지 않는다(drift는 경고일 뿐). 루트 레거시 문서(`docs/PRD.md`·`docs/ADR.md`·`MANUAL.md`)는 위키가 요약하는 **source input**일 뿐 canonical SOT가 아니다(ADR 정본은 [adr](../adr/index.md)).
- **wiki SOT (통합 계층)** — `docs/wiki/**`(`_generated/` 제외). LLM이 raw source를 통합·요약·교차참조하는 곳이며 문서의 canonical.
- **generated projection (생성물)** — `docs/wiki/_generated/**`·`docs/LLM_WIKI.md`. `python scripts/wiki.py build` 출력 전용, 수기 편집 금지.

변경은 **raw → wiki → PR → review** 한 방향으로 흐른다.

- LLM은 raw source를 읽고 → wiki SOT 계층을 작성/수정 → `build` → `check` 한다(절차 정본은 위 `위키 규율` 섹션의 load→save→build→check).
- **default branch(`master`/`main`) 직접 커밋/푸시 금지** — 모든 변경은 전용 브랜치에서 한다.
- 위키/문서 변경은 LLM이 직접 머지하지 않고 **PR로 제안**한다.
- **머지 전 human review 필수** — LLM은 wiki 변경을 self-merge 하지 않는다.
- 문서 PR은 **documentation-only**: 앱 코드·CI·스케줄러 변경을 같이 싣지 않는다.
