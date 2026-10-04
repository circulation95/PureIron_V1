---
name: gh-log-triage-pr
description: This skill should be used when the user asks to triage recent project logs into GitHub issues or pull requests — for example "최근 로그 점검해줘", "로그 보고 이슈 등록해줘", "recent logs triage", "로그에서 문제 찾아서 GitHub issue 만들어줘", "로그 문제 정리하고 PR 열어줘", or when the scheduled headless triage run (`/gh-log-triage`) executes. It collects recent logs, classifies real problems by severity, deduplicates by signature, and files GitHub issues (and, in interactive mode only, opens PRs).
---

# GitHub Log Triage

최근 프로젝트 로그를 수집·점검하고, 실제 문제를 분류·중복 제거한 뒤 GitHub Issue로 등록하는 워크플로. 상세 기준은 references에 있다:

- 심각도(P0~P3)·signature·dedupe·noise 기준: `references/severity-and-dedupe.md`
- Issue/PR 템플릿·label·중복 처리: `references/github-issue-pr-template.md`
- 매일 9시 무인 실행·Task Scheduler·안전 정책: `references/headless-schedule.md`

## 모드

| | 대화형 (interactive) | 무인 (headless, `/gh-log-triage` 경유) |
|---|---|---|
| 진입 | 사용자 자연어 요청 | Task Scheduler → `scripts/run_scheduled_triage.ps1` → `claude -p "/gh-log-triage ..."` |
| Issue 게시 | redacted 초안을 사용자에게 보여주고 **승인받은 뒤에만** | redact 통과 + 명확한 증거 + 비중복의 3조건 충족 시 자동 (`--mode issue`일 때만) |
| PR | 사용자가 명시적으로 지시한 경우에만 | **절대 금지** |
| 결과 | 대화 내 보고 | `.omc/state/log-triage/` 보고서 파일 필수 |

## 절차

스크립트는 항상 repo root 기준 상대경로로 호출한다(cwd = repo root 전제).

1. **repo 상태 확인** — `git status --short`, 현재 branch, `gh auth status`. gh 미인증이면 issue 게시 불가를 보고하고 조사만 진행.
2. **로그 소스 탐색** — `logs/**`, `backend/**/logs/**`, `frontend/**/logs/**`, `.omc/logs/**` + docker compose 서비스가 떠 있으면 `docker compose logs --since <window>` 가용성 확인. 기간 미지정 시 기본 최근 24시간. 실제 적용한 시간 범위를 보고서·issue에 명시.
3. **수집** — `python skills/gh-log-triage-pr/scripts/collect_recent_logs.py --hours <N>` 실행. 대형 파일은 tail 기준으로 읽히며 manifest에 truncation·누락 가능성이 기록된다. 윈도우가 비어 있으면 `--list-all`로 실재 파일을 확인해 범위를 보고하고, 대화형이면 윈도우 확대 여부를 사용자에게 확인, headless면 "수집 대상 없음" 보고서를 남기고 정상 종료.
4. **문제 탐지** — ERROR, CRITICAL, traceback, exception, failed, timeout, connection refused, migration failure, auth failure, 5xx, unhandled rejection, TypeScript/build failure, pytest failure를 우선 탐지. 단순 경고·개발 서버 재시작·사용자 취소·알려진 harmless noise는 별도 섹션으로 분리(기준: `references/severity-and-dedupe.md`).
5. **중복 제거** — 같은 stack trace, 같은 endpoint+status, 같은 exception class+message, 같은 failing test를 하나의 signature로 묶는다. signature hash를 계산하고 기존 issue와 **로컬 매칭**(`gh issue list --label log-triage --state all --limit 200 --json number,title,body`). 중복이면 새 issue 대신 기존 issue에 코멘트(throttle 규칙 적용).
6. **심각도 분류** — P0(데이터 손상·보안·전체 중단) / P1(핵심 기능·인증·DB·API 지속 실패) / P2(특정 기능 장애·복구 가능) / P3(경고·개발 편의·추후 조사).
7. **게시** — issue 본문에 넣을 로그 발췌는 반드시 `python skills/gh-log-triage-pr/scripts/redact_logs.py`를 통과시킨다(JSONL은 `--jsonl`). redaction은 best-effort 필터이며 안전 보장이 아니다. 대화형: redacted 초안 승인 후 게시. headless: 3조건(redact 통과·명확한 증거·비중복) 충족 시에만 `--mode issue`에서 자동 게시, `--mode report`면 보고서만.
8. **PR(대화형 전용)** — 사용자가 명시적으로 지시한 경우에만 별도 branch에서 draft PR을 연다. 템플릿은 `references/github-issue-pr-template.md`.
9. **안전 규칙**
   - secret 원문을 issue·PR·commit·문서에 절대 쓰지 않는다.
   - 확실하지 않은 원인은 단정하지 말고 "추정"으로 표시한다.
   - transient noise를 issue로 남발하지 않는다. 실제 문제 증거가 없으면 issue 대신 조사 보고서만 남긴다.
   - destructive command 금지.
   - 대화형에서 GitHub write action(issue/comment/PR)은 사용자의 명시 지시가 있을 때만 수행한다.
