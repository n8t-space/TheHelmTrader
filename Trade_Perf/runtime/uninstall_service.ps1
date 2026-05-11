# Stop and remove the Helm Dashboard Watchdog service.
#
# Run from an ELEVATED PowerShell:
#     pwsh -NoProfile -ExecutionPolicy Bypass -File runtime\uninstall_service.ps1

[CmdletBinding()]
param(
    [string]$ServiceName = 'HelmDashboardWatchdog'
)

$ErrorActionPreference = 'Stop'

$current = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($current)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must run as Administrator."
}

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "Service '$ServiceName' is not installed; nothing to do." -ForegroundColor Yellow
    return
}

$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssm) { Write-Error "NSSM not found on PATH; cannot remove the service via NSSM." }

if ($svc.Status -ne 'Stopped') {
    Write-Host "Stopping $ServiceName ..." -ForegroundColor Cyan
    & $nssm stop $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

& $nssm remove $ServiceName confirm | Out-Null
Start-Sleep -Seconds 1

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Error "Service still present after remove; check manually."
} else {
    Write-Host "Service '$ServiceName' removed." -ForegroundColor Green
}
