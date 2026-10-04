# run_scheduled_triage.ps1 — headless daily log triage runner (gh-log-triage-pr)
#
# Invoked by Windows Task Scheduler (see references/headless-schedule.md).
# Safety model: capability-layer tool allow/deny sets pinned below.
# Unattended runs may create GitHub ISSUES only (in -Mode issue); PRs,
# pushes and commits are denied at the tool layer, not just by prompt.
#
#   -Mode report : analyze + write report only, no GitHub writes (default; burn-in)
#   -Mode issue  : additionally allow gh issue create/comment (post-burn-in)
#   -DryRun      : smoke test — no GitHub writes at all, also attempts
#                  denied actions to verify the permission boundary holds

param(
    [ValidateSet("report", "issue")]
    [string]$Mode = "report",
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"

# repo root = three levels up from this script (skills/gh-log-triage-pr/scripts/)
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.Parent.FullName
Set-Location $RepoRoot

$OutDir = Join-Path $RepoRoot ".omc\state\log-triage"   # NOT a collection path (self-ingestion guard)
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force $OutDir | Out-Null }
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$RunLog = Join-Path $OutDir "log-triage-$Stamp.log"
$Sentinel = Join-Path $OutDir "LAST_FAILURE.txt"

function Fail([string]$Reason, [int]$Code) {
    "$(Get-Date -Format o) FAILURE: $Reason" | Out-File -FilePath $Sentinel -Encoding utf8
    "FAILURE: $Reason" | Out-File -FilePath $RunLog -Append -Encoding utf8
    # Best-effort health-issue comment (runner-level, not LLM). Skipped if gh itself is broken.
    try {
        $health = gh issue list --search "[log-triage] scheduler health in:title" --state open --json number --limit 1 2>$null | ConvertFrom-Json
        if ($health -and $health.Count -ge 1) {
            gh issue comment $health[0].number --body "scheduled run failed at $(Get-Date -Format o): $Reason (see $RunLog on host)" 2>$null | Out-Null
        } else {
            gh issue create --title "[log-triage] scheduler health" --label log-triage --body "Health tracking issue for the scheduled log triage run. The runner comments here on failures. First failure: $(Get-Date -Format o): $Reason" 2>$null | Out-Null
        }
    } catch {}
    exit $Code
}

# --- pre-checks -------------------------------------------------------------
git rev-parse --is-inside-work-tree *> $null
if ($LASTEXITCODE -ne 0) { Fail "not a git work tree: $RepoRoot" 2 }
gh auth status *> $null
if ($LASTEXITCODE -ne 0) { Fail "gh auth status failed (token unavailable in this session?)" 3 }

# --- tool allow/deny sets (capability boundary — keep in sync with plan) ----
# Claude Code permission rules use "Bash(prefix:*)" for prefix match and
# "Bash(exact command)" for exact match; both forms are included.
$AllowedTools = @(
    "Read", "Glob", "Grep", "Write",
    "Bash(python skills/gh-log-triage-pr/scripts/redact_logs.py:*)",
    "Bash(python skills/gh-log-triage-pr/scripts/collect_recent_logs.py:*)",
    "Bash(gh issue list:*)", "Bash(gh issue view:*)",
    "Bash(gh label list:*)", "Bash(gh label list)",
    "Bash(gh auth status:*)", "Bash(gh auth status)",
    "Bash(git status:*)", "Bash(git status)",
    "Bash(git log:*)", "Bash(git diff:*)", "Bash(git branch:*)", "Bash(git branch)",
    "Bash(docker compose logs:*)", "Bash(docker compose ps:*)", "Bash(docker compose ps)"
)
if ($Mode -eq "issue" -and -not $DryRun) {
    $AllowedTools += @("Bash(gh issue create:*)", "Bash(gh issue comment:*)", "Bash(gh label create:*)")
}
$DisallowedTools = @(
    "Bash(gh pr:*)", "Bash(gh repo:*)", "Bash(gh api:*)",
    "Bash(git push:*)", "Bash(git commit:*)", "Bash(git checkout:*)",
    "Bash(git reset:*)", "Bash(git clean:*)", "Bash(git rebase:*)", "Bash(git merge:*)",
    "Edit", "NotebookEdit"
)

# --- build prompt and run ---------------------------------------------------
$PromptArgs = "--mode $Mode"
if ($DryRun) { $PromptArgs += " --dry-run" }
$Prompt = "/gh-log-triage $PromptArgs"

"$(Get-Date -Format o) starting: claude -p `"$Prompt`" (Mode=$Mode DryRun=$DryRun)" | Out-File -FilePath $RunLog -Encoding utf8

# Capture output and append as UTF-8 (PS 5.1 *>> writes UTF-16 and mangles
# native stderr into NativeCommandError records).
$Output = & claude -p $Prompt `
    --allowedTools ($AllowedTools -join ",") `
    --disallowedTools ($DisallowedTools -join ",") 2>&1
$Output | ForEach-Object { "$_" } | Out-File -FilePath $RunLog -Append -Encoding utf8

if ($LASTEXITCODE -ne 0) { Fail "claude exited with code $LASTEXITCODE" 4 }

"$(Get-Date -Format o) completed OK" | Out-File -FilePath $RunLog -Append -Encoding utf8
exit 0
