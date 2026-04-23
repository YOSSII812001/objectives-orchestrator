# Unregister Objectives Orchestrator scheduled task
# Usage: powershell .\scripts\unregister_task.ps1

$TaskName = "ObjectivesOrchestrator"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $existing) {
    Write-Host "[SKIP] Task not found: $TaskName"
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "[OK] Task unregistered: $TaskName"
