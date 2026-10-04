---
description: Headless/deterministic entrypoint for scheduled log triage (gh-log-triage-pr skill)
argument-hint: "--mode report|issue [--dry-run] [--hours N]"
---

무인(headless) log triage의 **결정적 진입점**. 절차 정본은 `skills/gh-log-triage-pr/SKILL.md`이며, 이 명령은 그 절차를 headless 모드로 실행하는 배선이다(중복 정의 아님). Task Scheduler → `skills/gh-log-triage-pr/scripts/run_scheduled_triage.ps1` → `claude -p "/gh-log-triage ..."` 체인으로 호출된다. 대화형에서 직접 호출해도 된다.

인자: `$ARGUMENTS` (`--mode report|issue`, `--dry-run`, `--hours N`; 기본 `--mode report`, 24시간)

## 실행 규칙 (headless)

`skills/gh-log-triage-pr/SKILL.md`의 절차 1~7단계를 그대로 따르되, headless 모드 규칙을 적용한다:

1. **PR·push·commit 절대 금지.** 도구 deny-set으로도 차단되어 있지만 시도 자체를 하지 않는다.
2. `--mode report`(기본): GitHub write 전무. 분석 결과를 보고서로만 남긴다.
3. `--mode issue`: `references/severity-and-dedupe.md`의 3조건(redact 스크립트 통과 + 명확한 증거 + signature 비중복)을 충족한 문제만 issue 자동 생성. 중복은 throttle 규칙에 따라 코멘트. 증거가 불충분하면 issue 없이 보고서만.
4. **보고서 필수**: 실행 결과를 `.omc/state/log-triage/report-<YYYYMMDD-HHmmss>.md`에 Write로 저장한다. 포함 항목: 적용한 시간 윈도우, 수집 manifest 요약(truncation·누락 포함), 발견 문제(signature·심각도·처리 결과), noise 섹션, 생성/코멘트한 issue 번호, 미게시 사유.
5. 로그 발췌를 보고서·issue에 넣기 전에 반드시 `python skills/gh-log-triage-pr/scripts/redact_logs.py`를 통과시킨다(JSONL은 `--jsonl`).
6. 수집이 비어 있으면 "수집 대상 없음" 보고서를 남기고 정상 종료한다.

## --dry-run (스모크 전용)

`--dry-run`이 있으면 GitHub write를 전면 금지하고, 추가로 **권한 경계 검증**을 수행해 결과를 보고서에 기록한다:

1. Bash로 `gh pr list` 실행을 시도한다 — **차단(권한 거부)이 정상**이다.
2. Bash로 체인 명령 `gh issue list --limit 1; git push --dry-run` 실행을 시도한다 — **차단이 정상**이다.
3. Bash로 단독 `gh issue list --limit 1` 실행을 시도한다 — read-only이므로 **허용이 정상**이다(차단되면 issue 모드의 dedupe 조회가 불가능하다는 뜻 — FAIL로 기록).
4. 세 시도의 실제 결과(차단됨/실행됨)를 보고서의 `permission-boundary` 섹션에 기록한다. 기대와 다르면 FAIL로 명시한다.
5. 수집·탐지·분류는 정상 수행하되 게시는 하지 않는다.
