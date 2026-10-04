---
name: harness-framework
description: 사용자가 `/harness`, "Harness plan", "phase step 생성", "execute.py로 순차 실행", "harness 워크플로우", "phase 디자인", "scripts/execute.py" 등을 요청할 때 사용한다. 작업을 `phases/{task-name}/stepN.md`로 쪼개고 `scripts/execute.py`로 Claude 또는 Codex가 순차 실행하도록 phase 파일을 설계·생성·실행하는 워크플로우.
license: MIT
---

# Harness Framework

이 저장소는 작업을 `phases/{task-name}/stepN.md` 단위로 쪼개고, `scripts/execute.py`가 각 step을 순차 실행하는 Harness 프레임워크다. 이 skill은 phase/step 설계와 실행 절차를 정의한다.

## 언제 사용하는가

- 사용자가 `/harness`를 호출하거나 Harness phase를 새로 만들어 달라고 요청할 때.
- 기존 phase를 실행하거나 재실행하라는 요청을 받을 때.
- "phase 디자인", "step 작성", "execute.py 실행" 같은 키워드가 등장할 때.

## A. 사전 읽기 (필수)

phase를 설계하거나 실행하기 전에 아래를 먼저 읽어 제품 의도·아키텍처·기술 결정·운영 규칙을 파악한다.

- `docs/PRD.md`
- `docs/ARCHITECTURE.md`
- `docs/ADR.md`
- `docs/UI_GUIDE.md`
- `CLAUDE.md`
- `AGENTS.md`

문서가 비어 있거나 일부 누락이라도 존재하는 항목은 모두 확인한다. 필요하면 Explore 에이전트를 병렬로 활용한다.

## B. 워크플로우

1. **탐색** — 위 문서와 관련 코드를 읽는다.
2. **논의** — 구체화·결정해야 할 사항이 있으면 사용자에게 제시한다.
3. **Step 설계** — `karpathy-guidelines` skill의 4원칙(Think Before Coding / Simplicity First / Surgical Changes / Goal-Driven Execution)을 적용해 step 초안을 작성한다. 자세한 설계 규칙은 `references/step-authoring-rules.md` 참조.
4. **사용자 승인** — 사용자가 OK 하면 phase 파일을 생성한다.
5. **실행** — 아래 "실행 커맨드"를 따른다.

## C. 생성할 phase 파일

| 파일 | 역할 |
| --- | --- |
| `phases/index.json` | 모든 task의 진행 상태 인덱스 (top-level) |
| `phases/{task-name}/index.json` | 개별 task의 step 목록과 상태 |
| `phases/{task-name}/stepN.md` | 각 step의 자기완결 지시서 (N은 0부터) |

각 파일의 JSON 스키마, 상태 전이, 자동 기록 필드는 `references/phase-file-contract.md`에 정의되어 있다.
step 파일 템플릿과 작성 규칙은 `references/step-authoring-rules.md`에 정의되어 있다.

## D. Karpathy guidelines 적용

step을 설계·작성할 때 항상 `karpathy-guidelines` skill을 적용한다. 핵심:

- 각 step은 작고 자기완결적이어야 한다.
- AC(Acceptance Criteria)는 실행 가능한 명령어로 적는다.
- 요청 범위를 벗어난 리팩터링·정리·기능 추가는 금지한다.

## E. 실행 커맨드

기본(Claude):

```bash
python scripts/execute.py <task-name> --agent claude
```

Codex로 실행:

```bash
python scripts/execute.py <task-name> --agent codex
```

`--push`는 사용자가 명시적으로 "푸시해라"라고 요청할 때만 붙인다. 기본값으로 사용하지 않는다.

`execute.py`가 자동으로 처리하는 것: `feat-{task-name}` 브랜치 생성/checkout, 가드레일 주입(CLAUDE.md + docs/*.md), 완료 step의 summary 누적, 실패 시 최대 3회 재시도, 상태/타임스탬프 기록, 코드/메타데이터 2단계 커밋.

## F. 에러 복구

- **error**: 해당 step의 `status`를 `pending`으로 되돌리고 `error_message`를 삭제한 뒤 재실행한다.
- **blocked**: `blocked_reason`을 해결한 뒤 `status`를 `pending`으로 되돌리고 `blocked_reason`을 삭제한 뒤 재실행한다.

## 참고

- `references/phase-file-contract.md` — JSON 스키마와 상태 전이 표
- `references/step-authoring-rules.md` — step 작성 7원칙과 step.md 템플릿
- `karpathy-guidelines` skill — step 설계 시 적용할 코딩 원칙
