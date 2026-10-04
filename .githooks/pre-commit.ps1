# PureIron Wiki pre-commit hook (PowerShell 보조 — Windows 네이티브 수동 실행/디버그용).
#
# 정책: generated 파일을 갱신하되 자동 stage 하지 않는다.
#       변경이 생기면 커밋을 중단하고 사용자가 직접 `git add` 후 재커밋한다.
#
# 수동 실행: powershell -ExecutionPolicy Bypass -File .githooks/pre-commit.ps1

$ErrorActionPreference = "Stop"

$py = (Get-Command python -ErrorAction SilentlyContinue)
if ($null -eq $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue) }
if ($null -eq $py) { $py = (Get-Command py -ErrorAction SilentlyContinue) }
if ($null -eq $py) {
    Write-Error "pre-commit: python 을 찾을 수 없습니다 (PATH 확인)."
    exit 1
}

& $py.Source scripts/wiki.py build
if ($LASTEXITCODE -ne 0) { exit 1 }
& $py.Source scripts/wiki.py check
if ($LASTEXITCODE -ne 0) { exit 1 }

git diff --quiet -- docs/wiki/_generated docs/LLM_WIKI.md
if ($LASTEXITCODE -ne 0) {
    Write-Host "pre-commit: 생성물이 갱신되었습니다. 아래를 stage 한 뒤 다시 커밋하세요:"
    Write-Host "  git add docs/wiki/_generated docs/LLM_WIKI.md"
    exit 1
}

exit 0
