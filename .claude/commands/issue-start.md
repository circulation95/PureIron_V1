---
description: Linear 이슈 자동 파이프라인 시작 (PUR-N 시작) — 규모 판정 → worktree 워커 구현 → Codex 리뷰 → 반영 (linear-issue-pipeline skill)
argument-hint: "PUR-12 [PUR-15 ...]"
---

Linear 이슈 파이프라인의 **진입점**. 절차 정본은 `skills/linear-issue-pipeline/SKILL.md`이고, 워커 프롬프트 템플릿은 `skills/linear-issue-pipeline/references/worker-specs.md`다. 이 명령은 그 절차를 시작하는 배선이다(중복 정의 아님). 사용자가 대화에서 "PUR-12 시작"이라고 말해도 같은 절차를 따른다.

인자: `$ARGUMENTS` (하나 이상의 Linear 이슈 키. 비어 있으면 어떤 이슈를 시작할지 묻는다)

## 실행

1. `skills/linear-issue-pipeline/SKILL.md`를 읽고 그 절차를 그대로 따른다.
2. 이슈마다 사전 확인 → 규모 판정(사용자에게 한 줄 보고) → 필요 시 인터뷰 → Linear 시작 표시 → 구현 워커 시작.
3. 모든 구현 워커를 띄운 뒤 백그라운드 대기로 들어가고, 완료 알림마다 SKILL.md `메시지 라우팅` 표대로 다음 워커를 띄운다.
4. 머지는 하지 않는다. 이슈마다 merge-ready PR과 결과를 보고하고 끝낸다.
