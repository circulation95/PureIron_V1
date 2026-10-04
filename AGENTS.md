# AGENTS.md — PureIron Agent Entrypoint

> **PureIron_V1** — Foundation 재설계의 새 코드 repo다. M1(2026-10-04)부터 서비스 코드 골격(`services/`·`packages/`·`deploy/`)과 G1 게이트(`scripts/g1.sh`, `.github/workflows/foundation-ci.yml`)가 있다. 이 파일의 backend/frontend/docker 명령·`scripts/execute.py`(harness)는 legacy [`PureIron_ignition@legacy-final`](https://github.com/circulation95/PureIron_ignition/tree/legacy-final)에서만 동작한다.

이 파일은 PureIron에서 LLM Agent가 처음 읽는 **짧은 projection**이다. 정본은 `docs/wiki/**`와 `USER.md`에 있다.

장문 제품 설명, 아키텍처 상세, 사용자 성향, 작업 규칙을 이 파일에 중복해서 쓰지 않는다. 필요한 내용은 아래 SOT를 읽고 따른다.

## 1. 작업 전 읽기 순서

1. 현재 대화에서 사용자가 직접 준 지시
2. `USER.md` — 왕순환/팀 역할과 작업 성향
3. `docs/wiki/index.md` — PureIron Wiki 허브
4. `docs/wiki/sot/agent-rules.md` — Agent 작업 규칙 정본
5. 작업 성격에 맞는 SOT 문서
   - 제품/고객/v1 범위: `docs/wiki/sot/product.md`
   - 아키텍처/런타임 구조: `docs/wiki/sot/architecture.md`
   - DB·설정 계약: `docs/wiki/sot/data-contracts.md`
   - API·이벤트: `docs/wiki/sot/api-events.md`
   - 프론트엔드: `docs/wiki/sot/frontend.md`
   - 런타임 설정/env: `docs/wiki/sot/runtime-config.md`
   - 테스트: `docs/wiki/sot/testing.md`
   - 결정 근거: `docs/wiki/adr/index.md`
   - 첫 계약/사업 결정: `docs/wiki/gtm/first-contract.md`
   - 구현 격차 audit(시점 스냅샷): `docs/wiki/reports/mvp-gap.md`

위키는 문서 **수명**으로 구획된다: `sot/`=불변 계약, `gtm/`=사업 결정, `reports/`=시점 스냅샷, `adr/`=결정 근거, `archive/`=동결. 감사·격차 점검 결과는 `sot/`가 아니라 `reports/`에 둔다. `reports/**`는 `last_verified` 시점 기준이므로 결론 근거로 쓰기 전에 코드로 재확인한다.

위 SOT는 대부분 10KB 이상(`testing.md` 27KB)이다. **문서를 통째로 읽지 않는다.** `docs/wiki/_generated/section-index.tsv`를 grep 해 필요한 절의 `offset`/`limit`을 얻고 `Read(path, offset, limit)`으로 그 절만 읽는다. `_generated/llms-full.md`는 외부 ingest 전용이라 열지 않는다. 정본은 [agent-rules](docs/wiki/sot/agent-rules.md)의 `문서 읽기 규율` 섹션.

충돌 시 우선순위는 **현재 사용자 지시 > SOT 문서 > projection 문서**다.

## 2. PureIron 판단 기준

주요 판단은 다음 순서로 한다.

1. 첫 계약 가능성
2. 고객 설득력
3. 2인 팀 감당 가능성
4. 장기 플랫폼화
5. 실행 속도
6. 기술적 완성도

기술적으로 멋있어도 첫 계약과 v1 고객 설득에 직접 기여하지 않으면 보류한다.

## 3. 불변 규칙 요약

상세와 근거는 `docs/wiki/sot/agent-rules.md`와 `docs/wiki/adr/index.md`를 따른다. V1 새 코드(`services/`·`packages/`·`deploy/`)에 적용하며 ADR-019~028 목표 설계 기준이다.

- No ORM — asyncpg raw SQL만
- 단일 조립 지점 — core는 `SiteRuntime.build()` + 생성자 주입, 전역 조회·`app.state` 금지, import-linter로 경계 강제
- 서비스 경계 — collector 무상태·DB 무접근, collector↔core는 `collector.proto` gRPC만
- Lock discipline — TagStore 락 안에서 DB I/O·이벤트 발행 금지
- 계약 우선 — proto·OpenAPI·JSON Schema가 정본, 호환성 diff 통과, 생성물 drift 0
- 마이그레이션 — `pureiron-core migrate` ADD-only만, 부팅 자동 실행 금지
- 서명 번들 — 현장 변경은 서명된 `.pib`로만, 검증 실패는 중단 전 거부
- 프로젝트 정본 — 고객 project repo(Git), 비밀값은 `secret://`만, 고객별 코드 분기 금지
- Graceful degradation — `ai`·중앙 없이도 현장 동작, collector 다운 = BAD/DISCONNECTED, site-db 필수
- Device health is not a tag — collector `DeviceHealth` → core `/api/v1/devices`
- PLC 로직 불변·쓰기 가드레일 — PLC 프로그램 수정 금지. 쓰기는 core만, 선언된 태그·현장별 승인(기본 읽기 전용)·사람 확인 또는 승인된 예약·readback·감사로만(ADR-013 개정)
- No plugin system / No config UI / 20개 고정 expression 함수 — 현장 편집은 REST `PATCH`만
- 엄격 게이트 — 억제 주석(`type: ignore`·`eslint-disable`·`as any`·`@ts-ignore`·`@ts-expect-error`) 0건, 생성 코드는 검사 제외 + drift 게이트

legacy 코드(`gateway.*`, Redis 등) 규칙은 agent-rules의 `legacy 코드 참고 규칙` 절에만 있고 새 코드에 적용하지 않는다.

## 4. 작업 방식

- 설계 없이 성급하게 코딩하지 않는다.
- 구현 요청을 받았으면 설명만 하고 멈추지 말고 실제 파일 수정, 실행, 테스트까지 진행한다.
- 완료 보고는 실제 실행 결과에 근거한다.
- 모르면 추정하지 말고 관련 SOT, 코드, 테스트를 확인한다.
- 범위가 커지면 v1/첫 계약 기준으로 잘라낸다.

## 5. Wiki / 문서 규율

SOT 문서 수정 시:

```bash
python scripts/wiki.py build
python scripts/wiki.py check
```

- `docs/wiki/sot/**`와 `docs/wiki/adr/index.md`가 수기 관리 정본이다.
- `docs/wiki/_generated/**`와 `docs/LLM_WIKI.md`는 생성물이다. 직접 수정하지 않는다.
- `AGENTS.md`, `CLAUDE.md`, `README.md`는 짧은 projection으로 유지한다.
- secret/API key 값을 문서 본문에 복사하지 않는다.
- check 경고를 없애려고 소스 코드나 env 기본값을 임의로 바꾸지 않는다.
- **Default branch 직접 변경 금지**: 위키/문서 변경은 새 브랜치 + PR로 제안하고 **human review 후 merge**한다. 3계층(raw 원본 → wiki SOT → 생성물) 역할과 전체 흐름은 [agent-rules](docs/wiki/sot/agent-rules.md)의 `기여 흐름` 섹션 참조.

## 6. 자주 쓰는 명령

```bash
# Wiki
python scripts/wiki.py inventory
python scripts/wiki.py build
python scripts/wiki.py check

# V1 G1 게이트 (로컬 = CI, 상세: docs/wiki/sot/testing.md 11절)
uv sync --all-packages
bash scripts/g1.sh all                    # lint-type·import-linter·unit·contracts·migration-lint·web
bash scripts/g1.sh integration-compose    # 이미지 빌드 + compose + TimescaleDB 통합
bash scripts/g1.sh contracts-gen          # 계약 생성물 갱신(OpenAPI·proto·schema.d.ts)
uv run pureiron-core migrate status --json
```

legacy backend/frontend 명령은 `PureIron_ignition@legacy-final`에서만 동작한다.
