<#
.SYNOPSIS
  G2 nightly 시작(M6-3 ★H13) — Windows 작업 스케줄러가 매일 03:17(이 PC 로컬 시각 = KST)에
  `gh workflow run nightly.yml`을 부르게 등록한다. 일요일은 suite=weekly, 나머지는 g2.
  둘째 작업(M6-4a P9): 매일 00:47에 `gh workflow run foundation-ci.yml --ref master`(G1 밤 전체 실행 —
  조건부 job 포함). foundation-ci.yml의 GitHub schedule을 지웠다(늦게 만들어져 03:17 G2와 겹쳤다).

.DESCRIPTION
  GitHub schedule 이벤트는 이 저장소에서 매번 3시간 30분~7시간 30분 늦게 만들어져(nightly.yml에서
  cron을 지웠다) 이 PC의 작업 스케줄러가 제시간에 workflow_dispatch를 부른다. 세 가지 모드:

  등록(기본)     이 스크립트를 <E2E 루트>\bin\nightly-dispatch.ps1로 복사하고(브랜치를 바꿔도 작업이
                 깨지지 않게) 그 사본을 -Dispatch로 부르는 작업을 만든다. 조건: 사용자 로그온
                 세션에서만(gh는 Windows 자격 증명 관리자 keyring의 토큰을 쓴다), PC가 꺼져 있어
                 놓친 예약은 나중에 실행하지 않음, 동시에 둘 이상 실행하지 않음, 10분 제한.
  -Unregister    두 작업을 지운다(복사한 사본·로그는 남긴다).
  -Dispatch      작업이 부르는 모드: 요일로 suite를 고르고 gh workflow run을 부른다. 실행 기록은
                 <E2E 루트>\logs\nightly-dispatch.log에 한 줄씩(시각·suite·명령·종료 코드·gh 출력)
                 남긴다. 토큰은 이 프로세스 환경변수(GH_TOKEN)에만 두고 파일·로그에 쓰지 않는다.
                 -WhatIf면 gh를 부르지 않고 고른 suite와 명령만 출력한다(-Date로 날짜를 바꿔 본다).
                 gh가 실패하면(로그인 안 됨·keyring 잠김·네트워크·HTTP 4xx) 종료 코드와 출력 끝부분을
                 로그 한 줄로 남기고 0이 아닌 코드로 끝난다(작업 스케줄러 "마지막 실행 결과").

  -DispatchCi    둘째 작업이 부르는 모드: gh workflow run foundation-ci.yml --ref <Ref>. 로그·토큰·
                 -WhatIf 규칙은 -Dispatch와 같다(로그 줄은 workflow=foundation-ci.yml).

  -Gh            gh 실행 파일 이름·경로(기본 gh). 실패 경로를 가짜 명령으로 확인할 때 쓴다.

  <E2E 루트> = 환경변수 PUREIRON_E2E_ROOT, 없으면 C:\PureIron_e2e(nightly.py와 같다).

.EXAMPLE
  # 등록(사용자 계정 PowerShell에서 1회 — 관리자 권한 필요 없음)
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1

.EXAMPLE
  # 요일 → suite 확인(gh를 부르지 않는다)
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1 -Dispatch -WhatIf -Date 2026-11-15

.EXAMPLE
  # foundation-ci 밤 실행 명령 확인(gh를 부르지 않는다)
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\register-nightly.ps1 -DispatchCi -WhatIf

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
    [string]$CiAt = '00:47',
    [string]$CiTaskName = 'PureIron foundation-ci nightly',
    [string]$Gh = 'gh',
    [switch]$Dispatch,
    [switch]$DispatchCi,
    [switch]$Unregister,
    [switch]$WhatIf,
    [datetime]$Date = (Get-Date)
)

$ErrorActionPreference = 'Stop'
$Workflow = 'nightly.yml'
$CiWorkflow = 'foundation-ci.yml'
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

function Get-CiDispatchArgs {
    return @('workflow', 'run', $CiWorkflow, '--ref', $Ref, '-R', $Repo)
}

function Write-DispatchLog([string]$Line) {
    $logs = Join-Path (Get-E2ERoot) 'logs'
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    $stamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    Add-Content -Path (Join-Path $logs 'nightly-dispatch.log') -Value "$stamp $Line" -Encoding UTF8
}

function Get-Tail([string[]]$Lines) {
    # 로그 한 줄용: 마지막 5줄을 공백 하나로 잇고 400자로 자른다.
    $flat = ((@($Lines) | Select-Object -Last 5) -join ' ' -replace '\s+', ' ').Trim()
    if ($flat.Length -gt 400) { $flat = '...' + $flat.Substring($flat.Length - 400) }
    return $flat
}

