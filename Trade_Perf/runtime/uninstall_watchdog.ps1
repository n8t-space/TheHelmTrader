# Removes the FYF_Watchdog scheduled task and stops it if running.

[CmdletBinding()]
param([string]$TaskName = 'FYF_Watchdog')

$ErrorActionPreference = 'Continue'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "Task '$TaskName' not registered."
    return
}

try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch { }
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'."
Write-Host "Note: any uvicorn process the watchdog spawned is still running. Stop it via Task Manager or:"
Write-Host "  Get-NetTCPConnection -LocalPort 8000 | ForEach-Object { Stop-Process -Id `$_.OwningProcess -Force }"
