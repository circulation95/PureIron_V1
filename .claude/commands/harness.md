이 프로젝트는 Harness 프레임워크를 사용한다. `/harness` 워크플로우는 아래 6단계를 따른다. 자세한 규칙은 `skills/harness-framework`와 `skills/karpathy-guidelines` skill에 정의되어 있으니, 작업 중 필요한 시점에 해당 skill과 그 `references/`를 참조한다.

## 1. 프로젝트 문서 읽기

다음 파일을 먼저 읽고 제품 의도·아키텍처·기술 결정·운영 규칙을 파악한다. 필요시 Explore 에이전트를 병렬로 사용한다.

- `docs/PRD.md`
- `docs/ARCHITECTURE.md`
- `docs/ADR.md`
- `docs/UI_GUIDE.md`
- `CLAUDE.md`
- `AGENTS.md`

## 2. Karpathy guidelines 적용

`skills/karpathy-guidelines` skill의 4원칙(Think Before Coding / Simplicity First / Surgical Changes / Goal-Driven Execution)을 step 설계·작성 내내 적용한다. 특히:

- 각 step은 작고 자기완결적이어야 한다.
- AC는 실행 가능한 명령어로 적는다.
- 요청 범위를 벗어난 리팩터링·정리·기능 추가는 금지한다.

## 3. phase/step 설계

step 초안을 작성해 사용자 피드백을 받는다. 작성 규칙과 step.md 템플릿은 `skills/harness-framework/references/step-authoring-rules.md` 참조.

## 4. 사용자 승인 후 phase 파일 생성

승인되면 다음을 생성한다:

- `phases/index.json` (top-level 인덱스)
- `phases/{task-name}/index.json` (task 상세)
- `phases/{task-name}/stepN.md` (각 step의 자기완결 지시서)

JSON 스키마와 상태 전이는 `skills/harness-framework/references/phase-file-contract.md` 참조.

## 5. 실행 (Claude)

```bash
python scripts/execute.py <task-name> --agent claude
```

## 6. Codex로 실행 요청 시

```bash
python scripts/execute.py <task-name> --agent codex
```

## push 정책

`--push`는 사용자가 명시적으로 "푸시해라"라고 요청할 때만 붙인다. 기본값으로 사용하지 않는다.

## execute.py가 자동으로 처리하는 것

`feat-{task-name}` 브랜치 생성/checkout, 가드레일 주입(`CLAUDE.md` + `docs/*.md`), 완료 step의 summary 누적, 실패 시 최대 3회 재시도, 상태/타임스탬프 자동 기록, 코드/메타데이터 2단계 커밋.

## 에러 복구

- **error**: 해당 step의 `status`를 `pending`으로 되돌리고 `error_message`를 삭제한 뒤 재실행한다.
- **blocked**: `blocked_reason`을 해결한 뒤 `status`를 `pending`으로 되돌리고 `blocked_reason`을 삭제한 뒤 재실행한다.
