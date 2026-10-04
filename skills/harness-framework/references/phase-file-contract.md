# Phase File Contract

Harness phase 파일들의 JSON 스키마, 상태 전이, 자동 기록 필드 정의.

## 1. `phases/index.json` (top-level)

여러 task를 관리하는 top-level 인덱스. 이미 존재하면 `phases` 배열에 새 항목을 추가한다.

```json
{
  "phases": [
    {
      "dir": "0-mvp",
      "status": "pending"
    }
  ]
}
```

필드:

- `dir`: task 디렉토리명 (`phases/{dir}/` 와 일치).
- `status`: `"pending"` | `"completed"` | `"error"` | `"blocked"`. `execute.py`가 실행 중 자동으로 업데이트한다.

자동 기록 필드 (생성 시 넣지 않는다):

- `completed_at`, `failed_at`, `blocked_at` — `execute.py`가 상태 변경 시 자동으로 기록.

## 2. `phases/{task-name}/index.json` (task 상세)

```json
{
  "project": "<프로젝트명>",
  "phase": "<task-name>",
  "steps": [
    { "step": 0, "name": "project-setup", "status": "pending" },
    { "step": 1, "name": "core-types", "status": "pending" },
    { "step": 2, "name": "api-layer", "status": "pending" }
  ]
}
```

필드 규칙:

- `project`: 프로젝트명 (`CLAUDE.md` 참조).
- `phase`: task 이름. 디렉토리명과 일치시킨다.
- `steps[].step`: 0부터 시작하는 순번.
- `steps[].name`: kebab-case slug.
- `steps[].status`: 초기값은 모두 `"pending"`.

## 3. 상태 전이와 자동 기록

| 전이 | 기록되는 필드 | 기록 주체 |
|------|--------------|----------|
| → `completed` | `completed_at`, `summary` | Claude/Codex 세션 (summary), `execute.py` (timestamp) |
| → `error` | `failed_at`, `error_message` | 세션 (message), `execute.py` (timestamp) |
| → `blocked` | `blocked_at`, `blocked_reason` | 세션 (reason), `execute.py` (timestamp) |

`summary` 규칙:

- step 완료 시 산출물을 한 줄로 요약한 것.
- `execute.py`가 다음 step의 프롬프트에 컨텍스트로 누적 전달한다.
- 따라서 다음 step에 유용한 정보(생성된 파일 경로, 핵심 결정 사항 등)를 담아야 한다.

`created_at`은 `execute.py`가 최초 실행 시 task 레벨에 한 번만 기록한다.
step 레벨의 `started_at`도 `execute.py`가 각 step 시작 시 자동 기록한다.
이 필드들은 모두 생성 시 넣지 않는다.

## 4. `phases/{task-name}/stepN.md`

각 step의 자기완결 지시서. 템플릿과 작성 규칙은 `step-authoring-rules.md` 참조.
