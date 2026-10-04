---
description: PureIron 일일 브리핑 — PR·CI·캘린더를 모아 우선순위 정리
---

PureIron 프로젝트에 대해 브리핑 해줘. 이 커맨드는 repo 안에 있으니 현재 repo
(PureIron)를 기준으로 한다. 우선순위 정해주고 꼭 해야할일과 오늘 꼭 안해도 될일도 분류해줘.

- 캘린더에 있는 오늘 미팅 일정을 요약해줘. (Google Calendar — 계정 전역)
- 미머지 PR을 요약해줘: `gh pr list --state open`. 각 PR의 핵심 변경·영향 범위를 한 줄씩.
- 최근 CI 상태를 확인해줘: `gh run list --limit 8`.
  - 실패 run이 있으면 `gh run view <id> --log-failed`로 로그를 분석하고,
    **CI 환경 부재(DB/Redis 없음)로 인한 실패 vs 진짜 코드 버그**를 구분해서 보고해줘.
- 로컬 미커밋 변경도 확인: `git status -s`.

## 출력 형식

```
🎯 오늘 우선순위 (AI 판단):
1. ... — 이유
2. ...
(안 해도 됨: ...)

📅 오늘 미팅:
- HH:MM 제목 (소요시간) — 핵심 안건

❌ CI 실패:
- 잡 이름 — 증상. 원인: [환경 부재 | 코드 버그]. 상세: ...
```

미팅은 고정 슬롯이니 먼저 떼어내고, 빈 시간에 들어갈 일만
**급한 것(CI 깨짐/블로커) → 중요한 것(스프린트 피처) → 하면 좋은 것(리팩토링/문서)** 순으로.

## PureIron 특이사항

- **master 직접 커밋** 워크플로 — 열린 PR이 0건일 수 있다. 그 경우 CI·캘린더 중심으로.
- CI는 `.github/workflows/ci.yml`(push 트리거: ruff·pytest·tsc). 통합/e2e 테스트는
  Postgres/Redis가 없으면 환경 사유로 실패하니, 코드 회귀와 혼동하지 말 것.
- 문서·SOT 변경은 `/wiki` 절차(load→save→build→check)를 따른다.
