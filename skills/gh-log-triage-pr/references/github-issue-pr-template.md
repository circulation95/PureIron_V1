# GitHub Issue / PR 템플릿과 절차

`gh-log-triage-pr` skill의 게시 형식 정본. **headless 모드에서 PR 섹션은 사용 금지** — PR은 대화형에서 사용자가 명시 지시한 경우에만.

## Label

`log-triage`, `bug`, `needs-investigation`, `P0`/`P1`/`P2`/`P3` 중 해당 등급 1개.

label이 없으면 최초 1회 생성한다:

```bash
gh label create log-triage --color 5319e7 --description "automated log triage" --force
gh label create needs-investigation --color fbca04 --description "cause not confirmed" --force
gh label create P0 --color b60205 --force
gh label create P1 --color d93f0b --force
gh label create P2 --color e99695 --force
gh label create P3 --color c2e0c6 --force
```

## Issue 제목

```
[log-triage][P1] 짧은 문제 요약
```

## Issue 본문 템플릿

signature marker는 **redaction이 끝난 본문에** 템플릿 단계에서 부착한다(redactor를 통과시키지 않는다 — hash는 non-secret 입력만 사용).

```markdown
<!-- log-triage-signature: <hash12> -->

## 발생 시간 범위
<정확한 시작~끝, 타임존 포함>

## 로그 출처
<파일 경로 목록 + docker compose 서비스명. tail로 읽은 파일은 truncation 사실 명시>

## 관찰된 증상
<재현 절차 또는 로그상 관찰 사실만. 추측 금지>

## 로그 발췌 (redacted)
```text
<scripts/redact_logs.py 통과본만. best-effort 필터이므로 붙여넣기 전 육안 확인>
```

## 영향도
<어떤 기능/사용자가 어떻게 영향받는지>

## 추정 원인과 불확실성
<확실하지 않은 원인은 반드시 "추정"으로 표기. 배제한 가설도 기록>

## 권장 다음 조치
<조사 또는 수정 단계 제안>

---
_automated log triage (`gh-log-triage-pr`) · window: <시간 범위> · mode: <interactive|headless>_
```

```bash
gh issue create --title "[log-triage][P1] ..." --label log-triage --label bug --label P1 --body-file <redacted-body.md>
```

## 중복 시 코멘트 절차

signature가 기존 issue와 매칭되면 (throttle 규칙은 `severity-and-dedupe.md`):

```bash
gh issue comment <number> --body "재발 확인: <시간 범위>, <발생 횟수>. 심각도 변화: <없음|P2→P1>. window: <범위>"
```

## PR 템플릿 (대화형 전용)

수정/조사 PR은 별도 branch에서 draft로 연다. base는 `master`.

- 제목: `fix(<scope>): <요약>` 또는 조사용 `chore(triage): investigate <요약>`
- 본문 필수 항목:
  - 관련 issue 링크(`Closes #N` 또는 `Refs #N`)
  - 변경/조사 내용 요약
  - 실행한 검증 명령과 실제 결과 (Testing SOT §10 형식: 명령·pass/fail·미실행 항목과 이유)
  - 남은 risk
- commit은 conventional commits + `Co-Authored-By` trailer.

**headless 모드에서는 이 섹션을 사용하지 않는다. 어떤 경우에도 무인으로 branch push·PR 생성을 하지 않는다.**
