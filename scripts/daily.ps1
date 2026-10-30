<#
  HOME RADAR 매일 수집 — Windows 작업 스케줄러용.

  등록 (관리자 권한 PowerShell 에서 한 번만):

    $a = New-ScheduledTaskAction -Execute "powershell.exe" `
         -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\minkkk\home-radar\scripts\daily.ps1"
    $t = New-ScheduledTaskTrigger -Daily -At 08:40
    Register-ScheduledTask -TaskName "HOME RADAR 수집" -Action $a -Trigger $t `
         -Description "청약 공고 수집 + 마감 알림 로그"

  해제:
    Unregister-ScheduledTask -TaskName "HOME RADAR 수집" -Confirm:$false

  로그는 logs\daily-YYYY-MM-DD.log 에 쌓인다.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
$logDir = Join-Path $root 'logs'

if (-not (Test-Path $py)) { throw "가상환경이 없습니다: $py" }
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$log = Join-Path $logDir ("daily-" + (Get-Date -Format 'yyyy-MM-dd') + ".log")
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

Set-Location $root
"==== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 수집 시작 ====" | Out-File $log -Append -Encoding utf8

try {
    & $py -m app refresh      | Out-File $log -Append -Encoding utf8
    & $py -m app show         | Out-File $log -Append -Encoding utf8
    "==== 정상 종료 ====" | Out-File $log -Append -Encoding utf8
}
catch {
    "!!!! 실패: $_" | Out-File $log -Append -Encoding utf8
    exit 1
}
