<#
.SYNOPSIS
  G2 nightly 시작(M6-3 ★H13) — Windows 작업 스케줄러가 매일 03:17(이 PC 로컬 시각 = KST)에
  `gh workflow run nightly.yml`을 부르게 등록한다. 일요일은 suite=weekly, 나머지는 g2.

.DESCRIPTION
  GitHub schedule 이벤트는 이 저장소에서 매번 3시간 30분~7시간 30분 늦게 만들어져(nightly.yml에서
  cron을 지웠다) 이 PC의 작업 스케줄러가 제시간에 workflow_dispatch를 부른다. 세 가지 모드:

  등록(기본)     이 스크립트를 <E2E 루트>\bin\nightly-dispatch.ps1로 복사하고(브랜치를 바꿔도 작업이
                 깨지지 않게) 그 사본을 -Dispatch로 부르는 작업을 만든다. 조건: 사용자 로그온
                 세션에서만(gh는 Windows 자격 증명 관리자 keyring의 토큰을 쓴다), PC가 꺼져 있어
                 놓친 예약은 나중에 실행하지 않음, 동시에 둘 이상 실행하지 않음, 10분 제한.
  -Unregister    작업을 지운다(복사한 사본·로그는 남긴다).
  -Dispatch      작업이 부르는 모드: 요일로 suite를 고르고 gh workflow run을 부른다. 실행 기록은
                 <E2E 루트>\logs\nightly-dispatch.log에 한 줄씩(시각·suite·명령·종료 코드·gh 출력)
                 남긴다. 토큰은 이 프로세스 환경변수(GH_TOKEN)에만 두고 파일·로그에 쓰지 않는다.
                 -WhatIf면 gh를 부르지 않고 고른 suite와 명령만 출력한다(-Date로 날짜를 바꿔 본다).

  <E2E 루트> = 환경변수 PUREIRON_E2E_ROOT, 없으면 C:\PureIron_e2e(nightly.py와 같다).

.EXAMPLE
  # 등록(사용자 계정 PowerShell에서 1회 — 관리자 권한 필요 없음)
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1

.EXAMPLE
  # 요일 → suite 확인(gh를 부르지 않는다)
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1 -Dispatch -WhatIf -Date 2026-11-15

.EXAMPLE
  # 등록 해제
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$Repo = 'circulation95/PureIron_V1',
    [string]$GhUser = 'circulation95',
    [string]$Ref = 'master',
    [string]$At = '03:17',
    [string]$TaskName = 'PureIron G2 nightly',
    [switch]$Dispatch,
    [switch]$Unregister,
    [switch]$WhatIf,
    [datetime]$Date = (Get-Date)
)

$ErrorActionPreference = 'Stop'
$Workflow = 'nightly.yml'
$WeeklyDay = [System.DayOfWeek]::Sunday
$TimeLimitMinutes = 10

function Get-E2ERoot {
    if ($env:PUREIRON_E2E_ROOT) { return $env:PUREIRON_E2E_ROOT }
    return 'C:\PureIron_e2e'
}

function Get-Suite([datetime]$Day) {
    # 일요일 03:17 KST = 주간 묶음(평일 시험 전부 + crash 16건), 나머지 날 = 평일 묶음.
    if ($Day.DayOfWeek -eq $WeeklyDay) { return 'weekly' }
    return 'g2'
}

function Get-DispatchArgs([string]$Suite) {
    return @('workflow', 'run', $Workflow, '--ref', $Ref, '-R', $Repo, '-f', "ref=$Ref", '-f', "suite=$Suite")
}

function Write-DispatchLog([string]$Line) {
    $logs = Join-Path (Get-E2ERoot) 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $stamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    Add-Content -Path (Join-Path $logs 'nightly-dispatch.log') -Value "$stamp $Line" -Encoding UTF8
}

function Invoke-Dispatch {
    $suite = Get-Suite $Date
    $ghArgs = Get-DispatchArgs $suite
    $shown = "gh $($ghArgs -join ' ')"
    if ($WhatIf) {
        Write-Output "date=$($Date.ToString('yyyy-MM-dd')) day=$($Date.DayOfWeek) suite=$suite"
        Write-Output $shown
        return 0
    }
    # gh 계정의 토큰을 이 프로세스에만 둔다(파일·로그·명령 줄에 남기지 않는다).
    $token = & gh auth token -u $GhUser 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $token) {
        Write-DispatchLog "suite=$suite FAILED: no gh token for $GhUser (gh auth login, user logon session)"
        return 1
    }
    $env:GH_TOKEN = ($token | Out-String).Trim()
    Remove-Variable token
    try {
        $out = & gh @ghArgs 2>&1 | Out-String
        $code = $LASTEXITCODE
    } finally {
        Remove-Item Env:\GH_TOKEN -ErrorAction SilentlyContinue
    }
    $flat = ($out -replace '\s+', ' ').Trim()
    Write-DispatchLog "suite=$suite exit=$code $shown :: $flat"
    return $code
}

function Register-Nightly {
    $root = Get-E2ERoot
    $bin = Join-Path $root 'bin'
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $copy = Join-Path $bin 'nightly-dispatch.ps1'
    Copy-Item -Path $PSCommandPath -Destination $copy -Force
    $arguments = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$copy`"",
        '-Dispatch', '-Repo', $Repo, '-GhUser', $GhUser, '-Ref', $Ref
    ) -join ' '
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $bin
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    # 로그온 세션에서만(gh keyring) — 암호를 저장하지 않는 Interactive 작업.
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    # StartWhenAvailable=false: PC가 꺼져 있어 놓친 03:17을 켠 뒤 실행하지 않는다(낮에 VM 2대를
    # 띄우지 않게 — 지운 늦은 시작 가드의 역할).
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitMinutes) `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    $settings.StartWhenAvailable = $false
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description "G2 nightly: gh workflow run $Workflow (Sunday = weekly)" -Force | Out-Null
    Write-Output "registered '$TaskName': daily $At (local time), $Repo@$Ref as $GhUser, script $copy"
    Write-Output "log: $(Join-Path $root 'logs\nightly-dispatch.log')"
    return 0
}

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "unregistered '$TaskName'"
    } else {
        Write-Output "no task '$TaskName'"
    }
    exit 0
}
if ($Dispatch) {
    exit (Invoke-Dispatch)
}
exit (Register-Nightly)