function Invoke-Gh([string[]]$GhArgs) {
    # PS 5.1에서 $ErrorActionPreference = 'Stop'이면 네이티브 명령의 stderr 줄이 `2>&1`·`2>$null`
    # 어느 쪽이든 RemoteException 예외가 되어(M6-3b 리뷰 M1) 실패한 밤의 로그 줄이 남지 않는다. 그래서
    # gh는 이 함수에서만, 지역 Continue로 부르고 성패는 종료 코드로만 판단한다. stdout(토큰일 수
    # 있다)과 stderr를 따로 돌려준다 — stderr 줄은 ErrorRecord로 온다.
    $ErrorActionPreference = 'Continue'
    if (-not (Get-Command $Gh -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ Code = 127; Out = @(); Err = @("command not found: $Gh") }
    }
    $out = New-Object System.Collections.Generic.List[string]
    $err = New-Object System.Collections.Generic.List[string]
    try {
        & $Gh @GhArgs 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $err.Add("$_") } else { $out.Add("$_") }
        }
        $code = $LASTEXITCODE
    } catch {
        $err.Add("$($_.Exception.Message)")
        $code = 1
    }
    return [pscustomobject]@{ Code = $code; Out = $out.ToArray(); Err = $err.ToArray() }
}

function Invoke-Dispatch([switch]$Ci) {
    if ($Ci) {
        $label = "workflow=$CiWorkflow"
        $ghArgs = Get-CiDispatchArgs
    } else {
        $suite = Get-Suite $Date
        $label = "suite=$suite"
        $ghArgs = Get-DispatchArgs $suite
    }
    $shown = "gh $($ghArgs -join ' ')"
    if ($WhatIf) {
        # Write-Host: 함수의 출력(Write-Output)은 반환값이 되어 `exit (Invoke-Dispatch)`에 삼켜진다
        Write-Host "date=$($Date.ToString('yyyy-MM-dd')) day=$($Date.DayOfWeek) $label"
        Write-Host $shown
        return 0
    }
    # gh 계정의 토큰을 이 프로세스에만 둔다(파일·로그·명령 줄에 남기지 않는다). 실패 줄에는 stderr만
    # 쓴다 — stdout은 토큰이다.
    $auth = Invoke-Gh @('auth', 'token', '-u', $GhUser)
    $token = ($auth.Out -join '').Trim()
    if ($auth.Code -ne 0 -or -not $token) {
        $code = if ($auth.Code -ne 0) { $auth.Code } else { 1 }
        Write-DispatchLog "$label FAILED: no gh token for $GhUser exit=$code (gh auth login, user logon session) :: $(Get-Tail $auth.Err)"
        return $code
    }
    $env:GH_TOKEN = $token
    Remove-Variable token, auth
    try {
        $run = Invoke-Gh $ghArgs
    } finally {
        Remove-Item Env:\GH_TOKEN -ErrorAction SilentlyContinue
    }
    $status = if ($run.Code -eq 0) { 'ok' } else { 'FAILED' }
    Write-DispatchLog "$label $status exit=$($run.Code) $shown :: $(Get-Tail (@($run.Out) + @($run.Err)))"
    return $run.Code
}

function Register-One([string]$Name, [string]$Time, [string]$Mode, [string]$Copy, [string]$Bin, [string]$Text) {
    $arguments = @(
        '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$Copy`"",
        $Mode, '-Repo', $Repo, '-GhUser', $GhUser, '-Ref', $Ref, '-Gh', "`"$Gh`""
    ) -join ' '
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $Bin
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
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
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description $Text -Force | Out-Null
    Write-Output "registered '$Name': daily $Time (local time), $Repo@$Ref as $GhUser, script $Copy $Mode"
}

function Register-Nightly {
    $root = Get-E2ERoot
    $bin = Join-Path $root 'bin'
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $copy = Join-Path $bin 'nightly-dispatch.ps1'
    Copy-Item -Path $PSCommandPath -Destination $copy -Force
    Register-One $TaskName $At '-Dispatch' $copy $bin "G2 nightly: gh workflow run $Workflow (Sunday = weekly)"
    Register-One $CiTaskName $CiAt '-DispatchCi' $copy $bin "G1 nightly: gh workflow run $CiWorkflow (full run incl. conditional jobs)"
    Write-Output "log: $(Join-Path $root 'logs\nightly-dispatch.log')"
    return 0
}

if ($Unregister) {
    foreach ($name in @($TaskName, $CiTaskName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Output "unregistered '$name'"
        } else {
            Write-Output "no task '$name'"
        }
    }
    exit 0
}
if ($Dispatch) {
    exit (Invoke-Dispatch)
}
if ($DispatchCi) {
    exit (Invoke-Dispatch -Ci)
}
exit (Register-Nightly)
