# DEPRECATED 2026-05-09 -- superseded by install_service.ps1 (NSSM-wrapped
# Windows Service). The Task Scheduler approach proved fragile (silent
# uninstalls, user-context binding issues). Kept as a fallback only.
#
# Registers the watchdog as a per-user Task Scheduler entry that fires on logon.
#
# Usage (run once, no admin needed):
#   pwsh -ExecutionPolicy Bypass -File install_watchdog.ps1
#
# To uninstall:  pwsh -ExecutionPolicy Bypass -File uninstall_watchdog.ps1

[CmdletBinding()]
param(
    [string]$TaskName = 'FYF_Watchdog'
)

$ErrorActionPreference = 'Stop'

$watchdog = (Resolve-Path "$PSScriptRoot\watchdog.ps1").Path

# Use Windows PowerShell 5.1 (powershell.exe) -- universally available; pwsh.exe
# (PS 7+) may not be on the user's PATH.
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watchdog`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Run forever, no execution-time limit; allow on battery; restart if it crashes.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Run interactively (no service-account boundary) so subprocess can find
# python.exe on the user's PATH and Get-Process can see NinjaTrader.exe.
$principal = New-ScheduledTaskPrincipal `
    -UserId   $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Description 'Auto-starts the Trade_Perf dashboard while NinjaTrader is running.'

# Replace any existing registration so re-running is safe.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task | Out-Null

Write-Host ""
Write-Host "Installed scheduled task '$TaskName'."
Write-Host "  Trigger:  At user logon"
Write-Host "  Watchdog: $watchdog"
Write-Host "  Logs:     $(Resolve-Path "$PSScriptRoot\..\data" -ErrorAction SilentlyContinue)\watchdog.log"
Write-Host ""
Write-Host "Start it now without waiting for next logon:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Stop / disable:"
Write-Host "  Stop-ScheduledTask -TaskName $TaskName        # stop current run"
Write-Host "  Disable-ScheduledTask -TaskName $TaskName     # don't fire next logon"
