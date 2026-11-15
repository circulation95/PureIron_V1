#Requires -RunAsAdministrator
<#
.SYNOPSIS
  e2e 호스트 1회 준비(PD-24): Hyper-V Administrators 그룹, 내부 전용 vSwitch, 호스트 쪽 IP.

.DESCRIPTION
  관리자 PowerShell에서 한 번만 실행한다. 이미 된 단계는 건너뛴다(재실행 안전).
  1. 현재 사용자를 Hyper-V Administrators(SID S-1-5-32-578)에 추가 — 이후 로그아웃·로그인 필요.
  2. 내부 전용(Internal) vSwitch `pureiron-int` — 외부 NIC에 연결되지 않아 VM은 인터넷에 닿지 않는다.
  3. 호스트 vNIC `vEthernet (pureiron-int)`에 10.77.0.1/24 — VM(10.77.0.x)과 SSH·번들 복사용.
     호스트는 이 망을 다른 곳으로 전달(라우팅·NAT)하지 않는다.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tests\e2e\infra\setup-host.ps1
#>
param(
    [string]$User = "$env:USERDOMAIN\$env:USERNAME",
    [string]$Switch = "pureiron-int",
    [string]$HostIp = "10.77.0.1",
    [int]$PrefixLength = 24
)
$ErrorActionPreference = "Stop"

$group = Get-LocalGroup -SID "S-1-5-32-578"
$member = Get-LocalGroupMember -Group $group -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ieq $User }
if ($member) {
    Write-Host "[1] $User is already in $($group.Name)"
} else {
    Add-LocalGroupMember -Group $group -Member $User
    Write-Host "[1] added $User to $($group.Name) — sign out and sign in again before running e2e"
}

if (Get-VMSwitch -Name $Switch -ErrorAction SilentlyContinue) {
    Write-Host "[2] vSwitch $Switch exists"
} else {
    New-VMSwitch -Name $Switch -SwitchType Internal | Out-Null
    Write-Host "[2] created internal vSwitch $Switch"
}

$alias = "vEthernet ($Switch)"
if (Get-NetIPAddress -InterfaceAlias $alias -IPAddress $HostIp -ErrorAction SilentlyContinue) {
    Write-Host "[3] $alias already has $HostIp"
} else {
    New-NetIPAddress -InterfaceAlias $alias -IPAddress $HostIp -PrefixLength $PrefixLength | Out-Null
    Write-Host "[3] assigned $HostIp/$PrefixLength to $alias"
}
