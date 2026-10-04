# 무인(headless) 스케줄 실행 — Task Scheduler

매일 09:00 무인 log triage 실행의 정본 문서. 진입 체인:

```
Windows Task Scheduler (daily 09:00)
  → skills/gh-log-triage-pr/scripts/run_scheduled_triage.ps1 [-Mode report|issue]
    → claude -p "/gh-log-triage --mode <report|issue>"   (결정적 커맨드 진입점, .claude/commands/gh-log-triage.md)
      → SKILL.md 절차 (headless 모드)
```

## 등록 / 확인 / 해제

repo 경로는 실제 위치로 치환한다 (예: `C:\PureIron_ignition`).

```powershell
# 등록 (초기에는 반드시 -Mode report — 아래 burn-in 참조)
schtasks /Create /TN "PureIron-LogTriage" /SC DAILY /ST 09:00 `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\PureIron_ignition\skills\gh-log-triage-pr\scripts\run_scheduled_triage.ps1 -Mode report"

# 확인
schtasks /Query /TN "PureIron-LogTriage" /V /FO LIST

# 수동 1회 실행 (스케줄 컨텍스트 검증)
schtasks /Run /TN "PureIron-LogTriage"

# 해제
schtasks /Delete /TN "PureIron-LogTriage" /F
```

### 실행 계정 정책

- **로그인 사용자 계정으로 등록한다**(`/RU`/`/RP` 미지정 = 현재 사용자, logged-on-only). SYSTEM 계정으로 돌리면 gh 토큰(사용자 keyring)을 찾지 못한다.
- 이 구성은 **logged-on-only**다: 9시에 PC가 꺼져 있거나 로그아웃 상태면 그 날은 실행되지 않는다(의도된 tradeoff).
- `schtasks /Run` 검증은 현재 잠금 해제된 세션에서 도는 것이므로, **화면 잠금 상태의 keyring 접근성까지 재현하지는 못한다**. 첫 실제 9시 실행 결과를 확인할 것.
- `-ExecutionPolicy Bypass`는 이 로컬 스크립트 1개에 대한 실행 허용이다(광범위 정책 변경 아님).

## 안전 정책 (headless)

1. **Issue만, PR 금지.** 무인 실행은 `gh issue create/comment/list/view`와 `gh label`만 쓴다. `gh pr *`, `git push`, `git commit`은 도구 deny-set으로 차단된다(프롬프트 지시가 아니라 **능력 계층에서 강제**).
2. **3중 게시 조건**: redact 스크립트 통과 + 명확한 문제 증거 + signature 비중복일 때만 issue 자동 생성. 하나라도 미충족이면 보고서만.
3. **Repo private 전제.** 무인 자동 게시는 이 repo가 private임을 전제로 승인됐다. 단, private repo라도 issue에 게시된 내용은 collaborator 화면을 넘어 **알림 이메일, REST API, GitHub 검색**으로도 전파된다 — "collaborator 한정"보다 넓은 표면임을 인지할 것. **repo를 public으로 전환하면 이 정책을 재검토해야 한다.**
4. **Prompt-injection 표면**: headless LLM은 redaction 전 원본 로그를 읽으므로, 로그 내용에 심긴 지시문이 모델을 조종하려는 시도가 이론적으로 가능하다. deny-set(no push/pr/commit)이 그 영향 반경을 "private repo에 가짜 issue 1건"으로 한정하는 핵심 통제다.
5. 보고서·러너 로그는 `.omc/state/log-triage/`에 쓴다. 이 경로는 수집 대상(`.omc/logs/**` 등)이 **아니며**, `collect_recent_logs.py`도 자기 출력(`log-triage-*`)을 하드코딩으로 제외한다 — 어제 러너 출력을 오늘 다시 triage하는 자기 재수집 루프 방지.

## 시간 윈도우

- 기본: 직전 24시간(mtime 기준). 실제 적용 범위를 보고서와 issue 본문에 기록한다.
- 윈도우가 비면 "수집 대상 없음" 보고서를 남기고 정상 종료한다(오류 아님).

## 실패 시 동작

러너는 실패(사전 점검 실패, claude 비정상 종료)를 다음 순서로 알린다:

1. `.omc/state/log-triage/log-triage-<ts>.log`에 전체 출력 기록
2. `.omc/state/log-triage/LAST_FAILURE.txt` sentinel 갱신 (**gh 무의존 최후 신호** — gh auth 자체가 죽어도 남음)
3. 고정 health issue(제목 `[log-triage] scheduler health`)에 코멘트 시도(러너 레벨 PowerShell, LLM 아님; health issue가 없으면 생성)
4. 비정상 종료코드 반환(Task Scheduler 이력에 기록됨)

## Burn-in 롤아웃 (필수)

signature 안정성이 확인되기 전에는 issue 자동 생성을 켜지 않는다:

1. **초기 등록은 `-Mode report`** — issue를 만들지 않고 보고서만 남긴다.
2. 수일(권장 3일+) 동안 `.omc/state/log-triage/` 보고서에서 **같은 오류가 같은 signature로 안정적으로 잡히는지** 확인한다. 매일 signature가 바뀌면 정규화 규칙(`severity-and-dedupe.md`)을 보강한다.
3. 전환 전에 `run_scheduled_triage.ps1 -DryRun`을 한 번 돌려 보고서의 `permission-boundary` 섹션에서 **단독 `gh issue list`가 허용**되는지 확인한다(차단이면 issue 모드의 dedupe 조회·issue 생성이 불가).
4. 안정 확인 후 사용자 확인을 받아 issue 모드로 전환:

```powershell
schtasks /Delete /TN "PureIron-LogTriage" /F
schtasks /Create /TN "PureIron-LogTriage" /SC DAILY /ST 09:00 `
  /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\PureIron_ignition\skills\gh-log-triage-pr\scripts\run_scheduled_triage.ps1 -Mode issue"
```
