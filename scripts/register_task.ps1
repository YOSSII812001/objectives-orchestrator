# Register Objectives Orchestrator as hourly Windows Scheduled Task
# Usage: powershell .\scripts\register_task.ps1

$ErrorActionPreference = "Stop"

$TaskName = "ObjectivesOrchestrator"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$BatPath = Join-Path $ScriptDir "run_hourly.bat"

if (-not (Test-Path $BatPath)) {
    Write-Error "run_hourly.bat not found: $BatPath"
    exit 1
}

# Overwrite if existing (-Force)
$Action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $ProjectDir

# Run at the top of every hour. First trigger is the next 00 minute.
$Now = Get-Date
$StartAt = $Now.Date.AddHours($Now.Hour + 1)
$Trigger = New-ScheduledTaskTrigger -Once -At $StartAt `
           -RepetitionInterval (New-TimeSpan -Hours 1)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 55)

# LogonType S4U = "run whether the user is logged on or not" WITHOUT storing a
# password. The task then runs in a non-interactive session, so NO console
# window pops up every hour. It can still reach LM Studio on localhost.
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal `
    -Description "Objectives Orchestrator hourly run (silent)" -Force | Out-Null

Write-Host "[OK] Task registered: $TaskName"
Write-Host "  Start at : $StartAt"
Write-Host "  Interval : 1 hour"
Write-Host "  Action   : $BatPath"
Write-Host "  Mode     : silent (S4U, no console window)"
Write-Host ""
Write-Host "Inspect   : schtasks /query /tn $TaskName /v /fo LIST"
Write-Host "Run now   : schtasks /run   /tn $TaskName"
Write-Host "Unregister: .\scripts\unregister_task.ps1"
